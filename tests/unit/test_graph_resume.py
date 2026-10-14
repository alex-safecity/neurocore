"""Resume of graph flows routed through flowengine's GraphExecutor (0.5.0).

A suspended graph flow — ports, cycles — resumes at the node that suspended: the
suspended node is handed to the executor, completed nodes propagate the ports they
set, nothing upstream re-executes, and the run completes. Before 0.5.0 the cyclic
flow re-ran its upstream nodes and the acyclic flow silently skipped the suspended
node behind its port edge.
"""

from __future__ import annotations

from flowengine import FlowContext

from neurocore import Skill, SkillMeta
from neurocore.config.schema import NeuroCoreConfig, PersistenceConfig
from neurocore.persistence import InMemoryRunStore, RunStatus
from neurocore.runtime.blueprint import (
    Blueprint,
    BlueprintComponent,
    FlowDefinition,
    FlowEdge,
    FlowGraph,
)
from neurocore.runtime.executor import execute_blueprint_tracked, resume_blueprint
from neurocore.skills.registry import SkillRegistry


class Router(Skill):
    skill_meta = SkillMeta(name="t.router", version="0.1.0")

    def process(self, context: FlowContext) -> FlowContext:
        context.set("order", [*context.get("order", []), self.name])
        self.set_output_port(context, self.config.get("port", "yes"))
        return context


class Counter(Skill):
    skill_meta = SkillMeta(name="t.counter", version="0.1.0")

    def process(self, context: FlowContext) -> FlowContext:
        n = context.get("n", 0) + 1
        context.set("n", n)
        context.set("order", [*context.get("order", []), self.name])
        self.set_output_port(context, "again" if n < self.config.get("times", 2) else "done")
        return context


class Gate(Skill):
    """Suspends until resume_data carries a choice, then exits on 'chosen'."""

    skill_meta = SkillMeta(name="t.gate", version="0.1.0")

    def process(self, context: FlowContext) -> FlowContext:
        context.set("order", [*context.get("order", []), self.name])
        data = context.get("resume_data")
        choice = getattr(data, "choice", None) if data is not None else None
        if choice is not None:
            context.set("choice", choice)
            context.delete("resume_data")
            self.set_output_port(context, "chosen")
            return context
        context.suspend(node_id=self.name, reason="waiting for the citizen")
        return context


class Mark(Skill):
    skill_meta = SkillMeta(name="t.mark", version="0.1.0")

    def process(self, context: FlowContext) -> FlowContext:
        context.set("order", [*context.get("order", []), self.name])
        return context


def _registry() -> SkillRegistry:
    reg = SkillRegistry()
    for cls in (Router, Counter, Gate, Mark):
        reg.register(cls)
    return reg


def _config() -> NeuroCoreConfig:
    return NeuroCoreConfig(persistence=PersistenceConfig(backend="memory"))


def _cyclic() -> Blueprint:
    """router --yes--> counter (loops twice) --done--> gate (suspends) --chosen--> mark."""
    return Blueprint(
        name="cyclic",
        components=[
            BlueprintComponent(name="router", type="t.router", config={"port": "yes"}),
            BlueprintComponent(name="counter", type="t.counter", config={"times": 2}),
            BlueprintComponent(name="gate", type="t.gate"),
            BlueprintComponent(name="mark", type="t.mark"),
            BlueprintComponent(name="other", type="t.mark"),
        ],
        flow=FlowDefinition(
            type="graph",
            nodes=[
                FlowGraph(id="n_router", component="router"),
                FlowGraph(id="n_counter", component="counter", max_visits=5),
                FlowGraph(id="n_gate", component="gate"),
                FlowGraph(id="n_mark", component="mark"),
                FlowGraph(id="n_other", component="other"),
            ],
            edges=[
                FlowEdge(source="n_router", target="n_counter", port="yes"),
                FlowEdge(source="n_router", target="n_other", port="no"),
                FlowEdge(source="n_counter", target="n_counter", port="again"),
                FlowEdge(source="n_counter", target="n_gate", port="done"),
                FlowEdge(source="n_gate", target="n_mark", port="chosen"),
            ],
        ),
    )


def _acyclic() -> Blueprint:
    """router --yes--> gate (suspends) --chosen--> mark ; router --no--> other."""
    return Blueprint(
        name="acyclic",
        components=[
            BlueprintComponent(name="router", type="t.router", config={"port": "yes"}),
            BlueprintComponent(name="gate", type="t.gate"),
            BlueprintComponent(name="mark", type="t.mark"),
            BlueprintComponent(name="other", type="t.mark"),
        ],
        flow=FlowDefinition(
            type="graph",
            nodes=[
                FlowGraph(id="n_router", component="router"),
                FlowGraph(id="n_gate", component="gate"),
                FlowGraph(id="n_mark", component="mark"),
                FlowGraph(id="n_other", component="other"),
            ],
            edges=[
                FlowEdge(source="n_router", target="n_gate", port="yes"),
                FlowEdge(source="n_router", target="n_other", port="no"),
                FlowEdge(source="n_gate", target="n_mark", port="chosen"),
            ],
        ),
    )


def _suspend_then_resume(bp: Blueprint) -> tuple[FlowContext, FlowContext, InMemoryRunStore]:
    store = InMemoryRunStore()
    first = execute_blueprint_tracked(bp, _registry(), _config(), run_store=store)
    run = store.list_runs()[0]
    assert first.metadata.suspended and run.status == RunStatus.SUSPENDED
    assert run.suspended_at_node == "n_gate"  # the node id, not the component name
    resumed = resume_blueprint(
        run.run_id, _registry(), _config(), resume_data={"choice": "fine-1"}, run_store=store
    )
    return first, resumed, store


def test_cyclic_graph_resumes_at_the_gate_without_reexecuting_upstream():
    first, resumed, store = _suspend_then_resume(_cyclic())
    assert first.get("n") == 2
    assert first.metadata.node_visit_counts == {"n_router": 1, "n_counter": 2}

    assert not resumed.metadata.suspended
    assert resumed.get("choice") == "fine-1"
    assert resumed.get("n") == 2, "the counter must not run again on resume"
    assert resumed.get("order") == ["router", "counter", "counter", "gate", "gate", "mark"]
    assert resumed.metadata.node_visit_counts["n_router"] == 1
    assert store.list_runs()[0].status == RunStatus.COMPLETED


def test_acyclic_graph_resumes_the_gate_behind_its_port_edge():
    _, resumed, store = _suspend_then_resume(_acyclic())
    assert not resumed.metadata.suspended
    assert resumed.get("choice") == "fine-1"
    assert resumed.get("order") == ["router", "gate", "gate", "mark"]
    assert "gate" not in resumed.metadata.skipped_components
    assert store.list_runs()[0].status == RunStatus.COMPLETED


def test_run_record_carries_the_executor_state():
    store = InMemoryRunStore()
    execute_blueprint_tracked(_cyclic(), _registry(), _config(), run_store=store)
    meta = store.list_runs()[0].final_context["metadata"]
    assert meta["suspended_at_node"] == "n_gate"
    assert meta["node_ports"] == {"n_router": "yes", "n_counter": "done"}
    assert meta["pending_nodes"] == []


def test_a_queued_run_resumes_with_the_accepted_payload():
    """The 0.5.0 resume protocol end to end: request_resume queues the run with the
    action's payload; the worker that claims it calls resume_blueprint without data."""
    store = InMemoryRunStore()
    execute_blueprint_tracked(_acyclic(), _registry(), _config(), run_store=store)
    run = store.list_runs()[0]
    outcome = store.request_resume(
        run.run_id, action_id="click-1", payload={"choice": "fine-2"}, expected_step_id="n_gate"
    )
    assert outcome.accepted and store.load_run(run.run_id).status == RunStatus.QUEUED
    # the worker claims the lease first (the row is RUNNING by then), then resumes
    assert store.acquire_lease(run.run_id, "w1", 30, expected_resume_seq=1)
    assert store.load_run(run.run_id).status == RunStatus.RUNNING
    resumed = resume_blueprint(run.run_id, _registry(), _config(), run_store=store)
    assert resumed.get("choice") == "fine-2"
    assert store.load_run(run.run_id).status == RunStatus.COMPLETED
