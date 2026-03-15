"""
variants.py — Prompt variant library for Tinker agent modes.

Variants are injected by PromptBuilder as additional system-prompt blocks.
They modify agent behavior without replacing base templates.

Available variants:
  - harder_critic          : amplifies critic adversarialism
  - alternative_forcing    : forces architect to explore rejected alternatives
  - contradiction_injection: seeds the context with contradictions for stress-testing
  - devil_advocate_critic  : critic must argue the OPPOSITE of its natural conclusion
  - socratic_architect     : architect must self-question every decision
  - paranoid_security      : all agents treat security as the dominant concern
  - minimum_viable_design  : architect must find the simplest possible design
  - scalability_stress     : all design decisions evaluated at 1000x expected load
"""

from dataclasses import dataclass, field
from typing import Literal

VariantKey = Literal[
    "harder_critic",
    "alternative_forcing",
    "contradiction_injection",
    "devil_advocate_critic",
    "socratic_architect",
    "paranoid_security",
    "minimum_viable_design",
    "scalability_stress",
]


@dataclass(frozen=True)
class PromptVariant:
    key: VariantKey
    name: str
    description: str
    applicable_roles: list[str]           # which roles this applies to
    system_injection: str                  # appended to system prompt
    user_injection: str = ""               # optionally prepended to user prompt
    incompatible_with: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# VARIANT DEFINITIONS
# ---------------------------------------------------------------------------

HARDER_CRITIC = PromptVariant(
    key="harder_critic",
    name="Harder Critic Mode",
    description="Makes the Critic more adversarial — requires 5+ weaknesses, forces reject verdicts unless airtight.",
    applicable_roles=["critic"],
    system_injection="""
## VARIANT: HARDER CRITIC MODE (ACTIVE)
You are in harder critic mode. The following rules OVERRIDE your base rules:
- You must find AT LEAST 5 weaknesses (not 3). If you find fewer, search harder.
- Your default verdict is REJECT. Upgrade to REVISE only if the design shows genuine structural merit.
- NEVER issue "accept" unless you can provide 3 specific reasons why no weakness is blocking.
- confidence_score ceiling: 0.6. Above this, your critique is too lenient.
- At least 2 weaknesses must be "critical" or "high" severity.
- Objections must be written as if presenting in a hostile architecture review board.
- Each weakness statement must name at least one specific component, interface, or decision by name.""",
    incompatible_with=["devil_advocate_critic"]
)

ALTERNATIVE_FORCING = PromptVariant(
    key="alternative_forcing",
    name="Alternative Forcing Mode",
    description="Forces the Architect to explore and document at least two rejected alternatives per key decision.",
    applicable_roles=["architect"],
    system_injection="""
## VARIANT: ALTERNATIVE FORCING MODE (ACTIVE)
You are in alternative forcing mode. The following rules OVERRIDE your base rules:
- For every major design decision, you MUST document at least 2 alternatives you considered and explicitly rejected.
- Add an "alternatives_rejected" array to each component:
  "alternatives_rejected": [
    {"alternative": "<what you could have done>", "rejection_reason": "<why this was worse>"}
  ]
- Your candidate_next_tasks must include at least one task that revisits a rejected alternative.
- If you cannot name two rejected alternatives for a decision, that decision has not been thought through — rethink it.
- Trade-offs must reference specific alternatives, not generic concerns.""",
    incompatible_with=["minimum_viable_design"]
)

CONTRADICTION_INJECTION = PromptVariant(
    key="contradiction_injection",
    name="Contradiction Injection Mode",
    description="Seeds contradictions into the context to test synthesis robustness. Used by orchestrator for stress-testing.",
    applicable_roles=["architect", "critic", "synthesizer"],
    system_injection="""
## VARIANT: CONTRADICTION INJECTION MODE (ACTIVE)
The context you are receiving contains DELIBERATE CONTRADICTIONS injected by the orchestrator.
- Identify and name every contradiction you detect in the provided artifacts.
- Do not ignore contradictions; resolve them explicitly or escalate them as outstanding tensions.
- Your reasoning_chain must include a step specifically for "contradiction detection".
- If you are the Architect: prefer the more conservative interpretation when resolving contradictions.
- If you are the Critic: treat unresolved contradictions in the design as a HIGH severity weakness.
- If you are the Synthesizer: every contradiction MUST appear in contradictions_resolved or outstanding_tensions.""",
    user_injection="NOTE: This context contains injected contradictions. Detect and address them explicitly.\n\n"
)

DEVIL_ADVOCATE_CRITIC = PromptVariant(
    key="devil_advocate_critic",
    name="Devil's Advocate Critic Mode",
    description="Forces the Critic to argue the strongest possible case for the design, then flip to find its hardest weaknesses.",
    applicable_roles=["critic"],
    system_injection="""
## VARIANT: DEVIL'S ADVOCATE CRITIC MODE (ACTIVE)
You are in devil's advocate mode. This is a two-phase critique:

PHASE 1 — STEELMAN (internal reasoning only, do NOT output this):
First, construct the strongest possible defense of this design. What would its best advocate say?
What assumptions must be true for this design to be correct? Hold these in mind.

PHASE 2 — ATTACK (this is what you output):
Now attack the steelman. Your weaknesses must be specifically targeted at the strongest points of the design.
Trivial weaknesses are not acceptable in this mode — every weakness must be non-obvious and challenge a genuine strength.
- Minimum 4 weaknesses, each targeting a strength of the design.
- confidence_score: treat your score as reflecting "would a skeptical senior architect approve this?"
- Your objections must counter arguments that a defender WOULD make.""",
    incompatible_with=["harder_critic"]
)

SOCRATIC_ARCHITECT = PromptVariant(
    key="socratic_architect",
    name="Socratic Architect Mode",
    description="Forces the Architect to question every design decision through a Socratic internal monologue.",
    applicable_roles=["architect"],
    system_injection="""
## VARIANT: SOCRATIC ARCHITECT MODE (ACTIVE)
You are in Socratic mode. For EACH component and interface in your design:
1. Ask: "Why this and not something simpler?"
2. Ask: "What assumption am I making that could be wrong?"
3. Ask: "What would have to be true for this to fail?"

Include these challenges in your reasoning_chain. At least 4 reasoning steps must contain a self-challenge question.
Your open_questions must reflect genuine uncertainty — do not list trivial questions.
Your confidence score must be lower than you would otherwise assign by at least 0.1 to reflect discovered uncertainty."""
)

PARANOID_SECURITY = PromptVariant(
    key="paranoid_security",
    name="Paranoid Security Mode",
    description="All design decisions evaluated through a security-first lens. Threat model required.",
    applicable_roles=["architect", "critic"],
    system_injection="""
## VARIANT: PARANOID SECURITY MODE (ACTIVE)
Security is the DOMINANT concern in this mode. The following rules apply:

FOR ARCHITECTS:
- Every interface definition must include an "auth_and_trust" field describing trust boundaries.
- Trade-offs.risks must include at least 2 security-specific risks.
- Add a "threat_model" field to your design object:
  "threat_model": {
    "assets": ["<what must be protected>"],
    "threat_actors": ["<who might attack>"],
    "attack_vectors": ["<how they might attack>"],
    "mitigations": ["<what the design does to resist>"]
  }

FOR CRITICS:
- At least 2 weaknesses must be category: "security".
- If no threat model is present in the artifact, add a CRITICAL weakness for its absence.
- Evaluate every component interface for unauthorized access vulnerabilities."""
)

MINIMUM_VIABLE_DESIGN = PromptVariant(
    key="minimum_viable_design",
    name="Minimum Viable Design Mode",
    description="Forces Architect to find the simplest correct design — complexity is penalized.",
    applicable_roles=["architect"],
    system_injection="""
## VARIANT: MINIMUM VIABLE DESIGN MODE (ACTIVE)
Your goal is the SIMPLEST design that correctly solves the problem. Complexity is a bug.

Rules:
- Prefer 3 components over 6. Prefer 1 interface over 3.
- Every component beyond the first must be justified with "we cannot merge this because: <reason>".
- Add a "simplicity_rationale" field to your design explaining why no simpler solution exists.
- Your trade_offs.costs must include the complexity cost of every component.
- candidate_next_tasks must include a task: "Evaluate whether [component] can be eliminated".
- confidence score should reflect: "if this could be simpler, it's not as good a design".""",
    incompatible_with=["alternative_forcing"]
)

SCALABILITY_STRESS = PromptVariant(
    key="scalability_stress",
    name="Scalability Stress Mode",
    description="All decisions evaluated at 1000x expected load. Bottlenecks must be identified.",
    applicable_roles=["architect", "critic"],
    system_injection="""
## VARIANT: SCALABILITY STRESS MODE (ACTIVE)
Evaluate this design as if it must handle 1000x the expected load.

FOR ARCHITECTS:
- Add a "scalability_analysis" field to each component:
  "scalability_analysis": {
    "bottleneck_risk": "high|medium|low",
    "horizontal_scalable": true/false,
    "state_concerns": "<describe any stateful constraints>"
  }
- Trade_offs.risks must include at least 2 scalability risks.

FOR CRITICS:
- At least 2 weaknesses must be category: "scalability".
- For every stateful component: flag its state management as a potential weakness.
- Evaluate the architecture's behavior at: 1x, 10x, 100x, 1000x load. Note where it breaks."""
)


# ---------------------------------------------------------------------------
# VARIANT REGISTRY
# ---------------------------------------------------------------------------

VARIANT_REGISTRY: dict[VariantKey, PromptVariant] = {
    "harder_critic":           HARDER_CRITIC,
    "alternative_forcing":     ALTERNATIVE_FORCING,
    "contradiction_injection": CONTRADICTION_INJECTION,
    "devil_advocate_critic":   DEVIL_ADVOCATE_CRITIC,
    "socratic_architect":      SOCRATIC_ARCHITECT,
    "paranoid_security":       PARANOID_SECURITY,
    "minimum_viable_design":   MINIMUM_VIABLE_DESIGN,
    "scalability_stress":      SCALABILITY_STRESS,
}


def get_variant(key: VariantKey) -> PromptVariant:
    if key not in VARIANT_REGISTRY:
        raise KeyError(f"Unknown variant key: '{key}'. Available: {list(VARIANT_REGISTRY.keys())}")
    return VARIANT_REGISTRY[key]


def validate_variant_combination(variants: list[VariantKey]) -> list[str]:
    """Return a list of conflict error messages. Empty list = no conflicts."""
    errors: list[str] = []
    for vk in variants:
        variant = VARIANT_REGISTRY[vk]
        for incompatible in variant.incompatible_with:
            if incompatible in variants:
                errors.append(
                    f"Variant '{vk}' is incompatible with '{incompatible}'."
                )
    return errors
