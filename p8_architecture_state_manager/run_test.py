"""
tinker/tests/run_test.py
Standalone test runner. No pytest needed.
Run:  python3 tinker/tests/run_test.py
"""

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from p8_architecture_state_manager.manager import ArchitectureStateManager

# ──────────────────────────────────────────────
# Update payloads
# ──────────────────────────────────────────────

LOOP_1_UPDATE = {
    "loop": 1,
    "system_name": "Tinker",
    "system_purpose": "Autonomous architecture-thinking engine that continuously designs and refines software architectures.",
    "system_scope": "Local LLM inference, tool execution, and persistent architecture state management.",
    "overall_confidence": 0.35,
    "loop_note": "Initial discovery pass. Three top-level components identified. Confidence is low — many unknowns remain.",
    "components": [
        {
            "name": "Orchestrator",
            "description": "Drives the macro reasoning loop; coordinates all subsystems.",
            "responsibilities": [
                "Trigger each macro loop",
                "Pass context to Context Assembler",
                "Commit state after synthesis",
            ],
            "subsystem": "control",
            "confidence_value": 0.45,
            "confidence_note": "Role is clear but internal loop logic is unknown",
        },
        {
            "name": "Model Client",
            "description": "Thin wrapper around Ollama HTTP API for LLM inference.",
            "responsibilities": [
                "Send prompts to Ollama",
                "Parse streaming responses",
                "Handle retries on transient errors",
            ],
            "subsystem": "inference",
            "confidence_value": 0.72,
            "confidence_note": "Well-understood — standard Ollama HTTP client pattern",
        },
        {
            "name": "Architecture State Manager",
            "description": "Versioned document store tracking Tinker's beliefs about the target system.",
            "responsibilities": [
                "Persist ArchitectureState as JSON",
                "Auto-commit to Git on each loop",
                "Provide context-window summaries",
            ],
            "subsystem": "state",
            "confidence_value": 0.60,
            "confidence_note": "Purpose clear; merge semantics still being defined",
        },
    ],
    "open_questions": [
        {
            "question": "How does the Orchestrator decide when to terminate a reasoning loop?",
            "context": "Needs Anti-Stagnation input or a fixed iteration count.",
            "subsystem": "control",
            "priority": 0.9,
        },
        {
            "question": "Which Ollama model should be used as the default Architect?",
            "context": "Affects quality of architecture analysis significantly.",
            "subsystem": "inference",
            "priority": 0.7,
        },
    ],
    "subsystems": [
        {"name": "control",   "purpose": "Top-level orchestration and loop management.", "components": ["Orchestrator"],                  "confidence_value": 0.40},
        {"name": "inference", "purpose": "LLM inference via Ollama.",                    "components": ["Model Client"],                  "confidence_value": 0.65},
        {"name": "state",     "purpose": "Persistent, versioned architecture state.",    "components": ["Architecture State Manager"],    "confidence_value": 0.55},
    ],
}

LOOP_2_UPDATE = {
    "loop": 2,
    "overall_confidence": 0.52,
    "loop_note": "Deeper analysis. Added Context Assembler and Memory Manager. Resolved model selection question. Two design decisions proposed.",
    "components": [
        {
            "name": "Context Assembler",
            "description": "Builds the prompt context for each inference call.",
            "responsibilities": [
                "Pull compressed state summary",
                "Inject tool results",
                "Enforce context window budget",
            ],
            "subsystem": "control",
            "confidence_value": 0.55,
            "confidence_note": "Interface clear; token-budget logic not yet defined",
        },
        {
            "name": "Memory Manager",
            "description": "Stores and retrieves long-term facts across loops.",
            "responsibilities": [
                "Persist facts beyond context window",
                "Retrieve relevant memories by embedding similarity",
                "Expire stale entries",
            ],
            "subsystem": "state",
            "confidence_value": 0.38,
            "confidence_note": "Concept understood; storage backend undecided",
        },
        {
            "name": "Orchestrator",
            "description": "Drives the macro reasoning loop; coordinates all subsystems.",
            "responsibilities": ["Evaluate termination signal from Anti-Stagnation System"],
            "subsystem": "control",
            "confidence_value": 0.60,
            "confidence_note": "Termination logic clarified",
        },
        {
            "name": "Architecture State Manager",
            "description": "Versioned document store with Git-backed history and diff support.",
            "responsibilities": ["Produce human-readable diffs between loop versions"],
            "subsystem": "state",
            "confidence_value": 0.72,
            "confidence_note": "Merge semantics finalised",
        },
    ],
    "relationships": [
        {
            "source_id": "Orchestrator",
            "target_id": "Context Assembler",
            "kind": "calls",
            "description": "Requests context assembly before each inference call",
            "confidence_value": 0.65,
        },
        {
            "source_id": "Context Assembler",
            "target_id": "Architecture State Manager",
            "kind": "reads_from",
            "description": "Pulls compressed summary from state",
            "confidence_value": 0.70,
        },
        {
            "source_id": "Architecture State Manager",
            "target_id": "Memory Manager",
            "kind": "depends_on",
            "description": "May delegate long-term storage",
            "confidence_value": 0.35,
        },
    ],
    "decisions": [
        {
            "title": "Use Git for state versioning",
            "description": "Each macro loop auto-commits the ArchitectureState JSON to a local Git repo.",
            "rationale": "Git provides free diff, history, and rollback with zero dependencies.",
            "status": "proposed",
            "subsystem": "state",
            "confidence_value": 0.78,
            "alternatives_considered": ["SQLite with row versioning", "S3-style object store"],
        },
        {
            "title": "Additive-only state merges",
            "description": "State updates only add or update items; nothing is ever deleted.",
            "rationale": "Preserves full reasoning history.",
            "status": "proposed",
            "subsystem": "state",
            "confidence_value": 0.65,
        },
    ],
    "open_questions": [
        {
            "question": "Which Ollama model should be used as the default Architect?",
            "resolved": True,
            "resolution": "Use llama3:70b for Architect; llama3:8b for Synthesizer.",
            "resolved_loop": 2,
        },
        {
            "question": "Should Memory Manager use ChromaDB or a plain JSON vector store?",
            "context": "ChromaDB adds a dependency but provides ANN search.",
            "subsystem": "state",
            "priority": 0.8,
        },
    ],
    "subsystems": [
        {"name": "control", "components": ["Context Assembler"], "confidence_value": 0.52},
        {"name": "state",   "components": ["Memory Manager"],    "confidence_value": 0.48},
    ],
}

LOOP_3_UPDATE = {
    "loop": 3,
    "overall_confidence": 0.68,
    "loop_note": "Consolidation. All proposed decisions accepted. Memory Manager backend resolved. Confidence rising.",
    "components": [
        {"name": "Orchestrator",               "confidence_value": 0.78, "confidence_note": "Fully specified"},
        {"name": "Model Client",               "confidence_value": 0.88, "confidence_note": "Straightforward implementation"},
        {"name": "Architecture State Manager", "confidence_value": 0.85, "confidence_note": "Schema, merge, Git all defined"},
        {"name": "Context Assembler",          "confidence_value": 0.70, "confidence_note": "Token-budget sketched"},
        {"name": "Memory Manager",             "confidence_value": 0.58, "confidence_note": "Backend selected (ChromaDB); embedding TBD"},
    ],
    "decisions": [
        {"title": "Use Git for state versioning", "status": "accepted", "confidence_value": 0.90, "confidence_note": "No objections"},
        {"title": "Additive-only state merges",   "status": "accepted", "confidence_value": 0.82, "confidence_note": "Confirmed after tests"},
        {
            "title": "Use ChromaDB for Memory Manager",
            "description": "Memory Manager will use ChromaDB as its vector store backend.",
            "rationale": "ANN search quality outweighs added dependency.",
            "status": "accepted",
            "subsystem": "state",
            "confidence_value": 0.64,
            "alternatives_considered": ["Plain JSON with cosine similarity", "Qdrant", "Weaviate"],
        },
    ],
    "rejected_alternatives": [
        {
            "title": "SQLite row versioning for state history",
            "description": "Store each state version as a row in SQLite.",
            "rejection_reason": "Adds schema migration complexity with no benefit over Git JSON files.",
        },
        {
            "title": "Plain JSON vector store for Memory Manager",
            "description": "Brute-force cosine search over all embeddings.",
            "rejection_reason": "Does not scale beyond a few thousand memories.",
        },
    ],
    "open_questions": [
        {
            "question": "Should Memory Manager use ChromaDB or a plain JSON vector store?",
            "resolved": True,
            "resolution": "ChromaDB selected. See decision.",
            "resolved_loop": 3,
        },
        {
            "question": "Which embedding model should Memory Manager use?",
            "context": "Needs to run locally via Ollama. nomic-embed-text is a candidate.",
            "subsystem": "state",
            "priority": 0.75,
        },
    ],
    "subsystems": [
        {"name": "control",   "confidence_value": 0.70},
        {"name": "inference", "confidence_value": 0.82},
        {"name": "state",     "confidence_value": 0.68},
    ],
}

# ──────────────────────────────────────────────
# Assertions helper
# ──────────────────────────────────────────────

_passed = 0
_failed = 0

def assert_eq(label, got, expected):
    global _passed, _failed
    if got == expected:
        print(f"  ✓  {label}")
        _passed += 1
    else:
        print(f"  ✗  {label}: expected {expected!r}, got {got!r}")
        _failed += 1

def assert_true(label, condition):
    global _passed, _failed
    if condition:
        print(f"  ✓  {label}")
        _passed += 1
    else:
        print(f"  ✗  {label}: condition was False")
        _failed += 1

def hr(title=""):
    print("\n" + "═" * 70)
    if title:
        pad = (70 - len(title) - 2) // 2
        print(" " * pad + f" {title} ")
        print("═" * 70)


# ──────────────────────────────────────────────
# Main test
# ──────────────────────────────────────────────

def run():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = ArchitectureStateManager(
            workspace=tmp,
            system_name="Tinker",
            auto_git=True,
        )

        # ─────────────── Loop 1 ──────────────────────────────────────
        hr("LOOP 1 — Initial Discovery")
        s1 = mgr.apply_update(LOOP_1_UPDATE)

        assert_eq("loop number",      s1.macro_loop,            1)
        assert_eq("component count",  len(s1.components),       3)
        assert_eq("question count",   len(s1.open_questions),   2)

        hr("Loop 1 Summary")
        print(mgr.summarise(budget_tokens=600))

        # ─────────────── Loop 2 ──────────────────────────────────────
        hr("LOOP 2 — Deeper Analysis")
        s2 = mgr.apply_update(LOOP_2_UPDATE)

        assert_eq("loop number",           s2.macro_loop,             2)
        assert_eq("component count",       len(s2.components),        5)
        assert_eq("decision count",        len(s2.decisions),         2)
        assert_eq("relationship count",    len(s2.relationships),     3)

        model_q = s2.question_by_text("Which Ollama model should be used as the default Architect?")
        assert_true("model question resolved", model_q is not None and model_q.resolved)

        hr("Loop 2 Summary")
        print(mgr.summarise(budget_tokens=800))

        hr("Diff: Loop 1 → Loop 2")
        print(mgr.diff(loop_a=1, loop_b=2))

        # ─────────────── Loop 3 ──────────────────────────────────────
        hr("LOOP 3 — Consolidation")
        s3 = mgr.apply_update(LOOP_3_UPDATE)

        assert_eq("loop number",                s3.macro_loop,              3)
        assert_eq("decision count",             len(s3.decisions),          3)
        assert_eq("rejected alt count",         len(s3.rejected_alternatives), 2)

        asm = s3.component_by_name("Architecture State Manager")
        assert_true("ASM confidence > 0.70", asm is not None and asm.confidence.value > 0.70)
        assert_true("ASM evidence_count > 1", asm.confidence.evidence_count > 1)

        # All accepted decisions
        for d in s3.decisions.values():
            if d.title in ("Use Git for state versioning", "Additive-only state merges"):
                assert_true(f"decision '{d.title}' accepted", d.status == "accepted")

        hr("Loop 3 Summary")
        print(mgr.summarise(budget_tokens=1000))

        hr("Diff: Loop 2 → Loop 3")
        print(mgr.diff(loop_a=2, loop_b=3))

        # ─────────────── Queries ─────────────────────────────────────
        hr("Queries")

        low = mgr.low_confidence_components(threshold=0.65)
        print(f"\nLow-confidence components (<0.65):")
        for c in low:
            print(f"  [{c.confidence.value:.3f}] {c.name}")
        assert_true("Memory Manager is low-confidence", any(c.name == "Memory Manager" for c in low))

        unresolved = mgr.unresolved_questions()
        print(f"\nUnresolved questions ({len(unresolved)}):")
        for q in unresolved:
            print(f"  [priority={q.priority:.1f}] {q.question}")
        assert_true("at least one unresolved question", len(unresolved) >= 1)

        state_decs = mgr.decisions_for_subsystem("state")
        print(f"\nDecisions in 'state' subsystem ({len(state_decs)}):")
        for d in state_decs:
            print(f"  [{d.status}] {d.title}")
        assert_true("at least 2 state decisions", len(state_decs) >= 2)

        cmap = mgr.confidence_map()
        print(f"\nConfidence map ({len(cmap)} entries):")
        for k, v in sorted(cmap.items(), key=lambda x: -x[1]):
            print(f"  {v:.3f}  {k}")
        assert_true("confidence map has entries", len(cmap) > 0)

        # ─────────────── Snapshot history ────────────────────────────
        hr("Snapshot History")
        snaps = mgr.list_snapshots()
        for s in snaps:
            print(f"  loop={s['loop']:>2}  components={s['components']}  "
                  f"decisions={s['decisions']}  confidence={s['confidence']:.3f}")
        assert_eq("snapshot count", len(snaps), 3)

        # ─────────────── Git log ──────────────────────────────────────
        hr("Git Log")
        log = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=tmp, capture_output=True, text=True,
        )
        print(log.stdout)
        assert_true("git log has commits", "loop" in log.stdout)

        # ─────────────── Summary ─────────────────────────────────────
        hr("TEST RESULTS")
        print(f"  Passed : {_passed}")
        print(f"  Failed : {_failed}")
        if _failed == 0:
            print("\n  ALL TESTS PASSED ✓")
        else:
            print(f"\n  {_failed} TEST(S) FAILED ✗")
            sys.exit(1)


if __name__ == "__main__":
    run()
