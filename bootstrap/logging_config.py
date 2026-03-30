"""
bootstrap/logging_config.py
===========================

Single responsibility: configure the unified log sink for Tinker.

Extracted from main.py so that logging setup can be read, tested,
and changed independently of component wiring or health checks.

Usage
-----
::

    from bootstrap.logging_config import setup_logging

    setup_logging("INFO")          # human-readable coloured output
    setup_logging("DEBUG")         # same, verbose
    TINKER_JSON_LOGS=true  →       # newline-delimited JSON (Datadog / Loki)
"""

from __future__ import annotations

import logging
import os
import sys


class _InterceptHandler(logging.Handler):
    """Route stdlib logging records through loguru.

    All Tinker modules use ``logging.getLogger(name)`` from the stdlib.
    This handler intercepts every record and re-emits it via loguru so
    that all log lines share the same sink configuration.
    """

    def emit(self, record: logging.LogRecord) -> None:
        # Only imported here so the module loads even if loguru is absent.
        from loguru import logger as _loguru

        try:
            level: str = _loguru.level(record.levelname).name
        except ValueError:
            level = record.levelname

        # Walk past logging internals so loguru reports the original call site.
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back  # type: ignore[assignment]
            depth += 1

        _loguru.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging(level: str) -> None:
    """Configure loguru as the unified log sink for all of Tinker.

    * Removes loguru's default stderr sink.
    * Adds a new sink: JSON (for production) or coloured text (for dev).
    * Installs _InterceptHandler on the root stdlib logger so every
      ``logging.getLogger(...)`` call ends up here.

    If loguru is not installed, falls back to ``logging.basicConfig``.

    Parameters
    ----------
    level : str
        Log level — ``"DEBUG"``, ``"INFO"``, ``"WARNING"``, or ``"ERROR"``.
    """
    try:
        from loguru import logger as _loguru

        _loguru.remove()

        json_logs = os.getenv("TINKER_JSON_LOGS", "false").lower() == "true"
        if json_logs:
            _loguru.add(
                sys.stderr,
                level=level,
                serialize=True,
                enqueue=False,
            )
        else:
            _loguru.add(
                sys.stderr,
                level=level,
                format=(
                    "<green>{time:HH:mm:ss}</green>  "
                    "<level>{level:<8}</level>  "
                    "<cyan>{name}</cyan>  {message}"
                ),
                colorize=True,
            )

        logging.root.handlers = [_InterceptHandler()]
        logging.root.setLevel(0)
        for name in list(logging.root.manager.loggerDict):
            lg = logging.getLogger(name)
            lg.handlers = []
            lg.propagate = True

    except ImportError:
        logging.basicConfig(
            level=level,
            format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
            datefmt="%H:%M:%S",
        )
