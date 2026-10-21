import threading
import time

import pytest

from benchgate.executors import AttemptFailed, AttemptTimeout, Executor
from benchgate.lease import SqliteLeaseStore
from benchgate.models import JobSpec, JobState, RunRecord
from benchgate.orchestrator import Orchestrator
from benchgate.storage.duckdb_store import DuckDBStore


def rec(job, node, i=0):
    return RunRecord(job_id=job.id, commit=job.spec.commit, workload=job.spec.workload,
                     repetition=i, node=node, iteration_ns=[1000, 1100, 1200],
                     p50_ns=1100, p90_ns=1180, p99_ns=1198, mean_ns=1100, checksum=1.0)


class FakeExecutor(Executor):
    def __init__(self, nodes, behaviour=None, duration=0.05):
        self._nodes = nodes
        self.behaviour = behaviour or (lambda job, node: None)
        self.duration = duration
        self.active: dict[str, int] = {n: 0 for n in nodes}
        self.max_active = 0
        self.overlap = False
        self.calls = []
        self.lock = threading.Lock()

    def nodes(self):
        return list(self._nodes)

    def execute(self, job, node):
        with self.lock:
            self.active[node] += 1
            if self.active[node] > 1:
                self.overlap = True
            self.max_active = max(self.max_active, sum(self.active.values()))
            self.calls.append((job.id, node))
        try:
            time.sleep(self.duration)
            self.behaviour(job, node)
            return [rec(job, node)]
        finally:
            with self.lock:
                self.active[node] -= 1


def make(executor, **kw):
    store = DuckDBStore()
    o = Orchestrator(executor, SqliteLeaseStore(), store, poll_s=0.01,
                     backoff_base_s=0.01, **kw).start()
    return o, store


def test_never_two_jobs_on_one_node():
    ex = FakeExecutor(["n0", "n1", "n2"], duration=0.03)
    o, store = make(ex)
    jobs = [o.submit(JobSpec(commit=f"c{i}", workload="matrix")) for i in range(30)]
    assert o.wait([j.id for j in jobs], timeout=30)
    o.stop()
    assert not ex.overlap
    assert ex.max_active <= 3
    assert all(j.state == JobState.SUCCEEDED for j in jobs)
    assert len(store.get_runs("c7")) == 1


def test_retry_moves_to_another_node():
    def flaky_on_n0(job, node):
        if node == "n0":
            raise AttemptFailed("node n0 is broken")
    ex = FakeExecutor(["n0", "n1"], flaky_on_n0)
    o, _ = make(ex)
    jobs = [o.submit(JobSpec(commit=f"c{i}", workload="planner", max_retries=2)) for i in range(6)]
    assert o.wait([j.id for j in jobs], timeout=30)
    o.stop()
    assert all(j.state == JobState.SUCCEEDED for j in jobs)
    for j in jobs:
        nodes = [n for jid, n in ex.calls if jid == j.id]
        if nodes[0] == "n0":
            assert nodes[1] == "n1"  # second attempt avoided the failed node


def test_exhausted_retries_and_timeouts():
    def always_timeout(job, node):
        raise AttemptTimeout("too slow")
    ex = FakeExecutor(["n0", "n1"], always_timeout)
    o, store = make(ex)
    j = o.submit(JobSpec(commit="slow", workload="matrix", max_retries=2))
    assert o.wait([j.id], timeout=30)
    o.stop()
    assert j.state == JobState.TIMED_OUT
    assert j.attempts == 3
    assert "too slow" in j.error
    assert store.query("SELECT state, attempts FROM jobs WHERE id = ?", [j.id]) == [("timed_out", 3)]


def test_lost_lease_invalidates_attempt():
    class StealingLeases(SqliteLeaseStore):
        def renew(self, node, holder, ttl_s):
            return False  # someone else took the node
    ex = FakeExecutor(["n0"], duration=0.2)
    o = Orchestrator(ex, StealingLeases(), None, lease_ttl_s=0.15, poll_s=0.01,
                     backoff_base_s=0.01).start()
    j = o.submit(JobSpec(commit="x", workload="matrix", max_retries=0))
    assert o.wait([j.id], timeout=10)
    o.stop()
    assert j.state == JobState.FAILED and "lease" in j.error


@pytest.mark.parametrize("n_nodes", [1, 4])
def test_fifo_and_throughput(n_nodes):
    ex = FakeExecutor([f"n{i}" for i in range(n_nodes)], duration=0.02)
    o, _ = make(ex)
    jobs = [o.submit(JobSpec(commit=f"c{i}", workload="matrix")) for i in range(8)]
    o.wait([j.id for j in jobs], timeout=30)
    o.stop()
    if n_nodes == 1:
        assert [jid for jid, _ in ex.calls] == [j.id for j in jobs]
