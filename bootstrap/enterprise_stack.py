"""
bootstrap/enterprise_stack.py
==============================

Single responsibility: build the resilience and observability stack.

Extracted from main.py so that enterprise component construction can be
read and modified without touching AI component wiring or logging.

All components are optional — each degrades gracefully when its
dependencies are missing or its feature flag is disabled.

Usage
-----
::

    from bootstrap.enterprise_stack import build_enterprise_stack

    enterprise = build_enterprise_stack()
    await enterprise["dlq"].connect()
    await enterprise["audit_log"].connect()
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger("tinker.bootstrap.enterprise")


def build_enterprise_stack() -> dict:
    """Initialise all enterprise-grade components.

    Returns
    -------
    dict with keys:
      circuit_registry, dist_lock, dlq, idempotency_cache,
      rate_registry, backpressure, alerter, sla_tracker,
      audit_log, tracer, lineage_tracker, ab_testing,
      capacity_planner, feature_flags, backup_manager,
      auto_recovery, health_server (None until start() called).
    """
    # ── Alerting ──────────────────────────────────────────────────────────────
    from infra.observability.alerting import AlertManager, NullAlertManager
    from tinker_platform.features.flags import default_flags as flags

    slack_url = os.getenv("TINKER_SLACK_WEBHOOK")
    webhook_url = os.getenv("TINKER_ALERT_WEBHOOK")
    alerter = (
        AlertManager(slack_webhook_url=slack_url, webhook_url=webhook_url)
        if (slack_url or webhook_url)
        else NullAlertManager()
    )

    # ── Circuit breakers ──────────────────────────────────────────────────────
    from infra.resilience.circuit_breaker import build_default_registry

    circuit_registry = build_default_registry(
        on_state_change=alerter.on_circuit_state_change
        if flags.is_enabled("circuit_breakers")
        else None
    )

    # ── Distributed lock ──────────────────────────────────────────────────────
    redis_url = os.getenv("TINKER_REDIS_URL", "redis://localhost:6379")
    if flags.is_enabled("distributed_locking"):
        from infra.resilience.distributed_lock import DistributedLock

        dist_lock = DistributedLock(redis_url=redis_url)
    else:
        from infra.resilience.distributed_lock import NullDistributedLock

        dist_lock = NullDistributedLock()

    # ── Dead letter queue ─────────────────────────────────────────────────────
    from infra.resilience.dead_letter_queue import DeadLetterQueue

    dlq = DeadLetterQueue(db_path=os.getenv("TINKER_DLQ_PATH", "tinker_dlq.sqlite"))

    # ── Idempotency cache ─────────────────────────────────────────────────────
    from infra.resilience.idempotency import IdempotencyCache

    idempotency_cache = IdempotencyCache(
        redis_url=redis_url,
        default_ttl=int(os.getenv("TINKER_IDEMPOTENCY_TTL", "3600")),
    )

    # ── Rate limiters ─────────────────────────────────────────────────────────
    from infra.resilience.rate_limiter import build_default_rate_limiters

    rate_registry = build_default_rate_limiters()

    # ── Backpressure ──────────────────────────────────────────────────────────
    from infra.resilience.backpressure import BackpressureController

    backpressure = BackpressureController(
        queue_warn_depth=int(os.getenv("TINKER_BP_WARN_DEPTH", "50")),
        queue_pause_depth=int(os.getenv("TINKER_BP_PAUSE_DEPTH", "200")),
    )

    # ── SLA tracker ───────────────────────────────────────────────────────────
    from infra.observability.alerting import AlertType as _AlertType
    from infra.observability.sla_tracker import build_default_sla_tracker

    _sla_log = logging.getLogger("tinker.sla_tracker")

    def _sla_breach_callback(report) -> None:
        task = asyncio.create_task(
            alerter.alert(
                alert_type=_AlertType.SLA_BREACH,
                title=f"SLA breach: {report.name}",
                message=f"p99={report.p99_s:.1f}s > target {report.sla_p99:.1f}s",
                context=report.to_dict(),
            )
        )

        def _on_done(t: asyncio.Task) -> None:
            if not t.cancelled() and t.exception() is not None:
                _sla_log.warning("SLA breach alert failed: %s", t.exception())

        task.add_done_callback(_on_done)

    sla_tracker = build_default_sla_tracker(alert_on_breach=_sla_breach_callback)

    # ── Audit log ─────────────────────────────────────────────────────────────
    from infra.observability.audit_log import AuditLog

    audit_log = AuditLog(db_path=os.getenv("TINKER_AUDIT_LOG_PATH", "tinker_audit.sqlite"))

    # ── Tracing ───────────────────────────────────────────────────────────────
    from infra.observability.tracing import Tracer

    tracer = Tracer(
        max_traces=int(os.getenv("TINKER_TRACER_WINDOW", "100")),
        auto_log=True,
    )

    # ── Data lineage ──────────────────────────────────────────────────────────
    from tinker_platform.lineage.tracker import LineageTracker

    lineage_tracker = LineageTracker(
        db_path=os.getenv("TINKER_LINEAGE_PATH", "tinker_lineage.sqlite")
    )

    # ── A/B testing ───────────────────────────────────────────────────────────
    from tinker_platform.experiments.ab_testing import ABTestingFramework

    ab_testing = ABTestingFramework()

    # ── Capacity planning ─────────────────────────────────────────────────────
    from tinker_platform.capacity.planner import CapacityPlanner

    capacity_planner = CapacityPlanner(
        workspace_path=os.getenv("TINKER_WORKSPACE", "./tinker_workspace"),
        artifact_path=os.getenv("TINKER_ARTIFACT_DIR", "./tinker_artifacts"),
    )

    # ── Backup manager ────────────────────────────────────────────────────────
    from infra.backup.backup_manager import BackupManager

    backup_manager = BackupManager(
        backup_dir=os.getenv("TINKER_BACKUP_DIR", "./tinker_backups"),
        duckdb_path=os.getenv("TINKER_DUCKDB_PATH", "tinker_session.duckdb"),
        sqlite_path=os.getenv("TINKER_SQLITE_PATH", "tinker_tasks.sqlite"),
        chroma_path=os.getenv("TINKER_CHROMA_PATH", "./chroma_db"),
        retention_days=int(os.getenv("TINKER_BACKUP_RETENTION_DAYS", "7")),
    )

    logger.info(
        "Enterprise stack built: circuit_breakers=%s, distributed_locking=%s, alerting=%s",
        flags.is_enabled("circuit_breakers"),
        flags.is_enabled("distributed_locking"),
        bool(slack_url or webhook_url),
    )

    return {
        "circuit_registry": circuit_registry,
        "dist_lock": dist_lock,
        "dlq": dlq,
        "idempotency_cache": idempotency_cache,
        "rate_registry": rate_registry,
        "backpressure": backpressure,
        "alerter": alerter,
        "sla_tracker": sla_tracker,
        "audit_log": audit_log,
        "tracer": tracer,
        "lineage_tracker": lineage_tracker,
        "ab_testing": ab_testing,
        "capacity_planner": capacity_planner,
        "feature_flags": flags,
        "backup_manager": backup_manager,
        "auto_recovery": None,  # wired later after memory_manager exists
        "health_server": None,  # started later after orchestrator exists
    }
