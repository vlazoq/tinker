"""
agents/grub/__main__.py
================
Entry point for running Grub from the command line.

Usage
-----
::

    # Normal mode: poll Tinker for tasks indefinitely
    python -m grub

    # Specify a config file
    python -m grub --config /path/to/grub_config.json

    # Override execution mode (without editing config)
    GRUB_EXEC_MODE=parallel python -m grub

    # Queue mode: start as a worker (separate from the queue manager)
    python -m grub --mode worker --worker-id my-3090-worker

    # Run on a specific task directly (bypasses Tinker, for testing)
    python -m grub --run-task "Implement API router" --artifact ./design.md
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Make sure the tinker root is on the path
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load .env if present
_env = ROOT / ".env"
if _env.exists():
    try:
        from dotenv import load_dotenv

        load_dotenv(_env)
    except ImportError:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("grub.main")


def _parse_args():
    parser = argparse.ArgumentParser(description="Grub — AI code implementation agent")
    parser.add_argument(
        "--config",
        default="grub_config.json",
        help="Path to grub_config.json (default: ./grub_config.json)",
    )
    parser.add_argument(
        "--mode",
        default="agent",
        choices=["agent", "worker"],
        help=(
            "agent  = full agent (poll Tinker, dispatch tasks) [default]\n"
            "worker = queue worker only (for Mode C multi-machine setup)"
        ),
    )
    parser.add_argument(
        "--worker-id", default=None, help="Worker ID for queue mode (default: hostname)"
    )
    parser.add_argument(
        "--run-task",
        default=None,
        help="Run a single task directly (title string). For testing.",
    )
    parser.add_argument("--artifact", default=None, help="Design artifact path for --run-task")
    return parser.parse_args()


async def _run_agent(args) -> None:
    from agents.grub.agent import GrubAgent

    agent = GrubAgent.from_config(args.config)
    await agent.run()


async def _run_worker(args) -> None:
    """Start a queue worker (Mode C)."""
    import socket

    from agents.grub.agent import GrubAgent
    from agents.grub.loop import GrubQueue, run_queue_worker

    worker_id = args.worker_id or socket.gethostname()
    agent = GrubAgent.from_config(args.config)
    queue = GrubQueue(agent.config.queue_db_path)

    logger.info("Starting queue worker: %s", worker_id)
    await run_queue_worker(worker_id, queue, agent.pipeline)


async def _run_single_task(args) -> None:
    """Run a single task directly (for testing without Tinker)."""
    from agents.grub.agent import GrubAgent
    from agents.grub.contracts.task import GrubTask

    agent = GrubAgent.from_config(args.config)
    task = GrubTask(
        title=args.run_task,
        description=f"Implement: {args.run_task}",
        artifact_path=args.artifact or "",
        subsystem="test",
    )

    logger.info("Running single task: %s", task.title)
    results = await agent.run_tasks([task])
    for r in results:
        print(f"\nResult: status={r.status.value}  score={r.score:.2f}")
        print(f"Files:  {', '.join(r.files_written) or '(none)'}")
        print(f"Summary: {r.summary}")


def main() -> None:
    args = _parse_args()

    if args.run_task:
        asyncio.run(_run_single_task(args))
    elif args.mode == "worker":
        asyncio.run(_run_worker(args))
    else:
        asyncio.run(_run_agent(args))


if __name__ == "__main__":
    main()
