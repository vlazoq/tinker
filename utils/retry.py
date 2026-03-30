"""Async retry with exponential backoff and jitter."""

from __future__ import annotations

import asyncio
import functools
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")


def retry_with_backoff(
    fn: Callable[..., Awaitable[T]] | None = None,
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retryable_exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Any:
    """Async retry decorator/wrapper with exponential backoff and jitter.

    Can be used as a decorator with or without arguments::

        @retry_with_backoff
        async def do_work(): ...

        @retry_with_backoff(max_retries=5, base_delay=0.5)
        async def do_work(): ...

    Or called directly::

        result = await retry_with_backoff(some_coroutine_fn, max_retries=2)()
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exc: BaseException | None = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except retryable_exceptions as exc:
                    last_exc = exc
                    if attempt == max_retries:
                        break
                    delay = min(base_delay * (2**attempt), max_delay)
                    delay *= 0.5 + random.random()  # jitter: [0.5x, 1.5x]
                    await asyncio.sleep(delay)
            raise last_exc  # type: ignore[misc]

        return wrapper

    # Support both @retry_with_backoff and @retry_with_backoff(...)
    if fn is not None:
        return decorator(fn)
    return decorator
