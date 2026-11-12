#!/usr/bin/env bash
# Create a GKE cluster with a dedicated, tainted bench node pool.
#
#   PROJECT_ID=my-proj REGION=us-central1 ./deploy/gke/create-cluster.sh
#
# What GKE gives us vs. self-managed nodes (see docs/design.md, "Node tuning"):
#   + SMT off            --threads-per-core=1
#   + exclusive cores    static CPU manager (bench-node-system-config.yaml)
#   + no neighbours      optional sole-tenant node group (SOLE_TENANT_GROUP)
#   + fixed hardware     one machine family / zone for the whole pool
#   - no isolcpus/nohz_full/governor control on COS; use tuning/ for that.
set -euo pipefail
: "${PROJECT_ID:?}" "${REGION:=us-central1}" "${ZONE:=${REGION}-a}"
CLUSTER=${CLUSTER:-benchgate}
BENCH_MACHINE=${BENCH_MACHINE:-c3-standard-8}
BENCH_NODES=${BENCH_NODES:-8}   # see docs/capacity.md
HERE=$(cd "$(dirname "$0")" && pwd)

gcloud config set project "$PROJECT_ID"
gcloud container clusters create "$CLUSTER" \
  --zone "$ZONE" --num-nodes 1 --machine-type e2-standard-4 \
  --workload-pool "${PROJECT_ID}.svc.id.goog" --release-channel regular

extra=()
if [[ -n "${SOLE_TENANT_GROUP:-}" ]]; then
  extra+=(--node-group "$SOLE_TENANT_GROUP")
fi
gcloud container node-pools create bench \
  --cluster "$CLUSTER" --zone "$ZONE" \
  --machine-type "$BENCH_MACHINE" --num-nodes "$BENCH_NODES" \
  --threads-per-core 1 \
  --node-labels benchgate.io/bench-node=true \
  --node-taints benchgate.io/dedicated=bench:NoSchedule \
  --system-config-from-file "$HERE/bench-node-system-config.yaml" \
  --no-enable-autoupgrade --no-enable-autorepair \
  --image-type COS_CONTAINERD "${extra[@]}"
# Autoupgrade/autorepair are off: a silent kernel or node-image change is a
# step change in every benchmark. Upgrade deliberately, then re-baseline.

# Results in BigQuery via Workload Identity.
bq --location=US mk -d "${PROJECT_ID}:benchgate" || true
gcloud iam service-accounts create benchgate-orchestrator || true
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:benchgate-orchestrator@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role roles/bigquery.dataEditor
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:benchgate-orchestrator@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role roles/bigquery.jobUser
gcloud iam service-accounts add-iam-policy-binding \
  "benchgate-orchestrator@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role roles/iam.workloadIdentityUser \
  --member "serviceAccount:${PROJECT_ID}.svc.id.goog[benchgate/benchgate-orchestrator]"

gcloud container clusters get-credentials "$CLUSTER" --zone "$ZONE"
echo "Now: build+push the image, replace PROJECT_ID in deploy/k8s/overlays/gke, then"
echo "  kubectl apply -k deploy/k8s/overlays/gke"
