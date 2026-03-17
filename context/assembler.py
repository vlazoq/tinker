"""
context_assembler.py
====================
Tinker – Context Assembler (Component 6)

Translates Tinker's layered memory into a single, token-budgeted prompt
ready to be sent to the model on every reasoning loop iteration.

Public surface
--------------
    ContextAssembler.assemble(task, role, loop_level) -> AssembledContext
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from exceptions import ContextError, ConfigurationError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------

class AgentRole(str, Enum):
    ARCHITECT   = "architect"
    CRITIC      = "critic"
    RESEARCHER  = "researcher"
    SYNTHESIZER = "synthesizer"


@dataclass
class Task:
    id: str
    description: str
    goal: str
    constraints: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_text(self) -> str:
        lines = [
            f"Task ID : {self.id}",
            f"Goal    : {self.goal}",
            f"Details : {self.description}",
        ]
        if self.constraints:
            lines.append("Constraints:")
            lines.extend(f"  • {c}" for c in self.constraints)
        return "\n".join(lines)


@dataclass
class MemoryItem:
    """A single retrieved piece of memory (artifact or research note)."""
    id: str
    content: str
    score: float          # semantic similarity [0, 1]
    source: str           # "session" | "archive" | "critique"
    timestamp: float = field(default_factory=time.time)


@dataclass
class AssembledContext:
    """The finished product: a prompt string + assembly metadata."""
    prompt: str
    tokens_used: int
    tokens_budget: int
    sections_included: list[str]
    sections_dropped: list[str]
    assembly_time_ms: float
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Token Budget Manager
# ---------------------------------------------------------------------------

# Section priority order (highest → lowest).  Higher-priority sections
# consume their allocation first; lower-priority sections are truncated or
# dropped when the budget is exhausted.
SECTION_PRIORITY = [
    "system_identity",
    "task",
    "arch_state",
    "recent_artifacts",
    "prior_critique",
    "research_notes",
    "output_format",
]

# Default fractional allocation of the *total* token budget per section.
# Must sum to ≤ 1.0.  Remainder is kept as a safety margin.
DEFAULT_ALLOCATION: dict[str, float] = {
    "system_identity"  : 0.05,
    "task"             : 0.10,
    "arch_state"       : 0.25,
    "recent_artifacts" : 0.25,
    "prior_critique"   : 0.15,
    "research_notes"   : 0.15,
    "output_format"    : 0.05,
}


class TokenBudgetManager:
    """
    Allocates a fixed token budget across context sections and provides
    truncation helpers.

    Parameters
    ----------
    total_tokens : int
        Hard ceiling for the assembled prompt (e.g. 8192, 16384).
    chars_per_token : float
        Conservative characters-per-token estimate used for length checks.
        OpenAI / LLaMA tokenizers average ~3.8–4.2 chars/token; we default
        to 3.8 for safety.
    allocation_overrides : dict | None
        Per-section fraction overrides (must sum ≤ 1.0).
    """

    def __init__(
        self,
        total_tokens: int = 8192,
        chars_per_token: float = 3.8,
        allocation_overrides: dict[str, float] | None = None,
    ):
        self.total_tokens = total_tokens
        self.chars_per_token = chars_per_token
        self.allocation = {**DEFAULT_ALLOCATION, **(allocation_overrides or {})}
        self._validate_allocation()

    # ------------------------------------------------------------------
    def _validate_allocation(self) -> None:
        total = sum(self.allocation.values())
        if total > 1.0:
            raise ConfigurationError(
                f"Token allocations sum to {total:.3f} > 1.0. "
                "Reduce one or more section allocations.",
                context={"total": total, "allocation": dict(self.allocation)},
            )

    # ------------------------------------------------------------------
    def budget_for(self, section: str) -> int:
        """Return the integer token budget for a named section."""
        fraction = self.allocation.get(section, 0.0)
        return int(self.total_tokens * fraction)

    def chars_for(self, section: str) -> int:
        """Return the character budget for a named section."""
        return int(self.budget_for(section) * self.chars_per_token)

    def estimate_tokens(self, text: str) -> int:
        """Rough token count from character length."""
        return max(1, int(len(text) / self.chars_per_token))

    def truncate(self, text: str, section: str, suffix: str = " …[truncated]") -> str:
        """
        Hard-truncate *text* so it fits within the section's character budget.
        Truncation always happens on a word boundary where possible.
        """
        limit = self.chars_for(section)
        if len(text) <= limit:
            return text
        cut = limit - len(suffix)
        # Walk back to the last whitespace to avoid mid-word cuts
        boundary = text.rfind(" ", 0, cut)
        cut = boundary if boundary > cut // 2 else cut
        return text[:cut] + suffix

    def fits(self, text: str, section: str) -> bool:
        return len(text) <= self.chars_for(section)

    def remaining_tokens(self, used: int) -> int:
        return max(0, self.total_tokens - used)


# ---------------------------------------------------------------------------
# Abstract interface definitions (Protocol classes)
#
# These are NOT incomplete implementations — they are interface contracts
# (similar to Python ABCs or TypeScript interfaces).  Each method body
# raises NotImplementedError intentionally, so that subclasses which forget
# to implement a method fail loudly at call time instead of silently returning
# None.  The concrete implementations live in context/stubs.py (for tests)
# and in memory/manager.py + prompt_builder.py (for production).
# ---------------------------------------------------------------------------

class _MemoryManagerProtocol:
    """Protocol / stub for the real MemoryManager (Component 2)."""

    async def get_arch_state_summary(self) -> str:
        raise NotImplementedError

    async def semantic_search_session(
        self, query: str, top_k: int = 5
    ) -> list[MemoryItem]:
        raise NotImplementedError

    async def semantic_search_archive(
        self, query: str, top_k: int = 5
    ) -> list[MemoryItem]:
        raise NotImplementedError

    async def get_prior_critique(self, task_id: str) -> list[MemoryItem]:
        raise NotImplementedError


class _PromptBuilderProtocol:
    """Protocol / stub for the real PromptBuilder (Component 4)."""

    def build_system_identity(self, role: AgentRole) -> str:
        raise NotImplementedError

    def build_output_format(self, role: AgentRole, loop_level: int) -> str:
        raise NotImplementedError

    def render_template(self, template_name: str, **kwargs: Any) -> str:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Context Assembler
# ---------------------------------------------------------------------------

class ContextAssembler:
    """
    Assembles a complete, token-budgeted prompt for a single model call.

    Parameters
    ----------
    memory_manager : _MemoryManagerProtocol
        Provides async access to session memory and research archive.
    prompt_builder : _PromptBuilderProtocol
        Fills agent-role–specific prompt templates.
    budget_manager : TokenBudgetManager | None
        Defaults to TokenBudgetManager(total_tokens=8192).
    retrieval_top_k : int
        Number of memory items fetched per semantic search.
    retrieval_timeout : float
        Per-retrieval timeout in seconds. On timeout the section is skipped
        gracefully.
    """

    def __init__(
        self,
        memory_manager: _MemoryManagerProtocol,
        prompt_builder: _PromptBuilderProtocol,
        budget_manager: TokenBudgetManager | None = None,
        retrieval_top_k: int = 5,
        retrieval_timeout: float = 3.0,
    ):
        self.memory  = memory_manager
        self.builder = prompt_builder
        self.budget  = budget_manager or TokenBudgetManager()
        self.top_k   = retrieval_top_k
        self.timeout = retrieval_timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def assemble(
        self,
        task: Task,
        role: AgentRole,
        loop_level: int,
    ) -> AssembledContext:
        """
        Build a complete context prompt for the given task / role / loop.

        Returns an AssembledContext whose `.prompt` field is ready to send
        to the model.  Never raises — all failures are caught and reflected
        in `.warnings` and `.sections_dropped`.
        """
        t0 = time.perf_counter()
        warnings:         list[str] = []
        sections_included: list[str] = []
        sections_dropped:  list[str] = []

        # --- 1. Fetch all memory sections concurrently --------------------
        query = f"{task.goal} {task.description}"

        (
            arch_state,
            artifacts,
            research,
            critique,
        ) = await self._fetch_all(query, task.id, warnings)

        # --- 2. Build static sections (never fail) -----------------------
        system_identity = self.builder.build_system_identity(role)
        output_format   = self.builder.build_output_format(role, loop_level)
        task_text       = task.to_text()

        # --- 3. Assemble sections in priority order ----------------------
        sections: dict[str, str] = {
            "system_identity"  : system_identity,
            "task"             : task_text,
            "arch_state"       : arch_state or "",
            "recent_artifacts" : self._format_items(artifacts, "Recent Artifacts"),
            "prior_critique"   : self._format_items(critique,  "Prior Critique"),
            "research_notes"   : self._format_items(research,  "Research Notes"),
            "output_format"    : output_format,
        }

        prompt_parts: list[str] = []
        tokens_used = 0

        for section_name in SECTION_PRIORITY:
            raw = sections.get(section_name, "")
            if not raw.strip():
                sections_dropped.append(section_name)
                continue

            budget_tokens = self.budget.budget_for(section_name)
            remaining     = self.budget.remaining_tokens(tokens_used)
            effective_cap = min(budget_tokens, remaining)

            # Temporarily shrink the budget manager's allocation to the
            # effective cap so truncate() uses the right limit.
            effective_chars = int(effective_cap * self.budget.chars_per_token)

            if effective_chars <= 0:
                sections_dropped.append(section_name)
                warnings.append(
                    f"Section '{section_name}' dropped: token budget exhausted."
                )
                continue

            truncated = self._truncate_to_chars(raw, effective_chars)
            prompt_parts.append(self._wrap_section(section_name, truncated))
            tokens_used += self.budget.estimate_tokens(truncated)
            sections_included.append(section_name)

        # --- 4. Join into final prompt -----------------------------------
        prompt = "\n\n".join(prompt_parts)

        elapsed_ms = (time.perf_counter() - t0) * 1000
        return AssembledContext(
            prompt             = prompt,
            tokens_used        = tokens_used,
            tokens_budget      = self.budget.total_tokens,
            sections_included  = sections_included,
            sections_dropped   = sections_dropped,
            assembly_time_ms   = round(elapsed_ms, 2),
            warnings           = warnings,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _fetch_all(
        self,
        query: str,
        task_id: str,
        warnings: list[str],
    ) -> tuple[str, list[MemoryItem], list[MemoryItem], list[MemoryItem]]:
        """
        Fire all memory retrievals concurrently.  Each is individually
        guarded; failures return empty results and append to warnings.
        """
        arch_coro      = self._safe_fetch(
            self.memory.get_arch_state_summary(),
            "arch_state", warnings, default="",
        )
        artifacts_coro = self._safe_fetch(
            self.memory.semantic_search_session(query, self.top_k),
            "recent_artifacts", warnings, default=[],
        )
        research_coro  = self._safe_fetch(
            self.memory.semantic_search_archive(query, self.top_k),
            "research_notes", warnings, default=[],
        )
        critique_coro  = self._safe_fetch(
            self.memory.get_prior_critique(task_id),
            "prior_critique", warnings, default=[],
        )

        results = await asyncio.gather(
            arch_coro, artifacts_coro, research_coro, critique_coro,
        )
        return results  # type: ignore[return-value]

    async def _safe_fetch(
        self,
        coro,
        label: str,
        warnings: list[str],
        default: Any,
    ) -> Any:
        """Wrap a coroutine in a timeout + exception guard."""
        try:
            return await asyncio.wait_for(coro, timeout=self.timeout)
        except asyncio.TimeoutError:
            warnings.append(
                f"[{label}] retrieval timed out after {self.timeout}s — "
                "section will be empty."
            )
            return default
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"[{label}] retrieval failed: {exc}")
            return default

    # ------------------------------------------------------------------

    @staticmethod
    def _format_items(items: list[MemoryItem], header: str) -> str:
        if not items:
            return ""
        lines = [f"### {header}"]
        for item in items:
            score_str = f"(similarity: {item.score:.2f})"
            lines.append(f"— [{item.id}] {score_str}\n{item.content.strip()}")
        return "\n\n".join(lines)

    @staticmethod
    def _truncate_to_chars(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        suffix = " …[truncated]"
        cut = limit - len(suffix)
        boundary = text.rfind(" ", 0, cut)
        cut = boundary if boundary > cut // 2 else cut
        return text[:cut] + suffix

    @staticmethod
    def _wrap_section(name: str, content: str) -> str:
        divider = "=" * 60
        title   = name.replace("_", " ").upper()
        return f"{divider}\n{title}\n{divider}\n{content}"

    # ------------------------------------------------------------------
    # Orchestrator-facing adapter
    # ------------------------------------------------------------------

    async def build(self, task: dict, max_artifacts: int = 10) -> dict:
        """
        Simplified adapter used by the Orchestrator's micro loop.

        Converts the plain dict *task* into a context dict the Architect
        agent can use.  Delegates to `assemble()` internally for full
        token-budgeted assembly, or falls back to a lightweight dict if
        `assemble()` fails (e.g. memory backend not yet connected).

        Returns a dict:  {"task": task, "prompt": str, "prior_artifacts": [...], ...}
        """
        # Build an internal Task object from the dict
        internal_task = Task(
            id=task.get("id", "unknown"),
            description=task.get("description", ""),
            goal=task.get("title", task.get("description", "architecture design task")),
            constraints=task.get("constraints", []),
            metadata={k: v for k, v in task.items()
                      if k not in ("id", "description", "title", "constraints")},
        )

        try:
            assembled = await self.assemble(
                task=internal_task,
                role=AgentRole.ARCHITECT,
                loop_level=0,
            )
            return {
                "task": task,
                "prompt": assembled.prompt,
                "tokens_used": assembled.tokens_used,
                "sections_included": assembled.sections_included,
                "sections_dropped": assembled.sections_dropped,
                "warnings": assembled.warnings,
                "prior_artifacts": [],
                "max_artifacts_requested": max_artifacts,
            }
        except Exception as exc:
            # Graceful fallback: return a minimal context dict so the
            # orchestrator can continue even if context assembly fails.
            return {
                "task": task,
                "prompt": internal_task.to_text(),
                "prior_artifacts": [],
                "max_artifacts_requested": max_artifacts,
                "context_assembly_error": str(exc),
            }
