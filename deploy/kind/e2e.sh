#!/usr/bin/env bash
# End-to-end on kind: real cluster, real Jobs, real Leases.
#   1. create a 1+2 node cluster with two labelled bench nodes
#   2. build the image and side-load it
#   3. deploy the orchestrator (local DuckDB store, k8s executor)
#   4. ask for a verdict on an injected +30% regression and on a clean A/A
#   5. assert: regression blocked, leases exist, never >1 bench pod per node
set -euo pipefail
cd "$(dirname "$0")/../.."
CLUSTER=${CLUSTER:-benchgate}

if ! kind get clusters | grep -qx "$CLUSTER"; then
  kind create cluster --name "$CLUSTER" --config deploy/kind/kind-config.yaml --wait 180s
fi
docker build -t benchgate:dev .
kind load docker-image benchgate:dev --name "$CLUSTER"
kubectl apply -k deploy/k8s/overlays/kind
kubectl -n benchgate rollout status deploy/benchgate-orchestrator --timeout=180s

kubectl -n benchgate port-forward svc/benchgate 18080:80 >/tmp/pf.log 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null || true' EXIT
for _ in $(seq 30); do curl -sf localhost:18080/healthz && break; sleep 1; done
echo

# Watch for co-scheduled bench pods for the whole run.
( while true; do
    kubectl -n benchgate get pods -l benchgate.io/role=bench \
      --field-selector=status.phase=Running -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' \
      | sort | uniq -d
    sleep 1
  done ) > /tmp/overlap.log 2>/dev/null &
WATCH=$!

compare() {  # base head slowdown_pct -> verdict JSON
  local id
  id=$(curl -sf -XPOST localhost:18080/compare -d "{\"base\":\"$1\",\"head\":\"$2\",\"workloads\":[\"matrix\",\"planner\"],\"repetitions\":8,\"iterations\":150,\"slowdown_mode\":\"uniform\",\"slowdown_pct\":$3}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
  for _ in $(seq 120); do
    out=$(curl -sf "localhost:18080/compare/$id")
    status=$(echo "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
    [[ "$status" == done || "$status" == error ]] && break
    sleep 5
  done
  echo "$out"
}

# Two comparisons in parallel -> 4 jobs competing for 2 bench nodes.
compare base head-slow 30 > /tmp/slow.json &
compare base head-same 0 > /tmp/same.json &
wait %1 %2 2>/dev/null || wait
kill $WATCH

python3 - <<'PY'
import json
slow = json.load(open("/tmp/slow.json")); same = json.load(open("/tmp/same.json"))
for name, c in (("slow", slow), ("same", same)):
    assert c["status"] == "done", (name, c.get("error"))
    print(name, c["result"]["verdict"], [(t["workload"], t["metric"], round(t["ci"]["estimate"]*100, 1)) for t in c["result"]["tests"]])
assert slow["result"]["verdict"] == "regression", slow["result"]["verdict"]
assert same["result"]["verdict"] != "regression"
PY
kubectl -n benchgate get leases -l benchgate.io/lease=bench-node
if [[ -s /tmp/overlap.log ]]; then
  echo "FAIL: two bench pods ran on one node:"; cat /tmp/overlap.log; exit 1
fi
echo "kind e2e OK"
