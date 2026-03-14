"""
Base Tool interface for the Tinker Tool Layer.
All tools must inherit from BaseTool and implement execute().
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    """Standardised response envelope every tool must return."""
    success: bool
    tool_name: str
    data: Any                        # The actual payload
    error: str | None = None
    duration_ms: float = 0.0
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "tool_name": self.tool_name,
            "data": self.data,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 2),
            "metadata": self.metadata,
        }


@dataclass
class ToolSchema:
    """Describes a tool so the Orchestrator can expose it to the model."""
    name: str
    description: str
    parameters: dict        # JSON-Schema object describing the input
    returns: str            # Human-readable description of the return value


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class BaseTool(ABC):
    """Every tool in the Tinker Tool Layer inherits from this class."""

    @property
    @abstractmethod
    def schema(self) -> ToolSchema:
        """Return the static schema for this tool."""
        ...

    @abstractmethod
    async def _execute(self, **kwargs) -> Any:
        """Implement the actual tool logic here; return raw payload."""
        ...

    async def execute(self, **kwargs) -> ToolResult:
        """Public entry point — wraps _execute with timing and error handling."""
        t0 = time.monotonic()
        try:
            data = await self._execute(**kwargs)
            duration = (time.monotonic() - t0) * 1000
            return ToolResult(
                success=True,
                tool_name=self.schema.name,
                data=data,
                duration_ms=duration,
            )
        except Exception as exc:          # noqa: BLE001
            duration = (time.monotonic() - t0) * 1000
            return ToolResult(
                success=False,
                tool_name=self.schema.name,
                data=None,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=duration,
            )
