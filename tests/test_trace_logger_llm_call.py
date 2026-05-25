"""Smoke test for new trace_logger.llm_call + serpapi_call + current-trace singleton."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.trace_logger import TraceLogger, set_current, get_current, estimate_tokens


def _read_events(p: Path):
    with open(p, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_llm_call_event_shape():
    with tempfile.TemporaryDirectory() as d:
        trace = TraceLogger(tweet_id="t_test", output_dir=d)
        set_current(trace)
        assert get_current() is trace
        trace.llm_call(
            model="claude-opus-4.7",
            purpose="test",
            latency_ms=12.5,
            tokens_in_est=100,
            tokens_out_est=50,
        )
        set_current(None)

        events = _read_events(trace.path)
        llm = [e for e in events if e["step"] == "llm_call"]
        assert len(llm) == 1
        e = llm[0]
        assert e["model"] == "claude-opus-4.7"
        assert e["purpose"] == "test"
        assert e["latency_ms"] == 12.5
        assert e["tokens_in_est"] == 100
        assert e["tokens_out_est"] == 50
        assert e["cache_hit"] is False
        assert "t" in e


def test_serpapi_call_event_shape():
    with tempfile.TemporaryDirectory() as d:
        trace = TraceLogger(tweet_id="t_serp", output_dir=d)
        trace.event("serpapi_call", query="LeBron stats", latency_ms=234.0,
                    result_count=5, cache_hit=False)
        events = _read_events(trace.path)
        serp = [e for e in events if e["step"] == "serpapi_call"]
        assert len(serp) == 1
        assert serp[0]["query"] == "LeBron stats"
        assert serp[0]["result_count"] == 5


def test_estimate_tokens_heuristic():
    assert estimate_tokens("") == 1
    assert estimate_tokens(None) == 1
    assert estimate_tokens("a" * 40) == 10
    assert estimate_tokens("x") == 1  # max(1, 0)


def test_get_current_default_is_none():
    set_current(None)
    assert get_current() is None


if __name__ == "__main__":
    test_llm_call_event_shape()
    test_serpapi_call_event_shape()
    test_estimate_tokens_heuristic()
    test_get_current_default_is_none()
    print("OK")
