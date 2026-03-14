"""
Tinker — model_client
=====================
Public API surface for the Model Client component.

Quick start
-----------
    from tinker.model_client import ModelRouter, ModelRequest, AgentRole, Message

    async with ModelRouter() as router:
        resp = await router.complete_json(
            role       = AgentRole.ARCHITECT,
            prompt     = "Propose a service mesh architecture for 12 microservices.",
            schema_hint= '{"name": str, "services": [...], "rationale": str}',
        )
        print(resp.json)
"""

from .client  import OllamaClient, ModelClientError, ConnectionError, TimeoutError
from .parsing import extract_json, build_json_instruction
from .router  import ModelRouter
from .types   import (
    AgentRole,
    Machine,
    MachineConfig,
    Message,
    ModelRequest,
    ModelResponse,
    RetryConfig,
    ROLE_MACHINE_MAP,
)

__all__ = [
    "ModelRouter",
    "OllamaClient",
    "AgentRole",
    "Machine",
    "MachineConfig",
    "Message",
    "ModelRequest",
    "ModelResponse",
    "RetryConfig",
    "ROLE_MACHINE_MAP",
    "ModelClientError",
    "ConnectionError",
    "TimeoutError",
    "extract_json",
    "build_json_instruction",
]
