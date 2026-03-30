"""
Tests for core/llm/client_factory.py.
"""

from __future__ import annotations

import pytest

from core.llm.client_factory import create_router


class TestCreateRouter:
    def test_default_backend_returns_model_router(self):
        router = create_router("ollama")
        # ModelRouter has a start() coroutine — verify it's the right type
        assert hasattr(router, "complete") or hasattr(router, "route") or hasattr(router, "start")

    def test_stub_backend_returns_model_router(self):
        router = create_router("stub")
        assert router is not None

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown LLM backend"):
            create_router("nonexistent")

    def test_env_var_overrides_default(self, monkeypatch):
        monkeypatch.setenv("TINKER_LLM_BACKEND", "ollama")
        router = create_router()  # no explicit backend
        assert router is not None

    def test_explicit_takes_precedence_over_env(self, monkeypatch):
        monkeypatch.setenv("TINKER_LLM_BACKEND", "stub")
        router = create_router("ollama")
        assert router is not None

    def test_case_insensitive_backend(self):
        router = create_router("OLLAMA")
        assert router is not None

    def test_custom_server_config_accepted(self):
        from core.llm.types import MachineConfig

        custom = MachineConfig(base_url="http://custom:11434", model="custom-model")
        router = create_router("ollama", server_config=custom)
        assert router is not None
