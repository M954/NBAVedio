"""ffmpeg filter_complex 直接合成视频，绕开 MoviePy 逐帧路径。

参数语义与 tweet_video_agent.py 步骤5-7 等价：
- 背景：源视频(可选) + 静态截图
- N 张字幕 PNG 透明叠加（带 fade in/out 0.15s）
- BGM (循环)，TTS (各句 start)，可选高光段原音
- 输出 H.264 + AAC, 1080×1920 @24fps

调用方负责把素材准备好（字幕 PNG 已渲染、TTS wav 已生成、BGM 路径已确定）。
本模块只做最后的合成 + 编码。
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import time
from typing import Optional


def _find_ffmpeg() -> str:
    cands = glob.glob(os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\ffmpeg-*-full_build\bin\ffmpeg.exe"
    ))
    if cands:
        return cands[0]
    return shutil.which("ffmpeg") or "ffmpeg"


def render(
    *,
    output_path: str,
    width: int,
    height: int,
    fps: int,
    actual_duration: float,
    intro_duration: float,
    frame_path: str,
    source_video: Optional[str],
    bgm_path: str,
    bgm_volume: float,
    subtitles: list,
    tts: list,
    highlight_audio: list,
    codec: str = "h264_nvenc",
    encoder_params: Optional[list] = None,
    ffmpeg_bin: Optional[str] = None,
    logger=None,
) -> float:
    """合成并编码到 output_path，返回耗时(秒)。

    subtitles: [{text, start, duration, png, y, kind}]
    tts:       [{path, start}]
    highlight_audio: [{src, src_start, src_end, abs_start}]  # src 通常等于 source_video
    """
    log = logger or (lambda *a, **k: None)
    ffmpeg_bin = ffmpeg_bin or _find_ffmpeg()

    inputs: list = []
    next_idx = 0

    def add_input(*args):
        nonlocal next_idx
        idx = next_idx
        inputs.extend(args)
        next_idx += 1
        return idx

    # --- 输入 ---
    # 0: frame.png（intro 期/无源视频时是全程背景）
    intro_dur = intro_duration if source_video else actual_duration
    frame_idx = add_input("-loop", "1", "-t", f"{intro_dur:.3f}", "-i", frame_path)
    # 源视频
    src_idx = None
    if source_video:
        src_idx = add_input("-i", source_video)
    # 字幕 PNG
    sub_input_idxs = []
    for s in subtitles:
        sub_input_idxs.append(add_input("-loop", "1", "-t", f"{s['duration']:.3f}", "-i", s["png"]))
    # BGM
    bgm_idx = add_input("-stream_loop", "-1", "-t", f"{actual_duration:.3f}", "-i", bgm_path)
    # TTS
    tts_input_idxs = []
    for t in tts:
        tts_input_idxs.append(add_input("-i", t["path"]))

    # --- filter_complex ---
    fc = []
    # 背景视频
    fc.append(f"[{frame_idx}:v]scale={width}:{height},setsar=1,format=yuva420p,fps={fps}[bg0]")
    if source_video:
        fc.append(
            f"[{src_idx}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps}[srcv]"
        )
        fc.append(
            f"[bg0][srcv]concat=n=2:v=1:a=0,trim=duration={actual_duration:.3f},"
            f"setpts=PTS-STARTPTS[bg]"
        )
    else:
        fc.append(f"[bg0]trim=duration={actual_duration:.3f},setpts=PTS-STARTPTS[bg]")

    # 字幕链
    fade_in = 0.15
    fade_out = 0.15
    last = "bg"
    for i, s in enumerate(subtitles):
        idx = sub_input_idxs[i]
        end = s["start"] + s["duration"]
        fc.append(
            f"[{idx}:v]format=rgba,"
            f"fade=t=in:st=0:d={fade_in}:alpha=1,"
            f"fade=t=out:st={s['duration']-fade_out:.3f}:d={fade_out}:alpha=1,"
            f"setpts=PTS-STARTPTS+{s['start']:.3f}/TB[s{i}]"
        )
        out_lbl = f"v{i}"
        fc.append(
            f"[{last}][s{i}]overlay=x=(W-w)/2:y={s['y']}:"
            f"enable='between(t,{s['start']:.3f},{end:.3f})'[{out_lbl}]"
        )
        last = out_lbl
    fc.append(f"[{last}]trim=duration={actual_duration:.3f}[vout]")

    # 音频
    fc.append(
        f"[{bgm_idx}:a]volume={bgm_volume},atrim=duration={actual_duration:.3f},"
        f"asetpts=PTS-STARTPTS[abgm]"
    )
    audio_lbls = ["abgm"]
    for i, t in enumerate(tts):
        idx = tts_input_idxs[i]
        delay_ms = int(t["start"] * 1000)
        fc.append(f"[{idx}:a]adelay={delay_ms}|{delay_ms},asetpts=PTS-STARTPTS[atts{i}]")
        audio_lbls.append(f"atts{i}")
    if source_video and highlight_audio:
        for i, h in enumerate(highlight_audio):
            delay_ms = int(h["abs_start"] * 1000)
            fc.append(
                f"[{src_idx}:a]atrim={h['src_start']:.3f}:{h['src_end']:.3f},"
                f"asetpts=PTS-STARTPTS,volume=0.95,adelay={delay_ms}|{delay_ms}[ahl{i}]"
            )
            audio_lbls.append(f"ahl{i}")

    if len(audio_lbls) == 1:
        fc.append(f"[{audio_lbls[0]}]anull[aout]")
    else:
        fc.append(
            f"{''.join(f'[{l}]' for l in audio_lbls)}"
            f"amix=inputs={len(audio_lbls)}:duration=first:dropout_transition=0,"
            f"atrim=duration={actual_duration:.3f},asetpts=PTS-STARTPTS[aout]"
        )

    filter_str = ";".join(fc)

    # 编码器参数
    if codec == "h264_nvenc":
        enc = encoder_params or ["-preset", "p1", "-tune", "ll", "-rc", "constqp",
                                 "-qp", "28", "-bf", "0", "-pix_fmt", "yuv420p"]
        enc = ["-c:v", codec] + enc
    elif codec == "libx264":
        enc = encoder_params or ["-preset", "ultrafast", "-crf", "26", "-pix_fmt", "yuv420p"]
        enc = ["-c:v", codec] + enc
    else:
        enc = ["-c:v", codec] + (encoder_params or [])

    cmd = [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "warning",
           *inputs,
           "-filter_complex", filter_str,
           "-map", "[vout]", "-map", "[aout]",
           "-r", str(fps), "-t", f"{actual_duration:.3f}",
           *enc, "-c:a", "aac", "-b:a", "160k",
           output_path]

    log(f"[FFmpegRender] inputs={sum(1 for x in cmd if x == '-i')} "
        f"subs={len(subtitles)} tts={len(tts)} hl={len(highlight_audio)}")
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True)
    dt = time.time() - t0
    if r.returncode != 0:
        err = r.stderr.decode(errors="ignore")[-2000:]
        raise RuntimeError(f"ffmpeg 渲染失败 (exit {r.returncode}, {dt:.1f}s): {err}")
    return dt
