"""轻量 JSONL trace logger,记录 pipeline 关键事件供事后 debug。

设计原则:
- **不替换** logger。logger 给人看,trace 给机器读。
- 每个事件一行 JSON,字段稳定,便于 grep / jq / pandas。
- 不侵入 ai_assistant 内部调用——只在 pipeline 边界落事件。
- NullTrace 作为默认值,调用方不传 trace 时 100% 无开销。
"""
from __future__ import annotations

import datetime
import json
import os
import time as _t
from pathlib import Path
from typing import Any


_TRACE_SCHEMA_VERSION = 1


class NullTrace:
    """Default no-op trace。调用方不显式传 trace 时使用。"""

    def event(self, step: str, **payload: Any) -> None:  # noqa: ARG002
        pass

    def close(self) -> None:
        pass


class TraceLogger:
    """把 pipeline 事件一行一行写到 output/traces/{tweet_id}.jsonl。

    使用方式:
        trace = TraceLogger(tweet_id="abc12345")
        trace.event("pipeline_start", request_id=rid)
        trace.event("step1_polish", n=len(translations))
        trace.event("round_end", round=1, score=82, grade="B")
        trace.close()  # 可选,不调用也不会丢数据(每 event 立即 flush)
    """

    def __init__(self, tweet_id: str = "", output_dir: str | None = None) -> None:
        base = Path(output_dir) if output_dir else Path("output") / "traces"
        base.mkdir(parents=True, exist_ok=True)
        name = tweet_id or datetime.datetime.now().strftime("anon_%Y%m%d_%H%M%S")
        self.path = base / f"{name}.jsonl"
        self.tweet_id = tweet_id
        self._t0 = _t.time()

        # 第一条永远是 meta,用于版本兼容
        self._write({
            "step": "_meta",
            "schema_version": _TRACE_SCHEMA_VERSION,
            "tweet_id": tweet_id,
            "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
        })

    def event(self, step: str, **payload: Any) -> None:
        """记录一个事件。payload 内容序列化失败的 key 会被 repr 化,不丢事件。"""
        record = {
            "step": step,
            "t": round(_t.time() - self._t0, 3),
        }
        for k, v in payload.items():
            try:
                json.dumps(v, ensure_ascii=False, default=str)
                record[k] = v
            except (TypeError, ValueError):
                record[k] = repr(v)
        self._write(record)

    def _write(self, record: dict) -> None:
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            # 不让 trace 失败影响 pipeline;静默丢这一条
            pass

    def llm_call(
        self,
        *,
        model: str,
        purpose: str,
        latency_ms: float,
        tokens_in_est: int,
        tokens_out_est: int,
        cache_hit: bool = False,
        **extra: Any,
    ) -> None:
        # tokens_in/out 用 len(text)//4 启发式估算,不引入 tokenizer 依赖。
        self.event(
            "llm_call",
            model=model,
            purpose=purpose,
            latency_ms=round(float(latency_ms), 2),
            tokens_in_est=int(tokens_in_est),
            tokens_out_est=int(tokens_out_est),
            cache_hit=bool(cache_hit),
            **extra,
        )

    def close(self) -> None:
        # 占位:目前每行立即 flush,close 无必要。保留接口避免调用方记不住。
        pass


# 让 NullTrace 也支持 llm_call,免得调用点要判类型
def _null_llm_call(self, **_kw: Any) -> None:
    pass


NullTrace.llm_call = _null_llm_call  # type: ignore[attr-defined]


# 模块级"当前 trace" — pipeline 在入口 set_current(trace),深层 LLM 调用
# 不必把 trace 一路 thread 下去。pipeline 退出时调用 set_current(None) 复位。
_CURRENT_TRACE: "NullTrace | None" = None


def set_current(trace: "NullTrace | None") -> None:
    global _CURRENT_TRACE
    _CURRENT_TRACE = trace


def get_current() -> "NullTrace | None":
    return _CURRENT_TRACE


def estimate_tokens(text: Any) -> int:
    # len // 4 启发式;真实 tokenizer 留给 eval 离线脚本做精确化
    if not text:
        return 1
    return max(1, len(str(text)) // 4)
