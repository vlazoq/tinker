"""
Tinker Model Client — Type definitions and configuration.
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
    ARCHITECT   = "architect"
    RESEARCHER  = "researcher"
    SYNTHESIZER = "synthesizer"
    CRITIC      = "critic"


# ---------------------------------------------------------------------------
# Machine targets
# ---------------------------------------------------------------------------

class Machine(str, Enum):
    SERVER    = "server"     # i7-7700 / RTX 3090  — 7B models
    SECONDARY = "secondary"  # lighter machine      — 2-3B models


# Routing table: which machine handles each role
ROLE_MACHINE_MAP: dict[AgentRole, Machine] = {
    AgentRole.ARCHITECT:   Machine.SERVER,
    AgentRole.RESEARCHER:  Machine.SERVER,
    AgentRole.SYNTHESIZER: Machine.SERVER,
    AgentRole.CRITIC:      Machine.SECONDARY,
}


# ---------------------------------------------------------------------------
# Model names (override via environment or MachineConfig)
# ---------------------------------------------------------------------------

DEFAULT_SERVER_MODEL    = "qwen3:7b"
DEFAULT_SECONDARY_MODEL = "phi3:mini"


# ---------------------------------------------------------------------------
# Machine-level configuration
# ---------------------------------------------------------------------------

@dataclass
class MachineConfig:
    base_url: str
    model: str
    context_window: int = 8192         # tokens the model can accept
    max_output_tokens: int = 2048
    request_timeout: float = 120.0     # seconds
    connect_timeout: float = 10.0      # seconds

    @classmethod
    def server_defaults(cls) -> "MachineConfig":
        return cls(
            base_url=os.getenv("TINKER_SERVER_URL", "http://localhost:11434"),
            model=os.getenv("TINKER_SERVER_MODEL", DEFAULT_SERVER_MODEL),
            context_window=int(os.getenv("TINKER_SERVER_CTX", "8192")),
            max_output_tokens=int(os.getenv("TINKER_SERVER_MAX_OUT", "2048")),
            request_timeout=float(os.getenv("TINKER_SERVER_TIMEOUT", "120")),
        )

    @classmethod
    def secondary_defaults(cls) -> "MachineConfig":
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
    role: str          # "system" | "user" | "assistant"
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class ModelRequest:
    agent_role: AgentRole
    messages: list[Message]
    temperature: float = 0.7
    expect_json: bool = False          # If True, extract/validate JSON from reply
    json_schema_hint: str | None = None  # Optional schema description for the prompt

    # Populated by the client before sending
    resolved_model: str = ""
    resolved_machine: Machine = Machine.SERVER


@dataclass
class ModelResponse:
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
        return bool(self.raw_text)

    @property
    def json(self) -> dict[str, Any] | list[Any]:
        if self.structured is None:
            raise ValueError("Response did not contain parseable JSON.")
        return self.structured


# ---------------------------------------------------------------------------
# Retry config
# ---------------------------------------------------------------------------

@dataclass
class RetryConfig:
    max_attempts: int = 3
    base_delay: float = 2.0       # seconds
    max_delay: float = 30.0
    backoff_factor: float = 2.0   # exponential
    retryable_status_codes: set[int] = field(
        default_factory=lambda: {429, 500, 502, 503, 504}
    )
