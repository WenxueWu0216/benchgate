# Statistics: method and measured results

Full tables: [`results/detection.md`](results/detection.md),
[`results/changepoint.md`](results/changepoint.md). Raw data:
`results/*.json`. Reproduce (about 4 minutes on 4 cores):

```bash
python -m benchgate.sim.collect --out data/calibration/sandbox_runs.jsonl.gz  # ~6 min, real runs
python -m benchgate.sim.detection --out docs/results
python -m benchgate.sim.history   --out docs/results
```

## The gate in one paragraph

Each run (one process) is reduced to its p50 and p99. For each workload ×
metric, a one-sided Mann-Whitney U test asks whether head's runs tend to be
slower than base's. A 95% BCa bootstrap over runs gives the relative change
of the median. Holm-Bonferroni corrects across every test in the gate at
α = 1%. A test is a **regression** only if it is significant after
correction, its CI excludes zero, and the estimate is ≥ 1%. A flagged
workload is re-run as an independent batch and must be flagged again
before the merge is blocked. Base and head always run interleaved
(ABBA…) on the same node.

## How it was evaluated

Three independent sources of noise, so the conclusions do not rest on one
model:

1. **Synthetic, tuned bare metal.** Run-to-run sd 0.4%, within-run sd 3%,
   rare small spikes. This is the regime vendors and published tuning
   guides report after isolcpus, fixed frequency and SMT off. It is an
   assumption until `tuning/README.md` is run on real hardware.
2. **Synthetic, untuned cloud VM.** Run-to-run sd 1.5%, 5% of runs hit by
   a noisy neighbour (+8% mean), more spikes.
3. **Real.** The C++ workloads on the development sandbox (shared 4-vCPU
   Xeon VM, nothing tunable), with **real injected slowdowns** through the
   harness flag. 960 runs, conditions shuffled and interleaved. Used two
   ways: directly, by drawing disjoint sets of runs from the pool, and to
   fit a third synthetic model (`sandbox-fit-matrix`: run-to-run sd 3.4%,
   10% contaminated runs at +57%).

The fitted model and the direct real-data evaluation agree. At 5% injected,
10 runs per side, the fitted model detects **34%** and the real matrix +
pointcloud runs **40–47%** (32% across all three workloads). The noise model
captures what matters.

## Headline numbers (10 runs per side, single batch unless noted)

| Environment | False alarms (A/A) | 2% regression | 5% | 10% |
|---|---|---|---|---|
| Tuned bare metal (model) | **1/1000 (0.1%)** | **100%** | 100% | 100% |
| Untuned cloud VM (model) | 9/1000 (0.9%) | 38% | 99% | 100% |
| Dev sandbox, real workloads + real injection | **5/600 (0.8%)** | 4% | 32% | 66% |
| Tuned, with confirmation re-run | **0/5000** (95% upper bound 0.1%) | 100% | 100% | 100% |
| Untuned VM, with confirmation re-run | **0/5000** (95% upper bound 0.1%) | 16% | 98% | 100% |

The brief asked for false alarms across 200 clean runs: on real workloads
it is 1/200 (matrix), 2/200 (planner) and 2/200 (pointcloud). The synthetic
models use 1000 (5000 with confirmation) for tighter intervals.

Tail-only regressions ("tail X%" stretches iterations above the p95, so p99
moves and p50 does not): on tuned nodes 99% detection at 2% with 10 runs.
On the untuned VM, 58% at 5%. On the real sandbox, at most 21% even at
10%. The sandbox's p99 is dominated by host noise, so tail regressions are
effectively invisible there.

## Things the numbers say

* **Node quality matters more than statistics.** The same gate goes from
  38% to 100% detection of a 2% regression when the noise model goes from
  untuned VM to tuned bare metal.
* **More short runs beat fewer long runs.** At the same node-time per side
  on the untuned VM, 2% detection is 13% (5×600), 33% (10×300), 67% (20×150),
  84% (30×100). Iterations inside one process are correlated, so extra
  iterations add little information. Extra processes do.
* **Pair base and head on one node.** Comparing a commit's runs with its
  parent's stored runs from a different node, in an 8-node fleet with 1%
  node offsets, gave 76 false flags per 1000 commits on the untuned model
  and 20 on the tuned one. Paired on one node: 8 and 0.04.
* **Confirmation removes false alarms at almost no cost when nodes are
  good.** On the untuned VM it costs a lot of power at 2% (40% → 16%), which
  is another argument for tuning over tolerating noise.
* **History catches what the gate cannot.** Ten consecutive commits each
  0.5% slower: the paired gate caught that in 22% (tuned) and 32% (untuned)
  of histories. PELT caught 100% in both, with 0.0 and 0.04 false flags per
  1000 commits. CUSUM: 100% with about 1 false flag per 1000 commits. PELT
  on the untuned model also finds 94% of 2% steps, versus 42% for the
  pairwise gate, because it averages over many commits on each side of the
  change.

## Validation of the statistics code

* Mann-Whitney U matches `scipy.stats.mannwhitneyu` (exact and asymptotic,
  with ties, all three alternatives) to 1e-6 relative.
* BCa bootstrap matches `scipy.stats.bootstrap(method="BCa")` within 0.006
  absolute. Coverage on 300 simulated datasets: inside [88%, 99%] for a
  nominal 95%.
* PELT gives segmentations identical to `ruptures.Pelt(model="l2")` at the
  same penalty. The default penalty is 3·ln n: 2·ln n (BIC) put a spurious
  change point in about 8% of clean 120-commit histories, 3·ln n in about 2%.
* CUSUM is self-starting (running baseline, frozen while evidence
  accumulates) with h = 8σ, k = 0.5σ. The textbook h = 5 alarmed in about
  30% of clean 100-commit histories, because the baseline mean is estimated
  from a short warmup.
