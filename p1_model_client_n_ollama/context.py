"""
Tinker Model Client — Token budget enforcement.

We don't have a tokenizer available for every model, so we use a conservative
character-based heuristic (1 token ≈ 3.5 chars for English/code mixed text)
and cap the estimate with a safety margin.

If the tiktoken library is installed we use it for OpenAI-compatible models
(which Ollama follows); otherwise we fall back to the heuristic.
"""

from __future__ import annotations

import logging
from typing import Sequence

from .types import Message

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 3.5        # heuristic
SAFETY_MARGIN   = 0.92       # keep 8 % headroom


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------

def _count_heuristic(text: str) -> int:
    return max(1, int(len(text) / CHARS_PER_TOKEN))


try:
    import tiktoken
    _enc = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:  # type: ignore[misc]
        return len(_enc.encode(text))

    logger.debug("tiktoken available — using cl100k_base encoder")

except ImportError:
    def count_tokens(text: str) -> int:
        return _count_heuristic(text)

    logger.debug("tiktoken not installed — using character heuristic for token counting")


def count_messages_tokens(messages: Sequence[Message]) -> int:
    """Rough total token count for a message list (includes role overhead)."""
    total = 0
    for m in messages:
        total += count_tokens(m.content)
        total += 4  # role + formatting overhead per message
    total += 2      # priming tokens for assistant reply
    return total


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------

def enforce_context_limit(
    messages: list[Message],
    context_window: int,
    max_output_tokens: int,
) -> list[Message]:
    """
    Ensure that *messages* fit within the available input budget:

        budget = floor(context_window * SAFETY_MARGIN) - max_output_tokens

    Strategy
    --------
    1. Always preserve the **system** message (index 0 if role=="system").
    2. Always preserve the **last user** message.
    3. Drop older messages from the middle until we fit.
    4. If a single message still exceeds the budget, truncate its content.
    """
    budget = int(context_window * SAFETY_MARGIN) - max_output_tokens
    if budget <= 0:
        raise ValueError(
            f"context_window={context_window} is too small for max_output_tokens={max_output_tokens}"
        )

    # Fast path
    if count_messages_tokens(messages) <= budget:
        return list(messages)

    logger.warning(
        "Message list exceeds context budget (%d tokens). Truncating history.", budget
    )

    # Separate protected messages from the evictable middle
    system_msgs  = [m for m in messages if m.role == "system"]
    other_msgs   = [m for m in messages if m.role != "system"]

    # The last user/assistant pair must be kept
    protected_tail = other_msgs[-1:]
    evictable      = other_msgs[:-1]

    # Build result, dropping oldest first
    while evictable:
        candidate = system_msgs + evictable + protected_tail
        if count_messages_tokens(candidate) <= budget:
            logger.debug("Dropped %d message(s) to fit context window.", len(other_msgs[:-1]) - len(evictable))
            return candidate
        evictable.pop(0)

    # Only system + last message remain — if still too big, truncate content
    result = system_msgs + protected_tail
    total  = count_messages_tokens(result)
    if total > budget:
        logger.warning("Single message exceeds budget; truncating content.")
        last = result[-1]
        max_chars = int(budget * CHARS_PER_TOKEN * 0.9)
        result[-1] = Message(role=last.role, content=last.content[:max_chars] + "\n[... truncated ...]")

    return result
