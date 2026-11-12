"""Every DuckDB dashboard query must actually run against the real schema."""

import re

import numpy as np

from benchgate.dashboards import build_all
from benchgate.models import Job, JobSpec, RunRecord
from benchgate.stats.gate import GateConfig, evaluate
from benchgate.storage.duckdb_store import DuckDBStore


def _populated():
    s = DuckDBStore()
    rng = np.random.default_rng(0)
    for c, commit in enumerate(["c1", "c2"]):
        for w in ("pointcloud", "planner", "matrix"):
            s.write_runs([RunRecord(job_id="j", commit=commit, workload=w, repetition=i,
                                    node=f"n{i % 2}", iteration_ns=[1, 2], p50_ns=1e6 + rng.normal(),
                                    p90_ns=1.1e6, p99_ns=1.2e6, mean_ns=1e6, checksum=1.0,
                                    started_at=1.7e9 + c * 100 + i,
                                    trace={"sched_jitter": {"p99_us": 5.0},
                                           "proc": {"cpu_utilization": 1.0, "involuntary_cs": 2}})
                          for i in range(4)])
    runs = {"matrix": [rng.normal(1e6, 1e3, 20) for _ in range(5)]}
    s.write_verdict("c1", "c2", evaluate(runs, runs, GateConfig(n_boot=200)))
    s.write_job(Job(spec=JobSpec(commit="c2", workload="matrix")))
    return s


def test_duckdb_dashboard_queries_execute():
    store = _populated()
    n = 0
    for name, d in build_all().items():
        if not name.startswith("duckdb/"):
            continue
        queries = [t["rawSql"] for p in d["panels"] for t in p["targets"]]
        queries += [v["query"] for v in d["templating"]["list"] if v["type"] == "query"]
        for sql in queries:
            sql = re.sub(r"\$__timeFilter\((\w+)\)", "TRUE", sql).replace("$commit", "c2")
            assert "$" not in sql, sql
            store.query(sql)
            n += 1
    assert n > 20


def test_panel_ids_unique_and_gridpos_valid():
    for name, d in build_all().items():
        ids = [p["id"] for p in d["panels"]]
        assert len(ids) == len(set(ids)), name
        for p in d["panels"]:
            g = p["gridPos"]
            assert 0 <= g["x"] and g["x"] + g["w"] <= 24, (name, p["title"])
