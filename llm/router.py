"""
Tinker Model Client — ModelRouter: the main orchestrator for AI completions.

What this file does
-------------------
``ModelRouter`` is the single class that all Tinker agents talk to when they
want a response from an AI model.  It is the "conductor" that coordinates the
other pieces of the ``llm`` package:

  1. Looks up which machine and model to use based on the agent's role.
  2. Appends a "please respond in JSON" instruction when needed.
  3. Trims the conversation if it would exceed the model's context window.
  4. Calls the low-level HTTP client (OllamaClient) to send the request.
  5. Extracts structured JSON from the raw text response.
  6. Wraps everything in a clean ModelResponse object.

Why it exists
-------------
Without the router, every agent would have to know about HTTP clients, token
budgets, retry logic, JSON extraction, and machine routing.  The router keeps
all that complexity in one place so agents can just say "here's my question,
give me an answer" without caring about the plumbing underneath.

Think of the router like a travel agent: you say "I want to go from London to
Tokyo", and the travel agent figures out the flights, connections, baggage
rules, and seat assignments — you just show up.

How it fits into Tinker
-----------------------
Every Tinker agent (Architect, Researcher, Synthesizer, Critic) uses the
router.  The Orchestrator creates one shared router and passes it to all
agents, or each agent creates its own.  Both patterns work because
``ModelRouter`` is stateless between requests.

Usage
-----
    from tinker.llm import ModelRouter, ModelRequest, AgentRole, Message

    router = ModelRouter()           # uses environment-variable defaults
    await router.start()

    response = await router.complete(ModelRequest(
        agent_role  = AgentRole.ARCHITECT,
        messages    = [Message("user", "Design a microservices auth system.")],
        expect_json = True,
    ))
    print(response.structured)   # parsed Python dict from the model's JSON reply

    await router.shutdown()

Or, more conveniently, as an async context manager (auto-starts and shuts down):

    async with ModelRouter() as router:
        response = await router.complete(...)
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .client import OllamaClient
from .context import enforce_context_limit
from .parsing import build_json_instruction, extract_json
from .types import (
    AgentRole,
    Machine,
    MachineConfig,
    Message,
    ModelRequest,
    ModelResponse,
    RetryConfig,
    ROLE_MACHINE_MAP,
)

logger = logging.getLogger(__name__)


class ModelRouter:
    """
    Route ModelRequests to the correct Ollama machine, enforce context limits,
    parse JSON responses, and surface a single `.complete()` method.
    """

    def __init__(
        self,
        server_config:    MachineConfig | None = None,
        secondary_config: MachineConfig | None = None,
        retry_config:     RetryConfig   | None = None,
    ) -> None:
        self._server_cfg    = server_config    or MachineConfig.server_defaults()
        self._secondary_cfg = secondary_config or MachineConfig.secondary_defaults()
        self._retry         = retry_config     or RetryConfig()

        self._clients: dict[Machine, OllamaClient] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise HTTP sessions for both machines."""
        self._clients[Machine.SERVER] = OllamaClient(
            self._server_cfg, self._retry
        )
        self._clients[Machine.SECONDARY] = OllamaClient(
            self._secondary_cfg, self._retry
        )
        logger.info(
            "ModelRouter started — server=%s  secondary=%s",
            self._server_cfg.base_url,
            self._secondary_cfg.base_url,
        )

    async def shutdown(self) -> None:
        """Close all HTTP sessions."""
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
        logger.info("ModelRouter shut down.")

    async def __aenter__(self) -> "ModelRouter":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.shutdown()

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health(self) -> dict[Machine, bool]:
        """Ping both machines; returns {Machine: is_alive}."""
        results: dict[Machine, bool] = {}
        for machine, client in self._clients.items():
            results[machine] = await client.health_check()
        return results

    # ------------------------------------------------------------------
    # Core completion
    # ------------------------------------------------------------------

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """
        Execute a completion request end-to-end.

        Steps
        -----
        1. Resolve machine + model from agent role.
        2. If expect_json, append JSON instruction to the system prompt.
        3. Enforce context-window budget (truncate history if needed).
        4. Call the underlying OllamaClient.
        5. Extract token counts and structured output.
        6. Return a ModelResponse.
        """
        machine = ROLE_MACHINE_MAP[request.agent_role]
        config  = self._config_for(machine)
        client  = self._client_for(machine)

        request.resolved_machine = machine
        request.resolved_model   = config.model

        # --- Inject JSON instruction ---
        messages = list(request.messages)
        if request.expect_json:
            messages = _inject_json_instruction(messages, request.json_schema_hint)

        # --- Context window enforcement ---
        messages = enforce_context_limit(
            messages,
            context_window=config.context_window,
            max_output_tokens=config.max_output_tokens,
        )

        # --- Send ---
        t0 = time.monotonic()
        raw_response = await client.chat(
            messages=messages,
            model=config.model,
            temperature=request.temperature,
            max_tokens=config.max_output_tokens,
        )
        elapsed = time.monotonic() - t0

        # --- Parse response ---
        raw_text, usage, attempts = _unpack_ollama_response(raw_response)

        structured = None
        if request.expect_json:
            structured, strategy = extract_json(raw_text)
            if structured is None:
                logger.warning(
                    "JSON extraction failed for %s response (role=%s). "
                    "Returning raw_text only.",
                    machine.value,
                    request.agent_role.value,
                )
            else:
                logger.debug(
                    "JSON extracted via '%s' for role=%s",
                    strategy,
                    request.agent_role.value,
                )

        return ModelResponse(
            raw_text=raw_text,
            structured=structured,
            model=config.model,
            machine=machine,
            agent_role=request.agent_role,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            attempts=attempts,
            elapsed_seconds=elapsed,
        )

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------

    async def complete_text(
        self,
        role: AgentRole,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.7,
    ) -> ModelResponse:
        """Simple single-turn text completion."""
        messages: list[Message] = []
        if system:
            messages.append(Message("system", system))
        messages.append(Message("user", prompt))

        return await self.complete(
            ModelRequest(
                agent_role=role,
                messages=messages,
                temperature=temperature,
                expect_json=False,
            )
        )

    async def complete_json(
        self,
        role: AgentRole,
        prompt: str,
        system: str | None = None,
        schema_hint: str | None = None,
        temperature: float = 0.3,
    ) -> ModelResponse:
        """Single-turn completion that expects and parses a JSON response."""
        messages: list[Message] = []
        if system:
            messages.append(Message("system", system))
        messages.append(Message("user", prompt))

        return await self.complete(
            ModelRequest(
                agent_role=role,
                messages=messages,
                temperature=temperature,
                expect_json=True,
                json_schema_hint=schema_hint,
            )
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _config_for(self, machine: Machine) -> MachineConfig:
        return self._server_cfg if machine == Machine.SERVER else self._secondary_cfg

    def _client_for(self, machine: Machine) -> OllamaClient:
        if not self._clients:
            raise RuntimeError("ModelRouter not started. Call await router.start() first.")
        return self._clients[machine]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _inject_json_instruction(
    messages: list[Message],
    schema_hint: str | None,
) -> list[Message]:
    """
    Append the JSON instruction to an existing system message, or prepend a new
    system message if none exists.
    """
    instruction = build_json_instruction(schema_hint)
    result      = list(messages)

    if result and result[0].role == "system":
        result[0] = Message(
            role="system",
            content=result[0].content.rstrip() + "\n\n" + instruction,
        )
    else:
        result.insert(0, Message(role="system", content=instruction))

    return result


def _unpack_ollama_response(raw: dict) -> tuple[str, dict, int]:
    """
    Pull the assistant text + usage stats out of an OpenAI-compat response.
    Returns (text, usage_dict, attempt_count).
    """
    try:
        text = raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"Unexpected Ollama response structure: {raw}") from exc

    usage = raw.get("usage", {})
    return text, usage, 1
