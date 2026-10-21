"""Trace capture around a benchmark process.

Tiered, because what is available depends heavily on where we run:

1. ``perf stat`` hardware counters (cycles, instructions, cache misses,
   context switches, migrations) and, when the PMU exposes them, uncore IMC
   CAS counts for true DRAM bandwidth. Optional ``perf record`` for a profile.
2. ``bpftrace`` scripts (run-queue latency, off-CPU time) when root + BTF.
3. Always-available fallback: CPU utilization and voluntary/involuntary
   context switches from the child's rusage, plus a timer-wakeup probe thread that measures
   scheduling jitter (how late a 1 ms sleep wakes up) while the benchmark runs.

Inside the cloud sandbox perf is typically blocked (perf_event_paranoid,
no PMU in the VM) and bpftrace absent; capture degrades to tier 3 and
records *why* each tier was skipped, instead of failing the run.

Raw perf/bpftrace output is converted to rows by the C++ ``trace_parser``
(streaming) when built, with a small Python fallback for perf-stat CSV.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import tempfile
import threading
import time

import numpy as np

REPO = Path(__file__).resolve().parents[2]
TRACE_PARSER = os.environ.get("BENCHGATE_TRACE_PARSER",
                              str(REPO / "workloads" / "build" / "trace_parser"))
BPF_DIR = REPO / "trace" / "bpftrace"

PERF_EVENTS = ["task-clock", "context-switches", "cpu-migrations", "page-faults",
               "cycles", "instructions", "cache-references", "cache-misses",
               "LLC-load-misses"]
# Present on Intel server parts with uncore PMU access; each CAS = 64 bytes.
IMC_EVENTS = ["uncore_imc/cas_count_read/", "uncore_imc/cas_count_write/"]


@dataclass
class Capabilities:
    perf: bool = False
    perf_uncore: bool = False
    bpftrace: bool = False
    reasons: dict = field(default_factory=dict)


def _try(cmd: list[str], timeout: float = 10) -> tuple[bool, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, (p.stderr or p.stdout)[-300:]
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)


def detect_capabilities() -> Capabilities:
    caps = Capabilities()
    if not shutil.which("perf"):
        caps.reasons["perf"] = "perf binary not found"
    else:
        ok, msg = _try(["perf", "stat", "-x,", "-e", "cycles,instructions", "true"])
        # perf exits 0 even when counters are "<not supported>" in a VM.
        if ok and "<not supported>" not in msg and "<not counted>" not in msg:
            caps.perf = True
        else:
            paranoid = (Path("/proc/sys/kernel/perf_event_paranoid").read_text().strip()
                        if Path("/proc/sys/kernel/perf_event_paranoid").exists() else "?")
            caps.reasons["perf"] = f"hardware counters unavailable (perf_event_paranoid={paranoid}): {msg.strip()[:160]}"
        if caps.perf:
            ok, msg = _try(["perf", "stat", "-x,", "-a", "-e", ",".join(IMC_EVENTS), "true"])
            caps.perf_uncore = ok and "<not supported>" not in msg and "event syntax error" not in msg
            if not caps.perf_uncore:
                caps.reasons["perf_uncore"] = "uncore IMC events unavailable; memory bandwidth will be estimated from LLC misses"
    if not shutil.which("bpftrace"):
        caps.reasons["bpftrace"] = "bpftrace binary not found"
    elif os.geteuid() != 0:
        caps.reasons["bpftrace"] = "bpftrace requires root"
    elif not Path("/sys/kernel/btf/vmlinux").exists():
        caps.reasons["bpftrace"] = "kernel BTF not available"
    else:
        caps.bpftrace = True
    return caps


class JitterProbe:
    """cyclictest-lite: a thread that sleeps 1 ms in a loop and records how
    late each wakeup is. Late wakeups while the benchmark is running mean the
    node is being disturbed (interrupts, other tasks, frequency transitions)."""

    def __init__(self, period_s: float = 0.001):
        self.period_s = period_s
        self.lateness_us: list[float] = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        nxt = time.perf_counter() + self.period_s
        while not self._stop.is_set():
            time.sleep(max(0.0, nxt - time.perf_counter()))
            now = time.perf_counter()
            self.lateness_us.append((now - nxt) * 1e6)
            nxt = now + self.period_s

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(timeout=1)

    def summary(self) -> dict:
        a = np.asarray(self.lateness_us)
        if a.size == 0:
            return {"samples": 0}
        return {"samples": int(a.size), "p50_us": float(np.percentile(a, 50)),
                "p99_us": float(np.percentile(a, 99)), "max_us": float(a.max())}


def parse_perf_stat_rows(text: str) -> list[dict]:
    """Use the C++ streaming parser when built, else a minimal Python parser."""
    if Path(TRACE_PARSER).exists():
        p = subprocess.run([TRACE_PARSER, "--format", "perf-stat"], input=text,
                           capture_output=True, text=True, timeout=30)
        return [json.loads(line) for line in p.stdout.splitlines() if line.strip()]
    rows = []
    for line in text.splitlines():
        f = line.split(",")
        if len(f) < 3 or line.startswith("#"):
            continue
        try:
            v = float(f[0])
        except ValueError:
            v = None
        rows.append({"kind": "counter", "event": f[2], "value": v, "unit": f[1] or None})
    return rows


def derive_metrics(rows: list[dict], wall_s: float) -> dict:
    c = {r["event"]: r.get("value") for r in rows if r.get("kind") == "counter"}
    m: dict = {}
    if c.get("cycles") and c.get("instructions"):
        m["ipc"] = c["instructions"] / c["cycles"]
    if c.get("task-clock") is not None and wall_s > 0:
        m["cpu_utilization"] = c["task-clock"] / 1e3 / wall_s  # msec -> s
    for k in ("context-switches", "cpu-migrations", "cache-misses", "page-faults"):
        if c.get(k) is not None:
            m[k.replace("-", "_")] = c[k]
    reads, writes = c.get(IMC_EVENTS[0]), c.get(IMC_EVENTS[1])
    if reads is not None and writes is not None and wall_s > 0:
        # perf reports IMC counts pre-scaled to MiB.
        m["mem_bw_gbps"] = (reads + writes) * 1048576 / 1e9 / wall_s
        m["mem_bw_source"] = "uncore_imc"
    elif c.get("LLC-load-misses") and wall_s > 0:
        m["mem_bw_gbps"] = c["LLC-load-misses"] * 64 / 1e9 / wall_s
        m["mem_bw_source"] = "llc_miss_estimate"
    return m


class TraceCapture:
    """Wrap a command with whatever tracing is available on this host."""

    def __init__(self, record: bool = False, bpftrace_scripts: list[str] | None = None,
                 out_dir: str | None = None):
        self.caps = detect_capabilities()
        self.record = record
        self.bpftrace_scripts = bpftrace_scripts if bpftrace_scripts is not None else ["runqlat.bt"]
        self.out_dir = Path(out_dir or tempfile.mkdtemp(prefix="benchgate-trace-"))

    def run(self, cmd: list[str], timeout: float, preexec_fn=None) -> tuple[str, dict]:
        trace: dict = {"capabilities": {"perf": self.caps.perf, "perf_uncore": self.caps.perf_uncore,
                                        "bpftrace": self.caps.bpftrace},
                       "skipped": dict(self.caps.reasons)}
        stat_file = self.out_dir / "perf_stat.csv"
        full = list(cmd)
        if self.caps.perf:
            events = PERF_EVENTS + (IMC_EVENTS if self.caps.perf_uncore else [])
            pre = ["perf", "stat", "-x,", "-o", str(stat_file), "-e", ",".join(events)]
            if self.caps.perf_uncore:
                pre.insert(2, "-a")  # uncore events are system-wide
            if self.record:
                pre = ["perf", "record", "-g", "-o", str(self.out_dir / "perf.data"), "--"] + pre
            full = pre + ["--"] + full

        bpf_procs = []
        if self.caps.bpftrace:
            for s in self.bpftrace_scripts:
                path = BPF_DIR / s
                if path.exists():
                    f = open(self.out_dir / f"{path.stem}.txt", "w")
                    bpf_procs.append((s, subprocess.Popen(["bpftrace", str(path)], stdout=f,
                                                          stderr=subprocess.STDOUT), f))
            time.sleep(1.0)  # let probes attach before the workload starts

        t0 = time.perf_counter()
        # Children's rusage delta: exact CPU time and context switches of the
        # workload, however short it is (runs are executed one at a time).
        ru0 = resource.getrusage(resource.RUSAGE_CHILDREN)
        with JitterProbe() as probe:
            proc = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, preexec_fn=preexec_fn)
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise
        wall = time.perf_counter() - t0
        ru1 = resource.getrusage(resource.RUSAGE_CHILDREN)

        for name, p, f in bpf_procs:
            p.send_signal(2)  # SIGINT makes bpftrace print its maps
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
            f.close()
            trace.setdefault("bpftrace", {})[name] = str(self.out_dir / f"{Path(name).stem}.txt")

        if proc.returncode != 0:
            from benchgate.runner import RunError
            raise RunError(f"workload exited {proc.returncode}: {err.strip()[:500]}")

        trace["wall_s"] = wall
        trace["sched_jitter"] = probe.summary()
        cpu_s = (ru1.ru_utime - ru0.ru_utime) + (ru1.ru_stime - ru0.ru_stime)
        trace["proc"] = {"cpu_s": cpu_s, "cpu_utilization": cpu_s / wall if wall else None,
                         "voluntary_cs": ru1.ru_nvcsw - ru0.ru_nvcsw,
                         "involuntary_cs": ru1.ru_nivcsw - ru0.ru_nivcsw}
        if self.caps.perf and stat_file.exists():
            rows = parse_perf_stat_rows(stat_file.read_text())
            trace["perf"] = derive_metrics(rows, wall)
        if self.record:
            trace["perf_data"] = str(self.out_dir / "perf.data")
        return out, trace
