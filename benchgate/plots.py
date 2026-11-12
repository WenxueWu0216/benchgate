"""Verdict plot: per (workload, metric), every run of base and head as dots,
normalized to the base median, with the bootstrap CI of the change."""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from benchgate.stats.gate import GateResult, run_statistics  # noqa: E402

COLORS = {"pass": "#2f7d32", "regression": "#c62828", "improvement": "#1565c0",
          "inconclusive": "#ef6c00"}


def verdict_plot(result: GateResult, base_runs: dict, head_runs: dict, path: str,
                 title: str = "") -> str:
    tests = result.tests
    n = max(1, len(tests))
    cols = min(n, 3)
    rows = int(np.ceil(n / cols))
    fig, axes = plt.subplots(rows, cols, figsize=(4.2 * cols, 3.4 * rows), squeeze=False)
    rng = np.random.default_rng(0)
    for ax, t in zip(axes.flat, tests):
        b = run_statistics(base_runs[t.workload], t.metric)
        h = run_statistics(head_runs[t.workload], t.metric)
        ref = np.median(b)
        for i, (vals, lab) in enumerate(((b, "base"), (h, "head"))):
            x = i + rng.uniform(-0.12, 0.12, vals.size)
            ax.scatter(x, (vals / ref - 1) * 100, s=16, alpha=0.75,
                       color="#607d8b" if i == 0 else COLORS[t.verdict.value], label=lab)
            ax.hlines((np.median(vals) / ref - 1) * 100, i - 0.25, i + 0.25, color="black", lw=1.5)
        ax.axhline(0, color="#999", lw=0.8, ls="--")
        ax.set_xticks([0, 1], ["base", "head"])
        ax.set_ylabel(f"{t.metric} vs base median (%)")
        ax.set_title(f"{t.workload} {t.metric}: {t.verdict.value.upper()}",
                     color=COLORS[t.verdict.value], fontsize=10)
        ax.text(0.02, 0.97,
                f"Δ {t.ci.estimate * 100:+.2f}%\n95% CI [{t.ci.low * 100:+.2f}, {t.ci.high * 100:+.2f}]\n"
                f"p(adj) {t.p_adjusted:.2g}  n={t.n_base}/{t.n_cand}",
                transform=ax.transAxes, va="top", fontsize=8, family="monospace",
                bbox={"facecolor": "white", "alpha": 0.85, "edgecolor": "#ccc", "pad": 2})
    for ax in list(axes.flat)[len(tests):]:
        ax.axis("off")
    fig.suptitle(title or f"BenchGate verdict: {result.verdict.value.upper()}",
                 color=COLORS[result.verdict.value], fontweight="bold")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path
