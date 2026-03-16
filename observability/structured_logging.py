"""
observability/structured_logging.py
=====================================

Structured, JSON-formatted logging with trace ID propagation for Tinker.

Why structured logging?
------------------------
Plain text logs are great for humans reading a single terminal, but terrible
for production:
  - Log aggregators (Datadog, Loki, CloudWatch) can't parse unstructured text
  - You can't filter by "show me all logs for micro loop iteration 42"
  - Correlating logs across components is impossible without trace IDs
  - Debugging a failure requires grepping through thousands of lines

Structured logging (JSON lines) solves this:
  - Every log entry is a JSON object with consistent fields
  - You can filter by trace_id, loop_level, task_id, subsystem, etc.
  - Log aggregators ingest it natively
  - Trace IDs link all log lines for a single micro loop together

Trace IDs
----------
A trace ID is a random identifier assigned at the start of each micro loop.
All log messages during that loop carry the same trace_id, so you can
reconstruct exactly what happened in any given loop.

Context variables (asyncio.contextvars) propagate the trace ID automatically
through async calls without needing to pass it manually.

Usage
------
::

    # At startup, call setup_structured_logging() to install the JSON formatter:
    from observability.structured_logging import setup_structured_logging
    setup_structured_logging(level=logging.INFO, json_output=True)

    # In your code, set trace context before doing meaningful work:
    from observability.structured_logging import set_trace_context, clear_trace_context
    set_trace_context(trace_id="abc123", loop_level="micro", task_id="task:xyz")

    # All subsequent logger calls will include these fields:
    logger.info("Architect call started")
    # → {"time": "...", "level": "INFO", "name": "tinker.orchestrator.micro",
    #    "msg": "Architect call started", "trace_id": "abc123",
    #    "loop_level": "micro", "task_id": "task:xyz"}

    # At the end of the loop, clear the context:
    clear_trace_context()
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from contextvars import ContextVar
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Context variables for trace propagation
# ---------------------------------------------------------------------------
# These are asyncio ContextVars — they work like thread-local storage but
# for asyncio tasks.  Each micro loop gets its own context with its own
# trace ID, and all async calls made during that loop inherit the context.

_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
_loop_level_var: ContextVar[str] = ContextVar("loop_level", default="")
_task_id_var: ContextVar[str] = ContextVar("task_id", default="")
_subsystem_var: ContextVar[str] = ContextVar("subsystem", default="")
_iteration_var: ContextVar[int] = ContextVar("iteration", default=0)


def generate_trace_id() -> str:
    """Generate a short random trace ID (12 hex chars = 48 bits)."""
    return secrets.token_hex(6)


def set_trace_context(
    trace_id: Optional[str] = None,
    loop_level: str = "",
    task_id: str = "",
    subsystem: str = "",
    iteration: int = 0,
) -> str:
    """
    Set the trace context for the current async task.

    Call this at the start of each micro/meso/macro loop.  All log messages
    generated during the loop will automatically include these fields.

    Parameters
    ----------
    trace_id   : Unique trace identifier.  Auto-generated if None.
    loop_level : "micro", "meso", or "macro".
    task_id    : The task being processed.
    subsystem  : The subsystem being worked on.
    iteration  : The loop iteration number.

    Returns
    -------
    str : The trace ID (useful if auto-generated).
    """
    tid = trace_id or generate_trace_id()
    _trace_id_var.set(tid)
    _loop_level_var.set(loop_level)
    _task_id_var.set(task_id)
    _subsystem_var.set(subsystem)
    _iteration_var.set(iteration)
    return tid


def clear_trace_context() -> None:
    """Clear all trace context variables after a loop completes."""
    _trace_id_var.set("")
    _loop_level_var.set("")
    _task_id_var.set("")
    _subsystem_var.set("")
    _iteration_var.set(0)


def get_trace_context() -> dict:
    """Return the current trace context as a dict."""
    ctx = {}
    if _trace_id_var.get():
        ctx["trace_id"] = _trace_id_var.get()
    if _loop_level_var.get():
        ctx["loop_level"] = _loop_level_var.get()
    if _task_id_var.get():
        ctx["task_id"] = _task_id_var.get()
    if _subsystem_var.get():
        ctx["subsystem"] = _subsystem_var.get()
    if _iteration_var.get():
        ctx["iteration"] = _iteration_var.get()
    return ctx


# ---------------------------------------------------------------------------
# JSON log formatter
# ---------------------------------------------------------------------------

class JsonFormatter(logging.Formatter):
    """
    Log formatter that outputs one JSON object per line.

    Each JSON object contains:
    - time        : ISO8601 timestamp
    - level       : Log level (INFO, WARNING, ERROR, etc.)
    - name        : Logger name (e.g. "tinker.orchestrator.micro")
    - msg         : The log message
    - trace_id    : Current trace ID (if set)
    - loop_level  : Current loop level (if set)
    - task_id     : Current task ID (if set)
    - subsystem   : Current subsystem (if set)
    - iteration   : Current iteration number (if set)
    - exc_info    : Exception traceback (if an exception was logged)
    - extra       : Any extra fields passed to the logger

    Usage
    ------
    ::

        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logging.root.addHandler(handler)
    """

    def format(self, record: logging.LogRecord) -> str:
        # Build the base log object
        obj: dict[str, Any] = {
            "time":  self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "name":  record.name,
            "msg":   record.getMessage(),
        }

        # Add trace context from ContextVars
        ctx = get_trace_context()
        obj.update(ctx)

        # Add exception info if present
        if record.exc_info:
            obj["exc_info"] = self.formatException(record.exc_info)

        # Add any extra fields the caller passed via the 'extra' parameter
        for key, val in record.__dict__.items():
            if key not in (
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "message",
                "taskName",
            ) and not key.startswith("_"):
                try:
                    json.dumps(val)   # Only include JSON-serialisable values
                    obj[key] = val
                except (TypeError, ValueError):
                    obj[key] = str(val)

        return json.dumps(obj, ensure_ascii=False, default=str)


class HumanReadableFormatter(logging.Formatter):
    """
    Human-readable formatter that includes trace context in the message.

    Use this for development/debugging when JSON is harder to read.
    Format: "HH:MM:SS  LEVEL     name  [trace=xxx loop=micro task=yyy]  message"
    """

    def format(self, record: logging.LogRecord) -> str:
        ctx = get_trace_context()
        ctx_parts = []
        if ctx.get("trace_id"):
            ctx_parts.append(f"trace={ctx['trace_id']}")
        if ctx.get("loop_level"):
            ctx_parts.append(f"loop={ctx['loop_level']}")
        if ctx.get("task_id"):
            ctx_parts.append(f"task={ctx['task_id'][:8]}")
        if ctx.get("subsystem"):
            ctx_parts.append(f"sys={ctx['subsystem']}")

        ctx_str = f"[{' '.join(ctx_parts)}] " if ctx_parts else ""
        base = super().format(record)
        return f"{base.split('  ', 2)[0]}  {record.levelname:<8}  {record.name}  {ctx_str}{record.getMessage()}"


# ---------------------------------------------------------------------------
# Setup helper
# ---------------------------------------------------------------------------

def setup_structured_logging(
    level: int = logging.INFO,
    json_output: bool = False,
    log_file: Optional[str] = None,
) -> None:
    """
    Configure Tinker's root logger with structured (or human-readable) output.

    Call this once at startup in ``main.py`` BEFORE any other log calls.

    Parameters
    ----------
    level       : Root log level (logging.INFO, logging.DEBUG, etc.).
    json_output : If True, use JSON formatter (for production/log aggregation).
                  If False, use human-readable format with trace context.
    log_file    : Optional path to write logs to a file (in addition to stdout).
                  Useful for log persistence across restarts.

    Note: If loguru is installed, consider using it directly instead of this
    function, as it provides better structured logging support natively.
    """
    root = logging.getLogger()
    root.setLevel(level)

    # Remove any existing handlers (e.g. from basicConfig)
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    # Choose formatter based on output mode
    if json_output:
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            fmt="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
            datefmt="%H:%M:%S",
        )

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    # Optional file handler
    if log_file:
        try:
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(JsonFormatter())  # Always JSON in files
            root.addHandler(file_handler)
        except Exception as exc:
            logging.warning("Could not open log file '%s': %s", log_file, exc)

    logging.getLogger("tinker").info(
        "Logging configured (level=%s, json=%s, file=%s)",
        logging.getLevelName(level), json_output, log_file,
    )
