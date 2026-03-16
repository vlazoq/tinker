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
    The main entry point for sending AI completion requests in Tinker.

    The router's job is to hide all the complexity of the ``llm`` package
    behind a single clean method: ``complete()``.  Callers don't need to
    know which machine to use, how to trim long conversations, or how to
    coax JSON out of a model's reply — the router handles all of that.

    Analogy: the router is like a restaurant manager.  You (the agent) just
    say "I'd like the architecture proposal, please."  The manager (router)
    figures out which chef (machine/model) to assign, makes sure the order
    is the right size (context limit), and brings back the plated dish
    (structured ModelResponse).

    The router manages two ``OllamaClient`` instances (one per machine) and
    is responsible for their lifecycle: creating them on ``start()`` and
    closing them on ``shutdown()``.

    Parameters
    ----------
    server_config    : Settings for the main (SERVER) machine.  Defaults to
                       ``MachineConfig.server_defaults()`` which reads from
                       environment variables.
    secondary_config : Settings for the lighter (SECONDARY) machine.
    retry_config     : How many times to retry failed requests.  Defaults to
                       ``RetryConfig()`` (3 attempts, exponential back-off).
    """

    def __init__(
        self,
        server_config:    MachineConfig | None = None,
        secondary_config: MachineConfig | None = None,
        retry_config:     RetryConfig   | None = None,
    ) -> None:
        # Use provided configs or fall back to environment-variable defaults
        self._server_cfg    = server_config    or MachineConfig.server_defaults()
        self._secondary_cfg = secondary_config or MachineConfig.secondary_defaults()
        self._retry         = retry_config     or RetryConfig()

        # Will hold {Machine.SERVER: OllamaClient, Machine.SECONDARY: OllamaClient}
        # after start() is called.
        self._clients: dict[Machine, OllamaClient] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """
        Create HTTP client sessions for both machines.

        Must be called before ``complete()``.  The ``async with ModelRouter()``
        pattern calls this automatically so you don't forget.
        """
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
        """
        Close all HTTP sessions and free network resources.

        After calling this, ``complete()`` will raise an error until
        ``start()`` is called again.  The ``async with`` pattern handles
        this automatically.
        """
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
        logger.info("ModelRouter shut down.")

    async def __aenter__(self) -> "ModelRouter":
        """Called at the start of an ``async with ModelRouter() as router:`` block."""
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        """Called at the end of an ``async with`` block — shuts down automatically."""
        await self.shutdown()

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health(self) -> dict[Machine, bool]:
        """
        Check whether each machine's Ollama server is reachable.

        Returns a dict like ``{Machine.SERVER: True, Machine.SECONDARY: False}``.
        Useful for monitoring dashboards or pre-flight checks before a long run.
        """
        results: dict[Machine, bool] = {}
        for machine, client in self._clients.items():
            results[machine] = await client.health_check()
        return results

    # ------------------------------------------------------------------
    # Core completion
    # ------------------------------------------------------------------

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """
        Execute one AI completion request from start to finish.

        This is the heart of the router.  Here is what happens, step by step:

        Step 1 — Routing
            Look up the agent role in ``ROLE_MACHINE_MAP`` to find which
            machine (SERVER or SECONDARY) should handle this request.

        Step 2 — JSON instruction injection (optional)
            If ``request.expect_json`` is True, add a clear instruction to
            the system prompt telling the model to respond with JSON only.

        Step 3 — Context window enforcement
            Count the tokens in the message list.  If it's too long for the
            model, drop old messages from the middle until it fits.

        Step 4 — Send
            Call the appropriate OllamaClient's ``chat()`` method.  This
            actually makes the HTTP request and handles retries.

        Step 5 — Parse
            Extract the assistant's text from the raw Ollama JSON response.
            If JSON was expected, run the extraction strategies from
            parsing.py to pull out the structured data.

        Step 6 — Return
            Wrap everything in a ModelResponse and return it.

        Parameters
        ----------
        request : A fully populated ModelRequest object.

        Returns
        -------
        ModelResponse : Contains raw text, optional parsed JSON, token counts,
                        timing, and which machine/model was used.
        """
        # Step 1: figure out which machine and config to use for this role
        machine = ROLE_MACHINE_MAP[request.agent_role]
        config  = self._config_for(machine)
        client  = self._client_for(machine)

        # Record the resolved machine/model back into the request (useful for logging)
        request.resolved_machine = machine
        request.resolved_model   = config.model

        # Step 2: append the "respond in JSON" instruction to the system prompt
        messages = list(request.messages)
        if request.expect_json:
            messages = _inject_json_instruction(messages, request.json_schema_hint)

        # Step 3: trim the conversation if it exceeds the model's context window
        messages = enforce_context_limit(
            messages,
            context_window=config.context_window,
            max_output_tokens=config.max_output_tokens,
        )

        # Step 4: send the request and measure how long it takes
        t0 = time.monotonic()
        raw_response = await client.chat(
            messages=messages,
            model=config.model,
            temperature=request.temperature,
            max_tokens=config.max_output_tokens,
        )
        elapsed = time.monotonic() - t0

        # Step 5: unpack the raw Ollama response JSON into (text, usage, attempts)
        raw_text, usage, attempts = _unpack_ollama_response(raw_response)

        structured = None
        if request.expect_json:
            # Try to extract structured JSON from the model's text reply
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

        # Step 6: build and return the response object
        return ModelResponse(
            raw_text=raw_text,
            structured=structured,
            model=config.model,
            machine=machine,
            agent_role=request.agent_role,
            # usage dict comes from Ollama's response; default to 0 if missing
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
        """
        Shortcut for a simple single-turn text completion (no JSON parsing).

        Use this when you want a plain prose answer from the model, not
        structured data.

        Parameters
        ----------
        role        : Which agent role is asking (determines which machine is used).
        prompt      : The user's question or instruction.
        system      : Optional system message (agent instructions).  If omitted,
                      no system message is sent.
        temperature : Creativity (0.0 = predictable, 1.0 = creative).

        Returns
        -------
        ModelResponse : ``response.raw_text`` contains the model's answer.
        """
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
        """
        Shortcut for a single-turn completion that expects a JSON response.

        The temperature defaults to 0.3 (lower than text completion) because
        structured data extraction benefits from the model being less creative
        and more predictable.

        Parameters
        ----------
        role        : Which agent role is asking.
        prompt      : The user's question or instruction.
        system      : Optional system message.
        schema_hint : Optional description of the expected JSON shape.  Helps
                      the model produce the right fields.
        temperature : Defaults to 0.3 for more reliable structured output.

        Returns
        -------
        ModelResponse : ``response.json`` returns the parsed dict/list, or
                        raises ValueError if parsing failed.
        """
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
    # Private helpers
    # ------------------------------------------------------------------

    def _config_for(self, machine: Machine) -> MachineConfig:
        """
        Return the MachineConfig for the given machine.

        Parameters
        ----------
        machine : Machine.SERVER or Machine.SECONDARY.

        Returns
        -------
        MachineConfig for that machine.
        """
        return self._server_cfg if machine == Machine.SERVER else self._secondary_cfg

    def _client_for(self, machine: Machine) -> OllamaClient:
        """
        Return the live OllamaClient for the given machine.

        Raises RuntimeError if ``start()`` has not been called yet, because
        there won't be any clients in ``self._clients``.

        Parameters
        ----------
        machine : Machine.SERVER or Machine.SECONDARY.

        Returns
        -------
        OllamaClient for that machine.
        """
        if not self._clients:
            raise RuntimeError("ModelRouter not started. Call await router.start() first.")
        return self._clients[machine]


# ---------------------------------------------------------------------------
# Module-level helper functions
# ---------------------------------------------------------------------------

def _inject_json_instruction(
    messages: list[Message],
    schema_hint: str | None,
) -> list[Message]:
    """
    Add a "respond with JSON" instruction to the message list.

    If there is already a system message at position 0, the instruction is
    appended to it (so we don't lose the original system instructions).
    If there is no system message, a new one is inserted at the front.

    We prefer appending to the existing system message because the model
    reads system instructions in order; adding the JSON instruction at the
    end of the system prompt makes it feel like the *most recent* constraint,
    which typically results in better compliance.

    Parameters
    ----------
    messages    : The original list of Message objects.
    schema_hint : Optional schema description forwarded to build_json_instruction.

    Returns
    -------
    list[Message] : A new list with the JSON instruction added.  The original
                    list is not modified.
    """
    instruction = build_json_instruction(schema_hint)
    result      = list(messages)  # make a copy so we don't mutate the caller's list

    if result and result[0].role == "system":
        # Append to the existing system message, separated by a blank line
        result[0] = Message(
            role="system",
            content=result[0].content.rstrip() + "\n\n" + instruction,
        )
    else:
        # No system message exists — create one with just the JSON instruction
        result.insert(0, Message(role="system", content=instruction))

    return result


def _unpack_ollama_response(raw: dict) -> tuple[str, dict, int]:
    """
    Extract the assistant's reply text and usage stats from Ollama's raw response.

    Ollama follows the OpenAI chat-completions response format, which looks like:

        {
          "choices": [
            {
              "message": {"role": "assistant", "content": "The model's reply..."},
              ...
            }
          ],
          "usage": {"prompt_tokens": 42, "completion_tokens": 128, "total_tokens": 170}
        }

    This function navigates that structure and returns the three things the
    router needs: the text, the usage dict, and the attempt count.

    Parameters
    ----------
    raw : The dict returned by ``OllamaClient.chat()`` (already JSON-parsed).

    Returns
    -------
    (text, usage_dict, attempt_count)
        text         : The model's reply as a plain string.
        usage_dict   : Token usage counts (may be empty if Ollama didn't report).
        attempt_count: Always 1 here; the OllamaClient handles retry counting.

    Raises
    ------
    ValueError : If the response doesn't match the expected structure (e.g.
                 Ollama returned something unexpected).
    """
    try:
        # Navigate: response -> choices[0] -> message -> content
        text = raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"Unexpected Ollama response structure: {raw}") from exc

    # usage is optional — older Ollama versions don't always include it
    usage = raw.get("usage", {})
    return text, usage, 1
