"""
infra/observability/alerting.py
==========================

Webhook and Slack alerting for Tinker failures and stagnation events.

Why alerting?
--------------
Without alerts, Tinker can fail silently for hours:
  - The task queue drains and Tinker stops generating architecture
  - Stagnation loops for hours without producing useful output
  - A disk-full error stops artifact storage but logs are never seen
  - Redis goes down and working memory is lost — no one notices

With alerting, an operator is notified immediately and can intervene.

Alert channels
---------------
  - Webhook : Generic HTTP POST with JSON payload.
              Works with PagerDuty, Opsgenie, custom endpoints, etc.
  - Slack   : Formatted Slack message via incoming webhook URL.
  - Log     : Always-available fallback — logs at ERROR level.

Alert types
-----------
  STAGNATION        : The StagnationMonitor fired an intervention directive.
  CIRCUIT_OPEN      : A circuit breaker opened (service unavailable).
  CONSECUTIVE_FAILURES: Too many micro loop failures in a row.
  DLQ_SPIKE         : Dead letter queue has accumulated many pending items.
  HEALTH_CHECK_FAIL : A startup or periodic health check failed.
  MACRO_FAILED      : The macro architectural snapshot failed to commit.
  SLA_BREACH        : A loop SLA was breached.
  CUSTOM            : Any custom alert triggered by application code.

Usage
------
::

    alerter = AlertManager(
        slack_webhook_url = os.getenv("TINKER_SLACK_WEBHOOK"),
        webhook_url       = os.getenv("TINKER_ALERT_WEBHOOK"),
    )

    # Send an alert:
    await alerter.alert(
        alert_type = AlertType.STAGNATION,
        title      = "Semantic loop detected",
        message    = "Same topic repeated 5 times — injecting exploration task",
        severity   = AlertSeverity.WARNING,
        context    = {"subsystem": "api_gateway", "loop": 42},
    )

    # Automatically alert on circuit breaker opens:
    breaker.on_state_change(alerter.on_circuit_state_change)
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


class AlertType(enum.Enum):
    """Type of alert being sent."""

    STAGNATION = "stagnation"
    CIRCUIT_OPEN = "circuit_open"
    CIRCUIT_CLOSE = "circuit_close"
    CONSECUTIVE_FAILURES = "consecutive_failures"
    DLQ_SPIKE = "dlq_spike"
    HEALTH_CHECK_FAIL = "health_check_fail"
    MACRO_FAILED = "macro_failed"
    SLA_BREACH = "sla_breach"
    BACKUP_FAILED = "backup_failed"
    CUSTOM = "custom"


class AlertSeverity(enum.Enum):
    """Alert urgency level."""

    INFO = "info"  # FYI — no action needed
    WARNING = "warning"  # Investigate soon
    ERROR = "error"  # Action required
    CRITICAL = "critical"  # Immediate action required


# Map severity to Slack message colours
_SLACK_COLORS = {
    AlertSeverity.INFO: "#36a64f",  # green
    AlertSeverity.WARNING: "#e8a838",  # yellow
    AlertSeverity.ERROR: "#d73a49",  # red
    AlertSeverity.CRITICAL: "#7d1f1f",  # dark red
}

# Rate limiting: don't send the same alert type more than once per N seconds
_DEFAULT_COOLDOWN_SECONDS: dict[AlertType, float] = {
    AlertType.STAGNATION: 300,  # 5 minutes
    AlertType.CIRCUIT_OPEN: 60,  # 1 minute
    AlertType.CONSECUTIVE_FAILURES: 120,  # 2 minutes
    AlertType.DLQ_SPIKE: 600,  # 10 minutes
    AlertType.HEALTH_CHECK_FAIL: 300,  # 5 minutes
    AlertType.SLA_BREACH: 120,  # 2 minutes
    AlertType.BACKUP_FAILED: 600,  # 10 minutes
    AlertType.CUSTOM: 0,  # No cooldown
}


class AlertManager:
    """
    Sends alerts to Slack, generic webhooks, and logs.

    Supports rate limiting (cooldown) to prevent alert storms.
    If no webhook URLs are configured, all alerts go to the log only.

    Parameters
    ----------
    slack_webhook_url : Slack incoming webhook URL.
                        Get one from: https://api.slack.com/messaging/webhooks
    webhook_url       : Generic HTTP endpoint for JSON alerts.
    min_severity      : Only send alerts at this level or above.
                        Default: WARNING (don't send INFO to Slack).
    """

    def __init__(
        self,
        slack_webhook_url: Optional[str] = None,
        webhook_url: Optional[str] = None,
        min_severity: AlertSeverity = AlertSeverity.WARNING,
    ) -> None:
        self._slack_url = slack_webhook_url
        self._webhook_url = webhook_url
        self._min_severity = min_severity
        self._last_alert_at: dict[AlertType, float] = {}
        self._total_sent: int = 0
        self._total_suppressed: int = 0

    async def alert(
        self,
        alert_type: AlertType,
        title: str,
        message: str,
        severity: AlertSeverity = AlertSeverity.WARNING,
        context: Optional[dict] = None,
    ) -> bool:
        """
        Send an alert through all configured channels.

        Respects severity thresholds and cooldown periods to prevent alert storms.

        Parameters
        ----------
        alert_type : Category of the alert (for routing and cooldown).
        title      : Short alert title (shown in Slack subject line).
        message    : Detailed alert message.
        severity   : Urgency level (filters out low-priority alerts).
        context    : Optional additional context dict (shown in alert body).

        Returns
        -------
        True if the alert was sent, False if it was suppressed (cooldown or threshold).
        """
        # Check severity threshold
        severity_rank = list(AlertSeverity).index(severity)
        min_rank = list(AlertSeverity).index(self._min_severity)
        if severity_rank < min_rank:
            return False

        # Check cooldown
        cooldown = _DEFAULT_COOLDOWN_SECONDS.get(alert_type, 0)
        last = self._last_alert_at.get(alert_type, 0)
        if cooldown > 0 and time.monotonic() - last < cooldown:
            self._total_suppressed += 1
            logger.debug(
                "Alert suppressed (cooldown): type=%s title='%s'",
                alert_type.value,
                title,
            )
            return False

        self._last_alert_at[alert_type] = time.monotonic()
        self._total_sent += 1

        # Always log the alert
        log_msg = f"[ALERT:{severity.value.upper()}] {title} — {message}"
        if context:
            log_msg += f" | context={context}"
        if severity in (AlertSeverity.ERROR, AlertSeverity.CRITICAL):
            logger.error(log_msg)
        else:
            logger.warning(log_msg)

        # Send to configured channels (fire and forget — don't crash on failure)
        tasks = []
        if self._slack_url:
            tasks.append(
                self._send_slack(title, message, severity, context, alert_type)
            )
        if self._webhook_url:
            tasks.append(
                self._send_webhook(title, message, severity, context, alert_type)
            )

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.warning("Alert send failed (channel %d): %s", i, result)

        return True

    # ------------------------------------------------------------------
    # Channel implementations
    # ------------------------------------------------------------------

    async def _send_slack(
        self,
        title: str,
        message: str,
        severity: AlertSeverity,
        context: Optional[dict],
        alert_type: AlertType,
    ) -> None:
        """Send a formatted message to a Slack incoming webhook."""
        color = _SLACK_COLORS.get(severity, "#cccccc")
        fields = [
            {"title": "Type", "value": alert_type.value, "short": True},
            {"title": "Severity", "value": severity.value, "short": True},
        ]
        if context:
            for k, v in list(context.items())[:5]:  # Max 5 context fields
                fields.append({"title": str(k), "value": str(v), "short": True})

        payload = {
            "attachments": [
                {
                    "color": color,
                    "title": f"Tinker: {title}",
                    "text": message,
                    "fields": fields,
                    "footer": "Tinker Alerter",
                    "ts": int(time.time()),
                }
            ]
        }
        await self._post_json(self._slack_url, payload)

    async def _send_webhook(
        self,
        title: str,
        message: str,
        severity: AlertSeverity,
        context: Optional[dict],
        alert_type: AlertType,
    ) -> None:
        """Send a generic JSON alert to a webhook endpoint."""
        payload = {
            "source": "tinker",
            "alert_type": alert_type.value,
            "severity": severity.value,
            "title": title,
            "message": message,
            "context": context or {},
            "timestamp": time.time(),
        }
        await self._post_json(self._webhook_url, payload)

    async def _post_json(self, url: str, payload: dict) -> None:
        """POST a JSON payload to a URL."""
        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    data=json.dumps(payload),
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status >= 400:
                        logger.warning("Alert webhook returned HTTP %d", resp.status)
        except ImportError:
            logger.debug("aiohttp not available — alert not sent to webhook")
        except Exception as exc:
            logger.warning("Alert webhook error: %s", exc)

    # ------------------------------------------------------------------
    # Convenience callbacks for wiring to other components
    # ------------------------------------------------------------------

    def on_circuit_state_change(
        self, breaker: Any, old_state: Any, new_state: Any
    ) -> None:
        """
        Callback to wire to CircuitBreaker.on_state_change.

        Sends an alert when any circuit breaker opens.

        Usage::

            breaker.on_state_change(alerter.on_circuit_state_change)
        """
        from infra.resilience.circuit_breaker import CircuitState

        if new_state == CircuitState.OPEN:
            asyncio.create_task(
                self.alert(
                    alert_type=AlertType.CIRCUIT_OPEN,
                    title=f"Circuit breaker OPEN: {breaker.name}",
                    message=(
                        f"Service '{breaker.name}' is unavailable after "
                        f"{breaker.failure_count} consecutive failures. "
                        f"Tinker will retry in {breaker.recovery_timeout:.0f}s."
                    ),
                    severity=AlertSeverity.ERROR,
                    context=breaker.stats(),
                )
            )
        elif new_state == CircuitState.CLOSED and old_state != CircuitState.CLOSED:
            asyncio.create_task(
                self.alert(
                    alert_type=AlertType.CIRCUIT_CLOSE,
                    title=f"Circuit breaker RECOVERED: {breaker.name}",
                    message=f"Service '{breaker.name}' has recovered and is now healthy.",
                    severity=AlertSeverity.INFO,
                )
            )

    def on_stagnation(self, directive: Any) -> None:
        """
        Callback to wire to stagnation detection.

        Sends an alert when a high-severity stagnation directive is fired.
        """
        severity_val = getattr(directive, "severity", 0.0)
        if severity_val < 0.7:
            return  # Only alert on high-severity stagnation

        stagnation_type = getattr(directive, "stagnation_type", None)
        intervention = getattr(directive, "intervention_type", None)

        asyncio.create_task(
            self.alert(
                alert_type=AlertType.STAGNATION,
                title=f"Stagnation detected: {getattr(stagnation_type, 'value', 'unknown')}",
                message=(
                    f"Stagnation type '{getattr(stagnation_type, 'value', 'unknown')}' "
                    f"detected with severity {severity_val:.2f}. "
                    f"Intervention: {getattr(intervention, 'value', 'none')}."
                ),
                severity=AlertSeverity.WARNING,
                context={
                    "stagnation_type": getattr(stagnation_type, "value", "unknown"),
                    "intervention": getattr(intervention, "value", "none"),
                    "severity": severity_val,
                },
            )
        )

    def stats(self) -> dict:
        """Return alert statistics for monitoring."""
        return {
            "total_sent": self._total_sent,
            "total_suppressed": self._total_suppressed,
            "min_severity": self._min_severity.value,
            "channels": {
                "slack": bool(self._slack_url),
                "webhook": bool(self._webhook_url),
                "log": True,
            },
        }


class NullAlertManager:
    """
    No-op alert manager for testing or when alerting is disabled.

    All methods are no-ops that return immediately.
    """

    async def alert(self, *args, **kwargs) -> bool:
        return False

    def on_circuit_state_change(self, *args, **kwargs) -> None:
        pass

    def on_stagnation(self, *args, **kwargs) -> None:
        pass

    def stats(self) -> dict:
        return {"enabled": False}
