"""Bootstrap confidence intervals for the relative change between two groups.

The resampling unit is the *run*, not the iteration: iterations inside one
process share CPU frequency, memory layout, page placement and neighbours, so
treating 500 iterations as 500 independent samples gives absurdly narrow
intervals. Each run is first reduced to a statistic (its median or p99) and
the bootstrap is over those per-run values.

Implements percentile and BCa (bias-corrected and accelerated) intervals,
fully vectorized so the simulation can afford thousands of comparisons.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.stats import norm


@dataclass(frozen=True)
class BootstrapCI:
    estimate: float  # relative change, e.g. 0.05 == candidate 5% slower
    low: float
    high: float
    confidence: float
    method: str

    def contains(self, v: float) -> bool:
        return self.low <= v <= self.high


def _rel_change(b: np.ndarray, c: np.ndarray, center) -> np.ndarray:
    return center(c, axis=-1) / center(b, axis=-1) - 1.0


def bootstrap_relative_change(
    base,
    cand,
    *,
    confidence: float = 0.95,
    n_boot: int = 4000,
    method: str = "bca",
    center=np.median,
    rng: np.random.Generator | None = None,
) -> BootstrapCI:
    """CI for center(cand)/center(base) - 1 by resampling runs within each group."""
    b = np.asarray(base, dtype=float)
    c = np.asarray(cand, dtype=float)
    if b.size < 2 or c.size < 2:
        raise ValueError("need at least 2 runs per group")
    rng = rng or np.random.default_rng()
    theta = float(_rel_change(b, c, center))

    bi = rng.integers(0, b.size, size=(n_boot, b.size))
    ci = rng.integers(0, c.size, size=(n_boot, c.size))
    boots = _rel_change(b[bi], c[ci], center)

    alpha = 1 - confidence
    if method == "percentile":
        lo, hi = np.quantile(boots, [alpha / 2, 1 - alpha / 2])
        return BootstrapCI(theta, float(lo), float(hi), confidence, method)
    if method != "bca":
        raise ValueError(f"unknown method {method!r}")

    # Bias correction: fraction of bootstrap replicates below the estimate.
    prop = (np.sum(boots < theta) + 0.5 * np.sum(boots == theta)) / n_boot
    prop = np.clip(prop, 1.0 / (n_boot + 1), n_boot / (n_boot + 1))
    z0 = norm.ppf(prop)

    # Acceleration from the two-sample jackknife (leave one run out of either group).
    jack = []
    for g, other, is_base in ((b, c, True), (c, b, False)):
        idx = np.arange(g.size)
        loo = np.stack([g[idx != i] for i in idx])  # (n, n-1)
        if is_base:
            jack.append(_rel_change(loo, np.broadcast_to(other, (g.size, other.size)), center))
        else:
            jack.append(_rel_change(np.broadcast_to(other, (g.size, other.size)), loo, center))
    num = 0.0
    den = 0.0
    for j in jack:
        d = j.mean() - j
        num += np.sum(d ** 3) / j.size ** 3
        den += np.sum(d ** 2) / j.size ** 2
    a = num / (6.0 * den ** 1.5) if den > 0 else 0.0

    z = norm.ppf([alpha / 2, 1 - alpha / 2])
    adj = norm.cdf(z0 + (z0 + z) / (1 - a * (z0 + z)))
    lo, hi = np.quantile(boots, adj)
    return BootstrapCI(theta, float(lo), float(hi), confidence, method)
