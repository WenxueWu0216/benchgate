import time

import numpy as np

from benchgate.executors import Executor
from benchgate.lease import SqliteLeaseStore
from benchgate.models import RunRecord
from benchgate.orchestrator import Orchestrator
from benchgate.service import BenchGateService
from benchgate.storage.duckdb_store import DuckDBStore


class ScriptedExecutor(Executor):
    """Each paired job returns synthetic runs; head's slowdown per batch is scripted."""

    def __init__(self, head_slowdowns):
        self.slow = iter(head_slowdowns)
        self.rng = np.random.default_rng(0)

    def nodes(self):
        return ["n0"]

    def execute(self, job, node):
        s = next(self.slow)
        out = []
        for rep in range(job.spec.repetitions):
            for commit, mult in ((job.spec.baseline_commit, 1.0), (job.spec.commit, 1.0 + s)):
                it = (self.rng.normal(1e6, 5e3, 50) * mult * np.exp(self.rng.normal(0, 0.002))).astype(int)
                out.append(RunRecord(job_id=job.id, commit=commit, workload=job.spec.workload,
                                     repetition=rep, node=node, iteration_ns=it.tolist(),
                                     p50_ns=float(np.median(it)), p90_ns=0, p99_ns=0, mean_ns=0,
                                     checksum=1.0))
        return out


def _compare(slowdowns, tmp_path):
    store = DuckDBStore()
    orch = Orchestrator(ScriptedExecutor(slowdowns), SqliteLeaseStore(), store, poll_s=0.01).start()
    svc = BenchGateService(orch, store, plot_dir=str(tmp_path))
    c = svc.start_comparison("b", "h", ["matrix"], repetitions=8)
    for _ in range(300):
        if c.status != "running":
            break
        time.sleep(0.05)
    orch.stop()
    assert c.status == "done", c.error
    return c


def test_confirmed_regression_blocks(tmp_path):
    c = _compare([0.10, 0.10], tmp_path)
    assert c.result["verdict"] == "regression"
    assert c.result["confirmation"]["confirmed"] == ["matrix"]
    assert len(c.job_ids) == 2


def test_unconfirmed_regression_does_not_block(tmp_path):
    c = _compare([0.10, 0.0], tmp_path)
    assert c.result["verdict"] == "inconclusive" and not c.result["blocking"]
    assert c.result["confirmation"]["confirmed"] == []


def test_clean_needs_no_confirmation(tmp_path):
    c = _compare([0.0], tmp_path)
    assert c.result["verdict"] == "pass" and "confirmation" not in c.result
    assert len(c.job_ids) == 1
