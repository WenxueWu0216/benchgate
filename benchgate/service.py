"""BenchGate service logic: turn a (base, head) comparison request into
benchmark jobs, wait for them, and produce a stored verdict + plot."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
import threading
import time
import uuid

import numpy as np

from benchgate.models import JobSpec, JobState
from benchgate.orchestrator import Orchestrator
from benchgate.stats.changepoint import cusum, pelt, segments
from benchgate.stats.gate import GateConfig, GateResult, Verdict, evaluate, overall
from benchgate.storage.base import ResultStore

log = logging.getLogger("benchgate.service")


@dataclass
class Comparison:
    base: str
    head: str
    workloads: list[str]
    id: str = field(default_factory=lambda: "cmp-" + uuid.uuid4().hex[:12])
    status: str = "pending"  # pending | running | done | error
    job_ids: list[str] = field(default_factory=list)
    result: dict | None = None
    error: str | None = None
    plot: str | None = None
    created_at: float = field(default_factory=time.time)
    _job_args: dict = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict:
        return {"id": self.id, "base": self.base, "head": self.head,
                "workloads": self.workloads, "status": self.status, "job_ids": self.job_ids,
                "result": self.result, "error": self.error,
                "plot_url": f"/plots/{self.id}.png" if self.plot else None}


class BenchGateService:
    def __init__(self, orch: Orchestrator, store: ResultStore, plot_dir: str = "/tmp/benchgate-plots",
                 gate: GateConfig = GateConfig(), confirm: bool = True):
        self.orch = orch
        self.store = store
        self.plot_dir = plot_dir
        self.gate = gate
        # Block only if an independent second batch of paired runs agrees.
        # Costs extra node time on regressions only (rare), and squares the
        # false-alarm rate (docs/results/detection.md, section 4).
        self.confirm = confirm
        self.comparisons: dict[str, Comparison] = {}
        os.makedirs(plot_dir, exist_ok=True)

    def start_comparison(self, base: str, head: str, workloads: list[str], *,
                         repetitions: int = 10, iterations: int = 300,
                         head_image: str | None = None, base_image: str | None = None,
                         slowdown_mode: str = "none", slowdown_pct: float = 0.0,
                         timeout_s: float = 900.0) -> Comparison:
        c = Comparison(base=base, head=head, workloads=list(workloads))
        self.comparisons[c.id] = c
        c._job_args = dict(repetitions=repetitions, iterations=iterations, image=head_image,
                           baseline_image=base_image, slowdown_mode=slowdown_mode,
                           slowdown_pct=slowdown_pct, timeout_s=timeout_s)
        c.job_ids = self._submit(c, workloads)
        c.status = "running"
        threading.Thread(target=self._finish, args=(c,), daemon=True).start()
        return c

    def _submit(self, c: Comparison, workloads: list[str]) -> list[str]:
        # Paired: base and head interleaved on the same node in one job.
        return [self.orch.submit(JobSpec(commit=c.head, workload=w, baseline_commit=c.base,
                                         **c._job_args)).id for w in workloads]

    def _collect(self, c: Comparison, job_ids: list[str]) -> tuple[dict, dict]:
        self.orch.wait(job_ids)
        jobs = [self.orch.get(j) for j in job_ids]
        bad = [j for j in jobs if j.state != JobState.SUCCEEDED]
        if bad:
            raise RuntimeError("; ".join(f"{j.spec.workload}: {j.state.value} ({j.error})"
                                         for j in bad))
        base_runs, head_runs = {}, {}
        for j in jobs:
            base_runs[j.spec.workload] = [r.iteration_ns for r in j.runs if r.commit == c.base]
            head_runs[j.spec.workload] = [r.iteration_ns for r in j.runs if r.commit == c.head]
        return base_runs, head_runs

    def _finish(self, c: Comparison) -> None:
        try:
            base_runs, head_runs = self._collect(c, c.job_ids)
            first = evaluate(base_runs, head_runs, self.gate)
            confirmation = None
            if self.confirm and first.verdict == Verdict.REGRESSION:
                regressed = sorted({t.workload for t in first.tests
                                    if t.verdict == Verdict.REGRESSION})
                ids = self._submit(c, regressed)
                c.job_ids += ids
                b2, h2 = self._collect(c, ids)
                second = evaluate(b2, h2, self.gate)
                confirmed = sorted({t.workload for t in second.tests
                                    if t.verdict == Verdict.REGRESSION} & set(regressed))
                confirmation = {"workloads": regressed, "confirmed": confirmed,
                                "first_regressions": [(t.workload, t.metric) for t in first.tests
                                                      if t.verdict == Verdict.REGRESSION],
                                "result": second.to_dict()}
                # Report the confirmation batch for confirmed workloads and the
                # first batch elsewhere; plot what decided the verdict.
                for w in regressed:
                    base_runs[w], head_runs[w] = b2[w], h2[w]
            c.result = self.verdict(c.base, c.head, base_runs, head_runs, plot_id=c.id,
                                    confirmation=confirmation)
            c.plot = os.path.join(self.plot_dir, f"{c.id}.png")
            c.status = "done"
        except Exception as e:  # noqa: BLE001
            log.exception("comparison %s failed", c.id)
            c.error, c.status = str(e), "error"

    def verdict(self, base, head, base_runs, head_runs, plot_id: str | None = None,
                confirmation: dict | None = None) -> dict:
        res = evaluate(base_runs, head_runs, self.gate)
        if confirmation is not None:
            # The two batches disagree for unconfirmed workloads: never block,
            # but do not call it a clean pass either.
            unconfirmed = set(confirmation["workloads"]) - set(confirmation["confirmed"])
            flagged = {tuple(x) for x in confirmation["first_regressions"]}
            for t in res.tests:
                if t.workload in unconfirmed and (t.workload, t.metric) in flagged:
                    t.verdict = Verdict.INCONCLUSIVE
            res = GateResult(overall(res.tests), res.tests, res.config)
        vid = self.store.write_verdict(base, head, res)
        d = res.to_dict()
        d["verdict_id"] = vid
        if confirmation is not None:
            d["confirmation"] = {k: confirmation[k] for k in ("workloads", "confirmed")}
        if plot_id:
            from benchgate.plots import verdict_plot
            verdict_plot(res, base_runs, head_runs, os.path.join(self.plot_dir, f"{plot_id}.png"),
                         title=f"{head[:10]} vs {base[:10]}: {res.verdict.value.upper()}")
        return d

    def history_check(self, workload: str, metric: str = "p50_ns", branch: str = "main",
                      min_effect: float = 0.01) -> dict:
        return history_report(self.store.history(workload, metric, branch), min_effect)


def history_report(hist: list[tuple[str, int, float, int]], min_effect: float = 0.01) -> dict:
    """Change points across a commit history, filtered to shifts > min_effect."""
    if len(hist) < 8:
        return {"n_commits": len(hist), "change_points": [], "alarms": []}
    shas = [h[0] for h in hist]
    x = np.array([h[2] for h in hist])
    rel = x / np.median(x[: max(5, len(x) // 4)])
    cps = pelt(rel)
    segs = segments(rel, cps)
    out = []
    for prev, seg in zip(segs, segs[1:]):
        shift = seg.mean / prev.mean - 1
        if abs(shift) >= min_effect:
            out.append({"commit": shas[seg.start], "index": seg.start, "shift": shift,
                        "kind": "regression" if shift > 0 else "improvement"})
    alarms = [{"commit": shas[a.index], "index": a.index, "start_commit": shas[a.start_estimate]}
              for a in cusum(rel)]
    return {"n_commits": len(hist), "change_points": out, "alarms": alarms}
