#!/usr/bin/env bash
# Measure run-to-run variance of the BenchGate workloads on this machine.
#
#   tuning/measure_variance.sh --label before --runs 30 --cpu 3
#   sudo ansible-playbook -i inventory.ini tuning/ansible/site.yml   # tune + reboot
#   tuning/measure_variance.sh --label after  --runs 30 --cpu 3
#   tuning/measure_variance.sh --compare variance-before.json variance-after.json
#
# Each run is a fresh process (that is what the gate compares). Per workload
# it reports the CV and robust CV (MAD-based) of the per-run p50 and p99,
# the worst run, and the timer-wakeup jitter on the pinned CPU.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
BIN=${BIN:-$REPO/workloads/build/benchgate_workloads}
LABEL=run; RUNS=30; ITER=300; CPU=""; WORKLOADS="pointcloud planner matrix"; OUT=""

compare() {
  python3 - "$1" "$2" <<'PY'
import json, sys
a, b = (json.load(open(p)) for p in sys.argv[1:3])
print(f"{'workload':12} {'metric':6} {a['label']:>12} {b['label']:>12} {'ratio':>8}")
for w in a["workloads"]:
    for k in ("cv_p50_pct", "robust_cv_p50_pct", "cv_p99_pct", "max_dev_p50_pct"):
        x, y = a["workloads"][w][k], b["workloads"][w][k]
        print(f"{w:12} {k:18} {x:8.2f}% {y:8.2f}% {x / y if y else float('inf'):7.1f}x")
for k in ("jitter_p99_us", "jitter_max_us"):
    print(f"{'timer':12} {k:18} {a['jitter'][k]:8.1f}  {b['jitter'][k]:8.1f}")
PY
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --label) LABEL=$2; shift 2;;
    --runs) RUNS=$2; shift 2;;
    --iterations) ITER=$2; shift 2;;
    --cpu) CPU=$2; shift 2;;
    --workloads) WORKLOADS=$2; shift 2;;
    --out) OUT=$2; shift 2;;
    --compare) compare "$2" "$3"; exit 0;;
    *) echo "unknown arg $1"; exit 2;;
  esac
done
OUT=${OUT:-variance-$LABEL.json}
[[ -x $BIN ]] || { cmake -S "$REPO/workloads" -B "$REPO/workloads/build" -DCMAKE_BUILD_TYPE=Release >/dev/null; cmake --build "$REPO/workloads/build" -j >/dev/null; }
if [[ -z $CPU ]]; then
  # Default: first isolated CPU if any, else the last CPU.
  iso=$(cat /sys/devices/system/cpu/isolated 2>/dev/null || true)
  CPU=${iso%%[-,]*}; [[ -n $CPU ]] || CPU=$(( $(nproc) - 1 ))
fi
PIN=(taskset -c "$CPU")
command -v chrt >/dev/null && [[ $EUID -eq 0 ]] && PIN=(chrt -f 50 "${PIN[@]}")

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
echo "measuring $RUNS runs x $ITER iterations per workload on CPU $CPU ($LABEL)" >&2
for ((r=0; r<RUNS; r++)); do
  for w in $WORKLOADS; do   # interleave workloads so drift hits all equally
    "${PIN[@]}" "$BIN" --workload "$w" --iterations "$ITER" --out "$TMP/$w-$r.json"
  done
done

python3 - "$TMP" "$LABEL" "$CPU" "$OUT" $WORKLOADS <<'PY'
import glob, json, os, platform, statistics, sys, time
tmp, label, cpu, out, *workloads = sys.argv[1:]
cpu = int(cpu)

def read(p):
    try: return open(p).read().strip()
    except OSError: return None

def robust_cv(xs):
    m = statistics.median(xs)
    return 100 * 1.4826 * statistics.median(abs(x - m) for x in xs) / m

res = {}
for w in workloads:
    runs = [json.load(open(f)) for f in sorted(glob.glob(f"{tmp}/{w}-*.json"))]
    p50 = [r["summary"]["p50_ns"] for r in runs]
    p99 = [r["summary"]["p99_ns"] for r in runs]
    m = statistics.median(p50)
    res[w] = {"runs": len(runs), "median_p50_us": m / 1e3,
              "cv_p50_pct": 100 * statistics.stdev(p50) / statistics.mean(p50),
              "robust_cv_p50_pct": robust_cv(p50),
              "cv_p99_pct": 100 * statistics.stdev(p99) / statistics.mean(p99),
              "max_dev_p50_pct": 100 * max(abs(x / m - 1) for x in p50)}

# Timer-wakeup jitter on the pinned CPU: 5000 x 1 ms sleeps.
os.sched_setaffinity(0, {cpu})
lat = []
for _ in range(5000):
    t = time.perf_counter(); time.sleep(0.001)
    lat.append((time.perf_counter() - t - 0.001) * 1e6)
lat.sort()
jitter = {"jitter_p50_us": lat[len(lat) // 2], "jitter_p99_us": lat[int(len(lat) * .99)],
          "jitter_max_us": lat[-1]}
cmd = read("/proc/cmdline") or ""
env = {"kernel": platform.release(), "cmdline": cmd, "cpu": cpu,
       "governor": read(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_governor"),
       "no_turbo": read("/sys/devices/system/cpu/intel_pstate/no_turbo"),
       "boost": read("/sys/devices/system/cpu/cpufreq/boost"),
       "smt": read("/sys/devices/system/cpu/smt/control"),
       "isolated": read("/sys/devices/system/cpu/isolated"),
       "model": next((l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo") if l.startswith("model name")), None)}
doc = {"label": label, "time": time.time(), "env": env, "workloads": res, "jitter": jitter}
json.dump(doc, open(out, "w"), indent=2)
print(f"[{label}] CPU {cpu}  governor={env['governor']} smt={env['smt']} isolated={env['isolated'] or '-'}")
for w, r in res.items():
    print(f"  {w:11} p50 {r['median_p50_us']:8.1f} us  CV {r['cv_p50_pct']:5.2f}%  robust CV {r['robust_cv_p50_pct']:5.2f}%"
          f"  CV(p99) {r['cv_p99_pct']:5.2f}%  worst run {r['max_dev_p50_pct']:5.1f}%")
print(f"  timer jitter p50 {jitter['jitter_p50_us']:.0f} us  p99 {jitter['jitter_p99_us']:.0f} us  max {jitter['jitter_max_us']:.0f} us")
print(f"wrote {out}")
PY
