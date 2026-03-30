"""
Tinker — llm package
=====================

What this file does
-------------------
This is the "front door" for the entire ``llm`` package.  When any other part
of Tinker writes ``from tinker.llm import ModelRouter``, Python executes this
file first.  Its only job is to re-export the things other code actually needs,
so callers never have to know which sub-module an object lives in.

Why it exists
-------------
Without this file, callers would have to write long imports like:
    from tinker.llm.router import ModelRouter
    from tinker.llm.types  import AgentRole, Message

With it, they can just write:
    from tinker.llm import ModelRouter, AgentRole, Message

How it fits into Tinker
-----------------------
The ``llm`` package is the only part of Tinker that talks to the AI models
(via Ollama, a locally-running AI model server).  Every agent — Architect,
Critic, Researcher, Synthesizer — sends its prompts through this package and
gets back text or structured JSON responses.

Think of this file as the reception desk of a building: you don't need to know
which office everyone sits in; you just ask reception and they point you to the
right place.

Quick start
-----------
    from tinker.llm import ModelRouter, ModelRequest, AgentRole, Message

    async with ModelRouter() as router:
        resp = await router.complete_json(
            role       = AgentRole.ARCHITECT,
            prompt     = "Propose a service mesh architecture for 12 microservices.",
            schema_hint= '{"name": str, "services": [...], "rationale": str}',
        )
        print(resp.json)   # a parsed Python dict

Sub-modules in this package
---------------------------
- types.py   : Data classes and enums (AgentRole, Message, ModelRequest, …)
- client.py  : Low-level HTTP client that actually calls Ollama
- context.py : Keeps messages within the model's token (word-count) limit
- parsing.py : Extracts JSON from the model's response even when it's messy
- router.py  : High-level entry point that orchestrates the above four pieces
"""

# ---------------------------------------------------------------------------
# Imports — pulling names from sub-modules into this package's namespace
# ---------------------------------------------------------------------------

# The HTTP client and the errors it can raise
from .client import ConnectionError, ModelClientError, OllamaClient, TimeoutError

# JSON extraction helpers (used by router, but useful to callers too)
from .parsing import build_json_instruction, extract_json

# Vendor-agnostic provider abstraction (Ollama, OpenAI, LM Studio, vLLM)
from .providers import PROVIDER_PRESETS, ProviderConfig, build_client

# The main entry-point most callers will use
from .router import ModelRouter

# All data-types that callers need when building requests or reading responses
from .types import (
    ROLE_MACHINE_MAP,  # lookup table: agent role → server machine
    AgentRole,  # which AI agent is asking (ARCHITECT, CRITIC, …)
    Machine,  # which physical server to send the request to
    MachineConfig,  # connection settings for one server
    Message,  # a single chat message (role + content)
    ModelRequest,  # a fully-described request to the model
    ModelResponse,  # everything that came back from the model
    RetryConfig,  # settings for how to retry failed requests
)

# ---------------------------------------------------------------------------
# __all__ — the official public surface of this package
# ---------------------------------------------------------------------------
# Anything listed here shows up when someone writes ``from tinker.llm import *``
# and also tells linters / IDEs that these names are intentionally exported.

__all__ = [
    "PROVIDER_PRESETS",  # known provider presets (ollama, openai, lm_studio, …)
    "ROLE_MACHINE_MAP",  # dict mapping each AgentRole to its Machine
    "AgentRole",  # enum: ARCHITECT | RESEARCHER | SYNTHESIZER | CRITIC
    "ConnectionError",  # raised when Ollama is unreachable
    "Machine",  # enum: SERVER | SECONDARY
    "MachineConfig",  # dataclass: URL, model name, timeouts, …
    "Message",  # dataclass: role + content text
    "ModelClientError",  # base exception class
    "ModelRequest",  # dataclass: everything needed to make a request
    "ModelResponse",  # dataclass: everything that came back
    "ModelRouter",  # the main class callers use
    "OllamaClient",  # the low-level HTTP client (advanced use)
    "ProviderConfig",  # dataclass: named provider settings
    "RetryConfig",  # dataclass: retry/backoff settings
    "TimeoutError",  # raised when a request takes too long
    "build_client",  # factory: build OllamaClient for any provider
    "build_json_instruction",  # helper to add "respond in JSON" to a prompt
    "extract_json",  # standalone helper to pull JSON out of raw text
]
