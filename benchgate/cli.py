"""benchgate command line.

  benchgate serve        run the service (orchestrator + API)
  benchgate gate         ask a running service for a verdict on base..head
  benchgate gate-local   build-free, in-process verdict for two local binaries
  benchgate check        post a verdict JSON as a GitHub check run
  benchgate history      change-point report over stored commit history
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.request

from benchgate.report import markdown_summary


def _build_service(args):
    from benchgate.lease import KubernetesLeaseStore, SqliteLeaseStore
    from benchgate.orchestrator import Orchestrator
    from benchgate.service import BenchGateService
    from benchgate.storage import open_store

    store = open_store(args.store)
    if args.executor == "k8s":
        from benchgate.executors import KubernetesExecutor
        from benchgate.kube import Kube
        kube = Kube(namespace=args.namespace)
        executor = KubernetesExecutor(kube, image=args.image, cpus=args.bench_cpus)
        leases = KubernetesLeaseStore(kube)
    else:
        from benchgate.executors import LocalExecutor
        node_cpus = None
        if args.local_nodes:
            # "local-0=0,1;local-1=2,3"
            node_cpus = {k: [int(c) for c in v.split(",")]
                         for k, v in (p.split("=") for p in args.local_nodes.split(";"))}
        executor = LocalExecutor(node_cpus=node_cpus, binary_dir=args.binary_dir)
        leases = SqliteLeaseStore(args.lease_db)
    orch = Orchestrator(executor, leases, store, lease_ttl_s=args.lease_ttl).start()
    return BenchGateService(orch, store, plot_dir=args.plot_dir)


def cmd_serve(args) -> int:
    from benchgate.api import serve
    serve(_build_service(args), args.host, args.port)
    return 0


def _http(method: str, url: str, body: dict | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if os.environ.get("BENCHGATE_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['BENCHGATE_TOKEN']}"
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                 method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _write_outputs(args, verdict: dict, base: str, head: str, plot_url: str | None) -> int:
    if args.out:
        with open(args.out, "w") as f:
            json.dump(verdict, f, indent=2, default=str)
    md = markdown_summary(verdict, base, head, plot_url)
    print(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(md + "\n")
    return 1 if verdict.get("blocking") else 0


def cmd_gate(args) -> int:
    url = args.service.rstrip("/")
    c = _http("POST", f"{url}/compare", {
        "base": args.base, "head": args.head, "workloads": args.workloads.split(","),
        "repetitions": args.repetitions, "iterations": args.iterations,
        "head_image": args.head_image, "base_image": args.base_image})
    deadline = time.time() + args.timeout
    while c["status"] in ("pending", "running"):
        if time.time() > deadline:
            print(f"timed out waiting for comparison {c['id']}", file=sys.stderr)
            return 2
        time.sleep(5)
        c = _http("GET", f"{url}/compare/{c['id']}")
    if c["status"] != "done":
        print(f"comparison failed: {c['error']}", file=sys.stderr)
        return 2
    plot_url = f"{url}{c['plot_url']}" if c.get("plot_url") else None
    if plot_url and args.plot:
        req = urllib.request.Request(plot_url)
        if os.environ.get("BENCHGATE_TOKEN"):
            req.add_header("Authorization", f"Bearer {os.environ['BENCHGATE_TOKEN']}")
        with urllib.request.urlopen(req, timeout=60) as r, open(args.plot, "wb") as f:
            f.write(r.read())
    c["result"]["plot_url"] = plot_url
    return _write_outputs(args, c["result"], args.base, args.head, plot_url)


def cmd_gate_local(args) -> int:
    """Runs the whole pipeline in-process: orchestrator, local executor with
    one pinned 'node', paired ABBA runs, gate, plot. Used by CI when no
    bench fleet is configured, and for local experiments."""
    import tempfile

    from benchgate.executors import LocalExecutor
    from benchgate.lease import SqliteLeaseStore
    from benchgate.orchestrator import Orchestrator
    from benchgate.service import BenchGateService
    from benchgate.storage import open_store

    bindir = tempfile.mkdtemp(prefix="benchgate-bin-")
    for sha, src in ((args.base, args.base_binary), (args.head, args.head_binary)):
        os.makedirs(os.path.join(bindir, sha), exist_ok=True)
        dst = os.path.join(bindir, sha, "benchgate_workloads")
        if not os.path.exists(dst):
            os.symlink(os.path.abspath(src), dst)
    cpus = [int(c) for c in args.cpus.split(",")] if args.cpus else [os.cpu_count() - 1]
    executor = LocalExecutor({"local-0": cpus}, binary_dir=bindir)
    store = open_store(args.store)
    orch = Orchestrator(executor, SqliteLeaseStore(), store).start()
    svc = BenchGateService(orch, store, plot_dir=os.path.dirname(os.path.abspath(args.plot or "x")) or ".")
    c = svc.start_comparison(args.base, args.head, args.workloads.split(","),
                             repetitions=args.repetitions, iterations=args.iterations,
                             slowdown_mode=args.inject_mode, slowdown_pct=args.inject_pct)
    orch.wait(c.job_ids)
    while c.status == "running":
        time.sleep(0.2)
    orch.stop()
    if c.status != "done":
        print(f"comparison failed: {c.error}", file=sys.stderr)
        return 2
    if args.plot and c.plot and os.path.abspath(c.plot) != os.path.abspath(args.plot):
        os.replace(c.plot, args.plot)
    return _write_outputs(args, c.result, args.base, args.head, None)


def cmd_check(args) -> int:
    from benchgate.github_check import check_payload, post_check
    with open(args.verdict) as f:
        verdict = json.load(f)
    payload = check_payload(verdict, args.base, args.head, args.plot_url or verdict.get("plot_url"),
                            args.details_url)
    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0
    res = post_check(payload, args.repo, os.environ["GITHUB_TOKEN"])
    print(f"posted check run {res.get('id')}: {payload['conclusion']}")
    return 0


def cmd_history(args) -> int:
    from benchgate.service import history_report
    from benchgate.storage import open_store
    store = open_store(args.store)
    rep = history_report(store.history(args.workload, args.metric, args.branch), args.min_effect)
    print(json.dumps(rep, indent=2))
    return 1 if any(c["kind"] == "regression" for c in rep["change_points"]) else 0


def main(argv=None) -> int:
    logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="benchgate")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    s.add_argument("--store", default=os.environ.get("BENCHGATE_STORE", "duckdb://benchgate.duckdb"))
    s.add_argument("--executor", choices=["local", "k8s"], default=os.environ.get("BENCHGATE_EXECUTOR", "local"))
    s.add_argument("--namespace", default=os.environ.get("BENCHGATE_NAMESPACE", "benchgate"))
    s.add_argument("--image", default=os.environ.get("BENCHGATE_IMAGE", "benchgate:dev"))
    s.add_argument("--bench-cpus", default=os.environ.get("BENCHGATE_BENCH_CPUS"))
    s.add_argument("--local-nodes", default=os.environ.get("BENCHGATE_LOCAL_NODES"))
    s.add_argument("--binary-dir", default=os.environ.get("BENCHGATE_BINARY_DIR"))
    s.add_argument("--lease-db", default=os.environ.get("BENCHGATE_LEASE_DB", "benchgate-leases.sqlite"))
    s.add_argument("--lease-ttl", type=float, default=60.0)
    s.add_argument("--plot-dir", default=os.environ.get("BENCHGATE_PLOT_DIR", "/tmp/benchgate-plots"))
    s.set_defaults(fn=cmd_serve)

    def gate_common(g):
        g.add_argument("--base", required=True)
        g.add_argument("--head", required=True)
        g.add_argument("--workloads", default="pointcloud,planner,matrix")
        g.add_argument("--repetitions", type=int, default=10)
        g.add_argument("--iterations", type=int, default=300)
        g.add_argument("--out", help="write verdict JSON here")
        g.add_argument("--plot", help="write verdict plot PNG here")

    g = sub.add_parser("gate")
    gate_common(g)
    g.add_argument("--service", default=os.environ.get("BENCHGATE_URL"), required=not os.environ.get("BENCHGATE_URL"))
    g.add_argument("--head-image")
    g.add_argument("--base-image")
    g.add_argument("--timeout", type=float, default=3600)
    g.set_defaults(fn=cmd_gate)

    g = sub.add_parser("gate-local")
    gate_common(g)
    g.add_argument("--base-binary", required=True)
    g.add_argument("--head-binary", required=True)
    g.add_argument("--cpus", help="CPU list for the single local node (default: last CPU)")
    g.add_argument("--store", default="duckdb://")
    g.add_argument("--inject-mode", default="none", choices=["none", "uniform", "tail"],
                   help="inject a slowdown into head (to demo/validate the gate)")
    g.add_argument("--inject-pct", type=float, default=0.0)
    g.set_defaults(fn=cmd_gate_local)

    c = sub.add_parser("check")
    c.add_argument("--verdict", required=True)
    c.add_argument("--base", required=True)
    c.add_argument("--head", required=True)
    c.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    c.add_argument("--plot-url")
    c.add_argument("--details-url")
    c.add_argument("--dry-run", action="store_true")
    c.set_defaults(fn=cmd_check)

    h = sub.add_parser("history")
    h.add_argument("--store", required=True)
    h.add_argument("--workload", required=True)
    h.add_argument("--metric", default="p50_ns")
    h.add_argument("--branch", default="main")
    h.add_argument("--min-effect", type=float, default=0.01)
    h.set_defaults(fn=cmd_history)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
