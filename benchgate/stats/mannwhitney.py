"""Mann-Whitney U test, implemented from scratch.

Exact null distribution for small, tie-free samples (the common case for
10-vs-10 per-run medians); normal approximation with tie and continuity
correction otherwise. Cross-checked against scipy in tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import math

import numpy as np
from scipy.stats import rankdata


@dataclass(frozen=True)
class MWUResult:
    u: float          # U statistic for sample y ("candidate")
    p_value: float
    method: str       # "exact" or "asymptotic"
    # Common-language effect size: P(Y > X) + 0.5 P(Y == X).
    prob_superiority: float


@lru_cache(maxsize=256)
def _exact_counts(m: int, n: int) -> np.ndarray:
    """Number of arrangements giving each U in [0, m*n] (Mann-Whitney recursion)."""
    # c[i][j] = counts for sizes (i, j); c(i,j,u) = c(i-1,j,u-j) + c(i,j-1,u)
    prev_row = [np.ones(1, dtype=float) for _ in range(n + 1)]  # i = 0
    for i in range(1, m + 1):
        row = [np.ones(1, dtype=float)]  # j = 0
        for j in range(1, n + 1):
            a = prev_row[j]           # c(i-1, j): shift by j
            b = row[j - 1]            # c(i, j-1)
            out = np.zeros(i * j + 1)
            out[j:j + a.size] += a
            out[:b.size] += b
            row.append(out)
        prev_row = row
    return prev_row[n]


def mann_whitney_u(x, y, alternative: str = "greater", exact_max: int = 25) -> MWUResult:
    """Test whether y tends to be larger than x.

    alternative: "greater" (y stochastically larger, i.e. a slowdown when the
    values are latencies), "less", or "two-sided".
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    m, n = x.size, y.size
    if m == 0 or n == 0:
        raise ValueError("both samples must be non-empty")
    ranks = rankdata(np.concatenate([x, y]))
    r_y = ranks[m:].sum()
    u_y = r_y - n * (n + 1) / 2.0
    ps = u_y / (m * n)
    ties = np.unique(ranks, return_counts=True)[1]
    has_ties = bool((ties > 1).any())

    if not has_ties and m <= exact_max and n <= exact_max:
        counts = _exact_counts(m, n)
        total = counts.sum()
        k = int(round(u_y))
        p_ge = counts[k:].sum() / total
        p_le = counts[: k + 1].sum() / total
        if alternative == "greater":
            p = p_ge
        elif alternative == "less":
            p = p_le
        else:
            p = min(1.0, 2 * min(p_ge, p_le))
        return MWUResult(u_y, float(p), "exact", float(ps))

    mu = m * n / 2.0
    N = m + n
    tie_term = (ties ** 3 - ties).sum() / (N * (N - 1))
    sigma = math.sqrt(m * n / 12.0 * ((N + 1) - tie_term))
    if sigma == 0:
        return MWUResult(u_y, 1.0, "asymptotic", float(ps))
    from scipy.stats import norm

    if alternative == "greater":
        z = (u_y - mu - 0.5) / sigma
        p = norm.sf(z)
    elif alternative == "less":
        z = (u_y - mu + 0.5) / sigma
        p = norm.cdf(z)
    else:
        z = (abs(u_y - mu) - 0.5) / sigma
        p = min(1.0, 2 * norm.sf(z))
    return MWUResult(u_y, float(p), "asymptotic", float(ps))
