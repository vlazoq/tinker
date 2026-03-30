"""
tinker/dashboard/__main__.py
─────────────────────────────
Runs the dashboard in demo mode (mock Orchestrator).

    python -m tinker.dashboard [--redis <url>] [--mock] [--refresh <seconds>]

Options
───────
  --mock              Use the built-in mock Orchestrator (default if no --redis)
  --redis <url>       Connect to a Redis pub/sub Orchestrator (e.g. redis://localhost:6379)
  --refresh <float>   UI poll interval in seconds (default: 1.0)
  --log-level <str>   Minimum log level for stdlib bridge (default: DEBUG)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging


def main() -> None:
    parser = argparse.ArgumentParser(description="Tinker Observability Dashboard")
    parser.add_argument(
        "--mock",
        action="store_true",
        default=True,
        help="Run with synthetic mock Orchestrator (default)",
    )
    parser.add_argument("--redis", metavar="URL", default=None, help="Redis URL for pub/sub mode")
    parser.add_argument(
        "--refresh",
        metavar="SEC",
        type=float,
        default=1.0,
        help="UI refresh interval (seconds)",
    )
    parser.add_argument("--log-level", metavar="LEVEL", default="DEBUG", dest="log_level")
    args = parser.parse_args()

    # ── stdlib log bridge ─────────────────────────────────────────
    from .log_handler import install_stdlib_bridge

    logging.basicConfig(level=logging.DEBUG)
    install_stdlib_bridge()

    # ── choose subscriber ─────────────────────────────────────────
    if args.redis:
        from .subscriber import RedisSubscriber

        subscriber = RedisSubscriber(redis_url=args.redis)
        use_mock = False
    else:
        from .subscriber import QueueSubscriber

        subscriber = QueueSubscriber()
        use_mock = True

    # ── loguru sink (if loguru is installed) ──────────────────────
    try:
        from loguru import logger

        from .log_handler import loguru_sink

        logger.add(
            loguru_sink,
            format="{time}|{level}|{name}:{function}:{line}|{message}",
            colorize=False,
        )
    except ImportError:
        pass

    # ── build and run app ─────────────────────────────────────────
    from .app import TinkerDashboard

    app = TinkerDashboard(subscriber=subscriber, refresh_interval=args.refresh)

    if use_mock:
        # Run mock Orchestrator as a background asyncio task inside the
        # same event loop that Textual manages.
        from .mock_orchestrator import run_mock

        async def _run_with_mock() -> None:
            mock_task = asyncio.create_task(run_mock(tick=1.5))
            try:
                await app.run_async()
            finally:
                mock_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await mock_task

        asyncio.run(_run_with_mock())
    else:
        app.run()


if __name__ == "__main__":
    main()
