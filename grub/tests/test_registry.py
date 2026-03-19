"""
grub/tests/test_registry.py
============================
Tests for MinionRegistry — minion registration, skill loading, get_minion().
"""

import pytest

from grub.config import GrubConfig
from grub.registry import MinionRegistry
from grub.minions.base import BaseMinion
from grub.contracts.task import GrubTask
from grub.contracts.result import MinionResult, ResultStatus


# ── A minimal stub Minion for testing ─────────────────────────────────────────


class StubMinion(BaseMinion):
    MINION_NAME = "stub"
    BASE_SYSTEM_PROMPT = "You are a stub."

    async def run(self, task: GrubTask) -> MinionResult:
        return MinionResult(
            task_id=task.id,
            minion_name=self.name,
            status=ResultStatus.SUCCESS,
            score=1.0,
        )


# ═══════════════════════════════════════════════════════════════════════════════


class TestMinionRegistry:
    @pytest.fixture
    def registry(self):
        cfg = GrubConfig()
        return MinionRegistry(cfg)

    def test_register_and_list(self, registry):
        registry.register_minion("stub", StubMinion)
        assert "stub" in registry.list_minions()

    def test_get_minion_returns_instance(self, registry):
        registry.register_minion("stub", StubMinion)
        minion = registry.get_minion("stub")
        assert isinstance(minion, StubMinion)

    def test_get_unknown_minion_raises_key_error(self, registry):
        with pytest.raises(KeyError, match="not registered"):
            registry.get_minion("does_not_exist")

    def test_each_get_returns_fresh_instance(self, registry):
        registry.register_minion("stub", StubMinion)
        m1 = registry.get_minion("stub")
        m2 = registry.get_minion("stub")
        assert m1 is not m2  # fresh instance each time

    def test_register_skill_and_get(self, registry):
        registry.register_skill("my_skill", "You are an expert.")
        text = registry.get_skill("my_skill")
        assert text == "You are an expert."

    def test_get_unknown_skill_returns_empty_string(self, registry):
        text = registry.get_skill("completely_unknown_skill_xyz")
        assert text == ""

    def test_load_skill_from_file(self, tmp_path, monkeypatch):
        """Skills are auto-loaded from the skills/ directory."""
        skill_dir = tmp_path / "skills"
        skill_dir.mkdir()
        (skill_dir / "my_file_skill.md").write_text("# Expert skill content")

        cfg = GrubConfig()
        registry = MinionRegistry(cfg)
        # Patch the skills dir to point to our temp directory
        monkeypatch.setattr(registry, "_skills_dir", skill_dir)
        registry.load_all_skills()

        text = registry.get_skill("my_file_skill")
        assert "Expert skill content" in text

    def test_load_all_skills_finds_md_files(self, tmp_path, monkeypatch):
        skill_dir = tmp_path / "skills"
        skill_dir.mkdir()
        (skill_dir / "skill_a.md").write_text("Skill A")
        (skill_dir / "skill_b.md").write_text("Skill B")
        (skill_dir / "not_a_skill.json").write_text("{}")

        cfg = GrubConfig()
        registry = MinionRegistry(cfg)
        monkeypatch.setattr(registry, "_skills_dir", skill_dir)
        registry.load_all_skills()

        skills = registry.list_skills()
        assert "skill_a" in skills
        assert "skill_b" in skills
        assert "not_a_skill" not in skills

    def test_minion_receives_skills(self, registry, tmp_path, monkeypatch):
        """Skills assigned in config are injected into the Minion at instantiation."""
        registry.register_minion("stub", StubMinion)
        registry.register_skill("test_skill", "Expert knowledge here")
        # Assign this skill to the stub minion in config
        registry._config.minion_skills["stub"] = ["test_skill"]

        minion = registry.get_minion("stub")
        assert "Expert knowledge here" in minion.skills

    def test_load_defaults_registers_all_builtin_minions(self):
        cfg = GrubConfig()
        # Use smaller/faster models so the test doesn't need Ollama
        cfg.models = {k: "qwen3:0.6b" for k in cfg.models}
        registry = MinionRegistry(cfg)
        registry.load_defaults()

        expected = {"coder", "reviewer", "tester", "debugger", "refactorer"}
        registered = set(registry.list_minions())
        assert expected.issubset(registered)

    def test_summary(self, registry):
        registry.register_minion("stub", StubMinion)
        registry.register_skill("sk", "skill text")
        s = registry.summary()
        assert "stub" in s["minions"]
        assert "sk" in s["skills"]
