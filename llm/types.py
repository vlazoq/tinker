"""
Tinker Model Client — Type definitions and configuration.

What this file does
-------------------
This file defines all the "blueprints" (data types) that the rest of the
``llm`` package uses to describe agents, machines, messages, requests,
and responses.  Nothing in here does any real work — it just describes the
shape of data.

Why it exists
-------------
Keeping all data types in one file means:
- You can find any type definition in one place.
- Every other module imports from here, so there are no circular imports.
- Changing a field name or type only requires editing one file.

Think of this file like a set of standardised forms.  Before you send a
request to the model, you fill in a "ModelRequest form".  When the answer
comes back, it arrives in a "ModelResponse form".  Both forms are defined here.

How it fits into Tinker
-----------------------
Almost every other module in the ``llm`` package imports from this file.
Callers (agents, orchestrators) also import ``AgentRole``, ``Message``, and
``ModelRequest`` from here when they want to talk to the model.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Agent roles
# ---------------------------------------------------------------------------


class AgentRole(str, Enum):
    """
    The four AI agents that make up Tinker's thinking engine.

    Each agent has a distinct personality and purpose:
    - ARCHITECT   : proposes software architecture designs
    - RESEARCHER  : gathers background knowledge and context
    - SYNTHESIZER : combines findings into a coherent picture
    - CRITIC      : challenges proposals and finds weaknesses

    This is an Enum (a fixed list of named values), so you write
    ``AgentRole.ARCHITECT`` instead of the string ``"architect"``.
    This prevents typos and makes IDEs show you the valid choices.

    It also inherits from ``str`` so that an AgentRole value is directly
    usable wherever a plain string is expected (e.g. when logging).
    """

    ARCHITECT = "architect"
    RESEARCHER = "researcher"
    SYNTHESIZER = "synthesizer"
    CRITIC = "critic"


# ---------------------------------------------------------------------------
# Machine targets
# ---------------------------------------------------------------------------


class Machine(str, Enum):
    """
    The physical machines (computers) that run Ollama.

    Tinker uses two separate machines so that heavy "thinking" models and
    lighter "reviewing" models do not compete for the same GPU memory.

    - SERVER    : a more powerful machine (e.g. i7-7700 + RTX 3090) that
                  runs larger 7-billion-parameter models.  Used for the
                  Architect, Researcher, and Synthesizer agents.
    - SECONDARY : a lighter machine that runs smaller 2-3B models.  Used
                  for the Critic, which needs to be quick but not as deep.
    """

    SERVER = "server"  # i7-7700 / RTX 3090  — 7B models
    SECONDARY = "secondary"  # lighter machine      — 2-3B models


# Routing table: which machine handles each role.
# Think of this as a phone directory — given an agent role, look up which
# machine you should call.
ROLE_MACHINE_MAP: dict[AgentRole, Machine] = {
    AgentRole.ARCHITECT: Machine.SERVER,  # needs the biggest model
    AgentRole.RESEARCHER: Machine.SERVER,  # needs depth
    AgentRole.SYNTHESIZER: Machine.SERVER,  # needs breadth
    AgentRole.CRITIC: Machine.SECONDARY,  # needs speed, not raw power
}


# ---------------------------------------------------------------------------
# Model names (override via environment or MachineConfig)
# ---------------------------------------------------------------------------

# Default AI model identifiers in Ollama's naming format ("name:size").
# You can override these with environment variables at runtime (see MachineConfig).
DEFAULT_SERVER_MODEL = "qwen3:7b"  # 7-billion-parameter model for the main server
DEFAULT_SECONDARY_MODEL = "phi3:mini"  # smaller, faster model for the secondary machine


# ---------------------------------------------------------------------------
# Machine-level configuration
# ---------------------------------------------------------------------------


@dataclass
class MachineConfig:
    """
    All the connection and capability settings for one Ollama server.

    Think of this as a settings sheet for a single machine.  It tells the
    client where to find the server (``base_url``), which AI model to use,
    how many "words" the model can handle at once (``context_window``),
    and when to give up waiting (the timeout values).

    Fields
    ------
    base_url          : The HTTP address of the Ollama server, e.g.
                        ``http://localhost:11434``.
    model             : The Ollama model identifier, e.g. ``"qwen3:7b"``.
    context_window    : Maximum number of tokens (roughly: word-pieces) the
                        model can read in one request.  Longer conversations
                        must be trimmed to fit.  Defaults: 8192 for the server
                        (override via TINKER_SERVER_CTX), 4096 for the secondary
                        (override via TINKER_SECONDARY_CTX).
    max_output_tokens : Maximum length of the model's reply.  Default: 2048.
    request_timeout   : How many seconds to wait for a complete reply before
                        giving up.  Default: 120 seconds (2 minutes).
    connect_timeout   : How many seconds to wait just to establish the network
                        connection.  Default: 10 seconds.

    The two class-methods (``server_defaults`` and ``secondary_defaults``)
    build a ready-to-use config from environment variables, with sensible
    fallback values if the environment variables are not set.
    """

    base_url: str
    model: str
    context_window: int = 8192  # tokens the model can accept in one call
    max_output_tokens: int = 2048  # max tokens the model will generate
    request_timeout: float = 120.0  # seconds before a slow reply is abandoned
    connect_timeout: float = 10.0  # seconds to wait for the TCP handshake

    @classmethod
    def server_defaults(cls) -> "MachineConfig":
        """
        Build a MachineConfig for the main server using environment variables.

        Environment variables (with their defaults if not set):
          TINKER_SERVER_URL     → http://localhost:11434
          TINKER_SERVER_MODEL   → qwen3:7b
          TINKER_SERVER_CTX     → 8192
          TINKER_SERVER_MAX_OUT → 2048
          TINKER_SERVER_TIMEOUT → 120

        Using environment variables means you can change the server address
        or model without editing code — just set the variable before running.
        """
        return cls(
            base_url=os.getenv("TINKER_SERVER_URL", "http://localhost:11434"),
            model=os.getenv("TINKER_SERVER_MODEL", DEFAULT_SERVER_MODEL),
            context_window=int(os.getenv("TINKER_SERVER_CTX", "8192")),
            max_output_tokens=int(os.getenv("TINKER_SERVER_MAX_OUT", "2048")),
            request_timeout=float(os.getenv("TINKER_SERVER_TIMEOUT", "120")),
        )

    @classmethod
    def secondary_defaults(cls) -> "MachineConfig":
        """
        Build a MachineConfig for the secondary (lighter) machine.

        Environment variables (with their defaults if not set):
          TINKER_SECONDARY_URL     → http://secondary:11434
          TINKER_SECONDARY_MODEL   → phi3:mini
          TINKER_SECONDARY_CTX     → 4096  (smaller model = smaller window)
          TINKER_SECONDARY_MAX_OUT → 1024
          TINKER_SECONDARY_TIMEOUT → 60
        """
        return cls(
            base_url=os.getenv("TINKER_SECONDARY_URL", "http://secondary:11434"),
            model=os.getenv("TINKER_SECONDARY_MODEL", DEFAULT_SECONDARY_MODEL),
            context_window=int(os.getenv("TINKER_SECONDARY_CTX", "4096")),
            max_output_tokens=int(os.getenv("TINKER_SECONDARY_MAX_OUT", "1024")),
            request_timeout=float(os.getenv("TINKER_SECONDARY_TIMEOUT", "60")),
        )


# ---------------------------------------------------------------------------
# Request / Response objects
# ---------------------------------------------------------------------------


@dataclass
class Message:
    """
    A single message in a conversation with the AI model.

    AI chat models work like a text-message thread: you send a series of
    messages, each labelled with who wrote it, and the model replies.

    Fields
    ------
    role    : Who sent this message.  One of:
              - ``"system"``    : background instructions given before the
                                  conversation starts (e.g. "You are an
                                  expert architect").
              - ``"user"``      : the human's (or agent's) turn.
              - ``"assistant"`` : the model's previous reply (used when
                                  continuing a multi-turn conversation).
    content : The actual text of the message.
    """

    role: str  # "system" | "user" | "assistant"
    content: str

    def to_dict(self) -> dict[str, str]:
        """
        Convert this message to a plain Python dictionary.

        Ollama's HTTP API expects messages as simple dicts like
        ``{"role": "user", "content": "Hello!"}``.  This method produces
        exactly that, so the client can pass a list of these dicts in the
        JSON request body.
        """
        return {"role": self.role, "content": self.content}


@dataclass
class ModelRequest:
    """
    Everything needed to send one request to the AI model.

    Think of this as filling out a form before pressing "send".  You specify:
    which agent is asking (``agent_role``), the conversation so far
    (``messages``), how creative the answer should be (``temperature``),
    and whether you want the response to be structured JSON.

    The router fills in ``resolved_model`` and ``resolved_machine``
    automatically just before sending, so you don't need to set those.

    Fields
    ------
    agent_role        : Which Tinker agent is making this request.  The router
                        uses this to pick the right machine and model.
    messages          : The full conversation history, as a list of Message
                        objects (system instruction + user turn(s) + any
                        previous assistant replies).
    temperature       : Controls randomness.  0.0 = very deterministic and
                        predictable; 1.0 = more creative and varied.
                        Default is 0.7 (a good general-purpose balance).
    expect_json       : If True, the router will add extra instructions
                        telling the model to respond in JSON, and will try
                        to parse the reply.  Default: False.
    json_schema_hint  : An optional plain-English or pseudo-schema description
                        of the JSON structure you want.  Appended to the JSON
                        instruction so the model knows what fields to include.
    resolved_model    : Filled in by the router; the exact model name sent to
                        Ollama (e.g. ``"qwen3:7b"``).
    resolved_machine  : Filled in by the router; which Machine was used.
    """

    agent_role: AgentRole
    messages: list[Message]
    temperature: float = 0.7
    expect_json: bool = False  # If True, extract/validate JSON from reply
    json_schema_hint: str | None = None  # Optional schema description for the prompt

    # Populated by the router before sending — callers don't set these
    resolved_model: str = ""
    resolved_machine: Machine = Machine.SERVER


@dataclass
class ModelResponse:
    """
    Everything that came back from the AI model after a request.

    After the router processes a request it wraps the result in this object.
    You get both the raw text (useful for debugging) and the parsed Python
    object if you asked for JSON.  You also get timing and token-usage data.

    Fields
    ------
    raw_text          : The model's reply exactly as it came back, before any
                        parsing.  Always present.
    structured        : The parsed Python dict or list if ``expect_json`` was
                        True and parsing succeeded; otherwise ``None``.
    model             : The Ollama model name that produced the reply.
    machine           : Which Machine handled the request.
    agent_role        : Which agent role this response is for.
    prompt_tokens     : Number of tokens in the request (input cost).
    completion_tokens : Number of tokens in the reply (output cost).
    total_tokens      : prompt_tokens + completion_tokens.
    attempts          : How many HTTP attempts were made (1 = no retries needed).
    elapsed_seconds   : Wall-clock time for the whole request, in seconds.
    """

    raw_text: str
    structured: dict[str, Any] | list[Any] | None
    model: str
    machine: Machine
    agent_role: AgentRole
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    attempts: int = 1
    elapsed_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        """
        True if the model returned any text at all.

        A simple sanity check — if ``raw_text`` is an empty string, something
        went wrong upstream (the model returned nothing).
        """
        return bool(self.raw_text)

    @property
    def json(self) -> dict[str, Any] | list[Any]:
        """
        Return the parsed JSON response, raising an error if it isn't available.

        Use this when you know you asked for JSON (``expect_json=True``) and
        you want to access the result directly.  If parsing failed,
        ``structured`` will be ``None`` and this property raises ``ValueError``
        with a clear message rather than silently returning ``None``.

        Raises
        ------
        ValueError : if the response did not contain parseable JSON.
        """
        if self.structured is None:
            raise ValueError("Response did not contain parseable JSON.")
        return self.structured


# ---------------------------------------------------------------------------
# Retry config
# ---------------------------------------------------------------------------


@dataclass
class RetryConfig:
    """
    Settings that control how the client retries failed HTTP requests.

    Networks and AI servers are unreliable — a request might fail due to a
    momentary overload, a dropped connection, or a server restart.  Rather
    than crashing immediately, the client will try a few times with
    increasing pauses in between (called "exponential backoff").

    Analogy: imagine calling a busy restaurant.  If the line is busy, you
    wait a bit, try again, wait a little longer, try again — you don't just
    give up after one busy signal.

    Fields
    ------
    max_attempts          : Total number of tries (including the first one).
                            Default: 3 (so up to 2 retries).
    base_delay            : Seconds to wait before the first retry.  Default: 2.
    max_delay             : The wait time will grow but never exceed this.
                            Default: 30 seconds.
    backoff_factor        : Each wait is multiplied by this.  With factor=2
                            the waits are 2s, 4s, 8s, 16s, … up to max_delay.
    retryable_status_codes: HTTP status codes that are worth retrying.
                            429 = "too many requests" (rate limit).
                            5xx = server-side errors (server may recover).
                            Client errors (4xx except 429) are NOT retried
                            because they mean the request itself is wrong.
    """

    max_attempts: int = 3
    base_delay: float = 2.0  # seconds before first retry
    max_delay: float = 30.0  # cap on how long we wait between retries
    backoff_factor: float = 2.0  # multiplier applied to delay after each retry
    retryable_status_codes: set[int] = field(
        # 429 = rate limit; 500/502/503/504 = server-side errors worth retrying
        default_factory=lambda: {429, 500, 502, 503, 504}
    )
