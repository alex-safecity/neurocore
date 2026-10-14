"""RunStore conformance (0.5.0): the resume protocol, the row lease, the envelope log and
the unit of work behave identically on every implementation.

Run against the in-memory store (reference semantics) and the SQLite store (real
conditional updates). A new store implementation is added to ``STORES`` and must pass.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from neurocore.persistence import (
    EnvelopeRecord,
    InMemoryRunStore,
    RunRecord,
    RunStatus,
    RunStore,
    SQLiteRunStore,
    StepRecord,
)
from neurocore.runtime.events import FlowEventType

STORES: dict[str, Callable[[Path], RunStore]] = {
    "memory": lambda tmp: InMemoryRunStore(),
    "sqlite": lambda tmp: SQLiteRunStore(tmp / "runs.sqlite"),
}


@pytest.fixture(params=list(STORES))
def store(request, tmp_path: Path) -> RunStore:
    s = STORES[request.param](tmp_path)
    yield s
    s.close()


def _suspended(store: RunStore, run_id: str = "r1", step: str = "n_gate") -> RunRecord:
    run = RunRecord(
        run_id=run_id,
        blueprint_name="bp",
        status=RunStatus.SUSPENDED,
        suspended_at_node=step,
        wait_step_id=step,
    )
    store.save_run(run)
    return run


# ── resume protocol (D58) ─────────────────────────────────────────────────────


def test_resume_is_accepted_exactly_once_and_replays_the_same_action(store: RunStore):
    _suspended(store)
    first = store.request_resume("r1", action_id="a-1", payload={"choice": "fine-1"})
    assert first.accepted and not first.replay and first.resume_seq == 1
    assert first.status == RunStatus.QUEUED

    again = store.request_resume("r1", action_id="a-1", payload={"choice": "fine-1"})
    assert again.accepted and again.replay and again.resume_seq == 1

    other = store.request_resume("r1", action_id="a-2", payload={"choice": "fine-2"})
    assert not other.accepted and other.reason == "conflict" and other.status == RunStatus.QUEUED

    run = store.load_run("r1")
    assert run is not None
    assert run.resume_seq == 1 and run.resume_action_id == "a-1"
    assert run.resume_payload == {"choice": "fine-1"}


def test_resume_checks_the_step_it_waits_at(store: RunStore):
    _suspended(store, step="n_gate")
    wrong = store.request_resume("r1", action_id="a-1", expected_step_id="n_other")
    assert not wrong.accepted and wrong.reason == "wrong_step"
    right = store.request_resume("r1", action_id="a-1", expected_step_id="n_gate")
    assert right.accepted


def test_resume_of_unknown_or_running_run_is_refused(store: RunStore):
    assert store.request_resume("nope", action_id="a").reason == "not_found"
    store.save_run(RunRecord(run_id="r2", blueprint_name="bp", status=RunStatus.RUNNING))
    out = store.request_resume("r2", action_id="a")
    assert not out.accepted and out.reason == "conflict"


def test_fifty_parallel_resumes_accept_one(store: RunStore):
    _suspended(store)
    results = []
    lock = threading.Lock()

    def attempt(i: int) -> None:
        out = store.request_resume("r1", action_id=f"a-{i}", payload={"i": i})
        with lock:
            results.append(out)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    accepted = [r for r in results if r.accepted]
    assert len(accepted) == 1 and accepted[0].resume_seq == 1
    assert all(r.reason == "conflict" for r in results if not r.accepted)


# ── row lease (D57) ───────────────────────────────────────────────────────────


def test_one_worker_claims_a_queued_run(store: RunStore):
    _suspended(store)
    seq = store.request_resume("r1", action_id="a-1").resume_seq
    assert store.acquire_lease("r1", "worker-a", 60, expected_resume_seq=seq)
    assert not store.acquire_lease("r1", "worker-b", 60, expected_resume_seq=seq)
    assert store.acquire_lease("r1", "worker-a", 60), "the holder may re-acquire"
    run = store.load_run("r1")
    assert run is not None and run.status == RunStatus.RUNNING and run.lease_owner == "worker-a"


def test_stale_resume_seq_is_dropped(store: RunStore):
    _suspended(store)
    store.request_resume("r1", action_id="a-1")  # seq 1
    assert not store.acquire_lease("r1", "worker-a", 60, expected_resume_seq=0)
    assert store.acquire_lease("r1", "worker-a", 60, expected_resume_seq=1)


def test_expired_lease_is_reclaimable_and_listed_for_the_sweeper(store: RunStore):
    _suspended(store)
    store.request_resume("r1", action_id="a-1")
    assert store.acquire_lease("r1", "worker-a", 0.01)
    import time

    time.sleep(0.05)
    assert [r.run_id for r in store.expired_leases()] == ["r1"]
    assert not store.renew_lease("r1", "worker-a", 60), "an expired lease cannot be renewed"
    assert store.acquire_lease("r1", "worker-b", 60), "another worker reclaims it"
    assert store.expired_leases() == []
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert [r.run_id for r in store.expired_leases(future)] == ["r1"]


def test_renew_and_release(store: RunStore):
    _suspended(store)
    store.request_resume("r1", action_id="a-1")
    assert store.acquire_lease("r1", "worker-a", 60)
    assert store.renew_lease("r1", "worker-a", 120)
    assert not store.renew_lease("r1", "worker-b", 120)
    store.release_lease("r1", "worker-b")  # not the holder: no-op
    assert store.load_run("r1").lease_owner == "worker-a"  # type: ignore[union-attr]
    store.release_lease("r1", "worker-a")
    run = store.load_run("r1")
    assert run is not None and run.lease_owner is None and run.lease_until is None


def test_lease_needs_a_claimable_status(store: RunStore):
    store.save_run(RunRecord(run_id="done", blueprint_name="bp", status=RunStatus.COMPLETED))
    assert not store.acquire_lease("done", "worker-a", 60)
    _suspended(store, run_id="susp")
    assert not store.acquire_lease("susp", "worker-a", 60), "suspended runs are not claimable"


# ── envelopes ─────────────────────────────────────────────────────────────────


def test_envelope_log(store: RunStore):
    _suspended(store)
    assert store.latest_envelope("r1") is None
    for seq, kind in ((1, "chat"), (2, "choose"), (3, "confirm")):
        store.save_envelope(
            EnvelopeRecord(
                run_id="r1",
                seq=seq,
                step_id="n_gate",
                kind=kind,
                payload={"seq": seq},
                session_id="s1",
            )
        )
    latest = store.latest_envelope("r1")
    assert latest is not None and latest.kind == "confirm" and latest.payload == {"seq": 3}
    assert [e.seq for e in store.load_envelopes("r1", since_seq=1)] == [2, 3]
    with pytest.raises(ValueError, match="already exists"):
        store.save_envelope(EnvelopeRecord(run_id="r1", seq=2, step_id="n_gate", kind="x"))
    store.delete_run("r1")
    assert store.load_envelopes("r1") == []


# ── unit of work (D55) ────────────────────────────────────────────────────────


def test_unit_of_work_rolls_back_every_write_together(store: RunStore):
    _suspended(store)
    uow = store.unit_of_work()
    assert uow.active
    store.save_step(StepRecord(run_id="r1", step_index=0, component="a"))
    store.save_envelope(EnvelopeRecord(run_id="r1", seq=1, step_id="n_gate", kind="chat"))
    run = store.load_run("r1")
    assert run is not None
    run.status = RunStatus.RUNNING
    store.save_run(run)
    uow.rollback()
    assert not uow.active
    assert store.load_steps("r1") == []
    assert store.latest_envelope("r1") is None
    assert store.load_run("r1").status == RunStatus.SUSPENDED  # type: ignore[union-attr]


def test_unit_of_work_commits_every_write_together(store: RunStore):
    _suspended(store)
    uow = store.unit_of_work()
    store.save_step(StepRecord(run_id="r1", step_index=0, component="a"))
    store.save_envelope(EnvelopeRecord(run_id="r1", seq=1, step_id="n_gate", kind="chat"))
    uow.commit()
    assert [s.component for s in store.load_steps("r1")] == ["a"]
    assert store.latest_envelope("r1") is not None
    # commit/rollback after the end are no-ops
    uow.commit()
    uow.rollback()


def test_only_one_unit_of_work_at_a_time(store: RunStore):
    uow = store.unit_of_work()
    with pytest.raises(RuntimeError, match="already open"):
        store.unit_of_work()
    uow.rollback()
    store.unit_of_work().commit()


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
