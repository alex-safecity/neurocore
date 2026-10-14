"""In-memory RunStore for tests and ephemeral runs.

The reference implementation of the store semantics (resume protocol, row lease,
envelope log, unit of work); :class:`SQLiteRunStore` mirrors it with real conditional
updates. Not thread-safe beyond a single lock around each operation.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from neurocore.persistence.base import (
    EnvelopeRecord,
    ResumeOutcome,
    RunRecord,
    RunStatus,
    RunStore,
    StepRecord,
    UnitOfWork,
    utcnow_iso,
)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def lease_deadline(ttl_seconds: float, now: datetime | None = None) -> str:
    return ((now or datetime.now(UTC)) + timedelta(seconds=ttl_seconds)).isoformat()


class MemoryUnitOfWork(UnitOfWork):
    """Snapshot-and-restore transaction over the dict state."""

    def __init__(self, store: InMemoryRunStore) -> None:
        self._store = store
        self._snapshot = store._snapshot()
        self._active = True

    @property
    def active(self) -> bool:
        return self._active

    def commit(self) -> None:
        self._end()

    def rollback(self) -> None:
        if self._active:
            self._store._restore(self._snapshot)
        self._end()

    def _end(self) -> None:
        self._active = False
        if self._store._uow is self:
            self._store._uow = None


class InMemoryRunStore(RunStore):
    """Non-persistent RunStore backed by dicts. Useful for tests."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runs: dict[str, RunRecord] = {}
        self._steps: dict[str, dict[int, StepRecord]] = {}
        self._envelopes: dict[str, dict[int, EnvelopeRecord]] = {}
        self._uow: MemoryUnitOfWork | None = None
        # Lazily populated by checkpoint_store_for() so flowengine checkpoints
        # survive across a suspend/resume cycle. Typed loosely to avoid a hard
        # import of flowengine's CheckpointStore here.
        self._checkpoint_store: object | None = None

    # -- runs and steps ---------------------------------------------------------
    def save_run(self, run: RunRecord) -> str:
        with self._lock:
            self._runs[run.run_id] = run.model_copy(deep=True)
            self._steps.setdefault(run.run_id, {})
        return run.run_id

    def save_step(self, step: StepRecord) -> None:
        with self._lock:
            self._steps.setdefault(step.run_id, {})[step.step_index] = step.model_copy(deep=True)

    def load_run(self, run_id: str) -> RunRecord | None:
        with self._lock:
            run = self._runs.get(run_id)
            return run.model_copy(deep=True) if run else None

    def load_steps(self, run_id: str) -> list[StepRecord]:
        with self._lock:
            steps = self._steps.get(run_id, {})
            return [steps[i].model_copy(deep=True) for i in sorted(steps)]

    def list_runs(
        self,
        *,
        status: RunStatus | None = None,
        blueprint: str | None = None,
        limit: int = 50,
    ) -> list[RunRecord]:
        with self._lock:
            runs = list(self._runs.values())
            if status is not None:
                runs = [r for r in runs if r.status == status]
            if blueprint is not None:
                runs = [r for r in runs if r.blueprint_name == blueprint]
            runs.sort(key=lambda r: r.created_at, reverse=True)
            return [r.model_copy(deep=True) for r in runs[:limit]]

    def delete_run(self, run_id: str) -> None:
        with self._lock:
            self._runs.pop(run_id, None)
            self._steps.pop(run_id, None)
            self._envelopes.pop(run_id, None)

    # -- resume protocol --------------------------------------------------------
    def request_resume(
        self,
        run_id: str,
        *,
        action_id: str,
        payload: dict[str, Any] | None = None,
        expected_step_id: str | None = None,
    ) -> ResumeOutcome:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return ResumeOutcome(accepted=False, reason="not_found")
            if run.resume_action_id == action_id and run.status in (
                RunStatus.QUEUED,
                RunStatus.RUNNING,
                RunStatus.COMPLETED,
                RunStatus.SUSPENDED,
            ):
                # the same action again: the same answer
                return ResumeOutcome(
                    accepted=True, replay=True, resume_seq=run.resume_seq, status=run.status
                )
            if run.status != RunStatus.SUSPENDED:
                return ResumeOutcome(accepted=False, status=run.status, reason="conflict")
            if expected_step_id is not None and run.wait_step_id != expected_step_id:
                return ResumeOutcome(accepted=False, status=run.status, reason="wrong_step")
            run.status = RunStatus.QUEUED
            run.resume_seq += 1
            run.resume_action_id = action_id
            run.resume_payload = dict(payload) if payload is not None else None
            run.updated_at = utcnow_iso()
            return ResumeOutcome(accepted=True, resume_seq=run.resume_seq, status=run.status)

    # -- row lease --------------------------------------------------------------
    def acquire_lease(
        self,
        run_id: str,
        owner: str,
        ttl_seconds: float,
        *,
        expected_resume_seq: int | None = None,
    ) -> bool:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.status not in (RunStatus.QUEUED, RunStatus.RUNNING):
                return False
            if expected_resume_seq is not None and run.resume_seq != expected_resume_seq:
                return False
            now = datetime.now(UTC)
            held = (
                run.lease_owner is not None
                and run.lease_owner != owner
                and run.lease_until is not None
                and _parse(run.lease_until) > now
            )
            if held:
                return False
            run.lease_owner = owner
            run.lease_until = lease_deadline(ttl_seconds, now)
            run.status = RunStatus.RUNNING
            run.updated_at = utcnow_iso()
            return True

    def renew_lease(self, run_id: str, owner: str, ttl_seconds: float) -> bool:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.lease_owner != owner or run.lease_until is None:
                return False
            if _parse(run.lease_until) <= datetime.now(UTC):
                return False
            run.lease_until = lease_deadline(ttl_seconds)
            return True

    def release_lease(self, run_id: str, owner: str) -> None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is not None and run.lease_owner == owner:
                run.lease_owner = None
                run.lease_until = None

    def expired_leases(self, now_iso: str | None = None) -> list[RunRecord]:
        now = _parse(now_iso) if now_iso else datetime.now(UTC)
        with self._lock:
            return [
                r.model_copy(deep=True)
                for r in self._runs.values()
                if r.status == RunStatus.RUNNING
                and r.lease_until is not None
                and _parse(r.lease_until) <= now
            ]

    # -- envelopes --------------------------------------------------------------
    def save_envelope(self, envelope: EnvelopeRecord) -> None:
        with self._lock:
            by_seq = self._envelopes.setdefault(envelope.run_id, {})
            if envelope.seq in by_seq:
                raise ValueError(f"envelope ({envelope.run_id}, {envelope.seq}) already exists")
            by_seq[envelope.seq] = envelope.model_copy(deep=True)

    def latest_envelope(self, run_id: str) -> EnvelopeRecord | None:
        with self._lock:
            by_seq = self._envelopes.get(run_id)
            if not by_seq:
                return None
            return by_seq[max(by_seq)].model_copy(deep=True)

    def load_envelopes(self, run_id: str, *, since_seq: int = 0) -> list[EnvelopeRecord]:
        with self._lock:
            by_seq = self._envelopes.get(run_id, {})
            return [by_seq[s].model_copy(deep=True) for s in sorted(by_seq) if s > since_seq]

    # -- unit of work -----------------------------------------------------------
    def unit_of_work(self) -> UnitOfWork:
        with self._lock:
            if self._uow is not None and self._uow.active:
                raise RuntimeError("a unit of work is already open on this store")
            self._uow = MemoryUnitOfWork(self)
            return self._uow

    def _snapshot(self) -> dict[str, Any]:
        return {
            "runs": {k: v.model_copy(deep=True) for k, v in self._runs.items()},
            "steps": {k: dict(v) for k, v in self._steps.items()},
            "envelopes": {k: dict(v) for k, v in self._envelopes.items()},
        }

    def _restore(self, snap: dict[str, Any]) -> None:
        with self._lock:
            self._runs = snap["runs"]
            self._steps = snap["steps"]
            self._envelopes = snap["envelopes"]
