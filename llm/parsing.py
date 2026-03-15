"""
Tinker Model Client — Structured output (JSON) extraction from model responses.

What this file does
-------------------
When Tinker asks the AI model to respond with JSON, the model doesn't always
comply perfectly.  Instead of a clean JSON object, the reply might look like:

    "Sure! Here is the architecture you asked for:

    ```json
    {"services": ["auth", "gateway"], "rationale": "..."}
    ```

    I hope this helps!"

This file contains functions that try multiple strategies to find and extract
the actual JSON from such messy responses.  Think of it as a smart extractor
that can peel away the wrapping to get to the data inside.

Why it exists
-------------
AI language models are trained to be conversational and helpful, not to output
machine-readable data.  Even when told "respond with JSON only", they often:
- Add polite preamble ("Sure! Here is...").
- Wrap the JSON in markdown code fences (```json ... ```).
- Add a closing remark after the JSON.

This module handles all of these cases gracefully, rather than crashing when
the model doesn't follow instructions precisely.

How it fits into Tinker
-----------------------
``ModelRouter.complete()`` calls ``extract_json()`` from this module whenever
``expect_json=True`` on the request.  ``build_json_instruction()`` is used
to craft the system-prompt addition that instructs the model to output JSON.
"""

from __future__ import annotations

import json
import re
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Extraction strategies (tried in order, from most strict to most lenient)
# ---------------------------------------------------------------------------

def _try_direct(text: str) -> dict | list | None:
    """
    Strategy 1 — Direct parse: treat the entire text as JSON.

    This is the happy path: the model followed instructions perfectly and
    returned nothing but a JSON object or array.  We strip leading/trailing
    whitespace and try to parse it directly.

    Returns the parsed object, or None if parsing fails.
    """
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return None


def _try_fenced(text: str) -> dict | list | None:
    """
    Strategy 2 — Markdown fence extraction.

    Many models wrap their JSON output in a markdown code fence like:

        ```json
        {"key": "value"}
        ```

    or just:

        ```
        {"key": "value"}
        ```

    This function uses a regular expression to find that pattern and extracts
    the content between the backticks.

    Pattern explanation:
    - ```(?:json)?  — three backticks, optionally followed by "json"
    - \\s*\\n?      — optional whitespace/newline after the opening fence
    - (.*?)         — capture the content (lazily, so we stop at the first ```)
    - ```           — closing three backticks
    - re.DOTALL     — makes "." match newlines too (needed for multi-line JSON)
    - re.IGNORECASE — matches ```JSON as well as ```json

    Returns the parsed object, or None if no fence is found or parsing fails.
    """
    pattern = r"```(?:json)?\s*\n?(.*?)```"
    match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            return None
    return None


def _try_first_brace(text: str) -> dict | list | None:
    """
    Strategy 3 — Brace/bracket scan: find the first '{' or '[' and parse from there.

    This handles cases where the model adds preamble text before the JSON:

        "Here is the result: {"key": "value"}"

    We scan forward to find the opening brace/bracket, then walk character-by-
    character to find the matching closing brace/bracket (respecting nested
    structures and string literals), and try to parse that slice as JSON.

    The character-by-character walk handles:
    - Nested braces/brackets (depth counter goes up on ``{`` / ``[``, down on
      ``}`` / ``]``; we stop when depth returns to 0).
    - String literals (we ignore braces inside ``"..."``).
    - Escape sequences (a ``\\`` inside a string means the next character is
      escaped, not a literal quote or brace).

    Returns the parsed object, or None if nothing parseable is found.
    """
    for start_char, end_char in [('{', '}'), ('[', ']')]:
        start = text.find(start_char)
        if start == -1:
            continue  # no opening brace/bracket found; try the other type
        # Walk character-by-character from the opening brace to find the match
        depth = 0          # how many levels of nesting we're currently inside
        in_string = False  # are we inside a "..." string literal right now?
        escape_next = False  # is the next character escaped with a backslash?
        for i, ch in enumerate(text[start:], start=start):
            if escape_next:
                # This character is escaped — skip it, reset flag
                escape_next = False
                continue
            if ch == '\\' and in_string:
                # A backslash inside a string — next character is escaped
                escape_next = True
                continue
            if ch == '"':
                # Toggle in/out of string mode
                in_string = not in_string
                continue
            if in_string:
                # Inside a string — braces don't count, skip
                continue
            if ch == start_char:
                depth += 1  # entering a nested object/array
            elif ch == end_char:
                depth -= 1
                if depth == 0:
                    # We found the matching close — try to parse this slice
                    candidate = text[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break  # malformed JSON even after finding matching brace
    return None


def _try_relaxed(text: str) -> dict | list | None:
    """
    Strategy 4 — Relaxed: strip preamble lines, then retry strategies 2 and 3.

    This is the last resort.  Some models add multiple lines of conversational
    preamble before the JSON starts.  We scan line-by-line to find the first
    line that looks like the start of JSON content (i.e. starts with ``{``,
    ``[``, or ` ``` `), discard everything before it, and re-run the earlier
    strategies on the remaining text.

    Example input that this handles:
        "Of course! I'd be happy to help.
        Here is the architecture:
        {"services": [...]}"

    Returns the parsed object, or None if this strategy also fails.
    """
    lines = text.splitlines()
    # Find the first line that looks like the beginning of JSON or a code fence
    json_start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(('{', '[', '```')):
            json_start = i
            break
    if json_start is not None and json_start > 0:
        # Re-join from the first JSON-looking line onward
        remainder = "\n".join(lines[json_start:])
        # Try the fenced strategy first (it's more precise), then brace-scan
        return _try_fenced(remainder) or _try_first_brace(remainder)
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# The ordered list of strategies.  We try them one by one until one succeeds.
# The name string is used only for logging — it helps diagnose how often each
# strategy is needed in practice.
STRATEGIES = [
    ("direct",      _try_direct),       # best case: model responded with clean JSON
    ("fenced",      _try_fenced),       # model wrapped JSON in ```json ... ```
    ("first_brace", _try_first_brace),  # model added prose before the JSON
    ("relaxed",     _try_relaxed),      # model added multi-line prose before the JSON
]


def extract_json(text: str) -> tuple[dict | list | None, str | None]:
    """
    Try to extract a JSON object or array from raw model output text.

    Tries four increasingly lenient strategies in order.  Returns as soon as
    one succeeds.  If all four fail, returns ``(None, None)`` — it is the
    caller's responsibility to decide what to do (e.g. log a warning, use the
    raw text, or raise an error).

    Parameters
    ----------
    text : The raw string returned by the AI model.

    Returns
    -------
    (parsed, strategy_name)
        parsed        : The parsed Python dict or list, or ``None`` if all
                        strategies failed.
        strategy_name : A string naming which strategy worked (e.g.
                        ``"fenced"``), or ``None`` if all failed.  Useful
                        for logging and debugging.

    Example
    -------
    >>> parsed, strategy = extract_json('```json\\n{"key": "val"}\\n```')
    >>> parsed
    {'key': 'val'}
    >>> strategy
    'fenced'
    """
    for name, fn in STRATEGIES:
        result = fn(text)
        if result is not None:
            logger.debug("JSON extraction succeeded via strategy '%s'", name)
            return result, name

    # All strategies failed — log a warning with the beginning of the text
    # so developers can see what the model actually returned.
    logger.warning("All JSON extraction strategies failed for text (first 200 chars): %r", text[:200])
    return None, None


def build_json_instruction(schema_hint: str | None = None) -> str:
    """
    Build a text instruction telling the model to respond with JSON only.

    This instruction is appended to the system prompt before sending the
    request.  It makes the model's task explicit: "don't chat, just output
    JSON".

    Parameters
    ----------
    schema_hint : An optional description of the expected JSON structure.
                  This can be a pseudo-schema like
                  ``'{"name": str, "services": list}'``, or just plain English
                  like ``"an object with 'title' and 'description' fields"``.
                  If provided, it is appended to the instruction so the model
                  knows what shape the JSON should take.

    Returns
    -------
    str : A ready-to-use instruction string to add to the system prompt.

    Example
    -------
    >>> print(build_json_instruction('{"name": str, "score": int}'))
    You MUST respond with valid JSON only. ...
    Expected schema:
    {"name": str, "score": int}
    """
    base = (
        "You MUST respond with valid JSON only. "
        "Do NOT include any explanation, markdown fences, or prose outside the JSON object. "
        "Output a single JSON object or array and nothing else."
    )
    if schema_hint:
        # Append the expected structure so the model knows what fields to include
        return f"{base}\n\nExpected schema:\n{schema_hint}"
    return base
