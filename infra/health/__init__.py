"""
infra/health/ — HTTP health check endpoints for Tinker.

Exposes liveness and readiness probes for Kubernetes, load balancers,
and monitoring tools.

  http_server  — Runs a lightweight HTTP server on a configurable port
                 (default: 8080) with /health, /ready, /live, and /metrics
                 endpoints.

Default endpoints:
  GET /live     — Liveness: returns 200 if the process is alive.
  GET /ready    — Readiness: returns 200 if all components are connected.
  GET /health   — Detailed: returns JSON with component health and stats.
  GET /metrics  — Prometheus metrics (if prometheus-client is installed).

Usage:
    health_server = HealthServer(orchestrator=orch, memory_manager=mm)
    await health_server.start(port=8080)
    ...
    await health_server.stop()
"""
