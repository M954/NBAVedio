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
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.ai_assistant import AIAssistant  # noqa: E402
from agents.research_agent import ResearchAgent  # noqa: E402
from evals import _cache  # noqa: E402

DATASET = Path(__file__).parent / "research_brief_dataset.jsonl"

_CONF_RANK = {"low": 0, "medium": 1, "high": 2}


def _load_cases():
    cases = []
    for line in DATASET.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            cases.append(json.loads(line))
    return cases


def _patch(assistant, record_mode: bool, miss_log: list):
    original = assistant._call

    def cached(prompt, system="你是一个专业的NBA篮球内容编辑和翻译。"):
        try:
            return _cache.cached_call(
                "research_brief.call",
                {"prompt": prompt, "system": system},
                lambda: original(prompt, system),
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
    for c in cases:
        miss_before = len(miss_log)
        try:
            facts, brief = agent._extract_facts(
                c["tweet_text"], c.get("author", ""),
                c["slim_results"], raw_brief=""
            )
        except _cache.CacheMiss:
            results.append({"id": c["id"], "passed": None, "cache_miss": True,
                            "missing": [], "fabricated": [], "confidence": "?", "conf_ok": False})
            continue
        had_miss = len(miss_log) > miss_before
        scored = _score_case(c, facts or {}, brief or "")
        scored["cache_miss"] = had_miss
        results.append(scored)

    # metrics
    scored = [r for r in results if r["passed"] is not None]
    misses = sum(1 for r in results if r.get("cache_miss"))
    passed = sum(1 for r in scored if r["passed"])
    fab_count = sum(1 for r in scored if r["fabricated"])
    missing_count = sum(1 for r in scored if r["missing"])
    conf_fail = sum(1 for r in scored if not r["conf_ok"])

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

    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true",
                    help="allow live LLM calls on cache miss")
    args = ap.parse_args()
    run(record_mode=args.record)
