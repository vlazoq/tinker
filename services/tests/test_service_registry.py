"""
Tests for services/registry.py and services/protocol.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from services import ServiceRegistry, ServiceRequest, ServiceResponse
from services.protocol import ServiceInterface


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class StubService:
    """Minimal ServiceInterface implementation for tests."""

    def __init__(self, name: str, status: str = "ok"):
        self._name = name
        self._status = status
        self.started = False
        self.stopped = False

    @property
    def name(self) -> str:
        return self._name

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def health(self) -> dict[str, Any]:
        return {"status": self._status, "name": self._name}


class BrokenService(StubService):
    """Health check always raises."""

    async def health(self) -> dict[str, Any]:
        raise RuntimeError("health endpoint down")


# ---------------------------------------------------------------------------
# ServiceInterface protocol check
# ---------------------------------------------------------------------------


class TestServiceInterfaceProtocol:
    def test_stub_satisfies_protocol(self):
        svc = StubService("test")
        assert isinstance(svc, ServiceInterface)

    def test_missing_health_method_fails(self):
        class NoHealth:
            @property
            def name(self):
                return "x"

            async def start(self):
                pass

            async def stop(self):
                pass

        # Protocol check without health() should fail
        assert not isinstance(NoHealth(), ServiceInterface)


# ---------------------------------------------------------------------------
# ServiceRegistry — registration
# ---------------------------------------------------------------------------


class TestServiceRegistryRegistration:
    def test_register_and_get(self):
        reg = ServiceRegistry()
        svc = StubService("orchestrator")
        reg.register("orchestrator", svc)
        assert reg.get("orchestrator") is svc

    def test_duplicate_raises(self):
        reg = ServiceRegistry()
        reg.register("orch", StubService("orch"))
        with pytest.raises(ValueError, match="already registered"):
            reg.register("orch", StubService("orch"))

    def test_non_service_raises(self):
        reg = ServiceRegistry()
        with pytest.raises(TypeError):
            reg.register("bad", object())  # type: ignore

    def test_unregister_returns_true(self):
        reg = ServiceRegistry()
        reg.register("grub", StubService("grub"))
        assert reg.unregister("grub") is True
        assert "grub" not in reg

    def test_unregister_nonexistent_returns_false(self):
        reg = ServiceRegistry()
        assert reg.unregister("nonexistent") is False

    def test_contains(self):
        reg = ServiceRegistry()
        reg.register("fritz", StubService("fritz"))
        assert "fritz" in reg
        assert "other" not in reg

    def test_len(self):
        reg = ServiceRegistry()
        assert len(reg) == 0
        reg.register("a", StubService("a"))
        reg.register("b", StubService("b"))
        assert len(reg) == 2

    def test_all_names(self):
        reg = ServiceRegistry()
        reg.register("x", StubService("x"))
        reg.register("y", StubService("y"))
        assert set(reg.all_names()) == {"x", "y"}

    def test_get_unknown_raises(self):
        reg = ServiceRegistry()
        with pytest.raises(KeyError, match="No service registered"):
            reg.get("ghost")

    def test_get_or_none_unknown(self):
        reg = ServiceRegistry()
        assert reg.get_or_none("ghost") is None


# ---------------------------------------------------------------------------
# ServiceRegistry — lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestServiceRegistryLifecycle:
    async def test_start_all(self):
        reg = ServiceRegistry()
        a = StubService("a")
        b = StubService("b")
        reg.register("a", a)
        reg.register("b", b)
        await reg.start_all()
        assert a.started and b.started

    async def test_stop_all(self):
        reg = ServiceRegistry()
        a = StubService("a")
        b = StubService("b")
        reg.register("a", a)
        reg.register("b", b)
        await reg.stop_all()
        assert a.stopped and b.stopped


# ---------------------------------------------------------------------------
# ServiceRegistry — health report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestServiceRegistryHealthReport:
    async def test_all_healthy(self):
        reg = ServiceRegistry()
        reg.register("a", StubService("a", "ok"))
        reg.register("b", StubService("b", "ok"))
        report = await reg.health_report()
        assert report["overall"] == "ok"
        assert report["services"]["a"]["status"] == "ok"
        assert report["services"]["b"]["status"] == "ok"

    async def test_one_degraded(self):
        reg = ServiceRegistry()
        reg.register("a", StubService("a", "ok"))
        reg.register("b", StubService("b", "degraded"))
        report = await reg.health_report()
        assert report["overall"] == "degraded"

    async def test_broken_health_captured(self):
        reg = ServiceRegistry()
        reg.register("broken", BrokenService("broken"))
        report = await reg.health_report()
        assert report["services"]["broken"]["status"] == "down"
        assert "error" in report["services"]["broken"]
        assert report["overall"] == "degraded"

    async def test_empty_registry_is_ok(self):
        reg = ServiceRegistry()
        report = await reg.health_report()
        assert report["overall"] == "ok"


# ---------------------------------------------------------------------------
# ServiceRequest / ServiceResponse
# ---------------------------------------------------------------------------


class TestServiceRequest:
    def test_auto_trace_id(self):
        r1 = ServiceRequest(action="ping")
        r2 = ServiceRequest(action="ping")
        assert r1.trace_id != r2.trace_id

    def test_custom_payload(self):
        req = ServiceRequest(action="submit_task", payload={"task_id": "abc"})
        assert req.payload["task_id"] == "abc"


class TestServiceResponse:
    def test_success_factory(self):
        resp = ServiceResponse.success(data={"result": 42}, trace_id="t1")
        assert resp.ok is True
        assert resp.data["result"] == 42
        assert resp.error is None

    def test_failure_factory(self):
        resp = ServiceResponse.failure(error="Not found", trace_id="t2")
        assert resp.ok is False
        assert resp.error == "Not found"
