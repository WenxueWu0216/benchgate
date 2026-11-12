"""End-to-end through the real binary, runner, local executor, orchestrator,
gate, storage and HTTP API."""

from http.server import ThreadingHTTPServer
import json
import threading
import time
import urllib.request

from benchgate.api import make_handler
from benchgate.cli import main as cli_main
from benchgate.executors import LocalExecutor
from benchgate.lease import SqliteLeaseStore
from benchgate.models import JobSpec
from benchgate.orchestrator import Orchestrator
from benchgate.runner import run_job
from benchgate.service import BenchGateService
from benchgate.storage.duckdb_store import DuckDBStore


def test_paired_job_interleaves(binary):
    spec = JobSpec(commit="head", workload="matrix", repetitions=3, iterations=20,
                   baseline_commit="base")
    runs = run_job(spec, job_id="t", binary=binary, baseline_binary=binary)
    assert [r.commit for r in runs] == ["base", "head", "head", "base", "base", "head"]


def test_gate_local_blocks_injected_regression(binary, tmp_path):
    out = tmp_path / "v.json"
    plot = tmp_path / "v.png"
    rc = cli_main(["gate-local", "--base", "aaa", "--head", "bbb", "--base-binary", binary,
                   "--head-binary", binary, "--workloads", "matrix", "--repetitions", "12",
                   "--iterations", "40", "--inject-mode", "uniform", "--inject-pct", "40",
                   "--out", str(out), "--plot", str(plot)])
    v = json.loads(out.read_text())
    assert v["verdict"] == "regression" and rc == 1
    assert plot.exists() and plot.stat().st_size > 10_000


def test_http_api_compare(binary, tmp_path):
    store = DuckDBStore()
    ex = LocalExecutor({"local-0": [0]}, binary=binary)
    orch = Orchestrator(ex, SqliteLeaseStore(), store, poll_s=0.02).start()
    svc = BenchGateService(orch, store, plot_dir=str(tmp_path))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(svc, token="s3cret"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(method, path, body=None, auth=True):
        req = urllib.request.Request(url + path, method=method,
                                     data=json.dumps(body).encode() if body else None,
                                     headers={"Authorization": "Bearer s3cret"} if auth else {})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()

    try:
        assert call("GET", "/healthz", auth=False)[0] == 200
        try:
            call("GET", "/compare/x", auth=False)
            raise AssertionError("expected 401")
        except urllib.error.HTTPError as e:
            assert e.code == 401
        _, body = call("POST", "/compare", {"base": "b0", "head": "h1", "workloads": ["planner"],
                                            "repetitions": 4, "iterations": 40})
        c = json.loads(body)
        for _ in range(300):
            c = json.loads(call("GET", f"/compare/{c['id']}")[1])
            if c["status"] in ("done", "error"):
                break
            time.sleep(0.1)
        assert c["status"] == "done", c
        assert {t["metric"] for t in c["result"]["tests"]} == {"p50", "p99"}
        status, png = call("GET", c["plot_url"])
        assert status == 200 and png[:4] == b"\x89PNG"
        assert len(store.get_runs("b0", "planner")) == 4
    finally:
        httpd.shutdown()
        orch.stop()
