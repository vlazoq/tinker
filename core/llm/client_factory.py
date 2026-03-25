"""
core/llm/client_factory.py
===========================

Factory for creating LLM router / client instances.

Why a factory?
--------------
``ModelRouter`` and ``OllamaClient`` should not be constructed inline in
application code.  The factory centralises the decision and reads the
``TINKER_LLM_BACKEND`` environment variable so the backend can be swapped
without code changes (12-factor style, OCP / DIP).

Supported backends
------------------
``ollama`` (default)
    ``ModelRouter`` wrapping two ``OllamaClient`` instances — one for the
    main server, one for the secondary (lighter) machine.

``stub``
    Returns a no-op stub router useful for tests that do not need real
    model calls.

Usage
-----
::

    # Let env decide (12-factor style):
    router = create_router()

    # Explicit ollama:
    router = create_router("ollama")

    # Explicit stub (no Ollama needed):
    router = create_router("stub")

    # Custom configs (override env defaults):
    from core.llm.types import MachineConfig
    router = create_router(
        "ollama",
        server_config=MachineConfig(base_url="http://gpu-box:11434", model="qwen3:14b"),
    )
"""

from __future__ import annotations

import os
from typing import Any


def create_router(
    backend: str | None = None,
    **kwargs: Any,
) -> Any:
    """Create and return a configured LLM router.

    Parameters
    ----------
    backend : str, optional
        ``"ollama"`` or ``"stub"``.
        Defaults to ``TINKER_LLM_BACKEND`` env var, or ``"ollama"``.
    **kwargs
        Passed to the router constructor.
        For ``"ollama"``: ``server_config``, ``secondary_config``.

    Returns
    -------
    ModelRouter | StubRouter
        A fully initialised router ready for use.

    Raises
    ------
    ValueError
        If an unsupported backend name is given.
    """
    effective = (
        (backend or os.getenv("TINKER_LLM_BACKEND", "ollama")).lower().strip()
    )

    if effective == "ollama":
        from core.llm.router import ModelRouter
        from core.llm.types import MachineConfig

        return ModelRouter(
            server_config=kwargs.get("server_config") or MachineConfig.server_defaults(),
            secondary_config=kwargs.get("secondary_config") or MachineConfig.secondary_defaults(),
        )

    if effective == "stub":
        from core.llm.router import ModelRouter
        from core.llm.types import MachineConfig

        # Stub uses localhost with an obviously-fake model name so callers
        # that accidentally hit it get a clear connection error rather than a
        # silent wrong result.
        stub_cfg = MachineConfig(
            base_url="http://localhost:11434",
            model="stub-model",
        )
        return ModelRouter(
            server_config=kwargs.get("server_config") or stub_cfg,
            secondary_config=kwargs.get("secondary_config") or stub_cfg,
        )

    raise ValueError(
        f"Unknown LLM backend: {effective!r}.  Supported values: 'ollama', 'stub'."
    )
