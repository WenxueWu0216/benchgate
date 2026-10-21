import numpy as np

from benchgate.models import RunRecord
from benchgate.stats.gate import GateConfig, evaluate
from benchgate.storage import open_store
from benchgate.storage.base import TABLES, run_row


def rr(commit, workload, p50, rep=0, trace=None):
    return RunRecord(job_id="j", commit=commit, workload=workload, repetition=rep, node="n0",
                     iteration_ns=[int(p50)] * 5, p50_ns=p50, p90_ns=p50, p99_ns=p50 * 1.1,
                     mean_ns=p50, checksum=3.0, trace=trace or {})


def test_duckdb_roundtrip_and_history(tmp_path):
    s = open_store(f"duckdb://{tmp_path}/r.duckdb")
    trace = {"sched_jitter": {"p99_us": 42.0}, "proc": {"cpu_utilization": 0.99, "involuntary_cs": 3}}
    s.write_runs([rr("c1", "matrix", 1000, i, trace) for i in range(3)])
    s.write_runs([rr("c2", "matrix", 1100, i) for i in range(3)] + [rr("c2", "planner", 50)])
    got = s.get_runs("c1", "matrix")
    assert len(got) == 3 and got[0].trace["sched_jitter"]["p99_us"] == 42.0
    assert got[0].iteration_ns == [1000] * 5
    assert s.query("SELECT sched_jitter_p99_us, involuntary_cs FROM runs WHERE commit='c1' LIMIT 1") == [(42.0, 3)]
    s.record_commit("c1", None, 1)
    s.record_commit("c2", "c1", 2)
    assert s.history("matrix") == [("c1", 1, 1000.0, 3), ("c2", 2, 1100.0, 3)]


def test_verdict_rows(tmp_path):
    s = open_store("duckdb://")
    rng = np.random.default_rng(0)
    runs = {"w": [rng.normal(1e6, 1e3, 50) for _ in range(5)]}
    res = evaluate(runs, runs, GateConfig(n_boot=200))
    vid = s.write_verdict("a", "b", res)
    assert s.query("SELECT count(*) FROM verdict_tests WHERE verdict_id = ?", [vid]) == [(2,)]


def test_run_row_covers_schema():
    row = run_row(rr("c", "w", 1.0))
    assert set(row) == {c for c, _ in TABLES["runs"]}


def test_bigquery_store_with_fake_client():
    import pytest
    pytest.importorskip("google.cloud.bigquery")
    from benchgate.storage.bigquery_store import BigQueryStore

    class FakeClient:
        def __init__(self):
            self.rows = {}

        def insert_rows_json(self, table, rows):
            self.rows.setdefault(table, []).extend(rows)
            return []
    c = FakeClient()
    BigQueryStore("proj", "ds", client=c).write_runs([rr("c", "w", 1.0)])
    (row,) = c.rows["proj.ds.runs"]
    assert row["started_at"].endswith("+00:00")
