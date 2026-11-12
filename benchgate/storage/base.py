"""Storage interface and the row mapping shared by all backends."""

from __future__ import annotations

import abc
import json
import time
import uuid

from benchgate.models import Job, RunRecord
from benchgate.stats.gate import GateResult

# Logical schema, expressed once; each backend maps the types.
# Flattened trace metrics are top-level columns so dashboards can chart them
# without JSON extraction.
RUNS_COLUMNS = [
    ("job_id", "STRING"), ("commit", "STRING"), ("workload", "STRING"),
    ("repetition", "INT64"), ("node", "STRING"), ("started_at", "TIMESTAMP"),
    ("duration_s", "FLOAT64"), ("n_iter", "INT64"),
    ("p50_ns", "FLOAT64"), ("p90_ns", "FLOAT64"), ("p99_ns", "FLOAT64"), ("mean_ns", "FLOAT64"),
    ("checksum", "FLOAT64"), ("slowdown_mode", "STRING"), ("slowdown_pct", "FLOAT64"),
    ("sched_jitter_p99_us", "FLOAT64"), ("cpu_utilization", "FLOAT64"),
    ("involuntary_cs", "INT64"), ("mem_bw_gbps", "FLOAT64"), ("ipc", "FLOAT64"),
    ("iteration_ns", "INT64[]"), ("env", "JSON"), ("trace", "JSON"),
]
JOBS_COLUMNS = [
    ("id", "STRING"), ("commit", "STRING"), ("workload", "STRING"), ("state", "STRING"),
    ("attempts", "INT64"), ("node", "STRING"), ("error", "STRING"),
    ("created_at", "TIMESTAMP"), ("finished_at", "TIMESTAMP"), ("spec", "JSON"),
]
COMMITS_COLUMNS = [
    ("sha", "STRING"), ("parent", "STRING"), ("seq", "INT64"), ("branch", "STRING"),
    ("committed_at", "TIMESTAMP"),
]
VERDICTS_COLUMNS = [
    ("id", "STRING"), ("base_commit", "STRING"), ("head_commit", "STRING"),
    ("created_at", "TIMESTAMP"), ("verdict", "STRING"), ("blocking", "BOOL"), ("details", "JSON"),
]
VERDICT_TESTS_COLUMNS = [
    ("verdict_id", "STRING"), ("base_commit", "STRING"), ("head_commit", "STRING"),
    ("created_at", "TIMESTAMP"), ("workload", "STRING"), ("metric", "STRING"),
    ("base_median", "FLOAT64"), ("cand_median", "FLOAT64"), ("rel_change", "FLOAT64"),
    ("ci_low", "FLOAT64"), ("ci_high", "FLOAT64"), ("p_value", "FLOAT64"),
    ("p_adjusted", "FLOAT64"), ("verdict", "STRING"),
]
TABLES = {
    "runs": RUNS_COLUMNS, "jobs": JOBS_COLUMNS, "commits": COMMITS_COLUMNS,
    "verdicts": VERDICTS_COLUMNS, "verdict_tests": VERDICT_TESTS_COLUMNS,
}


def run_row(r: RunRecord) -> dict:
    t = r.trace or {}
    perf = t.get("perf", {})
    proc = t.get("proc", {})
    return {
        "job_id": r.job_id, "commit": r.commit, "workload": r.workload,
        "repetition": r.repetition, "node": r.node, "started_at": r.started_at,
        "duration_s": r.duration_s, "n_iter": len(r.iteration_ns),
        "p50_ns": r.p50_ns, "p90_ns": r.p90_ns, "p99_ns": r.p99_ns, "mean_ns": r.mean_ns,
        "checksum": r.checksum, "slowdown_mode": r.slowdown_mode, "slowdown_pct": r.slowdown_pct,
        "sched_jitter_p99_us": t.get("sched_jitter", {}).get("p99_us"),
        "cpu_utilization": perf.get("cpu_utilization", proc.get("cpu_utilization")),
        "involuntary_cs": proc.get("involuntary_cs"),
        "mem_bw_gbps": perf.get("mem_bw_gbps"), "ipc": perf.get("ipc"),
        "iteration_ns": list(r.iteration_ns), "env": json.dumps(r.env),
        "trace": json.dumps(t),
    }


def row_to_run(d: dict) -> RunRecord:
    def js(v):
        if v is None:
            return {}
        return json.loads(v) if isinstance(v, str) else v
    started = d["started_at"]
    if hasattr(started, "timestamp"):
        started = started.timestamp()
    return RunRecord(
        job_id=d["job_id"], commit=d["commit"], workload=d["workload"],
        repetition=d["repetition"], node=d["node"], iteration_ns=list(d["iteration_ns"]),
        p50_ns=d["p50_ns"], p90_ns=d["p90_ns"], p99_ns=d["p99_ns"], mean_ns=d["mean_ns"],
        checksum=d["checksum"], slowdown_mode=d["slowdown_mode"], slowdown_pct=d["slowdown_pct"],
        started_at=started, duration_s=d["duration_s"], env=js(d["env"]), trace=js(d["trace"]),
    )


def job_row(j: Job) -> dict:
    return {"id": j.id, "commit": j.spec.commit, "workload": j.spec.workload,
            "state": j.state.value, "attempts": j.attempts, "node": j.node, "error": j.error,
            "created_at": j.created_at, "finished_at": j.finished_at,
            "spec": json.dumps(j.spec.to_dict())}


def verdict_rows(base: str, head: str, result: GateResult) -> tuple[dict, list[dict]]:
    vid = "v-" + uuid.uuid4().hex[:12]
    now = time.time()
    v = {"id": vid, "base_commit": base, "head_commit": head, "created_at": now,
         "verdict": result.verdict.value, "blocking": result.blocking,
         "details": json.dumps(result.to_dict())}
    tests = [{"verdict_id": vid, "base_commit": base, "head_commit": head, "created_at": now,
              "workload": t.workload, "metric": t.metric, "base_median": t.base_median,
              "cand_median": t.cand_median, "rel_change": t.ci.estimate, "ci_low": t.ci.low,
              "ci_high": t.ci.high, "p_value": t.p_value, "p_adjusted": t.p_adjusted,
              "verdict": t.verdict.value} for t in result.tests]
    return v, tests


class ResultStore(abc.ABC):
    @abc.abstractmethod
    def init_schema(self) -> None: ...

    @abc.abstractmethod
    def write_runs(self, runs: list[RunRecord]) -> None: ...

    @abc.abstractmethod
    def write_job(self, job: Job) -> None: ...

    @abc.abstractmethod
    def get_runs(self, commit: str, workload: str | None = None) -> list[RunRecord]: ...

    @abc.abstractmethod
    def record_commit(self, sha: str, parent: str | None, seq: int,
                      branch: str = "main", committed_at: float | None = None) -> None: ...

    @abc.abstractmethod
    def history(self, workload: str, metric: str = "p50_ns", branch: str = "main",
                limit: int = 500) -> list[tuple[str, int, float, int]]:
        """Per-commit median of the per-run metric, oldest first:
        (sha, seq, value, n_runs)."""

    @abc.abstractmethod
    def write_verdict(self, base: str, head: str, result: GateResult) -> str: ...

    def close(self) -> None:  # pragma: no cover - optional
        pass
