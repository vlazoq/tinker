"""
tinker.prompts — Prompt system for Tinker autonomous architecture engine.

Public API:

    from prompts import PromptBuilder, OutputValidator, validate_output
    from prompts import TEMPLATE_REGISTRY, SCHEMA_REGISTRY, VARIANT_REGISTRY
    from prompts.examples import get_example_artifact, print_example_exchange

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
    from prompts import validate_output
    result = validate_output(raw_model_string, role="architect", loop_level="micro")
    if result.valid:
        artifact = result.data
    else:
        print(result.errors)
"""

from .builder   import PromptBuilder, PromptBuilderError, build_context_summary, estimate_tokens
from .validator import OutputValidator, ValidationResult, validate_output
from .schemas   import SCHEMA_REGISTRY
from .templates import TEMPLATE_REGISTRY, PromptTemplate
from .variants  import VARIANT_REGISTRY, PromptVariant, get_variant, validate_variant_combination

__all__ = [
    # Builder
    "PromptBuilder",
    "PromptBuilderError",
    "build_context_summary",
    "estimate_tokens",
    # Validator
    "OutputValidator",
    "ValidationResult",
    "validate_output",
    # Registries
    "SCHEMA_REGISTRY",
    "TEMPLATE_REGISTRY",
    "VARIANT_REGISTRY",
    # Types
    "PromptTemplate",
    "PromptVariant",
    # Helpers
    "get_variant",
    "validate_variant_combination",
]
