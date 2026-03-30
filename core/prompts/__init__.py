"""
tinker.prompts — Prompt system for Tinker autonomous architecture engine.

Public API:

    from core.prompts import PromptBuilder, OutputValidator, validate_output
    from core.prompts import TEMPLATE_REGISTRY, SCHEMA_REGISTRY, VARIANT_REGISTRY
    from core.prompts.examples import get_example_artifact, print_example_exchange

Quick start:

    # Build prompts
    builder = PromptBuilder()
    system, user = builder.build(
        role="architect",
        loop_level="micro",
        context={
            "architecture_state": "...",
            "task_description":   "Design the Memory Manager",
            "constraints":        "No external DB",
            "context":            "Prior loops established event bus",
        },
        variants=["socratic_architect"],
    )

    # Validate model output
    from core.prompts import validate_output
    result = validate_output(raw_model_string, role="architect", loop_level="micro")
    if result.valid:
        artifact = result.data
    else:
        print(result.errors)
"""

from .builder import (
    PromptBuilder,
    PromptBuilderError,
    build_context_summary,
    estimate_tokens,
)
from .schemas import SCHEMA_REGISTRY
from .templates import TEMPLATE_REGISTRY, PromptTemplate
from .validator import OutputValidator, ValidationResult, validate_output
from .variants import (
    VARIANT_REGISTRY,
    PromptVariant,
    get_variant,
    validate_variant_combination,
)

__all__ = [
    # Registries
    "SCHEMA_REGISTRY",
    "TEMPLATE_REGISTRY",
    "VARIANT_REGISTRY",
    # Validator
    "OutputValidator",
    # Builder
    "PromptBuilder",
    "PromptBuilderError",
    # Types
    "PromptTemplate",
    "PromptVariant",
    "ValidationResult",
    "build_context_summary",
    "estimate_tokens",
    # Helpers
    "get_variant",
    "validate_output",
    "validate_variant_combination",
]
