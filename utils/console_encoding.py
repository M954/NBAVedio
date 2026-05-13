"""把 stdout/stderr 重配成 utf-8 + errors='replace'。

Windows 中文版控制台默认是 GBK（cp936），打印 emoji / 部分中文外字符会触发
``UnicodeEncodeError`` 直接崩溃运行中的任务（生产里多次踩过）。

使用方式：在每个 Python 入口（CLI 或 Web 服务）顶部最早处调用一次
``ensure_utf8_console()`` 即可，整个进程后续所有 print 都安全。

实现细节：
* 仅在 Windows 上动作，其他系统直接返回（POSIX 默认就是 utf-8）。
* Python 3.7+ 的 ``TextIOWrapper.reconfigure`` 是官方支持的运行时重配 API。
* 不替换 stdout 对象本身，避免破坏 uvicorn/MoviePy 等库已经持有的引用。
"""
from __future__ import annotations

import sys


def ensure_utf8_console() -> None:
    """把 stdout/stderr 切到 utf-8，遇到不可编码字符时用 ? 兜底。"""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
