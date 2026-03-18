"""
test_context_assembler.py
=========================
Tests for Tinker's Context Assembler (Component 6).

Run with:
    cd context_assembler
    python -m pytest test_context_assembler.py -v

or run standalone:
    python test_context_assembler.py
"""

from __future__ import annotations

import asyncio
import textwrap
import time
import unittest
from unittest.mock import AsyncMock, patch

from .assembler import (
    AgentRole,
    AssembledContext,
    ContextAssembler,
    MemoryItem,
    Task,
    TokenBudgetManager,
    SECTION_PRIORITY,
)
from .stubs import StubMemoryManager, StubPromptBuilder
from exceptions import ConfigurationError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_task(
    description: str = "Design the API Gateway component",
    goal: str = "Produce a resilient, observable API Gateway for the Tinker platform",
    constraints: list[str] | None = None,
) -> Task:
    return Task(
        id="task-001",
        description=description,
        goal=goal,
        constraints=constraints or [
            "Max p99 latency: 50 ms",
            "No single point of failure",
            "Must support gRPC + REST",
        ],
    )


def build_assembler(
    total_tokens: int = 8192,
    latency: float = 0.02,
) -> ContextAssembler:
    return ContextAssembler(
        memory_manager=StubMemoryManager(latency=latency),
        prompt_builder=StubPromptBuilder(),
        budget_manager=TokenBudgetManager(total_tokens=total_tokens),
        retrieval_top_k=3,
        retrieval_timeout=2.0,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestTokenBudgetManager(unittest.TestCase):

    def setUp(self):
        self.bm = TokenBudgetManager(total_tokens=4096)

    def test_budget_for_known_section(self):
        tokens = self.bm.budget_for("task")
        # "task" gets 10 % by default
        self.assertEqual(tokens, int(4096 * 0.10))

    def test_budget_for_unknown_section_is_zero(self):
        self.assertEqual(self.bm.budget_for("nonexistent"), 0)

    def test_chars_for_scales_with_tokens(self):
        chars = self.bm.chars_for("arch_state")
        expected = int(4096 * 0.25 * self.bm.chars_per_token)
        self.assertEqual(chars, expected)

    def test_estimate_tokens_round_trip(self):
        text = "Hello world " * 100   # 1200 chars
        tokens = self.bm.estimate_tokens(text)
        self.assertGreater(tokens, 0)
        self.assertLess(tokens, 1200)  # must be less than chars

    def test_truncate_short_text_unchanged(self):
        short = "Short text."
        self.assertEqual(self.bm.truncate(short, "task"), short)

    def test_truncate_long_text(self):
        long_text = "word " * 10_000
        result = self.bm.truncate(long_text, "task")
        self.assertLessEqual(len(result), self.bm.chars_for("task"))
        self.assertTrue(result.endswith("…[truncated]"))

    def test_fits(self):
        self.assertTrue(self.bm.fits("hi", "task"))
        self.assertFalse(self.bm.fits("x" * 100_000, "task"))

    def test_invalid_allocation_raises(self):
        # ConfigurationError (from the central exceptions module) is raised
        # when section allocations sum to > 1.0.
        with self.assertRaises(ConfigurationError):
            TokenBudgetManager(allocation_overrides={
                "system_identity": 0.9,
                "task": 0.9,  # sum > 1.0
            })

    def test_remaining_tokens(self):
        self.assertEqual(self.bm.remaining_tokens(1000), 3096)
        self.assertEqual(self.bm.remaining_tokens(5000), 0)  # clamped at 0


class TestContextAssembler(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.task      = make_task()
        self.assembler = build_assembler()

    # ------------------------------------------------------------------
    # Happy path
    # ------------------------------------------------------------------

    async def test_assemble_returns_assembled_context(self):
        ctx = await self.assembler.assemble(
            task=self.task,
            role=AgentRole.ARCHITECT,
            loop_level=3,
        )
        self.assertIsInstance(ctx, AssembledContext)

    async def test_prompt_is_non_empty(self):
        ctx = await self.assembler.assemble(self.task, AgentRole.ARCHITECT, 1)
        self.assertGreater(len(ctx.prompt), 100)

    async def test_all_core_sections_present(self):
        ctx = await self.assembler.assemble(self.task, AgentRole.ARCHITECT, 1)
        for section in ("SYSTEM IDENTITY", "TASK", "ARCH STATE",
                        "RECENT ARTIFACTS", "RESEARCH NOTES", "OUTPUT FORMAT"):
            self.assertIn(section, ctx.prompt, msg=f"Missing section: {section}")

    async def test_tokens_within_budget(self):
        ctx = await self.assembler.assemble(self.task, AgentRole.ARCHITECT, 1)
        self.assertLessEqual(ctx.tokens_used, ctx.tokens_budget)

    async def test_sections_included_list_populated(self):
        ctx = await self.assembler.assemble(self.task, AgentRole.CRITIC, 2)
        self.assertGreater(len(ctx.sections_included), 0)

    async def test_assembly_time_recorded(self):
        ctx = await self.assembler.assemble(self.task, AgentRole.RESEARCHER, 1)
        self.assertGreater(ctx.assembly_time_ms, 0)

    async def test_task_goal_in_prompt(self):
        ctx = await self.assembler.assemble(self.task, AgentRole.ARCHITECT, 1)
        self.assertIn(self.task.goal, ctx.prompt)

    async def test_task_constraints_in_prompt(self):
        ctx = await self.assembler.assemble(self.task, AgentRole.ARCHITECT, 1)
        for constraint in self.task.constraints:
            self.assertIn(constraint, ctx.prompt)

    async def test_all_agent_roles(self):
        """Assembler must work for every defined role."""
        for role in AgentRole:
            ctx = await self.assembler.assemble(self.task, role, loop_level=1)
            self.assertIsInstance(ctx, AssembledContext)
            self.assertGreater(len(ctx.prompt), 50)

    # ------------------------------------------------------------------
    # Graceful degradation
    # ------------------------------------------------------------------

    async def test_graceful_on_memory_timeout(self):
        """Even with a very short timeout all retrievals should time out
        gracefully and still produce a valid (reduced) context."""
        assembler = ContextAssembler(
            memory_manager=StubMemoryManager(latency=10.0),  # very slow
            prompt_builder=StubPromptBuilder(),
            budget_manager=TokenBudgetManager(),
            retrieval_timeout=0.01,  # will trigger timeout
        )
        ctx = await assembler.assemble(self.task, AgentRole.ARCHITECT, 1)
        self.assertIsInstance(ctx, AssembledContext)
        self.assertGreater(len(ctx.warnings), 0)
        # Even with empty memory, task + identity + format must be present
        self.assertIn("TASK", ctx.prompt)
        self.assertIn("SYSTEM IDENTITY", ctx.prompt)

    async def test_graceful_on_memory_exception(self):
        """Memory manager that throws should not crash the assembler."""

        class BrokenMemory(StubMemoryManager):
            async def semantic_search_session(self, query, top_k=5):
                raise RuntimeError("DB connection lost")

        assembler = ContextAssembler(
            memory_manager=BrokenMemory(),
            prompt_builder=StubPromptBuilder(),
            budget_manager=TokenBudgetManager(),
        )
        ctx = await assembler.assemble(self.task, AgentRole.ARCHITECT, 1)
        self.assertIsInstance(ctx, AssembledContext)
        self.assertTrue(any("recent_artifacts" in w for w in ctx.warnings))

    # ------------------------------------------------------------------
    # Token budget enforcement
    # ------------------------------------------------------------------

    async def test_tiny_budget_still_produces_prompt(self):
        """Even with a very tight budget (512 tokens) we must get a prompt."""
        assembler = ContextAssembler(
            memory_manager=StubMemoryManager(),
            prompt_builder=StubPromptBuilder(),
            budget_manager=TokenBudgetManager(total_tokens=512),
        )
        ctx = await assembler.assemble(self.task, AgentRole.ARCHITECT, 1)
        self.assertIsInstance(ctx, AssembledContext)
        self.assertGreater(len(ctx.prompt), 10)
        self.assertLessEqual(ctx.tokens_used, 512)

    async def test_dropped_sections_logged(self):
        """With a very small budget some sections should appear in dropped."""
        assembler = ContextAssembler(
            memory_manager=StubMemoryManager(),
            prompt_builder=StubPromptBuilder(),
            budget_manager=TokenBudgetManager(total_tokens=256),
        )
        ctx = await assembler.assemble(self.task, AgentRole.ARCHITECT, 1)
        # dropped + included should together cover all SECTION_PRIORITY
        all_sections = set(ctx.sections_included) | set(ctx.sections_dropped)
        # Every section that has content must be accounted for
        for s in SECTION_PRIORITY:
            self.assertIn(s, all_sections)


# ---------------------------------------------------------------------------
# Integration demo  (not a unit test — prints a full assembled context)
# ---------------------------------------------------------------------------

async def _demo():
    print("\n" + "=" * 70)
    print("TINKER CONTEXT ASSEMBLER — INTEGRATION DEMO")
    print("=" * 70)

    task = Task(
        id="task-arch-008",
        description=textwrap.dedent("""\
            Design a circuit-breaker strategy for the API Gateway.
            The gateway currently forwards all traffic to six downstream
            microservices.  We need automatic failure isolation so that a
            degraded service does not cascade into full system outage.
        """),
        goal=(
            "Add circuit-breaker + fallback mechanisms to the API Gateway "
            "without breaking the 50 ms p99 latency SLO."
        ),
        constraints=[
            "Must support per-route breaker configuration",
            "Fallback responses must be documented in the API contract",
            "No additional infrastructure dependencies",
            "Observable via existing Prometheus metrics",
        ],
    )

    assembler = build_assembler(total_tokens=8192, latency=0.05)

    start = time.perf_counter()
    ctx = await assembler.assemble(
        task=task,
        role=AgentRole.ARCHITECT,
        loop_level=7,
    )
    elapsed = (time.perf_counter() - start) * 1000

    print(ctx.prompt)
    print("\n" + "=" * 70)
    print("ASSEMBLY METADATA")
    print("=" * 70)
    print(f"  Tokens used   : {ctx.tokens_used} / {ctx.tokens_budget}")
    print(f"  Sections in   : {ctx.sections_included}")
    print(f"  Sections out  : {ctx.sections_dropped}")
    print(f"  Assembly time : {ctx.assembly_time_ms} ms  (wall: {elapsed:.1f} ms)")
    if ctx.warnings:
        print("  Warnings:")
        for w in ctx.warnings:
            print(f"    ⚠ {w}")
    else:
        print("  Warnings      : none")
    print("=" * 70)


if __name__ == "__main__":
    # Run demo first, then unit tests
    asyncio.run(_demo())
    unittest.main(verbosity=2)
