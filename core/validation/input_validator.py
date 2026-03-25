"""
core/validation/input_validator.py
==============================

Input validation and sanitization for all Tinker data boundaries.

Why validation matters
-----------------------
Tinker processes inputs from multiple untrusted sources:
  - User-supplied problem statements (from command line)
  - AI-generated task descriptions (from Architect/Critic/Synthesizer)
  - URLs returned by web search (from SearXNG)
  - File paths for artifact output (from config or AI output)
  - JSON responses from AI models (from Ollama)

Without validation:
  - A user could inject a prompt that makes the AI output malicious tasks
  - A web search could return a URL that causes the scraper to read local files
  - An AI could output a file path like "../../etc/passwd" for artifact storage
  - A corrupt AI response could crash the micro loop with a KeyError
  - A base64-encoded payload could smuggle injection through the string check

This module provides validators for all major input types.
Validation happens at the boundary — not deep in the business logic.

Key features
-------------
- ``sanitize_string``: strip control chars, Unicode-normalise (NFC), truncate
- ``check_prompt_injection``: regex heuristics + encoded-payload detection
  (base64 and URL-encoded payloads are decoded and re-checked)
- ``validate_batch``: run multiple validators at once, collect all errors
- ``validate_problem_statement``, ``validate_task``, ``validate_url``,
  ``validate_file_path``, ``validate_ai_json``: domain-specific validators

Usage
------
::

    from core.validation.input_validator import (
        validate_problem_statement,
        validate_task,
        validate_url,
        validate_file_path,
        validate_ai_json,
        validate_batch,
    )

    # Validate user input at startup:
    safe_problem = validate_problem_statement(raw_problem)

    # Validate task before passing to Architect:
    safe_task = validate_task(raw_task)

    # Validate URL before scraping:
    safe_url = validate_url(raw_url)

    # Validate AI output before storing:
    safe_output = validate_ai_json(raw_output, expected_keys=["content", "score"])

    # Collect all validation errors without raising on the first one:
    errors = validate_batch([
        (validate_problem_statement, raw_problem),
        (validate_url, raw_url),
    ])
    if errors:
        for err in errors:
            print(err)
"""

from __future__ import annotations

import base64
import logging
import re
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum lengths for string fields (prevents DoS via huge inputs)
MAX_PROBLEM_LENGTH = 2000
MAX_TASK_DESCRIPTION_LENGTH = 5000
MAX_SUBSYSTEM_LENGTH = 100
MAX_TASK_TITLE_LENGTH = 200
MAX_CONTENT_LENGTH = 100_000  # 100KB — reasonable artifact size
MAX_QUERY_LENGTH = 500

# Allowed URL schemes for web scraping (prevents file:// and other unsafe schemes)
ALLOWED_URL_SCHEMES = {"http", "https"}

# Blocked URL patterns (localhost, private networks)
BLOCKED_URL_PATTERNS = [
    r"localhost",
    r"127\.\d+\.\d+\.\d+",
    r"10\.\d+\.\d+\.\d+",
    r"172\.(1[6-9]|2\d|3[01])\.\d+\.\d+",
    r"192\.168\.\d+\.\d+",
    r"0\.0\.0\.0",
    r"::1",
    r"file://",
    r"ftp://",
]

# Regex for detecting suspicious prompt injection patterns.
# Covers common jailbreak and context-hijacking phrasing.
# Note: this is a heuristic filter — not a security guarantee.
INJECTION_PATTERNS = [
    # Classic "ignore previous instructions" variants
    r"ignore\s+previous\s+instructions",
    r"ignore\s+all\s+previous",
    r"disregard\s+(your\s+)?(previous\s+)?instructions",
    r"disregard\s+(all\s+)?prior",
    r"forget\s+(your\s+)?(previous\s+|all\s+)?instructions",
    r"override\s+(your\s+)?(previous\s+|all\s+)?instructions",
    # Role / persona hijacking
    r"\bact\s+as\s+(a\s+|an\s+)?(?!architect|critic|synthesizer)\w",
    r"\bpretend\s+(you\s+are|to\s+be)\b",
    r"\byou\s+are\s+now\b",
    r"\bnew\s+persona\b",
    r"\bassume\s+(the\s+)?role\s+of\b",
    r"\brespond\s+as\s+(?!an?\s+architect|a\s+critic)\b",
    # System prompt injection markers
    r"system\s*:\s*you\s+are",
    r"<\s*/?\s*(system|human|assistant)\s*>",
    r"\[SYSTEM\]",
    r"\[INST\]",
    r"<<SYS>>",
    # Jailbreak keywords
    r"\bjailbreak\b",
    r"\bdan\s+mode\b",  # "Do Anything Now" jailbreak
    r"\bgrandma\s+trick\b",
    r"\btoken\s+smuggling\b",
    r"\bprompt\s+injection\b",
    r"\bprompt\s+leak\b",
    # Instruction boundary manipulation
    r"---+\s*end\s+of\s+(system\s+)?prompt",
    r"---+\s*new\s+instructions",
    r"\bstop\s+following\s+(your\s+)?instructions\b",
]


# ValidationError is defined in the central exceptions module (inheriting
# from both TinkerError and ValueError for backwards compatibility) and
# re-exported here so ``from core.validation.input_validator import ValidationError``
# continues to work.
from exceptions import ValidationError  # noqa: E402, F401  (intentional re-export)


# ---------------------------------------------------------------------------
# String sanitization
# ---------------------------------------------------------------------------


def sanitize_string(
    value: Any,
    max_length: int = MAX_CONTENT_LENGTH,
    field: str = "string",
    allow_empty: bool = False,
) -> str:
    """
    Sanitize a string: enforce type, strip control characters, limit length.

    Parameters
    ----------
    value      : The raw value to sanitize (will be coerced to str).
    max_length : Maximum allowed length (truncates with a warning).
    field      : Field name for error messages.
    allow_empty: If False, raises ValidationError for empty strings.

    Returns
    -------
    str : The sanitized string.

    Raises
    ------
    ValidationError : If the value cannot be coerced to string or is empty.
    """
    if value is None:
        if allow_empty:
            return ""
        raise ValidationError(field, value, "field is required (got None)")

    # Coerce to string
    try:
        text = str(value)
    except Exception:
        raise ValidationError(field, value, "cannot convert to string")

    # Remove null bytes and other dangerous control characters
    # (keep \n, \r, \t which are legitimate in multi-line text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)

    # Normalise Unicode to NFC (prevents homograph attacks)
    text = unicodedata.normalize("NFC", text)

    # Strip leading/trailing whitespace
    text = text.strip()

    if not text and not allow_empty:
        raise ValidationError(field, value, "field cannot be empty after sanitization")

    # Truncate oversized values (warn but don't error — be lenient about length)
    if len(text) > max_length:
        logger.warning(
            "Sanitizing '%s': truncating from %d to %d characters",
            field,
            len(text),
            max_length,
        )
        text = text[:max_length]

    return text


# ---------------------------------------------------------------------------
# Prompt injection detection
# ---------------------------------------------------------------------------


def _decode_encoded_payloads(text: str) -> list[str]:
    """
    Attempt to decode base64 and URL-encoded payloads embedded in text.

    Returns a list of decoded strings to check alongside the original.
    This catches injection attempts that wrap their payload in encoding to
    bypass naive regex filters.

    Only decodes chunks that look like well-formed encoded data — does not
    raise on decode failures.
    """
    decoded: list[str] = []

    # URL-decode: catch %XX-encoded sequences
    try:
        url_decoded = urllib.parse.unquote(text)
        if url_decoded != text:
            decoded.append(url_decoded)
    except Exception:
        pass

    # Base64-decode: find long runs of base64 chars and try to decode them
    # Pattern: 20+ contiguous base64 chars (likely encoded payload, not a word)
    b64_candidates = re.findall(r"[A-Za-z0-9+/]{20,}={0,2}", text)
    for candidate in b64_candidates:
        try:
            raw = base64.b64decode(candidate + "==")  # pad to avoid errors
            decoded_str = raw.decode("utf-8", errors="ignore")
            if decoded_str and len(decoded_str) >= 10:
                decoded.append(decoded_str)
        except Exception:
            pass

    return decoded


def check_prompt_injection(text: str, field: str = "input") -> Optional[str]:
    """
    Detect potential prompt injection attempts in a string.

    Checks the raw text AND any base64/URL-decoded payloads embedded in it,
    so encoding tricks cannot bypass the heuristic filter.

    Returns a warning message if suspicious patterns are found, or None if clean.

    Note: This is a heuristic best-effort check, not a security guarantee.
    It prevents obvious injection attempts but may miss sophisticated ones.
    """
    candidates = [text] + _decode_encoded_payloads(text)

    for candidate in candidates:
        lower = candidate.lower()
        for pattern in INJECTION_PATTERNS:
            if re.search(pattern, lower, re.IGNORECASE):
                source = "" if candidate is text else " (in encoded payload)"
                msg = (
                    f"Potential prompt injection detected in '{field}'{source}: "
                    f"pattern '{pattern}'"
                )
                logger.warning(msg)
                return msg
    return None


def validate_batch(
    validators: list[tuple[Callable, Any]],
) -> list[str]:
    """
    Run multiple validators, collecting all errors instead of raising on the first.

    Each entry is a ``(validator_fn, raw_value)`` tuple.  The validator is
    called as ``validator_fn(raw_value)`` and is expected to raise
    ``ValidationError`` on failure.

    Parameters
    ----------
    validators : List of ``(callable, value)`` pairs to validate.

    Returns
    -------
    list[str] : List of error messages.  Empty list means all passed.

    Example
    -------
    ::

        errors = validate_batch([
            (validate_problem_statement, raw_problem),
            (validate_url, raw_url),
        ])
        if errors:
            for e in errors:
                logger.warning(e)
    """
    errors: list[str] = []
    for validator, value in validators:
        try:
            validator(value)
        except ValidationError as exc:
            errors.append(str(exc))
        except Exception as exc:
            errors.append(f"Unexpected validation error: {exc}")
    return errors


# ---------------------------------------------------------------------------
# Domain-specific validators
# ---------------------------------------------------------------------------


def validate_problem_statement(raw: Any) -> str:
    """
    Validate and sanitize a user-supplied problem statement.

    The problem statement is the main user input and should be:
    - A non-empty string
    - No longer than 2000 characters
    - Free of obvious prompt injection patterns

    Parameters
    ----------
    raw : The raw problem statement from the command line or API.

    Returns
    -------
    str : The sanitized problem statement.

    Raises
    ------
    ValidationError : If the input is invalid.
    """
    text = sanitize_string(
        raw, max_length=MAX_PROBLEM_LENGTH, field="problem_statement"
    )

    # Warn (but don't block) on suspected injection
    check_prompt_injection(text, "problem_statement")

    logger.debug("Problem statement validated (%d chars)", len(text))
    return text


def validate_task(raw: Any) -> dict:
    """
    Validate a task dict before passing it to the Architect AI.

    Ensures the task has required fields and that all string values are safe.

    Parameters
    ----------
    raw : The raw task dict from the task engine.

    Returns
    -------
    dict : The validated (and sanitized) task dict.

    Raises
    ------
    ValidationError : If the task is missing required fields.
    """
    if not isinstance(raw, dict):
        raise ValidationError("task", raw, f"expected dict, got {type(raw).__name__}")

    task_id = raw.get("id")
    if not task_id or not isinstance(task_id, str):
        raise ValidationError(
            "task.id", task_id, "task must have a non-empty string id"
        )

    # Sanitize string fields (non-destructive — keeps dict structure)
    safe = dict(raw)

    if "description" in safe:
        safe["description"] = sanitize_string(
            safe["description"],
            max_length=MAX_TASK_DESCRIPTION_LENGTH,
            field="task.description",
            allow_empty=True,
        )
        check_prompt_injection(safe["description"], "task.description")

    if "title" in safe:
        safe["title"] = sanitize_string(
            safe["title"],
            max_length=MAX_TASK_TITLE_LENGTH,
            field="task.title",
            allow_empty=True,
        )

    if "subsystem" in safe and safe["subsystem"]:
        safe["subsystem"] = sanitize_string(
            safe["subsystem"],
            max_length=MAX_SUBSYSTEM_LENGTH,
            field="task.subsystem",
            allow_empty=True,
        )
        # Subsystem names should be alphanumeric (slugs)
        if safe["subsystem"] and not re.match(
            r"^[a-zA-Z0-9_\-. ]+$", safe["subsystem"]
        ):
            logger.warning(
                "task.subsystem contains unexpected characters: '%s'", safe["subsystem"]
            )

    return safe


def validate_url(raw: Any, field: str = "url") -> str:
    """
    Validate a URL before fetching it (web search results, scraping targets).

    Enforces:
    - Valid URL format
    - Only http/https schemes (no file://, ftp://, etc.)
    - Not a private/localhost address

    Parameters
    ----------
    raw   : The raw URL string to validate.
    field : Field name for error messages.

    Returns
    -------
    str : The validated URL.

    Raises
    ------
    ValidationError : If the URL is invalid or blocked.
    """
    url = sanitize_string(raw, max_length=2000, field=field)

    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise ValidationError(field, url, f"URL parse failed: {exc}") from exc

    if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
        raise ValidationError(
            field, url, f"URL scheme '{parsed.scheme}' not allowed (only http/https)"
        )

    host = parsed.hostname or ""
    for pattern in BLOCKED_URL_PATTERNS:
        if re.search(pattern, host, re.IGNORECASE):
            raise ValidationError(
                field,
                url,
                f"URL blocked: host '{host}' matches restricted pattern '{pattern}'",
            )

    return url


def validate_file_path(
    raw: Any,
    base_dir: str,
    field: str = "file_path",
) -> Path:
    """
    Validate and sanitize a file path, preventing path traversal attacks.

    Ensures the resolved path stays within ``base_dir``.

    Parameters
    ----------
    raw      : The raw path string (possibly from AI output).
    base_dir : The allowed base directory — all paths must be under this dir.
    field    : Field name for error messages.

    Returns
    -------
    Path : The resolved, safe absolute path.

    Raises
    ------
    ValidationError : If the path would escape ``base_dir``.
    """
    raw_str = sanitize_string(raw, max_length=500, field=field)

    # Remove null bytes that could confuse the OS (sanitize_string already does
    # this, but be explicit here since path-handling is security-critical)
    raw_str = raw_str.replace("\x00", "")

    # Reject absolute paths outright — only relative paths are accepted.
    # In pathlib, (base / "/abs/path") silently drops the base and returns
    # "/abs/path", so this guard must come before the join to prevent that
    # corner case from bypassing the relative_to() check.
    if Path(raw_str).is_absolute():
        raise ValidationError(
            field,
            raw,
            f"Absolute paths are not allowed: '{raw_str}'",
        )

    base = Path(base_dir).resolve()
    candidate = (base / raw_str).resolve()

    # Ensure the resolved path is under base_dir.
    # Path.resolve() follows symlinks on Python 3.6+, so symlink attacks that
    # would point outside the base directory are caught by relative_to().
    try:
        candidate.relative_to(base)
    except ValueError:
        raise ValidationError(
            field,
            raw,
            f"Path traversal detected: '{raw_str}' would escape base dir '{base_dir}'",
        )

    return candidate


def validate_ai_json(
    raw: Any,
    expected_keys: Optional[list[str]] = None,
    field: str = "ai_output",
) -> dict:
    """
    Validate JSON output from an AI model.

    Ensures the output is a dict (not a list or primitive) and contains
    all expected keys.

    Parameters
    ----------
    raw           : The raw AI output (already JSON-decoded dict or None).
    expected_keys : Keys that must be present in the output.
    field         : Field name for error messages.

    Returns
    -------
    dict : The validated AI output.

    Raises
    ------
    ValidationError : If the output is not a dict or is missing required keys.
    """
    if raw is None:
        raise ValidationError(field, raw, "AI output is None — model may have failed")

    if not isinstance(raw, dict):
        raise ValidationError(
            field,
            type(raw).__name__,
            f"Expected dict from AI, got {type(raw).__name__}",
        )

    if expected_keys:
        missing = [k for k in expected_keys if k not in raw]
        if missing:
            raise ValidationError(
                field, list(raw.keys()), f"AI output missing required keys: {missing}"
            )

    return raw


def validate_config_value(
    value: Any,
    name: str,
    value_type: type,
    min_val: Any = None,
    max_val: Any = None,
) -> Any:
    """
    Validate a configuration value (timeout, count, etc.).

    Ensures the value is the right type and within the allowed range.

    Parameters
    ----------
    value      : The config value to validate.
    name       : Config field name (for error messages).
    value_type : Expected Python type (e.g. float, int).
    min_val    : Optional minimum value.
    max_val    : Optional maximum value.

    Returns
    -------
    The validated value (cast to value_type).

    Raises
    ------
    ValidationError : If the value is invalid.
    """
    try:
        cast = value_type(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            name, value, f"Cannot cast to {value_type.__name__}: {exc}"
        ) from exc

    if min_val is not None and cast < min_val:
        raise ValidationError(name, cast, f"Value {cast} is below minimum {min_val}")

    if max_val is not None and cast > max_val:
        raise ValidationError(name, cast, f"Value {cast} exceeds maximum {max_val}")

    return cast
