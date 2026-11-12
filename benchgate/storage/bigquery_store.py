"""BigQuery backend for a shared fleet: many orchestrators, long retention,
Grafana's BigQuery datasource reading the same tables.

Optional dependency: ``pip install google-cloud-bigquery``. Runs are
partitioned by day on started_at and clustered by (workload, commit), which
is what both the gate (point lookups by commit) and dashboards (time ranges
per workload) filter on.
"""

from __future__ import annotations

import datetime as dt

from benchgate.models import Job, RunRecord
from benchgate.stats.gate import GateResult
from benchgate.storage.base import TABLES, ResultStore, job_row, row_to_run, run_row, verdict_rows

_METRICS = {"p50_ns", "p90_ns", "p99_ns", "mean_ns", "sched_jitter_p99_us",
            "cpu_utilization", "mem_bw_gbps", "ipc"}


def _bq_field(name: str, typ: str):
    from google.cloud import bigquery
    if typ == "INT64[]":
        return bigquery.SchemaField(name, "INT64", mode="REPEATED")
    return bigquery.SchemaField(name, typ, mode="NULLABLE")


def _iso(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return dt.datetime.fromtimestamp(v, tz=dt.timezone.utc).isoformat()
    return v


class BigQueryStore(ResultStore):
    def __init__(self, project: str, dataset: str = "benchgate", client=None,
                 location: str = "US"):
        if client is None:
            from google.cloud import bigquery
            client = bigquery.Client(project=project)
        self.client = client
        self.project = project
        self.dataset = dataset
        self.location = location

    def _t(self, name: str) -> str:
        return f"`{self.project}.{self.dataset}.{name}`"

    def init_schema(self) -> None:
        from google.cloud import bigquery
        self.client.create_dataset(bigquery.Dataset(f"{self.project}.{self.dataset}"),
                                   exists_ok=True)
        for name, cols in TABLES.items():
            table = bigquery.Table(f"{self.project}.{self.dataset}.{name}",
                                   schema=[_bq_field(c, t) for c, t in cols])
            if name == "runs":
                table.time_partitioning = bigquery.TimePartitioning(field="started_at")
                table.clustering_fields = ["workload", "commit"]
            self.client.create_table(table, exists_ok=True)

    def _insert(self, table: str, rows: list[dict]) -> None:
        if not rows:
            return
        types = dict(TABLES[table])
        clean = [{k: (_iso(v) if types.get(k) == "TIMESTAMP" else v) for k, v in r.items()}
                 for r in rows]
        errors = self.client.insert_rows_json(f"{self.project}.{self.dataset}.{table}", clean)
        if errors:
            raise RuntimeError(f"BigQuery insert into {table} failed: {errors[:3]}")

    def _query(self, sql: str, params: list) -> list[dict]:
        from google.cloud import bigquery
        cfg = bigquery.QueryJobConfig(query_parameters=params)
        return [dict(r.items()) for r in self.client.query(sql, job_config=cfg).result()]

    def write_runs(self, runs: list[RunRecord]) -> None:
        self._insert("runs", [run_row(r) for r in runs])

    def write_job(self, job: Job) -> None:
        # Append-only: streaming inserts can't be updated for ~30 min. Readers
        # take the latest row per id (see dashboards).
        self._insert("jobs", [job_row(job)])

    def get_runs(self, commit: str, workload: str | None = None) -> list[RunRecord]:
        from google.cloud import bigquery
        sql = f"SELECT * FROM {self._t('runs')} WHERE commit = @commit"
        params = [bigquery.ScalarQueryParameter("commit", "STRING", commit)]
        if workload:
            sql += " AND workload = @workload"
            params.append(bigquery.ScalarQueryParameter("workload", "STRING", workload))
        return [row_to_run(r) for r in self._query(sql + " ORDER BY started_at", params)]

    def record_commit(self, sha, parent, seq, branch="main", committed_at=None) -> None:
        self._insert("commits", [{"sha": sha, "parent": parent, "seq": seq, "branch": branch,
                                  "committed_at": committed_at}])

    def history(self, workload, metric="p50_ns", branch="main", limit=500):
        from google.cloud import bigquery
        if metric not in _METRICS:
            raise ValueError(f"unknown metric {metric}")
        sql = f"""
            SELECT sha, seq, v, n FROM (
              SELECT c.sha, c.seq, APPROX_QUANTILES(r.{metric}, 2)[OFFSET(1)] AS v, COUNT(*) AS n
              FROM {self._t('commits')} c JOIN {self._t('runs')} r ON r.commit = c.sha
              WHERE c.branch = @branch AND r.workload = @workload
              GROUP BY c.sha, c.seq ORDER BY c.seq DESC LIMIT @limit)
            ORDER BY seq"""
        rows = self._query(sql, [
            bigquery.ScalarQueryParameter("branch", "STRING", branch),
            bigquery.ScalarQueryParameter("workload", "STRING", workload),
            bigquery.ScalarQueryParameter("limit", "INT64", limit)])
        return [(r["sha"], int(r["seq"]), float(r["v"]), int(r["n"])) for r in rows]

    def write_verdict(self, base: str, head: str, result: GateResult) -> str:
        v, tests = verdict_rows(base, head, result)
        self._insert("verdicts", [v])
        self._insert("verdict_tests", tests)
        return v["id"]
