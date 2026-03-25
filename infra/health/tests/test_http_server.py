"""
Tests for health/http_server.py
==================================

Covers liveness, readiness (with disk check), /health, and /metrics
Prometheus exposition.  No real HTTP connections needed — we invoke
the handlers directly via mock reader/writer objects.
"""

from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock

from infra.health.http_server import HealthServer


class FakeWriter:
    """Captures bytes written by the health server handlers."""

    def __init__(self):
        self._data = b""

    def write(self, data: bytes) -> None:
        self._data += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    def response_body(self) -> dict:
        """Parse the JSON body from the HTTP response."""
        # Split headers from body
        parts = self._data.split(b"\r\n\r\n", 1)
        if len(parts) < 2:
            return {}
        return json.loads(parts[1])

    def response_text(self) -> str:
        """Return the raw response body as text."""
        parts = self._data.split(b"\r\n\r\n", 1)
        if len(parts) < 2:
            return ""
        return parts[1].decode("utf-8")

    def status_code(self) -> int:
        line = self._data.decode("utf-8").split("\r\n", 1)[0]
        return int(line.split(" ")[1])


@pytest.fixture
def server():
    return HealthServer(data_dir=".", disk_warn_pct=99.9)


class TestLiveness:
    @pytest.mark.asyncio
    async def test_live_returns_200(self, server):
        writer = FakeWriter()
        await server._handle_live(writer)
        assert writer.status_code() == 200
        body = writer.response_body()
        assert body["status"] == "alive"
        assert "uptime_seconds" in body


class TestReadiness:
    @pytest.mark.asyncio
    async def test_ready_no_dependencies_returns_200(self, server):
        writer = FakeWriter()
        await server._handle_ready(writer)
        assert writer.status_code() == 200

    @pytest.mark.asyncio
    async def test_ready_open_circuit_returns_503(self):
        registry = MagicMock()
        registry.all_stats.return_value = {"ollama": {"state": "open"}}
        server = HealthServer(circuit_registry=registry, disk_warn_pct=99.9)
        writer = FakeWriter()
        await server._handle_ready(writer)
        assert writer.status_code() == 503
        body = writer.response_body()
        assert any("OPEN" in issue for issue in body["issues"])

    @pytest.mark.asyncio
    async def test_disk_check_warns_when_full(self, tmp_path):
        """disk_warn_pct=0 means any disk usage triggers the warning."""
        server = HealthServer(data_dir=str(tmp_path), disk_warn_pct=0.0)
        writer = FakeWriter()
        await server._handle_ready(writer)
        assert writer.status_code() == 503
        body = writer.response_body()
        assert any("disk" in issue.lower() for issue in body["issues"])


class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_health_returns_200(self, server):
        writer = FakeWriter()
        await server._handle_health(writer)
        assert writer.status_code() == 200
        body = writer.response_body()
        assert "status" in body

    @pytest.mark.asyncio
    async def test_health_includes_disk_info(self, server):
        writer = FakeWriter()
        await server._handle_health(writer)
        body = writer.response_body()
        assert "disk" in body
        assert "used_pct" in body["disk"]

    @pytest.mark.asyncio
    async def test_health_degraded_when_circuit_open(self):
        registry = MagicMock()
        registry.any_open.return_value = True
        registry.all_stats.return_value = {}
        server = HealthServer(circuit_registry=registry, disk_warn_pct=99.9)
        writer = FakeWriter()
        await server._handle_health(writer)
        body = writer.response_body()
        assert body["status"] == "degraded"


class TestMetricsEndpoint:
    @pytest.mark.asyncio
    async def test_metrics_returns_prometheus_text(self, server):
        writer = FakeWriter()
        await server._handle_metrics(writer)
        assert writer.status_code() == 200
        text = writer.response_text()
        assert "# HELP" in text
        assert "# TYPE" in text

    @pytest.mark.asyncio
    async def test_metrics_includes_uptime(self, server):
        writer = FakeWriter()
        await server._handle_metrics(writer)
        text = writer.response_text()
        assert "tinker_uptime_seconds" in text

    @pytest.mark.asyncio
    async def test_metrics_includes_disk(self, server):
        writer = FakeWriter()
        await server._handle_metrics(writer)
        text = writer.response_text()
        assert "tinker_disk_used_bytes" in text
        assert "tinker_disk_free_bytes" in text

    @pytest.mark.asyncio
    async def test_metrics_includes_loop_counters_when_orchestrator_present(self):
        state = MagicMock()
        state.total_micro_loops = 42
        state.total_meso_loops = 5
        state.total_macro_loops = 1
        state.consecutive_failures = 0
        state.stagnation_events_total = 2
        orchestrator = MagicMock()
        orchestrator.state = state
        server = HealthServer(orchestrator=orchestrator, disk_warn_pct=99.9)
        writer = FakeWriter()
        await server._handle_metrics(writer)
        text = writer.response_text()
        assert "tinker_micro_loops_total" in text
        assert "42" in text
