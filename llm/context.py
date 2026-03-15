"""
Tinker Model Client — Token budget enforcement (context-window management).

What this file does
-------------------
Every AI language model has a "context window" — a hard limit on how much
text it can read in one go.  Think of it like working memory for the model:
once you hit the limit, the model simply cannot see any earlier parts of the
conversation.

This file ensures that before we send a conversation to the model, its total
length (measured in "tokens") fits within the model's context window.  If it
doesn't fit, we intelligently drop old messages from the middle of the
conversation to make room — always keeping the system instructions and the
most recent user message.

Why "tokens" and not "words"?
------------------------------
Language models don't process text word-by-word.  They split text into
"tokens" — roughly 3-4 characters each for English text.  For example,
"architecture" might become ["architect", "ure"] (2 tokens).  The exact
split depends on the model, so we use an estimate: 1 token ≈ 3.5 characters.

If the ``tiktoken`` library is installed, we use its precise tokenizer
(designed for OpenAI-compatible models, which Ollama follows).  Otherwise
we fall back to the character-based estimate.  Either way, we add a small
safety margin so we never accidentally exceed the true limit.

How it fits into Tinker
-----------------------
The ``ModelRouter`` calls ``enforce_context_limit`` on every request, right
before sending to the HTTP client.  The router passes in the model's known
context window size and maximum reply length; this module figures out how
much room is left for the input messages and trims if needed.
"""

from __future__ import annotations

import logging
from typing import Sequence

from .types import Message

logger = logging.getLogger(__name__)

# 1 token is roughly 3.5 characters for mixed English/code text.
# This is a well-known heuristic used when a precise tokenizer isn't available.
CHARS_PER_TOKEN = 3.5

# We keep 8% headroom below the stated context window.  Models often have
# slightly imprecise stated limits, and rounding errors accumulate, so this
# margin prevents us from accidentally going over by a few tokens.
SAFETY_MARGIN   = 0.92       # keep 8 % headroom


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------

def _count_heuristic(text: str) -> int:
    """
    Estimate the number of tokens in ``text`` using the character-to-token ratio.

    Returns at least 1, even for a single character, because every non-empty
    string takes at least one token.

    Parameters
    ----------
    text : The string to estimate token count for.

    Returns
    -------
    int : Estimated token count.
    """
    return max(1, int(len(text) / CHARS_PER_TOKEN))


# ---------------------------------------------------------------------------
# Pick the best available tokenizer at import time.
# ---------------------------------------------------------------------------
# We try to import tiktoken (a fast, accurate tokenizer from OpenAI).
# If it's installed, we use it.  If not, we fall back to the heuristic.
# This pattern — try/except at module level — runs once when the module is
# first imported, so there's no runtime cost per call.

try:
    import tiktoken
    # cl100k_base is the encoding used by GPT-4 and similar models.
    # Ollama models follow the same tokenization conventions.
    _enc = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:  # type: ignore[misc]
        """Count tokens precisely using tiktoken's cl100k_base encoder."""
        return len(_enc.encode(text))

    logger.debug("tiktoken available — using cl100k_base encoder")

except ImportError:
    # tiktoken not installed — use the character heuristic instead
    def count_tokens(text: str) -> int:
        """Estimate token count using the 3.5-chars-per-token heuristic."""
        return _count_heuristic(text)

    logger.debug("tiktoken not installed — using character heuristic for token counting")


def count_messages_tokens(messages: Sequence[Message]) -> int:
    """
    Estimate the total token count for a list of messages.

    This counts the content of every message, plus a small fixed overhead
    per message for the role label and formatting bytes that the model's
    chat template adds.

    Parameters
    ----------
    messages : A sequence of Message objects (system/user/assistant turns).

    Returns
    -------
    int : Estimated total token count for the whole conversation.
    """
    total = 0
    for m in messages:
        total += count_tokens(m.content)
        total += 4  # each message has overhead: role name + chat-template formatting tokens
    total += 2      # the model always "primes" its own reply with 2 tokens
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
    Trim a conversation so that it fits within the model's context window.

    Background
    ----------
    The model needs room for both the input (your messages) and its output
    (its reply).  The total budget is:

        safe_window = floor(context_window * SAFETY_MARGIN)
        input_budget = safe_window - max_output_tokens

    All messages must fit within ``input_budget``.

    Trimming strategy (oldest messages are least valuable):
    -------------------------------------------------------
    1. Always keep the **system message** (if present).  It contains the
       agent's core instructions and must always be visible.
    2. Always keep the **most recent message** (the current question/prompt).
    3. Drop messages from the middle, oldest first, until everything fits.
    4. Last resort: if the system message + last message alone still exceed
       the budget (e.g. the user sent a 10,000-word essay), physically
       truncate the last message's text and append "[... truncated ...]".

    Parameters
    ----------
    messages          : The full list of Message objects to potentially trim.
    context_window    : The model's stated token limit (e.g. 8192).
    max_output_tokens : How many tokens the model's reply may use (e.g. 2048).

    Returns
    -------
    list[Message] : A (possibly shorter) list of messages that fits the budget.

    Raises
    ------
    ValueError : If the context window is so small that even max_output_tokens
                 alone exceeds the safe window (i.e. the config is broken).
    """
    # Calculate how many tokens the input messages are allowed to use.
    # We subtract max_output_tokens because the model needs that space for its reply.
    budget = int(context_window * SAFETY_MARGIN) - max_output_tokens
    if budget <= 0:
        raise ValueError(
            f"context_window={context_window} is too small for max_output_tokens={max_output_tokens}"
        )

    # Fast path: if everything already fits, just return a copy and skip all the work.
    if count_messages_tokens(messages) <= budget:
        return list(messages)

    logger.warning(
        "Message list exceeds context budget (%d tokens). Truncating history.", budget
    )

    # Separate the system message (must keep) from everything else
    system_msgs  = [m for m in messages if m.role == "system"]
    other_msgs   = [m for m in messages if m.role != "system"]

    # The very last message (e.g. the current user prompt) must always be kept.
    protected_tail = other_msgs[-1:]   # list with just the last message
    evictable      = other_msgs[:-1]   # all older non-system messages

    # Drop the oldest evictable message on each iteration until we fit.
    # ``evictable.pop(0)`` removes the first (oldest) item each time.
    while evictable:
        candidate = system_msgs + evictable + protected_tail
        if count_messages_tokens(candidate) <= budget:
            logger.debug("Dropped %d message(s) to fit context window.", len(other_msgs[:-1]) - len(evictable))
            return candidate
        evictable.pop(0)  # drop the oldest remaining evictable message and try again

    # We've dropped everything evictable — only system + last message remain.
    result = system_msgs + protected_tail
    total  = count_messages_tokens(result)
    if total > budget:
        # Even these two messages together are too long — truncate the last one.
        logger.warning("Single message exceeds budget; truncating content.")
        last = result[-1]
        # Calculate how many characters fit in the remaining token budget,
        # with an extra 10% trim (0.9 factor) to be safe against estimation error.
        max_chars = int(budget * CHARS_PER_TOKEN * 0.9)
        # Replace the last message with a truncated version
        result[-1] = Message(role=last.role, content=last.content[:max_chars] + "\n[... truncated ...]")

    return result
