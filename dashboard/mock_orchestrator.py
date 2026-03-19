"""
tinker/dashboard/mock_orchestrator.py
──────────────────────────────────────
Synthetic Orchestrator that pumps realistic fake state into the shared
queue so you can run and develop the dashboard without the real Tinker
engine.

Usage
─────
    # In a terminal:
    python -m tinker.dashboard.mock_orchestrator

Or import and call run_mock() from your own script.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from datetime import datetime, timedelta

from .log_handler import get_log_buffer, LogRecord
from .state import (
    ArchitectOutput,
    ArchitectureState,
    CriticOutput,
    LoopLevel,
    TaskInfo,
    TaskStatus,
    TaskType,
)
from .subscriber import publish_state

# ──────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────

SUBSYSTEMS = [
    "ModelClient",
    "MemoryManager",
    "ToolLayer",
    "AgentPrompts",
    "TaskEngine",
    "ContextAssembler",
    "Orchestrator",
    "ArchitectureStateManager",
    "AntiStagnationSystem",
]

TASK_TYPES = list(TaskType)

TASK_DESCS = {
    TaskType.DESIGN: "Design subsystem interface for {}",
    TaskType.CRITIQUE: "Critique proposed architecture of {}",
    TaskType.REFINE: "Refine dependency graph in {}",
    TaskType.RESEARCH: "Research patterns for {} scalability",
    TaskType.COMMIT: "Commit architecture snapshot v{}",
    TaskType.STAGNATION_BREAK: "Break stagnation in {} via perturbation",
}

ARCH_SUMMARIES = [
    "Event-driven microkernel with pluggable agent plugins. Tool layer abstracted behind adaptor interfaces.",
    "Layered architecture: Model ↔ Context ↔ Agent ↔ Memory. Orchestrator as thin coordinator.",
    "Hexagonal design: domain core isolated from I/O ports. Redis pub/sub for inter-component events.",
    "CQRS applied to task queue: separate write model (task creation) from read model (dashboard).",
    "Streaming context windows with LRU memory eviction. Agent prompts versioned in sqlite.",
]

OBJECTIONS = [
    "ContextAssembler may overflow token budget under concurrent meso loops.",
    "No circuit breaker on ModelClient — cascading failure risk if Ollama goes down.",
    "Memory eviction strategy (LRU) does not account for task dependency graphs.",
    "Anti-stagnation perturbation may conflict with ongoing COMMIT tasks.",
    "Tool layer lacks idempotency guarantees on retry.",
]

LOG_MESSAGES = [
    ("INFO", "Orchestrator tick {}"),
    ("DEBUG", "Context assembled: {} tokens"),
    ("INFO", "Task {} dispatched to agent"),
    ("SUCCESS", "Architecture committed: v{}"),
    ("WARNING", "Model latency spike: {} ms"),
    ("DEBUG", "Memory eviction: {} artifacts removed"),
    ("INFO", "Meso loop boundary crossed"),
    ("ERROR", "Tool call failed: timeout after {}s"),
    ("INFO", "Anti-stagnation monitor: score={}"),
    ("DEBUG", "Queue depth: {}"),
]


def _fake_task(status: TaskStatus = TaskStatus.ACTIVE) -> TaskInfo:
    ttype = random.choice(TASK_TYPES)
    sub = random.choice(SUBSYSTEMS)
    ver = f"{random.randint(0, 5)}.{random.randint(0, 9)}.{random.randint(0, 20)}"
    desc = TASK_DESCS[ttype].format(sub if ttype != TaskType.COMMIT else ver)
    now = datetime.utcnow()
    return TaskInfo(
        id=str(uuid.uuid4())[:12],
        type=ttype,
        subsystem=sub,
        description=desc,
        status=status,
        created_at=now - timedelta(seconds=random.randint(0, 60)),
        started_at=now - timedelta(seconds=random.randint(0, 30))
        if status in (TaskStatus.ACTIVE, TaskStatus.COMPLETE)
        else None,
        completed_at=now if status == TaskStatus.COMPLETE else None,
        result_summary="Generated 3 candidate interfaces with trade-off analysis."
        if status == TaskStatus.COMPLETE
        else None,
        full_content=(
            "## Interface Proposal A\n\n```python\nclass ModelClient:\n"
            "    async def complete(self, prompt: str) -> str: ...\n```\n\n"
            "Trade-offs: simple but not streaming-capable.\n\n"
            "## Interface Proposal B\n\nAsyncGenerator-based. Supports partial tokens."
        )
        if status == TaskStatus.COMPLETE
        else None,
    )


# ──────────────────────────────────────────
# Mock Orchestrator loop
# ──────────────────────────────────────────


async def run_mock(tick: float = 1.5) -> None:
    """
    Generates synthetic state patches and log lines on `tick` interval.
    Also injects fake log records directly into the log buffer.
    """
    print("[mock_orchestrator] Starting — publishing to shared queue")

    micro, meso, macro = 0, 0, 0
    arch_version_minor = 1
    stagnation_score = 0.0
    is_stagnant = False
    total_calls = 0
    recent_tasks: list[TaskInfo] = [_fake_task(TaskStatus.COMPLETE) for _ in range(3)]
    stag_events: list[dict] = []

    active_task = _fake_task(TaskStatus.ACTIVE)
    last_arch = ArchitectOutput(
        summary=random.choice(ARCH_SUMMARIES)[:100],
        full_content="Full architecture spec goes here...",
        timestamp=datetime.utcnow(),
        task_id=active_task.id,
    )
    last_critic = CriticOutput(
        score=random.uniform(5.0, 9.0),
        top_objection=random.choice(OBJECTIONS),
        full_content="Full critique reasoning...",
        timestamp=datetime.utcnow(),
        task_id=active_task.id,
    )
    arch_state = ArchitectureState(
        version=f"0.{arch_version_minor}.0",
        last_commit_time=datetime.utcnow() - timedelta(minutes=5),
        summary=random.choice(ARCH_SUMMARIES),
        full_content="# Architecture v0.1.0\n\n" + "\n\n".join(ARCH_SUMMARIES),
    )

    buf = get_log_buffer()
    loop_lvl = LoopLevel.MICRO

    while True:
        await asyncio.sleep(tick)

        # Advance counters
        micro += 1
        if micro % 10 == 0:
            meso += 1
            loop_lvl = LoopLevel.MESO
        else:
            loop_lvl = LoopLevel.MICRO
        if meso % 5 == 0 and micro % 10 == 0:
            macro += 1
            loop_lvl = LoopLevel.MACRO

        total_calls += random.randint(1, 4)
        avg_lat = random.gauss(400, 80)
        p99_lat = avg_lat + random.uniform(200, 800)
        err_rate = random.uniform(0, 0.03)

        # Occasionally rotate active task
        if random.random() < 0.2:
            recent_tasks.append(active_task)
            recent_tasks = recent_tasks[-10:]
            active_task = _fake_task(TaskStatus.ACTIVE)
            last_arch = ArchitectOutput(
                summary=random.choice(ARCH_SUMMARIES)[:100],
                full_content="Full architect output for task " + active_task.id,
                timestamp=datetime.utcnow(),
                task_id=active_task.id,
            )
            last_critic = CriticOutput(
                score=random.uniform(4.5, 9.5),
                top_objection=random.choice(OBJECTIONS),
                full_content="Full critique for task " + active_task.id,
                timestamp=datetime.utcnow(),
                task_id=active_task.id,
            )

        # Occasionally commit architecture
        if random.random() < 0.05:
            arch_version_minor += 1
            arch_state = ArchitectureState(
                version=f"0.{arch_version_minor}.0",
                last_commit_time=datetime.utcnow(),
                summary=random.choice(ARCH_SUMMARIES),
                full_content=f"# Architecture v0.{arch_version_minor}.0\n\n"
                + "\n\n".join(random.sample(ARCH_SUMMARIES, 3)),
            )

        # Stagnation drift
        stagnation_score = max(0.0, min(1.0, stagnation_score + random.gauss(0, 0.05)))
        is_stagnant = stagnation_score > 0.65
        if is_stagnant and random.random() < 0.3:
            stag_events.append(
                {
                    "timestamp": datetime.utcnow().isoformat(),
                    "description": f"Stagnation detected at score={stagnation_score:.2f}",
                    "action_taken": random.choice(
                        [
                            "Injected random design perturbation",
                            "Switched to macro loop exploration",
                            "Triggered forced research task",
                        ]
                    ),
                }
            )
            stag_events = stag_events[-5:]
            stagnation_score *= 0.6  # partial recovery

        # Queue stats
        n_pending = random.randint(2, 12)
        n_active = random.randint(1, 3)
        n_complete = random.randint(5, 30)
        n_failed = random.randint(0, 2)
        by_type = {t.value: random.randint(0, 5) for t in TaskType}

        # Build and publish patch
        patch = {
            "connected": True,
            "loop_level": loop_lvl.value,
            "micro_count": micro,
            "meso_count": meso,
            "macro_count": macro,
            "active_task": {
                "id": active_task.id,
                "type": active_task.type.value,
                "subsystem": active_task.subsystem,
                "description": active_task.description,
                "status": active_task.status.value,
                "created_at": active_task.created_at.isoformat(),
                "started_at": active_task.started_at.isoformat()
                if active_task.started_at
                else None,
                "full_content": active_task.full_content,
            },
            "last_architect": {
                "summary": last_arch.summary,
                "full_content": last_arch.full_content,
                "timestamp": last_arch.timestamp.isoformat(),
                "task_id": last_arch.task_id,
            },
            "last_critic": {
                "score": last_critic.score,
                "top_objection": last_critic.top_objection,
                "full_content": last_critic.full_content,
                "timestamp": last_critic.timestamp.isoformat(),
                "task_id": last_critic.task_id,
            },
            "queue_stats": {
                "total_depth": n_pending + n_active,
                "by_status": {
                    "pending": n_pending,
                    "active": n_active,
                    "complete": n_complete,
                    "failed": n_failed,
                },
                "by_type": by_type,
            },
            "recent_tasks": [
                {
                    "id": t.id,
                    "type": t.type.value,
                    "subsystem": t.subsystem,
                    "description": t.description,
                    "status": t.status.value,
                    "created_at": t.created_at.isoformat(),
                    "started_at": t.started_at.isoformat() if t.started_at else None,
                    "completed_at": t.completed_at.isoformat()
                    if t.completed_at
                    else None,
                    "result_summary": t.result_summary,
                }
                for t in recent_tasks[-5:]
            ],
            "arch_state": {
                "version": arch_state.version,
                "last_commit_time": arch_state.last_commit_time.isoformat()
                if arch_state.last_commit_time
                else None,
                "summary": arch_state.summary,
                "full_content": arch_state.full_content,
            },
            "stagnation": {
                "is_stagnant": is_stagnant,
                "stagnation_score": stagnation_score,
                "monitor_status": "stagnant" if is_stagnant else "nominal",
                "recent_events": stag_events,
            },
            "model_metrics": {
                "avg_latency_ms": avg_lat,
                "p99_latency_ms": p99_lat,
                "error_rate": err_rate,
                "total_calls": total_calls,
                "recent_errors": [],
            },
            "memory_stats": {
                "session_artifact_count": micro * 3,
                "research_archive_size": macro * 12 + meso * 2,
                "working_memory_tokens": random.randint(2000, 8000),
            },
        }
        publish_state(patch)

        # Inject synthetic log line
        level, tmpl = random.choice(LOG_MESSAGES)
        val = random.choice(
            [
                micro,
                random.randint(100, 9999),
                f"{random.uniform(0.1, 5.0):.1f}",
                active_task.id[:8],
            ]
        )
        buf.push(
            LogRecord(
                timestamp=datetime.utcnow(),
                level=level,
                message=tmpl.format(val),
                source=f"tinker.{random.choice(SUBSYSTEMS).lower()}:loop:{micro}",
            )
        )


# ──────────────────────────────────────────
# Standalone entry point
# ──────────────────────────────────────────

if __name__ == "__main__":
    asyncio.run(run_mock())
