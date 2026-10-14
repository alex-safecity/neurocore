"""SQLite-backed RunStore using the stdlib ``sqlite3`` module (no extra deps).

The database holds three tables: ``runs`` (one row per execution), ``steps``
(per-step records, cascade-deleted with the run), and ``checkpoints`` (used by
:mod:`neurocore.persistence.checkpoint_adapter` so flowengine's sync
suspend/resume shares the same file).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from neurocore.persistence.base import (
    EnvelopeRecord,
    ResumeOutcome,
    RunRecord,
    RunStatus,
    RunStore,
    StepRecord,
    StepStatus,
    UnitOfWork,
    utcnow_iso,
)

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,
    blueprint_name      TEXT NOT NULL,
    blueprint_version   TEXT NOT NULL,
    blueprint_path      TEXT,
    blueprint_snapshot  TEXT NOT NULL,
    flow_type           TEXT NOT NULL,
    status              TEXT NOT NULL,
    initial_data        TEXT NOT NULL,
    final_context       TEXT,
    error               TEXT,
    suspended_at_node   TEXT,
    suspension_reason   TEXT,
    checkpoint_id       TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    duration_ms         REAL
);
CREATE INDEX IF NOT EXISTS idx_runs_status  ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_bp      ON runs(blueprint_name);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);

CREATE TABLE IF NOT EXISTS steps (
    run_id            TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    step_index        INTEGER NOT NULL,
    component         TEXT NOT NULL,
    skill_type        TEXT,
    status            TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    duration_ms       REAL,
    error             TEXT,
    output_keys       TEXT NOT NULL,
    context_snapshot  TEXT,
    PRIMARY KEY (run_id, step_index)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id  TEXT PRIMARY KEY,
    flow_config    TEXT NOT NULL,
    context        TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS envelopes (
    run_id      TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    step_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    session_id  TEXT,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
"""

# Columns added in 0.5.0 (resume protocol and row lease); older files get them on open.
_RUN_COLUMNS_0_5 = {
    "wait_step_id": "TEXT",
    "resume_seq": "INTEGER NOT NULL DEFAULT 0",
    "resume_action_id": "TEXT",
    "resume_payload": "TEXT",
    "lease_owner": "TEXT",
    "lease_until": "TEXT",
}


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str)


class SQLiteUnitOfWork(UnitOfWork):
    """One SQLite transaction the store's writes join while it is open.

    The store commits nothing itself while a unit of work is active; ``commit`` makes
    every write since ``unit_of_work()`` durable together and ``rollback`` discards
    them together. One at a time per store (the dev store serves one worker process;
    the SQL Server store opens a connection per step).
    """

    def __init__(self, store: SQLiteRunStore) -> None:
        self._store = store
        self._active = True
        with store._lock:
            store._conn.execute("BEGIN")

    @property
    def active(self) -> bool:
        return self._active

    def commit(self) -> None:
        if not self._active:
            return
        with self._store._lock:
            self._store._conn.commit()
        self._end()

    def rollback(self) -> None:
        if not self._active:
            return
        with self._store._lock:
            self._store._conn.rollback()
        self._end()

    def _end(self) -> None:
        self._active = False
        if self._store._uow is self:
            self._store._uow = None


class SQLiteRunStore(RunStore):
    """RunStore persisting to a single SQLite file.

    Thread-safe: the DAG executor runs sync skills via ``run_in_executor`` so
    concurrent writes are possible. We use ``check_same_thread=False`` plus a
    lock around every write.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()  # re-entrant: a unit of work opens under it
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._uow: SQLiteUnitOfWork | None = None
        with self._lock:
            self._conn.executescript(_SCHEMA)
            have = {r["name"] for r in self._conn.execute("PRAGMA table_info(runs)")}
            for col, decl in _RUN_COLUMNS_0_5.items():
                if col not in have:
                    self._conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {decl}")
            self._conn.commit()

    def _commit(self) -> None:
        """Commit unless a unit of work is open — then its commit does it."""
        if self._uow is None or not self._uow.active:
            self._conn.commit()

    # -- runs --------------------------------------------------------------
    def save_run(self, run: RunRecord) -> str:
        row = (
            run.run_id,
            run.blueprint_name,
            run.blueprint_version,
            run.blueprint_path,
            _dumps(run.blueprint_snapshot),
            run.flow_type,
            str(run.status),
            _dumps(run.initial_data),
            _dumps(run.final_context) if run.final_context is not None else None,
            run.error,
            run.suspended_at_node,
            run.suspension_reason,
            run.checkpoint_id,
            run.created_at,
            run.updated_at,
            run.duration_ms,
            run.wait_step_id,
            run.resume_seq,
            run.resume_action_id,
            _dumps(run.resume_payload) if run.resume_payload is not None else None,
            run.lease_owner,
            run.lease_until,
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO runs (
                    run_id, blueprint_name, blueprint_version, blueprint_path,
                    blueprint_snapshot, flow_type, status, initial_data,
                    final_context, error, suspended_at_node, suspension_reason,
                    checkpoint_id, created_at, updated_at, duration_ms,
                    wait_step_id, resume_seq, resume_action_id, resume_payload,
                    lease_owner, lease_until
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status=excluded.status,
                    final_context=excluded.final_context,
                    error=excluded.error,
                    suspended_at_node=excluded.suspended_at_node,
                    suspension_reason=excluded.suspension_reason,
                    checkpoint_id=excluded.checkpoint_id,
                    updated_at=excluded.updated_at,
                    duration_ms=excluded.duration_ms,
                    wait_step_id=excluded.wait_step_id,
                    resume_seq=excluded.resume_seq,
                    resume_action_id=excluded.resume_action_id,
                    resume_payload=excluded.resume_payload,
                    lease_owner=excluded.lease_owner,
                    lease_until=excluded.lease_until
                """,
                row,
            )
            self._commit()
        return run.run_id

    def load_run(self, run_id: str) -> RunRecord | None:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,))
            row = cur.fetchone()
        return self._row_to_run(row) if row else None

    def list_runs(
        self,
        *,
        status: RunStatus | None = None,
        blueprint: str | None = None,
        limit: int = 50,
    ) -> list[RunRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status=?")
            params.append(str(status))
        if blueprint is not None:
            clauses.append("blueprint_name=?")
            params.append(blueprint)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._lock:
            cur = self._conn.execute(
                f"SELECT * FROM runs{where} ORDER BY created_at DESC LIMIT ?",
                params,
            )
            rows = cur.fetchall()
        return [self._row_to_run(r) for r in rows]

    def delete_run(self, run_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
            self._commit()

    # -- resume protocol ---------------------------------------------------
    def request_resume(
        self,
        run_id: str,
        *,
        action_id: str,
        payload: dict[str, Any] | None = None,
        expected_step_id: str | None = None,
    ) -> ResumeOutcome:
        now = utcnow_iso()
        with self._lock:
            # The conditional update: exactly one caller wins the flip to queued.
            cur = self._conn.execute(
                """
                UPDATE runs SET status=?, resume_seq=resume_seq+1, resume_action_id=?,
                                resume_payload=?, updated_at=?
                WHERE run_id=? AND status=?
                      AND (? IS NULL OR wait_step_id=?)
                """,
                (
                    str(RunStatus.QUEUED),
                    action_id,
                    _dumps(payload) if payload is not None else None,
                    now,
                    run_id,
                    str(RunStatus.SUSPENDED),
                    expected_step_id,
                    expected_step_id,
                ),
            )
            won = cur.rowcount == 1
            self._commit()
            row = self._conn.execute(
                "SELECT status, resume_seq, resume_action_id, wait_step_id FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            return ResumeOutcome(accepted=False, reason="not_found")
        status = RunStatus(row["status"])
        if won:
            return ResumeOutcome(accepted=True, resume_seq=row["resume_seq"], status=status)
        if row["resume_action_id"] == action_id:
            return ResumeOutcome(
                accepted=True, replay=True, resume_seq=row["resume_seq"], status=status
            )
        if (
            status == RunStatus.SUSPENDED
            and expected_step_id is not None
            and row["wait_step_id"] != expected_step_id
        ):
            return ResumeOutcome(accepted=False, status=status, reason="wrong_step")
        return ResumeOutcome(accepted=False, status=status, reason="conflict")

    # -- row lease ---------------------------------------------------------
    def acquire_lease(
        self,
        run_id: str,
        owner: str,
        ttl_seconds: float,
        *,
        expected_resume_seq: int | None = None,
    ) -> bool:
        now = datetime.now(UTC)
        until = (now + timedelta(seconds=ttl_seconds)).isoformat()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE runs SET lease_owner=?, lease_until=?, status=?, updated_at=?
                WHERE run_id=? AND status IN (?, ?)
                      AND (? IS NULL OR resume_seq=?)
                      AND (lease_owner IS NULL OR lease_owner=? OR lease_until IS NULL
                           OR lease_until <= ?)
                """,
                (
                    owner,
                    until,
                    str(RunStatus.RUNNING),
                    now.isoformat(),
                    run_id,
                    str(RunStatus.QUEUED),
                    str(RunStatus.RUNNING),
                    expected_resume_seq,
                    expected_resume_seq,
                    owner,
                    now.isoformat(),
                ),
            )
            won = cur.rowcount == 1
            self._commit()
        return won

    def renew_lease(self, run_id: str, owner: str, ttl_seconds: float) -> bool:
        now = datetime.now(UTC)
        until = (now + timedelta(seconds=ttl_seconds)).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE runs SET lease_until=? WHERE run_id=? AND lease_owner=? AND lease_until > ?",
                (until, run_id, owner, now.isoformat()),
            )
            won = cur.rowcount == 1
            self._commit()
        return won

    def release_lease(self, run_id: str, owner: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET lease_owner=NULL, lease_until=NULL WHERE run_id=? AND lease_owner=?",
                (run_id, owner),
            )
            self._commit()

    def expired_leases(self, now_iso: str | None = None) -> list[RunRecord]:
        now = now_iso or utcnow_iso()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE status=? AND lease_until IS NOT NULL AND lease_until <= ?",
                (str(RunStatus.RUNNING), now),
            ).fetchall()
        return [self._row_to_run(r) for r in rows]

    # -- envelopes ---------------------------------------------------------
    def save_envelope(self, envelope: EnvelopeRecord) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO envelopes (run_id, seq, step_id, kind, payload, session_id, created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (
                        envelope.run_id,
                        envelope.seq,
                        envelope.step_id,
                        envelope.kind,
                        _dumps(envelope.payload),
                        envelope.session_id,
                        envelope.created_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    f"envelope ({envelope.run_id}, {envelope.seq}) already exists"
                ) from exc
            self._commit()

    def latest_envelope(self, run_id: str) -> EnvelopeRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM envelopes WHERE run_id=? ORDER BY seq DESC LIMIT 1", (run_id,)
            ).fetchone()
        return self._row_to_envelope(row) if row else None

    def load_envelopes(self, run_id: str, *, since_seq: int = 0) -> list[EnvelopeRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM envelopes WHERE run_id=? AND seq>? ORDER BY seq ASC",
                (run_id, since_seq),
            ).fetchall()
        return [self._row_to_envelope(r) for r in rows]

    # -- unit of work ------------------------------------------------------
    def unit_of_work(self) -> UnitOfWork:
        with self._lock:
            if self._uow is not None and self._uow.active:
                raise RuntimeError("a unit of work is already open on this store")
            self._uow = SQLiteUnitOfWork(self)
            return self._uow

    # -- steps -------------------------------------------------------------
    def save_step(self, step: StepRecord) -> None:
        row = (
            step.run_id,
            step.step_index,
            step.component,
            step.skill_type,
            str(step.status),
            step.started_at,
            step.duration_ms,
            step.error,
            _dumps(step.output_keys),
            _dumps(step.context_snapshot) if step.context_snapshot is not None else None,
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO steps (
                    run_id, step_index, component, skill_type, status,
                    started_at, duration_ms, error, output_keys, context_snapshot
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id, step_index) DO UPDATE SET
                    component=excluded.component,
                    skill_type=excluded.skill_type,
                    status=excluded.status,
                    started_at=excluded.started_at,
                    duration_ms=excluded.duration_ms,
                    error=excluded.error,
                    output_keys=excluded.output_keys,
                    context_snapshot=excluded.context_snapshot
                """,
                row,
            )
            self._commit()

    def load_steps(self, run_id: str) -> list[StepRecord]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM steps WHERE run_id=? ORDER BY step_index ASC",
                (run_id,),
            )
            rows = cur.fetchall()
        return [self._row_to_step(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- row mapping -------------------------------------------------------
    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            blueprint_name=row["blueprint_name"],
            blueprint_version=row["blueprint_version"],
            blueprint_path=row["blueprint_path"],
            blueprint_snapshot=json.loads(row["blueprint_snapshot"]),
            flow_type=row["flow_type"],
            status=RunStatus(row["status"]),
            initial_data=json.loads(row["initial_data"]),
            final_context=json.loads(row["final_context"]) if row["final_context"] else None,
            error=row["error"],
            suspended_at_node=row["suspended_at_node"],
            suspension_reason=row["suspension_reason"],
            checkpoint_id=row["checkpoint_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            duration_ms=row["duration_ms"],
            wait_step_id=row["wait_step_id"],
            resume_seq=row["resume_seq"] or 0,
            resume_action_id=row["resume_action_id"],
            resume_payload=json.loads(row["resume_payload"]) if row["resume_payload"] else None,
            lease_owner=row["lease_owner"],
            lease_until=row["lease_until"],
        )

    @staticmethod
    def _row_to_envelope(row: sqlite3.Row) -> EnvelopeRecord:
        return EnvelopeRecord(
            run_id=row["run_id"],
            seq=row["seq"],
            step_id=row["step_id"],
            kind=row["kind"],
            payload=json.loads(row["payload"]),
            session_id=row["session_id"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_step(row: sqlite3.Row) -> StepRecord:
        return StepRecord(
            run_id=row["run_id"],
            step_index=row["step_index"],
            component=row["component"],
            skill_type=row["skill_type"],
            status=StepStatus(row["status"]),
            started_at=row["started_at"],
            duration_ms=row["duration_ms"],
            error=row["error"],
            output_keys=json.loads(row["output_keys"]),
            context_snapshot=json.loads(row["context_snapshot"])
            if row["context_snapshot"]
            else None,
        )
