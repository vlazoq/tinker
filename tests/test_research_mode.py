"""
tests/test_research_mode.py
============================
Tests for the architect/research mode switching feature.

Covers:
  - SystemMode enum and OrchestratorConfig validation
  - Research-mode prompt builders in agents/_shared.py
  - _read_system_mode helper
  - Mode API endpoint (GET/POST /api/mode)
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# SystemMode & OrchestratorConfig tests
# ---------------------------------------------------------------------------


class TestSystemMode:
    def test_enum_values(self):
        from runtime.orchestrator.config import SystemMode

        assert SystemMode.ARCHITECT == "architect"
        assert SystemMode.RESEARCH == "research"
        assert len(SystemMode) == 2

    def test_config_default_mode(self):
        from runtime.orchestrator.config import OrchestratorConfig

        config = OrchestratorConfig()
        assert config.system_mode == "architect"
        assert config.research_topic == ""

    def test_config_research_mode(self):
        from runtime.orchestrator.config import OrchestratorConfig

        config = OrchestratorConfig(system_mode="research", research_topic="sci-fi writing")
        assert config.system_mode == "research"
        assert config.research_topic == "sci-fi writing"

    def test_config_invalid_mode(self):
        from exceptions import ConfigurationError
        from runtime.orchestrator.config import OrchestratorConfig

        with pytest.raises(ConfigurationError, match="system_mode"):
            OrchestratorConfig(system_mode="invalid")

    def test_config_from_env(self):
        from runtime.orchestrator.config import OrchestratorConfig

        with patch.dict(os.environ, {
            "TINKER_SYSTEM_MODE": "research",
            "TINKER_RESEARCH_TOPIC": "quantum computing",
        }):
            config = OrchestratorConfig()
            assert config.system_mode == "research"
            assert config.research_topic == "quantum computing"


# ---------------------------------------------------------------------------
# Prompt builder tests
# ---------------------------------------------------------------------------


class TestResearchPrompts:
    def test_architect_prompts_default_mode(self):
        from agents._shared import _build_architect_prompts

        system, user = _build_architect_prompts(
            task_desc="Design caching layer",
            subsystem="memory_manager",
            context_str="prior context",
            grub_section="",
            constraints_str="",
        )
        assert "software architect" in system.lower() or "architect" in system.lower()
        assert "research" not in system.lower()

    def test_architect_prompts_research_mode(self):
        from agents._shared import _build_architect_prompts

        system, user = _build_architect_prompts(
            task_desc="Compare HNSW vs IVF for vector search",
            subsystem="vector_databases",
            context_str="prior findings",
            grub_section="",
            constraints_str="",
            system_mode="research",
            research_topic="vector search algorithms",
        )
        assert "research" in system.lower()
        assert "vector search algorithms" in system
        assert "vector_databases" in user

    def test_critic_prompts_default_mode(self):
        from agents._shared import _build_critic_prompts

        system, user = _build_critic_prompts(
            task_desc="Review design",
            design_content="Some design content",
        )
        assert "critic" in system.lower() or "architect" in system.lower()

    def test_critic_prompts_research_mode(self):
        from agents._shared import _build_critic_prompts

        system, user = _build_critic_prompts(
            task_desc="Review research findings",
            design_content="Research content here",
            system_mode="research",
        )
        assert "research" in system.lower()
        assert "accuracy" in system.lower() or "completeness" in system.lower()

    def test_synthesizer_prompts_meso_research(self):
        from agents._shared import _build_synthesizer_prompts

        system, user = _build_synthesizer_prompts(
            "meso",
            system_mode="research",
            subsystem="machine_learning",
            artifacts=[{"content": "finding 1"}, {"content": "finding 2"}],
        )
        assert "research" in system.lower() or "synthesise" in system.lower()
        assert "machine_learning" in user

    def test_synthesizer_prompts_macro_research(self):
        from agents._shared import _build_synthesizer_prompts

        system, user = _build_synthesizer_prompts(
            "macro",
            system_mode="research",
            documents=[{"content": "summary 1"}],
            snapshot_version=3,
            total_micro_loops=15,
        )
        assert "research" in system.lower()
        assert "v3" in user


# ---------------------------------------------------------------------------
# _read_system_mode tests
# ---------------------------------------------------------------------------


class TestReadSystemMode:
    def test_no_control_file(self, tmp_path):
        with patch.dict(os.environ, {"TINKER_CONTROL_DIR": str(tmp_path)}):
            from agents._shared import _read_system_mode

            mode, topic = _read_system_mode()
            assert mode == "architect"
            assert topic == ""

    def test_reads_mode_file(self, tmp_path):
        mode_file = tmp_path / "mode.json"
        mode_file.write_text(json.dumps({
            "system_mode": "research",
            "research_topic": "sci-fi writing styles",
        }))
        with patch.dict(os.environ, {"TINKER_CONTROL_DIR": str(tmp_path)}):
            from agents._shared import _read_system_mode

            mode, topic = _read_system_mode()
            assert mode == "research"
            assert topic == "sci-fi writing styles"

    def test_corrupt_mode_file(self, tmp_path):
        mode_file = tmp_path / "mode.json"
        mode_file.write_text("not valid json{{{")
        with patch.dict(os.environ, {"TINKER_CONTROL_DIR": str(tmp_path)}):
            from agents._shared import _read_system_mode

            mode, topic = _read_system_mode()
            assert mode == "architect"
            assert topic == ""


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestModeAPI:
    @pytest.fixture
    def client(self, tmp_path):
        """Create a minimal FastAPI test client with just the orchestrator routes."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from ui.web.routes.orchestrator_ctrl import router

        test_app = FastAPI()
        test_app.include_router(router)
        return TestClient(test_app)

    @pytest.fixture(autouse=True)
    def _use_tmp_control_dir(self, tmp_path):
        with patch.dict(os.environ, {"TINKER_CONTROL_DIR": str(tmp_path)}):
            yield

    def test_get_mode_default(self, client):
        resp = client.get("/api/mode")
        assert resp.status_code == 200
        data = resp.json()
        assert data["system_mode"] == "architect"
        assert "valid_modes" in data

    def test_set_mode_research(self, client):
        resp = client.post("/api/mode", json={
            "system_mode": "research",
            "research_topic": "quantum computing",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["system_mode"] == "research"

        # Verify it persisted
        resp2 = client.get("/api/mode")
        assert resp2.json()["system_mode"] == "research"
        assert resp2.json()["research_topic"] == "quantum computing"

    def test_set_mode_research_no_topic(self, client):
        resp = client.post("/api/mode", json={
            "system_mode": "research",
            "research_topic": "",
        })
        assert resp.status_code == 422

    def test_set_mode_invalid(self, client):
        resp = client.post("/api/mode", json={
            "system_mode": "invalid_mode",
        })
        assert resp.status_code == 422

    def test_switch_back_to_architect(self, client):
        # First switch to research
        client.post("/api/mode", json={
            "system_mode": "research",
            "research_topic": "AI safety",
        })
        # Then switch back
        resp = client.post("/api/mode", json={
            "system_mode": "architect",
            "research_topic": "",
        })
        assert resp.status_code == 200
        assert resp.json()["system_mode"] == "architect"
