"""Hierarchical latency noise model used by the detection simulations.

latency[run r, iteration i] =
    base_i                       # lognormal per-iteration variation (sigma_iter)
  * exp(run_offset_r)            # per-process effect: frequency, ASLR/layout, cache colouring
  * (1 + contamination_r,i)      # noisy-neighbour episodes hitting some runs
  + spike_r,i                    # interrupts / SMIs / page faults: rare, heavy-tailed

The run-level term is what makes iterations within a run correlated; it is
the dominant term on real machines and the reason the gate tests per-run
statistics instead of pooling iterations.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class NoiseModel:
    name: str
    base_ns: float = 1.0e6
    sigma_iter: float = 0.10        # sd of log latency within a run
    sigma_run: float = 0.015        # sd of log per-run offset
    p_contaminated: float = 0.0     # probability a run is hit by a noisy neighbour
    contamination_mean: float = 0.08  # mean slowdown of contaminated iterations
    contamination_frac: float = 0.5   # fraction of iterations a contaminated run loses
    p_spike: float = 0.002          # per-iteration interrupt spike probability
    spike_scale: float = 0.5        # spike size relative to base (Pareto, alpha=2)

    def runs(self, n_runs: int, n_iter: int, rng: np.random.Generator,
             slowdown: float = 0.0, mode: str = "uniform") -> np.ndarray:
        base = self.base_ns * np.exp(rng.normal(0, self.sigma_iter, (n_runs, n_iter)))
        if slowdown and mode == "tail":
            # Same rule as the C++ harness: stretch iterations above the
            # (warmup-estimated) p95 of the undisturbed distribution.
            thresh = self.base_ns * np.exp(1.6449 * self.sigma_iter)
            base = np.where(base > thresh, base * (1 + slowdown), base)
        elif slowdown:
            base = base * (1 + slowdown)
        x = base * np.exp(rng.normal(0, self.sigma_run, (n_runs, 1)))
        if self.p_contaminated > 0:
            hit = rng.random((n_runs, 1)) < self.p_contaminated
            mag = rng.exponential(self.contamination_mean, (n_runs, 1))
            frac = rng.random((n_runs, n_iter)) < self.contamination_frac
            x = x * (1 + hit * frac * mag)
        if self.p_spike > 0:
            sp = rng.random((n_runs, n_iter)) < self.p_spike
            x = x + sp * self.base_ns * self.spike_scale * (rng.pareto(2.0, (n_runs, n_iter)) + 1)
        return x


# A bare-metal node with isolcpus/nohz_full, fixed frequency, SMT off and
# IRQs pinned away (literature + vendor guidance: run-to-run CV of ~0.3-0.5%).
TUNED = NoiseModel("tuned-bare-metal", sigma_iter=0.03, sigma_run=0.004,
                   p_contaminated=0.0, p_spike=0.0005, spike_scale=0.2)

# An untuned dedicated cloud VM: frequency scaling, shared L3, occasional
# neighbour activity.
CLOUD_VM = NoiseModel("untuned-cloud-vm", sigma_iter=0.10, sigma_run=0.015,
                      p_contaminated=0.05, contamination_mean=0.08, p_spike=0.003)


def fit_from_runs(name: str, runs: list[np.ndarray]) -> NoiseModel:
    """Fit the model to real clean runs (robustly: outlier runs become
    'contamination' rather than inflating sigma_run)."""
    med = np.array([np.median(r) for r in runs])
    logm = np.log(med)
    center = np.median(logm)
    sigma_run = 1.4826 * np.median(np.abs(logm - center))
    out = logm - center > 3 * sigma_run
    p_cont = float(out.mean())
    cont_mean = float(np.mean(np.exp(logm[out] - center) - 1)) if out.any() else 0.0
    within = float(np.median([1.4826 * np.median(np.abs(np.log(r) - np.median(np.log(r))))
                              for r in runs]))
    # Spikes: iterations beyond 4 robust sd of their run.
    z = [np.log(r) - np.median(np.log(r)) for r in runs]
    p_spike = float(np.mean([np.mean(zz > 4 * within) for zz in z]))
    return NoiseModel(name, base_ns=float(np.median(med)), sigma_iter=within,
                      sigma_run=float(sigma_run), p_contaminated=p_cont,
                      contamination_mean=cont_mean, contamination_frac=1.0,
                      p_spike=p_spike, spike_scale=1.0)
