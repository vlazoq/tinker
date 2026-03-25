"""
services/ — Service boundary abstractions for microservice readiness.

Why this package exists
-----------------------
Tinker currently runs as a monolith (all loops in one process), which is the
right default: simple to deploy, debug, and reason about.

However, as the system grows, individual subsystems may need to scale or be
deployed independently.  For example:
  - Multiple Grub workers pulling from a shared task queue.
  - Fritz running as a separate git-service process.
  - The Orchestrator exposing an HTTP API so external tools can push tasks.

This package establishes the service boundary abstractions that make that
transition possible without rewriting call sites.

Key concepts
------------
``ServiceInterface``    — Protocol every service must implement (health, lifecycle).
``ServiceRegistry``     — Discover and locate registered services.
``ServiceRequest``      — Typed input envelope for cross-service calls.
``ServiceResponse``     — Typed output envelope.

Current state vs future state
------------------------------
Right now:
  - Components communicate via direct Python calls (in-process).
  - ServiceInterface is implemented by thin wrappers around existing classes.

Future:
  - A transport layer (HTTP / gRPC / message queue) can be dropped in under
    the same ServiceInterface without changing any call sites.
  - The ServiceRegistry can be backed by a real service-discovery system
    (e.g. consul, etcd) instead of an in-process dict.

Public API
----------
::

    from services import ServiceInterface, ServiceRegistry, ServiceRequest, ServiceResponse
"""

from .protocol import ServiceInterface, ServiceRequest, ServiceResponse
from .registry import ServiceRegistry

__all__ = [
    "ServiceInterface",
    "ServiceRegistry",
    "ServiceRequest",
    "ServiceResponse",
]
