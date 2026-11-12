"""In-memory stand-ins for kubectl, with real API-server semantics where
they matter: create is atomic (AlreadyExists), replace is compare-and-swap
on metadata.resourceVersion (Conflict)."""

from __future__ import annotations

import copy
import json
import threading

from benchgate.kube import KubeError


class FakeKube:
    def __init__(self, namespace="benchgate"):
        self.namespace = namespace
        self.objects: dict[tuple[str, str], dict] = {}
        self.nodes: list[dict] = []
        self.rv = 0
        self.lock = threading.Lock()
        self.on_create = None   # hook(obj) for Job behaviour
        self.job_status: dict[str, dict] = {}
        self.job_logs: dict[str, str] = {}
        self.created: list[dict] = []
        self.deleted: list[tuple[str, str]] = []

    def _key(self, kind, name):
        return (kind.lower(), name)

    def get(self, kind, name=None, selector=None):
        with self.lock:
            if kind == "nodes":
                return {"items": copy.deepcopy(self.nodes)}
            if kind == "job":
                if (("job", name)) not in self.objects:
                    raise KubeError("nf", 'Error from server (NotFound): jobs "x" not found')
                o = copy.deepcopy(self.objects[("job", name)])
                o["status"] = self.job_status.get(name, {})
                return o
            o = self.objects.get(self._key(kind, name))
            if o is None:
                raise KubeError("nf", f'Error from server (NotFound): {kind} "{name}" not found')
            return copy.deepcopy(o)

    def create(self, obj):
        with self.lock:
            kind = obj["kind"].lower()
            name = obj["metadata"]["name"]
            if (kind, name) in self.objects:
                raise KubeError("exists", f'Error from server (AlreadyExists): {kind} "{name}" already exists')
            self.rv += 1
            o = copy.deepcopy(obj)
            o["metadata"]["resourceVersion"] = str(self.rv)
            self.objects[(kind, name)] = o
            self.created.append(copy.deepcopy(o))
        if self.on_create:
            self.on_create(o)
        return copy.deepcopy(o)

    def replace(self, obj):
        with self.lock:
            kind = obj["kind"].lower()
            name = obj["metadata"]["name"]
            cur = self.objects.get((kind, name))
            if cur is None:
                raise KubeError("nf", "Error from server (NotFound)")
            want = obj["metadata"].get("resourceVersion")
            if want is not None and want != cur["metadata"]["resourceVersion"]:
                raise KubeError("conflict", "Operation cannot be fulfilled: the object has been modified (Conflict)")
            self.rv += 1
            o = copy.deepcopy(obj)
            o["metadata"]["resourceVersion"] = str(self.rv)
            self.objects[(kind, name)] = o
            return copy.deepcopy(o)

    def delete(self, kind, name):
        with self.lock:
            self.objects.pop(self._key(kind, name), None)
            self.deleted.append((kind, name))

    def logs(self, selector, container=None):
        name = selector.split("=", 1)[1]
        if name not in self.job_logs:
            raise KubeError("nf", "NotFound")
        return self.job_logs[name]


def ready_node(name, bench=True, unschedulable=False):
    return {"metadata": {"name": name, "labels": {"benchgate.io/bench-node": "true"} if bench else {}},
            "spec": {"unschedulable": unschedulable},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]}}


def result_log(runs):
    return "noise\nBENCHGATE_RESULT " + json.dumps({"ok": True, "runs": runs}) + "\n"
