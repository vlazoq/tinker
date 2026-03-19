"""
tinker/anti_stagnation/tests/test_anti_stagnation.py
──────────────────────────────────────────────────────
Simulates all five stagnation failure modes and verifies:
  - Correct StagnationType is detected
  - Correct InterventionType is returned
  - Severity is in [0, 1]
  - No false positives under normal (diverse) conditions
  - Event log is populated correctly
  - Config overrides are respected

Run with:
    python -m pytest tinker/anti_stagnation/tests/ -v
  or standalone:
    python tinker/anti_stagnation/tests/test_anti_stagnation.py
"""

from __future__ import annotations

import sys
import os
import unittest

# ── allow running from repo root without install ──────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

from stagnation import (
    FallbackTFIDFBackend,
    InterventionType,
    MicroLoopContext,
    StagnationMonitor,
    StagnationMonitorConfig,
    StagnationType,
)
from stagnation.config import (
    CritiqueCollapseConfig,
    ResearchSaturationConfig,
    SemanticLoopConfig,
    SubsystemFixationConfig,
    TaskStarvationConfig,
)


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────


def make_monitor(**overrides) -> StagnationMonitor:
    """
    Build a StagnationMonitor configured for fast triggering in tests.
    Uses the TF-IDF fallback backend so no Ollama is required.
    """
    cfg = StagnationMonitorConfig(
        semantic_loop=SemanticLoopConfig(
            window_size=4,
            similarity_threshold=0.90,
            min_breach_count=2,
        ),
        subsystem_fixation=SubsystemFixationConfig(
            window_size=5,
            fixation_threshold=0.70,
        ),
        critique_collapse=CritiqueCollapseConfig(
            window_size=4,
            collapse_threshold=0.85,
            min_samples=3,
        ),
        research_saturation=ResearchSaturationConfig(
            window_size=3,
            overlap_threshold=0.60,
            min_url_count=2,
        ),
        task_starvation=TaskStarvationConfig(
            low_depth_threshold=3,
            window_size=4,
            consecutive_negative_threshold=2,
        ),
        run_all_detectors=True,
    )
    backend = FallbackTFIDFBackend(max_vocab=256)
    monitor = StagnationMonitor(config=cfg, embedding_backend=backend)
    return monitor


def ctx(loop_index: int, **kwargs) -> MicroLoopContext:
    """Shorthand MicroLoopContext factory."""
    return MicroLoopContext(loop_index=loop_index, **kwargs)


# ─────────────────────────────────────────────────────────────
# 1. Semantic Loop
# ─────────────────────────────────────────────────────────────


class TestSemanticLoopDetector(unittest.TestCase):
    def test_detects_repeated_output(self):
        """Repeatedly submitting nearly identical text should trigger SEMANTIC_LOOP."""
        monitor = make_monitor()
        same_text = (
            "The memory manager uses an LRU cache backed by Redis. "
            "Keys are hashed by subsystem and task id. "
            "Eviction policy is LRU with a 24-hour TTL."
        )
        directives = []
        for i in range(6):
            directives = monitor.check(ctx(i, output_text=same_text))
            if directives:
                break

        self.assertTrue(
            any(d.stagnation_type == StagnationType.SEMANTIC_LOOP for d in directives),
            "Expected SEMANTIC_LOOP to be detected",
        )
        directive = next(
            d for d in directives if d.stagnation_type == StagnationType.SEMANTIC_LOOP
        )
        self.assertEqual(
            directive.intervention_type, InterventionType.ALTERNATIVE_FORCING
        )
        self.assertGreater(directive.severity, 0.0)
        self.assertLessEqual(directive.severity, 1.0)

    def test_no_false_positive_diverse_outputs(self):
        """Highly varied outputs should NOT trigger SEMANTIC_LOOP."""
        monitor = make_monitor()
        texts = [
            "The task engine dequeues items using a priority heap.",
            "OAuth2 token refresh is handled by the tool layer middleware.",
            "Macro synthesis aggregates subsystem critiques into a global score.",
            "The context assembler truncates history to fit the model's context window.",
            "Observability metrics are pushed to Prometheus every 30 seconds.",
            "Architecture state is persisted in a PostgreSQL JSONB column.",
        ]
        all_directives = []
        for i, text in enumerate(texts):
            all_directives.extend(monitor.check(ctx(i, output_text=text)))

        semantic_hits = [
            d
            for d in all_directives
            if d.stagnation_type == StagnationType.SEMANTIC_LOOP
        ]
        self.assertEqual(
            len(semantic_hits), 0, "False positive: diverse outputs flagged as loops"
        )

    def test_severity_is_bounded(self):
        monitor = make_monitor()
        text = "identical output " * 50
        for i in range(8):
            directives = monitor.check(ctx(i, output_text=text))
        for d in directives:
            self.assertGreaterEqual(d.severity, 0.0)
            self.assertLessEqual(d.severity, 1.0)


# ─────────────────────────────────────────────────────────────
# 2. Subsystem Fixation
# ─────────────────────────────────────────────────────────────


class TestSubsystemFixationDetector(unittest.TestCase):
    def test_detects_fixation_on_single_subsystem(self):
        """Looping on the same subsystem > 70% of the time should trigger."""
        monitor = make_monitor()
        tags = [
            "memory_manager",
            "memory_manager",
            "memory_manager",
            "tool_layer",
            "memory_manager",
        ]
        directives = []
        for i, tag in enumerate(tags):
            directives = monitor.check(ctx(i, subsystem_tag=tag))

        hits = [
            d
            for d in directives
            if d.stagnation_type == StagnationType.SUBSYSTEM_FIXATION
        ]
        self.assertTrue(len(hits) > 0, "Expected SUBSYSTEM_FIXATION")
        self.assertEqual(hits[0].intervention_type, InterventionType.FORCE_BRANCH)
        self.assertEqual(hits[0].metadata.get("avoid_subsystem"), "memory_manager")

    def test_balanced_subsystems_no_flag(self):
        """Round-robin through multiple subsystems should not trigger."""
        monitor = make_monitor()
        tags = [
            "memory_manager",
            "tool_layer",
            "orchestrator",
            "task_engine",
            "context_assembler",
        ]
        all_directives = []
        for i, tag in enumerate(tags):
            all_directives.extend(monitor.check(ctx(i, subsystem_tag=tag)))

        fixation_hits = [
            d
            for d in all_directives
            if d.stagnation_type == StagnationType.SUBSYSTEM_FIXATION
        ]
        self.assertEqual(len(fixation_hits), 0, "False positive: balanced tags flagged")

    def test_window_not_full_no_trigger(self):
        """Detector should wait for a full window before evaluating."""
        monitor = make_monitor()  # window_size=5
        # Only 4 entries (< window_size), all the same
        for i in range(4):
            directives = monitor.check(ctx(i, subsystem_tag="memory_manager"))
        hits = [
            d
            for d in directives
            if d.stagnation_type == StagnationType.SUBSYSTEM_FIXATION
        ]
        self.assertEqual(len(hits), 0, "Should not fire before window is full")


# ─────────────────────────────────────────────────────────────
# 3. Critique Collapse
# ─────────────────────────────────────────────────────────────


class TestCritiqueCollapseDetector(unittest.TestCase):
    def test_detects_high_rolling_scores(self):
        """Consistently high Critic scores should trigger CRITIQUE_COLLAPSE."""
        monitor = make_monitor()
        scores = [0.91, 0.93, 0.90, 0.95, 0.92]
        directives = []
        for i, score in enumerate(scores):
            directives = monitor.check(ctx(i, critic_score=score))

        hits = [
            d
            for d in directives
            if d.stagnation_type == StagnationType.CRITIQUE_COLLAPSE
        ]
        self.assertTrue(len(hits) > 0, "Expected CRITIQUE_COLLAPSE")
        self.assertEqual(
            hits[0].intervention_type, InterventionType.INJECT_CONTRADICTION
        )

    def test_no_false_positive_healthy_scores(self):
        """Scores varying healthily around 0.7 should not trigger."""
        monitor = make_monitor()
        scores = [0.65, 0.72, 0.68, 0.75, 0.70, 0.66]
        all_directives = []
        for i, score in enumerate(scores):
            all_directives.extend(monitor.check(ctx(i, critic_score=score)))

        collapse_hits = [
            d
            for d in all_directives
            if d.stagnation_type == StagnationType.CRITIQUE_COLLAPSE
        ]
        self.assertEqual(
            len(collapse_hits), 0, "False positive: healthy scores flagged"
        )

    def test_requires_min_samples(self):
        """Should not fire until min_samples (3) scores are recorded."""
        monitor = make_monitor()  # min_samples=3
        directives = []
        for i, score in enumerate([0.95, 0.96]):  # only 2 scores
            directives = monitor.check(ctx(i, critic_score=score))

        hits = [
            d
            for d in directives
            if d.stagnation_type == StagnationType.CRITIQUE_COLLAPSE
        ]
        self.assertEqual(len(hits), 0, "Should not fire before min_samples reached")


# ─────────────────────────────────────────────────────────────
# 4. Research Saturation
# ─────────────────────────────────────────────────────────────


class TestResearchSaturationDetector(unittest.TestCase):
    def test_detects_repeated_sources(self):
        """Researcher returning the same URLs repeatedly should trigger."""
        monitor = make_monitor()
        shared_urls = {
            "https://martin.fowler.com/articles/microservices.html",
            "https://12factor.net/",
            "https://docs.aws.amazon.com/wellarchitected/",
        }
        directives = []
        for i in range(4):
            directives = monitor.check(ctx(i, research_urls=shared_urls))

        hits = [
            d
            for d in directives
            if d.stagnation_type == StagnationType.RESEARCH_SATURATION
        ]
        self.assertTrue(len(hits) > 0, "Expected RESEARCH_SATURATION")
        self.assertEqual(hits[0].intervention_type, InterventionType.SPAWN_EXPLORATION)
        # Directive should carry the repeated URLs
        self.assertIn("exclude_urls", hits[0].metadata)

    def test_fresh_sources_no_flag(self):
        """Each loop finding different sources should not trigger."""
        monitor = make_monitor()
        url_sets = [
            {"https://source-a.com", "https://source-b.com"},
            {"https://source-c.com", "https://source-d.com"},
            {"https://source-e.com", "https://source-f.com"},
        ]
        all_directives = []
        for i, urls in enumerate(url_sets):
            all_directives.extend(monitor.check(ctx(i, research_urls=urls)))

        saturation_hits = [
            d
            for d in all_directives
            if d.stagnation_type == StagnationType.RESEARCH_SATURATION
        ]
        self.assertEqual(
            len(saturation_hits), 0, "False positive: fresh sources flagged"
        )

    def test_partial_overlap_below_threshold_no_flag(self):
        """50% overlap (below 60% threshold) should not trigger."""
        monitor = make_monitor()
        base = {"https://a.com", "https://b.com"}
        varied = {"https://a.com", "https://c.com"}  # 33% Jaccard
        for i in range(3):
            directives = monitor.check(
                ctx(i, research_urls=base if i % 2 == 0 else varied)
            )
        hits = [
            d
            for d in directives
            if d.stagnation_type == StagnationType.RESEARCH_SATURATION
        ]
        self.assertEqual(len(hits), 0)


# ─────────────────────────────────────────────────────────────
# 5. Task Starvation
# ─────────────────────────────────────────────────────────────


class TestTaskStarvationDetector(unittest.TestCase):
    def test_detects_draining_queue(self):
        """Low queue depth + consistent negative net generation should trigger."""
        monitor = make_monitor()
        scenarios = [
            dict(queue_depth=5, tasks_generated=1, tasks_consumed=3),
            dict(queue_depth=3, tasks_generated=0, tasks_consumed=2),
            dict(queue_depth=1, tasks_generated=0, tasks_consumed=1),
        ]
        directives = []
        for i, s in enumerate(scenarios):
            directives = monitor.check(ctx(i, **s))

        hits = [
            d for d in directives if d.stagnation_type == StagnationType.TASK_STARVATION
        ]
        self.assertTrue(len(hits) > 0, "Expected TASK_STARVATION")
        self.assertEqual(hits[0].intervention_type, InterventionType.ESCALATE_LOOP)

    def test_healthy_generation_rate_no_flag(self):
        """Queue depth low but generation keeping up should not trigger."""
        monitor = make_monitor()
        scenarios = [
            dict(queue_depth=2, tasks_generated=5, tasks_consumed=3),
            dict(queue_depth=4, tasks_generated=4, tasks_consumed=2),
            dict(queue_depth=5, tasks_generated=3, tasks_consumed=1),
        ]
        all_directives = []
        for i, s in enumerate(scenarios):
            all_directives.extend(monitor.check(ctx(i, **s)))

        starvation_hits = [
            d
            for d in all_directives
            if d.stagnation_type == StagnationType.TASK_STARVATION
        ]
        self.assertEqual(
            len(starvation_hits), 0, "False positive: healthy queue flagged"
        )

    def test_high_queue_depth_no_flag(self):
        """Even with negative net generation, a deep queue should not trigger."""
        monitor = make_monitor()
        for i in range(5):
            directives = monitor.check(
                ctx(
                    i,
                    queue_depth=20,
                    tasks_generated=1,
                    tasks_consumed=2,
                )
            )
        hits = [
            d for d in directives if d.stagnation_type == StagnationType.TASK_STARVATION
        ]
        self.assertEqual(len(hits), 0)


# ─────────────────────────────────────────────────────────────
# 6. Integration: multiple stagnation types in one session
# ─────────────────────────────────────────────────────────────


class TestIntegration(unittest.TestCase):
    def test_multiple_stagnation_types_independent(self):
        """
        Simulate a session that triggers subsystem fixation AND critique
        collapse simultaneously and verify both are detected.
        """
        monitor = make_monitor()

        for i in range(6):
            ctx_kwargs = dict(
                subsystem_tag="memory_manager",  # fixation
                critic_score=0.92,  # collapse
            )
            directives = monitor.check(ctx(i, **ctx_kwargs))

        types_detected = {d.stagnation_type for d in directives}
        self.assertIn(StagnationType.SUBSYSTEM_FIXATION, types_detected)
        self.assertIn(StagnationType.CRITIQUE_COLLAPSE, types_detected)

    def test_event_log_populated(self):
        """Every detected stagnation should append to the event log."""
        monitor = make_monitor()
        for i in range(6):
            monitor.check(
                ctx(
                    i,
                    subsystem_tag="memory_manager",
                    critic_score=0.94,
                )
            )

        self.assertGreater(monitor.event_log.total(), 0)
        counts = monitor.event_log.counts_by_type()
        self.assertGreater(counts.get(StagnationType.SUBSYSTEM_FIXATION.value, 0), 0)

    def test_directives_sorted_by_severity(self):
        """Returned directives must be sorted highest severity first."""
        monitor = make_monitor()
        for i in range(6):
            directives = monitor.check(
                ctx(
                    i,
                    subsystem_tag="memory_manager",
                    critic_score=0.94,
                    output_text="identical output text repeated again and again",
                )
            )

        if len(directives) >= 2:
            for a, b in zip(directives, directives[1:]):
                self.assertGreaterEqual(a.severity, b.severity)

    def test_reset_clears_state(self):
        """After reset, no stagnation should be detected from a clean slate."""
        monitor = make_monitor()

        # Pump enough cycles to detect fixation
        for i in range(6):
            monitor.check(ctx(i, subsystem_tag="memory_manager"))

        monitor.reset_all()

        # Single cycle after reset should never fire
        directives = monitor.check(ctx(0, subsystem_tag="memory_manager"))
        fixation_hits = [
            d
            for d in directives
            if d.stagnation_type == StagnationType.SUBSYSTEM_FIXATION
        ]
        self.assertEqual(len(fixation_hits), 0, "Detector should be reset")
        self.assertEqual(monitor.event_log.total(), 0)

    def test_config_from_dict(self):
        """StagnationMonitorConfig.from_dict should override specific thresholds."""
        overrides = {
            "subsystem_fixation": {"fixation_threshold": 0.50, "window_size": 4},
            "critique_collapse": {"collapse_threshold": 0.75},
            "run_all_detectors": False,
        }
        cfg = StagnationMonitorConfig.from_dict(overrides)
        self.assertEqual(cfg.subsystem_fixation.fixation_threshold, 0.50)
        self.assertEqual(cfg.subsystem_fixation.window_size, 4)
        self.assertEqual(cfg.critique_collapse.collapse_threshold, 0.75)
        self.assertFalse(cfg.run_all_detectors)

    def test_summary_structure(self):
        """monitor.summary() should return the expected keys."""
        monitor = make_monitor()
        summary = monitor.summary()
        self.assertIn("total_events", summary)
        self.assertIn("counts_by_type", summary)
        self.assertIn("recent_events", summary)

    def test_intervention_directive_to_dict(self):
        """InterventionDirective.to_dict() should be JSON-serialisable."""
        import json

        monitor = make_monitor()
        for i in range(6):
            directives = monitor.check(ctx(i, subsystem_tag="memory_manager"))

        if directives:
            d = directives[0]
            serialised = json.dumps(d.to_dict())
            self.assertIsInstance(serialised, str)

    def test_partial_context_no_crash(self):
        """Passing a context with only some fields should not crash any detector."""
        monitor = make_monitor()
        # Each tick provides only one kind of data
        monitor.check(ctx(0, output_text="hello world"))
        monitor.check(ctx(1, subsystem_tag="orchestrator"))
        monitor.check(ctx(2, critic_score=0.80))
        monitor.check(ctx(3, research_urls={"https://example.com"}))
        monitor.check(ctx(4, queue_depth=10, tasks_generated=2, tasks_consumed=1))
        # No assertions — just ensuring no exceptions are raised


# ─────────────────────────────────────────────────────────────
# 7. Edge cases
# ─────────────────────────────────────────────────────────────


class TestEdgeCases(unittest.TestCase):
    def test_empty_context_no_crash(self):
        monitor = make_monitor()
        directives = monitor.check(ctx(0))
        self.assertIsInstance(directives, list)

    def test_critic_score_clamped(self):
        """Out-of-range critic scores should be clamped, not crash."""
        monitor = make_monitor()
        for i, score in enumerate([1.5, -0.2, 2.0, 0.95, 0.97, 0.94]):
            monitor.check(ctx(i, critic_score=score))  # should not raise

    def test_run_all_false_stops_at_first_hit(self):
        """With run_all_detectors=False, only the first firing detector's directive is returned."""
        cfg = StagnationMonitorConfig(
            subsystem_fixation=SubsystemFixationConfig(
                window_size=5, fixation_threshold=0.70
            ),
            critique_collapse=CritiqueCollapseConfig(
                window_size=4, collapse_threshold=0.85, min_samples=3
            ),
            run_all_detectors=False,
        )
        monitor = StagnationMonitor(
            config=cfg, embedding_backend=FallbackTFIDFBackend()
        )
        for i in range(6):
            directives = monitor.check(
                ctx(
                    i,
                    subsystem_tag="memory_manager",
                    critic_score=0.92,
                )
            )

        # Only one directive should be returned when run_all=False
        self.assertLessEqual(len(directives), 1)


# ─────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    test_classes = [
        TestSemanticLoopDetector,
        TestSubsystemFixationDetector,
        TestCritiqueCollapseDetector,
        TestResearchSaturationDetector,
        TestTaskStarvationDetector,
        TestIntegration,
        TestEdgeCases,
    ]

    for cls in test_classes:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
