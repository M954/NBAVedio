"""TweetVideoAgent - 推特短视频生成器
将球星推特截图 + 中文翻译 + 配音 + 氛围音乐合成为竖屏短视频
"""
import os
import re
import warnings
warnings.filterwarnings("ignore", message=".*bytes wanted but 0 bytes read.*")
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


_VIDEO_CODEC_CACHE = None


def _detect_video_codec():
    """探测可用的视频编码器，优先 GPU。结果模块级缓存，只探一次。
    返回 (codec, ffmpeg_params)。"""
    global _VIDEO_CODEC_CACHE
    if _VIDEO_CODEC_CACHE is not None:
        return _VIDEO_CODEC_CACHE
    import subprocess, tempfile, shutil, glob
    ffmpeg_bin = None
    # 优先用 winget 装的完整版 (含 nvenc/qsv/amf)
    winget_glob = os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\ffmpeg-*-full_build\bin\ffmpeg.exe"
    )
    matches = glob.glob(winget_glob)
    if matches:
        ffmpeg_bin = matches[0]
    if not ffmpeg_bin:
        ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        try:
            from imageio_ffmpeg import get_ffmpeg_exe
            ffmpeg_bin = get_ffmpeg_exe()
        except Exception:
            ffmpeg_bin = "ffmpeg"
    print(f"[Codec] 使用 ffmpeg: {ffmpeg_bin}")
    candidates = [
        ("h264_nvenc", ["-preset", "p4", "-tune", "hq", "-rc", "vbr", "-cq", "23"]),
        ("h264_qsv",   ["-preset", "fast", "-global_quality", "23"]),
        ("h264_amf",   ["-quality", "speed", "-rc", "vbr_quality", "-qp_i", "23"]),
        ("libx264",    ["-preset", "veryfast", "-crf", "23"]),
    ]
    for codec, params in candidates:
        try:
            out = os.path.join(tempfile.gettempdir(), f"_codec_probe_{codec}.mp4")
            cmd = [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                   "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.5:r=24",
                   "-c:v", codec, *params, out]
            r = subprocess.run(cmd, capture_output=True, timeout=15)
            if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
                print(f"[Codec] 探测成功，使用 {codec}")
                _VIDEO_CODEC_CACHE = (codec, params)
                try: os.remove(out)
                except Exception: pass
                return _VIDEO_CODEC_CACHE
            else:
                err = (r.stderr or b"").decode(errors="ignore").strip().splitlines()[-1:] or [""]
                print(f"[Codec] {codec} 不可用: {err[0]}")
        except Exception as e:
            print(f"[Codec] {codec} 探测异常: {e}")
    _VIDEO_CODEC_CACHE = ("libx264", ["-preset", "veryfast", "-crf", "23"])
    print(f"[Codec] 全部 GPU 编码器不可用，回落 libx264 veryfast")
    return _VIDEO_CODEC_CACHE


def _get_full_ffmpeg():
    """返回完整版 ffmpeg 路径（含 nvenc/qsv），找不到则 None。供 MoviePy 切换用。"""
    import glob, shutil
    g = glob.glob(os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\ffmpeg-*-full_build\bin\ffmpeg.exe"
    ))
    if g:
        return g[0]
    sys_ff = shutil.which("ffmpeg")
    if sys_ff:
        return sys_ff
    return None


# 让 MoviePy/imageio_ffmpeg 用完整版 ffmpeg（支持 GPU 编码器）
_full_ff = _get_full_ffmpeg()
if _full_ff:
    os.environ["IMAGEIO_FFMPEG_EXE"] = _full_ff
    print(f"[Codec] MoviePy 切换到完整版 ffmpeg: {_full_ff}")


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

    def __init__(self, output_dir=None):
        if output_dir is None:
            output_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
        self.output_dir = output_dir
        self.audio_dir = os.path.join(output_dir, "audio")
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.audio_dir, exist_ok=True)
        self.music = MusicProvider(self.audio_dir)
        self.music_searcher = MusicSearcher()
        self.voice = VoiceActor(self.audio_dir, voice="zh-CN-YunxiNeural")
        self.last_subtitle_timeline = []  # [(text, start_time, duration)]

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

        # 截图适配竖屏：宽度占屏幕 95%，高度自适应
        target_w = int(WIDTH * 0.95)
        tw, th = tweet_img.size
        scale = target_w / tw
        target_h = int(th * scale)
        # 放宽最大高度限制
        max_h = int(HEIGHT * 0.80)
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
    def _chunk_subtitle_text(text, max_len=20):
        """把缺少标点的长句切成更适合逐句字幕的短片段。"""
        text = text.strip()
        if not text:
            return []

        tokens = re.findall(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*|[\u4e00-\u9fff]|[^\s]", text)
        if not tokens:
            return [text]

        chunks = []
        buf = ""
        for token in tokens:
            if not buf:
                candidate = token
            elif re.match(r"[A-Za-z0-9]", token) and re.search(r"[A-Za-z0-9]$", buf):
                candidate = f"{buf} {token}"
            else:
                candidate = buf + token

            if len(candidate) > max_len and buf:
                chunks.append(buf)
                buf = token
            else:
                buf = candidate

        if buf:
            chunks.append(buf)
        return chunks

    @staticmethod
    def _split_sentences(text):
        """将解说词拆分为短句，用于逐句展示字幕。保持语义完整，不过度拆分。"""
        clean = _strip_emoji(text).strip()
        if not clean:
            return []
        # 先按强停顿切句（句号、感叹号、问号、分号、换行）
        parts = re.split(r'[。！？!?；;\n]+', clean)
        sentences = []
        for p in parts:
            p = p.strip()
            if not p:
                continue
            # 如果段落 >20 字，按逗号拆分并合并短片段
            if len(p) > 20:
                sub_parts = [s.strip() for s in re.split(r'[，,]+', p) if s.strip()]
                merged = []
                buf = ""
                for sp in sub_parts:
                    if buf:
                        buf += "，" + sp
                    else:
                        buf = sp
                    if len(buf) >= 12:
                        merged.append(buf)
                        buf = ""
                if buf:
                    if merged:
                        merged[-1] += "，" + buf
                    else:
                        merged.append(buf)
                sentences.extend(merged)
            else:
                sentences.append(p)
        return sentences

    def _render_subtitle_frame(self, text, width=WIDTH, height=160):
        """渲染一帧透明背景的字幕图片（RGBA）"""
        font = _get_font(40, bold=True)
        line_height = 56
        vertical_padding = 12

        # 计算文字宽度居中
        lines = _wrap_text(text, font, width - 120)
        total_h = len(lines) * line_height
        frame_height = max(height, total_h + vertical_padding * 2)

        img = Image.new("RGBA", (width, frame_height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        y = max((frame_height - total_h) // 2, vertical_padding)

        for line in lines:
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
            y += line_height

        return img

    def _create_video_clip(self, video_path, target_duration=None):
        """将推文自带视频适配为 9:16 竖屏"""
        from moviepy import VideoFileClip
        clip = VideoFileClip(video_path)

        # 末尾留 0.1s 余量，避免读到不完整帧
        clip = clip.with_duration(clip.duration - 0.1)

        w, h = clip.size
        target_ratio = WIDTH / HEIGHT  # 0.5625

        current_ratio = w / h
        if current_ratio > target_ratio:
            # 横屏：裁剪左右
            new_w = int(h * target_ratio)
            x_center = w // 2
            clip = clip.cropped(x1=x_center - new_w // 2, x2=x_center + new_w // 2)
        elif current_ratio < target_ratio:
            # 竖屏偏窄或方形：裁剪上下
            new_h = int(w / target_ratio)
            y_center = h // 2
            clip = clip.cropped(y1=max(0, y_center - new_h // 2),
                                y2=min(h, y_center + new_h // 2))

        clip = clip.resized((WIDTH, HEIGHT))

        if target_duration:
            if clip.duration < target_duration:
                # 视频不够长时冻结最后一帧，不要循环重复
                clip = clip.with_effects([vfx.Freeze(t="end", total_duration=target_duration)])
            else:
                clip = clip.with_duration(target_duration)

        return clip

    def generate(self, images, translations, authors=None, mood="chill",
                 duration=12.0, output_name=None, commentary=None,
                 song_query=None, source_video=None, video_subtitles=None,
                 highlight_segments=None):
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

        import time as _t
        _t0 = _t.time()
        _last = [_t0]
        def _mark(label):
            now = _t.time()
            print(f"[Composer-计时] {label}: {now - _last[0]:.1f}s (累计 {now - _t0:.1f}s)")
            _last[0] = now

        # 1. 构建字幕序列：解说词(有TTS) + 视频字幕(无TTS，静默展示)
        narration_texts = commentary if commentary else translations
        full_text = narration_texts[0] if narration_texts else ""
        sentences = self._split_sentences(full_text)
        if not sentences:
            sentences = ["请关注详细内容"]

        # sentence_audio: [(text, audio_path_or_None, duration)]
        # audio_path=None 表示静默字幕段（仅显示翻译，不配音）
        sentence_audio = []

        # 为解说词生成 TTS
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

        # 如果有源视频且提供了 video_subtitles，在解说词之后追加静默字幕
        _video_subs = video_subtitles or []
        if _video_subs and source_video:
            for vs in _video_subs:
                vs = vs.strip().strip("（）()\"'")
                # 过滤无意义的字幕
                if not vs or len(vs) < 3:
                    continue
                vs_lower = vs.replace(" ", "")
                if any(kw in vs_lower for kw in ["无对话", "无旁白", "无字幕", "无语音", "无内容", "静音"]):
                    continue
                sentence_audio.append((vs, None, 3.0))

        # 计算总时长（包括静默字幕段）
        narration_dur = sum(d for _, _, d in sentence_audio)

        # 视频时长：有源视频时以源视频为准，不允许解说超出
        actual_duration = max(narration_dur + 3.0, duration)
        if source_video and os.path.exists(source_video):
            from moviepy import VideoFileClip as _VFC
            _src = _VFC(source_video)
            src_dur = _src.duration
            _src.close()
            actual_duration = src_dur + 5.0  # 5秒开场 + 完整源视频

            # 解说词不能超过视频时长，超过则重新生成更短的解说词
            max_narration = actual_duration - 3.0  # 留首尾各1.5s
            retry_count = 0
            while narration_dur > max_narration and retry_count < 3:
                retry_count += 1
                target_chars = int((max_narration - 2) * 4)  # 留余量
                print(f"  [Warning] 解说词 {narration_dur:.1f}s 超过视频 {max_narration:.1f}s，重新生成 (第{retry_count}次，目标{target_chars}字)")
                try:
                    from agents.ai_assistant import get_assistant
                    _ai = get_assistant()
                    shorter = _ai._call(
                        f"以下解说词的 TTS 朗读时长超过视频可承载长度，需要把它精简到 {target_chars} 字以内。\n\n"
                        f"=== 原稿（基线，必须基于此压缩，不要重写）===\n{full_text}\n\n"
                        f"压缩硬规则：\n"
                        f"1. 【最小修改原则】只删冗余、合并啰嗦表达、砍可有可无的形容词/副词；"
                        f"原稿里的开头钩子、关键事实、情绪结尾必须原样保留，不准重新创作。\n"
                        f"2. 不要换说法、不要替换近义词、不要调整句序——能删就删，删不了就别动。\n"
                        f"3. 完整性不能丢：开头-发展-结尾三段仍要齐全，结尾必须真的收住，不能砍成半截话。\n"
                        f"4. 可读性不能降：每句仍要顺口、短句、TTS 念得通。\n"
                        f"5. 标点保留：每短句以句号/感叹号/问号结尾。\n"
                        f"6. 字数严格 ≤ {target_chars} 字。\n\n"
                        f"只返回压缩后的解说词正文。"
                    )
                    if shorter and len(shorter.strip()) > 10:
                        full_text = shorter.strip().strip('"').strip("'")
                        sentences = self._split_sentences(full_text)
                        sentence_audio = []
                        for sent in sentences:
                            tts_file = f"tts_{uuid.uuid4().hex[:8]}.mp3"
                            try:
                                tts_path = self.voice.synthesize_segment(sent, tts_file, version=5)
                                if tts_path and os.path.exists(tts_path):
                                    ac = AudioFileClip(tts_path)
                                    sentence_audio.append((sent, tts_path, ac.duration))
                                    ac.close()
                            except Exception:
                                pass
                        narration_dur = sum(d for _, _, d in sentence_audio)
                        print(f"  [Info] 重新生成后解说词 {narration_dur:.1f}s")
                except Exception as e:
                    print(f"  [Error] 重新生成失败: {e}")
                    break

        _mark("步骤1 字幕序列+TTS")
        # 2. 获取背景音乐（AI选曲 → 搜索下载 → 合成）
        bgm_path = None
        _bgm_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "reference_videos", "bgm")

        # 2a. 让 AI 从本地 BGM 库选曲
        if os.path.isdir(_bgm_dir):
            try:
                from agents.ai_assistant import get_assistant
                _ai = get_assistant()
                chosen = _ai.select_bgm_from_library(
                    tweet_text=translations[0] if translations else "",
                    translation=translations[0] if translations else "",
                    author=authors[0] if authors else "",
                    bgm_dir=_bgm_dir,
                )
                if chosen:
                    bgm_path = os.path.join(_bgm_dir, chosen)
                    if os.path.exists(bgm_path):
                        print(f"  [Music] AI 选曲: {chosen}")
                    else:
                        bgm_path = None
            except Exception as e:
                print(f"  [Music] AI 选曲失败: {e}")

        # 2b. 搜索下载
        if not bgm_path and song_query:
            print(f"  [Music] 搜索: {song_query}")
            bgm_path = self.music_searcher.search_and_download(
                song_query, duration=int(actual_duration + 5)
            )
            if bgm_path:
                print(f"  [Music] 已获取在线歌曲配乐")

        # 2c. 合成
        if not bgm_path:
            bgm_name = f"bgm_{uuid.uuid4().hex[:8]}.wav"
            bgm_path = self.music.generate(
                duration=actual_duration + 3,
                mood=mood,
                output_name=bgm_name,
            )

        _mark("步骤2 BGM 获取")
        # 3. 生成背景
        frame_path = None
        INTRO_DUR = 5.0  # 截图开场时长（多停留让观众看清推文）
        if source_video and os.path.exists(source_video):
            print(f"  [Video] 使用推文自带视频: {source_video}")
            # 截图开场 → 淡入推文视频
            frame = self._create_frame(images[0], "", authors[0] if authors else "")
            frame_path = os.path.join(self.output_dir, "frame_0.png")
            frame.save(frame_path, quality=95)

            intro_clip = (
                ImageClip(frame_path)
                .with_duration(INTRO_DUR)
                .with_effects([vfx.FadeIn(0.5), vfx.FadeOut(0.5)])
            )

            video_dur = max(actual_duration - INTRO_DUR, 5.0)
            video_clip = self._create_video_clip(source_video, target_duration=video_dur)
            video_clip = (
                video_clip
                .with_start(INTRO_DUR)
                .with_effects([vfx.FadeIn(0.8), vfx.FadeOut(0.5)])
            )

            actual_duration = INTRO_DUR + video_dur
            bg_clip = CompositeVideoClip(
                [intro_clip, video_clip],
                size=(WIDTH, HEIGHT),
            ).with_duration(actual_duration)
        else:
            frame = self._create_frame(images[0], "", authors[0] if authors else "")
            frame_path = os.path.join(self.output_dir, "frame_0.png")
            frame.save(frame_path, quality=95)
            bg_clip = ImageClip(frame_path).with_duration(actual_duration)
            bg_clip = bg_clip.with_effects([vfx.FadeIn(0.5), vfx.FadeOut(0.5)])

        _mark("步骤3 背景生成")
        # 4. 构建逐句字幕（读一句展示一句，均匀分布在视频全程）
        subtitle_clips = []
        sub_bottom_margin = 120

        # 解说从 1s 开始，到视频结束前 2s
        narration_start = 1.0
        narration_end = actual_duration - 2.0
        narration_window = max(narration_end - narration_start, 5.0)

        # 计算总配音时长和自适应间隔
        total_audio_dur = sum(d for _, _, d in sentence_audio)
        n_gaps = max(len(sentence_audio) - 1, 1)
        if total_audio_dur < narration_window:
            # 有富余时间，均匀分配间隔
            extra_time = narration_window - total_audio_dur
            gap = min(extra_time / n_gaps, 1.5)  # 间隔最多 1.5s
        else:
            gap = 0.2  # 时间紧凑，最小间隔

        offset = narration_start
        tts_parts = []
        self.last_subtitle_timeline = []

        for sent_text, audio_path, audio_dur in sentence_audio:
            # 渲染字幕帧
            sub_img = self._render_subtitle_frame(sent_text)
            sub_path = os.path.join(self.output_dir, f"sub_{uuid.uuid4().hex[:6]}.png")
            sub_img.save(sub_path)
            sub_y = HEIGHT - sub_img.size[1] - sub_bottom_margin

            # 字幕 clip：与该句配音同步，多留0.3秒展示
            sub_clip = (
                ImageClip(sub_path, transparent=True)
                .with_duration(audio_dur + 0.3)
                .with_position(("center", sub_y))
                .with_start(offset)
                .with_effects([vfx.FadeIn(0.15), vfx.FadeOut(0.15)])
            )
            subtitle_clips.append(sub_clip)
            self.last_subtitle_timeline.append((sent_text, offset, audio_dur + 0.3))

            # 配音 clip（静默字幕段跳过）
            if audio_path is not None:
                tts_clip = AudioFileClip(audio_path).with_start(offset)
                tts_parts.append(tts_clip)

            offset += audio_dur + gap  # 自适应句间间隔

        _mark("步骤4 逐句字幕渲染")
        # 4b. 高光段中译字幕（来自源视频原音翻译）
        if highlight_segments and source_video and os.path.exists(source_video):
            for h in highlight_segments:
                _txt = (h.get("translation") or "").strip()
                if not _txt:
                    continue
                _s = float(h.get("start", 0))
                _e = float(h.get("end", 0))
                _dur = _e - _s
                if _dur <= 0:
                    continue
                _abs_start = INTRO_DUR + _s
                if _abs_start >= actual_duration:
                    continue
                _dur = min(_dur, actual_duration - _abs_start)
                _hl_img = self._render_subtitle_frame(_txt)
                _hl_path = os.path.join(self.output_dir, f"hl_{uuid.uuid4().hex[:6]}.png")
                _hl_img.save(_hl_path)
                _hl_y = HEIGHT - _hl_img.size[1] - sub_bottom_margin
                _hl_clip = (
                    ImageClip(_hl_path)
                    .with_duration(_dur)
                    .with_position(("center", _hl_y))
                    .with_start(_abs_start)
                    .with_effects([vfx.FadeIn(0.15), vfx.FadeOut(0.15)])
                )
                subtitle_clips.append(_hl_clip)
                self.last_subtitle_timeline.append((_txt, _abs_start, _dur))

        _mark("步骤4b 高光字幕")
        # 5. 合成视频：背景 + 字幕叠加
        video = CompositeVideoClip(
            [bg_clip] + subtitle_clips,
            size=(WIDTH, HEIGHT),
        ).with_duration(actual_duration)

        _mark("步骤5 视频合成 (CompositeVideoClip)")
        # 6. 混合音频：配音(前景) + BGM(背景)
        bgm_raw = AudioFileClip(bgm_path)
        if bgm_raw.duration < actual_duration:
            from moviepy import afx as _afx
            bgm_audio = bgm_raw.with_effects([_afx.AudioLoop(duration=actual_duration)])
        else:
            bgm_audio = bgm_raw.with_duration(actual_duration)

        if tts_parts:
            bgm_quiet = bgm_audio.with_effects([afx.MultiplyVolume(0.18)])
            audio_tracks = [bgm_quiet] + tts_parts
        else:
            audio_tracks = [bgm_audio]

        _mark("步骤6 音频混合")
        # 6b. 高光原音叠加（仅当源视频存在且有 highlight_segments）
        highlight_audio_clips = []
        _hl_src_holder = None  # 必须在渲染期间保持 reader 存活
        if (highlight_segments and source_video
                and os.path.exists(source_video)):
            try:
                from moviepy import VideoFileClip as _HVFC
                _hl_src_holder = _HVFC(source_video)
                if _hl_src_holder.audio:
                    src_audio = _hl_src_holder.audio
                    src_dur = src_audio.duration
                    for h in highlight_segments:
                        s = max(0.0, float(h.get("start", 0)))
                        e = min(src_dur, float(h.get("end", 0)))
                        if e <= s:
                            continue
                        abs_start = INTRO_DUR + s
                        if abs_start >= actual_duration:
                            continue
                        seg = (src_audio.subclipped(s, e)
                               .with_start(abs_start)
                               .with_effects([afx.MultiplyVolume(0.95)]))
                        highlight_audio_clips.append(seg)
                    print(f"  [Highlight] 叠加 {len(highlight_audio_clips)} 段原音")
                else:
                    _hl_src_holder.close()
                    _hl_src_holder = None
            except Exception as _he:
                print(f"  [Highlight] 原音叠加失败: {_he}")
                if _hl_src_holder:
                    try: _hl_src_holder.close()
                    except Exception: pass
                _hl_src_holder = None

        if highlight_audio_clips:
            audio_tracks.extend(highlight_audio_clips)

        if len(audio_tracks) > 1 or highlight_audio_clips:
            mixed = CompositeAudioClip(audio_tracks)
            mixed = mixed.with_duration(actual_duration)
            video = video.with_audio(mixed)
        else:
            video = video.with_audio(audio_tracks[0])

        _mark("步骤6b 高光原音叠加")
        # 7. 渲染输出
        if not output_name:
            output_name = f"tweet_{uuid.uuid4().hex[:8]}.mp4"
        output_path = os.path.join(self.output_dir, output_name)

        _codec, _ffmpeg_params = _detect_video_codec()
        video.write_videofile(
            output_path,
            fps=24,
            codec=_codec,
            audio_codec="aac",
            ffmpeg_params=_ffmpeg_params,
            threads=os.cpu_count() or 4,
            logger=None,
        )
        _mark(f"步骤7 写盘 (write_videofile, codec={_codec})")

        # 清理临时文件
        if frame_path and os.path.exists(frame_path):
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
