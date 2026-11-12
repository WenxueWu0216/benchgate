"""Thin kubectl wrapper.

Using kubectl (instead of the Python client) keeps the orchestrator's
dependencies small, behaves identically on kind and GKE (auth plugins,
in-cluster service-account config), and is trivial to fake in tests.
"""

from __future__ import annotations

import json
import subprocess


class KubeError(RuntimeError):
    def __init__(self, msg: str, stderr: str = ""):
        super().__init__(msg)
        self.stderr = stderr

    @property
    def not_found(self) -> bool:
        return "NotFound" in self.stderr or "not found" in self.stderr

    @property
    def already_exists(self) -> bool:
        return "AlreadyExists" in self.stderr

    @property
    def conflict(self) -> bool:
        return "Conflict" in self.stderr or "the object has been modified" in self.stderr


class Kube:
    def __init__(self, namespace: str = "benchgate", kubectl: str = "kubectl",
                 context: str | None = None):
        self.namespace = namespace
        self.kubectl = kubectl
        self.context = context

    def run(self, args: list[str], stdin: str | None = None, timeout: float = 60) -> str:
        cmd = [self.kubectl]
        if self.context:
            cmd += ["--context", self.context]
        cmd += ["-n", self.namespace, *args]
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            raise KubeError(f"kubectl {' '.join(args[:3])} failed: {p.stderr.strip()[:300]}",
                            p.stderr)
        return p.stdout

    def get(self, kind: str, name: str | None = None, selector: str | None = None) -> dict:
        args = ["get", kind] + ([name] if name else []) + ["-o", "json"]
        if selector:
            args += ["-l", selector]
        return json.loads(self.run(args))

    def create(self, obj: dict) -> dict:
        return json.loads(self.run(["create", "-o", "json", "-f", "-"], stdin=json.dumps(obj)))

    def replace(self, obj: dict) -> dict:
        # metadata.resourceVersion in obj makes this a compare-and-swap.
        return json.loads(self.run(["replace", "-o", "json", "-f", "-"], stdin=json.dumps(obj)))

    def delete(self, kind: str, name: str) -> None:
        try:
            self.run(["delete", kind, name, "--ignore-not-found", "--wait=false",
                      "--cascade=background"])
        except KubeError as e:
            if not e.not_found:
                raise

    def logs(self, selector: str, container: str | None = None) -> str:
        args = ["logs", "-l", selector, "--tail=-1"]
        if container:
            args += ["-c", container]
        return self.run(args, timeout=120)
