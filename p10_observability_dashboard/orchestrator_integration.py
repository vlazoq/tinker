"""
tinker/dashboard/orchestrator_integration.py
─────────────────────────────────────────────
Reference implementation showing EXACTLY how the real Tinker Orchestrator
should push state into the Dashboard.

Copy the relevant snippets into your Orchestrator; do not import this file
as a module — it is documentation-as-code.

There are two integration paths:

  Path A  ──  Same Python process  (asyncio.Queue, zero extra deps)
  Path B  ──  Separate processes   (Redis pub/sub, requires redis[asyncio])

─────────────────────────────────────────────
PATH A — In-process asyncio.Queue (recommended for single-machine setups)
─────────────────────────────────────────────

The Orchestrator and Dashboard share one Python interpreter.
The Dashboard is started as an asyncio.Task or thread; the Orchestrator
calls `publish_state(patch)` at the end of every loop tick.

  ┌─────────────────────────────────┐      asyncio.Queue      ┌──────────────────┐
  │         Orchestrator            │ ──── publish_state() ──► │    Dashboard     │
  │  (async loop, any thread)       │                          │  QueueSubscriber │
  └─────────────────────────────────┘                          └──────────────────┘


Minimal wiring in your Orchestrator:

    # At the top of orchestrator.py
    from tinker.dashboard.subscriber import publish_state

    # At the end of each micro-loop tick (or whenever state changes):
    publish_state({
        "connected":   True,
        "loop_level":  "micro",        # "micro" | "meso" | "macro"
        "micro_count": self.micro_count,
        "meso_count":  self.meso_count,
        "macro_count": self.macro_count,

        "active_task": {
            "id":          task.id,
            "type":        task.type,           # e.g. "design"
            "subsystem":   task.subsystem,
            "description": task.description,
            "status":      "active",
            "created_at":  task.created_at.isoformat(),
            "started_at":  task.started_at.isoformat(),
            "full_content": task.full_spec,     # shown in detail view
        },

        "last_architect": {
            "summary":      architect_result.summary,
            "full_content": architect_result.full_text,
            "timestamp":    datetime.utcnow().isoformat(),
            "task_id":      task.id,
        },

        "last_critic": {
            "score":         critic_result.score,
            "top_objection": critic_result.objections[0],
            "full_content":  critic_result.full_text,
            "timestamp":     datetime.utcnow().isoformat(),
            "task_id":       task.id,
        },

        "queue_stats": {
            "total_depth": len(self.task_queue),
            "by_status": {
                "pending":  self.task_queue.count_by_status("pending"),
                "active":   self.task_queue.count_by_status("active"),
                "complete": self.task_queue.count_by_status("complete"),
                "failed":   self.task_queue.count_by_status("failed"),
            },
            "by_type": {t: self.task_queue.count_by_type(t) for t in TASK_TYPES},
        },

        "recent_tasks": [
            {
                "id":           t.id,
                "type":         t.type,
                "subsystem":    t.subsystem,
                "description":  t.description,
                "status":       t.status,
                "created_at":   t.created_at.isoformat(),
                "started_at":   t.started_at.isoformat() if t.started_at else None,
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                "result_summary": t.result_summary,
                "full_content": t.full_content,
            }
            for t in self.task_queue.recent(10)
        ],

        "arch_state": {
            "version":          self.arch_state_manager.current_version,
            "last_commit_time": self.arch_state_manager.last_commit.isoformat(),
            "summary":          self.arch_state_manager.summary,
            "full_content":     self.arch_state_manager.full_spec,
        },

        "stagnation": {
            "is_stagnant":      self.anti_stagnation.is_stagnant,
            "stagnation_score": self.anti_stagnation.score,
            "monitor_status":   self.anti_stagnation.status_label,
            "recent_events": [
                {
                    "timestamp":    e.timestamp.isoformat(),
                    "description":  e.description,
                    "action_taken": e.action_taken,
                }
                for e in self.anti_stagnation.recent_events[-5:]
            ],
        },

        "model_metrics": {
            "avg_latency_ms": self.model_client.metrics.avg_latency_ms,
            "p99_latency_ms": self.model_client.metrics.p99_latency_ms,
            "error_rate":     self.model_client.metrics.error_rate,
            "total_calls":    self.model_client.metrics.total_calls,
            "recent_errors":  self.model_client.metrics.recent_errors[-5:],
        },

        "memory_stats": {
            "session_artifact_count": self.memory_manager.session_count,
            "research_archive_size":  self.memory_manager.archive_size,
            "working_memory_tokens":  self.context_assembler.current_token_count,
        },
    })

    # To launch the dashboard alongside the Orchestrator in the same process:

    import asyncio
    from tinker.dashboard import TinkerDashboard
    from tinker.dashboard.subscriber import QueueSubscriber

    async def run_system():
        dashboard = TinkerDashboard(subscriber=QueueSubscriber())
        orchestrator = MyOrchestrator()

        await asyncio.gather(
            dashboard.run_async(),
            orchestrator.run(),
        )

    asyncio.run(run_system())


─────────────────────────────────────────────
PATH B — Redis pub/sub (for separate-process / multi-machine setups)
─────────────────────────────────────────────

  ┌─────────────────┐   JSON patch    ┌───────────┐   pub/sub   ┌──────────────────┐
  │  Orchestrator   │ ──────────────► │   Redis   │ ──────────► │    Dashboard     │
  │  (process A)    │                 │           │             │  RedisSubscriber │
  └─────────────────┘                 └───────────┘             │  (process B)     │
                                                                 └──────────────────┘

Install extra deps:
    pip install "tinker-dashboard[redis]"
    # or:  pip install redis[asyncio]

In the Orchestrator (process A):
    import json, redis
    r = redis.Redis.from_url("redis://localhost:6379")

    def publish_state_redis(patch: dict) -> None:
        r.publish("tinker:state", json.dumps(patch))

    # Call publish_state_redis(patch) at the end of each loop tick
    # (same patch dict structure as Path A above)

Run the Dashboard in process B:
    python -m tinker.dashboard --redis redis://localhost:6379


─────────────────────────────────────────────
LOGURU INTEGRATION
─────────────────────────────────────────────

In your Orchestrator (or any Tinker component):

    from loguru import logger
    from tinker.dashboard.log_handler import loguru_sink

    # Add the dashboard sink — log records flow into the live log panel
    logger.add(
        loguru_sink,
        format="{time}|{level}|{name}:{function}:{line}|{message}",
        colorize=False,    # dashboard handles its own colouring
        level="DEBUG",
    )

    # From that point on, normal loguru calls appear in the dashboard:
    logger.info("Orchestrator started")
    logger.debug("Dispatching task {}", task.id)
    logger.warning("Model latency spike: {}ms", latency)


─────────────────────────────────────────────
DISCONNECTION HANDLING
─────────────────────────────────────────────

The Dashboard handles Orchestrator crashes gracefully:

• QueueSubscriber: if no update arrives within `timeout` seconds (default 5s),
  the subscriber leaves the store in "connected" state (Orchestrator may just
  be idle).  If you want explicit disconnection signalling, publish a patch with
  `{"connected": False}` before the Orchestrator shuts down.

• RedisSubscriber: any Redis connection error triggers `mark_disconnected()` and
  a reconnection loop with exponential backoff.  The UI shows "○ DISCONNECTED"
  in the status bar but keeps running and displaying the last known state.

• In both cases, the Dashboard process never crashes due to Orchestrator failure.


─────────────────────────────────────────────
RECOMMENDED UPDATE FREQUENCY
─────────────────────────────────────────────

  Every micro-loop tick   →  publish full state patch  (1–2 Hz is fine)
  On task status change   →  publish immediately
  On architecture commit  →  publish immediately
  On stagnation event     →  publish immediately

  The QueueSubscriber drops patches if the queue is full (maxsize=256),
  so very high-frequency publishing is safe — the dashboard simply shows
  the most recent state it received.
"""
