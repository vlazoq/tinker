"""
tinker/dashboard/log_handler.py
────────────────────────────────
Loguru sink that captures log records and makes them available to the
live log panel in the Textual dashboard.

Architecture
────────────
Loguru sinks are called synchronously in the logging thread.  Rather than
touching the Textual widget directly (cross-thread Textual mutation is
unsafe), we:

  1. Push each formatted record into a thread-safe ring buffer (deque).
  2. The LogStreamPanel polls the buffer on a Textual timer, pulling new
     lines and appending them to its RichLog widget safely inside the
     Textual event loop.

The buffer is a bounded deque; old lines are evicted automatically.
"""

from __future__ import annotations

import re
import threading
from collections import deque
from datetime import datetime
from typing import Deque, List, NamedTuple, Optional


# ──────────────────────────────────────────
# Log record
# ──────────────────────────────────────────

LEVEL_COLOURS = {
    "TRACE": "dim white",
    "DEBUG": "bright_black",
    "INFO": "bright_cyan",
    "SUCCESS": "bright_green",
    "WARNING": "yellow",
    "ERROR": "bright_red",
    "CRITICAL": "bold bright_red on dark_red",
}

# Strip ANSI escape codes (loguru colourises by default)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class LogRecord(NamedTuple):
    timestamp: datetime
    level: str
    message: str
    source: str  # module:function:line


# ──────────────────────────────────────────
# Ring buffer
# ──────────────────────────────────────────


class LogBuffer:
    """
    Thread-safe bounded ring buffer for log records.
    """

    def __init__(self, maxlen: int = 2000) -> None:
        self._buf: Deque[LogRecord] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._cursor = 0  # monotonic, for "fetch since" semantics

    def push(self, record: LogRecord) -> None:
        with self._lock:
            self._buf.append(record)
            self._cursor += 1

    def tail(self, n: int = 100) -> List[LogRecord]:
        """Return the most recent *n* records."""
        with self._lock:
            items = list(self._buf)
        return items[-n:]

    def since(self, cursor: int) -> tuple[List[LogRecord], int]:
        """
        Return all records added after *cursor*, and the new cursor value.
        Used by the panel to efficiently poll only new lines.
        """
        with self._lock:
            items = list(self._buf)
            current = self._cursor

        if cursor >= current:
            return [], current

        # How many new records are there?
        new_count = current - cursor
        new_items = items[-new_count:] if new_count <= len(items) else items
        return new_items, current


# ──────────────────────────────────────────
# Singleton buffer
# ──────────────────────────────────────────

_buffer = LogBuffer(maxlen=2000)


def get_log_buffer() -> LogBuffer:
    return _buffer


# ──────────────────────────────────────────
# Loguru sink
# ──────────────────────────────────────────


def loguru_sink(message) -> None:  # type: ignore[type-arg]
    """
    Install this as a Loguru sink:

        from loguru import logger
        from tinker.dashboard.log_handler import loguru_sink
        logger.add(loguru_sink, format="{time}|{level}|{name}:{function}:{line}|{message}")
    """
    record = message.record
    level = record["level"].name
    ts = record["time"]
    src = f"{record['name']}:{record['function']}:{record['line']}"
    msg = _ANSI_RE.sub("", record["message"])

    _buffer.push(
        LogRecord(
            timestamp=ts,
            level=level,
            message=msg,
            source=src,
        )
    )


# ──────────────────────────────────────────
# stdlib logging bridge (optional)
# ──────────────────────────────────────────

import logging as _stdlib_logging  # noqa: E402


class StdlibBridgeHandler(_stdlib_logging.Handler):
    """
    Forward stdlib logging records into the same LogBuffer.
    Useful for libraries that use logging rather than loguru.
    """

    _LEVEL_MAP = {
        "DEBUG": "DEBUG",
        "INFO": "INFO",
        "WARNING": "WARNING",
        "ERROR": "ERROR",
        "CRITICAL": "CRITICAL",
    }

    def emit(self, record: _stdlib_logging.LogRecord) -> None:
        level = self._LEVEL_MAP.get(record.levelname, record.levelname)
        src = f"{record.module}:{record.funcName}:{record.lineno}"
        msg = self.format(record)
        msg = _ANSI_RE.sub("", msg)
        _buffer.push(
            LogRecord(
                timestamp=datetime.fromtimestamp(record.created),
                level=level,
                message=msg,
                source=src,
            )
        )


def install_stdlib_bridge(root_logger: Optional[_stdlib_logging.Logger] = None) -> None:
    """
    Attach the bridge handler to the root stdlib logger so that all
    library log output flows into the dashboard's log buffer.
    """
    logger = root_logger or _stdlib_logging.getLogger()
    handler = StdlibBridgeHandler()
    handler.setLevel(_stdlib_logging.DEBUG)
    logger.addHandler(handler)
