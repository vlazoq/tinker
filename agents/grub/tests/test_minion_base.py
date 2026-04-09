"""
agents/grub/tests/test_minion_base.py
==============================
Tests for BaseMinion helpers — code block extraction, score parsing,
system prompt building.  No LLM calls needed.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.grub.config import GrubConfig
from agents.grub.contracts.result import MinionResult, ResultStatus
from agents.grub.contracts.task import GrubTask
from agents.grub.minions.base import BaseMinion

# ── Concrete subclass for testing ─────────────────────────────────────────────


class ConcreteMinion(BaseMinion):
    MINION_NAME = "concrete"
    BASE_SYSTEM_PROMPT = "You are a test minion."

    async def run(self, task: GrubTask) -> MinionResult:
        return MinionResult(task_id=task.id, minion_name=self.name, status=ResultStatus.SUCCESS)


@pytest.fixture
def minion():
    cfg = GrubConfig()
    return ConcreteMinion(name="concrete", config=cfg, skills=[])


@pytest.fixture
def task():
    return GrubTask(title="Test", description="Do the thing")


# ═══════════════════════════════════════════════════════════════════════════════


class TestBuildSystemPrompt:
    def test_contains_base_prompt(self, minion):
        prompt = minion._build_system_prompt()
        assert "You are a test minion." in prompt

    def test_injects_skills(self):
        cfg = GrubConfig()
        m = ConcreteMinion(
            name="concrete", config=cfg, skills=["Skill A content", "Skill B content"]
        )
        prompt = m._build_system_prompt()
        assert "Skill A content" in prompt
        assert "Skill B content" in prompt

    def test_injects_extra_context(self, minion):
        prompt = minion._build_system_prompt(extra_context="Extra context here")
        assert "Extra context here" in prompt

    def test_skills_separated_by_divider(self):
        cfg = GrubConfig()
        m = ConcreteMinion(name="c", config=cfg, skills=["Skill X"])
        prompt = m._build_system_prompt()
        assert "---" in prompt  # separator between sections


class TestExtractCodeBlocks:
    def test_extracts_python_block(self, minion):
        text = "Here's the code:\n```python\ndef hello(): pass\n```\nDone."
        blocks = minion._extract_code_blocks(text, "python")
        assert len(blocks) == 1
        assert "def hello()" in blocks[0]

    def test_extracts_multiple_blocks(self, minion):
        text = "```python\ncode1\n```\nsome text\n```python\ncode2\n```"
        blocks = minion._extract_code_blocks(text)
        assert len(blocks) == 2

    def test_returns_empty_when_no_blocks(self, minion):
        blocks = minion._extract_code_blocks("No code here at all.")
        assert blocks == []

    def test_strips_whitespace_from_blocks(self, minion):
        text = "```python\n\n  def foo(): pass\n\n```"
        blocks = minion._extract_code_blocks(text, "python")
        assert blocks[0] == "def foo(): pass"


class TestScoreFromText:
    def test_parses_decimal_score(self, minion):
        score = minion._score_from_text("The code is good. Score: 0.82")
        assert abs(score - 0.82) < 0.01

    def test_parses_out_of_ten_score(self, minion):
        score = minion._score_from_text("Overall rating: 8/10")
        assert abs(score - 0.8) < 0.01

    def test_parses_score_with_colon(self, minion):
        score = minion._score_from_text("Score: 7/10\nSome notes.")
        assert abs(score - 0.7) < 0.01

    def test_returns_half_when_no_score(self, minion):
        score = minion._score_from_text("No numeric score mentioned here.")
        assert score == 0.5

    def test_clamps_to_zero_to_one(self, minion):
        # Pathological input
        score = minion._score_from_text("Score: 150")
        assert 0.0 <= score <= 1.0


class TestMakeFailedResult:
    def test_returns_failed_status(self, minion, task):
        result = minion._make_failed_result(task, "Something went wrong")
        assert result.status == ResultStatus.FAILED
        assert result.score == 0.0

    def test_includes_reason_in_notes(self, minion, task):
        result = minion._make_failed_result(task, "Connection refused")
        assert "Connection refused" in result.notes

    def test_sets_task_id(self, minion, task):
        result = minion._make_failed_result(task, "error")
        assert result.task_id == task.id


class TestLlmMethod:
    @pytest.mark.asyncio
    async def test_returns_error_string_when_cannot_connect(self, minion, task):
        """When Ollama is not running, _llm() should return an ERROR: string."""
        with patch("httpx.AsyncClient") as mock_client:
            import httpx

            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                side_effect=httpx.ConnectError("connection refused")
            )
            response = await minion._llm("test prompt")
        assert response.startswith("ERROR:")

    @pytest.mark.asyncio
    async def test_returns_content_on_success(self, minion):
        """When Ollama responds correctly, _llm() returns the content string."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(
            return_value={"message": {"content": "Generated code here"}}
        )
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )
            response = await minion._llm("test prompt")
        assert response == "Generated code here"
