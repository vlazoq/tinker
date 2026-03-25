"""
infra/observability/ — Enterprise observability stack for Tinker.

Provides structured logging, distributed tracing, alerting, SLA tracking,
and audit logging — all the tools needed to understand Tinker's behaviour
in production and diagnose problems quickly.

Modules:
  structured_logging     — JSON-formatted logs with trace IDs and context propagation
  tracing                — Lightweight span-based tracing for loop performance analysis
  alerting               — Webhook/Slack notifications for failures and stagnation
  sla_tracker            — SLA definition, measurement, and breach detection
  audit_log              — Immutable append-only event log for compliance and forensics

Key helpers
-----------
``record_tinker_exception(exc, span)``
    Attach a ``TinkerError``'s ``.context`` dict and ``.retryable`` flag to an
    active tracing span so structured diagnostics appear in the trace timeline
    without parsing log strings.  Import from ``observability.tracing``.
"""

# Expose the exception-recording helper at the package level so callers can
# write: ``from infra.observability import record_tinker_exception``
from .tracing import record_tinker_exception  # noqa: F401
