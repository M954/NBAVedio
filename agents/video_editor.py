"""VideoEditor Agent - 视频编辑：合成最终视频"""
import os
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from moviepy import (
    ImageClip,
    AudioFileClip,
    AudioClip,
    CompositeVideoClip,
    CompositeAudioClip,
    concatenate_videoclips,
    vfx,
)


def _get_font(size=36):
    for fp in [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"]:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                continue
    return ImageFont.load_default(size=size)


def _burn_subtitle(image_path, text):
    """将字幕烧录到图片底部（优化版：减少遮挡）"""
    if not text:
        return image_path

    img = Image.open(image_path).copy()
    draw = ImageDraw.Draw(img, "RGBA")
    display_text = text[:45] + "..." if len(text) > 45 else text
    font = _get_font(32)

    bbox = font.getbbox(display_text)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    y = img.height - 85
    x = (img.width - text_w) // 2
    pad = 12
    
    # 半透明黑色背景
    draw.rectangle(
        [(x - pad, y - pad), (x + text_w + pad, y + text_h + pad)],
        fill=(0, 0, 0, 180)
    )
    
    draw.text((x, y), display_text, font=font, fill=(255, 255, 255, 255))

    out_path = image_path.replace(".png", "_sub.png")
    img.save(out_path, quality=95)
    return out_path


class VideoEditor:
    """合成图片+音频+字幕为最终视频"""

    def __init__(self, output_dir):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def create_segment_clip(self, image_paths, audio_path, text="", version=1):
        """创建单个片段的视频"""
        audio = AudioFileClip(audio_path)
        audio_dur = audio.duration
        padding = 1.5 if version >= 3 else 0.5
        duration = audio_dur + padding

        # 用静音填充音频到目标时长
        silence = AudioClip(
            lambda t: np.zeros((1, 2)),
            duration=duration,
            fps=44100,
        )
        padded_audio = CompositeAudioClip([audio.with_start(0), silence])
        padded_audio = padded_audio.with_duration(duration)

        # 支持一条新闻多图（例如3张）
        if isinstance(image_paths, str):
            image_paths = [image_paths]
        image_paths = image_paths or []
        if not image_paths:
            raise RuntimeError("当前片段没有可用图片")

        sub_duration = max(duration / len(image_paths), 1.0)
        sub_clips = []
        for i, image_path in enumerate(image_paths):
            final_image = image_path
            if version >= 4 and text:
                final_image = _burn_subtitle(image_path, text)
            clip = ImageClip(final_image).with_duration(sub_duration).with_position("center")
            if len(image_paths) > 1 and version >= 4:
                clip = clip.with_effects([vfx.FadeIn(0.2), vfx.FadeOut(0.2)])
            sub_clips.append(clip)

        visual_clip = concatenate_videoclips(sub_clips, method="chain")
        visual_clip = visual_clip.with_duration(duration)

        composite = CompositeVideoClip([visual_clip], size=(1920, 1080))
        composite = composite.with_audio(padded_audio)
        composite = composite.with_duration(duration)

        # 淡入淡出 v2+
        if version >= 2:
            fade_dur = 0.5 if version >= 4 else 0.3
            composite = composite.with_effects([
                vfx.FadeIn(fade_dur),
                vfx.FadeOut(fade_dur),
            ])

        return composite

    def run(self, image_data, audio_data, version=1):
        """合成最终视频"""
        clips = []
        for img_info, aud_info in zip(image_data, audio_data):
            try:
                text = aud_info["script"]["text"] if version >= 4 else ""
                image_paths = img_info.get("paths", [img_info["path"]])
                clip = self.create_segment_clip(
                    image_paths,
                    aud_info["path"],
                    text=text,
                    version=version,
                )
                clips.append(clip)
            except Exception as e:
                print(f"  [VideoEditor] 跳过片段: {e}")

        if not clips:
            raise RuntimeError("没有可用的视频片段")

        final = concatenate_videoclips(clips, method="chain")

        output_path = os.path.join(self.output_dir, f"nba_daily_v{version}.mp4")
        print(f"[VideoEditor] 正在渲染视频 v{version}...")

        try:
            final.write_videofile(
                output_path,
                fps=24,
                codec="libx264",
                audio_codec="aac",
                preset="ultrafast",
                bitrate="4000k" if version >= 3 else "3000k",
                logger=None,
            )
        except PermissionError:
            pass  # Windows temp file cleanup issue - video is already written
        print(f"[VideoEditor] 视频已保存: {output_path}")

        # 清理
        for clip in clips:
            try:
                clip.close()
            except Exception:
                pass
        try:
            final.close()
        except Exception:
            pass

        return output_path
