"""
ui/web/app.py
─────────────
FastAPI backend for the Tinker Web UI.

This file is the composition root — it creates the FastAPI app, wires
middleware, includes all route modules, and re-exports key symbols for
backward compatibility.

Route implementations live in ``ui/web/routes/``:
  health.py          — /api/health, /api/state, /api/version, /api/grub/status
  config.py          — /api/config, /api/flags
  orchestrator_ctrl.py — /api/confirmations, /api/confirm, /api/pause, /api/resume, /api/mcp/status
  tasks.py           — /api/tasks, /api/dlq
  audit.py           — /api/audit, /api/errors
  fritz.py           — /api/fritz/*
  models.py          — /api/models/*
  backups.py         — /api/backups
  streaming.py       — /api/logs/stream (SSE)

Run:  python -m tinker.ui.web          (default port 8082)
      TINKER_WEBUI_PORT=9000 python -m tinker.ui.web
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from infra.resilience.rate_limiter import TokenBucketRateLimiter

# ── Per-IP HTTP rate limiting ─────────────────────────────────────────────────
_WEBUI_RATE_PER_SEC: float = float(os.getenv("TINKER_WEBUI_RATE_PER_SEC", "2.0"))
_WEBUI_RATE_BURST: float = float(os.getenv("TINKER_WEBUI_RATE_BURST", "30.0"))

_ip_limiters: dict[str, TokenBucketRateLimiter] = {}
_ip_limiters_lock = asyncio.Lock()


async def _limiter_for_ip(ip: str) -> TokenBucketRateLimiter:
    """Return (lazily creating) the token-bucket rate limiter for *ip*."""
    async with _ip_limiters_lock:
        if ip not in _ip_limiters:
            _ip_limiters[ip] = TokenBucketRateLimiter(
                rate=_WEBUI_RATE_PER_SEC,
                capacity=_WEBUI_RATE_BURST,
            )
        return _ip_limiters[ip]


class _APIRateLimitMiddleware(BaseHTTPMiddleware):
    """
    Per-IP token-bucket rate limiter for ``/api/*`` endpoints.

    Non-API requests (static files, SPA shell) are not rate-limited.
    When a client exceeds the rate, the middleware returns HTTP 429 with
    standard ``Retry-After`` and ``X-RateLimit-*`` headers.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)

        ip = request.client.host if request.client else "unknown"
        limiter = await _limiter_for_ip(ip)
        acquired, retry_after = await limiter.try_acquire()

        if not acquired:
            return JSONResponse(
                {"error": "Rate limit exceeded", "retry_after": retry_after},
                status_code=429,
                headers={
                    "Retry-After": str(int(retry_after) + 1),
                    "X-RateLimit-Limit": str(_WEBUI_RATE_PER_SEC),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(_WEBUI_RATE_PER_SEC)
        return response


# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(title="Tinker Web UI", docs_url="/api/docs", redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(_APIRateLimitMiddleware)

_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")


# ── Include all route modules ─────────────────────────────────────────────────
from ui.web.routes.audit import router as audit_router
from ui.web.routes.backups import router as backups_router
from ui.web.routes.config import router as config_router
from ui.web.routes.fritz import router as fritz_router
from ui.web.routes.health import router as health_router
from ui.web.routes.models import router as models_router
from ui.web.routes.orchestrator_ctrl import router as orch_router
from ui.web.routes.reviews import router as reviews_router
from ui.web.routes.streaming import router as streaming_router
from ui.web.routes.tasks import router as tasks_router
from ui.web.routes.workflow import router as workflow_router

app.include_router(health_router)
app.include_router(config_router)
app.include_router(orch_router)
app.include_router(tasks_router)
app.include_router(audit_router)
app.include_router(fritz_router)
app.include_router(models_router)
app.include_router(backups_router)
app.include_router(streaming_router)
app.include_router(reviews_router)
app.include_router(workflow_router)


# ── SPA shell ─────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ── Backward-compatible re-exports ───────────────────────────────────────────
# main.py imports these symbols directly from ui.web.app:
#   _publisher, notify_state_change, app
# Tests import: app, api_logs_stream
from ui.web.routes.streaming import (  # noqa: F401
    StatePublisher,
    _publisher,
    api_logs_stream,
    notify_state_change,
)


@app.on_event("startup")
async def _attach_publisher() -> None:
    """Attach the StatePublisher to app.state on startup."""
    app.state.publisher = _publisher
