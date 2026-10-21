"""Orchestrator: queue of benchmark jobs, a pool of bench nodes, leases,
retries with backoff, and deadlines.

Scheduling loop (one thread):
  for each runnable queued job: pick a free node (preferring nodes this job
  has not already failed on), acquire its lease, hand the job to a worker.
Worker (one thread per in-flight job):
  heartbeat the lease, run one attempt via the executor, release the lease,
  then record success, or requeue with exponential backoff, or give up.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable
import uuid

from benchgate.executors import AttemptFailed, AttemptTimeout, Executor
from benchgate.lease import LeaseStore
from benchgate.models import Job, JobSpec, JobState
from benchgate.storage.base import ResultStore

log = logging.getLogger("benchgate.orchestrator")


class Orchestrator:
    def __init__(self, executor: Executor, leases: LeaseStore, store: ResultStore | None = None,
                 *, lease_ttl_s: float = 60.0, backoff_base_s: float = 5.0,
                 backoff_max_s: float = 300.0, poll_s: float = 0.2,
                 identity: str | None = None, clock: Callable[[], float] = time.time):
        self.executor = executor
        self.leases = leases
        self.store = store
        self.lease_ttl_s = lease_ttl_s
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self.poll_s = poll_s
        self.identity = identity or "orch-" + uuid.uuid4().hex[:8]
        self.clock = clock
        self.jobs: dict[str, Job] = {}
        self._not_before: dict[str, float] = {}
        self._failed_nodes: dict[str, set[str]] = {}
        self._busy: set[str] = set()
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ public API
    def submit(self, spec: JobSpec) -> Job:
        job = Job(spec=spec)
        with self._cv:
            self.jobs[job.id] = job
            self._cv.notify_all()
        self._persist(job)
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def wait(self, job_ids: list[str], timeout: float | None = None) -> bool:
        end = None if timeout is None else time.time() + timeout
        with self._cv:
            while not all(self.jobs[j].state in _TERMINAL for j in job_ids):
                rem = None if end is None else end - time.time()
                if rem is not None and rem <= 0:
                    return False
                self._cv.wait(timeout=min(rem, 1.0) if rem else 1.0)
        return True

    def start(self) -> "Orchestrator":
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        if self._thread:
            self._thread.join(timeout=5)

    # -------------------------------------------------------------- internals
    def _persist(self, job: Job) -> None:
        if self.store is not None:
            try:
                self.store.write_job(job)
            except Exception:  # storage trouble must not wedge the scheduler
                log.exception("failed to persist job %s", job.id)

    def _runnable(self) -> list[Job]:
        now = self.clock()
        q = [j for j in self.jobs.values()
             if j.state == JobState.QUEUED and self._not_before.get(j.id, 0) <= now]
        return sorted(q, key=lambda j: j.created_at)  # FIFO

    def _pick_node(self, job: Job, nodes: list[str]) -> str | None:
        failed = self._failed_nodes.get(job.id, set())
        # A retry waits for a node it has not failed on yet; only once every
        # node in the pool has failed this job may it go back to one.
        eligible = [n for n in nodes if n not in failed] or list(nodes)
        free = [n for n in eligible if n not in self._busy]
        for n in free:
            if self.leases.acquire(n, f"{self.identity}/{job.id}", self.lease_ttl_s):
                return n
        return None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                nodes = self.executor.nodes()
            except Exception:
                log.exception("listing nodes failed")
                nodes = []
            with self._cv:
                for job in self._runnable():
                    if len(self._busy) >= len(nodes):
                        break  # fleet full; keep FIFO order
                    node = self._pick_node(job, nodes)
                    if node is None:
                        continue  # e.g. a retry waiting for a different node
                    job.state, job.node = JobState.LEASED, node
                    self._busy.add(node)
                    threading.Thread(target=self._work, args=(job, node),
                                     name=f"worker-{job.id}", daemon=True).start()
                self._cv.wait(timeout=self.poll_s)

    def _heartbeat(self, node: str, holder: str, done: threading.Event, lost: threading.Event):
        while not done.wait(self.lease_ttl_s / 3):
            if not self.leases.renew(node, holder, self.lease_ttl_s):
                log.error("lost lease on %s for %s", node, holder)
                lost.set()
                return

    def _work(self, job: Job, node: str) -> None:
        holder = f"{self.identity}/{job.id}"
        done, lost = threading.Event(), threading.Event()
        hb = threading.Thread(target=self._heartbeat, args=(node, holder, done, lost), daemon=True)
        hb.start()
        job.attempts += 1
        job.state = JobState.RUNNING
        self._persist(job)
        err: Exception | None = None
        runs = []
        try:
            runs = self.executor.execute(job, node)
            if lost.is_set():
                # Someone else may have run on this node concurrently; the
                # timings are suspect, so the attempt does not count.
                raise AttemptFailed(f"lease on {node} lost during attempt")
        except Exception as e:  # noqa: BLE001 - every failure is retryable here
            err = e
        finally:
            done.set()
            hb.join(timeout=1)
            self.leases.release(node, holder)

        if err is None and self.store is not None:
            try:
                self.store.write_runs(runs)
            except Exception as e:  # noqa: BLE001
                err = AttemptFailed(f"storing results failed: {e}")

        with self._cv:
            self._busy.discard(node)
            if err is None:
                job.runs = runs
                job.state, job.error, job.finished_at = JobState.SUCCEEDED, None, self.clock()
                log.info("job %s succeeded on %s (%d runs)", job.id, node, len(runs))
            else:
                job.error = f"attempt {job.attempts} on {node}: {err}"
                self._failed_nodes.setdefault(job.id, set()).add(node)
                if job.attempts <= job.spec.max_retries:
                    delay = min(self.backoff_max_s, self.backoff_base_s * 2 ** (job.attempts - 1))
                    self._not_before[job.id] = self.clock() + delay
                    job.state, job.node = JobState.QUEUED, None
                    log.warning("job %s attempt %d failed (%s); retry in %.0fs",
                                job.id, job.attempts, err, delay)
                else:
                    job.state = (JobState.TIMED_OUT if isinstance(err, AttemptTimeout)
                                 else JobState.FAILED)
                    job.finished_at = self.clock()
                    log.error("job %s gave up after %d attempts: %s", job.id, job.attempts, err)
            self._cv.notify_all()
        self._persist(job)


_TERMINAL = {JobState.SUCCEEDED, JobState.FAILED, JobState.TIMED_OUT}
