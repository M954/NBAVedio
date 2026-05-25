"""Eval: ResearchAgent._extract_facts brief quality.

Tests that, given fixed SerpAPI snippets, the LLM-generated brief:
  1. Mentions every fact in `must_mention`        (recall on critical facts)
  2. Mentions NONE of the items in `must_not_mention` (hallucination check)
  3. confidence >= `min_confidence` ordering (low < medium < high)

Usage:
  python -m evals.run_research_brief          # cache-only (CI-safe)
  python -m evals.run_research_brief --record # allow live LLM calls + cache

Why this exists: Bug B series (B → B-4) was 4 iterations of prompt tweaks on
this exact code path, with no eval to prevent regression. This is that eval.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.ai_assistant import AIAssistant  # noqa: E402
from agents.research_agent import ResearchAgent  # noqa: E402
from evals import _cache  # noqa: E402

DATASET = Path(__file__).parent / "research_brief_dataset.jsonl"
RESULTS_DIR = Path(__file__).parent / "results"

_CONF_RANK = {"low": 0, "medium": 1, "high": 2}


def _git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(ROOT),
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return None


def _persist_jsonl(target: str, results: list[dict], metrics: dict, baselines: dict) -> Path | None:
    try:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = RESULTS_DIR / f"{target}_{ts}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            meta = {
                "_meta": True,
                "target": target,
                "timestamp": ts,
                "total_cases": len(results),
                "metrics": metrics,
                "baselines": baselines,
                "git_commit": _git_commit(),
            }
            f.write(json.dumps(meta, ensure_ascii=False) + "\n")
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        return path
    except Exception as e:
        print(f"[warn] failed to persist results: {e}")
        return None


def _empty_brief_baseline(cases: list[dict]) -> dict:
    """Trivial baseline: empty brief, confidence=low. Pass-rate per criterion + overall."""
    n = len(cases)
    if not n:
        return {}
    mm_pass = 0  # empty mentions nothing
    mn_pass = sum(1 for c in cases if True)  # never hallucinates anything
    mc_pass = sum(1 for c in cases if _CONF_RANK.get(c.get("min_confidence", "low"), 0) <= 0)
    overall_pass = sum(
        1 for c in cases
        if not c.get("must_mention")  # nothing required to mention
        and _CONF_RANK.get(c.get("min_confidence", "low"), 0) <= 0
    )
    return {
        "empty_brief": {
            "must_mention_pass_rate": mm_pass / n,
            "must_not_mention_pass_rate": mn_pass / n,
            "min_confidence_pass_rate": mc_pass / n,
            "overall_pass_rate": overall_pass / n,
        }
    }


def _load_cases():
    cases = []
    for line in DATASET.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            cases.append(json.loads(line))
    return cases


def _patch(assistant, record_mode: bool, miss_log: list):
    original = assistant._call

    def cached(prompt, system="你是一个专业的NBA篮球内容编辑和翻译。", purpose="unknown"):
        try:
            return _cache.cached_call(
                "research_brief.call",
                {"prompt": prompt, "system": system},
                lambda: original(prompt, system, purpose=purpose),
                record_mode=record_mode,
            )
        except _cache.CacheMiss as e:
            miss_log.append(str(e))
            raise

    assistant._call = cached


def _score_case(case: dict, facts: dict, brief: str) -> dict:
    """Return per-case scoring dict."""
    text_blob = (
        (facts.get("summary") or "") + "\n"
        + "\n".join(s.get("snippet", "") for s in facts.get("raw_snippets_relevant", []))
        + "\n" + brief
    ).lower()

    must_mention = case.get("must_mention", [])
    must_not = case.get("must_not_mention", [])
    min_conf = case.get("min_confidence", "low")

    missing = [m for m in must_mention if m.lower() not in text_blob]
    fabricated = [m for m in must_not if m.lower() in text_blob]
    got_conf = (facts.get("confidence") or "low").lower()
    conf_ok = _CONF_RANK.get(got_conf, 0) >= _CONF_RANK.get(min_conf, 0)

    passed = not missing and not fabricated and conf_ok
    return {
        "id": case["id"],
        "passed": passed,
        "missing": missing,
        "fabricated": fabricated,
        "confidence": got_conf,
        "conf_ok": conf_ok,
    }


def run(record_mode: bool = False):
    cases = _load_cases()
    ai = AIAssistant()
    miss_log: list = []
    _patch(ai, record_mode, miss_log)

    agent = ResearchAgent(ai=ai)

    results = []
    persisted_records: list[dict] = []
    metrics: dict = {}
    baselines: dict = {}
    try:
        for c in cases:
            miss_before = len(miss_log)
            try:
                facts, brief = agent._extract_facts(
                    c["tweet_text"], c.get("author", ""),
                    c["slim_results"], raw_brief=""
                )
            except _cache.CacheMiss:
                row = {"id": c["id"], "passed": None, "cache_miss": True,
                       "missing": [], "fabricated": [], "confidence": "?", "conf_ok": False}
                results.append(row)
                persisted_records.append({
                    "case_id": c["id"], "predicted": None, "expected": True,
                    "correct": False, "cache_miss": True,
                    "missing": [], "fabricated": [], "confidence": "?", "conf_ok": False,
                })
                continue
            had_miss = len(miss_log) > miss_before
            scored = _score_case(c, facts or {}, brief or "")
            scored["cache_miss"] = had_miss
            results.append(scored)
            persisted_records.append({
                "case_id": scored["id"],
                "predicted": scored["passed"],
                "expected": True,
                "correct": bool(scored["passed"]),
                "cache_miss": had_miss,
                "missing": scored["missing"],
                "fabricated": scored["fabricated"],
                "confidence": scored["confidence"],
                "conf_ok": scored["conf_ok"],
            })

        # metrics
        scored = [r for r in results if r["passed"] is not None]
        misses = sum(1 for r in results if r.get("cache_miss"))
        passed = sum(1 for r in scored if r["passed"])
        fab_count = sum(1 for r in scored if r["fabricated"])
        missing_count = sum(1 for r in scored if r["missing"])
        conf_fail = sum(1 for r in scored if not r["conf_ok"])
        metrics = {
            "scored": len(scored),
            "passed": passed,
            "pass_rate": passed / len(scored) if scored else 0.0,
            "fabrications": fab_count,
            "missing_facts": missing_count,
            "low_confidence": conf_fail,
            "cache_miss": misses,
        }

        print("\n=== research_brief eval ===")
        print(f"cases: {len(results)}  scored: {len(scored)}  cache_miss: {misses}")
        if scored:
            print(f"passed: {passed}/{len(scored)}  ({passed/len(scored):.0%})")
        print(f"breakdown: fabrications={fab_count}  missing_facts={missing_count}  low_confidence={conf_fail}")

        fail = [r for r in scored if not r["passed"]]
        if fail:
            print("\nFAILURES:")
            for r in fail:
                tags = []
                if r["fabricated"]:
                    tags.append(f"HALLUCINATED={r['fabricated']}")
                if r["missing"]:
                    tags.append(f"MISSING={r['missing']}")
                if not r["conf_ok"]:
                    tags.append(f"CONF_LOW(got={r['confidence']})")
                print(f"  [{r['id']}] {' | '.join(tags)}")

        if misses:
            print("\nCACHE MISSES (rerun with --record to seed):")
            for r in results:
                if r.get("cache_miss"):
                    print(f"  [{r['id']}]")

        baselines = _empty_brief_baseline(cases)
        if baselines:
            b = baselines["empty_brief"]
            print(f"\nBaseline (empty brief, confidence=low) on {len(cases)} cases:")
            print(f"  must_mention_pass_rate     = {b['must_mention_pass_rate']:.3f}")
            print(f"  must_not_mention_pass_rate = {b['must_not_mention_pass_rate']:.3f}")
            print(f"  min_confidence_pass_rate   = {b['min_confidence_pass_rate']:.3f}")
            print(f"  overall_pass_rate          = {b['overall_pass_rate']:.3f}")
            actual_pass_rate = metrics.get("pass_rate", 0.0)
            print(f"Model lift over empty-brief baseline: {actual_pass_rate - b['overall_pass_rate']:+.3f} pass-rate")

        return results
    finally:
        _persist_jsonl("research_brief", persisted_records, metrics, baselines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true",
                    help="allow live LLM calls on cache miss")
    args = ap.parse_args()
    run(record_mode=args.record)
