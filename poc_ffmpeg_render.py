"""PoC: 用一条 ffmpeg filter_complex 命令重渲染 tweet 视频，对比 MoviePy 版本。

用法：
    python poc_ffmpeg_render.py output/tweet_xxx_v1.manifest.json [--codec h264_nvenc]

读取 manifest（由 tweet_video_agent.py 自动 dump）→ 拼 filter_complex → 调 ffmpeg。
输出文件名为 <原名>_ffmpeg.mp4，与原 MoviePy 输出可逐帧对比。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time


def _esc(p: str) -> str:
    """ffmpeg 路径需要把反斜杠转义；filter 表达式里的冒号/方括号也要转。"""
    return p.replace("\\", "/").replace(":", "\\:")


def build_command(manifest: dict, ffmpeg_bin: str, codec: str) -> tuple[list, str]:
    W, H = manifest["width"], manifest["height"]
    dur = manifest["actual_duration"]
    intro = manifest["intro_duration"]
    fps = manifest["fps"]
    frame = manifest["frame_path"]
    src = manifest.get("source_video")
    bgm = manifest["bgm_path"]
    subs = manifest["subtitles"]
    tts = manifest["tts"]
    hl_audio = manifest["highlight_audio"]

    out_path = manifest["moviepy_output"].replace(".mp4", "_ffmpeg.mp4")

    inputs: list = []
    next_idx = 0

    def add_input(*args):
        nonlocal next_idx
        idx = next_idx
        inputs.extend(args)
        next_idx += 1
        return idx

    # 0: frame.png（intro 静态图 / 无源视频时是全程背景）
    frame_idx = add_input("-loop", "1", "-t", f"{intro if src else dur:.3f}", "-i", frame)
    # 1: source_video（如有）
    src_idx = None
    if src:
        src_idx = add_input("-i", src)
    # 字幕 PNG 入参
    sub_input_idxs = []
    for s in subs:
        sub_input_idxs.append(add_input("-loop", "1", "-t", f"{s['duration']:.3f}", "-i", s["png"]))
    # 音频入参
    bgm_idx = add_input("-stream_loop", "-1", "-t", f"{dur:.3f}", "-i", bgm)
    tts_input_idxs = []
    for t in tts:
        tts_input_idxs.append(add_input("-i", t["path"]))

    # ---------- filter_complex ----------
    fc = []
    # 背景视频流
    fc.append(f"[0:v]scale={W}:{H},setsar=1,format=yuva420p,fps={fps}[bg0]")
    if src:
        # 源视频 scale 到 9:16，居中黑边
        fc.append(
            f"[1:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps}[srcv]"
        )
        # 拼接 intro + source_video
        fc.append(f"[bg0][srcv]concat=n=2:v=1:a=0,trim=duration={dur:.3f},setpts=PTS-STARTPTS[bg]")
    else:
        fc.append(f"[bg0]trim=duration={dur:.3f},setpts=PTS-STARTPTS[bg]")

    # 链式叠字幕
    last = "bg"
    for i, s in enumerate(subs):
        idx = sub_input_idxs[i]
        end = s["start"] + s["duration"]
        fade_in = 0.15
        fade_out = 0.15
        # 字幕 PNG 居中横向；y 已是绝对像素
        fc.append(
            f"[{idx}:v]format=rgba,fade=t=in:st=0:d={fade_in}:alpha=1,"
            f"fade=t=out:st={s['duration']-fade_out:.3f}:d={fade_out}:alpha=1,"
            f"setpts=PTS-STARTPTS+{s['start']:.3f}/TB[s{i}]"
        )
        out_lbl = f"v{i}"
        fc.append(
            f"[{last}][s{i}]overlay=x=(W-w)/2:y={s['y']}:"
            f"enable='between(t,{s['start']:.3f},{end:.3f})'[{out_lbl}]"
        )
        last = out_lbl
    fc.append(f"[{last}]trim=duration={dur:.3f}[vout]")

    # 音频：BGM
    bgm_vol = 0.18 if tts else 1.0
    fc.append(f"[{bgm_idx}:a]volume={bgm_vol},atrim=duration={dur:.3f},asetpts=PTS-STARTPTS[abgm]")
    audio_lbls = ["abgm"]
    # TTS
    for i, t in enumerate(tts):
        idx = tts_input_idxs[i]
        delay_ms = int(t["start"] * 1000)
        fc.append(f"[{idx}:a]adelay={delay_ms}|{delay_ms},asetpts=PTS-STARTPTS[atts{i}]")
        audio_lbls.append(f"atts{i}")
    # 高光原音
    for i, h in enumerate(hl_audio):
        # src_idx 的音轨 1:a
        delay_ms = int(h["abs_start"] * 1000)
        fc.append(
            f"[{src_idx}:a]atrim={h['src_start']:.3f}:{h['src_end']:.3f},"
            f"asetpts=PTS-STARTPTS,volume=0.95,adelay={delay_ms}|{delay_ms}[ahl{i}]"
        )
        audio_lbls.append(f"ahl{i}")

    if len(audio_lbls) == 1:
        fc.append(f"[{audio_lbls[0]}]anull[aout]")
    else:
        fc.append(f"{''.join(f'[{l}]' for l in audio_lbls)}amix=inputs={len(audio_lbls)}:duration=first:dropout_transition=0,"
                  f"atrim=duration={dur:.3f},asetpts=PTS-STARTPTS[aout]")

    filter_str = ";".join(fc)

    # 编码参数
    if codec == "h264_nvenc":
        enc = ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll",
               "-rc", "constqp", "-qp", "28", "-bf", "0", "-pix_fmt", "yuv420p"]
    else:
        enc = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-pix_fmt", "yuv420p"]

    cmd = [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "warning",
           *inputs,
           "-filter_complex", filter_str,
           "-map", "[vout]", "-map", "[aout]",
           "-r", str(fps), "-t", f"{dur:.3f}",
           *enc, "-c:a", "aac", "-b:a", "160k",
           out_path]
    return cmd, out_path


# （inputs_count 已被 add_input 闭包计数器替换，无需保留）


def find_ffmpeg() -> str:
    import glob
    cands = glob.glob(os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\ffmpeg-*-full_build\bin\ffmpeg.exe"
    ))
    if cands:
        return cands[0]
    import shutil
    return shutil.which("ffmpeg") or "ffmpeg"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--codec", default="h264_nvenc")
    args = ap.parse_args()

    with open(args.manifest, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    ffmpeg_bin = find_ffmpeg()
    print(f"[PoC] ffmpeg: {ffmpeg_bin}")
    cmd, out_path = build_command(manifest, ffmpeg_bin, args.codec)

    # 调试：打印命令长度（filter_complex 可能很长）
    print(f"[PoC] inputs: {sum(1 for x in cmd if x == '-i')} 个 -i")
    print(f"[PoC] filter_complex 长度: {len([c for c in cmd if c.startswith('[')]):d} 字符")
    print(f"[PoC] 输出: {out_path}")

    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True)
    dt = time.time() - t0
    if r.returncode != 0:
        print(f"[PoC] ffmpeg 失败 (exit {r.returncode}, {dt:.1f}s)")
        print("--- stderr ---")
        print(r.stderr.decode(errors="ignore")[-3000:])
        sys.exit(1)
    size_mb = os.path.getsize(out_path) / 1024 / 1024
    print(f"[PoC] OK 完成，耗时 {dt:.1f}s, 文件 {size_mb:.1f}MB")
    print(f"[PoC] MoviePy 输出: {manifest['moviepy_output']}")
    print(f"[PoC] FFmpeg 输出: {out_path}")


if __name__ == "__main__":
    main()
