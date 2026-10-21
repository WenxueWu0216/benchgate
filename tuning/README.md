# Node tuning kit

Contents:

* `ansible/`: playbook and role that configure a bench node.
  * Kernel command line: `isolcpus=managed_irq,domain,<bench>`,
    `nohz_full`, `rcu_nocbs`, `irqaffinity=<housekeeping>`, C-states capped
    at C1, `nosmt`, THP off.
  * Kubelet static CPU manager with `reservedSystemCPUs`.
  * Installs and enables the systemd units below, then verifies that
    `/sys/devices/system/cpu/isolated` is populated.
* `systemd/benchgate-tune.{sh,service}`: runtime knobs, applied at boot
  before the kubelet starts. `performance` governor, min = max frequency,
  turbo/boost off, SMT off, THP off, NUMA balancing off, watchdog off, and
  writeback/workqueue cpumask set to the housekeeping cores. Missing sysfs
  files are skipped, not treated as errors.
* `systemd/benchgate-irq-pin.{sh,service,timer}`: moves every movable IRQ
  to the housekeeping cores and re-pins every 10 minutes (drivers reset
  affinity). `irqbalance` is disabled.
* `measure_variance.sh`: measures run-to-run variance (CV and robust CV of
  per-run p50/p99, worst run, timer-wakeup jitter) and compares two
  measurements.

## Status: before/after variance on real hardware is PENDING

The development sandbox is a shared cloud VM with no cpufreq interface, no
SMT control and no way to change the kernel command line, so tuning cannot
be applied or measured there. Its untuned baseline is recorded for reference
in `docs/results/variance-sandbox-untuned.json`:

```
[sandbox-untuned] CPU 3  governor=None smt=notsupported isolated=-
  pointcloud  p50   1171.0 us  CV  6.74%  robust CV  2.23%  CV(p99) 27.83%  worst run  34.9%
  planner     p50    358.1 us  CV  4.26%  robust CV  3.38%  CV(p99)  8.30%  worst run  13.6%
  matrix      p50   1004.8 us  CV  1.82%  robust CV  1.00%  CV(p99) 31.85%  worst run   4.7%
  timer jitter p50 107 us  p99 368 us  max 6802 us
```

Expected after tuning (a hypothesis to test, not a result): robust CV of p50
≤ 0.5%, CV of p99 ≤ 3%, and timer jitter p99 in the low tens of µs.

## Exact procedure

You need a Linux machine you can reboot, with root access and with
frequency and SMT controls exposed. Bare metal is best. A cloud option is
GCE sole-tenant or `c3-*-metal`, or AWS `*.metal`. Ordinary VMs usually
hide cpufreq. Ubuntu 22.04/24.04 is assumed.

```bash
# 0. On the target machine: get the code and build the workloads.
sudo apt-get update && sudo apt-get install -y git cmake g++ python3 ansible
git clone https://github.com/<you>/benchgate && cd benchgate
cmake -S workloads -B workloads/build -DCMAKE_BUILD_TYPE=Release
cmake --build workloads/build -j

# 1. Pick bench and housekeeping CPUs. Keep whole physical cores together:
lscpu -e=CPU,CORE,SOCKET,NODE
#    e.g. 8 cores / 16 threads: housekeeping 0-1, bench 2-7 (SMT siblings go away with nosmt)

# 2. BEFORE: measure the untouched machine (benchmark pinned to one bench CPU).
tuning/measure_variance.sh --label before --runs 30 --cpu 3

# 3. Tune (reboots once to apply the kernel command line).
cat > /tmp/inv.ini <<'EOF'
[bench]
localhost ansible_connection=local bench_cpus=2-7 housekeeping_cpus=0-1
EOF
sudo ansible-playbook -i /tmp/inv.ini tuning/ansible/site.yml

# 4. Verify, after the reboot:
cat /sys/devices/system/cpu/isolated                         # -> 2-7
cat /sys/devices/system/cpu/cpu3/cpufreq/scaling_governor    # -> performance
cat /sys/devices/system/cpu/smt/control                      # -> off / forceoff
cat /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null || cat /sys/devices/system/cpu/cpufreq/boost
grep -E 'isolcpus|nohz_full' /proc/cmdline
systemctl status benchgate-tune benchgate-irq-pin.timer --no-pager
grep -v '^ *[0-9]*: *0 *0' /proc/interrupts | awk 'NR==1 || $5>0' | head   # bench CPUs should be ~quiet

# 5. AFTER: same command, same CPU.
tuning/measure_variance.sh --label after --runs 30 --cpu 3

# 6. Compare, and commit both JSON files to docs/results/.
tuning/measure_variance.sh --compare variance-before.json variance-after.json
cp variance-before.json docs/results/variance-<host>-before.json
cp variance-after.json  docs/results/variance-<host>-after.json

# 7. Feed the measured noise back into the detection simulation:
python -m benchgate.sim.collect --out data/calibration/<host>_tuned.jsonl.gz --scale 0.5
python -m benchgate.sim.detection --pool data/calibration/<host>_tuned.jsonl.gz --out docs/results/<host>
```

Optional extra checks once perf is available on the tuned host:

```bash
# Hardware counters and scheduling events for one run; parsed by the C++ parser.
perf stat -x, -e task-clock,cycles,instructions,cache-misses,context-switches,cpu-migrations \
  taskset -c 3 workloads/build/benchgate_workloads --workload matrix 2>&1 >/dev/null \
  | workloads/build/trace_parser --format perf-stat
sudo perf record -e sched:sched_switch -C 3 -- sleep 10 &
taskset -c 3 workloads/build/benchgate_workloads --workload planner --iterations 5000 >/dev/null
wait; sudo perf script -F comm,pid,cpu,time,event,trace \
  | workloads/build/trace_parser --format perf-sched | tail -1
sudo bpftrace trace/bpftrace/runqlat.bt   # Ctrl-C after a run; pipe into --format bpftrace-hist
```

## On GKE

GKE's Container-Optimized OS does not allow changing the kernel command line
or the governor. `deploy/gke/create-cluster.sh` gets as close as GKE
allows: `--threads-per-core=1` (SMT off), static CPU manager (exclusive
cores), no autoupgrade, and optionally a sole-tenant node group. For full
tuning, run bench nodes as self-managed hosts (this playbook) and join
them to the cluster, or use bare-metal instances.
