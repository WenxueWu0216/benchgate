"""Measure the gate: detection rate for injected regressions and the false
alarm rate on clean (A/A) comparisons.

Two independent evaluations:

1. Synthetic: hierarchical noise model (sim/noise.py) for a tuned bare-metal
   node, an untuned cloud VM, and a model fitted to real runs collected on
   the development sandbox.
2. Empirical: the real C++ workloads on the sandbox, with real injected
   slowdowns. Comparisons are formed by drawing disjoint sets of runs from
   a pool (120 clean + 40 per injected condition per workload).

    python -m benchgate.sim.detection --out docs/results
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import gzip
import json
import math
import os
import time

import numpy as np

from benchgate.sim.noise import CLOUD_VM, TUNED, NoiseModel, fit_from_runs
from benchgate.stats.gate import GateConfig, Verdict, evaluate

EFFECTS = [0.0, 0.02, 0.05, 0.10]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def _cfg(metrics=("p50", "p99")) -> GateConfig:
    return GateConfig(metrics=metrics, n_boot=2000)


def _one_cell(args) -> dict:
    model, effect, mode, n_runs, n_iter, trials, seed = args
    rng = np.random.default_rng(seed)
    cfg = _cfg()
    flagged = 0
    verdicts = {v.value: 0 for v in Verdict}
    for _ in range(trials):
        base = model.runs(n_runs, n_iter, rng)
        head = model.runs(n_runs, n_iter, rng, slowdown=effect, mode=mode)
        res = evaluate({"w": base}, {"w": head}, cfg, rng)
        verdicts[res.verdict.value] += 1
        flagged += res.verdict == Verdict.REGRESSION
    lo, hi = wilson(flagged, trials)
    return {"model": model.name, "effect": effect, "mode": mode, "n_runs": n_runs,
            "n_iter": n_iter, "trials": trials, "flagged": flagged,
            "rate": flagged / trials, "ci95": [lo, hi], "verdicts": verdicts}


def synthetic(models: list[NoiseModel], n_runs_list=(5, 10, 20), n_iter=300,
              clean_trials=1000, effect_trials=300, seed=1, workers=None) -> list[dict]:
    cells = []
    s = seed
    for m in models:
        for n_runs in n_runs_list:
            for mode in ("uniform", "tail"):
                for e in EFFECTS:
                    if e == 0 and mode == "tail":
                        continue  # A/A is mode-independent
                    trials = clean_trials if e == 0 else effect_trials
                    cells.append((m, e, mode, n_runs, n_iter, trials, s))
                    s += 1
    with ProcessPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(_one_cell, cells))


def _confirm_cell(args) -> dict:
    model, effect, n_runs, n_iter, trials, seed = args
    rng = np.random.default_rng(seed)
    cfg = _cfg()
    single = confirmed = 0
    for _ in range(trials):
        r1 = evaluate({"w": model.runs(n_runs, n_iter, rng)},
                      {"w": model.runs(n_runs, n_iter, rng, slowdown=effect)}, cfg, rng)
        if r1.verdict != Verdict.REGRESSION:
            continue
        single += 1
        r2 = evaluate({"w": model.runs(n_runs, n_iter, rng)},
                      {"w": model.runs(n_runs, n_iter, rng, slowdown=effect)}, cfg, rng)
        confirmed += r2.verdict == Verdict.REGRESSION
    return {"model": model.name, "effect": effect, "n_runs": n_runs, "trials": trials,
            "single_rate": single / trials, "confirmed_rate": confirmed / trials,
            "single_flagged": single, "confirmed_flagged": confirmed,
            "confirmed_ci95": wilson(confirmed, trials)}


def confirmation(models, n_runs=10, n_iter=300, clean_trials=5000, effect_trials=300,
                 seed=500, workers=None) -> list[dict]:
    """Block only if a second, independent batch also says REGRESSION."""
    cells = [(m, e, n_runs, n_iter, clean_trials if e == 0 else effect_trials, seed + i)
             for i, (m, e) in enumerate((m, e) for m in models for e in EFFECTS)]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(_confirm_cell, cells))


def budget_allocation(model: NoiseModel, total_iters: int = 3000,
                      splits=((5, 600), (10, 300), (20, 150), (30, 100)),
                      effects=(0.0, 0.02, 0.05), trials: int = 300, seed: int = 99,
                      workers=None) -> list[dict]:
    """Same node-time per side, split into more short runs or fewer long ones."""
    cells = []
    for i, (n_runs, n_iter) in enumerate(splits):
        assert n_runs * n_iter == total_iters
        for j, e in enumerate(effects):
            cells.append((model, e, "uniform", n_runs, n_iter, trials, seed + 10 * i + j))
    with ProcessPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(_one_cell, cells))


def load_pool(path: str) -> tuple[dict, dict]:
    pool: dict = {}
    meta = {}
    with gzip.open(path, "rt") as f:
        for line in f:
            d = json.loads(line)
            if d["kind"] == "meta":
                meta = d
                continue
            key = (d["workload"], d["mode"], d["pct"])
            pool.setdefault(key, []).append(np.asarray(d["iteration_ns"], dtype=float))
    return pool, meta


def empirical(pool: dict, n_runs=10, trials=200, seed=2) -> list[dict]:
    rng = np.random.default_rng(seed)
    cfg = _cfg()
    out = []
    workloads = sorted({k[0] for k in pool})
    conditions = sorted({(k[1], k[2]) for k in pool if k[1] != "none"})
    for w in workloads:
        clean = pool[(w, "none", 0.0)]
        rows = [("none", 0.0)] + conditions
        for mode, pct in rows:
            flagged = 0
            for _ in range(trials):
                if mode == "none":
                    idx = rng.choice(len(clean), 2 * n_runs, replace=False)
                    base = [clean[i] for i in idx[:n_runs]]
                    head = [clean[i] for i in idx[n_runs:]]
                else:
                    slow = pool[(w, mode, pct)]
                    base = [clean[i] for i in rng.choice(len(clean), n_runs, replace=False)]
                    head = [slow[i] for i in rng.choice(len(slow), n_runs, replace=False)]
                res = evaluate({w: base}, {w: head}, cfg, rng)
                flagged += res.verdict == Verdict.REGRESSION
            lo, hi = wilson(flagged, trials)
            out.append({"workload": w, "mode": mode, "effect": pct / 100, "n_runs": n_runs,
                        "trials": trials, "flagged": flagged, "rate": flagged / trials,
                        "ci95": [lo, hi], "pool_clean": len(clean),
                        "pool_slow": len(pool.get((w, mode, pct), [])) if mode != "none" else None})
    return out


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def write_report(syn: list[dict], emp: list[dict], fitted: list[NoiseModel], meta: dict,
                 out_dir: str, elapsed: float, budget: list[dict] | None = None,
                 confirm: list[dict] | None = None) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "detection.json"), "w") as f:
        json.dump({"synthetic": syn, "empirical": emp, "budget": budget or [],
                   "confirmation": confirm or [],
                   "fitted_models": [m.__dict__ for m in fitted],
                   "sandbox_env": meta.get("env")}, f, indent=2)

    L = ["# Gate detection / false-alarm results", "",
         "Generated by `python -m benchgate.sim.detection` "
         f"({elapsed / 60:.1f} min). Gate: one-sided Mann-Whitney U on per-run p50 and p99, "
         "Holm-corrected at α=0.01, plus a 95% BCa bootstrap CI that must exclude 0 and a "
         "point estimate ≥ 1%. Rates are fractions of comparisons whose verdict was "
         "REGRESSION, with Wilson 95% intervals.", ""]

    L += ["## 1. Synthetic noise models", "",
          "| model | runs/side | false alarms (A/A) | 2% | 5% | 10% | tail 2% | tail 5% | tail 10% |",
          "|---|---:|---|---|---|---|---|---|---|"]
    by = {(r["model"], r["n_runs"], r["mode"], r["effect"]): r for r in syn}
    for m in dict.fromkeys(r["model"] for r in syn):
        for n in sorted({r["n_runs"] for r in syn}):
            aa = by[(m, n, "uniform", 0.0)]
            cells = [f"{aa['flagged']}/{aa['trials']} ({_pct(aa['rate'])})"]
            for mode in ("uniform", "tail"):
                for e in EFFECTS[1:]:
                    r = by[(m, n, mode, e)]
                    cells.append(f"{_pct(r['rate'])}")
            L.append(f"| {m} | {n} | " + " | ".join(cells) + " |")
    L += ["", "Detection columns are out of 300 comparisons each; A/A out of 1000. "
          "\"tail X%\" stretches only iterations above the p95 by X%, so p99 moves by ~X% "
          "while p50 does not move.", ""]

    L += ["### Fitted noise models (from real sandbox runs)", "",
          "| model | sigma_run (run-to-run) | p(contaminated run) | contamination mean | sigma_iter | p(spike) |",
          "|---|---:|---:|---:|---:|---:|"]
    for m in fitted:
        L.append(f"| {m.name} | {_pct(m.sigma_run)} | {_pct(m.p_contaminated)} | "
                 f"{_pct(m.contamination_mean)} | {m.sigma_iter:.3f} | {m.p_spike:.4f} |")

    L += ["", "## 2. Empirical: real workloads, real injected slowdowns (dev sandbox)", "",
          f"Machine: {meta.get('env', {}).get('cpu_model')} ({meta.get('env', {}).get('nproc')} vCPU "
          "cloud sandbox, no tuning possible: no cpufreq control, no isolcpus, shared host). "
          "10 runs per side; each comparison draws disjoint runs from the pool. Because the "
          "200 comparisons reuse a finite pool (120 clean runs per workload), they are not "
          "independent; the intervals are optimistic.", "",
          "| workload | A/A false alarms | uniform 2% | uniform 5% | uniform 10% | tail 5% | tail 10% |",
          "|---|---|---|---|---|---|---|"]
    eb = {(r["workload"], r["mode"], r["effect"]): r for r in emp}
    tot = {}
    for w in sorted({r["workload"] for r in emp}):
        cells = []
        for key in (("none", 0.0), ("uniform", 0.02), ("uniform", 0.05), ("uniform", 0.10),
                    ("tail", 0.05), ("tail", 0.10)):
            r = eb.get((w, *key))
            if r is None:
                cells.append("–")
                continue
            t = tot.setdefault(key, [0, 0])
            t[0] += r["flagged"]
            t[1] += r["trials"]
            cells.append(f"{r['flagged']}/{r['trials']} ({_pct(r['rate'])})")
        L.append(f"| {w} | " + " | ".join(cells) + " |")
    L.append("| **all** | " + " | ".join(
        f"**{tot[k][0]}/{tot[k][1]} ({_pct(tot[k][0] / tot[k][1])})**" for k in tot) + " |")
    if budget:
        L += ["", "## 3. Fixed budget: many short runs or few long runs?", "",
              f"Model: {budget[0]['model']}; every row spends the same "
              f"{budget[0]['n_runs'] * budget[0]['n_iter']} measured iterations per side.", "",
              "| runs x iterations | A/A false alarms | 2% | 5% |", "|---|---|---|---|"]
        bb = {}
        for r in budget:
            bb.setdefault((r["n_runs"], r["n_iter"]), {})[r["effect"]] = r
        for (n, it), d in bb.items():
            L.append(f"| {n} x {it} | {_pct(d[0.0]['rate'])} | {_pct(d[0.02]['rate'])} | "
                     f"{_pct(d[0.05]['rate'])} |")
    if confirm:
        L += ["", "## 4. Confirmation re-run before blocking", "",
              "The service re-runs a fresh paired batch for any workload flagged as a "
              "regression and blocks only if it is flagged again (10 runs/side per batch). "
              f"A/A rows use {confirm[0]['trials'] if confirm[0]['effect'] == 0 else ''} "
              "comparisons; detection rows 300.", "",
              "| model | effect | single batch | with confirmation |", "|---|---:|---:|---:|"]
        for r in confirm:
            L.append(f"| {r['model']} | {'A/A' if r['effect'] == 0 else _pct(r['effect'])} | "
                     f"{r['single_flagged']}/{r['trials']} ({_pct(r['single_rate'])}) | "
                     f"{r['confirmed_flagged']}/{r['trials']} ({_pct(r['confirmed_rate'])}, "
                     f"95% CI ≤ {_pct(r['confirmed_ci95'][1])}) |")
    with open(os.path.join(out_dir, "detection.md"), "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="docs/results")
    p.add_argument("--pool", default="data/calibration/sandbox_runs.jsonl.gz")
    p.add_argument("--clean-trials", type=int, default=1000)
    p.add_argument("--effect-trials", type=int, default=300)
    p.add_argument("--empirical-trials", type=int, default=200)
    p.add_argument("--quick", action="store_true")
    args = p.parse_args()
    if args.quick:
        args.clean_trials, args.effect_trials, args.empirical_trials = 100, 50, 30
    t0 = time.time()
    pool, meta = load_pool(args.pool)
    fitted = [fit_from_runs(f"sandbox-fit-{w}", pool[(w, "none", 0.0)])
              for w in ("pointcloud", "matrix")]
    # Normalize base latency; detection depends only on relative noise.
    fitted = [NoiseModel(**{**m.__dict__, "base_ns": 1e6}) for m in fitted]
    syn = synthetic([TUNED, CLOUD_VM, fitted[1]], clean_trials=args.clean_trials,
                    effect_trials=args.effect_trials)
    emp = empirical(pool, trials=args.empirical_trials)
    budget = budget_allocation(CLOUD_VM, trials=args.effect_trials)
    confirm = confirmation([TUNED, CLOUD_VM], clean_trials=5 * args.clean_trials,
                           effect_trials=args.effect_trials)
    write_report(syn, emp, fitted, meta, args.out, time.time() - t0, budget, confirm)


if __name__ == "__main__":
    main()
