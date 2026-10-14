"""Tests for A3 — executor run tracking and resume."""
from __future__ import annotations

import pytest
from flowengine import FlowContext

from neurocore import Skill, SkillMeta
from neurocore.config.schema import NeuroCoreConfig, PersistenceConfig
from neurocore.errors import ExecutionError
from neurocore.persistence import InMemoryRunStore, RunStatus, StepStatus
from neurocore.runtime.blueprint import (
    Blueprint,
    BlueprintComponent,
    FlowDefinition,
    FlowEdge,
    FlowGraph,
    FlowStep,
)
from neurocore.runtime.executor import execute_blueprint_tracked, resume_blueprint
from neurocore.skills.registry import SkillRegistry


class WriteA(Skill):
    skill_meta = SkillMeta(name="write-a", version="0.1.0", provides=["a"])

    def process(self, context: FlowContext) -> FlowContext:
        context.set("a", 1)
        return context


class WriteB(Skill):
    skill_meta = SkillMeta(name="write-b", version="0.1.0", provides=["b"])

    def process(self, context: FlowContext) -> FlowContext:
        context.set("b", 2)
        return context


class Boom(Skill):
    skill_meta = SkillMeta(name="boom", version="0.1.0")

    def process(self, context: FlowContext) -> FlowContext:
        raise RuntimeError("kaboom")


class SuspendOnce(Skill):
    """Suspends on the first pass; on resume reads resume_data and continues."""

    skill_meta = SkillMeta(name="suspend-once", version="0.1.0", provides=["decision"])

    def process(self, context: FlowContext) -> FlowContext:
        rd = context.get("resume_data")
        if rd is not None:
            context.set("decision", rd)
            context.delete("resume_data")
            return context
        context.suspend(node_id=self.name, reason="awaiting input")
        return context


class FailFirst(Skill):
    """Fails the first time, succeeds when re-run (uses a class counter)."""

    skill_meta = SkillMeta(name="fail-first", version="0.1.0", provides=["done"])
    attempts = 0

    def process(self, context: FlowContext) -> FlowContext:
        FailFirst.attempts += 1
        if FailFirst.attempts == 1:
            raise RuntimeError("transient")
        context.set("done", True)
        return context


def _registry(*skills):
    reg = SkillRegistry()
    for s in skills:
        reg.register(s)
    return reg


def _config():
    return NeuroCoreConfig(persistence=PersistenceConfig(backend="memory"))


def _seq(*components):
    return Blueprint(
        name="bp",
        version="1.0",
        components=[BlueprintComponent(name=c, type=c) for c in components],
        flow=FlowDefinition(
            type="sequential", steps=[FlowStep(component=c) for c in components]
        ),
    )


# ---- sequential tracking --------------------------------------------------

def test_tracked_sequential_records_run_and_steps():
    store = InMemoryRunStore()
    bp = _seq("write-a", "write-b")
    execute_blueprint_tracked(
        bp, _registry(WriteA, WriteB), _config(), run_store=store
    )
    runs = store.list_runs()
    assert len(runs) == 1
    assert runs[0].status == RunStatus.COMPLETED
    steps = store.load_steps(runs[0].run_id)
    assert [s.component for s in steps] == ["write-a", "write-b"]
    assert steps[0].output_keys == ["a"]
    assert runs[0].final_context["data"]["a"] == 1
    assert runs[0].final_context["metadata"]["completed_nodes"] == ["write-a", "write-b"]


def test_tracked_failure_records_failed_run_and_step():
    store = InMemoryRunStore()
    bp = _seq("write-a", "boom")
    with pytest.raises(ExecutionError):
        execute_blueprint_tracked(
            bp, _registry(WriteA, Boom), _config(), run_store=store
        )
    run = store.list_runs()[0]
    assert run.status == RunStatus.FAILED
    assert "kaboom" in (run.error or "")
    steps = store.load_steps(run.run_id)
    statuses = {s.component: s.status for s in steps}
    assert statuses["write-a"] == StepStatus.COMPLETED
    assert statuses["boom"] == StepStatus.FAILED


def test_persistence_disabled_falls_back():
    cfg = NeuroCoreConfig(persistence=PersistenceConfig(enabled=False))
    ctx = execute_blueprint_tracked(_seq("write-a"), _registry(WriteA), cfg)
    assert ctx.get("a") == 1


# ---- DAG tracking ---------------------------------------------------------

def test_tracked_dag_records_nodes():
    store = InMemoryRunStore()
    bp = Blueprint(
        name="dag", version="1.0",
        components=[
            BlueprintComponent(name="a", type="write-a"),
            BlueprintComponent(name="b", type="write-b"),
        ],
        flow=FlowDefinition(
            type="graph",
            nodes=[FlowGraph(id="a", component="a"), FlowGraph(id="b", component="b")],
            edges=[FlowEdge(source="a", target="b")],
        ),
    )
    execute_blueprint_tracked(bp, _registry(WriteA, WriteB), _config(), run_store=store)
    run = store.list_runs()[0]
    assert run.status == RunStatus.COMPLETED
    assert run.final_context["metadata"]["completed_nodes"] == ["a", "b"]


# ---- suspend + resume (async path) ---------------------------------------

class AsyncSuspend(SuspendOnce):
    """Force the async execution path by being async."""

    skill_meta = SkillMeta(
        name="async-suspend", version="0.1.0", provides=["decision"]
    )

    async def process(self, context: FlowContext) -> FlowContext:  # type: ignore[override]
        rd = context.get("resume_data")
        if rd is not None:
            context.set("decision", rd)
            context.delete("resume_data")
            return context
        context.suspend(node_id=self.name, reason="awaiting input")
        return context


def test_suspend_and_resume_async_path():
    store = InMemoryRunStore()
    bp = Blueprint(
        name="bp", version="1.0",
        components=[
            BlueprintComponent(name="write-a", type="write-a"),
            BlueprintComponent(name="gate", type="async-suspend"),
            BlueprintComponent(name="write-b", type="write-b"),
        ],
        flow=FlowDefinition(type="sequential", steps=[
            FlowStep(component="write-a"),
            FlowStep(component="gate"),
            FlowStep(component="write-b"),
        ]),
    )
    reg = _registry(WriteA, AsyncSuspend, WriteB)
    ctx = execute_blueprint_tracked(bp, reg, _config(), run_store=store)
    assert ctx.metadata.suspended
    run = store.list_runs()[0]
    assert run.status == RunStatus.SUSPENDED
    assert run.suspended_at_node == "gate"
    assert run.final_context["metadata"]["completed_nodes"] == ["write-a"]

    resumed = resume_blueprint(
        run.run_id, reg, _config(), resume_data={"ok": True}, run_store=store
    )
    assert resumed.get("decision") == {"ok": True}
    assert resumed.get("b") == 2  # step after the gate ran
    run2 = store.load_run(run.run_id)
    assert run2.status == RunStatus.COMPLETED


def test_resume_after_failure_skips_completed_steps():
    FailFirst.attempts = 0
    store = InMemoryRunStore()

    class AWrite(WriteA):
        skill_meta = SkillMeta(name="awrite", version="0.1.0", provides=["a"])

        async def process(self, context: FlowContext) -> FlowContext:  # async path
            context.set("a", 1)
            return context

    class AFail(FailFirst):
        skill_meta = SkillMeta(name="afail", version="0.1.0", provides=["done"])

        async def process(self, context: FlowContext) -> FlowContext:  # async path
            AFail.attempts += 1
            if AFail.attempts == 1:
                raise RuntimeError("transient")
            context.set("done", True)
            return context

    AFail.attempts = 0
    bp = Blueprint(
        name="bp", version="1.0",
        components=[
            BlueprintComponent(name="awrite", type="awrite"),
            BlueprintComponent(name="afail", type="afail"),
        ],
        flow=FlowDefinition(type="sequential", steps=[
            FlowStep(component="awrite"), FlowStep(component="afail"),
        ]),
    )
    reg = _registry(AWrite, AFail)
    with pytest.raises(ExecutionError):
        execute_blueprint_tracked(bp, reg, _config(), run_store=store)
    run = store.list_runs()[0]
    assert run.status == RunStatus.FAILED

    resumed = resume_blueprint(run.run_id, reg, _config(), run_store=store)
    assert resumed.get("done") is True
    # afail ran twice total (once failed, once on resume); awrite only once
    assert AFail.attempts == 2
    assert store.load_run(run.run_id).status == RunStatus.COMPLETED


def test_resume_rejects_completed_run():
    store = InMemoryRunStore()
    execute_blueprint_tracked(_seq("write-a"), _registry(WriteA), _config(),
                              run_store=store)
    run = store.list_runs()[0]
    with pytest.raises(ExecutionError, match="only suspended, queued"):
        resume_blueprint(run.run_id, _registry(WriteA), _config(), run_store=store)
