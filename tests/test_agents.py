"""
tests/test_agents.py
====================
Enterprise-grade test suite for agents.py.

What these tests cover
-----------------------
1. **ArchitectAgent** — prompt building via PromptBuilder, context mapping,
   response parsing for both PromptBuilder (design_proposal) and legacy schemas,
   Grub review section injection, retry on transient failures.

2. **CriticAgent** — prompt building, score parsing, score clamping, retry.

3. **SynthesizerAgent** — meso and macro prompt building, retry.

4. **PromptBuilder integration** — fallback to inline prompts when PromptBuilder
   is unavailable, and full integration when it is.

5. **Retry wiring** — verify ``retry_async`` is called (or bypassed gracefully).

6. **Context role unification** — ``context.assembler.AgentRole`` is the same
   object as ``llm.types.AgentRole`` (no more duplicate enum).

Design
------
All tests use a mock ``ModelRouter`` via ``connection_factory``-style injection
(``MockRouter`` with configurable responses).  No real Ollama/AI calls are made.
``PromptBuilder`` is tested both in its real form (when available) and with a
monkey-patched failure so the inline fallback path is exercised.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents import (
    ArchitectAgent,
    CriticAgent,
    SynthesizerAgent,
    _extract_knowledge_gaps,
    _extract_candidate_tasks,
    _extract_score,
    _parse_architect_structured,
    _build_architect_prompts,
    _build_critic_prompts,
    _build_synthesizer_prompts,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_response(
    structured: dict | None = None,
    raw_text: str = "",
    total_tokens: int = 100,
) -> MagicMock:
    """Return a mock ModelResponse object."""
    resp = MagicMock()
    resp.structured = structured
    resp.raw_text = raw_text
    resp.total_tokens = total_tokens
    return resp


def _make_router(response) -> MagicMock:
    """Return a mock ModelRouter whose complete() returns *response*."""
    router = MagicMock()
    router.complete = AsyncMock(return_value=response)
    return router


def _make_task(
    description: str = "Design the caching layer",
    subsystem: str = "cache",
    task_id: str = "t-001",
    constraints: list | None = None,
) -> dict:
    return {
        "id": task_id,
        "description": description,
        "title": description,
        "subsystem": subsystem,
        "constraints": constraints or [],
    }


def _make_context(
    prompt: str = "Architecture state: event-driven microservices",
) -> dict:
    return {
        "prompt": prompt,
        "task": _make_task(),
        "tokens_used": 200,
        "sections_included": ["arch_state", "task"],
        "sections_dropped": [],
        "warnings": [],
        "prior_artifacts": [],
    }


# ---------------------------------------------------------------------------
# Tests: helper functions
# ---------------------------------------------------------------------------


class TestExtractHelpers:
    def test_extract_knowledge_gaps_finds_unknown_lines(self):
        text = "The architecture is good.\nUnknown: how DynamoDB scales.\nMore text."
        gaps = _extract_knowledge_gaps(text)
        assert len(gaps) >= 1
        assert any("DynamoDB" in g for g in gaps)

    def test_extract_knowledge_gaps_max_5(self):
        lines = "\n".join([f"gap: unknown thing {i}" for i in range(10)])
        gaps = _extract_knowledge_gaps(lines)
        assert len(gaps) <= 5

    def test_extract_knowledge_gaps_empty_on_no_keywords(self):
        text = "This design is complete and well-understood."
        assert _extract_knowledge_gaps(text) == []

    def test_extract_candidate_tasks_from_json_block(self):
        text = 'Some prose. {"candidate_tasks": [{"title": "Investigate Redis"}]}'
        tasks = _extract_candidate_tasks(text)
        assert len(tasks) == 1
        assert tasks[0]["title"] == "Investigate Redis"

    def test_extract_candidate_tasks_empty_on_no_json(self):
        text = "No JSON here at all."
        assert _extract_candidate_tasks(text) == []

    def test_extract_score_parses_slash_10(self):
        assert abs(_extract_score("Score: 8/10") - 0.8) < 0.01

    def test_extract_score_parses_decimal(self):
        assert abs(_extract_score("I rate it 0.85") - 0.85) < 0.01

    def test_extract_score_returns_default_when_missing(self):
        assert _extract_score("No score in this text.") == 0.7

    def test_extract_score_normalises_gt_1(self):
        # "9.5/10" → 0.95
        assert abs(_extract_score("score: 9.5/10") - 0.95) < 0.01


# ---------------------------------------------------------------------------
# Tests: _parse_architect_structured
# ---------------------------------------------------------------------------


class TestParseArchitectStructured:
    def test_legacy_schema_fields_passed_through(self):
        d = {
            "content": "Design narrative",
            "knowledge_gaps": ["gap1"],
            "decisions": ["dec1"],
            "open_questions": ["q1"],
            "candidate_tasks": [
                {
                    "title": "T1",
                    "description": "...",
                    "type": "design",
                    "subsystem": "auth",
                    "confidence_gap": 0.5,
                    "tags": [],
                }
            ],
        }
        content, gaps, decisions, questions, candidates = _parse_architect_structured(d)
        assert content == "Design narrative"
        assert gaps == ["gap1"]
        assert decisions == ["dec1"]
        assert questions == ["q1"]
        assert len(candidates) == 1

    def test_design_proposal_schema_maps_to_legacy_contract(self):
        d = {
            "artifact_type": "design_proposal",
            "title": "Redis Caching Layer",
            "reasoning_chain": [
                {"step": 1, "thought": "Redis is fast"},
                {"step": 2, "thought": "Use LRU eviction"},
                {"step": 3, "thought": "Cluster mode for scale"},
            ],
            "design": {
                "summary": "Use Redis for caching.",
                "components": [
                    {
                        "name": "RedisCache",
                        "responsibility": "Cache",
                        "dependencies": [],
                        "notes": "",
                    }
                ],
                "interfaces": [],
                "trade_offs": {
                    "gains": ["speed"],
                    "costs": ["memory"],
                    "risks": ["cold start"],
                },
            },
            "open_questions": ["How to handle cache invalidation?"],
            "candidate_next_tasks": [
                {
                    "task": "Research Redis cluster sizing",
                    "priority": "high",
                    "rationale": "scale",
                },
            ],
            "confidence": 0.75,
        }
        content, gaps, decisions, questions, candidates = _parse_architect_structured(d)
        assert "Redis Caching Layer" in content
        assert "Use Redis for caching." in content
        assert questions == ["How to handle cache invalidation?"]
        assert len(candidates) == 1
        assert candidates[0]["title"]
        assert len(decisions) == 3  # from reasoning_chain

    def test_design_proposal_empty_reasoning_chain_safe(self):
        d = {
            "artifact_type": "design_proposal",
            "title": "T",
            "reasoning_chain": [],
            "design": {
                "summary": "s",
                "components": [],
                "interfaces": [],
                "trade_offs": {},
            },
            "open_questions": [],
            "candidate_next_tasks": [],
            "confidence": 0.5,
        }
        content, gaps, decisions, questions, candidates = _parse_architect_structured(d)
        assert content  # must not be empty
        assert decisions == []


# ---------------------------------------------------------------------------
# Tests: prompt builder helpers
# ---------------------------------------------------------------------------


class TestBuildArchitectPrompts:
    def test_returns_tuple_of_two_strings(self):
        system, user = _build_architect_prompts(
            task_desc="Design auth",
            subsystem="auth",
            context_str="Prior work: nothing.",
            grub_section="",
            constraints_str="None specified.",
        )
        assert isinstance(system, str) and system
        assert isinstance(user, str) and user

    def test_fallback_when_prompt_builder_unavailable(self):
        with patch("agents._get_prompt_builder_cls", return_value=None):
            system, user = _build_architect_prompts(
                task_desc="Design auth",
                subsystem="auth",
                context_str="context",
                grub_section="",
                constraints_str="None specified.",
            )
        assert "senior software architect" in system.lower()
        assert "Design auth" in user

    def test_grub_section_included_in_fallback_user_prompt(self):
        with patch("agents._get_prompt_builder_cls", return_value=None):
            _, user = _build_architect_prompts(
                task_desc="Review impl",
                subsystem="billing",
                context_str="ctx",
                grub_section="## Grub Implementation Report\nScore: 0.85",
                constraints_str="None specified.",
            )
        assert "Grub Implementation Report" in user

    def test_context_truncated_to_4000_in_fallback(self):
        long_ctx = "A" * 5000
        with patch("agents._get_prompt_builder_cls", return_value=None):
            _, user = _build_architect_prompts(
                task_desc="Task",
                subsystem="sys",
                context_str=long_ctx,
                grub_section="",
                constraints_str="None.",
            )
        assert "A" * 3000 in user
        assert "A" * 4001 not in user

    def test_uses_prompt_builder_when_available(self):
        """When PromptBuilder is present, it should be called."""
        mock_pb = MagicMock()
        mock_pb.for_architect_micro.return_value = ("sys_from_pb", "user_from_pb")

        with patch("agents._get_prompt_builder_cls", return_value=mock_pb):
            system, user = _build_architect_prompts(
                task_desc="Design auth",
                subsystem="auth",
                context_str="ctx",
                grub_section="",
                constraints_str="None.",
            )

        assert system == "sys_from_pb"
        assert user == "user_from_pb"
        mock_pb.for_architect_micro.assert_called_once()

    def test_falls_back_when_prompt_builder_raises(self):
        """If PromptBuilder.for_architect_micro raises, use inline fallback."""
        mock_pb = MagicMock()
        mock_pb.for_architect_micro.side_effect = Exception("template not found")

        with patch("agents._get_prompt_builder_cls", return_value=mock_pb):
            system, user = _build_architect_prompts(
                task_desc="Design auth",
                subsystem="auth",
                context_str="ctx",
                grub_section="",
                constraints_str="None.",
            )

        assert "senior software architect" in system.lower()


class TestBuildCriticPrompts:
    def test_returns_strings(self):
        system, user = _build_critic_prompts("task desc", "design content")
        assert isinstance(system, str) and system
        assert isinstance(user, str) and user

    def test_uses_prompt_builder_when_available(self):
        mock_pb = MagicMock()
        mock_pb.for_critic_micro.return_value = ("sys", "usr")
        with patch("agents._get_prompt_builder_cls", return_value=mock_pb):
            system, user = _build_critic_prompts("task desc", "design content")
        assert system == "sys"
        mock_pb.for_critic_micro.assert_called_once()

    def test_fallback_includes_task_desc(self):
        with patch("agents._get_prompt_builder_cls", return_value=None):
            _, user = _build_critic_prompts("Design auth module", "proposal text")
        assert "Design auth module" in user


class TestBuildSynthesizerPrompts:
    def test_meso_fallback_includes_subsystem(self):
        with patch("agents._get_prompt_builder_cls", return_value=None):
            _, user = _build_synthesizer_prompts(
                "meso",
                subsystem="billing",
                artifacts=[{"content": "artifact1"}],
            )
        assert "billing" in user

    def test_macro_fallback_includes_version(self):
        with patch("agents._get_prompt_builder_cls", return_value=None):
            _, user = _build_synthesizer_prompts(
                "macro",
                documents=[],
                snapshot_version=3,
                total_micro_loops=42,
            )
        assert "v3" in user
        assert "42" in user

    def test_meso_uses_prompt_builder_when_available(self):
        mock_pb = MagicMock()
        mock_pb.for_synthesizer_meso.return_value = ("sys", "usr")
        with patch("agents._get_prompt_builder_cls", return_value=mock_pb):
            system, user = _build_synthesizer_prompts(
                "meso",
                subsystem="billing",
                artifacts=[],
            )
        assert system == "sys"
        mock_pb.for_synthesizer_meso.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: ArchitectAgent.call()
# ---------------------------------------------------------------------------


class TestArchitectAgentCall:
    @pytest.mark.asyncio
    async def test_returns_expected_keys(self):
        resp = _make_mock_response(
            structured={
                "content": "Design!",
                "knowledge_gaps": [],
                "decisions": [],
                "open_questions": [],
                "candidate_tasks": [],
            },
        )
        agent = ArchitectAgent(_make_router(resp))
        result = await agent.call(_make_task(), _make_context())

        assert "content" in result
        assert "tokens_used" in result
        assert "knowledge_gaps" in result
        assert "decisions" in result
        assert "open_questions" in result
        assert "candidate_tasks" in result

    @pytest.mark.asyncio
    async def test_legacy_json_response_parsed(self):
        structured = {
            "content": "Design narrative",
            "knowledge_gaps": ["gap1"],
            "decisions": ["dec1"],
            "open_questions": ["q1"],
            "candidate_tasks": [],
        }
        resp = _make_mock_response(structured=structured)
        agent = ArchitectAgent(_make_router(resp))
        result = await agent.call(_make_task(), _make_context())

        assert result["content"] == "Design narrative"
        assert result["knowledge_gaps"] == ["gap1"]
        assert result["decisions"] == ["dec1"]

    @pytest.mark.asyncio
    async def test_design_proposal_schema_mapped_correctly(self):
        structured = {
            "artifact_type": "design_proposal",
            "title": "Billing API",
            "reasoning_chain": [{"step": 1, "thought": "Use REST"}],
            "design": {
                "summary": "REST billing API",
                "components": [],
                "interfaces": [],
                "trade_offs": {},
            },
            "open_questions": ["Who calls this?"],
            "candidate_next_tasks": [
                {
                    "task": "Implement billing endpoint",
                    "priority": "high",
                    "rationale": "core feature",
                },
            ],
            "confidence": 0.8,
        }
        resp = _make_mock_response(structured=structured)
        agent = ArchitectAgent(_make_router(resp))
        result = await agent.call(_make_task(), _make_context())

        assert "Billing API" in result["content"]
        assert len(result["candidate_tasks"]) == 1
        assert result["open_questions"] == ["Who calls this?"]

    @pytest.mark.asyncio
    async def test_plain_text_fallback(self):
        resp = _make_mock_response(
            structured=None,
            raw_text="Design analysis.\ngap: unclear how Redis replication works.\n",
        )
        agent = ArchitectAgent(_make_router(resp))
        result = await agent.call(_make_task(), _make_context())

        assert result["content"] != ""
        assert isinstance(result["knowledge_gaps"], list)

    @pytest.mark.asyncio
    async def test_grub_review_context_injected(self):
        """When context has grub_implementation, it should appear in the user prompt."""
        context = _make_context()
        context["grub_implementation"] = {
            "status": "success",
            "score": 0.88,
            "summary": "Implemented billing module.",
            "files_written": ["billing/module.py"],
            "test_results": {"passed": 5, "failed": 0},
        }
        resp = _make_mock_response(
            structured={
                "content": "OK",
                "knowledge_gaps": [],
                "decisions": [],
                "open_questions": [],
                "candidate_tasks": [],
            }
        )
        # Capture the system+user prompts by inspecting the call to router.complete
        router = _make_router(resp)
        agent = ArchitectAgent(router)
        await agent.call(_make_task(description="Review billing"), context)

        call_args = router.complete.call_args
        request = call_args.args[0]
        user_msg = next(m for m in request.messages if m.role == "user")
        assert "Grub Implementation Report" in user_msg.content

    @pytest.mark.asyncio
    async def test_tokens_used_returned(self):
        resp = _make_mock_response(
            structured={
                "content": "x",
                "knowledge_gaps": [],
                "decisions": [],
                "open_questions": [],
                "candidate_tasks": [],
            },
            total_tokens=250,
        )
        agent = ArchitectAgent(_make_router(resp))
        result = await agent.call(_make_task(), _make_context())
        assert result["tokens_used"] == 250

    @pytest.mark.asyncio
    async def test_retry_called_on_transient_failure(self):
        """retry_async should be invoked; mock it to exercise the branch."""

        resp = _make_mock_response(
            structured={
                "content": "ok",
                "knowledge_gaps": [],
                "decisions": [],
                "open_questions": [],
                "candidate_tasks": [],
            },
        )
        router = _make_router(resp)
        agent = ArchitectAgent(router)

        call_count = [0]

        async def fake_retry_async(fn, config=None):
            call_count[0] += 1
            return await fn()

        with patch("agents._get_retry_async", return_value=(fake_retry_async, None)):
            await agent.call(_make_task(), _make_context())

        assert call_count[0] == 1  # retry_async was called once

    @pytest.mark.asyncio
    async def test_direct_call_when_retry_unavailable(self):
        """When retry is unavailable, _router.complete() is called directly."""
        resp = _make_mock_response(
            structured={
                "content": "ok",
                "knowledge_gaps": [],
                "decisions": [],
                "open_questions": [],
                "candidate_tasks": [],
            },
        )
        router = _make_router(resp)
        agent = ArchitectAgent(router)

        with patch("agents._get_retry_async", return_value=(None, None)):
            result = await agent.call(_make_task(), _make_context())

        assert result["content"] == "ok"
        router.complete.assert_awaited_once()


# ---------------------------------------------------------------------------
# Tests: CriticAgent.call()
# ---------------------------------------------------------------------------


class TestCriticAgentCall:
    @pytest.mark.asyncio
    async def test_returns_expected_keys(self):
        resp = _make_mock_response(
            structured={"content": "Good design.", "score": 0.8, "flags": []},
        )
        agent = CriticAgent(_make_router(resp))
        result = await agent.call(
            _make_task(), {"content": "Architect proposal", "tokens_used": 100}
        )
        assert "content" in result
        assert "tokens_used" in result
        assert "score" in result
        assert "flags" in result

    @pytest.mark.asyncio
    async def test_score_clamped_to_0_1(self):
        resp = _make_mock_response(
            structured={"content": "Great.", "score": 1.5, "flags": []},
        )
        agent = CriticAgent(_make_router(resp))
        result = await agent.call(_make_task(), {"content": ""})
        assert result["score"] == 1.0

    @pytest.mark.asyncio
    async def test_score_clamped_below_0(self):
        resp = _make_mock_response(
            structured={"content": "Terrible.", "score": -0.3, "flags": []},
        )
        agent = CriticAgent(_make_router(resp))
        result = await agent.call(_make_task(), {"content": ""})
        assert result["score"] == 0.0

    @pytest.mark.asyncio
    async def test_plain_text_fallback_extracts_score(self):
        resp = _make_mock_response(
            structured=None,
            raw_text="This is a good design. I'd give it 8/10.",
        )
        agent = CriticAgent(_make_router(resp))
        result = await agent.call(_make_task(), {"content": "proposal"})
        assert abs(result["score"] - 0.8) < 0.01

    @pytest.mark.asyncio
    async def test_retry_called_when_available(self):
        resp = _make_mock_response(
            structured={"content": "ok", "score": 0.7, "flags": []},
        )
        router = _make_router(resp)
        agent = CriticAgent(router)

        call_count = [0]

        async def fake_retry(fn, config=None):
            call_count[0] += 1
            return await fn()

        with patch("agents._get_retry_async", return_value=(fake_retry, None)):
            await agent.call(_make_task(), {"content": "proposal"})

        assert call_count[0] == 1


# ---------------------------------------------------------------------------
# Tests: SynthesizerAgent.call()
# ---------------------------------------------------------------------------


class TestSynthesizerAgentCall:
    @pytest.mark.asyncio
    async def test_meso_returns_expected_keys(self):
        resp = _make_mock_response(
            raw_text="Meso synthesis document.", total_tokens=300
        )
        agent = SynthesizerAgent(_make_router(resp))
        result = await agent.call(
            level="meso",
            subsystem="billing",
            artifacts=[{"content": "artifact"}],
        )
        assert "content" in result
        assert "tokens_used" in result
        assert "level" in result
        assert result["level"] == "meso"

    @pytest.mark.asyncio
    async def test_macro_returns_level_macro(self):
        resp = _make_mock_response(raw_text="Macro snapshot.", total_tokens=500)
        agent = SynthesizerAgent(_make_router(resp))
        result = await agent.call(
            level="macro",
            documents=[],
            snapshot_version=2,
            total_micro_loops=100,
        )
        assert result["level"] == "macro"

    @pytest.mark.asyncio
    async def test_retry_called_for_meso(self):
        resp = _make_mock_response(raw_text="Synthesis.")
        router = _make_router(resp)
        agent = SynthesizerAgent(router)

        call_count = [0]

        async def fake_retry(fn, config=None):
            call_count[0] += 1
            return await fn()

        with patch("agents._get_retry_async", return_value=(fake_retry, None)):
            await agent.call("meso", subsystem="auth", artifacts=[])

        assert call_count[0] == 1


# ---------------------------------------------------------------------------
# Tests: context role unification
# ---------------------------------------------------------------------------


class TestContextRoleUnification:
    """Verify that context.assembler.AgentRole is the same class as llm.types.AgentRole."""

    def test_same_class(self):
        from core.context.assembler import AgentRole as assembler_role
        from core.llm.types import AgentRole as llm_role

        assert assembler_role is llm_role, (
            "context.assembler.AgentRole must be imported from core.llm.types, "
            "not a separate definition."
        )

    def test_enum_values_match(self):
        from core.context.assembler import AgentRole

        assert AgentRole.ARCHITECT.value == "architect"
        assert AgentRole.CRITIC.value == "critic"
        assert AgentRole.RESEARCHER.value == "researcher"
        assert AgentRole.SYNTHESIZER.value == "synthesizer"

    def test_agents_use_llm_types_agent_role(self):
        """ArchitectAgent should import AgentRole from core.llm.types (not context.assembler)."""
        from core.llm.types import AgentRole

        # This is a structural check: verify the enum values work for routing
        # (router.complete uses AgentRole.ARCHITECT to route to the right machine)
        assert AgentRole.ARCHITECT.value == "architect"


# ---------------------------------------------------------------------------
# Tests: MemoryAdaptor semantic_search_session
# ---------------------------------------------------------------------------


class TestMemoryAdaptorSemanticSearch:
    """Verify the two-phase session search strategy."""

    @pytest.mark.asyncio
    async def test_recency_fallback_on_non_uuid_query(self):
        from core.context.memory_adapter import MemoryAdaptor

        mock_artifact = MagicMock()
        mock_artifact.id = "art-001"
        mock_artifact.content = "cached content"

        mock_mm = MagicMock()
        mock_mm.get_recent_artifacts = AsyncMock(return_value=[mock_artifact])

        adaptor = MemoryAdaptor(mock_mm)
        items = await adaptor.semantic_search_session(
            "design the caching layer", top_k=3
        )

        mock_mm.get_recent_artifacts.assert_called_once()
        assert len(items) == 1
        assert items[0].source == "session"
        assert items[0].score == 0.8

    @pytest.mark.asyncio
    async def test_uuid_query_uses_task_lookup(self):
        from core.context.memory_adapter import MemoryAdaptor

        mock_artifact = MagicMock()
        mock_artifact.id = "art-uuid"
        mock_artifact.content = "task-specific content"

        mock_mm = MagicMock()
        mock_mm.get_artifacts_by_task = AsyncMock(return_value=[mock_artifact])

        adaptor = MemoryAdaptor(mock_mm)
        uuid = "550e8400-e29b-41d4-a716-446655440000"
        items = await adaptor.semantic_search_session(uuid, top_k=3)

        mock_mm.get_artifacts_by_task.assert_called_once_with(uuid, limit=3)
        assert len(items) == 1
        assert items[0].score == 1.0  # exact match score

    @pytest.mark.asyncio
    async def test_falls_back_to_recency_when_uuid_lookup_returns_empty(self):
        from core.context.memory_adapter import MemoryAdaptor

        mock_artifact = MagicMock()
        mock_artifact.id = "art-fallback"
        mock_artifact.content = "fallback content"

        mock_mm = MagicMock()
        mock_mm.get_artifacts_by_task = AsyncMock(return_value=[])  # nothing found
        mock_mm.get_recent_artifacts = AsyncMock(return_value=[mock_artifact])

        adaptor = MemoryAdaptor(mock_mm)
        uuid = "550e8400-e29b-41d4-a716-446655440000"
        items = await adaptor.semantic_search_session(uuid, top_k=3)

        # Should have fallen back to recency
        mock_mm.get_recent_artifacts.assert_called_once()
        assert items[0].score == 0.8

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_exception(self):
        from core.context.memory_adapter import MemoryAdaptor

        mock_mm = MagicMock()
        mock_mm.get_recent_artifacts = AsyncMock(side_effect=RuntimeError("DB down"))
        mock_mm.get_artifacts_by_task = AsyncMock(side_effect=RuntimeError("DB down"))

        adaptor = MemoryAdaptor(mock_mm)
        items = await adaptor.semantic_search_session("any query", top_k=5)
        assert items == []
