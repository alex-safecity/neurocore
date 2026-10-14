"""RunStore conformance (0.5.0) as a reusable test class.

Every implementation — the in-memory reference, SQLite, and stores that live outside
this package (a SQL Server store, say) — must pass the same suite. A test module
subclasses :class:`RunStoreConformance` and provides the ``store`` fixture::

    class TestMyStore(RunStoreConformance):
        @pytest.fixture
        def store(self):
            s = MyStore(...)
            yield s
            s.close()

The suite covers the resume protocol (D58), the row lease (D57), the envelope log and
the unit of work (D55). It needs ``pytest`` at import time, so it is imported by test
modules only.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from neurocore.persistence.base import (
    EnvelopeRecord,
    RunRecord,
    RunStatus,
    RunStore,
    StepRecord,
)


def suspended_run(store: RunStore, run_id: str = "r1", step: str = "n_gate") -> RunRecord:
    """Save and return a run suspended at ``step`` (the shape a citizen step leaves)."""
    run = RunRecord(
        run_id=run_id,
        blueprint_name="bp",
        status=RunStatus.SUSPENDED,
        suspended_at_node=step,
        wait_step_id=step,
    )
    store.save_run(run)
    return run


class RunStoreConformance:
    """Subclass and provide a ``store`` fixture yielding a fresh, empty store."""

    # ── resume protocol (D58) ────────────────────────────────────────────────

    def test_resume_is_accepted_exactly_once_and_replays_the_same_action(self, store: RunStore):
        suspended_run(store)
        first = store.request_resume("r1", action_id="a-1", payload={"choice": "fine-1"})
        assert first.accepted and not first.replay and first.resume_seq == 1
        assert first.status == RunStatus.QUEUED

        again = store.request_resume("r1", action_id="a-1", payload={"choice": "fine-1"})
        assert again.accepted and again.replay and again.resume_seq == 1

        other = store.request_resume("r1", action_id="a-2", payload={"choice": "fine-2"})
        assert not other.accepted and other.reason == "conflict"
        assert other.status == RunStatus.QUEUED

        run = store.load_run("r1")
        assert run is not None
        assert run.resume_seq == 1 and run.resume_action_id == "a-1"
        assert run.resume_payload == {"choice": "fine-1"}

    def test_resume_checks_the_step_it_waits_at(self, store: RunStore):
        suspended_run(store, step="n_gate")
        wrong = store.request_resume("r1", action_id="a-1", expected_step_id="n_other")
        assert not wrong.accepted and wrong.reason == "wrong_step"
        right = store.request_resume("r1", action_id="a-1", expected_step_id="n_gate")
        assert right.accepted

    def test_resume_of_unknown_or_running_run_is_refused(self, store: RunStore):
        assert store.request_resume("nope", action_id="a").reason == "not_found"
        store.save_run(RunRecord(run_id="r2", blueprint_name="bp", status=RunStatus.RUNNING))
        out = store.request_resume("r2", action_id="a")
        assert not out.accepted and out.reason == "conflict"

    def test_fifty_parallel_resumes_accept_one(self, store: RunStore):
        suspended_run(store)
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

    # ── row lease (D57) ──────────────────────────────────────────────────────

    def test_one_worker_claims_a_queued_run(self, store: RunStore):
        suspended_run(store)
        seq = store.request_resume("r1", action_id="a-1").resume_seq
        assert store.acquire_lease("r1", "worker-a", 60, expected_resume_seq=seq)
        assert not store.acquire_lease("r1", "worker-b", 60, expected_resume_seq=seq)
        assert store.acquire_lease("r1", "worker-a", 60), "the holder may re-acquire"
        run = store.load_run("r1")
        assert run is not None
        assert run.status == RunStatus.RUNNING and run.lease_owner == "worker-a"

    def test_stale_resume_seq_is_dropped(self, store: RunStore):
        suspended_run(store)
        store.request_resume("r1", action_id="a-1")  # seq 1
        assert not store.acquire_lease("r1", "worker-a", 60, expected_resume_seq=0)
        assert store.acquire_lease("r1", "worker-a", 60, expected_resume_seq=1)

    def test_expired_lease_is_reclaimable_and_listed_for_the_sweeper(self, store: RunStore):
        suspended_run(store)
        store.request_resume("r1", action_id="a-1")
        assert store.acquire_lease("r1", "worker-a", 0.01)
        time.sleep(0.05)
        assert [r.run_id for r in store.expired_leases()] == ["r1"]
        assert not store.renew_lease("r1", "worker-a", 60), "an expired lease cannot be renewed"
        assert store.acquire_lease("r1", "worker-b", 60), "another worker reclaims it"
        assert store.expired_leases() == []
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        assert [r.run_id for r in store.expired_leases(future)] == ["r1"]

    def test_renew_and_release(self, store: RunStore):
        suspended_run(store)
        store.request_resume("r1", action_id="a-1")
        assert store.acquire_lease("r1", "worker-a", 60)
        assert store.renew_lease("r1", "worker-a", 120)
        assert not store.renew_lease("r1", "worker-b", 120)
        store.release_lease("r1", "worker-b")  # not the holder: no-op
        assert store.load_run("r1").lease_owner == "worker-a"  # type: ignore[union-attr]
        store.release_lease("r1", "worker-a")
        run = store.load_run("r1")
        assert run is not None and run.lease_owner is None and run.lease_until is None

    def test_lease_needs_a_claimable_status(self, store: RunStore):
        store.save_run(RunRecord(run_id="done", blueprint_name="bp", status=RunStatus.COMPLETED))
        assert not store.acquire_lease("done", "worker-a", 60)
        suspended_run(store, run_id="susp")
        assert not store.acquire_lease("susp", "worker-a", 60), "suspended runs are not claimable"

    # ── envelopes ────────────────────────────────────────────────────────────

    def test_envelope_log(self, store: RunStore):
        suspended_run(store)
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

    # ── run and step history ─────────────────────────────────────────────────

    def test_run_record_round_trips_and_lists_newest_first(self, store: RunStore):
        run = RunRecord(
            run_id="r-a",
            blueprint_name="bp",
            blueprint_version="2.1",
            blueprint_path="flows/bp.yaml",
            blueprint_snapshot={"nodes": [{"id": "n1"}]},
            flow_type="graph",
            initial_data={"q": "عربي"},
            final_context={"out": 1},
            error=None,
            duration_ms=12.5,
        )
        store.save_run(run)
        store.save_run(RunRecord(run_id="r-b", blueprint_name="other", status=RunStatus.FAILED))
        loaded = store.load_run("r-a")
        assert loaded is not None
        assert loaded.blueprint_snapshot == {"nodes": [{"id": "n1"}]}
        assert loaded.initial_data == {"q": "عربي"} and loaded.final_context == {"out": 1}
        assert loaded.blueprint_version == "2.1" and loaded.duration_ms == 12.5
        assert [r.run_id for r in store.list_runs()] == ["r-b", "r-a"]
        assert [r.run_id for r in store.list_runs(status=RunStatus.FAILED)] == ["r-b"]
        assert [r.run_id for r in store.list_runs(blueprint="bp")] == ["r-a"]
        assert store.load_run("missing") is None

    def test_steps_round_trip_in_order(self, store: RunStore):
        suspended_run(store)
        store.save_step(StepRecord(run_id="r1", step_index=1, component="b", output_keys=["y"]))
        store.save_step(
            StepRecord(
                run_id="r1",
                step_index=0,
                component="a",
                skill_type="demo.echo",
                output_keys=["x"],
                context_snapshot={"x": 1},
                duration_ms=3.0,
            )
        )
        steps = store.load_steps("r1")
        assert [s.component for s in steps] == ["a", "b"]
        assert steps[0].skill_type == "demo.echo" and steps[0].context_snapshot == {"x": 1}
        store.save_step(StepRecord(run_id="r1", step_index=0, component="a", error="boom"))
        assert store.load_steps("r1")[0].error == "boom", "upsert by (run_id, step_index)"
        store.delete_run("r1")
        assert store.load_steps("r1") == [] and store.load_run("r1") is None

    # ── unit of work (D55) ───────────────────────────────────────────────────

    def test_unit_of_work_rolls_back_every_write_together(self, store: RunStore):
        suspended_run(store)
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

    def test_unit_of_work_commits_every_write_together(self, store: RunStore):
        suspended_run(store)
        uow = store.unit_of_work()
        store.save_step(StepRecord(run_id="r1", step_index=0, component="a"))
        store.save_envelope(EnvelopeRecord(run_id="r1", seq=1, step_id="n_gate", kind="chat"))
        uow.commit()
        assert [s.component for s in store.load_steps("r1")] == ["a"]
        assert store.latest_envelope("r1") is not None
        # commit/rollback after the end are no-ops
        uow.commit()
        uow.rollback()

    def test_only_one_unit_of_work_at_a_time(self, store: RunStore):
        uow = store.unit_of_work()
        with pytest.raises(RuntimeError, match="already open"):
            store.unit_of_work()
        uow.rollback()
        store.unit_of_work().commit()


__all__ = ["RunStoreConformance", "suspended_run"]
