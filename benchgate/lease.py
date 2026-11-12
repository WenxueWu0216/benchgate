"""Node leasing: a node runs at most one benchmark job at a time.

Two jobs on one node would measure each other. Leases are the source of
truth for "who owns this node"; scheduling constraints in Kubernetes
(taints + pod anti-affinity, see deploy/k8s) are a second, independent guard.

Leases carry a TTL and are renewed by a heartbeat while the job runs, so a
crashed orchestrator cannot strand a node forever: an expired lease can be
taken over. Takeover and renewal are compare-and-swap operations, so two
orchestrator replicas racing for the same node cannot both win.
"""

from __future__ import annotations

import abc
import datetime as dt
import sqlite3
import threading
import time

from benchgate.kube import Kube, KubeError


class LeaseStore(abc.ABC):
    @abc.abstractmethod
    def acquire(self, node: str, holder: str, ttl_s: float) -> bool: ...

    @abc.abstractmethod
    def renew(self, node: str, holder: str, ttl_s: float) -> bool: ...

    @abc.abstractmethod
    def release(self, node: str, holder: str) -> None: ...

    @abc.abstractmethod
    def holder(self, node: str) -> str | None: ...


class SqliteLeaseStore(LeaseStore):
    """Local/dev backend. sqlite's BEGIN IMMEDIATE takes the write lock up
    front, so check-then-set is atomic even across processes sharing the file."""

    def __init__(self, path: str = ":memory:", clock=time.time):
        self.clock = clock
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("CREATE TABLE IF NOT EXISTS leases (node TEXT PRIMARY KEY, "
                        "holder TEXT, expires_at REAL)")

    def _txn(self, fn):
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                out = fn()
                self.db.execute("COMMIT")
                return out
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def acquire(self, node, holder, ttl_s):
        def f():
            row = self.db.execute("SELECT holder, expires_at FROM leases WHERE node = ?",
                                  [node]).fetchone()
            now = self.clock()
            if row and row[0] and row[0] != holder and row[1] > now:
                return False
            self.db.execute("INSERT OR REPLACE INTO leases VALUES (?, ?, ?)",
                            [node, holder, now + ttl_s])
            return True
        return self._txn(f)

    def renew(self, node, holder, ttl_s):
        def f():
            cur = self.db.execute("UPDATE leases SET expires_at = ? WHERE node = ? AND holder = ?",
                                  [self.clock() + ttl_s, node, holder])
            return cur.rowcount == 1
        return self._txn(f)

    def release(self, node, holder):
        self._txn(lambda: self.db.execute(
            "UPDATE leases SET holder = NULL WHERE node = ? AND holder = ?", [node, holder]))

    def holder(self, node):
        row = self.db.execute("SELECT holder, expires_at FROM leases WHERE node = ?",
                              [node]).fetchone()
        if not row or not row[0] or row[1] <= self.clock():
            return None
        return row[0]


def _fmt(t: float) -> str:
    return dt.datetime.fromtimestamp(t, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(s: str | None) -> float:
    if not s:
        return 0.0
    s = s.rstrip("Z")
    fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in s else "%Y-%m-%dT%H:%M:%S"
    return dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc).timestamp()


class KubernetesLeaseStore(LeaseStore):
    """coordination.k8s.io/v1 Lease per bench node, named bench-node-<node>.

    create() is atomic (AlreadyExists if someone else got there first) and
    replace() with metadata.resourceVersion is a compare-and-swap (Conflict
    if the object changed since we read it). Release clears holderIdentity
    rather than deleting, so it is CAS-protected too.
    """

    def __init__(self, kube: Kube, clock=time.time):
        self.kube = kube
        self.clock = clock

    @staticmethod
    def name(node: str) -> str:
        return "bench-node-" + node.lower().replace(".", "-").replace("_", "-")[:50]

    def _obj(self, node, holder, ttl_s, now, rv=None, transitions=0):
        md = {"name": self.name(node), "namespace": self.kube.namespace,
              "labels": {"app.kubernetes.io/part-of": "benchgate",
                         "benchgate.io/lease": "bench-node"},
              "annotations": {"benchgate.io/node": node}}
        if rv:
            md["resourceVersion"] = rv
        spec = {"holderIdentity": holder or "", "leaseDurationSeconds": int(max(1, round(ttl_s))),
                "renewTime": _fmt(now), "leaseTransitions": transitions}
        if holder:
            spec["acquireTime"] = _fmt(now)
        return {"apiVersion": "coordination.k8s.io/v1", "kind": "Lease", "metadata": md,
                "spec": spec}

    def _get(self, node):
        try:
            return self.kube.get("lease", self.name(node))
        except KubeError as e:
            if e.not_found:
                return None
            raise

    @staticmethod
    def _expired(obj, now) -> bool:
        spec = obj.get("spec", {})
        if not spec.get("holderIdentity"):
            return True
        return _parse(spec.get("renewTime")) + spec.get("leaseDurationSeconds", 0) < now

    def acquire(self, node, holder, ttl_s):
        now = self.clock()
        cur = self._get(node)
        try:
            if cur is None:
                self.kube.create(self._obj(node, holder, ttl_s, now))
                return True
            if cur["spec"].get("holderIdentity") == holder or self._expired(cur, now):
                self.kube.replace(self._obj(
                    node, holder, ttl_s, now, cur["metadata"]["resourceVersion"],
                    cur["spec"].get("leaseTransitions", 0) + 1))
                return True
            return False
        except KubeError as e:
            if e.already_exists or e.conflict:
                return False  # lost the race
            raise

    def renew(self, node, holder, ttl_s):
        cur = self._get(node)
        if cur is None or cur["spec"].get("holderIdentity") != holder:
            return False
        obj = self._obj(node, holder, ttl_s, self.clock(), cur["metadata"]["resourceVersion"],
                        cur["spec"].get("leaseTransitions", 0))
        obj["spec"]["acquireTime"] = cur["spec"].get("acquireTime", obj["spec"]["acquireTime"])
        try:
            self.kube.replace(obj)
            return True
        except KubeError as e:
            if e.conflict:
                return False
            raise

    def release(self, node, holder):
        cur = self._get(node)
        if cur is None or cur["spec"].get("holderIdentity") != holder:
            return
        try:
            self.kube.replace(self._obj(node, None, 1, self.clock(),
                                        cur["metadata"]["resourceVersion"],
                                        cur["spec"].get("leaseTransitions", 0)))
        except KubeError as e:
            if not e.conflict:
                raise

    def holder(self, node):
        cur = self._get(node)
        if cur is None or self._expired(cur, self.clock()):
            return None
        return cur["spec"]["holderIdentity"]
