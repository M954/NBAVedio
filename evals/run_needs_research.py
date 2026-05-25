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
import random
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.ai_assistant import AIAssistant  # noqa: E402
from evals import _cache  # noqa: E402

DATASET = Path(__file__).parent / "dataset.jsonl"
RESULTS_DIR = Path(__file__).parent / "results"


def _prf1(preds: list[bool], golds: list[bool]) -> tuple[float, float, float]:
    tp = sum(1 for p, g in zip(preds, golds) if p and g)
    fp = sum(1 for p, g in zip(preds, golds) if p and not g)
    fn = sum(1 for p, g in zip(preds, golds) if not p and g)
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return prec, rec, f1


def _compute_baselines(golds: list[bool]) -> dict:
    n = len(golds)
    out = {}
    out["always_true"] = dict(zip(("precision", "recall", "f1"), _prf1([True] * n, golds)))
    out["always_false"] = dict(zip(("precision", "recall", "f1"), _prf1([False] * n, golds)))
    rng = random.Random(42)
    rand_preds = [rng.random() < 0.5 for _ in range(n)]
    out["random_50_50"] = dict(zip(("precision", "recall", "f1"), _prf1(rand_preds, golds)))
    return out


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

    def cached(prompt, system="你是一个专业的NBA篮球内容编辑和翻译。", purpose="unknown"):
        try:
            return _cache.cached_call(
                "needs_research.call",
                {"prompt": prompt, "system": system},
                lambda: original(prompt, system, purpose=purpose),
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
    persisted_records: list[dict] = []
    metrics: dict = {}
    baselines: dict = {}
    try:
        try:
            for c in cases:
                miss_before = len(miss_log)
                need, query = assistant.needs_research(
                    original_text=c["text"],
                    translation="",
                    author=c.get("author", ""),
                )
                had_miss = len(miss_log) > miss_before
                results.append({**c, "predicted": need, "query": query, "cache_miss": had_miss})
                persisted_records.append({
                    "case_id": c.get("id"),
                    "category": c.get("category"),
                    "predicted": need,
                    "expected": c.get("should_research"),
                    "correct": (need == c.get("should_research")),
                    "cache_miss": had_miss,
                    "query": query,
                })
        finally:
            # cases done (or partially done); compute what we can before stdout/persist
            pass

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
        metrics = {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
                   "precision": prec, "recall": rec, "f1": f1, "accuracy": acc,
                   "cache_miss": misses}

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

        # baselines on same scored cases (skip cache-missed where prediction is meaningless? keep all — they have a predicted value)
        scored_results = [r for r in results if not r["cache_miss"]]
        golds = [bool(r["should_research"]) for r in scored_results]
        if golds:
            baselines = _compute_baselines(golds)
            best_baseline_f1 = max(b["f1"] for b in baselines.values())
            print("\nBaselines (same {} cases):".format(len(golds)))
            for name, b in baselines.items():
                print(f"  {name:<13} precision={b['precision']:.3f} recall={b['recall']:.3f} f1={b['f1']:.3f}")
            print(f"Model lift over best baseline: {f1 - best_baseline_f1:+.3f} F1")

        return results
    finally:
        _persist_jsonl("needs_research", persisted_records, metrics, baselines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true",
                    help="allow live LLM calls on cache miss")
    args = ap.parse_args()
    run(record_mode=args.record)
