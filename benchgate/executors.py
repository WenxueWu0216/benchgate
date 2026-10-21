"""Executors run one attempt of a job on one leased node.

LocalExecutor:  a "node" is a CPU set on this machine; the runner is a child
                process pinned to it. Used for development and CI.
KubernetesExecutor: a node is a Kubernetes node in the bench pool; the job
                is a batch/v1 Job pinned to that node. Works on kind and GKE.

Both enforce the attempt deadline themselves and raise AttemptTimeout /
AttemptFailed; the orchestrator decides whether to retry.
"""

from __future__ import annotations

import abc
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from benchgate.kube import Kube, KubeError
from benchgate.models import Job, RunRecord
from benchgate.runner import DEFAULT_BINARY, RunError, parse_result_line


class AttemptFailed(RuntimeError):
    pass


class AttemptTimeout(AttemptFailed):
    pass


class Executor(abc.ABC):
    @abc.abstractmethod
    def nodes(self) -> list[str]:
        """Nodes currently eligible for bench jobs."""

    @abc.abstractmethod
    def execute(self, job: Job, node: str) -> list[RunRecord]: ...


def _binary_for(commit: str, binary_dir: str | None, default: str) -> str:
    """$BENCHGATE_BINARY_DIR/<commit>/benchgate_workloads if it exists."""
    if binary_dir:
        p = Path(binary_dir) / commit / "benchgate_workloads"
        if p.exists():
            return str(p)
    return default


class LocalExecutor(Executor):
    def __init__(self, node_cpus: dict[str, list[int]] | None = None,
                 binary: str = DEFAULT_BINARY, binary_dir: str | None = None):
        if node_cpus is None:
            # One node per physical core pair by default; never more nodes than
            # cores or the "nodes" would just be measuring each other.
            n = os.cpu_count() or 1
            node_cpus = {f"local-{i}": [i] for i in range(n)}
        self.node_cpus = node_cpus
        self.binary = binary
        self.binary_dir = binary_dir or os.environ.get("BENCHGATE_BINARY_DIR")

    def nodes(self) -> list[str]:
        return list(self.node_cpus)

    def execute(self, job: Job, node: str) -> list[RunRecord]:
        spec = job.spec
        cmd = [sys.executable, "-m", "benchgate.runner", "--job-id", job.id,
               "--spec", json.dumps(spec.to_dict()),
               "--binary", _binary_for(spec.commit, self.binary_dir, self.binary),
               "--cpus", ",".join(map(str, self.node_cpus[node]))]
        if spec.baseline_commit:
            cmd += ["--baseline-binary",
                    _binary_for(spec.baseline_commit, self.binary_dir, self.binary)]
        env = dict(os.environ, BENCHGATE_NODE_NAME=node)
        # New session so a timeout kills the runner *and* the workload it spawned.
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=env, start_new_session=True)
        try:
            out, err = proc.communicate(timeout=spec.timeout_s)
        except subprocess.TimeoutExpired as e:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise AttemptTimeout(f"attempt exceeded {spec.timeout_s}s on {node}") from e
        try:
            res = parse_result_line(out)
        except RunError as e:
            raise AttemptFailed(f"runner exited {proc.returncode}: {err.strip()[-300:]}") from e
        if not res.get("ok"):
            raise AttemptFailed(res.get("error", "unknown runner error"))
        return [RunRecord.from_dict(r) for r in res["runs"]]


BENCH_NODE_LABEL = "benchgate.io/bench-node"


def render_job_manifest(job: Job, node: str, *, namespace: str, image: str,
                        cpus: str | None = None, attempt: int = 0) -> dict:
    """batch/v1 Job pinned to one bench node.

    - nodeName-equivalent pinning via required node affinity on hostname
      (keeps the scheduler in the loop, unlike spec.nodeName).
    - Tolerates the bench-pool taint; nothing else does, so no stray pods.
    - Required pod anti-affinity against any other benchgate bench pod on
      the same hostname: Kubernetes itself refuses to co-schedule two bench
      jobs, independent of our leases.
    - Guaranteed QoS (requests == limits, integer CPUs) so the kubelet's
      static CPU manager gives the pod exclusive cores.
    - backoffLimit 0: retries are the orchestrator's job, so a retry can move
      to a different node.
    """
    spec = job.spec
    name = f"bench-{job.id[4:]}-a{attempt}"[:63]
    labels = {"app.kubernetes.io/name": "benchgate-bench", "app.kubernetes.io/part-of": "benchgate",
              "benchgate.io/job-id": job.id, "benchgate.io/role": "bench"}
    env = [
        {"name": "BENCHGATE_JOB_ID", "value": job.id},
        {"name": "BENCHGATE_JOB_SPEC", "value": json.dumps(spec.to_dict())},
        {"name": "BENCHGATE_NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}},
    ]
    if cpus:
        env.append({"name": "BENCHGATE_CPUS", "value": cpus})
    volumes, mounts, init = [], [], []
    if spec.baseline_commit:
        # Copy the baseline commit's binary out of its own image so both
        # binaries run in one pod, on one node, interleaved.
        volumes.append({"name": "baseline", "emptyDir": {}})
        mounts.append({"name": "baseline", "mountPath": "/baseline"})
        init.append({"name": "baseline-binary", "image": spec.baseline_image or image,
                     "command": ["cp", "/opt/benchgate/bin/benchgate_workloads", "/baseline/"],
                     "volumeMounts": [{"name": "baseline", "mountPath": "/baseline"}],
                     "resources": {"requests": {"cpu": "100m", "memory": "64Mi"},
                                   "limits": {"cpu": "100m", "memory": "64Mi"}}})
        env.append({"name": "BENCHGATE_BASELINE_BINARY",
                    "value": "/baseline/benchgate_workloads"})
    pod_spec = {
        "restartPolicy": "Never",
        "affinity": {
            "nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [{"matchExpressions": [
                    {"key": "kubernetes.io/hostname", "operator": "In", "values": [node]},
                    {"key": BENCH_NODE_LABEL, "operator": "In", "values": ["true"]}]}]}},
            "podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{
                "labelSelector": {"matchLabels": {"benchgate.io/role": "bench"}},
                "topologyKey": "kubernetes.io/hostname"}]},
        },
        "tolerations": [{"key": "benchgate.io/dedicated", "operator": "Equal",
                         "value": "bench", "effect": "NoSchedule"}],
        "serviceAccountName": "benchgate-bench",
        "automountServiceAccountToken": False,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001},
        "containers": [{
            "name": "runner", "image": spec.image or image, "imagePullPolicy": "IfNotPresent",
            "command": ["python", "-m", "benchgate.runner"],
            "env": env, "volumeMounts": mounts,
            "resources": {"requests": {"cpu": "2", "memory": "1Gi"},
                          "limits": {"cpu": "2", "memory": "1Gi"}},
        }],
        "volumes": volumes,
    }
    if init:
        pod_spec["initContainers"] = init
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": int(spec.timeout_s),
            "ttlSecondsAfterFinished": 600,
            "template": {"metadata": {"labels": labels}, "spec": pod_spec},
        },
    }


class KubernetesExecutor(Executor):
    def __init__(self, kube: Kube, image: str, poll_s: float = 2.0, cpus: str | None = None,
                 schedule_timeout_s: float = 300.0, sleep=time.sleep, clock=time.time):
        self.kube = kube
        self.image = image
        self.poll_s = poll_s
        self.cpus = cpus
        self.schedule_timeout_s = schedule_timeout_s
        self.sleep = sleep
        self.clock = clock

    def nodes(self) -> list[str]:
        data = self.kube.get("nodes", selector=f"{BENCH_NODE_LABEL}=true")
        out = []
        for n in data.get("items", []):
            if n.get("spec", {}).get("unschedulable"):
                continue  # cordoned for maintenance
            ready = any(c["type"] == "Ready" and c["status"] == "True"
                        for c in n.get("status", {}).get("conditions", []))
            if ready:
                out.append(n["metadata"]["name"])
        return sorted(out)

    def execute(self, job: Job, node: str) -> list[RunRecord]:
        manifest = render_job_manifest(job, node, namespace=self.kube.namespace,
                                       image=self.image, cpus=self.cpus, attempt=job.attempts)
        name = manifest["metadata"]["name"]
        selector = f"job-name={name}"
        try:
            self.kube.create(manifest)
        except KubeError as e:
            raise AttemptFailed(f"could not create Job: {e}") from e
        # Our own deadline covers time-to-schedule (image pull, a node that
        # never becomes schedulable) on top of the Job's activeDeadlineSeconds.
        deadline = self.clock() + job.spec.timeout_s + self.schedule_timeout_s
        try:
            while True:
                st = self.kube.get("job", name).get("status", {})
                conds = {c["type"]: c for c in st.get("conditions", []) if c.get("status") == "True"}
                if "Complete" in conds or st.get("succeeded"):
                    break
                if "Failed" in conds:
                    reason = conds["Failed"].get("reason", "")
                    log = self._safe_logs(selector)
                    if reason == "DeadlineExceeded":
                        raise AttemptTimeout(f"Job {name} exceeded activeDeadlineSeconds")
                    raise AttemptFailed(f"Job {name} failed ({reason}): {log[-300:]}")
                if self.clock() > deadline:
                    raise AttemptTimeout(f"Job {name} not finished by orchestrator deadline")
                self.sleep(self.poll_s)
            try:
                res = parse_result_line(self.kube.logs(selector, container="runner"))
            except (RunError, KubeError) as e:
                raise AttemptFailed(f"no result from {name}: {e}") from e
            if not res.get("ok"):
                raise AttemptFailed(res.get("error", "runner error"))
            return [RunRecord.from_dict(r) for r in res["runs"]]
        finally:
            self.kube.delete("job", name)

    def _safe_logs(self, selector: str) -> str:
        try:
            return self.kube.logs(selector, container="runner")
        except KubeError:
            return ""
