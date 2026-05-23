"""Eval: needs_research decision accuracy.

Usage:
  python -m evals.run_needs_research               # cache-only (CI-safe, no API spend)
  python -m evals.run_needs_research --record      # allow live LLM calls + cache them

Cases come from evals/dataset.jsonl. Each case has ground-truth `should_research`.
Metrics: precision / recall / F1 + per-category breakdown + per-case diff.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.ai_assistant import AIAssistant  # noqa: E402
from evals import _cache  # noqa: E402

DATASET = Path(__file__).parent / "dataset.jsonl"


def _load_cases():
    cases = []
    for line in DATASET.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            cases.append(json.loads(line))
    return cases


class _Sentinel:
    """Marker so CacheMiss inside _call (which is swallowed by needs_research's
    broad except) still surfaces to the eval runner."""


def _patch_llm_for_cache(assistant, record_mode: bool, miss_log: list):
    """Wrap assistant._call so every prompt is cached by hash.

    needs_research wraps _call in try/except Exception, so CacheMiss would be
    silently turned into need_llm=False. Instead, log misses to a side channel
    and return an empty string (which the caller treats as "LLM said nothing").
    """
    original = assistant._call

    def cached(prompt, system="你是一个专业的NBA篮球内容编辑和翻译。"):
        try:
            return _cache.cached_call(
                "needs_research.call",
                {"prompt": prompt, "system": system},
                lambda: original(prompt, system),
                record_mode=record_mode,
            )
        except _cache.CacheMiss as e:
            miss_log.append(str(e))
            raise

    assistant._call = cached


def run(record_mode: bool = False):
    cases = _load_cases()
    assistant = AIAssistant()
    miss_log: list = []
    _patch_llm_for_cache(assistant, record_mode, miss_log)

    results = []
    for c in cases:
        miss_before = len(miss_log)
        need, query = assistant.needs_research(
            original_text=c["text"],
            translation="",
            author=c.get("author", ""),
        )
        had_miss = len(miss_log) > miss_before
        results.append({**c, "predicted": need, "query": query, "cache_miss": had_miss})

    # metrics
    tp = sum(1 for r in results if r["predicted"] is True and r["should_research"])
    fp = sum(1 for r in results if r["predicted"] is True and not r["should_research"])
    fn = sum(1 for r in results if r["predicted"] is False and r["should_research"])
    tn = sum(1 for r in results if r["predicted"] is False and not r["should_research"])
    misses = sum(1 for r in results if r["cache_miss"])
    total_scored = tp + fp + fn + tn
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    acc = (tp + tn) / total_scored if total_scored else 0.0

    print("\n=== needs_research eval ===")
    print(f"cases: {len(results)}  scored: {total_scored}  cache_miss: {misses}")
    print(f"TP={tp} FP={fp} FN={fn} TN={tn}")
    print(f"accuracy={acc:.2%}  precision={prec:.2%}  recall={rec:.2%}  F1={f1:.2%}")

    # per-category
    cats: dict[str, list] = {}
    for r in results:
        cats.setdefault(r.get("category", "?"), []).append(r)
    print("\nper-category:")
    for cat, rs in sorted(cats.items()):
        correct = sum(1 for r in rs if r["predicted"] == r["should_research"])
        print(f"  {cat:<18} {correct}/{len(rs)}")

    # mismatches
    bad = [r for r in results if not r["cache_miss"] and r["predicted"] != r["should_research"]]
    if bad:
        print("\nMISMATCHES:")
        for r in bad:
            safe_text = r["text"].encode("ascii", "replace").decode("ascii")
            print(f"  [{r['id']}] expected={r['should_research']} got={r['predicted']}  -- {safe_text[:60]}")

    if misses:
        print("\nCACHE MISSES (rerun with --record to seed):")
        for r in results:
            if r["cache_miss"]:
                safe_text = r["text"].encode("ascii", "replace").decode("ascii")
                print(f"  [{r['id']}] {safe_text[:60]}")

    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true",
                    help="allow live LLM calls on cache miss")
    args = ap.parse_args()
    run(record_mode=args.record)
