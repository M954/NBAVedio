"""TweetVideoAgent - 推特短视频生成器
将球星推特截图 + 中文翻译 + 配音 + 氛围音乐合成为竖屏短视频
"""
import os
import re
import uuid
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from moviepy import (
    ImageClip,
    AudioFileClip,
    AudioClip,
    CompositeAudioClip,
    CompositeVideoClip,
    concatenate_videoclips,
    vfx,
    afx,
)
from agents.music_provider import MusicProvider
from agents.music_searcher import MusicSearcher
from agents.voice_actor import VoiceActor


# 竖屏分辨率 9:16
WIDTH = 1080
HEIGHT = 1920


def _get_font(size=36, bold=False):
    """加载中文字体"""
    paths = [
        r"C:\Windows\Fonts\msyhbd.ttc",  # 微软雅黑粗体
        r"C:\Windows\Fonts\msyh.ttc",    # 微软雅黑
        r"C:\Windows\Fonts\simhei.ttf",  # 黑体
    ]
    if not bold:
        paths = paths[1:] + paths[:1]
    for fp in paths:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                continue
    return ImageFont.load_default(size=size)


def _strip_emoji(text):
    """移除 emoji 字符（避免字体不支持显示为方块）"""
    result = []
    for ch in text:
        cp = ord(ch)
        # 跳过常见 emoji 区间
        if cp > 0xFFFF:  # 补充平面（大部分 emoji）
            continue
        if 0xFE00 <= cp <= 0xFE0F:  # 变体选择符
            continue
        if 0x2600 <= cp <= 0x27BF:  # 杂项符号
            continue
        if 0x200D == cp:  # ZWJ
            continue
        result.append(ch)
    return "".join(result).strip()


def _wrap_text(text, font, max_width):
    """智能换行：中文逐字、英文按词，避免断词重叠"""
    words = []
    buf = ""
    for ch in text:
        if ch == ' ':
            if buf:
                words.append(buf)
                buf = ""
            words.append(' ')
        elif '\u4e00' <= ch <= '\u9fff' or ch in '，。！？、；：""''（）—…':
            if buf:
                words.append(buf)
                buf = ""
            words.append(ch)
        else:
            buf += ch
    if buf:
        words.append(buf)

    lines = []
    current = ""
    for word in words:
        test = current + word
        bbox = font.getbbox(test)
        if bbox[2] - bbox[0] > max_width and current.strip():
            lines.append(current.rstrip())
            current = word.lstrip()
        else:
            current = test
    if current.strip():
        lines.append(current.rstrip())
    return lines


class TweetVideoAgent:
    """
    生成推特短视频（竖屏 1080x1920，10+秒，含背景音乐）
    
    输入：截图路径列表 + 翻译内容列表
    输出：视频文件路径
    """

    def __init__(self, output_dir="d:/vedio/output/tweet_videos"):
        self.output_dir = output_dir
        self.audio_dir = os.path.join(output_dir, "audio")
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.audio_dir, exist_ok=True)
        self.music = MusicProvider(self.audio_dir)
        self.music_searcher = MusicSearcher()
        self.voice = VoiceActor(self.audio_dir, voice="zh-CN-YunyangNeural")

    def _create_frame(self, screenshot_path, translation="", author=""):
        """
        将推特截图组合为一帧竖屏画面（截图居中，不显示翻译）
        
        布局:
        ┌─────────────┐
        │   暗色背景    │
        │              │
        │              │
        │  推特截图     │  (垂直水平居中, 带阴影)
        │              │
        │              │
        │              │  (底部留白给字幕叠加)
        └─────────────┘
        """
        # 创建暗色渐变背景
        bg = Image.new("RGB", (WIDTH, HEIGHT), (15, 15, 25))
        draw = ImageDraw.Draw(bg)
        
        # 渐变背景
        for y in range(HEIGHT):
            ratio = y / HEIGHT
            r = int(15 * (1 - ratio) + 8 * ratio)
            g = int(15 * (1 - ratio) + 12 * ratio)
            b = int(25 * (1 - ratio) + 35 * ratio)
            draw.line([(0, y), (WIDTH, y)], fill=(r, g, b))

        # 加载推特截图
        try:
            tweet_img = Image.open(screenshot_path).convert("RGB")
        except Exception as e:
            raise ValueError(f"无法加载截图: {screenshot_path}: {e}")

        # 截图适配竖屏：宽度占屏幕 90%，高度自适应
        target_w = int(WIDTH * 0.90)
        tw, th = tweet_img.size
        scale = target_w / tw
        target_h = int(th * scale)
        # 放宽最大高度限制（不再需要翻译区域）
        max_h = int(HEIGHT * 0.70)
        if target_h > max_h:
            scale = max_h / th
            target_w = int(tw * scale)
            target_h = max_h
        tweet_img = tweet_img.resize((target_w, target_h), Image.Resampling.LANCZOS)

        # 截图垂直居中（稍微偏上，底部留空间给字幕）
        tweet_x = (WIDTH - target_w) // 2
        tweet_y = (HEIGHT - target_h) // 2 - 120  # 偏上120px，给字幕留空间

        # 给截图添加阴影
        shadow_offset = 8
        shadow = Image.new("RGB", (target_w + shadow_offset * 2, target_h + shadow_offset * 2), (5, 5, 15))
        bg.paste(shadow, (tweet_x + shadow_offset // 2, tweet_y + shadow_offset // 2))
        
        # 白色边框
        border = 4
        border_img = Image.new("RGB", (target_w + border * 2, target_h + border * 2), (50, 50, 60))
        bg.paste(border_img, (tweet_x - border, tweet_y - border))
        bg.paste(tweet_img, (tweet_x, tweet_y))

        return bg

    @staticmethod
    def _split_sentences(text):
        """将解说词拆分为短句，用于逐句展示字幕"""
        clean = _strip_emoji(text).strip()
        if not clean:
            return []
        # 按中文句号、逗号等拆分，保留有意义的片段
        parts = re.split(r'[。！？；\n]+', clean)
        sentences = []
        for p in parts:
            p = p.strip()
            if not p:
                continue
            # 如果句子太长（>20字），再按逗号拆
            if len(p) > 20:
                sub = re.split(r'[，、]+', p)
                buf = ""
                for s in sub:
                    s = s.strip()
                    if not s:
                        continue
                    if buf and len(buf) + len(s) > 20:
                        sentences.append(buf)
                        buf = s
                    else:
                        buf = buf + "，" + s if buf else s
                if buf:
                    sentences.append(buf)
            else:
                sentences.append(p)
        return sentences

    def _render_subtitle_frame(self, text, width=WIDTH, height=160):
        """渲染一帧透明背景的字幕图片（RGBA）"""
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        font = _get_font(40, bold=True)

        # 计算文字宽度居中
        lines = _wrap_text(text, font, width - 120)
        total_h = len(lines) * 56
        y = (height - total_h) // 2

        for line in lines[:3]:
            bbox = font.getbbox(line)
            tw = bbox[2] - bbox[0]
            x = (width - tw) // 2

            # 描边效果（文字阴影）
            for dx in [-2, -1, 0, 1, 2]:
                for dy in [-2, -1, 0, 1, 2]:
                    if dx == 0 and dy == 0:
                        continue
                    draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0, 180))
            # 白色主文字
            draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
            y += 56

        return img

    def generate(self, images, translations, authors=None, mood="chill",
                 duration=12.0, output_name=None, commentary=None,
                 song_query=None):
        """
        生成推特短视频（逐句字幕版）
        
        Args:
            images: 推特截图路径列表
            translations: 对应的中文翻译列表
            authors: 对应的作者名列表（可选）
            mood: 背景音乐氛围 ("chill", "hype", "emotional")
            duration: 视频总时长（秒）
            output_name: 输出文件名（可选）
            commentary: 解说词列表（替代纯翻译作为配音内容）
            song_query: 搜索的歌曲名（如 "Lose Yourself - Eminem"）
            
        Returns:
            str: 生成的视频文件路径
        """
        if not images:
            raise ValueError("至少需要一张截图")
        if len(translations) < len(images):
            translations = translations + [""] * (len(images) - len(translations))
        if not authors:
            authors = [""] * len(images)
        elif len(authors) < len(images):
            authors = authors + [""] * (len(images) - len(authors))

        # 1. 拆分解说词为短句，逐句生成 TTS
        narration_texts = commentary if commentary else translations
        full_text = narration_texts[0] if narration_texts else ""
        sentences = self._split_sentences(full_text)
        if not sentences:
            sentences = ["请关注详细内容"]

        # 为每句话生成 TTS
        sentence_audio = []  # [(sentence_text, audio_path, duration)]
        for i, sent in enumerate(sentences):
            tts_file = f"tts_{uuid.uuid4().hex[:8]}.mp3"
            try:
                tts_path = self.voice.synthesize_segment(sent, tts_file, version=5)
                if tts_path and os.path.exists(tts_path):
                    ac = AudioFileClip(tts_path)
                    sentence_audio.append((sent, tts_path, ac.duration))
                    ac.close()
            except Exception:
                pass

        # 计算总配音时长
        narration_dur = sum(d for _, _, d in sentence_audio)

        # 视频时长 = max(配音时长 + 3秒缓冲, 原始 duration)
        actual_duration = max(narration_dur + 3.0, duration)

        # 2. 获取背景音乐
        bgm_path = None
        if song_query:
            print(f"  [Music] 搜索: {song_query}")
            bgm_path = self.music_searcher.search_and_download(
                song_query, duration=int(actual_duration + 5)
            )
            if bgm_path:
                print(f"  [Music] 已获取真实歌曲配乐")

        if not bgm_path:
            bgm_name = f"bgm_{uuid.uuid4().hex[:8]}.wav"
            bgm_path = self.music.generate(
                duration=actual_duration + 3,
                mood=mood,
                output_name=bgm_name,
            )

        # 3. 生成背景帧（截图居中，无翻译文字）
        frame = self._create_frame(images[0], "", authors[0] if authors else "")
        frame_path = os.path.join(self.output_dir, "frame_0.png")
        frame.save(frame_path, quality=95)

        bg_clip = ImageClip(frame_path).with_duration(actual_duration)
        bg_clip = bg_clip.with_effects([vfx.FadeIn(0.5), vfx.FadeOut(0.5)])

        # 4. 构建逐句字幕（读一句展示一句）
        subtitle_clips = []
        # 字幕位置：屏幕偏下
        sub_y = HEIGHT - 280

        offset = 1.0  # 配音延迟1秒
        tts_parts = []

        for sent_text, audio_path, audio_dur in sentence_audio:
            # 渲染字幕帧
            sub_img = self._render_subtitle_frame(sent_text)
            sub_path = os.path.join(self.output_dir, f"sub_{uuid.uuid4().hex[:6]}.png")
            sub_img.save(sub_path)

            # 字幕 clip：与该句配音同步，多留0.3秒展示
            sub_clip = (
                ImageClip(sub_path, transparent=True)
                .with_duration(audio_dur + 0.3)
                .with_position(("center", sub_y))
                .with_start(offset)
                .with_effects([vfx.FadeIn(0.15), vfx.FadeOut(0.15)])
            )
            subtitle_clips.append(sub_clip)

            # 配音 clip
            tts_clip = AudioFileClip(audio_path).with_start(offset)
            tts_parts.append(tts_clip)

            offset += audio_dur + 0.4  # 句间间隔0.4秒

        # 5. 合成视频：背景 + 字幕叠加
        video = CompositeVideoClip(
            [bg_clip] + subtitle_clips,
            size=(WIDTH, HEIGHT),
        ).with_duration(actual_duration)

        # 6. 混合音频：配音(前景) + BGM(背景)
        bgm_raw = AudioFileClip(bgm_path)
        if bgm_raw.duration < actual_duration:
            from moviepy import afx as _afx
            bgm_audio = bgm_raw.with_effects([_afx.AudioLoop(duration=actual_duration)])
        else:
            bgm_audio = bgm_raw.with_duration(actual_duration)

        if tts_parts:
            bgm_quiet = bgm_audio.with_effects([afx.MultiplyVolume(0.2)])
            mixed = CompositeAudioClip([bgm_quiet] + tts_parts)
            mixed = mixed.with_duration(actual_duration)
            video = video.with_audio(mixed)
        else:
            video = video.with_audio(bgm_audio)

        # 7. 渲染输出
        if not output_name:
            output_name = f"tweet_{uuid.uuid4().hex[:8]}.mp4"
        output_path = os.path.join(self.output_dir, output_name)

        video.write_videofile(
            output_path,
            fps=24,
            codec="libx264",
            audio_codec="aac",
            logger=None,
        )

        # 清理临时文件
        if os.path.exists(frame_path):
            try:
                os.remove(frame_path)
            except Exception:
                pass
        for f in os.listdir(self.output_dir):
            if f.startswith("sub_") and f.endswith(".png"):
                try:
                    os.remove(os.path.join(self.output_dir, f))
                except Exception:
                    pass

        return output_path
