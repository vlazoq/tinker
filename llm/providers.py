"""
llm/providers.py
================
Vendor-agnostic LLM provider abstraction.

Tinker's OllamaClient works with any OpenAI-compatible endpoint, but
wiring it to OpenAI, Anthropic, or other providers requires knowing
their base URLs, auth headers, and model name conventions.

This module provides:
  - ProviderConfig: named preset configs for common providers
  - build_client(provider, **kwargs): factory that returns a configured OllamaClient
  - PROVIDER_PRESETS: dict of known provider presets

Supported providers (out of the box):
  - "ollama"        : Local Ollama (default, no auth)
  - "openai"        : OpenAI API (requires OPENAI_API_KEY env var)
  - "anthropic"     : Anthropic API via its OpenAI-compatible endpoint
  - "azure_openai"  : Azure OpenAI (requires AZURE_OPENAI_* env vars)
  - "lm_studio"     : Local LM Studio (OpenAI-compatible, no auth)
  - "vllm"          : Local vLLM server (OpenAI-compatible, no auth)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .client import OllamaClient
from .types import MachineConfig


# ---------------------------------------------------------------------------
# Provider configuration dataclass
# ---------------------------------------------------------------------------


@dataclass
class ProviderConfig:
    """
    Named preset configuration for a single LLM provider.

    Fields
    ------
    name          : Human-readable provider identifier (e.g. "openai").
    base_url      : Root URL of the provider's API endpoint.
    api_key_env   : Name of the environment variable that holds the API key.
                    Empty string means no auth is required.
    default_model : Default model name to use when none is specified.
    extra_headers : Additional HTTP headers to send with every request
                    (e.g. API-version headers required by Azure).
    """

    name: str
    base_url: str
    api_key_env: str = ""
    default_model: str = ""
    extra_headers: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Known provider presets
# ---------------------------------------------------------------------------

PROVIDER_PRESETS: dict[str, ProviderConfig] = {
    "ollama": ProviderConfig(
        name="ollama",
        base_url="http://localhost:11434",
        api_key_env="",
        default_model="qwen3:7b",
    ),
    "openai": ProviderConfig(
        name="openai",
        base_url="https://api.openai.com",
        api_key_env="OPENAI_API_KEY",
        default_model="gpt-4o",
    ),
    "anthropic": ProviderConfig(
        name="anthropic",
        base_url="https://api.anthropic.com",
        api_key_env="ANTHROPIC_API_KEY",
        default_model="claude-sonnet-4-6",
    ),
    "azure_openai": ProviderConfig(
        name="azure_openai",
        # The endpoint is deployment-specific, so we pull it from the env.
        # An empty string here signals build_client to look up AZURE_OPENAI_ENDPOINT.
        base_url="",
        api_key_env="AZURE_OPENAI_API_KEY",
        default_model="gpt-4o",
    ),
    "lm_studio": ProviderConfig(
        name="lm_studio",
        base_url="http://localhost:1234",
        api_key_env="",
        default_model="",
    ),
    "vllm": ProviderConfig(
        name="vllm",
        base_url="http://localhost:8000",
        api_key_env="",
        default_model="",
    ),
}


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------


def build_client(
    provider: str = "ollama",
    model: str = "",
    base_url: str = "",
    api_key: str = "",
    **kwargs,
) -> OllamaClient:
    """
    Build a fully configured ``OllamaClient`` for the given provider.

    Resolution order
    ----------------
    base_url  : explicit arg > environment variable (for azure_openai) > preset default
    api_key   : explicit arg > environment variable named in preset.api_key_env
    model     : explicit arg > preset.default_model

    Parameters
    ----------
    provider  : One of the keys in ``PROVIDER_PRESETS`` (default: "ollama").
    model     : Model name override.  Falls back to the preset's default_model.
    base_url  : URL override.  Falls back to env var / preset default.
    api_key   : API key override.  Falls back to the env var named in the preset.
    **kwargs  : Additional keyword arguments forwarded to ``MachineConfig``
                (e.g. ``context_window``, ``request_timeout``).

    Returns
    -------
    OllamaClient : A ready-to-use async client pointed at the chosen provider.

    Raises
    ------
    ValueError : If ``provider`` is not a recognised preset name.
    """
    preset = PROVIDER_PRESETS.get(provider)
    if preset is None:
        known = ", ".join(sorted(PROVIDER_PRESETS))
        raise ValueError(
            f"Unknown provider {provider!r}. Known providers: {known}"
        )

    # ── Resolve base_url ────────────────────────────────────────────────────
    # For azure_openai the preset stores an empty string; the real endpoint
    # lives in an environment variable because it is deployment-specific.
    if base_url:
        resolved_url = base_url
    elif provider == "azure_openai":
        resolved_url = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
    else:
        resolved_url = preset.base_url

    # ── Resolve API key ─────────────────────────────────────────────────────
    if api_key:
        resolved_key = api_key
    elif preset.api_key_env:
        resolved_key = os.environ.get(preset.api_key_env, "")
    else:
        resolved_key = ""

    # ── Resolve model ───────────────────────────────────────────────────────
    resolved_model = model or preset.default_model

    # ── Build auth headers ──────────────────────────────────────────────────
    headers: dict[str, str] = dict(preset.extra_headers)
    if resolved_key:
        headers["Authorization"] = f"Bearer {resolved_key}"

    # ── Construct MachineConfig ─────────────────────────────────────────────
    # MachineConfig doesn't have a built-in headers field, so we store the
    # auth information in the session headers via a subclassed config.
    # Since OllamaClient creates its aiohttp session with only Content-Type,
    # we extend MachineConfig to carry extra headers that the client can use.
    config = _MachineConfigWithHeaders(
        base_url=resolved_url,
        model=resolved_model,
        extra_headers=headers,
        **kwargs,
    )

    return _OllamaClientWithHeaders(config)


# ---------------------------------------------------------------------------
# Internal helpers: MachineConfig and OllamaClient subclasses that carry
# provider-level HTTP headers (e.g. Authorization) into the aiohttp session.
# ---------------------------------------------------------------------------


@dataclass
class _MachineConfigWithHeaders(MachineConfig):
    """
    MachineConfig extended with an ``extra_headers`` dict.

    This allows ``build_client`` to pass auth headers (and any other
    provider-specific headers) through to the HTTP session without
    modifying the upstream MachineConfig dataclass.
    """

    extra_headers: dict = field(default_factory=dict)


class _OllamaClientWithHeaders(OllamaClient):
    """
    OllamaClient subclass that merges provider-level headers into every
    aiohttp session it creates.

    Overrides only ``_get_session`` — all retry, circuit-breaker, and
    streaming logic is inherited unchanged from OllamaClient.
    """

    async def _get_session(self):  # type: ignore[override]
        """
        Create (or reuse) an aiohttp session that includes the provider's
        extra headers (e.g. ``Authorization: Bearer …``).
        """
        # Let the parent build / reuse its session first.
        session = await super()._get_session()

        # Merge extra headers into the session's default headers.
        # aiohttp's CIMultiDictProxy is read-only, but we can update
        # _default_headers which is the mutable backing store.
        extra: dict = getattr(self.config, "extra_headers", {})
        if extra:
            session.headers.update(extra)

        return session
