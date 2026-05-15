"""按时间段拼接源视频，输出剪辑后的 MP4。

用于 Gemini 给出的 segments 物理裁剪源视频，使后续高光识别 / 字幕 / 合成
都基于裁剪后的版本。失败时由调用方决定回退策略。
"""
from __future__ import annotations

import os
from typing import Iterable, List, Tuple


def normalize_segments(
    segments: Iterable[dict],
    source_duration: float,
    max_total: float,
) -> List[Tuple[float, float]]:
    """将 Gemini 返回的 segments 规范化为 (start, end) 列表：
    - 越界裁剪到 [0, source_duration]
    - 丢弃 end <= start 的段
    - 按 start 排序，合并重叠段
    - 累计总时长不超过 max_total，超出则截断最后一段
    """
    cleaned: List[Tuple[float, float]] = []
    for seg in segments or []:
        try:
            s = float(seg.get("start", -1))
            e = float(seg.get("end", -1))
        except (TypeError, ValueError):
            continue
        s = max(0.0, min(s, source_duration))
        e = max(0.0, min(e, source_duration))
        if e - s < 0.2:
            continue
        cleaned.append((s, e))
    if not cleaned:
        return []

    cleaned.sort(key=lambda x: x[0])
    merged: List[Tuple[float, float]] = [cleaned[0]]
    for s, e in cleaned[1:]:
        ps, pe = merged[-1]
        if s <= pe:
            merged[-1] = (ps, max(pe, e))
        else:
            merged.append((s, e))

    capped: List[Tuple[float, float]] = []
    total = 0.0
    for s, e in merged:
        remain = max_total - total
        if remain <= 0.1:
            break
        dur = e - s
        if dur <= remain:
            capped.append((s, e))
            total += dur
        else:
            capped.append((s, s + remain))
            total += remain
            break
    return capped


def trim_to_segments(src_path: str, segments: List[Tuple[float, float]], out_path: str) -> str:
    """按 segments 顺序 subclip 后 concat 写出 MP4，返回输出路径。"""
    if not segments:
        raise ValueError("segments 为空，无可裁剪片段")
    from moviepy import VideoFileClip, concatenate_videoclips

    clip = VideoFileClip(src_path)
    sub_clips = []
    try:
        for s, e in segments:
            e = min(e, clip.duration)
            if e - s < 0.2:
                continue
            sub_clips.append(clip.subclipped(s, e))
        if not sub_clips:
            raise ValueError("subclip 后无有效片段")
        final = concatenate_videoclips(sub_clips, method="compose")
        try:
            os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
            final.write_videofile(
                out_path,
                codec="libx264",
                audio_codec="aac",
                logger=None,
                threads=2,
                preset="medium",
            )
        finally:
            final.close()
    finally:
        for sc in sub_clips:
            try:
                sc.close()
            except Exception:
                pass
        clip.close()
    return out_path
