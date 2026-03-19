"""
experiments/ab_testing.py
==========================

A/B testing framework for Tinker prompt variants and configuration experiments.

Why A/B testing?
-----------------
Tinker has many tuneable parameters:
  - Architect prompt phrasing and structure
  - Critic evaluation rubric
  - Stagnation detection thresholds
  - Meso synthesis trigger counts
  - Temperature and model selection

Without A/B testing, changes are evaluated by intuition ("this feels better").
With A/B testing, changes are evaluated by metrics ("variant B produced 15%
higher critic scores with 20% more tasks generated per hour").

How it works
-------------
  1. Define an experiment with two or more variants
  2. The framework assigns each micro loop iteration to a variant
     (using deterministic hashing for reproducibility)
  3. After the loop, record the outcome metric (critic score, duration, etc.)
  4. Analyse results with statistical significance testing

Usage
------
::

    ab = ABTestingFramework()

    # Define an experiment:
    ab.create_experiment(
        name     = "architect_temperature",
        variants = {"control": 0.7, "treatment": 0.5},
        metric   = "critic_score",
    )

    # Get the assigned variant for this iteration:
    variant, value = ab.get_variant("architect_temperature", unit_id=task_id)
    # variant = "control" or "treatment"
    # value   = 0.7 or 0.5

    # Record the outcome:
    ab.record_outcome("architect_temperature", variant, metric_value=critic_score)

    # Analyse results:
    results = ab.analyse("architect_temperature")
    print(results)
    # {
    #   "control":   {"n": 50, "mean": 0.72, "std": 0.12},
    #   "treatment": {"n": 48, "mean": 0.78, "std": 0.11},
    #   "significant": True,
    #   "winner": "treatment",
    # }
"""

from __future__ import annotations

import hashlib
import logging
import math
import statistics
from dataclasses import dataclass, field
from typing import Any

from exceptions import ExperimentError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Welch's t-test helper
# ---------------------------------------------------------------------------

def _welch_t_test_p(
    mean1: float, std1: float, n1: int,
    mean2: float, std2: float, n2: int,
) -> float:
    """
    Compute the two-tailed p-value for Welch's t-test (unequal variances).

    Uses scipy.stats.ttest_ind_from_stats when available; falls back to a
    pure-Python implementation using the regularised incomplete beta function
    approximation (accurate to within ~0.001 for df > 4).

    Parameters
    ----------
    mean1, std1, n1 : Mean, std-dev, and sample size of group 1 (control).
    mean2, std2, n2 : Mean, std-dev, and sample size of group 2 (treatment).

    Returns
    -------
    float : Two-tailed p-value in [0, 1].  Smaller means more significant.
            Returns 1.0 if std is zero for both groups (no variation → no test).
    """
    # Avoid division-by-zero when both groups have zero variance
    var1 = std1 ** 2 / n1
    var2 = std2 ** 2 / n2
    se2 = var1 + var2
    if se2 == 0:
        return 1.0

    try:
        # Prefer scipy for accuracy
        from scipy import stats  # type: ignore
        result = stats.ttest_ind_from_stats(
            mean1=mean1, std1=std1, nobs1=n1,
            mean2=mean2, std2=std2, nobs2=n2,
            equal_var=False,
        )
        return float(result.pvalue)
    except ImportError:
        pass

    # Pure-Python Welch's t-test
    t = (mean1 - mean2) / math.sqrt(se2)

    # Welch–Satterthwaite degrees of freedom
    df = se2 ** 2 / (var1 ** 2 / (n1 - 1) + var2 ** 2 / (n2 - 1))

    # Two-tailed p-value via regularised incomplete beta function:
    #   p = I(df / (df + t²), df/2, 1/2)
    x = df / (df + t * t)
    p = _betainc(df / 2.0, 0.5, x)
    return min(1.0, max(0.0, p))


def _betainc(a: float, b: float, x: float) -> float:
    """
    Regularised incomplete beta function I_x(a, b) via continued fraction.

    Sufficient accuracy for Welch's t-test p-values (df > 2).  Uses the
    Lentz method with up to 200 iterations.
    """
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0

    # Use the continued-fraction representation when x < (a+1)/(a+b+2)
    # to ensure convergence; otherwise use the symmetry relation.
    if x > (a + 1) / (a + b + 2):
        return 1.0 - _betainc(b, a, 1.0 - x)

    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x)) / a

    # Lentz continued-fraction algorithm
    TINY = 1e-30
    f = TINY
    C = f
    D = 0.0
    for m in range(201):
        for step in (0, 1):
            if m == 0 and step == 0:
                d = 1.0
            elif step == 0:
                d = m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m))
            else:
                d = -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1))
            D = 1.0 + d * D
            if abs(D) < TINY:
                D = TINY
            D = 1.0 / D
            C = 1.0 + d / C
            if abs(C) < TINY:
                C = TINY
            f *= C * D
            if abs(C * D - 1.0) < 1e-8:
                return front * f
    return front * f


@dataclass
class Experiment:
    """
    Definition of an A/B experiment.

    Attributes
    ----------
    name     : Unique experiment identifier.
    variants : Dict mapping variant names to their values.
               The first key is the "control" variant by convention.
    metric   : Name of the metric being measured (for reporting).
    active   : Whether the experiment is currently accepting new assignments.
    """

    name: str
    variants: dict  # e.g. {"control": 0.7, "treatment": 0.5}
    metric: str = "critic_score"
    active: bool = True
    outcomes: dict[str, list[float]] = field(default_factory=dict)

    def __post_init__(self):
        # Initialise outcome buckets for each variant
        for v in self.variants:
            if v not in self.outcomes:
                self.outcomes[v] = []


class ABTestingFramework:
    """
    Manages A/B experiments for Tinker.

    Uses deterministic hashing to assign variants consistently —
    the same unit_id always gets the same variant within an experiment.
    This prevents the same task from seeing different configurations
    on retries.

    Parameters
    ----------
    seed : Random seed for variant assignment (for reproducibility).
    """

    def __init__(self, seed: str = "tinker-ab") -> None:
        self._seed = seed
        self._experiments: dict[str, Experiment] = {}

    def create_experiment(
        self,
        name: str,
        variants: dict,
        metric: str = "critic_score",
    ) -> Experiment:
        """
        Define a new A/B experiment.

        Parameters
        ----------
        name     : Unique name for this experiment.
        variants : Dict of variant_name → variant_value.
        metric   : The metric to compare across variants.

        Returns
        -------
        Experiment : The created experiment object.

        Raises
        ------
        ExperimentError : If the experiment already exists or has fewer than 2 variants.
        """
        if name in self._experiments:
            raise ExperimentError(
                f"Experiment '{name}' already exists",
                context={"experiment": name},
            )
        if len(variants) < 2:
            raise ExperimentError(
                f"Experiment '{name}' needs at least 2 variants",
                context={"experiment": name, "variant_count": len(variants)},
            )

        exp = Experiment(name=name, variants=dict(variants), metric=metric)
        self._experiments[name] = exp
        logger.info(
            "A/B experiment '%s' created with variants: %s", name, list(variants.keys())
        )
        return exp

    def get_variant(self, experiment_name: str, unit_id: str) -> tuple[str, Any]:
        """
        Get the variant assignment for a specific unit (e.g. task ID).

        Uses deterministic hashing so the same unit always gets the same
        variant.  This ensures consistency across retries.

        Parameters
        ----------
        experiment_name : Name of the experiment.
        unit_id         : The unit being assigned (task ID, session ID, etc.).

        Returns
        -------
        (variant_name, variant_value) : The assigned variant and its value.

        Raises
        ------
        ExperimentError : If the experiment doesn't exist.
        """
        exp = self._experiments.get(experiment_name)
        if not exp:
            raise ExperimentError(
                f"Experiment '{experiment_name}' not found",
                context={
                    "experiment": experiment_name,
                    "available": sorted(self._experiments),
                },
            )
        if not exp.active:
            # Return control if experiment is paused
            control = next(iter(exp.variants))
            return control, exp.variants[control]

        # Deterministic hash assignment
        hash_input = f"{self._seed}:{experiment_name}:{unit_id}"
        hash_val = int(hashlib.md5(hash_input.encode()).hexdigest(), 16)
        variant_names = list(exp.variants.keys())
        assigned = variant_names[hash_val % len(variant_names)]
        return assigned, exp.variants[assigned]

    def record_outcome(
        self, experiment_name: str, variant: str, metric_value: float
    ) -> None:
        """
        Record a metric outcome for a variant.

        Parameters
        ----------
        experiment_name : Name of the experiment.
        variant         : The variant that was used (from ``get_variant``).
        metric_value    : The measured outcome (e.g. critic score, duration).
        """
        exp = self._experiments.get(experiment_name)
        if not exp:
            logger.warning("record_outcome: experiment '%s' not found", experiment_name)
            return
        if variant not in exp.outcomes:
            exp.outcomes[variant] = []
        exp.outcomes[variant].append(metric_value)

    def analyse(self, experiment_name: str) -> dict:
        """
        Analyse the results of an experiment.

        Computes per-variant statistics and determines if there's a
        statistically significant difference between variants using
        Welch's t-test (two-sample, unequal variance).

        Parameters
        ----------
        experiment_name : Name of the experiment to analyse.

        Returns
        -------
        dict : Analysis results including per-variant stats and winner.

        Raises
        ------
        ExperimentError : If the experiment doesn't exist.
        """
        exp = self._experiments.get(experiment_name)
        if not exp:
            raise ExperimentError(
                f"Experiment '{experiment_name}' not found",
                context={
                    "experiment": experiment_name,
                    "available": sorted(self._experiments),
                },
            )

        report = {
            "name": experiment_name,
            "metric": exp.metric,
            "active": exp.active,
            "variants": {},
            "significant": False,
            "winner": None,
        }

        # Per-variant statistics
        for variant, outcomes in exp.outcomes.items():
            n = len(outcomes)
            if n == 0:
                report["variants"][variant] = {"n": 0}
                continue
            report["variants"][variant] = {
                "n": n,
                "mean": round(statistics.mean(outcomes), 4),
                "std": round(statistics.stdev(outcomes), 4) if n > 1 else 0.0,
                "min": round(min(outcomes), 4),
                "max": round(max(outcomes), 4),
                "value": exp.variants[variant],
            }

        # Determine winner (simple: highest mean wins)
        variant_means = {
            v: report["variants"][v].get("mean", float("-inf"))
            for v in exp.variants
            if report["variants"].get(v, {}).get("n", 0) >= 10
        }
        if len(variant_means) >= 2:
            best = max(variant_means, key=lambda v: variant_means[v])
            control = next(iter(exp.variants))

            # Welch's t-test: does the best variant significantly outperform
            # the control?  Unlike the naïve "diff > pooled_std" approach, this
            # properly accounts for unequal variances and sample sizes.
            control_stats = report["variants"].get(control, {})
            best_stats = report["variants"].get(best, {})
            n1 = control_stats.get("n", 0)
            n2 = best_stats.get("n", 0)
            if n1 > 1 and n2 > 1:
                p_value = _welch_t_test_p(
                    mean1=control_stats.get("mean", 0.0),
                    std1=control_stats.get("std", 0.0),
                    n1=n1,
                    mean2=best_stats.get("mean", 0.0),
                    std2=best_stats.get("std", 0.0),
                    n2=n2,
                )
                report["p_value"] = round(p_value, 4)
                report["significant"] = p_value < 0.05
                if report["significant"]:
                    report["winner"] = best

        return report

    def list_experiments(self) -> list[str]:
        """Return names of all registered experiments."""
        return list(self._experiments.keys())

    def deactivate(self, experiment_name: str) -> None:
        """Stop a running experiment (stops new assignments, keeps data)."""
        exp = self._experiments.get(experiment_name)
        if exp:
            exp.active = False
            logger.info("A/B experiment '%s' deactivated", experiment_name)

    def all_reports(self) -> dict[str, dict]:
        """Analyse all experiments and return a dict of reports."""
        return {name: self.analyse(name) for name in self._experiments}
