"""
observability/ — Enterprise observability stack for Tinker.

Provides structured logging, distributed tracing, alerting, SLA tracking,
and audit logging — all the tools needed to understand Tinker's behaviour
in production and diagnose problems quickly.

Modules:
  structured_logging — JSON-formatted logs with trace IDs and context propagation
  tracing            — Lightweight span-based tracing for loop performance analysis
  alerting           — Webhook/Slack notifications for failures and stagnation
  sla_tracker        — SLA definition, measurement, and breach detection
  audit_log          — Immutable append-only event log for compliance and forensics
"""
