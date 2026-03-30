"""
tests/test_new_features.py
==========================
Tests for features added in recent sessions: ResearchEnhancer, ResearchTeam,
HumanJudge, AutoMemory, WebhookDispatcher, workflow viewer, example tools.

All tests use mocks — no real Ollama, SearXNG, or external services needed.
"""

from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_router_response(raw_text: str = "", total_tokens: int = 50):
    """Create a mock ModelResponse."""
    resp = MagicMock()
    resp.raw_text = raw_text
    resp.total_tokens = total_tokens
    return resp


def _mock_memory_search_results(score: float = 0.9, content: str = "cached research"):
    """Create mock memory search results."""
    return [{"score": score, "content": content, "snippet": content[:100]}]


# ---------------------------------------------------------------------------
# ResearchEnhancer Tests
# ---------------------------------------------------------------------------


class TestResearchEnhancer:
    def _make_enhancer(self, router=None, memory=None, **kwargs):
        from core.tools.research_enhancer import ResearchEnhancer

        return ResearchEnhancer(
            router=router,
            memory_manager=memory,
            **kwargs,
        )

    @pytest.mark.asyncio
    async def test_rewrite_query_success(self):
        """Mock router.complete_text to return rewritten queries, verify output."""
        router = MagicMock()
        resp = _mock_router_response(raw_text="optimized query one\noptimized query two")
        router.complete_text = AsyncMock(return_value=resp)

        enhancer = self._make_enhancer(router=router, query_rewrite=True)
        queries = await enhancer.rewrite_query("how does caching work")

        assert len(queries) == 2
        assert queries[0] == "optimized query one"
        assert queries[1] == "optimized query two"
        router.complete_text.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rewrite_query_fallback_router_none(self):
        """When router is None, returns [original_gap]."""
        enhancer = self._make_enhancer(router=None, query_rewrite=True)
        queries = await enhancer.rewrite_query("my gap")
        assert queries == ["my gap"]

    @pytest.mark.asyncio
    async def test_rewrite_query_fallback_router_fails(self):
        """When router raises, returns [original_gap]."""
        router = MagicMock()
        router.complete_text = AsyncMock(side_effect=RuntimeError("model down"))

        enhancer = self._make_enhancer(router=router, query_rewrite=True)
        queries = await enhancer.rewrite_query("my gap")
        assert queries == ["my gap"]

    @pytest.mark.asyncio
    async def test_rewrite_query_disabled(self):
        """When query_rewrite=False, returns [original_gap]."""
        router = MagicMock()
        enhancer = self._make_enhancer(router=router, query_rewrite=False)
        queries = await enhancer.rewrite_query("my gap")
        assert queries == ["my gap"]

    @pytest.mark.asyncio
    async def test_check_memory_hit(self):
        """Mock memory_manager.search returning high-score result, verify returns cached."""
        memory = MagicMock()
        # Return a coroutine for search
        results = _mock_memory_search_results(
            score=0.9,
            content="A" * 100,  # >50 chars to pass length check
        )
        memory.search = AsyncMock(return_value=results)

        enhancer = self._make_enhancer(memory=memory, memory_first=True)
        result = await enhancer.check_memory("caching strategies")

        assert result is not None
        assert result["from_memory"] is True
        assert result["memory_score"] == 0.9
        assert result["sources"] == ["memory-archive"]

    @pytest.mark.asyncio
    async def test_check_memory_miss(self):
        """Mock memory_manager.search returning low-score result, verify returns None."""
        memory = MagicMock()
        results = _mock_memory_search_results(score=0.3, content="A" * 100)
        memory.search = AsyncMock(return_value=results)

        enhancer = self._make_enhancer(memory=memory, memory_first=True, memory_min_score=0.7)
        result = await enhancer.check_memory("caching strategies")
        assert result is None

    @pytest.mark.asyncio
    async def test_check_memory_disabled(self):
        """When memory_first=False, returns None."""
        memory = MagicMock()
        memory.search = AsyncMock(return_value=_mock_memory_search_results())

        enhancer = self._make_enhancer(memory=memory, memory_first=False)
        result = await enhancer.check_memory("anything")
        assert result is None
        memory.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_summarize_content(self):
        """Mock router.complete_text, verify summarization."""
        router = MagicMock()
        resp = _mock_router_response(
            raw_text="A concise summary of the research content that is long enough to pass the 50 char check."
        )
        router.complete_text = AsyncMock(return_value=resp)

        enhancer = self._make_enhancer(router=router, summarize=True, summarize_threshold=100)
        content = "X" * 200  # exceeds threshold
        summary = await enhancer.summarize_content(content, "caching gap")

        assert "concise summary" in summary
        router.complete_text.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_summarize_below_threshold(self):
        """Content below threshold returns truncated original."""
        router = MagicMock()
        router.complete_text = AsyncMock()

        enhancer = self._make_enhancer(router=router, summarize=True, summarize_threshold=3000)
        content = "Short content"
        result = await enhancer.summarize_content(content, "gap")

        assert result == "Short content"
        router.complete_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_assess_and_refine_sufficient(self):
        """Mock router returning 'SUFFICIENT', verify returns None."""
        router = MagicMock()
        resp = _mock_router_response(raw_text="SUFFICIENT")
        router.complete_text = AsyncMock(return_value=resp)

        enhancer = self._make_enhancer(router=router, iterative_max_rounds=2)
        result_dict = {"result": "A" * 200}
        refined = await enhancer.assess_and_refine("gap", result_dict, round_num=0)
        assert refined is None

    @pytest.mark.asyncio
    async def test_assess_and_refine_needs_more(self):
        """Mock router returning 'REFINE: better query', verify returns refined query."""
        router = MagicMock()
        resp = _mock_router_response(raw_text="REFINE: better caching query")
        router.complete_text = AsyncMock(return_value=resp)

        enhancer = self._make_enhancer(router=router, iterative_max_rounds=2)
        result_dict = {"result": "A" * 200}
        refined = await enhancer.assess_and_refine("gap", result_dict, round_num=0)
        assert refined == "better caching query"

    @pytest.mark.asyncio
    async def test_enhanced_research_full_pipeline(self):
        """Mock everything, verify full pipeline runs memory->rewrite->search->summarize->assess."""
        router = MagicMock()
        # rewrite_query response
        rewrite_resp = _mock_router_response(raw_text="optimized query")
        # summarize response
        summarize_resp = _mock_router_response(
            raw_text="A summarized version of the content that is definitely longer than fifty characters for the check."
        )
        # assess response (sufficient on first check)
        assess_resp = _mock_router_response(raw_text="SUFFICIENT")

        router.complete_text = AsyncMock(side_effect=[rewrite_resp, summarize_resp, assess_resp])

        # Memory miss
        memory = MagicMock()
        memory.search = AsyncMock(return_value=[])

        enhancer = self._make_enhancer(
            router=router,
            memory=memory,
            query_rewrite=True,
            memory_first=True,
            summarize=True,
            summarize_threshold=50,
            iterative_max_rounds=2,
        )

        # Mock research function
        research_fn = AsyncMock(
            return_value={
                "result": "X" * 200,
                "sources": ["http://example.com"],
            }
        )

        result = await enhancer.enhanced_research("my gap", research_fn)

        assert result["from_memory"] is False
        assert result["query"] == "my gap"
        # memory was checked
        memory.search.assert_awaited_once()
        # research was called
        research_fn.assert_awaited()


# ---------------------------------------------------------------------------
# ResearchTeam Tests
# ---------------------------------------------------------------------------


class TestResearchTeam:
    def _make_team(self, tool_layer=None, **kwargs):
        from agents.research_team import ResearchTeam

        return ResearchTeam(tool_layer=tool_layer or MagicMock(), **kwargs)

    @pytest.mark.asyncio
    async def test_research_gaps_parallel(self):
        """Mock tool_layer.research, verify concurrent execution."""
        tool_layer = MagicMock()
        tool_layer.research = AsyncMock(
            return_value={
                "result": "Research content here",
                "sources": ["http://example.com"],
            }
        )

        team = self._make_team(tool_layer=tool_layer, max_concurrent=3)
        gaps = ["gap one", "gap two", "gap three"]
        results = await team.research_gaps(gaps, timeout=10.0)

        assert len(results) == 3
        assert tool_layer.research.await_count == 3

    @pytest.mark.asyncio
    async def test_research_gaps_dedup(self):
        """Duplicate gaps should be deduplicated."""
        tool_layer = MagicMock()
        tool_layer.research = AsyncMock(
            return_value={
                "result": "content",
                "sources": [],
            }
        )

        team = self._make_team(tool_layer=tool_layer)
        # "Gap One" and "gap one" normalize to the same thing
        gaps = ["Gap One", "gap one", "GAP ONE"]
        results = await team.research_gaps(gaps, timeout=10.0)

        # Only 1 unique gap after normalization
        assert tool_layer.research.await_count == 1
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_research_gaps_cache(self):
        """Second call with same gap returns cached result."""
        tool_layer = MagicMock()
        tool_layer.research = AsyncMock(
            return_value={
                "result": "cached content",
                "sources": [],
            }
        )

        team = self._make_team(tool_layer=tool_layer)

        # First call
        results1 = await team.research_gaps(["my gap"], timeout=10.0)
        assert len(results1) == 1
        assert tool_layer.research.await_count == 1

        # Second call with same gap — should use cache
        results2 = await team.research_gaps(["my gap"], timeout=10.0)
        assert len(results2) == 1
        # research should NOT have been called again
        assert tool_layer.research.await_count == 1

    @pytest.mark.asyncio
    async def test_research_gaps_empty(self):
        """Empty gaps list returns empty list."""
        team = self._make_team()
        results = await team.research_gaps([], timeout=10.0)
        assert results == []


# ---------------------------------------------------------------------------
# HumanJudge Tests
# ---------------------------------------------------------------------------


class TestHumanJudge:
    def _make_judge(self, judge_mode="llm", **config_overrides):
        from agents.human_judge import HumanJudge

        config = MagicMock()
        config.judge_mode = judge_mode
        config.hybrid_score_threshold = config_overrides.get("hybrid_score_threshold", 0.7)
        config.hybrid_review_interval = config_overrides.get("hybrid_review_interval", 5)
        config.human_judge_timeout = config_overrides.get("human_judge_timeout", 60)

        state = MagicMock()
        state.pending_reviews = {}
        state.human_review_requested = False

        return HumanJudge(config=config, state=state)

    def test_should_request_review_human_mode(self):
        """Always True in human mode."""
        judge = self._make_judge(judge_mode="human")
        assert judge.should_request_review() is True
        assert judge.should_request_review(llm_score=0.9, iteration=5) is True

    def test_should_request_review_llm_mode(self):
        """Always False in llm mode."""
        judge = self._make_judge(judge_mode="llm")
        assert judge.should_request_review() is False
        assert judge.should_request_review(llm_score=0.1, iteration=100) is False

    def test_should_request_review_hybrid_low_score(self):
        """Hybrid mode triggers when LLM score is below threshold."""
        judge = self._make_judge(judge_mode="hybrid", hybrid_score_threshold=0.7)
        assert judge.should_request_review(llm_score=0.5) is True
        assert judge.should_request_review(llm_score=0.8) is False

    def test_should_request_review_hybrid_interval(self):
        """Hybrid mode triggers at review interval."""
        judge = self._make_judge(judge_mode="hybrid", hybrid_review_interval=5)
        assert judge.should_request_review(llm_score=0.9, iteration=5) is True
        assert judge.should_request_review(llm_score=0.9, iteration=3) is False

    def test_resolve_sets_event(self):
        """Resolving sets the asyncio.Event."""
        judge = self._make_judge(judge_mode="human")

        # Simulate a pending review
        review_id = "test-123"
        judge._state.pending_reviews[review_id] = {
            "id": review_id,
            "status": "pending",
            "score": None,
            "feedback": None,
            "directive": None,
            "sticky": False,
        }
        event = asyncio.Event()
        judge._events[review_id] = event

        assert not event.is_set()

        response = {"score": 0.8, "feedback": "Good work"}
        result = judge.resolve(review_id, response)

        assert result is True
        assert event.is_set()
        assert judge._state.pending_reviews[review_id]["score"] == 0.8
        assert judge._state.pending_reviews[review_id]["status"] == "submitted"

    def test_resolve_unknown_review_id(self):
        """Resolving an unknown review_id returns False."""
        judge = self._make_judge()
        result = judge.resolve("nonexistent", {"score": 0.5})
        assert result is False

    def test_sticky_directives(self):
        """Adding and clearing sticky directives."""
        judge = self._make_judge()

        # Add directives
        judge.sticky_directives.append(
            {
                "directive": "Use Redis for caching",
                "added_at": 1000.0,
                "review_id": "r1",
            }
        )
        judge.sticky_directives.append(
            {
                "directive": "Avoid microservices",
                "added_at": 1001.0,
                "review_id": "r2",
            }
        )

        active = judge.get_active_directives()
        assert len(active) == 2
        assert "Use Redis for caching" in active[0]

        # Clear one
        assert judge.clear_sticky_directive(0) is True
        assert len(judge.sticky_directives) == 1
        assert judge.sticky_directives[0]["directive"] == "Avoid microservices"

        # Clear all
        count = judge.clear_all_sticky_directives()
        assert count == 1
        assert len(judge.sticky_directives) == 0

    def test_get_context_block_empty(self):
        """No directives returns empty string."""
        judge = self._make_judge()
        assert judge.get_context_block() == ""

    def test_get_context_block_with_directives(self):
        """Context block includes directives."""
        judge = self._make_judge()
        judge.sticky_directives.append(
            {
                "directive": "Focus on performance",
                "added_at": 1000.0,
                "review_id": "r1",
            }
        )
        block = judge.get_context_block()
        assert "HUMAN DIRECTIVES" in block
        assert "Focus on performance" in block


# ---------------------------------------------------------------------------
# AutoMemory Tests
# ---------------------------------------------------------------------------


class TestAutoMemory:
    def _make_auto_memory(self, tmpdir):
        from core.memory.auto_memory import AutoMemory

        return AutoMemory(
            memory_dir=str(tmpdir),
            high_score_threshold=0.85,
            low_score_threshold=0.4,
        )

    @pytest.mark.asyncio
    async def test_on_micro_completed_high_score(self, tmp_path):
        """High critic score creates memory entry."""
        am = self._make_auto_memory(tmp_path)

        event = MagicMock()
        event.payload = {
            "critic_score": 0.95,
            "subsystem": "caching",
            "iteration": 3,
            "architect_tokens": 100,
            "critic_tokens": 50,
        }

        await am._on_micro_completed(event)

        assert am._state.stats["high_score_count"] == 1
        assert am._state.stats["total_micro_loops_observed"] == 1
        assert len(am._state.entries) == 1
        assert am._state.entries[0]["category"] == "effective_pattern"
        assert "caching" in am._state.entries[0]["content"]

    @pytest.mark.asyncio
    async def test_on_micro_completed_low_score(self, tmp_path):
        """Low critic score creates pitfall entry."""
        am = self._make_auto_memory(tmp_path)

        event = MagicMock()
        event.payload = {
            "critic_score": 0.2,
            "subsystem": "auth",
            "iteration": 1,
        }

        await am._on_micro_completed(event)

        assert am._state.stats["low_score_count"] == 1
        assert len(am._state.entries) == 1
        assert am._state.entries[0]["category"] == "pitfall"
        assert "auth" in am._state.entries[0]["content"]

    @pytest.mark.asyncio
    async def test_on_micro_completed_mid_score(self, tmp_path):
        """Mid-range score does not create an entry."""
        am = self._make_auto_memory(tmp_path)

        event = MagicMock()
        event.payload = {
            "critic_score": 0.6,
            "subsystem": "general",
            "iteration": 1,
        }

        await am._on_micro_completed(event)

        assert am._state.stats["total_micro_loops_observed"] == 1
        assert am._state.stats["high_score_count"] == 0
        assert am._state.stats["low_score_count"] == 0
        assert len(am._state.entries) == 0

    def test_attach_subscribes_events(self, tmp_path):
        """Verify attach calls bus.subscribe_handler for expected events."""
        from core.events import EventType

        am = self._make_auto_memory(tmp_path)
        bus = MagicMock()
        am.attach(bus)

        # Check that subscribe_handler was called for all expected event types
        subscribed_events = {c[0][0] for c in bus.subscribe_handler.call_args_list}
        expected = {
            EventType.MICRO_LOOP_COMPLETED,
            EventType.MICRO_LOOP_FAILED,
            EventType.STAGNATION_DETECTED,
            EventType.MESO_LOOP_COMPLETED,
            EventType.MACRO_LOOP_COMPLETED,
            EventType.CRITIC_SCORED,
            EventType.REFINEMENT_ITERATION,
            EventType.HUMAN_REVIEW_SUBMITTED,
            EventType.SYSTEM_STOPPING,
        }
        assert expected == subscribed_events

    def test_get_lessons_filters_by_subsystem(self, tmp_path):
        """get_lessons filters by subsystem and includes general."""
        am = self._make_auto_memory(tmp_path)

        from dataclasses import asdict

        from core.memory.auto_memory import MemoryEntry

        am._state.entries = [
            asdict(
                MemoryEntry(
                    category="insight",
                    subsystem="caching",
                    content="cache tip",
                    source_event="test",
                )
            ),
            asdict(
                MemoryEntry(
                    category="insight", subsystem="auth", content="auth tip", source_event="test"
                )
            ),
            asdict(
                MemoryEntry(
                    category="insight",
                    subsystem="general",
                    content="general tip",
                    source_event="test",
                )
            ),
        ]

        lessons = am.get_lessons(subsystem="caching")
        contents = [lesson["content"] for lesson in lessons]
        assert "cache tip" in contents
        assert "general tip" in contents
        assert "auth tip" not in contents


# ---------------------------------------------------------------------------
# WebhookDispatcher Tests
# ---------------------------------------------------------------------------


class TestWebhookDispatcher:
    def _make_dispatcher(self, endpoints=None, **kwargs):
        from core.tools.webhook import WebhookDispatcher

        return WebhookDispatcher(endpoints=endpoints or [], **kwargs)

    def test_attach_subscribes_specific_events(self):
        """Subscribe to listed events."""
        from core.events import EventType
        from core.tools.webhook import WebhookDispatcher

        dispatcher = WebhookDispatcher(
            endpoints=[
                {
                    "url": "http://localhost:5678/hook",
                    "events": ["micro_loop_completed", "stagnation_detected"],
                },
            ]
        )

        bus = MagicMock()
        dispatcher.attach(bus)

        # Should have subscribed to 2 specific event types
        assert bus.subscribe_handler.call_count == 2
        subscribed = {c[0][0] for c in bus.subscribe_handler.call_args_list}
        assert EventType.MICRO_LOOP_COMPLETED in subscribed
        assert EventType.STAGNATION_DETECTED in subscribed

    def test_attach_subscribes_all_events(self):
        """Wildcard subscription."""
        from core.tools.webhook import WebhookDispatcher

        dispatcher = WebhookDispatcher(
            endpoints=[
                {"url": "http://localhost:5678/hook", "events": ["*"]},
            ]
        )

        bus = MagicMock()
        dispatcher.attach(bus)

        # Wildcard: subscribe_handler called once with None
        bus.subscribe_handler.assert_called_once_with(None, dispatcher._on_event)

    @pytest.mark.asyncio
    async def test_fire_sends_post(self):
        """Mock httpx, verify POST sent with correct payload."""
        import httpx as _httpx

        from core.events import Event, EventType
        from core.tools.webhook import WebhookDispatcher

        dispatcher = WebhookDispatcher(
            endpoints=[
                {"url": "http://localhost:5678/hook", "events": ["*"]},
            ]
        )

        event = Event(
            type=EventType.MICRO_LOOP_COMPLETED,
            payload={"score": 0.9},
            source="test",
        )
        endpoint = {"url": "http://localhost:5678/hook"}

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(return_value=mock_response)
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=False)

        with patch.object(_httpx, "AsyncClient", return_value=mock_client_instance):
            await dispatcher._fire(endpoint, event)

        mock_client_instance.post.assert_awaited_once()
        call_kwargs = mock_client_instance.post.call_args
        assert call_kwargs[1]["json"]["event_type"] == "micro_loop_completed"
        assert dispatcher._stats["sent"] == 1

    @pytest.mark.asyncio
    async def test_fire_handles_error(self):
        """Network error increments failed counter."""
        import httpx as _httpx

        from core.events import Event, EventType
        from core.tools.webhook import WebhookDispatcher

        dispatcher = WebhookDispatcher(
            endpoints=[
                {"url": "http://localhost:5678/hook", "events": ["*"]},
            ]
        )

        event = Event(
            type=EventType.MICRO_LOOP_COMPLETED,
            payload={},
            source="test",
        )
        endpoint = {"url": "http://localhost:5678/hook"}

        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(side_effect=ConnectionError("refused"))
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=False)

        with patch.object(_httpx, "AsyncClient", return_value=mock_client_instance):
            await dispatcher._fire(endpoint, event)

        assert dispatcher._stats["failed"] == 1
        assert dispatcher._stats["last_error"] is not None


# ---------------------------------------------------------------------------
# WebhookTool Tests
# ---------------------------------------------------------------------------


class TestWebhookTool:
    def test_webhook_tool_schema(self):
        """Verify schema name and parameters."""
        from core.tools.webhook import WebhookTool

        tool = WebhookTool()
        schema = tool.schema

        assert schema.name == "webhook"
        assert "url" in schema.parameters["properties"]
        assert "payload" in schema.parameters["properties"]
        assert "url" in schema.parameters["required"]
        assert "payload" in schema.parameters["required"]

    @pytest.mark.asyncio
    async def test_webhook_tool_execute(self):
        """Mock httpx, verify POST."""
        import httpx as _httpx

        from core.tools.webhook import WebhookTool

        tool = WebhookTool(timeout=5.0)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{"ok": true}'

        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(return_value=mock_response)
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=False)

        with patch.object(_httpx, "AsyncClient", return_value=mock_client_instance):
            result = await tool._execute(
                url="http://localhost:5678/hook",
                payload={"event": "test"},
            )

        assert result["success"] is True
        assert result["status_code"] == 200
        mock_client_instance.post.assert_awaited_once()


# ---------------------------------------------------------------------------
# Workflow Routes Tests
# ---------------------------------------------------------------------------


class TestWorkflowRoutes:
    def test_build_mermaid_empty_state(self):
        """Empty state returns valid mermaid."""
        from ui.web.routes.workflow import _build_mermaid

        mermaid = _build_mermaid({})
        assert mermaid.startswith("graph TD")
        assert "MICRO" in mermaid
        assert "MACRO" in mermaid
        assert "ARCHITECT" in mermaid
        # No style lines for active nodes with empty state
        assert "fill:#4CAF50" not in mermaid

    def test_build_mermaid_with_active_loop(self):
        """Active micro loop gets highlighted."""
        from ui.web.routes.workflow import _build_mermaid

        state = {
            "loops": {
                "current_level": "micro",
                "stagnation_events": 0,
            }
        }
        mermaid = _build_mermaid(state)
        assert "style MICRO fill:#4CAF50" in mermaid

    def test_build_mermaid_meso_active(self):
        """Active meso loop gets highlighted."""
        from ui.web.routes.workflow import _build_mermaid

        state = {"loops": {"current_level": "meso"}}
        mermaid = _build_mermaid(state)
        assert "style MESO fill:#4CAF50" in mermaid

    def test_build_mermaid_macro_active(self):
        """Active macro loop gets highlighted."""
        from ui.web.routes.workflow import _build_mermaid

        state = {"loops": {"current_level": "macro"}}
        mermaid = _build_mermaid(state)
        assert "style MACRO fill:#4CAF50" in mermaid

    def test_build_mermaid_stagnation_highlighted(self):
        """Stagnation events cause STAG node to be highlighted."""
        from ui.web.routes.workflow import _build_mermaid

        state = {"loops": {"current_level": "", "stagnation_events": 3}}
        mermaid = _build_mermaid(state)
        assert "style STAG fill:#FF9800" in mermaid


# ---------------------------------------------------------------------------
# Example Tools Tests
# ---------------------------------------------------------------------------


class TestFileReaderTool:
    @pytest.mark.asyncio
    async def test_file_reader_allowed(self, tmp_path):
        """Reading within allowed dir succeeds."""
        from core.tools.examples import FileReaderTool

        # Create a file in the allowed directory
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        tool = FileReaderTool(allowed_dirs=[str(tmp_path)])
        result = await tool._execute(path=str(test_file))

        assert result["content"] == "hello world"
        assert result["size_bytes"] > 0

    @pytest.mark.asyncio
    async def test_file_reader_blocked(self, tmp_path):
        """Reading outside allowed dir raises PermissionError."""
        from core.tools.examples import FileReaderTool

        allowed_dir = tmp_path / "allowed"
        allowed_dir.mkdir()
        outside_file = tmp_path / "secret.txt"
        outside_file.write_text("secrets")

        tool = FileReaderTool(allowed_dirs=[str(allowed_dir)])

        with pytest.raises(PermissionError):
            await tool._execute(path=str(outside_file))

    @pytest.mark.asyncio
    async def test_file_reader_not_found(self, tmp_path):
        """Reading a nonexistent file raises FileNotFoundError."""
        from core.tools.examples import FileReaderTool

        tool = FileReaderTool(allowed_dirs=[str(tmp_path)])

        with pytest.raises(FileNotFoundError):
            await tool._execute(path=str(tmp_path / "nonexistent.txt"))


class TestShellTool:
    @pytest.mark.asyncio
    async def test_shell_tool_whitelist_allowed(self):
        """Whitelisted commands execute."""
        from core.tools.examples import ShellTool

        tool = ShellTool(allowed_commands=["echo", "ls"])
        result = await tool._execute(command="echo hello")

        assert result["returncode"] == 0
        assert "hello" in result["stdout"]
        assert result["timed_out"] is False

    @pytest.mark.asyncio
    async def test_shell_tool_whitelist_blocked(self):
        """Non-whitelisted commands raise PermissionError."""
        from core.tools.examples import ShellTool

        tool = ShellTool(allowed_commands=["echo", "ls"])

        with pytest.raises(PermissionError, match="not in allowed list"):
            await tool._execute(command="rm -rf /")

    @pytest.mark.asyncio
    async def test_shell_tool_no_whitelist(self):
        """Without whitelist, any command runs."""
        from core.tools.examples import ShellTool

        tool = ShellTool(allowed_commands=None)
        result = await tool._execute(command="echo test")
        assert result["returncode"] == 0


class TestDatabaseQueryTool:
    @pytest.mark.asyncio
    async def test_database_query_select_only(self, tmp_path):
        """SELECT works, INSERT raises PermissionError."""
        from core.tools.examples import DatabaseQueryTool

        db_path = str(tmp_path / "test.db")
        # Create a test database
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO items (name) VALUES ('test_item')")
        conn.commit()
        conn.close()

        tool = DatabaseQueryTool(db_path=db_path)

        # SELECT should work
        result = await tool._execute(query="SELECT * FROM items")
        assert result["row_count"] == 1
        assert result["rows"][0]["name"] == "test_item"

    @pytest.mark.asyncio
    async def test_database_query_insert_blocked(self, tmp_path):
        """INSERT raises PermissionError."""
        from core.tools.examples import DatabaseQueryTool

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        conn.commit()
        conn.close()

        tool = DatabaseQueryTool(db_path=db_path)

        with pytest.raises(PermissionError, match="Only SELECT"):
            await tool._execute(query="INSERT INTO items (name) VALUES ('hack')")

    @pytest.mark.asyncio
    async def test_database_query_drop_blocked(self, tmp_path):
        """DROP raises PermissionError."""
        from core.tools.examples import DatabaseQueryTool

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        conn.commit()
        conn.close()

        tool = DatabaseQueryTool(db_path=db_path)

        with pytest.raises(PermissionError):
            await tool._execute(query="DROP TABLE items")
