"""
examples.py — Realistic example filled prompts for all key roles,
plus a complete Architect → Critic exchange showing the micro loop.

These serve as:
  1. Regression tests (validate that example outputs pass the validator)
  2. Few-shot examples that can be injected into prompts via PromptBuilder
  3. Documentation for integrators
"""

import json

# ---------------------------------------------------------------------------
# EXAMPLE: Full Architect Micro Exchange
# ---------------------------------------------------------------------------

EXAMPLE_ARCHITECT_MICRO_CONTEXT = {
    "architecture_state": """
Version: 0.1.0
Established components:
  - ModelClient: wraps Ollama HTTP API, manages request/response lifecycle
  - TaskEngine: maintains task queue, emits task events

Established patterns:
  - Async-first (asyncio throughout)
  - All inter-component communication via structured JSON messages
  - No shared mutable state between components

Current loop: Micro #3 — designing the Memory Manager
""",
    "task_description": """
Design the Memory Manager component for Tinker.

The Memory Manager must:
- Store and retrieve architectural artifacts (proposals, critiques, research notes, syntheses)
- Support efficient retrieval by: artifact_id, role, loop_level, and recency
- Provide a "compressed context" view for the Context Assembler — recent relevant artifacts
  summarized to fit within a configurable token budget
- Persist state to disk so a Tinker run can be resumed after restart
- Support concurrent reads (multiple agents reading simultaneously) 
  and serialized writes (one write at a time)
""",
    "constraints": """
- Must not require an external database service (embedded only — SQLite acceptable)
- Python 3.11+
- Read latency target: < 50ms for typical queries
- Write latency target: < 200ms (writes may be async)
- Token budget compression must be deterministic (same inputs → same output)
""",
    "context": """
Prior micro loop #2 established:
  - Context Assembler will call MemoryManager.get_compressed_context(token_budget=4096)
  - Artifacts are identified by UUID v4 artifact_id
  - All artifacts are JSON-serializable Python dicts
  
Research Note RN-001 established:
  - SQLite with WAL mode supports concurrent reads + serialized writes natively
  - FTS5 extension supports full-text search on JSON fields
""",
}

EXAMPLE_ARCHITECT_MICRO_OUTPUT = {
    "artifact_type": "design_proposal",
    "artifact_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "loop_level": "micro",
    "title": "Memory Manager: SQLite-backed artifact store with token-budget compression",
    "reasoning_chain": [
        {
            "step": 1,
            "thought": "The core tension here is between retrieval flexibility (query by role, loop_level, recency) and simplicity. A relational store (SQLite) gives us indexed queries without an external service. I'll use SQLite with WAL mode for concurrent read safety.",
        },
        {
            "step": 2,
            "thought": "The 'compressed context' operation is the most complex: given a token budget, select the most relevant recent artifacts and summarize them. Determinism requirement means I cannot use LLM summarization here — I must use a rule-based truncation strategy. I'll rank by recency + loop_level priority (macro > meso > micro) and truncate each artifact JSON to a character limit proportional to its budget share.",
        },
        {
            "step": 3,
            "thought": "Write serialization: SQLite handles this via its locking model. I'll use a single asyncio.Lock per MemoryManager instance as an additional application-level guard for the write path, ensuring no concurrent partial writes from within the same process.",
        },
        {
            "step": 4,
            "thought": "The API surface must be small. The Context Assembler needs: store_artifact(), get_artifact(id), query_artifacts(filters), get_compressed_context(token_budget). I'll keep the interface minimal to avoid coupling. The MemoryManager should not know anything about what the Context Assembler does with the data.",
        },
        {
            "step": 5,
            "thought": "Persistence and resumability: on startup, MemoryManager opens or creates the SQLite file. All state is in the file. No in-memory-only state. This gives us free resumability — restart Tinker, re-open the file, continue.",
        },
    ],
    "design": {
        "summary": "The Memory Manager wraps a local SQLite database (WAL mode) to store all Tinker artifacts. It exposes an async Python API for storing, retrieving, and querying artifacts by metadata. The compressed context operation is rule-based: artifacts are ranked by recency and loop_level priority, then truncated proportionally to fit within the caller's token budget.",
        "components": [
            {
                "name": "MemoryManager",
                "responsibility": "Primary interface. Owns the SQLite connection pool. Implements store_artifact(), get_artifact(), query_artifacts(), get_compressed_context(). Enforces write serialization via asyncio.Lock.",
                "dependencies": ["ArtifactStore", "ContextCompressor"],
                "notes": "Instantiated once per Tinker run. Path to SQLite file passed at construction. Async context manager protocol supported.",
            },
            {
                "name": "ArtifactStore",
                "responsibility": "Low-level SQLite operations. Manages schema creation, connection lifecycle (using aiosqlite), and CRUD for the artifacts table. Handles WAL mode configuration at startup.",
                "dependencies": [],
                "notes": "Schema: artifacts(id TEXT PK, artifact_type TEXT, role TEXT, loop_level TEXT, created_at REAL, artifact_json TEXT). FTS5 virtual table on artifact_json for search.",
            },
            {
                "name": "ContextCompressor",
                "responsibility": "Given a list of artifacts and a token budget (in characters, approximated as tokens×4), returns a compressed JSON-serializable structure. Ranking: macro > meso > micro; within level, most-recent-first. Truncation: each artifact gets a proportional character budget.",
                "dependencies": [],
                "notes": "Fully deterministic. No LLM calls. Token approximation: 1 token ≈ 4 chars (consistent with model tokenizers used in Tinker).",
            },
        ],
        "interfaces": [
            {
                "name": "IMemoryManager",
                "between": ["MemoryManager", "ContextAssembler"],
                "contract": "async store_artifact(artifact: dict) -> str [returns artifact_id]; async get_artifact(artifact_id: str) -> dict | None; async query_artifacts(role: str | None, loop_level: str | None, limit: int = 20) -> list[dict]; async get_compressed_context(token_budget: int) -> dict",
            },
            {
                "name": "IMemoryManager_TaskEngine",
                "between": ["MemoryManager", "TaskEngine"],
                "contract": "TaskEngine calls store_artifact() after each micro loop completes. No other coupling — TaskEngine does not read from MemoryManager directly.",
            },
        ],
        "trade_offs": {
            "gains": [
                "No external database dependency — zero ops overhead",
                "WAL mode provides concurrent read safety out of the box",
                "Deterministic compression is testable and reproducible",
                "SQLite file is the persistence layer — resumability is free",
            ],
            "costs": [
                "SQLite is not horizontally scalable — this is a single-process, single-node design",
                "Token budget compression via truncation loses semantic coherence; a partial artifact JSON may be uninterpretable",
                "asyncio.Lock on writes means write throughput is limited to single-writer at a time",
            ],
            "risks": [
                "SQLite file corruption on unclean shutdown (mitigated by WAL + checkpointing)",
                "ContextCompressor character-to-token ratio (4:1) may be inaccurate for non-ASCII content in multilingual research notes",
                "FTS5 search on JSON blobs may return false positives if field names match query terms",
            ],
        },
    },
    "open_questions": [
        "Should MemoryManager expose a watch/subscribe interface so the Orchestrator can react to new artifact writes without polling?",
        "What is the expected artifact volume per Tinker run? SQLite may need VACUUM scheduling if artifact count exceeds ~100k.",
        "Should ContextCompressor rank research notes separately from design artifacts, since they serve a different purpose in context?",
    ],
    "candidate_next_tasks": [
        {
            "task": "Implement ArtifactStore with aiosqlite, WAL mode, and FTS5 schema",
            "priority": "high",
            "rationale": "Blocking dependency for all other Memory Manager work and for integration testing with TaskEngine.",
        },
        {
            "task": "Implement ContextCompressor with unit tests covering edge cases (empty store, budget=0, single oversized artifact)",
            "priority": "high",
            "rationale": "ContextAssembler integration depends on this; determinism must be verified by tests before integration.",
        },
        {
            "task": "Define and document the IMemoryManager interface as a Python Protocol class",
            "priority": "medium",
            "rationale": "Enables mock implementations for testing other components that depend on MemoryManager.",
        },
        {
            "task": "Research: evaluate whether DuckDB would be a better fit than SQLite for this use case",
            "priority": "low",
            "rationale": "DuckDB offers columnar storage and richer analytics — may be superior if artifact query patterns evolve.",
        },
    ],
    "confidence": 0.74,
}


# ---------------------------------------------------------------------------
# EXAMPLE: Critic Micro Output (critiquing the above Architect output)
# ---------------------------------------------------------------------------

EXAMPLE_CRITIC_MICRO_OUTPUT = {
    "artifact_type": "critique",
    "artifact_id": "f9e8d7c6-b5a4-3210-fedc-ba9876543210",
    "loop_level": "micro",
    "target_artifact_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "confidence_score": 0.52,
    "weaknesses": [
        {
            "id": "W1",
            "severity": "high",
            "category": "correctness",
            "statement": "ContextCompressor's character-to-token approximation (4 chars = 1 token) is documented as a risk but treated as acceptable. For Qwen3-7B and Phi-3-mini — both of which use BPE tokenizers — CJK and code tokens can be 1-2 chars/token. A budget of 4096 tokens could overflow by 2-4x for mixed-content artifacts.",
            "evidence": "The design notes '1 token ≈ 4 chars (consistent with model tokenizers used in Tinker)' but provides no citation or measurement. The open question acknowledges non-ASCII risk but does not commit to a solution.",
            "impact": "Context Assembler will silently pass over-budget inputs to the model, causing truncation at the model level (lossy, unpredictable) or exceeding context window limits and crashing the inference call.",
        },
        {
            "id": "W2",
            "severity": "high",
            "category": "reliability",
            "statement": "There is no explicit artifact versioning or conflict detection strategy in ArtifactStore. The schema uses artifact_id as primary key, but does not track whether an artifact with the same ID was re-written (e.g. after auto-repair by the validator). A duplicate write will silently overwrite the original.",
            "evidence": "ArtifactStore schema is described as 'artifacts(id TEXT PK, ...)' with no updated_at, version, or write-conflict detection mentioned anywhere.",
            "impact": "If the Orchestrator retries a failed write after partial success, or if the validator repairs and re-stores an artifact with the same ID, the original is lost with no audit trail.",
        },
        {
            "id": "W3",
            "severity": "medium",
            "category": "observability",
            "statement": "MemoryManager exposes no metrics or health-check interface. There is no way for the Observability Dashboard to inspect store size, write latency, query counts, or compression efficiency without instrumenting MemoryManager internals.",
            "evidence": "The IMemoryManager interface (store_artifact, get_artifact, query_artifacts, get_compressed_context) contains no metrics methods. The design does not mention logging or metrics emission.",
            "impact": "Tinker runs for hours or days. Without observability into the Memory Manager, operators cannot detect growing store sizes, slow queries, or compression budget mis-calibrations until they cause failures.",
        },
        {
            "id": "W4",
            "severity": "medium",
            "category": "maintainability",
            "statement": "ContextCompressor's ranking rule (macro > meso > micro, then recency) is hardcoded in the design document but not expressed as a configurable policy. As Tinker evolves, different synthesis strategies may require different context weighting — this will require code changes rather than configuration changes.",
            "evidence": "Design states 'Ranking: macro > meso > micro; within level, most-recent-first' with no mention of configurability or a strategy pattern.",
            "impact": "Anti-Stagnation System or Orchestrator may need to weight contexts differently (e.g. prioritize research notes during a research-heavy loop) — currently impossible without modifying ContextCompressor directly.",
        },
        {
            "id": "W5",
            "severity": "low",
            "category": "performance",
            "statement": "FTS5 full-text search on artifact_json blobs will index all JSON keys and values — including structural tokens like 'artifact_type', 'true', 'false', numeric values — creating a noisy index. Queries for domain terms may return artifacts with matching field names rather than matching content.",
            "evidence": "Design specifies 'FTS5 virtual table on artifact_json' without tokenizer configuration or field targeting. Standard FTS5 tokenizes the entire blob.",
            "impact": "Query precision will degrade as the artifact store grows. The Researcher or ContextAssembler may retrieve irrelevant artifacts, wasting token budget.",
        },
    ],
    "objections": [
        {
            "objection": "The proposal claims 'resumability is free' because all state is in the SQLite file. This is only true if the SQLite file is written atomically per artifact. The write path (asyncio.Lock + aiosqlite) does not guarantee that a Tinker crash mid-write leaves the file in a consistent state. The design needs explicit transaction boundaries documented.",
            "specificity_score": 0.88,
        },
        {
            "objection": "The IMemoryManager_TaskEngine interface specifies that TaskEngine 'does not read from MemoryManager directly' — yet candidate_next_task #1 implies TaskEngine emits completion events that trigger Memory Manager writes. This is a coupling assumption that is not architecturally enforced and will likely be violated as Tinker evolves.",
            "specificity_score": 0.81,
        },
        {
            "objection": "The open question 'Should MemoryManager expose a watch/subscribe interface?' is answered implicitly as 'no' by the current design, yet the Orchestrator (by system definition) must react to new artifact writes to advance loop state. The absence of a subscription mechanism forces the Orchestrator into polling — a well-known scalability and latency anti-pattern.",
            "specificity_score": 0.79,
        },
    ],
    "verdict": "revise",
    "revision_required": True,
}


# ---------------------------------------------------------------------------
# EXAMPLE: Researcher Output
# ---------------------------------------------------------------------------

EXAMPLE_RESEARCHER_OUTPUT = {
    "artifact_type": "research_note",
    "artifact_id": "11223344-5566-7788-99aa-bbccddeeff00",
    "research_question": "What are the concurrency characteristics of SQLite WAL mode and is it suitable for Tinker's read/write access pattern?",
    "key_findings": [
        {
            "finding": "SQLite WAL mode supports true concurrent reads from multiple connections simultaneously without blocking each other, as readers use a snapshot of the database at the time they started reading.",
            "source_id": "S1",
            "relevance": "high",
        },
        {
            "finding": "In WAL mode, writes do not block reads and reads do not block writes. However, only one writer is allowed at a time — concurrent write attempts serialize automatically at the SQLite level.",
            "source_id": "S1",
            "relevance": "high",
        },
        {
            "finding": "aiosqlite wraps sqlite3 in a background thread, meaning asyncio-based code can perform non-blocking SQLite I/O. The asyncio.Lock pattern for writes is redundant with SQLite's own write lock but adds process-level protection against within-process write races before the DB layer.",
            "source_id": "S2",
            "relevance": "medium",
        },
        {
            "finding": "SQLite's default page_size of 4096 bytes and WAL file growth behavior mean that long-running processes with frequent writes should periodically call PRAGMA wal_checkpoint(TRUNCATE) to prevent unbounded WAL file growth.",
            "source_id": "S1",
            "relevance": "medium",
        },
    ],
    "source_notes": [
        {
            "source_id": "S1",
            "description": "SQLite official documentation: WAL mode (https://www.sqlite.org/wal.html) — retrieved via web_search tool",
            "credibility": "high",
        },
        {
            "source_id": "S2",
            "description": "aiosqlite GitHub README and documentation (https://github.com/omnilib/aiosqlite) — retrieved via web_fetch tool",
            "credibility": "high",
        },
    ],
    "synthesis": "SQLite WAL mode is well-suited to Tinker's access pattern of concurrent reads from multiple agents with serialized writes from the Orchestrator. The native WAL reader/writer separation eliminates the primary concurrency concern without application-level coordination. The asyncio.Lock pattern proposed in the MemoryManager design adds redundant but harmless within-process write serialization. The most operationally significant risk is WAL file growth over long Tinker runs — periodic checkpointing should be scheduled, ideally as part of MemoryManager startup and shutdown sequences. No alternative concurrency model (e.g. write-ahead queues, connection pools with write routing) appears necessary at Tinker's expected artifact volumes.",
    "knowledge_gaps": [
        "Maximum artifact volume per Tinker run is unknown — WAL checkpoint frequency should be empirically tuned.",
        "aiosqlite's behavior under high-frequency concurrent reads (e.g. 7 concurrent agent read requests) has not been benchmarked for Tinker's latency targets (<50ms).",
    ],
    "confidence": 0.85,
}


# ---------------------------------------------------------------------------
# Filled system + user prompt strings (for integration testing)
# ---------------------------------------------------------------------------

EXAMPLE_FILLED_ARCHITECT_SYSTEM = """You are the Architect agent in Tinker, an autonomous architecture-thinking engine.
Your sole job is to produce rigorous software architecture design artifacts.

RULES — follow every rule without exception:
1. Think step by step. Externalize every reasoning step in "reasoning_chain".
2. Your output MUST be a single JSON object. No prose before or after the JSON.
3. Do not invent fields not in the schema.
4. "artifact_id" must be a UUID v4 you generate.
5. "confidence" reflects genuine epistemic humility — never exceed 0.9 unless the problem is trivially constrained.
6. Identify at least one open question per component.
7. Propose at least two candidate_next_tasks with differing priorities.
8. Optimized for Qwen3-7B / Phi-3-mini: output compact, valid JSON only.

OUTPUT SCHEMA (strict — all fields required):
[... schema omitted for brevity — see ARCHITECT_MICRO template ...]

## BUILD METADATA
build_id: ex001 | role: architect | loop: micro | variants: [socratic_architect]

## VARIANT: SOCRATIC ARCHITECT MODE (ACTIVE)
[... variant injection ...]"""

EXAMPLE_FILLED_ARCHITECT_USER = """## CURRENT ARCHITECTURE STATE
Version: 0.1.0
Established components:
  - ModelClient: wraps Ollama HTTP API, manages request/response lifecycle
  - TaskEngine: maintains task queue, emits task events
[... full context as above ...]

## TASK
Design the Memory Manager component for Tinker.
[... full task as above ...]

## CONSTRAINTS
- Must not require an external database service (embedded only — SQLite acceptable)
[... full constraints ...]

## RELEVANT CONTEXT
Prior micro loop #2 established: Context Assembler will call MemoryManager.get_compressed_context(token_budget=4096)
[... full context ...]

Produce your design_proposal JSON now. Think carefully before writing. Output JSON only."""


# ---------------------------------------------------------------------------
# Registry of all example artifacts (for test suite)
# ---------------------------------------------------------------------------

EXAMPLE_ARTIFACTS = {
    "architect.micro": EXAMPLE_ARCHITECT_MICRO_OUTPUT,
    "critic.micro": EXAMPLE_CRITIC_MICRO_OUTPUT,
    "researcher.micro": EXAMPLE_RESEARCHER_OUTPUT,
}


def get_example_artifact(role: str, loop_level: str) -> dict:
    """Return a reference example artifact for a given role+loop_level."""
    key = f"{role}.{loop_level}"
    if key not in EXAMPLE_ARTIFACTS:
        raise KeyError(
            f"No example artifact for '{key}'. "
            f"Available: {list(EXAMPLE_ARTIFACTS.keys())}"
        )
    return EXAMPLE_ARTIFACTS[key]


def print_example_exchange() -> None:
    """Print the full Architect → Critic micro exchange to stdout."""
    print("=" * 70)
    print("EXAMPLE: Tinker Micro Loop — Architect → Critic Exchange")
    print("=" * 70)
    print("\n--- ARCHITECT OUTPUT ---\n")
    print(json.dumps(EXAMPLE_ARCHITECT_MICRO_OUTPUT, indent=2))
    print("\n--- CRITIC OUTPUT ---\n")
    print(json.dumps(EXAMPLE_CRITIC_MICRO_OUTPUT, indent=2))
    print("\n" + "=" * 70)


if __name__ == "__main__":
    print_example_exchange()
