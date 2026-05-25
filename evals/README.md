# NBAVedio Eval Targets

Two eval targets guard the highest-hallucination-risk steps in the pipeline.

## Targets

| Target | Cases | Tests | Baseline | Run |
|---|---|---|---|---|
| `needs_research` | 16 | needs_research decision F1 | F1 ≈ 0.857 | `python -m evals.run_needs_research` |
| `research_brief` | 6 | brief faithfulness to SerpAPI snippets | pass-rate vs empty-brief baseline | `python -m evals.run_research_brief` |

## Label provenance

- **Annotator**: single annotator (project author). No inter-annotator agreement measured. Known bias risk: labels and pipeline tuned by the same person.
- **Sampling strategy for needs_research**: cases collected from real tweets ingested by the sibling NBACrawler project, hand-picked to cover:
  - reaction tweets (GOAT / he's him / alien / 外星人)
  - condolence tweets (RIP X)
  - breaking news (trades, injuries)
  - tweets that are self-explanatory and should NOT trigger research
  - one injection-probe case (P5)

  Not a uniform sample — deliberately enriched for known failure modes.
- **Sampling strategy for research_brief**: cases hand-picked to exercise must_mention (key facts must appear), must_not_mention (no hallucinated opponents / scores), and min_confidence (downgrade rules from P3, including the `single_source_downgrade` case).
- **Status**: regression set, not a statistical sample. Confidence intervals on F1 would be meaningless at this N.

## What baselines mean

Each run prints trivial baselines alongside the model metric:
- `needs_research`: always_true / always_false / random(seed=42)
- `research_brief`: empty brief (no facts, confidence=low)

A model whose F1 / pass-rate doesn't beat the strongest trivial baseline is doing nothing.

## Persistence

Per-case results are written to `evals/results/<target>_<YYYYMMDD_HHMMSS>.jsonl` with a `_meta` first line (target, timestamp, metrics, baselines, git commit if available). Results dir is gitignored.

## Cache workflow (strict, paid APIs never re-billed in CI)

```bash
# 1. Record baseline once — allows live LLM calls, writes results into cache/
python -m evals.run_needs_research --record

# 2. After any prompt/heuristic change → cache-only run, zero API spend
python -m evals.run_needs_research
```

Without `--record`, a cache miss raises an error. CI never silently burns quota.

## Adding a case

1. Reproduce the bug → grab the exact tweet text + author + date.
2. Append one JSONL line to `dataset.jsonl`:
   ```json
   {"id": "short_slug", "text": "...", "author": "...", "tweet_date": "YYYY-MM-DD",
    "should_research": true, "category": "reaction", "notes": "why this case matters"}
   ```
3. Run `--record` once to populate cache.
4. Commit the new line **and** the new `cache/*.json` file together.

## When to extend the dataset

Add a case whenever:
- A new bug is found in production (lock in the regression)
- A prompt change is made that affects a category not yet covered
- An attack vector is identified (injection, jailbreak, etc.)

Never edit an existing case's expected label silently — if you change a label, note the reason in the case's `notes` field.
