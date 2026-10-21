"""Collect a pool of real benchmark runs on the current machine.

Conditions (clean / injected slowdowns) are shuffled and interleaved so slow
drift of the machine (thermal, a neighbour waking up) spreads evenly over all
conditions instead of masquerading as a regression.

    python -m benchgate.sim.collect --out data/calibration/sandbox_runs.jsonl.gz
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import time

from benchgate.models import JobSpec
from benchgate.runner import DEFAULT_BINARY, environment_fingerprint, run_once

CONDITIONS = [  # (mode, pct, count)
    ("none", 0.0, 120),
    ("uniform", 2.0, 40),
    ("uniform", 5.0, 40),
    ("uniform", 10.0, 40),
    ("tail", 5.0, 40),
    ("tail", 10.0, 40),
]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--workloads", default="pointcloud,planner,matrix")
    p.add_argument("--iterations", type=int, default=300)
    p.add_argument("--scale", type=float, default=1.0, help="multiply run counts")
    p.add_argument("--binary", default=DEFAULT_BINARY)
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args()

    plan = []
    for w in args.workloads.split(","):
        for mode, pct, n in CONDITIONS:
            plan += [(w, mode, pct)] * max(2, int(n * args.scale))
    random.Random(args.seed).shuffle(plan)

    env = environment_fingerprint()
    t0 = time.time()
    with gzip.open(args.out, "wt") as f:
        f.write(json.dumps({"kind": "meta", "env": env, "iterations": args.iterations,
                            "conditions": CONDITIONS, "started": t0}) + "\n")
        for i, (w, mode, pct) in enumerate(plan):
            spec = JobSpec(commit="calib", workload=w, iterations=args.iterations,
                           slowdown_mode=mode, slowdown_pct=pct)
            r = run_once(spec, i, job_id="calib", binary=args.binary, env=env)
            f.write(json.dumps({"kind": "run", "seq": i, "workload": w, "mode": mode,
                                "pct": pct, "t": r.started_at,
                                "iteration_ns": r.iteration_ns}) + "\n")
            if i % 100 == 0:
                print(f"{i}/{len(plan)} {time.time() - t0:.0f}s", flush=True)
    print(f"done: {len(plan)} runs in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
