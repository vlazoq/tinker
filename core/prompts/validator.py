"""
validator.py — Runtime validation of model outputs against Tinker JSON schemas.

Usage:
    from core.prompts.validator import OutputValidator, ValidationResult

    validator = OutputValidator()
    result = validator.validate(raw_output, role="architect", loop_level="micro")
    if not result.valid:
        print(result.errors)
    else:
        artifact = result.data
"""

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

try:
    import jsonschema  # noqa: F401
    from jsonschema import ValidationError as _JVError
    from jsonschema import validate as _jv_validate

    _JSONSCHEMA_AVAILABLE = True
except ImportError:
    _JSONSCHEMA_AVAILABLE = False

from .schemas import SCHEMA_REGISTRY
from .templates import LoopLevel, Role

# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    valid: bool
    data: dict[str, Any] | None  # parsed artifact, or None on failure
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    auto_repaired: bool = False  # True if heuristic repairs were applied

    def raise_if_invalid(self) -> None:
        if not self.valid:
            msg = "\n".join(self.errors)
            raise ValueError(f"Artifact validation failed:\n{msg}")


# ---------------------------------------------------------------------------
# Main validator
# ---------------------------------------------------------------------------


class OutputValidator:
    """
    Validates and optionally auto-repairs raw model output strings.

    Repair heuristics (applied before schema validation):
      - Strip leading/trailing non-JSON text (```json fences, prose)
      - Inject missing artifact_id (UUID v4) if absent
      - Inject missing loop_level if absent and inferrable
      - Clamp out-of-range numeric fields (confidence, specificity_score)
      - Ensure minimum array lengths by logging warnings (not injecting fake data)
    """

    def validate(
        self,
        raw_output: str,
        role: Role,
        loop_level: LoopLevel,
        auto_repair: bool = True,
    ) -> ValidationResult:
        """
        Parse and validate raw model output for a given (role, loop_level).

        Args:
            raw_output:  Raw string from the model.
            role:        Expected agent role.
            loop_level:  Expected loop level.
            auto_repair: If True, attempt heuristic repairs before schema validation.

        Returns:
            ValidationResult with valid/invalid status, parsed data, errors, warnings.
        """
        key = f"{role}.{loop_level}"

        # Step 1: Extract JSON
        extracted, extract_warnings = self._extract_json(raw_output)
        if extracted is None:
            return ValidationResult(
                valid=False,
                data=None,
                errors=["Could not extract a JSON object from model output."],
                warnings=extract_warnings,
            )

        warnings = list(extract_warnings)
        repaired = False

        # Step 2: Auto-repair
        if auto_repair:
            extracted, repair_warnings, repaired = self._auto_repair(extracted, role, loop_level)
            warnings.extend(repair_warnings)

        # Step 3: Schema validation
        schema = SCHEMA_REGISTRY.get(key)
        if schema is None:
            warnings.append(f"No schema registered for key '{key}' — skipping schema check.")
            return ValidationResult(
                valid=True, data=extracted, warnings=warnings, auto_repaired=repaired
            )

        schema_errors = self._schema_validate(extracted, schema)
        if schema_errors:
            return ValidationResult(
                valid=False,
                data=extracted,
                errors=schema_errors,
                warnings=warnings,
                auto_repaired=repaired,
            )

        # Step 4: Semantic checks (beyond JSON schema)
        semantic_errors, semantic_warnings = self._semantic_checks(extracted, role, loop_level)
        warnings.extend(semantic_warnings)
        if semantic_errors:
            return ValidationResult(
                valid=False,
                data=extracted,
                errors=semantic_errors,
                warnings=warnings,
                auto_repaired=repaired,
            )

        return ValidationResult(
            valid=True,
            data=extracted,
            warnings=warnings,
            auto_repaired=repaired,
        )

    # ------------------------------------------------------------------
    # JSON extraction
    # ------------------------------------------------------------------

    def _extract_json(self, raw: str) -> tuple[dict | None, list[str]]:
        """Try to extract a JSON object from raw model output."""
        warnings: list[str] = []
        raw = raw.strip()

        # Direct parse
        try:
            return json.loads(raw), warnings
        except json.JSONDecodeError:
            pass

        # Strip ```json … ``` fences
        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if fence_match:
            warnings.append("Stripped markdown code fence from output.")
            try:
                return json.loads(fence_match.group(1)), warnings
            except json.JSONDecodeError:
                pass

        # Find the first { … } block
        brace_match = re.search(r"(\{.*\})", raw, re.DOTALL)
        if brace_match:
            warnings.append("Extracted JSON block from mixed-content output.")
            try:
                return json.loads(brace_match.group(1)), warnings
            except json.JSONDecodeError:
                pass

        return None, [*warnings, "All JSON extraction attempts failed."]

    # ------------------------------------------------------------------
    # Auto-repair
    # ------------------------------------------------------------------

    def _auto_repair(
        self,
        data: dict,
        role: Role,
        loop_level: LoopLevel,
    ) -> tuple[dict, list[str], bool]:
        """Apply heuristic repairs. Returns (repaired_data, warnings, was_repaired)."""
        warnings: list[str] = []
        repaired = False

        # Inject artifact_id if missing or placeholder
        if not data.get("artifact_id") or data.get("artifact_id") in (
            "<uuid4>",
            "uuid4",
            "",
        ):
            data["artifact_id"] = str(uuid.uuid4())
            warnings.append("Auto-injected missing artifact_id (UUID v4).")
            repaired = True

        # Inject loop_level if missing
        if "loop_level" not in data or not data["loop_level"]:
            data["loop_level"] = loop_level
            warnings.append(f"Auto-injected missing loop_level='{loop_level}'.")
            repaired = True

        # Clamp confidence
        if "confidence" in data:
            c = data["confidence"]
            if not isinstance(c, (int, float)):
                data["confidence"] = 0.5
                warnings.append("confidence was non-numeric; reset to 0.5.")
                repaired = True
            elif c < 0.0:
                data["confidence"] = 0.0
                warnings.append("confidence clamped from below 0.0 to 0.0.")
                repaired = True
            elif c > 1.0:
                data["confidence"] = 1.0
                warnings.append("confidence clamped from above 1.0 to 1.0.")
                repaired = True

        # Clamp specificity_score values inside objections
        for obj in data.get("objections", []):
            if "specificity_score" in obj:
                s = obj["specificity_score"]
                if isinstance(s, (int, float)):
                    obj["specificity_score"] = max(0.0, min(1.0, s))
                else:
                    obj["specificity_score"] = 0.5
                    warnings.append("specificity_score was non-numeric; reset to 0.5.")
                    repaired = True

        # Repair weakness IDs: ensure W1, W2, ...
        weaknesses = data.get("weaknesses", [])
        for i, w in enumerate(weaknesses):
            expected_id = f"W{i + 1}"
            if w.get("id") != expected_id:
                w["id"] = expected_id
                repaired = True

        # Ensure revision_required is boolean
        if "revision_required" in data and not isinstance(data["revision_required"], bool):
            data["revision_required"] = data["verdict"] in ("revise", "reject")
            warnings.append("revision_required was non-boolean; derived from verdict.")
            repaired = True

        # Ensure version string is present for macro artifacts
        if loop_level == "macro" and "version" in data:
            v = data["version"]
            if not re.match(r"^\d+\.\d+\.\d+$", str(v)):
                warnings.append(
                    f"version '{v}' does not match MAJOR.MINOR.PATCH — not auto-repaired."
                )

        return data, warnings, repaired

    # ------------------------------------------------------------------
    # JSON schema validation
    # ------------------------------------------------------------------

    def _schema_validate(self, data: dict, schema: dict) -> list[str]:
        if _JSONSCHEMA_AVAILABLE:
            errors: list[str] = []
            try:
                _jv_validate(instance=data, schema=schema)
            except _JVError as e:
                errors.append(f"Schema violation at '{e.json_path}': {e.message}")
                # Also collect child errors
                for sub in getattr(e, "context", []):
                    errors.append(f"  → {sub.message}")
            return errors
        else:
            # Fallback: manual structural checks
            return self._manual_schema_check(data, schema)

    def _manual_schema_check(self, data: dict, schema: dict) -> list[str]:
        """Minimal fallback when jsonschema is not installed."""
        errors: list[str] = []
        required = schema.get("required", [])
        for field_name in required:
            if field_name not in data:
                errors.append(f"Missing required field: '{field_name}'")
        return errors

    # ------------------------------------------------------------------
    # Semantic checks (business rules beyond schema)
    # ------------------------------------------------------------------

    def _semantic_checks(
        self, data: dict, role: Role, loop_level: LoopLevel
    ) -> tuple[list[str], list[str]]:
        errors: list[str] = []
        warnings: list[str] = []

        if role == "critic":
            errors.extend(self._check_critic_semantics(data, loop_level))

        if role == "architect":
            errors.extend(self._check_architect_semantics(data, loop_level))

        if role == "synthesizer":
            warnings.extend(self._check_synthesizer_semantics(data, loop_level))

        return errors, warnings

    def _check_critic_semantics(self, data: dict, loop_level: LoopLevel) -> list[str]:
        errors: list[str] = []
        weaknesses = data.get("weaknesses", [])

        # Minimum 3 weaknesses
        if len(weaknesses) < 3:
            errors.append(f"Critic must identify at least 3 weaknesses; found {len(weaknesses)}.")

        # Check for generic/non-specific weakness statements
        generic_phrases = [
            "could be improved",
            "might have issues",
            "may cause problems",
            "could be better",
            "needs more thought",
        ]
        for w in weaknesses:
            stmt = w.get("statement", "").lower()
            for phrase in generic_phrases:
                if phrase in stmt:
                    errors.append(
                        f"Weakness '{w.get('id')}' contains generic phrase '{phrase}' — "
                        "must be specific (name components/interfaces/decisions)."
                    )
                    break

        # Ensure verdict aligns with revision_required
        verdict = data.get("verdict")
        revision_required = data.get("revision_required")
        if verdict == "accept" and revision_required is True:
            errors.append("Inconsistency: verdict='accept' but revision_required=True.")
        if verdict in ("revise", "reject") and revision_required is False:
            errors.append(f"Inconsistency: verdict='{verdict}' but revision_required=False.")

        # confidence_score should not be too high given weaknesses
        confidence = data.get("confidence_score", 0.0)
        critical_or_high = [w for w in weaknesses if w.get("severity") in ("critical", "high")]
        if critical_or_high and confidence > 0.8:
            errors.append(
                f"confidence_score={confidence:.2f} is too high given "
                f"{len(critical_or_high)} critical/high severity weaknesses."
            )

        return errors

    def _check_architect_semantics(self, data: dict, loop_level: LoopLevel) -> list[str]:
        errors: list[str] = []

        # Reasoning chain minimum depth
        chain = data.get("reasoning_chain", [])
        min_steps = {"micro": 3, "meso": 4, "macro": 5}
        required_steps = min_steps.get(loop_level, 3)
        if len(chain) < required_steps:
            errors.append(
                f"reasoning_chain has {len(chain)} steps; "
                f"minimum {required_steps} required for {loop_level} loop."
            )

        # No duplicate component names
        components = data.get("design", {}).get("components", [])
        if loop_level == "micro" and components:
            names = [c.get("name", "") for c in components]
            if len(names) != len(set(names)):
                errors.append("Duplicate component names found in design.components.")

        # candidate_next_tasks must have at least 2 tasks
        tasks = data.get("candidate_next_tasks", [])
        if len(tasks) < 2:
            errors.append(f"candidate_next_tasks has {len(tasks)} entries; minimum 2 required.")

        # At least one high priority task
        priorities = [t.get("priority") for t in tasks]
        if tasks and "high" not in priorities:
            errors.append("candidate_next_tasks must contain at least one 'high' priority task.")

        return errors

    def _check_synthesizer_semantics(self, data: dict, loop_level: LoopLevel) -> list[str]:
        warnings: list[str] = []

        # Warn if no contradictions were found (might mean synthesis was too shallow)
        contradictions = data.get("contradictions_resolved", [])
        if len(contradictions) == 0:
            warnings.append(
                "No contradictions resolved — synthesis may be too shallow. "
                "Consider if source artifacts truly had no conflicts."
            )

        # Warn if compressed_narrative is too short
        narrative = data.get("compressed_narrative", "")
        min_len = 200 if loop_level == "macro" else 100
        if len(narrative.split()) < min_len // 5:  # rough word count check
            warnings.append(f"compressed_narrative may be too brief for {loop_level} synthesis.")

        return warnings


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------


def validate_output(
    raw_output: str,
    role: Role,
    loop_level: LoopLevel,
    auto_repair: bool = True,
) -> ValidationResult:
    """Module-level convenience wrapper around OutputValidator."""
    return OutputValidator().validate(raw_output, role, loop_level, auto_repair)
