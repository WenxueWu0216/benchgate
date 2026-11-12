"""DuckDB backend: a single file, zero services, columnar and fast enough to
hold years of per-iteration data for a small fleet."""

from __future__ import annotations

import datetime as dt
import threading

import duckdb

from benchgate.models import Job, RunRecord
from benchgate.stats.gate import GateResult
from benchgate.storage.base import TABLES, ResultStore, job_row, row_to_run, run_row, verdict_rows

TYPE_MAP = {"STRING": "VARCHAR", "INT64": "BIGINT", "FLOAT64": "DOUBLE",
            "TIMESTAMP": "TIMESTAMP", "BOOL": "BOOLEAN", "JSON": "JSON",
            "INT64[]": "BIGINT[]"}
_METRICS = {"p50_ns", "p90_ns", "p99_ns", "mean_ns", "sched_jitter_p99_us",
            "cpu_utilization", "mem_bw_gbps", "ipc"}


def _ts(v):
    if v is None or isinstance(v, dt.datetime):
        return v
    return dt.datetime.fromtimestamp(float(v), tz=dt.timezone.utc).replace(tzinfo=None)


class DuckDBStore(ResultStore):
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.con = duckdb.connect(path)
        self._lock = threading.Lock()  # one connection, many API threads
        self.init_schema()

    def init_schema(self) -> None:
        with self._lock:
            for table, cols in TABLES.items():
                ddl = ", ".join(f'"{c}" {TYPE_MAP[t]}' for c, t in cols)
                self.con.execute(f"CREATE TABLE IF NOT EXISTS {table} ({ddl})")
            self.con.execute("CREATE INDEX IF NOT EXISTS runs_cw ON runs(commit, workload)")

    def _insert(self, table: str, rows: list[dict]) -> None:
        if not rows:
            return
        cols = [c for c, _ in TABLES[table]]
        types = dict(TABLES[table])
        vals = [[_ts(r.get(c)) if types[c] == "TIMESTAMP" else r.get(c) for c in cols]
                for r in rows]
        q = f"INSERT INTO {table} ({', '.join(chr(34) + c + chr(34) for c in cols)}) VALUES ({', '.join('?' * len(cols))})"
        with self._lock:
            self.con.executemany(q, vals)

    def write_runs(self, runs: list[RunRecord]) -> None:
        self._insert("runs", [run_row(r) for r in runs])

    def write_job(self, job: Job) -> None:
        with self._lock:
            self.con.execute("DELETE FROM jobs WHERE id = ?", [job.id])
        self._insert("jobs", [job_row(job)])

    def get_runs(self, commit: str, workload: str | None = None) -> list[RunRecord]:
        q = "SELECT * FROM runs WHERE commit = ?"
        args: list = [commit]
        if workload:
            q += " AND workload = ?"
            args.append(workload)
        q += " ORDER BY started_at, repetition"
        with self._lock:
            cur = self.con.execute(q, args)
            names = [d[0] for d in cur.description]
            rows = [dict(zip(names, r)) for r in cur.fetchall()]
        return [row_to_run(r) for r in rows]

    def record_commit(self, sha, parent, seq, branch="main", committed_at=None) -> None:
        with self._lock:
            self.con.execute("DELETE FROM commits WHERE sha = ? AND branch = ?", [sha, branch])
        self._insert("commits", [{"sha": sha, "parent": parent, "seq": seq, "branch": branch,
                                  "committed_at": committed_at}])

    def history(self, workload, metric="p50_ns", branch="main", limit=500):
        if metric not in _METRICS:
            raise ValueError(f"unknown metric {metric}")
        q = f"""
            SELECT c.sha, c.seq, median(r.{metric}) AS v, count(*) AS n
            FROM commits c JOIN runs r ON r.commit = c.sha
            WHERE c.branch = ? AND r.workload = ?
            GROUP BY c.sha, c.seq ORDER BY c.seq DESC LIMIT ?"""
        with self._lock:
            rows = self.con.execute(q, [branch, workload, limit]).fetchall()
        return [(sha, int(seq), float(v), int(n)) for sha, seq, v, n in reversed(rows)]

    def write_verdict(self, base: str, head: str, result: GateResult) -> str:
        v, tests = verdict_rows(base, head, result)
        self._insert("verdicts", [v])
        self._insert("verdict_tests", tests)
        return v["id"]

    def query(self, sql: str, params: list | None = None) -> list[tuple]:
        with self._lock:
            return self.con.execute(sql, params or []).fetchall()

    def close(self) -> None:
        self.con.close()
