"""
orchestrator/compat.py
======================

Async/sync compatibility helpers for the orchestrator loops.

Why does this exist?
--------------------
The orchestrator is designed to work with both ``async def`` and plain ``def``
component implementations — real backends are async; lightweight stubs used in
tests may be synchronous.  Rather than requiring every component to carry async
boilerplate, ``coroutine_if_needed`` wraps sync callables so they can be
awaited without blocking the event loop.

This helper previously lived at the bottom of ``micro_loop.py`` and was
monkey-patched onto the ``asyncio`` module (``asyncio.coroutine_if_needed``).
Moving it here avoids patching the standard library and makes the dependency
explicit.

Usage
-----
    from orchestrator.compat import coroutine_if_needed

    result = await asyncio.wait_for(
        coroutine_if_needed(component.some_method)(arg1, arg2),
        timeout=30.0,
    )
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable


def coroutine_if_needed(fn: Callable) -> Callable:
    """
    Wrap a plain (synchronous) callable so it can be awaited safely.

    * If ``fn`` is already an ``async def`` function, return it unchanged.
    * If ``fn`` is a regular ``def`` function, return a thin async wrapper
      that runs ``fn`` in the default thread-pool executor via
      ``loop.run_in_executor``.  This prevents the synchronous function from
      blocking the event loop while it executes.

    Parameters
    ----------
    fn : Any callable (sync or async).

    Returns
    -------
    An async callable that can be awaited.
    """
    if inspect.iscoroutinefunction(fn):
        return fn

    async def _wrapper(*args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    return _wrapper
