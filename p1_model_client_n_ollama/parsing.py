"""
Tinker Model Client — Structured output extraction.

The models are instructed to return JSON but they often wrap it in markdown
fences or add preamble text.  This module tries several strategies in order
before giving up.
"""

from __future__ import annotations

import json
import re
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Extraction strategies (tried in order)
# ---------------------------------------------------------------------------

def _try_direct(text: str) -> dict | list | None:
    """Text is already valid JSON."""
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return None


def _try_fenced(text: str) -> dict | list | None:
    """Extract JSON from a markdown code fence:  ```json … ``` or ``` … ```"""
    pattern = r"```(?:json)?\s*\n?(.*?)```"
    match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            return None
    return None


def _try_first_brace(text: str) -> dict | list | None:
    """Find the first { or [ and try to parse from there."""
    for start_char, end_char in [('{', '}'), ('[', ']')]:
        start = text.find(start_char)
        if start == -1:
            continue
        # Walk to find the balanced closing bracket
        depth = 0
        in_string = False
        escape_next = False
        for i, ch in enumerate(text[start:], start=start):
            if escape_next:
                escape_next = False
                continue
            if ch == '\\' and in_string:
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == start_char:
                depth += 1
            elif ch == end_char:
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break
    return None


def _try_relaxed(text: str) -> dict | list | None:
    """
    Last-resort: strip common LLM preamble lines and retry the above
    strategies on the remainder.
    """
    lines = text.splitlines()
    # Drop lines that look like preamble ("Here is the JSON:", "Sure!", etc.)
    json_start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(('{', '[', '```')):
            json_start = i
            break
    if json_start is not None and json_start > 0:
        remainder = "\n".join(lines[json_start:])
        return _try_fenced(remainder) or _try_first_brace(remainder)
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

STRATEGIES = [
    ("direct",      _try_direct),
    ("fenced",      _try_fenced),
    ("first_brace", _try_first_brace),
    ("relaxed",     _try_relaxed),
]


def extract_json(text: str) -> tuple[dict | list | None, str | None]:
    """
    Try to extract a JSON object or array from *text*.

    Returns
    -------
    (parsed, strategy_name)
        parsed        – the parsed Python object, or None if all strategies failed
        strategy_name – which strategy succeeded, or None
    """
    for name, fn in STRATEGIES:
        result = fn(text)
        if result is not None:
            logger.debug("JSON extraction succeeded via strategy '%s'", name)
            return result, name

    logger.warning("All JSON extraction strategies failed for text (first 200 chars): %r", text[:200])
    return None, None


def build_json_instruction(schema_hint: str | None = None) -> str:
    """
    Returns a system-prompt addendum instructing the model to output JSON.
    """
    base = (
        "You MUST respond with valid JSON only. "
        "Do NOT include any explanation, markdown fences, or prose outside the JSON object. "
        "Output a single JSON object or array and nothing else."
    )
    if schema_hint:
        return f"{base}\n\nExpected schema:\n{schema_hint}"
    return base
