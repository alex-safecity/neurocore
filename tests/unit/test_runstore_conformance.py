"""RunStore conformance (0.5.0): the resume protocol, the row lease, the envelope log and
the unit of work behave identically on every implementation.

The suite lives in :mod:`neurocore.persistence.conformance` so stores outside this
package run the same tests. Here: the in-memory store (reference semantics) and the
SQLite store (real conditional updates), plus the SQLite-only durability checks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from neurocore.persistence import InMemoryRunStore, RunStatus, SQLiteRunStore
from neurocore.persistence.conformance import RunStoreConformance
from neurocore.runtime.events import FlowEventType


class TestInMemoryRunStore(RunStoreConformance):
    @pytest.fixture
    def store(self):
        s = InMemoryRunStore()
        yield s
        s.close()


class TestSQLiteRunStore(RunStoreConformance):
    @pytest.fixture
    def store(self, tmp_path: Path):
        s = SQLiteRunStore(tmp_path / "runs.sqlite")
        yield s
        s.close()


# ── durability and schema upgrade (sqlite only) ───────────────────────────────


def test_sqlite_state_survives_reopen_and_old_files_get_the_new_columns(tmp_path: Path):
    import sqlite3

    path = tmp_path / "runs.sqlite"
    # a 0.4.0-shaped file: the runs table without the 0.5.0 columns
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, blueprint_name TEXT NOT NULL,
            blueprint_version TEXT NOT NULL, blueprint_path TEXT, blueprint_snapshot TEXT NOT NULL,
            flow_type TEXT NOT NULL, status TEXT NOT NULL, initial_data TEXT NOT NULL,
            final_context TEXT, error TEXT, suspended_at_node TEXT, suspension_reason TEXT,
            checkpoint_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, duration_ms REAL);
        INSERT INTO runs VALUES ('old', 'bp', '1.0', NULL, '{}', 'graph', 'suspended', '{}',
            NULL, NULL, 'n_gate', 'waiting', NULL, '2026-10-01T00:00:00+00:00',
            '2026-10-01T00:00:00+00:00', NULL);
        """
    )
    conn.commit()
    conn.close()

    store = SQLiteRunStore(path)
    old = store.load_run("old")
    assert old is not None and old.resume_seq == 0 and old.lease_owner is None
    old.wait_step_id = "n_gate"
    store.save_run(old)
    assert store.request_resume("old", action_id="a-1", expected_step_id="n_gate").accepted
    store.close()

    reopened = SQLiteRunStore(path)
    run = reopened.load_run("old")
    assert run is not None and run.status == RunStatus.QUEUED and run.resume_seq == 1
    reopened.close()


def test_flow_event_kinds_for_the_runtime():
    assert {FlowEventType.ENVELOPE, FlowEventType.GUARD_DECISION, FlowEventType.TIMER_FIRED} <= set(
        FlowEventType
    )
    assert FlowEventType("envelope") is FlowEventType.ENVELOPE
