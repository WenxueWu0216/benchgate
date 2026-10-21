# Capacity model and node-count recommendation

Model: `benchgate/capacity/queue_sim.py`. Results:
[`results/capacity.md`](results/capacity.md). Reproduce with
`python -m benchgate.capacity.queue_sim --out docs/results` (about 5 s).

## Model

A discrete-event simulation of the bench fleet as a FIFO multi-server queue
(c nodes, one job per node at a time), over 28 simulated days.

* **Arrivals.** 150 gate requests per weekday. 80% of them land between
  09:00 and 19:00, and weekends carry 20% of weekday load. This is a
  non-homogeneous Poisson process, sampled by thinning.
* **Fan-out.** Each request becomes 6 paired jobs (one per workload). Jobs
  of one request can run on different nodes in parallel. CI wait is the time
  from request to its *last* job finishing.
* **Service time.** 45 s of overhead (scheduling, warm image, setup) +
  repetitions × 2 arms × 15 s per run. Lognormal, CV 0.25. 2% of attempts
  fail and are retried.
* **Cost.** $1.00 per node-hour (a dedicated 16-vCPU VM or small bare-metal
  host at committed-use prices). Budget $6,000/month, which allows at most
  **8 nodes**.
* **Validation.** With exponential arrivals and service, the simulation
  reproduces Erlang C: mean queue wait 35.4 s analytic vs 35.5 s simulated
  at ρ = 0.69, and 4.9 vs 4.8 s at ρ = 0.55.

Every assumption is a field in `Workload` / `Costs`; change it and re-run.

## Utilization vs CI wait (weekday peak, p95)

| nodes | $/month | 5 reps | 8 reps | 10 reps | 20 reps |
|---:|---:|---:|---:|---:|---:|
| 5 | 3,650 | 27 min | – | 310 min | – |
| 6 | 4,380 | 16 min | – | 175 min | – |
| 8 | 5,840 | 10 min (20% util) | **22 min (29%)** | 44 min (35%) | 450 min (66%) |
| 9 | 6,570 | 8 min | 17 min | 28 min (31%) | 346 min |
| 10 | 7,300 | 8 min | 14 min | 22 min | 261 min |
| 16 | 11,680 | 6 min | – | 12 min | 39 min |
| 20 | 14,600 | 6 min | – | 11 min | 25 min (26%) |

Two things stand out. First, wait time grows very steeply once peak load
nears capacity: 10 reps on 8 nodes is 35% average utilization but still
44 min p95, because the queue builds up at peak. Second, the fleet that
meets a 30-minute p95 runs at only 20–35% average utilization. That is
what bursty, fan-out CI traffic costs, not waste.

## Recommendation: 8 tuned nodes, 8 runs per side, confirmation on

Within $6,000/month (8 nodes):

1. **Spend on node quality before node count.** With tuned nodes (isolcpus,
   fixed frequency, SMT off, IRQs pinned; `tuning/`), 5 runs per side
   already detect a 2% regression 98% of the time and 10 runs 100% of the
   time, at 0.1% false alarms. On untuned VMs the same gate needs 20 runs
   for 74%, which needs **18 nodes** to meet a 30-minute p95 and is 2.2×
   over budget.
2. **Run 8 repetitions per side** on 8 nodes. The modelled p95 peak wait is
   22 min at 29% average utilization, leaving headroom for growth and for
   confirmation re-runs (only flagged workloads are re-run, so their load is
   small). Expected 2% detection on tuned nodes is ≥ 98% (between the
   simulated 5-run and 10-run results).
3. **Use shorter runs, not fewer.** At fixed node-time, splitting each side
   into more, shorter processes is the cheapest power increase there is
   (13% → 84% for a 2% regression on the untuned model going from 5×600 to
   30×100 iterations). Lower `iterations` per run until per-process
   overhead starts to dominate.
4. **Use off-peak hours for history.** Nights and weekends run below 15%
   utilization. Run the full suite on every main-branch commit there and
   run PELT/CUSUM over the history: it catches creep that no pairwise gate
   sees.
5. **Grow when** the measured p95 wait at peak exceeds 30 min or the PR rate
   passes ~180/weekday. At that point a 9th node buys 10 reps per side at
   28 min p95.

If the nodes cannot be tuned (shared cloud VMs), BenchGate can honestly
promise to catch 5% regressions (99% at 10 runs) but not 2% (38%). Either
set `min_effect` to match, or rely on history analysis for small shifts:
PELT finds 94% of 2% steps on that model.

## Caveats

* Load and per-run duration are assumptions. Re-run the model with measured
  PR rates and measured job durations from the `jobs` table (the fleet
  dashboard shows both).
* Detection power for tuned nodes comes from a noise model, not from
  measurement. It becomes real when `tuning/README.md` is run on the target
  SKU and the measured CV is fed back (`benchgate.sim.noise.fit_from_runs`).
