"""The merge gate decision.

Inputs are *runs* (independent process invocations), each a vector of
per-iteration latencies. For each (workload, metric) pair:

1. Reduce each run to one number (its p50 or p99).
2. One-sided Mann-Whitney U on those per-run values: is the candidate slower?
3. BCa bootstrap CI of the relative change of the median run-statistic.
4. Holm-Bonferroni across every test in the gate, so adding workloads or
   metrics does not silently raise the false-alarm rate.

A test is a REGRESSION only if it is statistically significant after Holm,
the CI excludes zero, AND the point estimate exceeds ``min_effect``: a
"significant" 0.3% change is real but not worth blocking a merge over.
When nothing is significant but the CI still admits a large slowdown, the
verdict is INCONCLUSIVE: the honest answer is "need more runs", not "pass".
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import enum
from typing import Mapping, Sequence

import numpy as np

from benchgate.stats.bootstrap import BootstrapCI, bootstrap_relative_change
from benchgate.stats.mannwhitney import mann_whitney_u


class Verdict(str, enum.Enum):
    PASS = "pass"
    REGRESSION = "regression"
    IMPROVEMENT = "improvement"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class GateConfig:
    alpha: float = 0.01            # family-wise error rate across all tests
    min_effect: float = 0.01       # smallest relative slowdown worth blocking
    inconclusive_effect: float = 0.05  # CI upper bound above this => can't rule out
    confidence: float = 0.95
    metrics: tuple[str, ...] = ("p50", "p99")
    n_boot: int = 4000
    block_on_inconclusive: bool = False


METRIC_FUNCS = {
    "p50": lambda a: float(np.median(a)),
    "p90": lambda a: float(np.percentile(a, 90)),
    "p99": lambda a: float(np.percentile(a, 99)),
    "mean": lambda a: float(np.mean(a)),
}


def run_statistics(runs: Sequence[Sequence[float]], metric: str) -> np.ndarray:
    f = METRIC_FUNCS[metric]
    return np.array([f(np.asarray(r, dtype=float)) for r in runs])


def holm(pvals: Sequence[float]) -> np.ndarray:
    """Holm-Bonferroni adjusted p-values (monotone, capped at 1)."""
    p = np.asarray(pvals, dtype=float)
    m = p.size
    order = np.argsort(p)
    adj = np.empty(m)
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, (m - rank) * p[i])
        adj[i] = min(1.0, running)
    return adj


@dataclass
class MetricVerdict:
    workload: str
    metric: str
    n_base: int
    n_cand: int
    base_median: float
    cand_median: float
    ci: BootstrapCI
    p_value: float           # raw one-sided MWU p (candidate slower)
    p_value_improve: float   # raw one-sided MWU p (candidate faster)
    p_adjusted: float = 1.0
    verdict: Verdict = Verdict.PASS

    def to_dict(self) -> dict:
        d = asdict(self)
        d["verdict"] = self.verdict.value
        return d


@dataclass
class GateResult:
    verdict: Verdict
    tests: list[MetricVerdict] = field(default_factory=list)
    config: GateConfig = field(default_factory=GateConfig)

    @property
    def blocking(self) -> bool:
        return self.verdict == Verdict.REGRESSION or (
            self.verdict == Verdict.INCONCLUSIVE and self.config.block_on_inconclusive)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "blocking": self.blocking,
            "config": asdict(self.config),
            "tests": [t.to_dict() for t in self.tests],
        }


def compare_runs(workload: str, base_runs, cand_runs, metric: str,
                 cfg: GateConfig, rng: np.random.Generator | None = None) -> MetricVerdict:
    b = run_statistics(base_runs, metric)
    c = run_statistics(cand_runs, metric)
    ci = bootstrap_relative_change(b, c, confidence=cfg.confidence,
                                   n_boot=cfg.n_boot, rng=rng)
    return MetricVerdict(
        workload=workload, metric=metric, n_base=b.size, n_cand=c.size,
        base_median=float(np.median(b)), cand_median=float(np.median(c)), ci=ci,
        p_value=mann_whitney_u(b, c, "greater").p_value,
        p_value_improve=mann_whitney_u(b, c, "less").p_value,
    )


def evaluate(base: Mapping[str, Sequence[Sequence[float]]],
             cand: Mapping[str, Sequence[Sequence[float]]],
             cfg: GateConfig = GateConfig(),
             rng: np.random.Generator | None = None) -> GateResult:
    """base/cand map workload -> list of runs (each a list of iteration latencies)."""
    rng = rng or np.random.default_rng(0)
    tests = [compare_runs(w, base[w], cand[w], m, cfg, rng)
             for w in sorted(set(base) & set(cand)) for m in cfg.metrics]
    if not tests:
        return GateResult(Verdict.INCONCLUSIVE, [], cfg)
    # Regression and improvement are separate one-sided families; each is
    # Holm-corrected on its own.
    adj_reg = holm([t.p_value for t in tests])
    adj_imp = holm([t.p_value_improve for t in tests])
    for t, pr, pi in zip(tests, adj_reg, adj_imp):
        t.p_adjusted = float(pr)
        if pr < cfg.alpha and t.ci.low > 0 and t.ci.estimate >= cfg.min_effect:
            t.verdict = Verdict.REGRESSION
        elif pi < cfg.alpha and t.ci.high < 0 and t.ci.estimate <= -cfg.min_effect:
            t.verdict = Verdict.IMPROVEMENT
        elif t.ci.high > cfg.inconclusive_effect:
            t.verdict = Verdict.INCONCLUSIVE
        else:
            t.verdict = Verdict.PASS
    return GateResult(overall(tests), tests, cfg)


def overall(tests: Sequence[MetricVerdict]) -> Verdict:
    """Worst verdict wins: regression > inconclusive > improvement > pass."""
    verdicts = {t.verdict for t in tests}
    for v in (Verdict.REGRESSION, Verdict.INCONCLUSIVE, Verdict.IMPROVEMENT):
        if v in verdicts:
            return v
    return Verdict.PASS
