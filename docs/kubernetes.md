# Running on Kubernetes (kind and GKE)

## How a job runs

1. The orchestrator lists nodes labelled `benchgate.io/bench-node=true`
   that are Ready and not cordoned.
2. It takes a `coordination.k8s.io/v1` Lease named `bench-node-<node>`.
   Creating it is atomic; taking over an expired one is a `replace` with
   `metadata.resourceVersion`, so it is compare-and-swap. A worker thread
   renews it every TTL/3.
3. It creates a `batch/v1` Job pinned to that node
   (`benchgate/executors.py:render_job_manifest`):
   * required node affinity on `kubernetes.io/hostname` and on the bench label
   * a toleration for the `benchgate.io/dedicated=bench:NoSchedule` taint,
     which keeps everything else off bench nodes
   * required pod anti-affinity against other `benchgate.io/role=bench`
     pods on the same hostname, so Kubernetes itself refuses to co-schedule
     two bench pods even if the leases were wrong
   * Guaranteed QoS with integer CPUs, so the static CPU manager gives
     exclusive cores
   * `backoffLimit: 0` (retries are the orchestrator's, so they can move
     nodes), `activeDeadlineSeconds`, `ttlSecondsAfterFinished`
   * paired mode: an init container copies the *base* commit's binary out of
     the base image into an `emptyDir`, so base and head run in one pod,
     interleaved
4. The pod prints `BENCHGATE_RESULT {...}`. The orchestrator reads it with
   `kubectl logs -l job-name=...`, deletes the Job, and releases the Lease.

The orchestrator talks to the API through `kubectl` (in-cluster
service-account config), so behaviour is identical on kind and GKE. RBAC
is a namespaced Role (jobs, pods/log, leases) plus a ClusterRole to read
nodes. Bench pods get no API token.

## kind (local)

```bash
./deploy/kind/e2e.sh
```

This creates a 4-node cluster (`deploy/kind/kind-config.yaml`): a control
plane, an infra worker, and two bench workers labelled and tainted like the
GKE pool. It builds and side-loads the image, deploys
`deploy/k8s/overlays/kind`, and runs two comparisons at once (4 jobs
competing for 2 bench nodes). It asserts that the injected 30% slowdown
is blocked, the clean A/A is not, the Lease objects exist, and no two
bench pods ever ran on one node.

**Where this was verified.** kind does not run inside the development
sandbox: the nested container runtime fails with `runc create failed: can't
get final child's PID from pipe`. The e2e therefore runs in GitHub Actions
(`.github/workflows/ci.yml`, job `kind-e2e`). In the sandbox, the Kubernetes
code paths are covered by:

* `kubeconform -strict` against all kustomize output and against a bench Job
  rendered by the executor
* `tests/test_k8s_executor.py` and `tests/test_lease.py`, which run against a
  fake API server with real AlreadyExists and Conflict semantics (16
  concurrent lease takers → exactly one winner)
* the container image built with Docker, with the runner (paired mode and
  trace fallback) executed inside it

## GKE

```bash
PROJECT_ID=my-proj REGION=us-central1 BENCH_NODES=8 ./deploy/gke/create-cluster.sh
docker build -t us-docker.pkg.dev/$PROJECT_ID/benchgate/benchgate:latest . && docker push ...
sed -i "s/PROJECT_ID/$PROJECT_ID/g" deploy/k8s/overlays/gke/kustomization.yaml
kubectl apply -k deploy/k8s/overlays/gke
```

The GKE overlay adds:

* images from Artifact Registry
* results in BigQuery (`bigquery://PROJECT_ID/benchgate`) through Workload
  Identity
* bench pods pinned to CPUs `2,3` (the static CPU manager's exclusive cores)
* an internal LoadBalancer for the API

The bench node pool is created with `--threads-per-core=1`,
`--node-taints`, `--node-labels`, a static CPU manager system config, and
autoupgrade/autorepair turned off. A silent node-image upgrade is a step
change in every benchmark.

With the BigQuery store the orchestrator can run more than one replica:
leases are the coordination point. With DuckDB, keep one replica (a
single-writer file on a ReadWriteOnce PVC).
