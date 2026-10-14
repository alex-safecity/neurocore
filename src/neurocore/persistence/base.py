"""Persistence models and the RunStore interface.

NeuroCore persists every blueprint execution as a durable *run* with an
ordered list of *step* records. This makes runs inspectable, resumable, and
replayable — the difference between a "nice framework" and a "production agent
runtime".

The ``RunStore`` interface is intentionally separate from flowengine's
``CheckpointStore``: a checkpoint is transient (deleted on resume), while a run
record is durable history. A ``RunStore`` can still *back* a ``CheckpointStore``
for flowengine's sync suspend/resume path — see
:mod:`neurocore.persistence.checkpoint_adapter`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


class RunStatus(StrEnum):
    """Lifecycle status of a run."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SUSPENDED = "suspended"  # awaiting human approval / external input
    QUEUED = "queued"  # a resume was accepted; waiting for a worker to claim the lease
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    """Outcome of a single step within a run."""

    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunRecord(BaseModel):
    """A durable record of one blueprint execution."""

    run_id: str
    blueprint_name: str
    blueprint_version: str = "1.0"
    blueprint_path: str | None = None
    # Full Blueprint.model_dump() so a run can be replayed/resumed even if the
    # source file has moved or changed.
    blueprint_snapshot: dict[str, Any] = Field(default_factory=dict)
    flow_type: str = "sequential"
    status: RunStatus = RunStatus.RUNNING
    initial_data: dict[str, Any] = Field(default_factory=dict)
    final_context: dict[str, Any] | None = None  # FlowContext.to_dict()
    error: str | None = None
    suspended_at_node: str | None = None
    suspension_reason: str | None = None
    checkpoint_id: str | None = None  # flowengine checkpoint id (sync path only)
    created_at: str = Field(default_factory=utcnow_iso)
    updated_at: str = Field(default_factory=utcnow_iso)
    duration_ms: float | None = None
    # Resume protocol (0.5.0): the node the run waits at, how many resumes were
    # accepted, the accepted action and its payload (already masked by the caller).
    wait_step_id: str | None = None
    resume_seq: int = 0
    resume_action_id: str | None = None
    resume_payload: dict[str, Any] | None = None
    # Row lease (0.5.0): one worker executes a run at a time.
    lease_owner: str | None = None
    lease_until: str | None = None  # ISO-8601 UTC


class ResumeOutcome(BaseModel):
    """Result of :meth:`RunStore.request_resume`.

    ``accepted`` with ``replay=False``: the conditional update won and the run is queued
    with ``resume_seq``. ``accepted`` with ``replay=True``: the same ``action_id`` was
    already accepted — the caller returns the same answer it gave before. Not accepted:
    ``reason`` says why (``conflict`` — another action was accepted first or the run is
    not waiting; ``wrong_step`` — it waits at a different step; ``not_found``).
    """

    accepted: bool
    replay: bool = False
    resume_seq: int = 0
    status: RunStatus | None = None
    reason: str | None = None


class EnvelopeRecord(BaseModel):
    """What the platform last sent to the outside for a run: the thing a resume is
    validated against (0.5.0). ``seq`` orders envelopes per run."""

    run_id: str
    seq: int
    step_id: str
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None
    created_at: str = Field(default_factory=utcnow_iso)


class StepRecord(BaseModel):
    """A record of one step (component invocation) within a run."""

    run_id: str
    step_index: int
    component: str
    skill_type: str | None = None
    status: StepStatus = StepStatus.COMPLETED
    started_at: str = Field(default_factory=utcnow_iso)
    duration_ms: float | None = None
    error: str | None = None
    output_keys: list[str] = Field(default_factory=list)
    context_snapshot: dict[str, Any] | None = None  # gated by persist_step_snapshots


class UnitOfWork(ABC):
    """A store transaction a step's writers share (:mod:`neurocore.runtime.invocation`).

    Writes made through the store while the unit of work is open are part of it;
    ``commit`` makes them durable together, ``rollback`` discards them together.
    """

    @abstractmethod
    def commit(self) -> None: ...

    @abstractmethod
    def rollback(self) -> None: ...

    @property
    @abstractmethod
    def active(self) -> bool: ...


class RunStore(ABC):
    """Abstract durable store for run history.

    Implementations: :class:`~neurocore.persistence.sqlite_store.SQLiteRunStore`
    (default) and :class:`~neurocore.persistence.memory_store.InMemoryRunStore`
    (tests). Besides run and step history a store implements the **resume protocol**
    (``request_resume``), the **row lease** (``acquire_lease`` / ``renew_lease`` /
    ``release_lease`` / ``expired_leases``), the **envelope log** (``save_envelope`` /
    ``latest_envelope`` / ``load_envelopes``) and the **unit of work** a step's writers
    join (``unit_of_work``). :func:`run_store_conformance` in the tests exercises all of
    it against every implementation.
    """

    @abstractmethod
    def save_run(self, run: RunRecord) -> str:
        """Insert or update a run by ``run_id``. Returns the run_id."""
        ...

    @abstractmethod
    def save_step(self, step: StepRecord) -> None:
        """Insert or update a step by ``(run_id, step_index)``."""
        ...

    @abstractmethod
    def load_run(self, run_id: str) -> RunRecord | None:
        """Load a run by id, or None if not found."""
        ...

    @abstractmethod
    def load_steps(self, run_id: str) -> list[StepRecord]:
        """Load a run's steps, ordered by step_index."""
        ...

    @abstractmethod
    def list_runs(
        self,
        *,
        status: RunStatus | None = None,
        blueprint: str | None = None,
        limit: int = 50,
    ) -> list[RunRecord]:
        """List runs newest-first, optionally filtered by status/blueprint."""
        ...

    @abstractmethod
    def delete_run(self, run_id: str) -> None:
        """Delete a run and its steps."""
        ...

    # -- resume protocol ------------------------------------------------------
    @abstractmethod
    def request_resume(
        self,
        run_id: str,
        *,
        action_id: str,
        payload: dict[str, Any] | None = None,
        expected_step_id: str | None = None,
    ) -> ResumeOutcome:
        """Flip a suspended run to queued with a conditional update that succeeds once.

        The caller validates the action against the last envelope first (and consumes
        any single-use token), then calls this; a repeated ``action_id`` replays, a
        different one while the run is already queued is a conflict.
        """
        ...

    # -- row lease ------------------------------------------------------------
    @abstractmethod
    def acquire_lease(
        self,
        run_id: str,
        owner: str,
        ttl_seconds: float,
        *,
        expected_resume_seq: int | None = None,
    ) -> bool:
        """Claim a queued (or expired-lease) run for ``owner``; False if someone else
        holds a live lease, the run is not claimable, or ``expected_resume_seq`` is stale
        (the worker then drops its message)."""
        ...

    @abstractmethod
    def renew_lease(self, run_id: str, owner: str, ttl_seconds: float) -> bool:
        """Heartbeat: extend the lease the owner holds; False if it no longer does."""
        ...

    @abstractmethod
    def release_lease(self, run_id: str, owner: str) -> None:
        """Give the lease back (no-op if the owner does not hold it)."""
        ...

    @abstractmethod
    def expired_leases(self, now_iso: str | None = None) -> list[RunRecord]:
        """Running runs whose lease expired — what the sweeper re-queues."""
        ...

    # -- envelopes ------------------------------------------------------------
    @abstractmethod
    def save_envelope(self, envelope: EnvelopeRecord) -> None:
        """Append an envelope (``(run_id, seq)`` is unique)."""
        ...

    @abstractmethod
    def latest_envelope(self, run_id: str) -> EnvelopeRecord | None: ...

    @abstractmethod
    def load_envelopes(self, run_id: str, *, since_seq: int = 0) -> list[EnvelopeRecord]:
        """Envelopes with ``seq > since_seq``, in order (a reconnecting client replays)."""
        ...

    # -- unit of work ---------------------------------------------------------
    @abstractmethod
    def unit_of_work(self) -> UnitOfWork:
        """Open the transaction a step's writers share (one at a time per store)."""
        ...

    def close(self) -> None:  # noqa: B027 — optional override, default no-op
        """Release any underlying resources (no-op by default)."""
