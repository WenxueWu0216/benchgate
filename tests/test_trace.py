import json
from pathlib import Path
import subprocess

from benchgate.trace.capture import TraceCapture, derive_metrics, detect_capabilities, parse_perf_stat_rows

DATA = Path(__file__).resolve().parents[1] / "workloads" / "tests" / "data"


def test_capabilities_never_raise():
    caps = detect_capabilities()
    assert isinstance(caps.reasons, dict)
    if not caps.perf:
        assert "perf" in caps.reasons


def test_capture_degrades_gracefully(binary):
    tc = TraceCapture()
    out, trace = tc.run([binary, "--workload", "matrix", "--iterations", "50"], timeout=60)
    assert json.loads(out)["workload"] == "matrix"
    assert trace["sched_jitter"]["samples"] > 0
    assert "proc" in trace and trace["proc"]["cpu_s"] >= 0
    if not trace["capabilities"]["perf"]:
        assert "perf" in trace["skipped"]


def test_perf_stat_parsing_and_metrics(binary):
    rows = parse_perf_stat_rows((DATA / "perf_stat.csv").read_text())
    m = derive_metrics(rows, wall_s=1.5)
    assert abs(m["ipc"] - 9876543210 / 4012345678) < 1e-6
    assert abs(m["cpu_utilization"] - 1.52345 / 1.5) < 1e-6


def test_cpp_parser_streams_large_input(binary):
    parser = Path(binary).parent / "trace_parser"
    line = (" bench  1234 [002]  {t:.6f}: sched:sched_switch: prev_comm=bench prev_pid=1234 "
            "prev_prio=120 prev_state=R ==> next_comm=k next_pid=7 next_prio=120\n"
            "      k     7 [002]  {t2:.6f}: sched:sched_switch: prev_comm=k prev_pid=7 prev_prio=120 "
            "prev_state=S ==> next_comm=bench next_pid=1234 next_prio=120\n")
    text = "".join(line.format(t=i * 0.001, t2=i * 0.001 + 0.00002) for i in range(50000))
    p = subprocess.run([str(parser), "--format", "perf-sched", "--pid", "1234"], input=text,
                       capture_output=True, text=True, check=True)
    summary = json.loads(p.stdout.strip().splitlines()[-1])
    assert summary["offcpu"] == 50000 and summary["preempted"] == 50000
    assert abs(summary["max_preempt_ns"] - 20000) < 50
