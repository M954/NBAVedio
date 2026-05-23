# Eval Framework

Hand-labeled regression tests for the LLM agents. **Strict cache: paid APIs never re-billed in CI.**

## Why

We hit the same module 3+ times debugging Bug B. That's the signal to stop manual debugging and build eval (see `LESSONS_LEARNED.md` §"When to Stop and Build Eval"). Every future prompt/heuristic change runs against this dataset before being declared a win.

## Layout

```
evals/
├── dataset.jsonl              # ground-truth cases (15 seeded, append as bugs surface)
├── _cache.py                  # strict on-disk cache; cache miss = error by default
├── cache/                     # cached LLM call results (commit to repo)
├── run_needs_research.py      # eval: should_research decision accuracy
└── run_extract_facts.py       # eval: research brief quality (TODO)
```

## Workflow

```bash
cd NBAVedio

# 1. Record baseline (one-time, costs API quota)
python -m evals.run_needs_research --record

# 2. After any prompt/heuristic change → cache-only run, no API spend
python -m evals.run_needs_research
```

`--record` allows live LLM calls and writes results to `cache/`. Without it, a cache miss raises — so CI never silently burns quota.

## Adding a case

1. Reproduce the bug → grab the exact tweet text + author + date.
2. Append one JSONL line to `dataset.jsonl`:
   ```json
   {"id": "short_slug", "text": "...", "author": "...", "tweet_date": "YYYY-MM-DD",
    "should_research": true, "category": "reaction", "notes": "why this case matters"}
   ```
3. Run `--record` once to populate cache.
4. Commit the new line **and** the new `cache/*.json` file together.

## Metrics

`run_needs_research` reports accuracy / precision / recall / F1, per-category breakdown, and a list of mismatches with their IDs so you can drill into specific failures.
