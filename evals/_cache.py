"""Strict on-disk cache for eval LLM calls.

Paid APIs (Gemini video review, SerpAPI) must never be re-billed by eval runs.
Default mode: cache miss raises. Use `record_mode=True` only when intentionally
seeding new cases.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

_CACHE_DIR = Path(__file__).parent / "cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)


class CacheMiss(RuntimeError):
    pass


def _key(namespace: str, payload: Any) -> Path:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    h = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]
    return _CACHE_DIR / f"{namespace}__{h}.json"


def cached_call(
    namespace: str,
    payload: dict,
    fn: Callable[[], Any],
    *,
    record_mode: bool = False,
) -> Any:
    path = _key(namespace, payload)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))["result"]
    if not record_mode:
        raise CacheMiss(
            f"cache miss for {namespace} (key={path.name}). "
            f"Re-run with record_mode=True to allow live API call."
        )
    result = fn()
    path.write_text(
        json.dumps({"namespace": namespace, "payload": payload, "result": result},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result


def load_if_present(namespace: str, payload: dict) -> Any | None:
    path = _key(namespace, payload)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))["result"]
    return None
