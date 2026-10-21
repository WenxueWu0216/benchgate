"""Capacity model: how many bench nodes, and how many repetitions per job?

Discrete-event simulation of the fleet as a FIFO multi-server queue:

* Gate requests arrive as a non-homogeneous Poisson process with a weekday
  diurnal profile (most pushes land between 09:00 and 19:00).
* Each request fans out into one paired job per workload. Jobs from one
  request may run in parallel on different nodes; a node runs one job at a
  time (leases).
* Job service time = pod/setup overhead + repetitions x 2 arms x run time,
  lognormal with CV 0.25; a small fraction of attempts fail and are retried.
* CI wait = request arrival -> last of its jobs finished.

Validated against Erlang C (M/M/c) in tests. The recommendation combines the
queueing result with the detection power from sim/detection.py.

    python -m benchgate.capacity.queue_sim --out docs/results
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import heapq
import json
import math
import os

import numpy as np

DAY = 86400.0


@dataclass(frozen=True)
class Workload:
    requests_per_weekday: float = 150.0   # PR pushes needing a verdict
    weekend_factor: float = 0.2
    business_share: float = 0.8           # share of weekday traffic in 09-19h
    workloads_per_request: int = 6
    repetitions: int = 10
    run_s: float = 15.0                   # one invocation of one workload
    overhead_s: float = 45.0              # scheduling, image pull (warm), setup
    service_cv: float = 0.25
    p_retry: float = 0.02


@dataclass(frozen=True)
class Costs:
    node_hour_usd: float = 1.00   # dedicated 16-vCPU VM / small bare-metal, committed-use price
    budget_month_usd: float = 6000.0


def rate_at(t: float, w: Workload) -> float:
    """Arrival rate (requests/s) at time t (t=0 is Monday 00:00)."""
    day = int(t // DAY) % 7
    hour = (t % DAY) / 3600
    per_day = w.requests_per_weekday * (w.weekend_factor if day >= 5 else 1.0)
    if 9 <= hour < 19:
        return per_day * w.business_share / (10 * 3600)
    return per_day * (1 - w.business_share) / (14 * 3600)


def max_rate(w: Workload) -> float:
    return max(rate_at(h * 3600, w) for h in range(24))


def mean_service_s(w: Workload) -> float:
    return (w.overhead_s + w.repetitions * 2 * w.run_s) * (1 + w.p_retry)


def simulate(nodes: int, w: Workload, days: int = 28, seed: int = 0,
             service_override=None, arrival_override=None) -> dict:
    """Returns wait/turnaround stats. *_override hooks are for M/M/c validation."""
    rng = np.random.default_rng(seed)
    horizon = days * DAY
    # Thinning for the non-homogeneous Poisson arrivals.
    lam_max = max_rate(w)
    arrivals = []
    t = 0.0
    while True:
        t += rng.exponential(1 / lam_max) if arrival_override is None else arrival_override(rng)
        if t >= horizon:
            break
        if arrival_override is not None or rng.random() < rate_at(t, w) / lam_max:
            arrivals.append(t)

    mean_s = w.overhead_s + w.repetitions * 2 * w.run_s
    sigma = math.sqrt(math.log(1 + w.service_cv ** 2))

    def service():
        if service_override is not None:
            return service_override(rng)
        s = mean_s * rng.lognormal(-sigma ** 2 / 2, sigma)
        while rng.random() < w.p_retry:  # failed attempt: partial time wasted, re-run
            s += mean_s * rng.uniform(0.1, 1.0)
        return s

    jobs_per_req = 1 if service_override is not None else w.workloads_per_request
    free_at = [0.0] * nodes
    heapq.heapify(free_at)
    turnaround, job_wait, busy = [], [], 0.0
    peak_turn = []
    for a in arrivals:
        done = a
        for _ in range(jobs_per_req):
            start = max(a, heapq.heappop(free_at))  # FIFO: earliest free node
            s = service()
            heapq.heappush(free_at, start + s)
            job_wait.append(start - a)
            busy += s
            done = max(done, start + s)
        turnaround.append(done - a)
        day = int(a // DAY) % 7
        hour = (a % DAY) / 3600
        if day < 5 and 9 <= hour < 19:
            peak_turn.append(done - a)
    ta = np.array(turnaround)
    pk = np.array(peak_turn) if peak_turn else ta
    return {
        "nodes": nodes, "requests": len(arrivals), "utilization": busy / (nodes * horizon),
        "mean_job_wait_s": float(np.mean(job_wait)),
        "turnaround_p50_min": float(np.percentile(ta, 50) / 60),
        "turnaround_p95_min": float(np.percentile(ta, 95) / 60),
        "peak_turnaround_p50_min": float(np.percentile(pk, 50) / 60),
        "peak_turnaround_p95_min": float(np.percentile(pk, 95) / 60),
    }


def erlang_c_wait(lam: float, mu: float, c: int) -> float:
    """Mean wait in queue for M/M/c."""
    a = lam / mu
    rho = a / c
    if rho >= 1:
        return math.inf
    s = sum(a ** k / math.factorial(k) for k in range(c))
    top = a ** c / math.factorial(c) / (1 - rho)
    pw = top / (s + top)
    return pw / (c * mu - lam)


# Detection power for a 2% / 5% uniform regression at a given repetitions
# count, taken from docs/results/detection.json when present.
def load_power(path: str) -> dict:
    try:
        with open(path) as f:
            syn = json.load(f)["synthetic"]
    except (OSError, KeyError, json.JSONDecodeError):
        return {}
    return {(r["model"], r["n_runs"], r["effect"]): r["rate"] for r in syn
            if r["mode"] == "uniform"}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="docs/results")
    p.add_argument("--slo-min", type=float, default=30.0, help="p95 peak-hour CI wait target")
    args = p.parse_args()
    costs = Costs()
    power = load_power(os.path.join(args.out, "detection.json"))
    rows = []
    for reps in (5, 8, 10, 20):
        w = Workload(repetitions=reps)
        for n in range(2, 25):
            r = simulate(n, w)
            r["repetitions"] = reps
            r["cost_month_usd"] = n * costs.node_hour_usd * 730
            rows.append(r)

    # Erlang-C cross-check on a stationary M/M/c version of the same load.
    mu = 1 / mean_service_s(Workload())
    lam = 5.5 * mu  # rho = 0.92 / 0.69 / 0.55 for 6 / 8 / 10 servers
    checks = []
    for c in (6, 8, 10):
        sim = simulate(c, Workload(), days=200, seed=0,
                       service_override=lambda rng: rng.exponential(1 / mu),
                       arrival_override=lambda rng: rng.exponential(1 / lam))
        checks.append({"c": c, "rho": lam / mu / c, "erlang_c_wait_s": erlang_c_wait(lam, mu, c),
                       "sim_wait_s": sim["mean_job_wait_s"]})

    max_nodes = int(costs.budget_month_usd // (costs.node_hour_usd * 730))
    rec = {}
    for reps in (5, 8, 10, 20):
        ok = [r for r in rows if r["repetitions"] == reps and r["peak_turnaround_p95_min"] <= args.slo_min]
        rec[reps] = min((r["nodes"] for r in ok), default=None)

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "capacity.json"), "w") as f:
        json.dump({"workload": asdict(Workload()), "costs": asdict(costs), "rows": rows,
                   "erlang_check": checks, "min_nodes_for_slo": rec,
                   "max_nodes_in_budget": max_nodes}, f, indent=2)

    w = Workload()
    L = ["# Capacity model results", "",
         "Generated by `python -m benchgate.capacity.queue_sim` (28 simulated days per row).", "",
         f"Load: {w.requests_per_weekday:.0f} gate requests per weekday ({w.business_share:.0%} "
         f"between 09:00 and 19:00, weekends at {w.weekend_factor:.0%}), "
         f"{w.workloads_per_request} paired jobs per request, a job = {w.overhead_s:.0f} s overhead "
         f"+ repetitions x 2 arms x {w.run_s:.0f} s, CV {w.service_cv}, {w.p_retry:.0%} retries. "
         f"Cost: ${costs.node_hour_usd:.2f}/node-hour, budget ${costs.budget_month_usd:,.0f}/month "
         f"=> at most **{max_nodes} nodes**.", "",
         "## Utilization vs CI wait (peak hours, weekdays 09-19)", "",
         "| nodes | $/month | reps | utilization | p50 wait (min) | p95 wait (min) |",
         "|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        if r["nodes"] in (4, 5, 6, 8, 9, 10, 12, 16, 20) and r["peak_turnaround_p95_min"] < 1e3:
            L.append(f"| {r['nodes']} | {r['cost_month_usd']:,.0f} | {r['repetitions']} | "
                     f"{r['utilization']:.0%} | {r['peak_turnaround_p50_min']:.1f} | "
                     f"{r['peak_turnaround_p95_min']:.1f} |")
    L += ["", "## Erlang C cross-check (M/M/c, stationary)", "",
          "| servers | rho | Erlang C mean wait (s) | simulated (s) |", "|---:|---:|---:|---:|"]
    for c in checks:
        L.append(f"| {c['c']} | {c['rho']:.2f} | {c['erlang_c_wait_s']:.1f} | {c['sim_wait_s']:.1f} |")
    L += ["", f"## Smallest fleet meeting p95 peak wait <= {args.slo_min:.0f} min", "",
          "| reps/side | min nodes | detection of 2% (cloud VM model) | detection of 2% (tuned model) |",
          "|---:|---:|---:|---:|"]
    for reps, n in rec.items():
        pc = power.get(("untuned-cloud-vm", reps, 0.02))
        pt = power.get(("tuned-bare-metal", reps, 0.02))
        L.append(f"| {reps} | {n if n is not None else '>24'} | "
                 f"{'–' if pc is None else f'{pc:.0%}'} | {'–' if pt is None else f'{pt:.0%}'} |")
    with open(os.path.join(args.out, "capacity.md"), "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
