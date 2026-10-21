import json

import pytest

from benchgate.executors import AttemptFailed, AttemptTimeout, KubernetesExecutor, render_job_manifest
from benchgate.models import Job, JobSpec

from fakes import FakeKube, ready_node, result_log


def _run(job, node):
    return {"job_id": job.id, "commit": job.spec.commit, "workload": job.spec.workload,
            "repetition": 0, "node": node, "iteration_ns": [1, 2, 3], "p50_ns": 2,
            "p90_ns": 3, "p99_ns": 3, "mean_ns": 2, "checksum": 1.0}


def test_manifest_isolation_guarantees():
    job = Job(spec=JobSpec(commit="abc", workload="matrix", timeout_s=300,
                           baseline_commit="def", baseline_image="img:def"))
    m = render_job_manifest(job, "node-a", namespace="benchgate", image="img:abc", cpus="2,3")
    spec = m["spec"]
    pod = spec["template"]["spec"]
    assert spec["backoffLimit"] == 0 and spec["activeDeadlineSeconds"] == 300
    terms = pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    exprs = terms["nodeSelectorTerms"][0]["matchExpressions"]
    assert {"key": "kubernetes.io/hostname", "operator": "In", "values": ["node-a"]} in exprs
    anti = pod["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][0]
    assert anti["topologyKey"] == "kubernetes.io/hostname"
    assert anti["labelSelector"]["matchLabels"] == {"benchgate.io/role": "bench"}
    assert spec["template"]["metadata"]["labels"]["benchgate.io/role"] == "bench"
    c = pod["containers"][0]
    assert c["resources"]["requests"] == c["resources"]["limits"]  # Guaranteed QoS
    assert pod["initContainers"][0]["image"] == "img:def"
    env = {e["name"]: e.get("value") for e in c["env"]}
    assert json.loads(env["BENCHGATE_JOB_SPEC"])["baseline_commit"] == "def"
    assert env["BENCHGATE_CPUS"] == "2,3"
    assert len(m["metadata"]["name"]) <= 63


def test_nodes_filters_cordoned_and_notready():
    kube = FakeKube()
    nr = ready_node("c")
    nr["status"]["conditions"][0]["status"] = "False"
    kube.nodes = [ready_node("a"), ready_node("b", unschedulable=True), nr]
    assert KubernetesExecutor(kube, "img").nodes() == ["a"]


def _executor(kube, **kw):
    t = {"now": 0.0}

    def sleep(s):
        t["now"] += s
    return KubernetesExecutor(kube, "img", poll_s=1, sleep=sleep, clock=lambda: t["now"], **kw)


def test_success_parses_logs_and_cleans_up():
    kube = FakeKube()
    job = Job(spec=JobSpec(commit="abc", workload="matrix"))

    def on_create(obj):
        name = obj["metadata"]["name"]
        kube.job_status[name] = {"succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}]}
        kube.job_logs[name] = result_log([_run(job, "node-a")])
    kube.on_create = on_create
    runs = _executor(kube).execute(job, "node-a")
    assert runs[0].node == "node-a"
    assert kube.deleted and kube.deleted[0][0] == "job"


def test_deadline_exceeded_is_timeout():
    kube = FakeKube()
    kube.on_create = lambda o: kube.job_status.__setitem__(
        o["metadata"]["name"], {"conditions": [{"type": "Failed", "status": "True",
                                                "reason": "DeadlineExceeded"}]})
    with pytest.raises(AttemptTimeout):
        _executor(kube).execute(Job(spec=JobSpec(commit="a", workload="matrix")), "n")


def test_never_scheduled_hits_orchestrator_deadline():
    kube = FakeKube()  # job never gets a status
    with pytest.raises(AttemptTimeout):
        _executor(kube, schedule_timeout_s=10).execute(
            Job(spec=JobSpec(commit="a", workload="matrix", timeout_s=5)), "n")
    assert kube.deleted  # cleaned up anyway


def test_runner_error_is_failure():
    kube = FakeKube()

    def on_create(obj):
        name = obj["metadata"]["name"]
        kube.job_status[name] = {"conditions": [{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]}
        kube.job_logs[name] = 'BENCHGATE_RESULT {"ok": false, "error": "checksum mismatch"}'
    kube.on_create = on_create
    with pytest.raises(AttemptFailed, match="checksum mismatch"):
        _executor(kube).execute(Job(spec=JobSpec(commit="a", workload="matrix")), "n")
