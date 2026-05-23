"""可复用的推文视频生成内核。

本模块从 ``tweet_api.py:_do_generate_ai_inner`` 抽取而来，提供与原函数等价的
端到端流水线（步骤 1-8：翻译优化 → 视频分析+字幕 → Research+解说词 →
配乐+高光 → 迭代生成审阅 → 重写+BGM 改进 → finalize）。

设计原则：
* 不依赖 ``tweet_api`` 模块内的任何全局状态（取消标志、日志缓存、executor 等）。
* ``ai`` / ``agent`` 由调用方注入，便于测试与复用。
* 通过 ``logger`` 注入日志通道；默认实现走 ``print``。
* 通过 ``on_cancel`` 回调实现取消支持（可为 ``None``，等价于不取消）。
"""

from __future__ import annotations

import os
import shutil
import time as _t
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# 公共数据结构
# ---------------------------------------------------------------------------


@dataclass
class PipelineResult:
    """``run_pipeline`` 的标准化返回值。"""

    video_path: str
    final_name: str
    score: int
    grade: str
    selected_round: int
    total_rounds: int
    polished_translations: list
    final_commentary: str
    final_song: Optional[str]
    recommended_mood: str
    original_translation: str
    rounds_log: list
    full_review: dict
    cancelled: bool = False
    video_description: str = ""


# ---------------------------------------------------------------------------
# 私有工具
# ---------------------------------------------------------------------------


def _default_logger(msg: str, level: str = "info") -> None:
    print(f"[{level}] {msg}")


def _collect_video_info(video_path: str) -> dict:
    """提取视频基础信息（duration / resolution / audio / file_size_mb）。

    ``VideoFileClip`` 必须在函数内部 close，避免句柄泄漏。"""
    from moviepy import VideoFileClip

    clip = VideoFileClip(video_path)
    try:
        info = {
            "duration": round(clip.duration, 1),
            "resolution": f"{clip.size[0]}x{clip.size[1]}",
            "has_audio": clip.audio is not None,
            "file_size_mb": round(os.path.getsize(video_path) / (1024 * 1024), 2),
        }
    finally:
        clip.close()
    return info


def _rewrite_commentary(
    *,
    ai,
    cur_commentary: str,
    review: dict,
    suggestions: list,
    target_video_duration: float,
    author0: str,
    orig0: str,
    polished: list,
    video_description: str,
    logger: Callable[..., None],
) -> str:
    """根据审阅反馈重写解说词；失败则返回原解说词。"""
    _content_issues = review.get("content_issues", []) or []
    _sub_mismatches = review.get("subtitle_mismatches", []) or []
    _suggestions = suggestions or []
    _details = review.get("details", {}) or {}

    # 字数硬约束：4 字/秒，预留首尾 3 秒，±20
    _avail = max(target_video_duration - 3, 8)
    _tgt_chars = int(_avail * 4)
    _min_chars = max(_tgt_chars - 20, 60)
    _max_chars = _tgt_chars + 20

    try:
        from agents.style_guide import (
            PLAYER_NICKNAMES as _PN,
            FORBIDDEN_WORDS as _FW,
        )

        _nick_table = "\n".join(f"  {e} = {c}" for e, c in _PN.items() if c)
        _forbid = "、".join(_FW)
    except Exception:
        _nick_table = ""
        _forbid = ""

    try:
        rewrite_prompt = (
            f"你是篮球邮差Melo风格NBA短视频博主。上一版解说词被审阅 agent 扣分了，"
            f"请根据反馈彻底修复所有被点名的问题，写出一版高质量的新解说词。\n\n"
            f"=== 审阅反馈（这是本次重写的核心依据，每一条都必须修）===\n"
            f"评分明细: {_details}\n"
            f"内容事实问题: {_content_issues if _content_issues else '无'}\n"
            f"字幕/画面错位: {_sub_mismatches if _sub_mismatches else '无'}\n"
            f"改进建议: {_suggestions if _suggestions else '无'}\n\n"
            f"=== 上一版解说词（仅供参考，可大改可重写，目标是修掉上面所有问题）===\n{cur_commentary}\n\n"
            f"=== 推文上下文 ===\n"
            f"作者: {author0}\n"
            f"原文: {orig0}\n"
            f"翻译: {polished[0] if polished else ''}\n"
        )
        if video_description:
            rewrite_prompt += f"视频内容: {video_description}\n"
        rewrite_prompt += (
            f"\n=== 重写硬规则 ===\n"
            f"⚠️ 解说词是整个视频的灵魂，必须同时满足【完整】+【顺畅可读】：\n"
            f"  - 完整：开头-发展-结尾三段齐全，核心事实交代完，结尾真的收住，不能戛然而止\n"
            f"  - 可读：念出来自然顺口，短句优先(8-20字)，禁止翻译腔/堆砌定语，朗读不卡壳\n\n"
            f"1. 【绝对优先】审阅反馈里的每一条事实错误、字幕错位、改进建议都必须修掉，不能漏\n"
            f"2. 必须忠实于推文原文事实；不能添油加醋、张冠李戴、编造细节\n"
            f"3. 如果有视频，解说词必须与视频画面/对白一致，不得描述画面里没有的东西\n"
            f"4. 字数严格在 {_min_chars}-{_max_chars} 字之间（视频 {target_video_duration:.0f}s，超长会被截）\n"
            f"5. 开头第一句必须是'XXX今日发推/转推'+情绪钩子；结尾必须收住（个人观点/反问/价值判断）\n"
            f"6. 必须使用口语词：真的、太、算是、天啊、好家伙、没得说、直接、拉满\n"
            f"7. 绝对禁用书面套话：{_forbid}\n"
            f"8. 标点：每短句以句号/感叹号/问号结尾，句内停顿用逗号，禁止用空格代替标点\n"
            f"9. 球员译名严格使用下表，禁止生造：\n{_nick_table}\n"
            f"   表外球员用国内主流篮球媒体通用音译，不确定就用全名音译\n\n"
            f"只返回修订后的解说词正文，不要前言、不要diff、不要解释。提交前对照审阅反馈逐条核对，"
            f"确认【所有反馈都修了 + 完整 + 顺口 + 字数达标】才提交。"
        )
        improved = ai._call(rewrite_prompt)
        if improved and len(improved.strip()) > 10:
            new_commentary = improved.strip().strip('"').strip("'")
            logger(f"  解说词已重写: {new_commentary}")
            return new_commentary
    except Exception as e:
        logger(f"  解说词重写失败: {e}", "warn")
    return cur_commentary


def _improve_bgm(
    *,
    ai,
    suggestions: list,
    cur_song: Optional[str],
    orig0: str,
    polished: list,
    author0: str,
) -> Optional[str]:
    """根据审阅建议尝试改进 BGM；返回新的 ``cur_song``（可能为 None 表示走库）。"""
    for s in suggestions:
        if "配乐" in s or "歌曲" in s or "音乐" in s or "BGM" in s or "合成" in s:
            try:
                _bgm_dir = os.path.join(
                    os.path.dirname(__file__), "..", "reference_videos", "bgm"
                )
                _bgm_dir = os.path.abspath(_bgm_dir)
                new_bgm = ai.select_bgm_from_library(
                    orig0, polished[0] if polished else "", author0, _bgm_dir
                )
                if new_bgm:
                    return None  # 走 BGM 库
                else:
                    new_song = ai.recommend_song(orig0, polished[0], author0)
                    if new_song and new_song != cur_song:
                        return new_song
            except Exception:
                pass
            break
    return cur_song


def _iterate_with_review(
    *,
    rnd: int,
    request_id: str,
    agent,
    ai,
    saved_paths: list,
    polished: list,
    author_list: Optional[list],
    mood: str,
    duration: float,
    cur_commentary: str,
    cur_song: Optional[str],
    saved_video_path: Optional[str],
    video_subtitles: list,
    highlight_segments: list,
    orig0: str,
    author0: str,
    video_description: str,
    context_brief: str,
    logger: Callable[..., None],
    on_cancel: Optional[Callable[[], bool]],
) -> tuple:
    """单轮：generate + 审阅。返回 (video_path, review, cancelled_before_review)。"""
    output_name = f"tweet_{request_id}_v{rnd}.mp4"
    _gen_t = _t.time()

    if on_cancel and on_cancel():
        return None, None, True

    video_path = agent.generate(
        images=saved_paths,
        translations=polished,
        authors=author_list,
        mood=mood,
        duration=duration,
        output_name=output_name,
        commentary=[cur_commentary],
        song_query=cur_song,
        source_video=saved_video_path,
        video_subtitles=video_subtitles,
        highlight_segments=highlight_segments,
    )
    logger(f"  视频生成耗时: {_t.time() - _gen_t:.1f}s")

    if on_cancel and on_cancel():
        # 视频已生成但跳过审阅
        return video_path, None, True

    _rev_t = _t.time()
    vinfo = _collect_video_info(video_path)
    info = {
        "commentary": cur_commentary,
        "translation": polished[0] if polished else "",
        "original_text": orig0,
        "author": author0,
        "video_description": video_description,
        "context_brief": context_brief,
        "bgm_song": cur_song or "BGM库",
        "mood": mood,
        "has_narration": True,
        "has_source_video": saved_video_path is not None,
        **vinfo,
    }

    try:
        review = ai.review_video(
            info,
            video_path=video_path,
            subtitle_timeline=agent.last_subtitle_timeline,
        )
    except Exception as e:
        review = {"score": 70, "grade": "C", "suggestions": [str(e)]}
    logger(f"  审阅耗时: {_t.time() - _rev_t:.1f}s")
    return video_path, review, False


def _finalize(
    *,
    request_id: str,
    agent,
    best_video: Optional[str],
    best_round: Optional[dict],
    cur_commentary: str,
    cur_song: Optional[str],
    max_rounds: int,
    best_review: dict,
    rounds_log: list,
    polished: list,
    trans_list: list,
    mood: str,
    video_description: str,
    cancelled: bool,
    logger: Callable[..., None],
    output_dir: Optional[str],
) -> PipelineResult:
    """拷贝最佳成片到最终路径并构造 PipelineResult。"""
    final_name = f"tweet_{request_id}.mp4"
    out_dir = output_dir or agent.output_dir
    final_path = os.path.join(out_dir, final_name)
    if best_video and best_video != final_path:
        try:
            shutil.copy2(best_video, final_path)
        except Exception as e:
            logger(f"  最终拷贝失败: {e}", "warn")

    final_commentary = best_round["commentary"] if best_round else cur_commentary
    final_song = best_round["song"] if best_round else cur_song
    final_round = best_round["round"] if best_round else max_rounds
    logger(f"[generate-ai] 采用第{final_round}轮作为最终成片")

    return PipelineResult(
        video_path=final_path,
        final_name=final_name,
        score=int(best_review.get("score", 0) or 0),
        grade=str(best_review.get("grade", "F")),
        selected_round=final_round,
        total_rounds=len(rounds_log),
        polished_translations=polished,
        final_commentary=final_commentary,
        final_song=final_song,
        recommended_mood=mood,
        original_translation=trans_list[0] if trans_list else "",
        rounds_log=rounds_log,
        full_review=best_review,
        cancelled=cancelled,
        video_description=video_description,
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def run_pipeline(
    saved_paths: list,
    trans_list: list,
    *,
    saved_video_path: Optional[str] = None,
    author_list: Optional[list] = None,
    orig_list: Optional[list] = None,
    duration: float = 12.0,
    max_rounds: int = 3,
    request_id: str = "",
    highlight: bool = False,
    tweet_id: str = "",
    ai,
    agent,
    logger: Optional[Callable[..., None]] = None,
    on_cancel: Optional[Callable[[], bool]] = None,
    output_dir: Optional[str] = None,
) -> PipelineResult:
    """执行完整的 AI 视频生成流水线（步骤 1-8）。

    与 ``tweet_api._do_generate_ai_inner`` 行为等价，但完全不依赖 tweet_api
    的全局状态。``ai`` / ``agent`` 必须由调用方注入。"""

    log = logger or _default_logger
    if not request_id:
        request_id = uuid.uuid4().hex[:8]

    orig0 = orig_list[0] if orig_list else ""
    author0 = author_list[0] if author_list else ""
    _pipeline_start = _t.time()

    # ---- 步骤 1: AI 优化翻译 ----
    _step_t = _t.time()
    log("[generate-ai] 步骤1: 优化翻译")
    polished = []
    for i, trans in enumerate(trans_list):
        orig = orig_list[i] if orig_list and i < len(orig_list) else ""
        try:
            result = ai.polish_translation(orig, trans)
            polished.append(result)
            log(f"  翻译优化: {trans} → {result}")
        except Exception as e:
            polished.append(trans)
            log(f"  翻译优化失败: {e}", "warn")
    log(f"[generate-ai] 步骤1完成，耗时 {_t.time() - _step_t:.1f}s")

    # ---- 步骤 2: 视频内容分析 + 字幕提取（无源视频时降级）----
    video_description = ""
    video_subtitles: list = []
    if saved_video_path:
        _step_t = _t.time()
        log("[generate-ai] 步骤2: 分析推文视频内容")

        # 2a: 判断是否需要剪辑（仅当源视频 > MAX_SOURCE_VIDEO_DURATION）
        try:
            from config import MAX_SOURCE_VIDEO_DURATION as _MAX_SRC
        except Exception:
            _MAX_SRC = 50.0
        _src_dur = 0.0
        try:
            _src_dur = _collect_video_info(saved_video_path).get("duration", 0.0) or 0.0
        except Exception as e:
            log(f"[generate-ai] 读取源视频时长失败: {e}", "warn")

        want_trim = _src_dur > _MAX_SRC
        if want_trim:
            log(f"[Trim] 源视频 {_src_dur:.1f}s > {_MAX_SRC:.0f}s，请求 Gemini 给出剪辑方案")

        analysis = None
        try:
            analysis = ai.analyze_video_content(
                saved_video_path, orig0, author0,
                want_trim_plan=want_trim,
                max_trim_total=_MAX_SRC,
            )
        except Exception as e:
            log(f"[generate-ai] 视频分析失败: {e}", "warn")

        # 拆分 description 与 trim_plan（兼容老返回值）
        trim_segments_raw: list = []
        needs_trim = False
        trim_reason = "none"
        if isinstance(analysis, dict):
            video_description = analysis.get("description", "") or ""
            needs_trim = bool(analysis.get("needs_trim"))
            trim_reason = analysis.get("trim_reason") or "none"
            trim_segments_raw = analysis.get("segments") or []
        elif isinstance(analysis, str):
            video_description = analysis

        # 2b: 执行剪辑（仅当 want_trim 触发）
        if want_trim:
            try:
                from utils.video_trim import normalize_segments, trim_to_segments

                segs = normalize_segments(trim_segments_raw, _src_dur, _MAX_SRC)
                if needs_trim and segs:
                    out_dir = os.path.dirname(saved_video_path) or "."
                    base, ext = os.path.splitext(os.path.basename(saved_video_path))
                    trimmed_path = os.path.join(out_dir, f"{base}_trimmed{ext or '.mp4'}")
                    log(
                        f"[Trim] Gemini 方案 reason={trim_reason}, 段数={len(segs)}, "
                        f"总时长={sum(e - s for s, e in segs):.1f}s → {trimmed_path}"
                    )
                    trim_to_segments(saved_video_path, segs, trimmed_path)
                    saved_video_path = trimmed_path
                    log(f"[Trim] 已替换 saved_video_path → {trimmed_path}")
                else:
                    # 兜底：硬截断到 MAX_SRC
                    log(
                        f"[Trim] Gemini 未给出可用 segments (needs_trim={needs_trim}, "
                        f"raw={len(trim_segments_raw)}段)，回退到硬截断 0-{_MAX_SRC:.0f}s",
                        "warn",
                    )
                    out_dir = os.path.dirname(saved_video_path) or "."
                    base, ext = os.path.splitext(os.path.basename(saved_video_path))
                    trimmed_path = os.path.join(out_dir, f"{base}_trimmed{ext or '.mp4'}")
                    trim_to_segments(saved_video_path, [(0.0, _MAX_SRC)], trimmed_path)
                    saved_video_path = trimmed_path
                    log(f"[Trim] 硬截断完成 → {trimmed_path}")
            except Exception as e:
                log(f"[Trim] 剪辑失败，沿用原片: {e}", "warn")

        if video_description:
            try:
                video_subtitles = ai.extract_video_dialogue(video_description, orig0, author0)
                if video_subtitles:
                    log(f"[generate-ai] 提取视频字幕: {video_subtitles}")
            except Exception as e:
                log(f"[generate-ai] 提取视频字幕失败: {e}", "warn")
        log(f"[generate-ai] 步骤2完成，耗时 {_t.time() - _step_t:.1f}s")

    # 计算目标视频时长
    target_video_duration = duration
    if saved_video_path:
        try:
            from moviepy import VideoFileClip as _VFC

            _vc = _VFC(saved_video_path)
            try:
                target_video_duration = max(duration, _vc.duration + 5.0)
            finally:
                _vc.close()
            log(
                f"[generate-ai] 目标视频时长: {target_video_duration:.1f}s "
                f"(源视频 {target_video_duration - 5:.1f}s + 5s开场)"
            )
        except Exception:
            pass

    # ---- 步骤 3: 解说词（含 Research）----
    _step_t = _t.time()
    log("[generate-ai] 步骤3: 生成解说词")

    context_brief = ""
    if tweet_id:
        try:
            need, refined_q = ai.needs_research(
                orig0, polished[0] if polished else "", author0,
                video_description=video_description,
            )
        except Exception as e:
            need, refined_q = False, ""
            log(f"  needs_research 调用失败: {e}", "warn")
        if not need:
            log("  [Research] 跳过（推文自身可理解）")
        else:
            try:
                from agents.research_agent import ResearchAgent, usage_this_month

                _researcher = ResearchAgent(ai=ai)
                _used, _quota = usage_this_month()
                log(
                    f"  [Research] 触发检索 (本月已用 {_used}/{_quota}); query={refined_q!r}"
                )
                _res = _researcher.research(
                    tweet_id, orig0, author0, query_override=refined_q
                )
                context_brief = _res.get("context_brief", "")
                if context_brief:
                    _head = context_brief[:120].replace("\n", " ")
                    log(f"  [Research] 拿到背景({len(context_brief)}字): {_head}...")
                else:
                    log(f"  [Research] 无结果 / {_res.get('error', '')}", "warn")
            except Exception as e:
                log(f"  [Research] 检索失败: {e}", "warn")

    commentaries = []
    for i, trans in enumerate(polished):
        orig = orig_list[i] if orig_list and i < len(orig_list) else ""
        author = author_list[i] if author_list and i < len(author_list) else ""
        try:
            c = ai.generate_commentary(
                orig, trans, author,
                has_video=saved_video_path is not None,
                video_description=video_description,
                target_duration=target_video_duration,
                context_brief=context_brief if i == 0 else "",
            )
            commentaries.append(c)
            log(f"  解说词: {c}")
        except Exception as e:
            commentaries.append(trans)
            log(f"  解说词生成失败: {e}", "warn")
    log(f"[generate-ai] 步骤3完成，耗时 {_t.time() - _step_t:.1f}s")

    # ---- 步骤 4: 配乐推荐 ----
    _step_t = _t.time()
    log("[generate-ai] 步骤4: 推荐配乐")
    song_query = None
    try:
        song_query = ai.recommend_song(orig0, polished[0] if polished else "", author0)
        log(f"  推荐歌曲: {song_query}")
    except Exception:
        pass

    try:
        mood = ai.recommend_mood(orig0, polished[0] if polished else "")
        log(f"  配乐氛围: {mood}")
    except Exception:
        mood = "chill"
    log(f"[generate-ai] 步骤4完成，耗时 {_t.time() - _step_t:.1f}s")

    # ---- 步骤 4b: 高光识别 ----
    highlight_segments: list = []
    if highlight and saved_video_path and os.path.exists(saved_video_path):
        _hl_t = _t.time()
        log("[generate-ai] 步骤4b: Gemini 识别原视频高光段")
        try:
            highlight_segments = ai.pick_highlight_segments_gemini(saved_video_path)
            for _h in highlight_segments:
                log(
                    f"  高光 [{_h['start']:.1f}-{_h['end']:.1f}s] "
                    f"原: {_h.get('original') or ''} | 译: {_h['translation']}"
                )
            if not highlight_segments:
                log("  Gemini 未挑出高光段")
        except Exception as _he:
            log(f"  高光识别失败: {_he}", "warn")
        log(f"[generate-ai] 步骤4b完成，耗时 {_t.time() - _hl_t:.1f}s")
    elif saved_video_path:
        log("[generate-ai] 步骤4b跳过 (highlight=off)")

    # ---- 步骤 5-8: 迭代生成 + 审阅 + 重写 ----
    log(f"[generate-ai] 步骤5-8: 开始迭代生成 (最多{max_rounds}轮)")
    best_video = None
    best_review: dict = {"score": 0, "grade": "F"}
    best_round: Optional[dict] = None
    cur_commentary = commentaries[0] if commentaries else (polished[0] if polished else "")
    cur_song = song_query
    rounds_log: list = []
    cancelled = False

    for rnd in range(1, max_rounds + 1):
        if on_cancel and on_cancel():
            log("[generate-ai] 收到取消请求，停止生成", "warn")
            cancelled = True
            break

        _rnd_t = _t.time()
        log(f"[generate-ai] 第{rnd}轮生成中...")

        video_path, review, cancel_hit = _iterate_with_review(
            rnd=rnd,
            request_id=request_id,
            agent=agent,
            ai=ai,
            saved_paths=saved_paths,
            polished=polished,
            author_list=author_list,
            mood=mood,
            duration=duration,
            cur_commentary=cur_commentary,
            cur_song=cur_song,
            saved_video_path=saved_video_path,
            video_subtitles=video_subtitles,
            highlight_segments=highlight_segments,
            orig0=orig0,
            author0=author0,
            video_description=video_description,
            context_brief=context_brief,
            logger=log,
            on_cancel=on_cancel,
        )

        if cancel_hit:
            log("[generate-ai] 收到取消请求，跳过审阅", "warn")
            cancelled = True
            if video_path:
                best_video = video_path
            break

        score = review.get("score", 0)
        grade = review.get("grade", "F")
        suggestions = review.get("suggestions", [])
        details = review.get("details", {})
        content_issues = review.get("content_issues", [])
        subtitle_mismatches = review.get("subtitle_mismatches", [])
        log(f"[generate-ai] 第{rnd}轮评分: {score}分 ({grade}级)")
        if details:
            log(f"  评分明细: {details}")
        if content_issues:
            log(f"  内容问题: {content_issues}", "warn")
        if subtitle_mismatches:
            log(f"  字幕不匹配: {subtitle_mismatches}", "warn")
        if suggestions:
            log(f"  改进建议: {suggestions}")

        rounds_log.append({
            "round": rnd,
            "score": score,
            "grade": grade,
            "commentary": cur_commentary,
            "song": cur_song,
            "suggestions": suggestions,
        })

        if score > best_review.get("score", 0):
            best_video = video_path
            best_review = review
            best_round = {
                "round": rnd,
                "commentary": cur_commentary,
                "song": cur_song,
            }

        if score >= 90:
            log("[generate-ai] A级达标，停止迭代", "success")
            log(f"  第{rnd}轮总耗时: {_t.time() - _rnd_t:.1f}s")
            break

        if rnd < max_rounds:
            log(f"[generate-ai] 第{rnd}轮未达标 (耗时 {_t.time() - _rnd_t:.1f}s)，准备改进...")
            cur_commentary = _rewrite_commentary(
                ai=ai,
                cur_commentary=cur_commentary,
                review=review,
                suggestions=suggestions,
                target_video_duration=target_video_duration,
                author0=author0,
                orig0=orig0,
                polished=polished,
                video_description=video_description,
                logger=log,
            )
            cur_song = _improve_bgm(
                ai=ai,
                suggestions=suggestions,
                cur_song=cur_song,
                orig0=orig0,
                polished=polished,
                author0=author0,
            )

    result = _finalize(
        request_id=request_id,
        agent=agent,
        best_video=best_video,
        best_round=best_round,
        cur_commentary=cur_commentary,
        cur_song=cur_song,
        max_rounds=max_rounds,
        best_review=best_review,
        rounds_log=rounds_log,
        polished=polished,
        trans_list=trans_list,
        mood=mood,
        video_description=video_description,
        cancelled=cancelled,
        logger=log,
        output_dir=output_dir,
    )

    _total = _t.time() - _pipeline_start
    log(
        f"[generate-ai] 完成! 最终评分: {result.score}分 ({result.grade}级), "
        f"总耗时: {_total:.1f}s ({_total / 60:.1f}min)",
        "success",
    )
    return result
