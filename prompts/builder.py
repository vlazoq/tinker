"""
builder.py — PromptBuilder assembles production-ready prompts for Tinker agents.

Usage:
    from prompts.builder import PromptBuilder

    builder = PromptBuilder()
    system, user = builder.build(
        role="architect",
        loop_level="micro",
        context={
            "architecture_state": "...",
            "task_description": "Design the Memory Manager component",
            "constraints": "Must work with Ollama HTTP API",
            "context": "Prior micro loops established event-bus pattern",
        },
        variants=["socratic_architect"],
    )
"""

import json
import uuid
from typing import Any

from .templates import TEMPLATE_REGISTRY, PromptTemplate, Role, LoopLevel
from .variants import (
    VariantKey,
    validate_variant_combination,
    get_variant,
)


# PromptBuilderError is defined in the central exceptions module and
# re-exported here so ``from prompts.builder import PromptBuilderError``
# continues to work.
from exceptions import PromptBuilderError  # noqa: F401  (intentional re-export)


class PromptBuilder:
    """
    Assembles final (system, user) prompt pairs for any Tinker agent.

    The builder:
      1. Selects the base template from TEMPLATE_REGISTRY
      2. Applies variant system injections in order
      3. Substitutes context variables into the user prompt
      4. Validates that all required placeholders are filled
      5. Optionally stamps a build_id into the system prompt for observability
    """

    # Required context keys per (role, loop_level) combination
    REQUIRED_CONTEXT: dict[str, list[str]] = {
        "architect.micro": [
            "architecture_state",
            "task_description",
            "constraints",
            "context",
        ],
        "architect.meso": [
            "micro_artifacts_json",
            "architecture_state",
            "synthesis_directive",
            "constraints",
        ],
        "architect.macro": [
            "prior_version",
            "prior_architecture",
            "meso_artifacts_json",
            "evolution_directive",
            "constraints",
            "research_notes",
        ],
        "critic.micro": ["target_artifact_json", "architecture_state", "focus_areas"],
        "critic.meso": [
            "target_artifact_json",
            "source_micro_artifacts_json",
            "architecture_state",
            "focus_areas",
        ],
        "critic.macro": [
            "target_artifact_json",
            "prior_architecture",
            "accumulated_critiques",
            "focus_areas",
        ],
        "researcher.micro": [
            "research_question",
            "tool_results",
            "architecture_context",
        ],
        "researcher.meso": [
            "research_question",
            "tool_results",
            "architecture_context",
        ],
        "synthesizer.meso": [
            "source_artifacts_json",
            "prior_meso_synthesis",
            "synthesis_directive",
        ],
        "synthesizer.macro": [
            "meso_syntheses_json",
            "macro_architect_json",
            "macro_critic_json",
            "prior_version",
            "prior_canonical_json",
            "research_notes_json",
        ],
    }

    def __init__(self, add_build_metadata: bool = True):
        self.add_build_metadata = add_build_metadata

    def build(
        self,
        role: Role,
        loop_level: LoopLevel,
        context: dict[str, Any],
        variants: list[VariantKey] | None = None,
        build_id: str | None = None,
    ) -> tuple[str, str]:
        """
        Assemble (system_prompt, user_prompt) for the given agent configuration.

        Args:
            role:        "architect" | "critic" | "researcher" | "synthesizer"
            loop_level:  "micro" | "meso" | "macro"
            context:     dict of placeholder_name → value (str or JSON-serializable)
            variants:    list of VariantKey strings to apply
            build_id:    optional ID for observability; auto-generated if None

        Returns:
            (system_prompt, user_prompt) as strings ready to send to the model.

        Raises:
            PromptBuilderError: if template not found, variants conflict, or context incomplete.
        """
        variants = variants or []
        build_id = build_id or str(uuid.uuid4())[:8]
        template_key = f"{role}.{loop_level}"

        # 1. Retrieve template
        template = self._get_template(template_key)

        # 2. Validate variants
        self._validate_variants(variants, role)

        # 3. Validate context completeness
        self._validate_context(template_key, context)

        # 4. Build system prompt
        system = self._assemble_system(template, variants, role, build_id)

        # 5. Build user prompt
        user = self._assemble_user(template, variants, context)

        return system, user

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_template(self, key: str) -> PromptTemplate:
        if key not in TEMPLATE_REGISTRY:
            raise PromptBuilderError(
                f"No template found for key '{key}'. "
                f"Available: {sorted(TEMPLATE_REGISTRY.keys())}"
            )
        return TEMPLATE_REGISTRY[key]

    def _validate_variants(self, variants: list[VariantKey], role: Role) -> None:
        # Check for incompatibilities
        conflicts = validate_variant_combination(variants)
        if conflicts:
            raise PromptBuilderError(
                "Variant conflicts detected:\n"
                + "\n".join(f"  - {c}" for c in conflicts)
            )
        # Check applicability
        for vk in variants:
            variant = get_variant(vk)
            if role not in variant.applicable_roles:
                raise PromptBuilderError(
                    f"Variant '{vk}' is not applicable to role '{role}'. "
                    f"Applicable roles: {variant.applicable_roles}"
                )

    def _validate_context(self, template_key: str, context: dict[str, Any]) -> None:
        required = self.REQUIRED_CONTEXT.get(template_key, [])
        missing = [k for k in required if k not in context]
        if missing:
            raise PromptBuilderError(
                f"Missing required context keys for '{template_key}': {missing}"
            )

    def _assemble_system(
        self,
        template: PromptTemplate,
        variants: list[VariantKey],
        role: Role,
        build_id: str,
    ) -> str:
        parts = [template.system]

        # Append variant injections in order
        for vk in variants:
            variant = get_variant(vk)
            if variant.system_injection:
                parts.append(variant.system_injection.strip())

        # Append build metadata
        if self.add_build_metadata:
            parts.append(
                f"\n## BUILD METADATA\n"
                f"build_id: {build_id} | role: {role} | loop: {template.loop_level} | "
                f"variants: [{', '.join(variants) or 'none'}]"
            )

        return "\n\n".join(parts)

    def _assemble_user(
        self,
        template: PromptTemplate,
        variants: list[VariantKey],
        context: dict[str, Any],
    ) -> str:
        # Normalise context values: dicts/lists → JSON strings
        normalised = {}
        for k, v in context.items():
            if isinstance(v, (dict, list)):
                normalised[k] = json.dumps(v, indent=2)
            else:
                normalised[k] = str(v)

        # Prepend any variant user injections
        prefix = ""
        for vk in variants:
            variant = get_variant(vk)
            if variant.user_injection:
                prefix += variant.user_injection

        # Substitute placeholders
        try:
            user_body = template.user.format(**normalised)
        except KeyError as exc:
            raise PromptBuilderError(
                f"User prompt references placeholder {exc} not found in context. "
                f"Provided keys: {list(normalised.keys())}"
            ) from exc

        return prefix + user_body

    # ------------------------------------------------------------------
    # Convenience factory methods
    # ------------------------------------------------------------------

    @classmethod
    def for_architect_micro(
        cls,
        architecture_state: str,
        task_description: str,
        constraints: str = "None specified.",
        context: str = "None.",
        variants: list[VariantKey] | None = None,
    ) -> tuple[str, str]:
        """Quick factory for the most common architect micro invocation."""
        b = cls()
        return b.build(
            role="architect",
            loop_level="micro",
            context={
                "architecture_state": architecture_state,
                "task_description": task_description,
                "constraints": constraints,
                "context": context,
            },
            variants=variants,
        )

    @classmethod
    def for_critic_micro(
        cls,
        target_artifact: dict[str, Any],
        architecture_state: str,
        focus_areas: str = "General design quality.",
        variants: list[VariantKey] | None = None,
    ) -> tuple[str, str]:
        """Quick factory for critic micro against a given artifact."""
        b = cls()
        return b.build(
            role="critic",
            loop_level="micro",
            context={
                "target_artifact_json": json.dumps(target_artifact, indent=2),
                "architecture_state": architecture_state,
                "focus_areas": focus_areas,
            },
            variants=variants,
        )

    @classmethod
    def for_researcher(
        cls,
        research_question: str,
        tool_results: str,
        architecture_context: str = "General Tinker system.",
        loop_level: LoopLevel = "micro",
    ) -> tuple[str, str]:
        b = cls()
        return b.build(
            role="researcher",
            loop_level=loop_level,
            context={
                "research_question": research_question,
                "tool_results": tool_results,
                "architecture_context": architecture_context,
            },
        )

    @classmethod
    def for_synthesizer_meso(
        cls,
        source_artifacts: list[dict],
        synthesis_directive: str,
        prior_meso_synthesis: str = "None — first meso synthesis.",
    ) -> tuple[str, str]:
        b = cls()
        return b.build(
            role="synthesizer",
            loop_level="meso",
            context={
                "source_artifacts_json": json.dumps(source_artifacts, indent=2),
                "synthesis_directive": synthesis_directive,
                "prior_meso_synthesis": prior_meso_synthesis,
            },
        )


# ---------------------------------------------------------------------------
# Token estimation (rough — for observability/trimming)
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token (GPT/Qwen approximation)."""
    return max(1, len(text) // 4)


def build_context_summary(system: str, user: str) -> dict[str, int]:
    """Return token estimates for a built prompt pair."""
    st = estimate_tokens(system)
    ut = estimate_tokens(user)
    return {
        "system_tokens": st,
        "user_tokens": ut,
        "total_tokens": st + ut,
        "system_chars": len(system),
        "user_chars": len(user),
    }
