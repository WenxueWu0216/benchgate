import threading

from benchgate.lease import KubernetesLeaseStore, SqliteLeaseStore

from fakes import FakeKube


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _contract(store, clock):
    assert store.acquire("n1", "a", 30)
    assert not store.acquire("n1", "b", 30)          # held
    assert store.acquire("n1", "a", 30)              # re-entrant for holder
    assert store.holder("n1") == "a"
    assert not store.renew("n1", "b", 30)            # only holder renews
    assert store.renew("n1", "a", 30)
    clock.t += 31
    assert store.holder("n1") is None                # expired
    assert store.acquire("n1", "b", 30)              # takeover after expiry
    assert not store.renew("n1", "a", 30)            # old holder lost it
    store.release("n1", "a")                         # non-holder release is a no-op
    assert store.holder("n1") == "b"
    store.release("n1", "b")
    assert store.holder("n1") is None
    assert store.acquire("n1", "c", 30)


def test_sqlite_contract():
    c = Clock()
    _contract(SqliteLeaseStore(clock=c), c)


def test_k8s_contract():
    c = Clock()
    _contract(KubernetesLeaseStore(FakeKube(), clock=c), c)


def _race(store_factory, n_threads=16):
    winners = []
    barrier = threading.Barrier(n_threads)

    def go(i):
        s = store_factory()
        barrier.wait()
        if s.acquire("hot", f"h{i}", 60):
            winners.append(i)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(n_threads)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return winners


def test_sqlite_race_single_winner(tmp_path):
    path = str(tmp_path / "l.sqlite")
    SqliteLeaseStore(path)  # create table
    assert len(_race(lambda: SqliteLeaseStore(path))) == 1


def test_k8s_race_single_winner():
    kube = FakeKube()
    assert len(_race(lambda: KubernetesLeaseStore(kube))) == 1


def test_k8s_lease_object_shape():
    kube = FakeKube()
    KubernetesLeaseStore(kube).acquire("gke-bench-pool-1.example", "orch/job-1", 60)
    (kind, name), obj = next(iter(kube.objects.items()))
    assert kind == "lease" and name == "bench-node-gke-bench-pool-1-example"
    assert obj["apiVersion"] == "coordination.k8s.io/v1"
    assert obj["spec"]["holderIdentity"] == "orch/job-1"
    assert obj["spec"]["leaseDurationSeconds"] == 60
