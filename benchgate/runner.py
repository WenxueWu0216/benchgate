"""Runner: executes one benchmark job (N repetitions of a workload) on the
node it is running on, and reports RunRecords.

Runs either in-process (local executor, CI) or as the entrypoint of a
Kubernetes Job pod, where it prints a single ``BENCHGATE_RESULT {...}`` line
that the orchestrator scrapes from the pod log. That avoids needing shared
storage between bench pods and the orchestrator.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import time

from benchgate.models import JobSpec, RunRecord

RESULT_MARKER = "BENCHGATE_RESULT "

DEFAULT_BINARY = os.environ.get(
    "BENCHGATE_BINARY",
    str(Path(__file__).resolve().parent.parent / "workloads" / "build" / "benchgate_workloads"),
)


class RunError(RuntimeError):
    pass


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def environment_fingerprint() -> dict:
    """What we know about the node. Stored with every run so a noisy result
    can be traced back to e.g. a governor that was not pinned."""
    cpu_model = None
    cpuinfo = _read("/proc/cpuinfo") or ""
    for line in cpuinfo.splitlines():
        if line.startswith("model name"):
            cpu_model = line.split(":", 1)[1].strip()
            break
    cmdline = _read("/proc/cmdline") or ""
    return {
        "hostname": socket.gethostname(),
        "node_name": os.environ.get("BENCHGATE_NODE_NAME", socket.gethostname()),
        "kernel": platform.release(),
        "cpu_model": cpu_model,
        "nproc": os.cpu_count(),
        "affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
        "governor": _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
        "smt": _read("/sys/devices/system/cpu/smt/control"),
        "no_turbo": _read("/sys/devices/system/cpu/intel_pstate/no_turbo"),
        "isolcpus": next((t.split("=", 1)[1] for t in cmdline.split() if t.startswith("isolcpus=")), None),
        "nohz_full": next((t.split("=", 1)[1] for t in cmdline.split() if t.startswith("nohz_full=")), None),
        "loadavg": _read("/proc/loadavg"),
    }


def run_once(spec: JobSpec, repetition: int, *, job_id: str, binary: str = DEFAULT_BINARY,
             cpus: list[int] | None = None, tracer=None, env: dict | None = None) -> RunRecord:
    cmd = [binary, "--workload", spec.workload, "--iterations", str(spec.iterations),
           "--warmup", str(spec.warmup), "--seed", str(spec.seed), "--commit", spec.commit]
    if spec.slowdown_mode != "none":
        cmd += ["--slowdown-mode", spec.slowdown_mode, "--slowdown-pct", str(spec.slowdown_pct)]
    preexec = None
    if cpus:
        def preexec():  # pin before exec so the workload never runs elsewhere
            os.sched_setaffinity(0, cpus)
    t0 = time.time()
    trace: dict = {}
    if tracer is not None:
        out, trace = tracer.run(cmd, timeout=spec.timeout_s, preexec_fn=preexec)
    else:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=spec.timeout_s,
                                  preexec_fn=preexec)
        except subprocess.TimeoutExpired as e:
            raise RunError(f"workload timed out after {spec.timeout_s}s") from e
        if proc.returncode != 0:
            raise RunError(f"workload exited {proc.returncode}: {proc.stderr.strip()[:500]}")
        out = proc.stdout
    try:
        d = json.loads(out)
    except json.JSONDecodeError as e:
        raise RunError(f"workload produced invalid JSON: {out[:200]!r}") from e
    s = d["summary"]
    return RunRecord(
        job_id=job_id, commit=spec.commit, workload=spec.workload, repetition=repetition,
        node=(env or {}).get("node_name") or socket.gethostname(),
        iteration_ns=d["iteration_ns"], p50_ns=s["p50_ns"], p90_ns=s["p90_ns"],
        p99_ns=s["p99_ns"], mean_ns=s["mean_ns"], checksum=d["checksum"],
        slowdown_mode=spec.slowdown_mode, slowdown_pct=spec.slowdown_pct,
        started_at=t0, duration_s=time.time() - t0, env=env or {}, trace=trace,
    )


def run_job(spec: JobSpec, *, job_id: str, binary: str = DEFAULT_BINARY,
            baseline_binary: str | None = None, cpus: list[int] | None = None,
            tracer=None) -> list[RunRecord]:
    """Run spec.repetitions invocations. In paired mode (spec.baseline_commit
    set) the baseline binary is run the same number of times, alternating
    ABBA ABBA... so that slow drift during the job affects both sides equally."""
    env = environment_fingerprint()
    arms = [(spec, binary)]
    if spec.baseline_commit:
        if not baseline_binary:
            raise RunError("paired job needs a baseline binary")
        base = dataclasses.replace(spec, commit=spec.baseline_commit, slowdown_mode="none",
                                   slowdown_pct=0.0, baseline_commit=None)
        arms = [(base, baseline_binary), (spec, binary)]
    runs: list[RunRecord] = []
    checksums: dict[str, float] = {}
    for rep in range(spec.repetitions):
        order = arms if rep % 2 == 0 else arms[::-1]
        for s, b in order:
            r = run_once(s, rep, job_id=job_id, binary=b, cpus=cpus,
                         tracer=tracer if spec.trace else None, env=env)
            # Same seed, same binary => same answer. A mismatch means the
            # benchmark is broken (or the node is), and its timings are meaningless.
            prev = checksums.setdefault(s.commit, r.checksum)
            if r.checksum != prev:
                raise RunError(f"checksum mismatch for {s.commit} on repetition {rep}: "
                               f"{r.checksum} != {prev}")
            runs.append(r)
    return runs


def main(argv: list[str] | None = None) -> int:
    """Pod entrypoint: run a job described by flags/env, print the result line."""
    p = argparse.ArgumentParser(description="BenchGate runner (one job on this node)")
    p.add_argument("--job-id", default=os.environ.get("BENCHGATE_JOB_ID", "local"))
    p.add_argument("--spec", default=os.environ.get("BENCHGATE_JOB_SPEC"),
                   help="JobSpec as JSON (default: $BENCHGATE_JOB_SPEC)")
    p.add_argument("--binary", default=DEFAULT_BINARY)
    p.add_argument("--baseline-binary", default=os.environ.get("BENCHGATE_BASELINE_BINARY"))
    p.add_argument("--cpus", default=os.environ.get("BENCHGATE_CPUS"),
                   help="comma-separated CPU list to pin to, e.g. the isolated cores")
    args = p.parse_args(argv)
    if not args.spec:
        p.error("--spec or $BENCHGATE_JOB_SPEC is required")
    spec = JobSpec.from_dict(json.loads(args.spec))
    cpus = [int(c) for c in args.cpus.split(",")] if args.cpus else None
    tracer = None
    if spec.trace:
        from benchgate.trace.capture import TraceCapture
        tracer = TraceCapture()
    try:
        runs = run_job(spec, job_id=args.job_id, binary=args.binary,
                       baseline_binary=args.baseline_binary, cpus=cpus, tracer=tracer)
    except RunError as e:
        print(RESULT_MARKER + json.dumps({"ok": False, "error": str(e)}), flush=True)
        return 1
    print(RESULT_MARKER + json.dumps({"ok": True, "runs": [r.to_dict() for r in runs]}), flush=True)
    return 0


def parse_result_line(log: str) -> dict:
    for line in reversed(log.splitlines()):
        if line.startswith(RESULT_MARKER):
            return json.loads(line[len(RESULT_MARKER):])
    raise RunError("no BENCHGATE_RESULT line in output")


if __name__ == "__main__":
    sys.exit(main())
