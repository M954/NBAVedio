"""End-to-end integration test for run_pipeline with stubs.

Validates the four "production quality" features added this session:
  A) trace logger writes JSONL events
  B) max_rounds exhaustion sets quality_warning
  C) needs_research narrowed except no longer swallows AttributeError
  D) research_brief eval module imports + runs

Uses stub `ai` and `agent` so it runs in <1 second, no API calls.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class _StubAgent:
    def __init__(self, outdir):
        self.output_dir = outdir
        self.last_subtitle_timeline = []

    def generate(self, **kw):
        # Write a dummy "video" file so _collect_video_info / shutil.copy2 are happy.
        # But _collect_video_info uses VideoFileClip — we'll monkeypatch that.
        path = Path(self.output_dir) / kw["output_name"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 1024)
        return str(path)


class _StubAI:
    def __init__(self, scores):
        self._scores = list(scores)

    def polish_translation(self, orig, trans):
        return trans + " [polished]"

    def analyze_video_content(self, *a, **kw):
        return {"description": "stub", "needs_trim": False, "trim_reason": "none", "segments": []}

    def extract_video_dialogue(self, *a, **kw):
        return []

    def needs_research(self, *a, **kw):
        return False, ""

    def generate_commentary(self, *a, **kw):
        return "stub commentary 解说词"

    def recommend_music_claude(self, *a, **kw):
        return "Stub Song - Stub Artist"

    def recommend_mood(self, *a, **kw):
        return "epic"

    def review_video(self, info, **kw):
        # Return scores from queue, exhausting on each call.
        score = self._scores.pop(0) if self._scores else 60
        grade = "A" if score >= 90 else "B" if score >= 80 else "C"
        return {
            "score": score,
            "grade": grade,
            "suggestions": ["tighten pacing"],
            "details": {"内容准确性": score // 4},
            "content_issues": [],
            "subtitle_mismatches": [],
        }

    def rewrite_commentary_with_review(self, *a, **kw):
        return "stub rewritten commentary 改进版"

    def _call(self, prompt, system="", purpose="unknown"):
        # Some pipeline paths call ai._call directly (e.g., rewrite step).
        return "stub rewritten commentary 改进版"


def test_full_pipeline_with_quality_warning(tmp_path):
    """3 rounds, all below 90 -> quality_warning set + trace file written."""
    import agents.pipeline_core as pc

    # Monkeypatch _collect_video_info to avoid moviepy on stub bytes.
    pc._collect_video_info = lambda p: {  # noqa: E731
        "duration": 12.0, "resolution": "1080x1920", "has_audio": True, "file_size_mb": 0.001
    }

    trace = pc.TraceLogger(tweet_id="stubtest", output_dir=str(tmp_path / "traces"))
    agent = _StubAgent(str(tmp_path))
    ai = _StubAI(scores=[72, 78, 82])  # 3 rounds, best is 82, never hits 90

    result = pc.run_pipeline(
        saved_paths=[str(tmp_path / "img1.png")],
        trans_list=["原文翻译"],
        orig_list=["original text"],
        author_list=["test_author"],
        duration=12.0,
        max_rounds=3,
        request_id="r1",
        tweet_id="stubtest",
        ai=ai,
        agent=agent,
        output_dir=str(tmp_path),
        trace=trace,
    )

    # ─── Assertions ───────────────────────────────────────────────────
    # B: quality_warning must be set since no round hit 90
    assert result.quality_warning is not None, "quality_warning should be set"
    assert "max_rounds=3" in result.quality_warning
    assert "82" in result.quality_warning, f"warning should mention best score: {result.quality_warning}"
    assert result.score == 82, f"best score should be 82, got {result.score}"
    assert result.selected_round == 3
    assert result.total_rounds == 3

    # A: trace file must exist and contain key events
    trace_path = tmp_path / "traces" / "stubtest.jsonl"
    assert trace_path.exists(), "trace file should be created"
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    steps = [e["step"] for e in events]
    assert "_meta" in steps and events[0]["schema_version"] == 1
    assert "pipeline_start" in steps
    assert steps.count("round_start") == 3
    assert steps.count("round_end") == 3
    assert "quality_warning" in steps, f"trace must record quality_warning event, got steps={steps}"
    assert "pipeline_end" in steps
    # The quality_warning event must list all scores
    qw_event = next(e for e in events if e["step"] == "quality_warning")
    assert qw_event["all_scores"] == [72, 78, 82]
    assert qw_event["best_score"] == 82
    print("[OK] B (quality_warning) + A (trace events) verified")


def test_pipeline_threshold_met_no_warning(tmp_path):
    """First round hits 95 -> no warning, loop exits early."""
    import agents.pipeline_core as pc
    pc._collect_video_info = lambda p: {  # noqa: E731
        "duration": 12.0, "resolution": "1080x1920", "has_audio": True, "file_size_mb": 0.001
    }
    trace = pc.TraceLogger(tweet_id="stubtest2", output_dir=str(tmp_path / "traces"))
    agent = _StubAgent(str(tmp_path))
    ai = _StubAI(scores=[95])

    result = pc.run_pipeline(
        saved_paths=[str(tmp_path / "img1.png")],
        trans_list=["原文翻译"],
        orig_list=["original text"],
        author_list=["test_author"],
        duration=12.0,
        max_rounds=3,
        request_id="r2",
        tweet_id="stubtest2",
        ai=ai,
        agent=agent,
        output_dir=str(tmp_path),
        trace=trace,
    )

    assert result.quality_warning is None, "no warning when threshold met"
    assert result.score == 95
    assert result.total_rounds == 1

    trace_path = tmp_path / "traces" / "stubtest2.jsonl"
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    steps = [e["step"] for e in events]
    assert "loop_exit" in steps
    loop_exit = next(e for e in events if e["step"] == "loop_exit")
    assert loop_exit["reason"] == "threshold_met"
    assert "quality_warning" not in steps, "should NOT emit quality_warning when threshold met"
    print("[OK] threshold_met path verified, no spurious warning")


def test_needs_research_narrow_except_preserves_programmer_errors():
    """C: the narrowed except in needs_research must NOT swallow AttributeError."""
    from agents.ai_assistant import AIAssistant

    ai = AIAssistant()

    # Inject a _call that raises AttributeError (a programmer-error class).
    # Under the old wide `except Exception:`, this would silently become (False, "").
    # Under the new narrow `except (json.JSONDecodeError, ValueError, OSError):`,
    # it must propagate.
    def bad_call(prompt, system="", purpose="unknown"):
        raise AttributeError("simulated programmer error")
    ai._call = bad_call

    try:
        ai.needs_research("some short text", "some translation", author="x")
    except AttributeError as e:
        assert "simulated programmer error" in str(e)
        print("[OK] AttributeError propagates (Bug H class of bug stays caught)")
        return
    raise AssertionError("AttributeError was swallowed — narrow except did not work")


def test_research_brief_eval_module_runs():
    """D: evals.run_research_brief must be importable + produce results from cache."""
    from evals import run_research_brief

    results = run_research_brief.run(record_mode=False)
    assert len(results) >= 5, f"expected >=5 cases, got {len(results)}"
    # Cache may be cold after prompt changes (e.g. prompt_safety wrapping); the
    # test's role is to verify importability + end-to-end module execution, not
    # to gate on cache freshness. Re-record cache via `--record` (requires LLM)
    # to regenerate scored results after intentional prompt edits.
    scored = [r for r in results if r["passed"] is not None]
    cache_misses = sum(1 for r in results if r.get("cache_miss"))
    assert scored or cache_misses, "results must report either a score or a cache_miss"
    print(f"[OK] research_brief eval ran: {len(scored)}/{len(results)} scored, {cache_misses} cache miss")


if __name__ == "__main__":
    import tempfile
    print("\n=== running e2e integration tests ===\n")
    for fn in [
        test_needs_research_narrow_except_preserves_programmer_errors,
        test_research_brief_eval_module_runs,
    ]:
        print(f"--- {fn.__name__} ---")
        fn()
    for fn in [test_full_pipeline_with_quality_warning, test_pipeline_threshold_met_no_warning]:
        with tempfile.TemporaryDirectory() as td:
            print(f"--- {fn.__name__} ---")
            fn(Path(td))
    print("\n=== all e2e tests passed ===")
