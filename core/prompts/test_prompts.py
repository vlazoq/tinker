"""
test_prompts.py — Self-contained test suite for the Tinker prompt system.

Run with:  python -m pytest test_prompts.py -v
     or:   python test_prompts.py
"""

import json
import sys
import uuid
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent))

from core.prompts import (
    SCHEMA_REGISTRY,
    TEMPLATE_REGISTRY,
    VARIANT_REGISTRY,
    PromptBuilder,
    PromptBuilderError,
    validate_output,
    validate_variant_combination,
)
from core.prompts.examples import (
    EXAMPLE_ARCHITECT_MICRO_OUTPUT,
    EXAMPLE_CRITIC_MICRO_OUTPUT,
    EXAMPLE_RESEARCHER_OUTPUT,
)

# ===========================================================================
# Helpers
# ===========================================================================


def assert_equal(a, b, msg=""):
    assert a == b, f"Expected {b!r}, got {a!r}. {msg}"


def assert_true(val, msg=""):
    assert val, f"Expected True, got {val!r}. {msg}"


def assert_false(val, msg=""):
    assert not val, f"Expected False, got {val!r}. {msg}"


def assert_in(item, container, msg=""):
    assert item in container, f"{item!r} not found in {container!r}. {msg}"


def run_test(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
        return True
    except Exception as e:
        print(f"  ✗ {name}: {e}")
        return False


# ===========================================================================
# Tests: Registry completeness
# ===========================================================================


def test_all_template_keys_present():
    expected = [
        "architect.micro",
        "architect.meso",
        "architect.macro",
        "critic.micro",
        "critic.meso",
        "critic.macro",
        "researcher.micro",
        "researcher.meso",
        "synthesizer.meso",
        "synthesizer.macro",
    ]
    for key in expected:
        assert_in(key, TEMPLATE_REGISTRY, f"Missing template: {key}")


def test_all_schema_keys_present():
    expected = [
        "architect.micro",
        "architect.meso",
        "architect.macro",
        "critic.micro",
        "critic.meso",
        "critic.macro",
        "researcher.micro",
        "researcher.meso",
        "synthesizer.meso",
        "synthesizer.macro",
    ]
    for key in expected:
        assert_in(key, SCHEMA_REGISTRY, f"Missing schema: {key}")


def test_all_variant_keys_present():
    expected = [
        "harder_critic",
        "alternative_forcing",
        "contradiction_injection",
        "devil_advocate_critic",
        "socratic_architect",
        "paranoid_security",
        "minimum_viable_design",
        "scalability_stress",
    ]
    for key in expected:
        assert_in(key, VARIANT_REGISTRY, f"Missing variant: {key}")


# ===========================================================================
# Tests: PromptBuilder
# ===========================================================================


def test_builder_architect_micro_basic():
    system, user = PromptBuilder.for_architect_micro(
        architecture_state="Version: 0.1.0. No components yet.",
        task_description="Design the TaskEngine component.",
    )
    assert_true(len(system) > 100, "System prompt too short")
    assert_true(len(user) > 50, "User prompt too short")
    assert_in("artifact_type", system)
    assert_in("design_proposal", system)
    assert_in("TaskEngine", user)


def test_builder_with_variant():
    system, _user = PromptBuilder.for_architect_micro(
        architecture_state="...",
        task_description="Design TaskEngine.",
        variants=["socratic_architect"],
    )
    assert_in("SOCRATIC ARCHITECT MODE", system)
    assert_in("Why this and not something simpler?", system)


def test_builder_variant_incompatibility():
    b = PromptBuilder()
    try:
        b.build(
            role="critic",
            loop_level="micro",
            context={
                "target_artifact_json": "{}",
                "architecture_state": "...",
                "focus_areas": "general",
            },
            variants=["harder_critic", "devil_advocate_critic"],
        )
        raise AssertionError("Should have raised PromptBuilderError")
    except PromptBuilderError as e:
        assert_in("incompatible", str(e).lower())


def test_builder_missing_context_raises():
    b = PromptBuilder()
    try:
        b.build(
            role="architect",
            loop_level="micro",
            context={"architecture_state": "..."},  # missing task_description etc.
        )
        raise AssertionError("Should have raised PromptBuilderError")
    except PromptBuilderError as e:
        assert_in("missing", str(e).lower())


def test_builder_wrong_variant_for_role():
    b = PromptBuilder()
    try:
        b.build(
            role="architect",
            loop_level="micro",
            context={
                "architecture_state": "...",
                "task_description": "...",
                "constraints": "...",
                "context": "...",
            },
            variants=["harder_critic"],  # critic-only variant
        )
        raise AssertionError("Should have raised PromptBuilderError")
    except PromptBuilderError as e:
        assert_in("not applicable", str(e).lower())


def test_builder_context_dict_serialized():
    """Context values that are dicts should be auto-serialized to JSON strings."""
    b = PromptBuilder()
    context = {
        "architecture_state": "...",
        "task_description": "...",
        "constraints": "...",
        "context": {"some": "dict"},  # should be auto-serialized
    }
    _system, user = b.build(role="architect", loop_level="micro", context=context)
    assert_in('"some"', user)


def test_builder_critic_micro():
    b = PromptBuilder()
    system, user = b.build(
        role="critic",
        loop_level="micro",
        context={
            "target_artifact_json": json.dumps(EXAMPLE_ARCHITECT_MICRO_OUTPUT),
            "architecture_state": "...",
            "focus_areas": "reliability and scalability",
        },
    )
    assert_in("critique", system)
    assert_in("weaknesses", system)
    assert_in("reliability", user)


def test_builder_researcher():
    system, user = PromptBuilder.for_researcher(
        research_question="Is SQLite WAL suitable for concurrent reads?",
        tool_results="[web_search result: SQLite WAL docs say readers don't block writers]",
    )
    assert_in("research_note", system)
    assert_in("SQLite WAL", user)


# ===========================================================================
# Tests: Validator — example outputs
# ===========================================================================


def test_validate_example_architect_micro():
    raw = json.dumps(EXAMPLE_ARCHITECT_MICRO_OUTPUT)
    result = validate_output(raw, role="architect", loop_level="micro")
    assert_true(result.valid, f"Architect micro example failed: {result.errors}")


def test_validate_example_critic_micro():
    raw = json.dumps(EXAMPLE_CRITIC_MICRO_OUTPUT)
    result = validate_output(raw, role="critic", loop_level="micro")
    assert_true(result.valid, f"Critic micro example failed: {result.errors}")


def test_validate_example_researcher():
    raw = json.dumps(EXAMPLE_RESEARCHER_OUTPUT)
    result = validate_output(raw, role="researcher", loop_level="micro")
    assert_true(result.valid, f"Researcher example failed: {result.errors}")


# ===========================================================================
# Tests: Validator — auto-repair
# ===========================================================================


def test_auto_repair_missing_artifact_id():
    artifact = {**EXAMPLE_ARCHITECT_MICRO_OUTPUT, "artifact_id": "<uuid4>"}
    raw = json.dumps(artifact)
    result = validate_output(raw, role="architect", loop_level="micro")
    assert_true(result.auto_repaired)
    # Should now be a valid UUID
    repaired_id = result.data["artifact_id"]
    try:
        uuid.UUID(repaired_id)
    except ValueError as err:
        raise AssertionError(f"Repaired artifact_id '{repaired_id}' is not a valid UUID") from err


def test_auto_repair_confidence_clamp_high():
    artifact = {**EXAMPLE_ARCHITECT_MICRO_OUTPUT, "confidence": 1.5}
    raw = json.dumps(artifact)
    result = validate_output(raw, role="architect", loop_level="micro")
    assert_true(result.auto_repaired)
    assert_equal(result.data["confidence"], 1.0)


def test_auto_repair_confidence_clamp_low():
    artifact = {**EXAMPLE_ARCHITECT_MICRO_OUTPUT, "confidence": -0.3}
    raw = json.dumps(artifact)
    result = validate_output(raw, role="architect", loop_level="micro")
    assert_true(result.auto_repaired)
    assert_equal(result.data["confidence"], 0.0)


def test_auto_repair_markdown_fence_stripping():
    raw = (
        "Here is my design:\n```json\n"
        + json.dumps(EXAMPLE_ARCHITECT_MICRO_OUTPUT)
        + "\n```\nLet me know if this works!"
    )
    result = validate_output(raw, role="architect", loop_level="micro")
    assert_true(result.valid, f"Should have repaired markdown fence: {result.errors}")
    assert_true(result.auto_repaired or len(result.warnings) > 0)


def test_auto_repair_weakness_ids_renumbered():
    artifact = json.loads(json.dumps(EXAMPLE_CRITIC_MICRO_OUTPUT))
    for i, w in enumerate(artifact["weaknesses"]):
        w["id"] = f"X{i + 10}"  # wrong IDs
    raw = json.dumps(artifact)
    result = validate_output(raw, role="critic", loop_level="micro")
    # IDs should be repaired to W1, W2, ...
    assert_equal(result.data["weaknesses"][0]["id"], "W1")


# ===========================================================================
# Tests: Validator — semantic failures
# ===========================================================================


def test_semantic_failure_too_few_weaknesses():
    artifact = json.loads(json.dumps(EXAMPLE_CRITIC_MICRO_OUTPUT))
    artifact["weaknesses"] = artifact["weaknesses"][:1]  # only 1 weakness
    raw = json.dumps(artifact)
    result = validate_output(raw, role="critic", loop_level="micro")
    assert_false(result.valid)
    assert_true(any("3 weaknesses" in e for e in result.errors))


def test_semantic_failure_confidence_too_high_with_critical_weakness():
    artifact = json.loads(json.dumps(EXAMPLE_CRITIC_MICRO_OUTPUT))
    artifact["confidence_score"] = 0.95
    artifact["weaknesses"][0]["severity"] = "critical"
    raw = json.dumps(artifact)
    result = validate_output(raw, role="critic", loop_level="micro")
    assert_false(result.valid)
    assert_true(any("confidence_score" in e for e in result.errors))


def test_semantic_failure_verdict_accept_but_revision_required():
    artifact = json.loads(json.dumps(EXAMPLE_CRITIC_MICRO_OUTPUT))
    artifact["verdict"] = "accept"
    artifact["revision_required"] = True
    raw = json.dumps(artifact)
    result = validate_output(raw, role="critic", loop_level="micro")
    assert_false(result.valid)
    assert_true(any("Inconsistency" in e for e in result.errors))


def test_semantic_failure_architect_too_few_reasoning_steps():
    artifact = json.loads(json.dumps(EXAMPLE_ARCHITECT_MICRO_OUTPUT))
    artifact["reasoning_chain"] = artifact["reasoning_chain"][:1]  # only 1 step
    raw = json.dumps(artifact)
    result = validate_output(raw, role="architect", loop_level="micro")
    assert_false(result.valid)
    assert_true(any("reasoning_chain" in e for e in result.errors))


def test_semantic_failure_no_high_priority_task():
    artifact = json.loads(json.dumps(EXAMPLE_ARCHITECT_MICRO_OUTPUT))
    for t in artifact["candidate_next_tasks"]:
        t["priority"] = "low"
    raw = json.dumps(artifact)
    result = validate_output(raw, role="architect", loop_level="micro")
    assert_false(result.valid)
    assert_true(any("high" in e for e in result.errors))


# ===========================================================================
# Tests: Variant validation
# ===========================================================================


def test_variant_conflict_detection():
    conflicts = validate_variant_combination(["harder_critic", "devil_advocate_critic"])
    assert_true(len(conflicts) > 0)


def test_variant_no_conflict():
    conflicts = validate_variant_combination(["harder_critic", "scalability_stress"])
    assert_equal(conflicts, [])


def test_variant_alternative_forcing_minimum_viable_conflict():
    conflicts = validate_variant_combination(["alternative_forcing", "minimum_viable_design"])
    assert_true(len(conflicts) > 0)


# ===========================================================================
# Runner
# ===========================================================================

TESTS = [
    # Registry
    ("Registry: all template keys present", test_all_template_keys_present),
    ("Registry: all schema keys present", test_all_schema_keys_present),
    ("Registry: all variant keys present", test_all_variant_keys_present),
    # Builder
    ("Builder: architect micro basic", test_builder_architect_micro_basic),
    ("Builder: with variant", test_builder_with_variant),
    ("Builder: variant incompatibility raises", test_builder_variant_incompatibility),
    ("Builder: missing context raises", test_builder_missing_context_raises),
    ("Builder: wrong variant for role raises", test_builder_wrong_variant_for_role),
    ("Builder: dict context auto-serialized", test_builder_context_dict_serialized),
    ("Builder: critic micro", test_builder_critic_micro),
    ("Builder: researcher", test_builder_researcher),
    # Validator: examples pass
    ("Validate: architect micro example", test_validate_example_architect_micro),
    ("Validate: critic micro example", test_validate_example_critic_micro),
    ("Validate: researcher example", test_validate_example_researcher),
    # Validator: auto-repair
    ("Repair: missing artifact_id", test_auto_repair_missing_artifact_id),
    ("Repair: confidence clamp high", test_auto_repair_confidence_clamp_high),
    ("Repair: confidence clamp low", test_auto_repair_confidence_clamp_low),
    ("Repair: markdown fence stripping", test_auto_repair_markdown_fence_stripping),
    ("Repair: weakness IDs renumbered", test_auto_repair_weakness_ids_renumbered),
    # Validator: semantic failures
    ("Semantic: too few weaknesses", test_semantic_failure_too_few_weaknesses),
    (
        "Semantic: confidence too high w/ critical",
        test_semantic_failure_confidence_too_high_with_critical_weakness,
    ),
    (
        "Semantic: accept + revision_required",
        test_semantic_failure_verdict_accept_but_revision_required,
    ),
    (
        "Semantic: too few reasoning steps",
        test_semantic_failure_architect_too_few_reasoning_steps,
    ),
    ("Semantic: no high priority task", test_semantic_failure_no_high_priority_task),
    # Variants
    ("Variant: conflict detection", test_variant_conflict_detection),
    ("Variant: no conflict", test_variant_no_conflict),
    (
        "Variant: alt-forcing/min-viable conflict",
        test_variant_alternative_forcing_minimum_viable_conflict,
    ),
]


if __name__ == "__main__":
    print("\nTinker Prompt System — Test Suite")
    print("=" * 50)
    passed = 0
    failed = 0
    for name, fn in TESTS:
        if run_test(name, fn):
            passed += 1
        else:
            failed += 1
    print("=" * 50)
    print(f"Results: {passed} passed, {failed} failed out of {len(TESTS)} tests")
    sys.exit(0 if failed == 0 else 1)
