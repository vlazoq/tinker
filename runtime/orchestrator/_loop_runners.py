"""
runtime/orchestrator/_loop_runners.py
======================================
Loop runner methods extracted from the Orchestrator class.

Contains the methods that execute individual micro/meso/macro loop
iterations and the model preset hot-reload check.
"""

from __future__ import annotations

import logging
import time

from core.events import EventType

from .macro_loop import run_macro_loop
from .meso_loop import run_meso_loop
from .micro_loop import MicroLoopError, run_micro_loop
from .state import LoopLevel, LoopStatus

logger = logging.getLogger("tinker.orchestrator")


class LoopRunnerMixin:
    """
    Mixin providing micro/meso/macro loop execution and model preset reload.

    Mixed into the Orchestrator class.  Methods access orchestrator state
    via ``self`` (config, state, metrics, stagnation_monitor, task_engine, etc.).
    """

    async def _run_micro(self) -> bool:
        """
        Execute one micro loop iteration and return True on success.

        Delegates the actual work to ``run_micro_loop()`` in micro_loop.py.
        This method handles exceptions, updates state, and returns True/False.
        """
        try:
            record = await run_micro_loop(self)

            self.state.total_micro_loops += 1
            self.state.current_task_id = record.task_id
            self.state.current_subsystem = record.subsystem

            # Per-loop cost attribution
            arch_tokens = record.architect_tokens or 0
            critic_tokens = record.critic_tokens or 0
            total_tokens = arch_tokens + critic_tokens
            logger.info(
                "micro[%d] cost — architect_tokens=%d critic_tokens=%d "
                "total_tokens=%d task=%s subsystem=%s",
                self.state.total_micro_loops,
                arch_tokens,
                critic_tokens,
                total_tokens,
                record.task_id,
                record.subsystem,
            )

            self.state.add_micro_record(record)

            if record.status == LoopStatus.SUCCESS:
                self.state.increment_subsystem(record.subsystem)

                if self.metrics is not None:
                    self.metrics.on_micro_loop(record)

                # Emit micro loop completed event
                await self.emit_event(
                    EventType.MICRO_LOOP_COMPLETED,
                    {
                        "iteration": self.state.total_micro_loops,
                        "task_id": record.task_id,
                        "subsystem": record.subsystem,
                        "critic_score": record.critic_score,
                        "architect_tokens": arch_tokens,
                        "critic_tokens": critic_tokens,
                        "artifact_id": record.artifact_id,
                    },
                )

                # Anti-stagnation check
                if self.stagnation_monitor is not None:
                    directives = await self._check_stagnation(record)
                    if directives:
                        await self._apply_stagnation_directive(directives[0])

                # Capacity planner update
                await self._update_capacity_planner(record)

                return True

            # Emit failure event
            await self.emit_event(
                EventType.MICRO_LOOP_FAILED,
                {
                    "iteration": self.state.total_micro_loops,
                    "task_id": record.task_id,
                    "subsystem": record.subsystem,
                    "error": record.error,
                },
            )
            return False

        except MicroLoopError as exc:
            logger.error("Micro loop failed: %s", exc)
            await self.emit_event(
                EventType.MICRO_LOOP_FAILED,
                {
                    "iteration": self.state.total_micro_loops,
                    "error": str(exc),
                },
            )
            return False

        except Exception as exc:
            logger.exception("Unexpected error in micro loop: %s", exc)
            await self.emit_event(
                EventType.MICRO_LOOP_FAILED,
                {
                    "iteration": self.state.total_micro_loops,
                    "error": str(exc),
                },
            )
            return False

    def _should_run_meso(self, subsystem: str) -> bool:
        """
        Return True if ``subsystem`` has accumulated enough micro loops to
        justify a meso synthesis.
        """
        count = self.state.subsystem_micro_counts.get(subsystem, 0)
        return count >= self.config.meso_trigger_count

    async def _run_meso(self, subsystem: str) -> None:
        """
        Execute a meso-level synthesis for ``subsystem``.

        Temporarily switches the reported level to MESO, delegates to
        ``run_meso_loop()``, then restores the previous level.
        """
        prev_level = self.state.current_level
        self.state.current_level = LoopLevel.MESO
        logger.info("Escalating to meso loop for subsystem=%s", subsystem)

        try:
            record = await run_meso_loop(self, subsystem, self.state.total_micro_loops)
            self.state.total_meso_loops += 1
            self.state.add_meso_record(record)
            if self.metrics is not None:
                self.metrics.on_meso_loop(record)
            await self.emit_event(
                EventType.MESO_LOOP_COMPLETED,
                {
                    "subsystem": subsystem,
                    "iteration": self.state.total_meso_loops,
                    "document_id": getattr(record, "document_id", None),
                },
            )
        except Exception as exc:
            logger.exception("Meso loop raised unexpectedly: %s", exc)
            await self.emit_event(
                EventType.MESO_LOOP_FAILED,
                {
                    "subsystem": subsystem,
                    "error": str(exc),
                },
            )
        finally:
            self.state.current_level = prev_level

    def _should_run_macro(self) -> bool:
        """
        Return True if enough time has passed since the last macro snapshot.
        """
        elapsed = time.monotonic() - self.state.last_macro_at
        return elapsed >= self.config.macro_interval_seconds

    async def _run_macro(self) -> None:
        """
        Execute a full macro architectural snapshot.

        Resets the macro timer immediately (so a slow macro run doesn't
        cascade), then delegates to ``run_macro_loop()``.
        """
        prev_level = self.state.current_level
        self.state.current_level = LoopLevel.MACRO
        logger.info("Triggering macro loop (architectural snapshot)")

        self.state.last_macro_at = time.monotonic()

        try:
            record = await run_macro_loop(self, self.state.total_micro_loops)
            self.state.total_macro_loops += 1
            self.state.add_macro_record(record)
            if self.metrics is not None:
                self.metrics.on_macro_loop(record)
            await self.emit_event(
                EventType.MACRO_LOOP_COMPLETED,
                {
                    "iteration": self.state.total_macro_loops,
                    "commit_hash": getattr(record, "commit_hash", None),
                    "snapshot_version": getattr(record, "snapshot_version", None),
                },
            )
        except Exception as exc:
            logger.exception("Macro loop raised unexpectedly: %s", exc)
            await self.emit_event(
                EventType.MACRO_LOOP_FAILED,
                {
                    "error": str(exc),
                },
            )
        finally:
            self.state.current_level = prev_level

    async def _check_model_preset(self, last_mtime: float) -> float:
        """
        Check whether the active model preset has changed since the last loop.

        If ``tinker_active_preset.json`` has a newer mtime than ``last_mtime``,
        load the preset and call ``router.hot_reload()`` to swap models without
        a process restart.

        Returns the current mtime of the preset file.
        """
        try:
            from core.models.library import ModelLibrary
            from core.models.presets import PresetManager
        except ImportError:
            return last_mtime

        if not hasattr(self, "_preset_manager"):
            lib = ModelLibrary()
            self._preset_manager = PresetManager(lib)

        mgr: PresetManager = self._preset_manager  # type: ignore[name-defined]
        current_mtime = mgr.active_file_mtime()

        if current_mtime <= last_mtime:
            return last_mtime

        preset = mgr.active_preset()
        if preset is None:
            logger.warning("Active preset file changed but preset not found in presets.json")
            return current_mtime

        lib = mgr._library  # type: ignore[attr-defined]
        main_entry = lib.get(preset.main_model_id)
        judge_entry = lib.get(preset.judge_model_id)

        if main_entry is None or judge_entry is None:
            logger.warning(
                "Preset '%s' references unknown model(s): main=%s judge=%s",
                preset.name,
                preset.main_model_id,
                preset.judge_model_id,
            )
            return current_mtime

        router = getattr(self, "model_router", None)
        if router is not None:
            await router.hot_reload(
                main_model=main_entry.model_tag,
                main_url=main_entry.ollama_url,
                judge_model=judge_entry.model_tag,
                judge_url=judge_entry.ollama_url,
                main_ctx=main_entry.context_window,
                judge_ctx=judge_entry.context_window,
            )

        grub = getattr(self, "grub_agent", None)
        if grub is not None and preset.grub_overrides:
            grub.apply_model_overrides(preset.grub_overrides)

        logger.info(
            "Model preset '%s' activated: Main=%s Judge=%s",
            preset.name,
            main_entry.model_tag,
            judge_entry.model_tag,
        )
        return current_mtime
