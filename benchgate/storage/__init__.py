"""Result storage. DuckDB is the local default; BigQuery is optional.

    store = open_store("duckdb:///var/lib/benchgate/results.duckdb")
    store = open_store("bigquery://my-project/benchgate")
"""

from benchgate.storage.base import ResultStore, run_row


def open_store(url: str) -> ResultStore:
    if url.startswith("duckdb://"):
        from benchgate.storage.duckdb_store import DuckDBStore
        path = url[len("duckdb://"):] or ":memory:"
        return DuckDBStore(path)
    if url.startswith("bigquery://"):
        from benchgate.storage.bigquery_store import BigQueryStore
        project, _, dataset = url[len("bigquery://"):].partition("/")
        return BigQueryStore(project=project, dataset=dataset or "benchgate")
    raise ValueError(f"unsupported store url {url!r}")


__all__ = ["ResultStore", "open_store", "run_row"]
