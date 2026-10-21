# BenchGate design

BenchGate runs a benchmark suite for every commit on a fleet of bench nodes
and blocks merges that make the code measurably slower. This document covers
why that is harder than it sounds, what this system treats as a "real"
regression, how it is built, and the questions that decide how big the fleet
has to be.

Numbers quoted here come from `docs/results/` and are reproducible with the
commands listed there.

## 1. Why performance benchmarking is hard

Re-running a benchmark gives a different number every time. Most of that
spread comes from the machine, not from the code:

| Source | Mechanism | Typical effect | Mitigation in BenchGate |
|---|---|---|---|
| **CPU frequency** | DVFS governors change clocks with load. Turbo depends on temperature, power budget and how many cores are busy, so a neighbour waking up slows you down. | 5–30% between runs; bimodal distributions | `performance` governor, min=max frequency, turbo off (`tuning/`) |
| **SMT siblings** | Two hyperthreads share L1/L2, the TLB and execution ports. | 10–40% when the sibling is busy | `nosmt` / `smt/control=off`; on GKE `--threads-per-core=1` |
| **Interrupts and softirqs** | NIC, disk and timer interrupts land on the core under test and pollute its cache. | Tail spikes of 10–1000 µs | IRQ affinity to housekeeping cores, `irqbalance` off, `managed_irq` isolation |
| **Kernel housekeeping** | Scheduler tick, RCU callbacks, kworkers, vmstat, watchdog. | Periodic µs-scale jitter | `isolcpus`, `nohz_full`, `rcu_nocbs`, workqueue cpumask, watchdog off |
| **Noisy neighbours** | Shared LLC, memory bandwidth and power budget. In the cloud, other tenants and hypervisor steal. | Whole runs slowed by 30–60% | dedicated nodes, one job per node (leases + anti-affinity), sole tenancy |
| **Memory layout** | ASLR, environment size, link order and page colouring change alignment and cache conflicts (Mytkowicz et al., *Producing wrong data without doing anything obviously wrong*, ASPLOS 2009). | A fixed per-process offset of 1–5% | treat the **process** as the unit of sampling; many short runs, not one long one |
| **Memory management** | THP compaction, NUMA balancing, page faults on first touch. | Stalls of ms | THP off, NUMA balancing off, warmup iterations |
| **Thermal and firmware** | Throttling, SMIs, microcode and BIOS updates. | Drift over hours, steps after upgrades | fixed fleet image, autoupgrade off, re-baseline after any upgrade, per-node drift dashboard |

Measured on the development sandbox (a shared 4-vCPU cloud VM where none of
these mitigations are available; `docs/results/variance-sandbox-untuned.json`,
`data/calibration/`):

* Run-to-run CV of the per-run median: **1.8–6.7%**; robust (MAD) CV 1.0–3.4%.
  The gap between the two is the heavy tail. In the 120-run calibration
  pool, about **10% of runs were "contaminated"**: the whole process ran
  31–57% slower on average.
* CV of the per-run p99: **8–32%**.
* Timer wakeup lateness on the pinned CPU: p50 107 µs, p99 368 µs, max 6.8 ms.

Two consequences shape everything below:

1. **Iterations within one process are not independent.** They share the
   frequency, the memory layout and the neighbours of that process. Pooling
   500 iterations × 10 runs into 5000 "samples" gives confidence intervals
   that are far too narrow and gates that fire constantly. BenchGate reduces
   each run to one number and does statistics over runs.
2. **The noise is heavy-tailed, not Gaussian.** A t-test on means is
   dominated by the one contaminated run. BenchGate uses rank-based tests
   and medians.

## 2. What makes a regression "real"

A change is reported as a regression only when **all** of these hold:

1. **Statistically significant at the right unit.** A one-sided Mann-Whitney
   U test on per-run statistics (p50 and p99 of each run). It is
   distribution-free and robust to the contaminated runs above. The
   null distribution is exact for the usual 10-vs-10 case.
2. **Corrected for multiple comparisons.** A gate tests every workload ×
   metric pair. Holm-Bonferroni keeps the family-wise false-alarm rate at
   α = 1% however many tests are added.
3. **Practically significant.** A 95% BCa bootstrap CI of the relative
   change (resampling runs) must exclude zero, and the point estimate must
   be ≥ 1% (`min_effect`). A real 0.3% slowdown is not worth blocking a merge.
4. **Reproducible.** A flagged workload is re-run as an independent paired
   batch, and the merge is blocked only if that batch is flagged again.
   This takes the per-PR false-alarm rate on an untuned VM from 0.8% to
   below what 5000 simulated A/A comparisons could detect (0 of 5000).
5. **Measured under the same conditions.** Base and head run in **one job on
   one node, interleaved ABBA ABBA…**. Node-to-node offsets and slow drift
   cancel. Without pairing, in the history simulation, unpaired
   commit-vs-parent comparisons across an 8-node fleet with 1% node offsets
   raised **76 false flags per 1000 commits**. Paired: 8.

Two cases are deliberately *not* "pass":

* **Inconclusive.** Nothing was significant, but the CI still allows more
  than a 5% slowdown. That means the measurement was too noisy to rule out a
  real problem. The check is neutral and asks for more runs; it does not say
  "pass".
* **Creep.** Ten commits that each add 0.5% are each below `min_effect`.
  A pairwise gate catches that 22% of the time even on tuned nodes. History
  analysis (PELT and CUSUM over per-commit medians) catches it 100% of the
  time (`docs/results/changepoint.md`). The pairwise gate protects each
  merge; the history job protects the trend.

Secondary evidence that makes a verdict credible to a human: trace counters
(IPC, cache misses, context switches, memory bandwidth, scheduling jitter)
recorded with every run. A slowdown together with a jump in
`involuntary_cs` or `sched_jitter_p99_us` on the same node points at the
machine, not the code.

## 3. Architecture

```
 GitHub PR ──► Actions: benchgate.yml ──► POST /compare {base, head, images}
                                              │
                         ┌────────────────────▼─────────────────────┐
                         │ BenchGate service (benchgate serve)       │
                         │  API ─► Orchestrator ─► Executor          │
                         │          │  queue, retries/backoff,       │
                         │          │  deadlines, node leases        │
                         │  Gate (stats) ◄── runs ──┐                │
                         │  Store: DuckDB | BigQuery │               │
                         └──────────┬──────────────┬─┘               │
                     Lease objects  │              │ batch/v1 Job per attempt
                     (coord.k8s.io) ▼              ▼                 │
                  ┌───────────┐ ┌───────────┐ ┌───────────┐          │
                  │bench node │ │bench node │ │bench node │ tainted, labelled,
                  │ runner pod│ │ runner pod│ │   idle    │ tuned, 1 job each
                  └─────┬─────┘ └───────────┘ └───────────┘
                        │ python -m benchgate.runner
                        │   ABBA: base binary (copied from base image by an
                        │   init container) / head binary, N repetitions,
                        │   optional perf / bpftrace / jitter probe
                        ▼
                 BENCHGATE_RESULT {...} on stdout ─► orchestrator reads the pod log
  ◄── check run "BenchGate" (verdict table + plot), conclusion=failure blocks merge
  Grafana ◄── same tables (per-module, per-commit and fleet dashboards)
```

| Component | Code | Notes |
|---|---|---|
| Workloads | `workloads/` (C++17, CMake) | point-cloud crop + voxel grid, A* with a binary heap, GEMM + Cholesky (Kalman update shape). Per-iteration latency to JSON. `--slowdown-mode uniform\|tail --slowdown-pct P` injects a known regression. Checksums detect wrong answers. |
| Runner | `benchgate/runner.py` | Runs N repetitions, pins to CPUs, fingerprints the node (governor, SMT, isolcpus...), checks checksums, prints one result line. |
| Orchestrator | `benchgate/orchestrator.py` | FIFO queue; a node runs one job at a time; heartbeat-renewed leases; failed attempts retry with exponential backoff on a *different* node; an attempt whose lease was lost is discarded. |
| Executors | `benchgate/executors.py` | `LocalExecutor` (CPU sets as nodes) and `KubernetesExecutor` (batch/v1 Job pinned to a node, `backoffLimit: 0`, `activeDeadlineSeconds`, pod anti-affinity, Guaranteed QoS). |
| Leases | `benchgate/lease.py` | SQLite (`BEGIN IMMEDIATE`) locally; `coordination.k8s.io/v1` Lease per node in-cluster, taken with create (atomic) or replace with `resourceVersion` (compare-and-swap). |
| Trace capture | `benchgate/trace/`, `workloads/tools/trace_parser.cpp` | Uses `perf stat` / `perf record` / bpftrace when available, and records why each was skipped when not. Always collects rusage and a timer-jitter probe. Streaming C++ parser turns perf/bpftrace output into JSONL rows. |
| Storage | `benchgate/storage/` | One interface. DuckDB (single file, default) or BigQuery (partitioned by day, clustered by workload and commit). |
| Statistics | `benchgate/stats/` | Mann-Whitney U (exact and asymptotic, written from scratch and checked against scipy), BCa bootstrap, Holm, PELT (checked against `ruptures`), self-starting CUSUM. |
| CI gate | `.github/workflows/benchgate.yml`, `benchgate/github_check.py` | Posts a `BenchGate` check run. Uses the fleet if `BENCHGATE_URL` is set, otherwise runs paired on the Actions runner. |
| Dashboards | `benchgate/dashboards.py` → `dashboards/grafana/` | Generated from code for both DuckDB and BigQuery. CI checks that they are up to date, and a test runs every DuckDB query against the real schema. |

### Failure handling

* **Two jobs on one node.** Prevented twice, independently: the
  orchestrator's lease, and the pod's required anti-affinity against any
  other bench pod on the same hostname. The kind e2e watches for overlap
  throughout the run.
* **Orchestrator crash.** Leases expire after a TTL (60 s, renewed every
  20 s), so a node is never stranded. Bench Jobs have their own deadline and
  a TTL-after-finished.
* **Stuck or slow node.** The Job's `activeDeadlineSeconds` plus an
  orchestrator-side deadline that also covers time to schedule. A retry
  waits for a node this job has not failed on yet.
* **Broken benchmark.** A checksum mismatch between repetitions fails the
  attempt: timings from code that computes different answers are meaningless.
* **Lease lost mid-run** (network partition, replica race). The attempt is
  discarded and retried, because someone else may have shared the node.

## 4. Gate parameters and why

| Parameter | Default | Reasoning |
|---|---|---|
| unit of sampling | process run | see §1 |
| runs per side | 10 | Tuned nodes detect 2% at 100% with 10 runs (98% with 5). Untuned VMs need 20+ runs to get to 74% (`detection.md` §1). |
| iterations per run | 300 | At a fixed node-time budget, more and shorter runs win: 30×100 detects 2% in 84% of cases versus 33% for 10×300 (`detection.md` §3). The floor is per-process startup and warmup cost. |
| α (family-wise) | 0.01 | Measured A/A false alarms: 0.1% tuned, 0.8% untuned cloud VM, 0.8% (5/600) on real workloads in the sandbox. |
| `min_effect` | 1% | Smallest slowdown worth blocking a merge over. Smaller real shifts are left to history analysis. |
| `inconclusive_effect` | 5% | A CI wider than this is reported as "need more data", not as a pass. |
| confirmation re-run | on | Untuned VM A/A: 0.8% → 0/5000. Cost: 2% detection on an untuned VM drops from 40% to 16%; no measurable cost on tuned nodes. |
| metrics | p50, p99 | p99 regressions are real for control loops, but p99 per run is 4–5× noisier than p50 (`variance-sandbox-untuned.json`), so the gate only finds big tail regressions unless the nodes are tuned. |

## 5. Fleet sizing questions

These have to be answered before buying hardware. `docs/capacity.md` gives
the model and a recommendation.

1. **How noisy is a node?** This decides how many runs per comparison you
   need for a given detection target, which decides node-time per PR.
   Measure it, before and after tuning, on the exact SKU
   (`tuning/measure_variance.sh`).
2. **What regression size must be caught, and how often may the gate be
   wrong?** These are the power and α targets. Being wrong 1% of the time
   on 150 PRs a day means 1.5 developers a day blocked for nothing.
3. **What is the load?** PR pushes per day, how peaky (most arrive
   09:00–19:00), workloads per PR, and run time per workload. Fan-out
   matters: one PR becomes W parallel jobs.
4. **What CI wait is acceptable?** A p95 target at peak, not an average.
   Peaky arrivals mean the fleet runs at only ~30% average utilization at
   the point where it meets a 30-minute p95.
5. **How homogeneous is the fleet?** Same SKU, BIOS, microcode, kernel and
   image. Paired runs make the gate immune to node offsets; history analysis
   is not. Qualify every node (burn-in, then a variance check against the
   fleet) before it takes jobs, and drain it when it drifts (fleet dashboard).
6. **Bare metal, sole-tenant VM, or shared VM?** Shared VMs cannot be tuned
   (no frequency control, steal time). In our numbers, tuning is worth more
   than adding nodes.
7. **What does tuning cost in capacity?** SMT off halves vCPUs, and
   isolcpus reserves housekeeping cores. Plan in cores, not vCPUs.
8. **Elasticity?** Bench nodes should not autoscale freely: a new node must
   be tuned, qualified and re-baselined first. Scale within a pool of
   qualified nodes. Off-peak capacity can run nightly full-history analysis
   and longer suites.
9. **Upgrades.** Kernel, firmware and node-image changes cause step changes.
   Upgrade the whole pool deliberately, then record a new baseline (and tag
   the change in history so PELT does not report it as a regression).

## 6. What is verified, what is pending

| Item | Status |
|---|---|
| C++ workloads, slowdown injection, trace parser | built and unit-tested (ctest), also inside the Docker build |
| Statistics | unit-tested against scipy and `ruptures`; detection and false-alarm rates measured (`docs/results/`) |
| Orchestrator, leases, retries, timeouts | unit-tested with fakes (races, lost leases, deadlines) and end to end with real processes |
| Kubernetes manifests | `kubeconform -strict` on all manifests and on the rendered bench Job; executor tested against a fake API server with real create and compare-and-swap semantics. **kind could not run in the development sandbox** (nested runc cannot create pod sandboxes), so the kind e2e (`deploy/kind/e2e.sh`) runs in GitHub Actions (`ci.yml` → `kind-e2e`). |
| GKE | manifests and `deploy/gke/create-cluster.sh` written for GKE; not applied (needs a GCP project) |
| Docker image | built locally; runner, paired mode and trace fallback smoke-tested in the container |
| Variance before/after tuning on real hardware | **PENDING**: needs a machine where the kernel command line and frequency can be controlled. The exact commands are in `tuning/README.md`. |
