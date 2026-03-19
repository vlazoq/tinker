"""
context/prompt_builder_adapter.py
===================================
Adapter that connects the real ``PromptBuilder`` (prompts/builder.py) to
the ``_PromptBuilderProtocol`` interface that ``ContextAssembler`` expects.

Architecture gap
----------------
``ContextAssembler`` was designed against a simple three-method protocol::

    build_system_identity(role)   → str
    build_output_format(role, n)  → str
    render_template(name, **ctx)  → str

The production ``PromptBuilder`` exposes a single ``build(role, loop_level,
context)`` method that returns a ``(system, user)`` tuple and requires all
context keys to be present before it can be called.

This adapter bridges the two by:

1. **``build_system_identity``** — reads ``PromptTemplate.system`` directly
   from ``TEMPLATE_REGISTRY`` (no context needed; the system prompt is static
   per role).

2. **``build_output_format``** — returns ``""`` because the output format
   instructions are already embedded in the system prompt from step 1.
   ``ContextAssembler`` treats an empty output_format section gracefully.

3. **``render_template``** — delegates to ``PromptBuilder.build()`` using
   the kwargs as the context dict; this is the full production code path
   used when the orchestrator has a complete context available.

All methods fall back gracefully: if the template registry or the builder
raise (e.g. a missing key), a minimal placeholder string is returned so
the assembler can always continue.

Usage
-----
::

    from context.prompt_builder_adapter import PromptBuilderAdapter
    from context.assembler import ContextAssembler

    assembler = ContextAssembler(
        memory_manager = ...,
        prompt_builder = PromptBuilderAdapter(),
    )
"""

from __future__ import annotations

import logging
from typing import Any

from .assembler import AgentRole, _PromptBuilderProtocol

logger = logging.getLogger(__name__)

# Lazy import: only pulled when needed so this module can be imported even
# if ``prompts`` has not been fully initialised yet.
_builder_cls = None
_tmpl_registry = None


def _get_template_registry():
    global _tmpl_registry
    if _tmpl_registry is None:
        try:
            from prompts.templates import TEMPLATE_REGISTRY

            _tmpl_registry = TEMPLATE_REGISTRY
        except Exception as exc:
            logger.warning(
                "PromptBuilderAdapter: could not load TEMPLATE_REGISTRY: %s", exc
            )
            _tmpl_registry = {}
    return _tmpl_registry


def _get_builder():
    global _builder_cls
    if _builder_cls is None:
        try:
            from prompts.builder import PromptBuilder

            _builder_cls = PromptBuilder
        except Exception as exc:
            logger.warning(
                "PromptBuilderAdapter: could not import PromptBuilder: %s", exc
            )
    return _builder_cls


# Map AgentRole enum → loop-level string for TEMPLATE_REGISTRY lookup
_ROLE_DEFAULT_LEVEL: dict[AgentRole, str] = {
    AgentRole.ARCHITECT: "micro",
    AgentRole.CRITIC: "micro",
    AgentRole.RESEARCHER: "micro",
    AgentRole.SYNTHESIZER: "meso",
}

# Map int loop_level → LoopLevel string for PromptBuilder
_INT_TO_LEVEL: dict[int, str] = {0: "micro", 1: "meso", 2: "macro"}


class PromptBuilderAdapter(_PromptBuilderProtocol):
    """
    Production adapter: wraps ``PromptBuilder`` to implement
    ``_PromptBuilderProtocol``.

    Parameters
    ----------
    add_build_metadata : bool
        Passed to ``PromptBuilder.__init__``.  Defaults to ``False`` so the
        build-ID stamp is not injected into contexts assembled during normal
        operation (it would waste token budget).
    """

    def __init__(self, add_build_metadata: bool = False) -> None:
        self._add_meta = add_build_metadata
        self._pb = None  # lazily initialised on first use

    def _prompt_builder(self):
        if self._pb is None:
            cls = _get_builder()
            if cls is not None:
                self._pb = cls(add_build_metadata=self._add_meta)
        return self._pb

    # ── _PromptBuilderProtocol ────────────────────────────────────────────────

    def build_system_identity(self, role: AgentRole) -> str:
        """
        Return the static system prompt for *role*.

        Reads ``PromptTemplate.system`` directly from ``TEMPLATE_REGISTRY``
        (using the role's default loop level).  This is the production
        system identity — the same text that ``PromptBuilder.build()`` would
        embed in the system prompt.

        Falls back to a minimal placeholder if the template is not found.
        """
        level = _ROLE_DEFAULT_LEVEL.get(role, "micro")
        key = f"{role.value}.{level}"
        tmpl = _get_template_registry().get(key)
        if tmpl is not None:
            return tmpl.system
        logger.debug(
            "PromptBuilderAdapter: no template for key %r — using fallback", key
        )
        return f"You are Tinker's {role.value} agent."

    def build_output_format(self, role: AgentRole, loop_level: int) -> str:
        """
        Return the output format instructions for *role* at *loop_level*.

        The production templates embed format instructions inside the system
        prompt itself (as an OUTPUT SCHEMA section), so there is nothing
        additional to add here — returning ``""`` is correct.

        ``ContextAssembler`` drops empty sections gracefully, so this never
        introduces a blank section into the assembled prompt.
        """
        return ""  # format is already in the system prompt (build_system_identity)

    def render_template(self, template_name: str, **kwargs: Any) -> str:
        """
        Render a named template with the provided context variables.

        Delegates to ``PromptBuilder.build(role, loop_level, context)``
        using *template_name* as the ``{role}.{loop_level}`` key.

        Returns a combined ``"[SYSTEM]\\n---\\n[USER]"`` string so the
        section can be embedded in the assembled prompt as a single block.

        Falls back to a minimal ``"[template_name] {kwargs}"`` string if
        the builder raises (missing template, incomplete context, etc.).
        """
        parts = template_name.split(".", 1)
        if len(parts) != 2:
            return f"[{template_name}] {kwargs}"

        role_str, level_str = parts
        pb = self._prompt_builder()
        if pb is None:
            return f"[{template_name}] {kwargs}"

        try:
            system, user = pb.build(role_str, level_str, kwargs)
            return f"{system}\n---\n{user}"
        except Exception as exc:
            logger.warning(
                "PromptBuilderAdapter.render_template(%r): %s — using fallback",
                template_name,
                exc,
            )
            return f"[{template_name}] {kwargs}"
