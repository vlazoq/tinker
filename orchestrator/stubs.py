"""
stubs.py — minimal faithful implementations of all component interfaces.

These are NOT mocks.  Each stub:
  - Has the same interface contract as the real component
  - Does just enough real work to exercise the orchestrator's logic
  - Produces realistic-shaped output so state transitions fire correctly

Use these to wire up a local Tinker instance quickly, or as the basis
for integration tests.
"""
from __future__ import annotations

import asyncio
import random
import time
import uuid
from typing import Any, Optional


# ── Task Engine ───────────────────────────────────────────────────────────────

class StubTaskEngine:
    """
    Simple FIFO queue.  Generates synthetic tasks and seeds the queue
    so the orchestrator always has work.
    """

    SUBSYSTEMS = ["api_gateway", "data_pipeline", "auth_service", "cache_layer", "messaging"]

    def __init__(self, initial_tasks: int = 20):
        self._queue: list[dict] = []
        self._completed: list[dict] = []
        for _ in range(initial_tasks):
            self._queue.append(self._make_task())

    def _make_task(self, parent_id: Optional[str] = None) -> dict:
        subsystem = random.choice(self.SUBSYSTEMS)
        return {
            "id": str(uuid.uuid4()),
            "subsystem": subsystem,
            "description": f"Design consideration for {subsystem}",
            "priority": random.randint(1, 5),
            "tags": [subsystem, "design"],
            "parent_id": parent_id,
            "created_at": time.time(),
        }

    async def select_task(self) -> Optional[dict]:
        if not self._queue:
            # Always have something to do
            self._queue.append(self._make_task())
        return self._queue.pop(0)

    async def complete_task(self, task_id: str, artifact_id: str) -> None:
        self._completed.append({"task_id": task_id, "artifact_id": artifact_id})

    async def generate_tasks(
        self, parent_task: dict, architect_result: dict, critic_result: dict
    ) -> list[dict]:
        # Spawn 1-3 follow-up tasks
        count = random.randint(1, 3)
        new_tasks = [self._make_task(parent_id=parent_task["id"]) for _ in range(count)]
        self._queue.extend(new_tasks)
        return new_tasks

    @property
    def queue_depth(self) -> int:
        return len(self._queue)


# ── Context Assembler ─────────────────────────────────────────────────────────

class StubContextAssembler:
    async def build(self, task: dict, max_artifacts: int = 10) -> dict:
        return {
            "task": task,
            "prior_artifacts": [],   # real impl fetches from memory
            "system_description": "Tinker architectural reasoning context",
            "max_artifacts_requested": max_artifacts,
        }


# ── Architect Agent ───────────────────────────────────────────────────────────

class StubArchitectAgent:
    """
    Simulates an LLM architect call.
    Occasionally flags a knowledge gap to exercise researcher routing.
    """

    async def call(self, task: dict, context: dict) -> dict:
        await asyncio.sleep(0.05)  # simulate LLM latency
        subsystem = task.get("subsystem", "unknown")
        has_gap = random.random() < 0.2  # 20% chance of knowledge gap
        return {
            "content": (
                f"## Architecture analysis for {subsystem}\n\n"
                f"Task: {task['description']}\n\n"
                "Proposed approach: event-driven microservice with async messaging.\n"
                "Key decisions: use CQRS pattern, separate read/write models.\n"
            ),
            "tokens_used": random.randint(300, 800),
            "knowledge_gaps": (
                [f"Best practices for {subsystem} observability"] if has_gap else []
            ),
            "decisions": ["CQRS", "event-driven"],
            "open_questions": ["How to handle eventual consistency?"],
        }


# ── Critic Agent ──────────────────────────────────────────────────────────────

class StubCriticAgent:
    async def call(self, task: dict, architect_result: dict) -> dict:
        await asyncio.sleep(0.03)
        score = round(random.uniform(0.6, 0.95), 2)
        return {
            "content": (
                f"Score: {score}\n"
                "Strengths: Clear separation of concerns, async-first design.\n"
                "Weaknesses: Missing failure-mode analysis.\n"
                "Recommendation: Add circuit-breaker pattern."
            ),
            "tokens_used": random.randint(100, 300),
            "score": score,
            "flags": [],
        }


# ── Synthesizer Agent ─────────────────────────────────────────────────────────

class StubSynthesizerAgent:
    async def call(self, level: str, **kwargs) -> dict:
        await asyncio.sleep(0.08)
        if level == "meso":
            subsystem = kwargs.get("subsystem", "unknown")
            artifact_count = len(kwargs.get("artifacts", []))
            content = (
                f"## Subsystem Design: {subsystem}\n\n"
                f"Synthesised from {artifact_count} artifact(s).\n\n"
                "Overall pattern: event-sourced CQRS with async messaging.\n"
                "Consensus decisions: separate read/write models, dead-letter queues.\n"
            )
        elif level == "macro":
            doc_count = len(kwargs.get("documents", []))
            version = kwargs.get("snapshot_version", 0)
            content = (
                f"# Architectural Snapshot v{version}\n\n"
                f"Built from {doc_count} subsystem document(s).\n\n"
                "## System-wide patterns\n"
                "- Event-driven everywhere\n"
                "- Strong domain boundaries\n"
                "- Observability first\n"
            )
        else:
            content = f"Synthesis for unknown level: {level}"

        return {
            "content": content,
            "tokens_used": random.randint(500, 1500),
            "level": level,
        }


# ── Memory Manager ────────────────────────────────────────────────────────────

class StubArtifact:
    """Minimal Artifact-like object returned by StubMemoryManager.store_artifact."""
    def __init__(self, artifact_id: str) -> None:
        self.id = artifact_id

    def __str__(self) -> str:
        return self.id


class StubMemoryManager:
    """In-process dict-based store.  Real impl uses a vector DB + metadata store."""

    def __init__(self):
        self._artifacts: dict[str, dict] = {}
        self._documents: dict[str, dict] = {}

    async def store_artifact(
        self,
        artifact: dict | None = None,
        content: str | None = None,
        artifact_type=None,
        task_id: str | None = None,
        metadata: dict | None = None,
        **kwargs,
    ) -> "StubArtifact":
        """
        Accept both the old dict-based call and the new keyword-arg call that
        matches the real MemoryManager signature.
        Returns a stub object with an .id attribute so callers can do artifact.id.
        """
        artifact_id = str(uuid.uuid4())
        record: dict = {"id": artifact_id, "stored_at": time.time()}
        if artifact is not None:
            # Old dict-based call: store_artifact({"task_id": ..., "subsystem": ...})
            record.update(artifact)
        else:
            # New keyword-arg call: store_artifact(content=..., task_id=..., metadata=...)
            record["content"]  = content or ""
            record["task_id"]  = task_id
            record["metadata"] = metadata or {}
            if artifact_type is not None:
                record["artifact_type"] = getattr(artifact_type, "value", str(artifact_type))
        self._artifacts[artifact_id] = record
        return StubArtifact(artifact_id)

    async def get_artifacts(self, subsystem: str, limit: int = 10) -> list[dict]:
        matching = [
            a for a in self._artifacts.values()
            if a.get("subsystem") == subsystem
        ]
        return sorted(matching, key=lambda a: a["stored_at"], reverse=True)[:limit]

    async def store_document(self, document: dict) -> str:
        doc_id = str(uuid.uuid4())
        self._documents[doc_id] = {**document, "id": doc_id, "stored_at": time.time()}
        return doc_id

    async def get_all_documents(self) -> list[dict]:
        return list(self._documents.values())

    @property
    def artifact_count(self) -> int:
        return len(self._artifacts)

    @property
    def document_count(self) -> int:
        return len(self._documents)


# ── Tool Layer ────────────────────────────────────────────────────────────────

class StubToolLayer:
    async def research(self, query: str) -> dict:
        await asyncio.sleep(0.04)
        return {
            "query": query,
            "result": f"Research findings for '{query}': industry standard is X, alternatives Y and Z exist.",
            "sources": ["docs.example.com", "papers.example.com"],
        }


# ── Architecture State Manager ────────────────────────────────────────────────

class StubArchStateManager:
    def __init__(self):
        self._commits: list[dict] = []

    async def commit(self, payload: dict) -> str:
        commit_hash = uuid.uuid4().hex[:8]
        self._commits.append({
            **payload,
            "hash": commit_hash,
            "committed_at": time.time(),
        })
        return commit_hash

    @property
    def commit_count(self) -> int:
        return len(self._commits)


# ── Factory ───────────────────────────────────────────────────────────────────

def build_stub_components() -> dict[str, Any]:
    """Return a dict of all stub components ready to inject into Orchestrator."""
    return {
        "task_engine": StubTaskEngine(initial_tasks=30),
        "context_assembler": StubContextAssembler(),
        "architect_agent": StubArchitectAgent(),
        "critic_agent": StubCriticAgent(),
        "synthesizer_agent": StubSynthesizerAgent(),
        "memory_manager": StubMemoryManager(),
        "tool_layer": StubToolLayer(),
        "arch_state_manager": StubArchStateManager(),
    }
