"""
resilience/ — Enterprise resilience primitives for Tinker.

This package provides the fault-tolerance building blocks that make Tinker
safe to run in production:

  circuit_breaker   — Prevents cascading failures by "opening" a breaker
                      when a downstream service repeatedly fails, then
                      "closing" it again once the service recovers.

  distributed_lock  — Redis-backed mutex that prevents duplicate work
                      when multiple Tinker instances share the same database.

  dead_letter_queue — Captures permanently failed operations so they can
                      be inspected, replayed, or discarded without silent
                      data loss.

  idempotency       — Deduplication cache that ensures retried operations
                      produce the same result instead of creating duplicates.

  rate_limiter      — Token-bucket rate limiter that caps how fast Tinker
                      can call AI models and external tools, preventing
                      runaway costs or quota exhaustion.

  backpressure      — Feedback loop that slows down task generation when
                      queue depth, memory pressure, or error rates are high.

  auto_recovery     — Self-healing logic that restarts failed components
                      and degrades gracefully when subsystems are unavailable.

All primitives are designed to be injected into the orchestrator and other
components so they can be replaced with no-op stubs during testing.
"""
