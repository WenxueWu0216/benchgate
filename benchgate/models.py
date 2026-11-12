"""Plain data types shared by the runner, orchestrator, storage and gate."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import enum
import time
import uuid


class JobState(str, enum.Enum):
    QUEUED = "queued"
    LEASED = "leased"        # a node is reserved, job not yet started
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"        # exhausted retries
    TIMED_OUT = "timed_out"  # exhausted retries, last attempt hit the deadline


@dataclass
class JobSpec:
    commit: str
    workload: str
    repetitions: int = 10
    iterations: int = 300
    warmup: int = 50
    seed: int = 42
    slowdown_mode: str = "none"
    slowdown_pct: float = 0.0
    timeout_s: float = 600.0
    max_retries: int = 2
    trace: bool = False
    image: str | None = None  # container image for the commit (k8s executor)
    # Paired mode: also run this baseline on the same node, alternating
    # base/head/head/base... so node-to-node variance and slow drift cancel.
    baseline_commit: str | None = None
    baseline_image: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "JobSpec":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class RunRecord:
    """One process invocation of a workload."""
    job_id: str
    commit: str
    workload: str
    repetition: int
    node: str
    iteration_ns: list[int]
    p50_ns: float
    p90_ns: float
    p99_ns: float
    mean_ns: float
    checksum: float
    slowdown_mode: str = "none"
    slowdown_pct: float = 0.0
    started_at: float = field(default_factory=time.time)
    duration_s: float = 0.0
    env: dict = field(default_factory=dict)
    trace: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RunRecord":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Job:
    spec: JobSpec
    id: str = field(default_factory=lambda: "job-" + uuid.uuid4().hex[:12])
    state: JobState = JobState.QUEUED
    attempts: int = 0
    node: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    runs: list[RunRecord] = field(default_factory=list)

    def to_dict(self, with_runs: bool = False) -> dict:
        d = {
            "id": self.id, "state": self.state.value, "attempts": self.attempts,
            "node": self.node, "error": self.error, "created_at": self.created_at,
            "finished_at": self.finished_at, "spec": self.spec.to_dict(),
            "num_runs": len(self.runs),
        }
        if with_runs:
            d["runs"] = [r.to_dict() for r in self.runs]
        return d
