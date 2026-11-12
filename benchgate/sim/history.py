"""Change-point detection over a simulated commit history.

Each history has 120 commits; each commit is benchmarked with R runs and
summarized as the median of its per-run p50s. Scenarios: clean, a step
regression of 2/5/10% at commit 80, and "creep": ten consecutive commits
each 0.5% slower (5% in total), every one of them below the gate's 1%
min-effect threshold.

Detectors compared:
  pairwise-paired    each commit vs its parent, both on one node (the merge gate)
  pairwise-unpaired  each commit vs its parent's stored runs from another node
  PELT           offline segmentation of the whole history, shifts >= 1% kept
  CUSUM          online, self-starting, h=8 sigma, k=0.5 sigma

    python -m benchgate.sim.history --out docs/results
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import json
import os

import numpy as np

from benchgate.sim.detection import wilson
from benchgate.sim.noise import CLOUD_VM, TUNED, NoiseModel
from benchgate.stats.bootstrap import bootstrap_relative_change
from benchgate.stats.changepoint import cusum, pelt, segments
from benchgate.stats.mannwhitney import mann_whitney_u

N_COMMITS = 120
ONSET = 80
CREEP_START, CREEP_LEN, CREEP_STEP = 70, 10, 0.005


def level(scenario: str) -> np.ndarray:
    lv = np.zeros(N_COMMITS)
    if scenario.startswith("step"):
        lv[ONSET:] = float(scenario[4:]) / 100
    elif scenario == "creep":
        for i in range(CREEP_LEN):
            lv[CREEP_START + i:] += CREEP_STEP
    return lv


def window(scenario: str) -> tuple[int, int]:
    """Commit indices where a flag counts as a detection."""
    if scenario == "creep":
        return CREEP_START - 3, CREEP_START + CREEP_LEN + 15
    return ONSET - 3, ONSET + 15


# Node-to-node offset: each commit lands on one of N_NODES fleet nodes, which
# are never perfectly identical. The paired gate is immune to this (base and
# head run on the same node); history across commits is not.
N_NODES = 8
SIGMA_NODE = {"tuned-bare-metal": 0.003, "untuned-cloud-vm": 0.01}


def simulate_history(model: NoiseModel, scenario: str, rng, R: int = 10, n_iter: int = 150):
    lv = level(scenario)
    node_off = np.exp(rng.normal(0, SIGMA_NODE.get(model.name, 0.01), N_NODES))
    per_run = np.empty((N_COMMITS, R))
    same_node = np.empty((N_COMMITS, R))
    for c in range(N_COMMITS):
        med = np.median(model.runs(R, n_iter, rng, slowdown=lv[c]), axis=1)
        same_node[c] = med
        per_run[c] = med * node_off[rng.integers(N_NODES)]
    # same_node: what a paired job sees (parent and commit on one node).
    return per_run, same_node


def pairwise_flags(per_run, rng, alpha=0.01, min_effect=0.01) -> list[int]:
    flags = []
    for c in range(1, per_run.shape[0]):
        b, h = per_run[c - 1], per_run[c]
        if mann_whitney_u(b, h, "greater").p_value >= alpha:
            continue
        ci = bootstrap_relative_change(b, h, n_boot=1000, rng=rng)
        if ci.low > 0 and ci.estimate >= min_effect:
            flags.append(c)
    return flags


def pelt_flags(series, min_effect=0.01) -> list[int]:
    rel = series / np.median(series[:20])
    segs = segments(rel, pelt(rel))
    return [s.start for p, s in zip(segs, segs[1:]) if s.mean / p.mean - 1 >= min_effect]


def cusum_flags(series) -> list[int]:
    rel = series / np.median(series[:20])
    return [a.index for a in cusum(rel, h=8.0, k=0.5)]


DETECTORS = ("pairwise-paired", "pairwise-unpaired", "pelt", "cusum")


def _cell(args):
    model, scenario, trials, seed = args
    rng = np.random.default_rng(seed)
    lo, hi = window(scenario)
    res = {d: {"detected": 0, "false_alarm_histories": 0, "false_flags": 0, "delays": []}
           for d in DETECTORS}
    for _ in range(trials):
        per_run, same_node = simulate_history(model, scenario, rng)
        series = np.median(per_run, axis=1)
        flags = {"pairwise-paired": pairwise_flags(same_node, rng),
                 "pairwise-unpaired": pairwise_flags(per_run, rng),
                 "pelt": pelt_flags(series), "cusum": cusum_flags(series)}
        for d, fl in flags.items():
            inside = [f for f in fl if lo <= f <= hi] if scenario != "clean" else []
            outside = [f for f in fl if not (lo <= f <= hi)] if scenario != "clean" else fl
            r = res[d]
            r["false_flags"] += len(outside)
            r["false_alarm_histories"] += bool(outside)
            if inside:
                r["detected"] += 1
                onset = CREEP_START if scenario == "creep" else ONSET
                r["delays"].append(min(inside) - onset)
    out = []
    for d, r in res.items():
        k = r["detected"]
        out.append({
            "model": model.name, "scenario": scenario, "detector": d, "trials": trials,
            "detection_rate": k / trials if scenario != "clean" else None,
            "detection_ci95": wilson(k, trials) if scenario != "clean" else None,
            "false_alarm_history_rate": r["false_alarm_histories"] / trials,
            "false_flags_per_1000_commits": 1000 * r["false_flags"] / (trials * N_COMMITS),
            "median_delay": float(np.median(r["delays"])) if r["delays"] else None,
        })
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="docs/results")
    p.add_argument("--trials", type=int, default=200)
    args = p.parse_args()
    scenarios = ["clean", "step2", "step5", "step10", "creep"]
    cells = [(m, s, args.trials, 1000 + i)
             for i, (m, s) in enumerate((m, s) for m in (TUNED, CLOUD_VM) for s in scenarios)]
    with ProcessPoolExecutor() as ex:
        rows = [r for chunk in ex.map(_cell, cells) for r in chunk]
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "changepoint.json"), "w") as f:
        json.dump(rows, f, indent=2)

    L = ["# Change-point detection over commit history", "",
         f"Generated by `python -m benchgate.sim.history` ({args.trials} histories per cell, "
         f"{N_COMMITS} commits each, 10 runs x 150 iterations per commit, each commit on one of "
         f"{N_NODES} nodes with a per-node offset of sd 0.3% (tuned) / 1% (cloud)). Detection = a flag "
         "within [onset-3, onset+15] (creep: through the end of the creep + 15). False flags "
         "are flags anywhere else. Delay is in commits from onset to the first flag "
         "(for PELT, which is offline, it is the localisation error).", "",
         "`pairwise-paired` is the merge gate (parent and commit interleaved on one node); "
         "`pairwise-unpaired` reuses the parent's stored runs from whichever node it ran on. "
         "PELT and CUSUM run on the stored per-commit series, which does include node offsets.",
         ""]
    for m in dict.fromkeys(r["model"] for r in rows):
        L += [f"## {m}", "",
              "| detector | false-alarm histories (clean) | false flags / 1000 commits (clean) "
              "| step 2% | step 5% | step 10% | creep 10x0.5% | median delay (step 5%) |",
              "|---|---|---|---|---|---|---|---|"]
        for d in DETECTORS:
            g = {r["scenario"]: r for r in rows if r["model"] == m and r["detector"] == d}
            c = g["clean"]
            det = [f"{g[s]['detection_rate'] * 100:.1f}%" for s in ("step2", "step5", "step10", "creep")]
            delay = g["step5"]["median_delay"]
            L.append(f"| {d} | {c['false_alarm_history_rate'] * 100:.1f}% | "
                     f"{c['false_flags_per_1000_commits']:.2f} | " + " | ".join(det) +
                     f" | {'–' if delay is None else f'{delay:+.0f}'} |")
        L.append("")
    with open(os.path.join(args.out, "changepoint.md"), "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
