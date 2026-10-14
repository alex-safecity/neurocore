"""The guarded invocation contract (0.5.0, moi-v2 H2 / D54 / D55).

Coverage: with the registry instrumented, every execution path — FlowEngine
sequential, async sequential, conditional, GraphExecutor sync and async (plain DAG,
ports, cycles, bindings), streaming, resume — produces exactly one guard invocation per
executed step. A path that called a skill without the contract would show up as a step
with no invocation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest
from flowengine import FlowContext

from neurocore import AsyncSkill, Skill, SkillMeta
from neurocore.config.schema import NeuroCoreConfig, PersistenceConfig
from neurocore.persistence import InMemoryRunStore, RunStatus
from neurocore.runtime.blueprint import (
    Blueprint,
    BlueprintComponent,
    FlowDefinition,
    FlowEdge,
    FlowGraph,
    FlowStep,
)
from neurocore.runtime.executor import (
    execute_blueprint,
    execute_blueprint_stream,
    execute_blueprint_tracked,
    resume_blueprint,
)
from neurocore.runtime.invocation import (
    ERROR_PORT,
    GuardDecision,
    Guarded,
    GuardedAsync,
    InvocationPolicy,
    NoopUnitOfWork,
    current_unit_of_work,
    invoke_skill,
)
from neurocore.skills.registry import SkillRegistry

# ── skills ───────────────────────────────────────────────────────────────────

CALLS: list[str] = []  # every skill execution, whatever the path


class Mark(Skill):
    skill_meta = SkillMeta(name="t.mark", version="0.1.0", provides=["marks"])

    def process(self, context: FlowContext) -> FlowContext:
        CALLS.append(self.name)
        context.set("marks", [*context.get("marks", []), self.name])
        context.set(f"{self.name}_uow", current_unit_of_work(context) is not None)
        return context


class AsyncMark(AsyncSkill):
    skill_meta = SkillMeta(name="t.amark", version="0.1.0", provides=["marks"])

    async def process(self, context: FlowContext) -> FlowContext:
        await asyncio.sleep(0)
        CALLS.append(self.name)
        context.set("marks", [*context.get("marks", []), self.name])
        return context


class Router(Skill):
    skill_meta = SkillMeta(name="t.router", version="0.1.0")

    def process(self, context: FlowContext) -> FlowContext:
        CALLS.append(self.name)
        context.set("marks", [*context.get("marks", []), self.name])
        self.set_output_port(context, self.config.get("port", "yes"))
        return context


class Counter(Skill):
    skill_meta = SkillMeta(name="t.counter", version="0.1.0", provides=["n"])

    def process(self, context: FlowContext) -> FlowContext:
        CALLS.append(self.name)
        n = context.get("n", 0) + 1
        context.set("n", n)
        context.set("marks", [*context.get("marks", []), self.name])
        self.set_output_port(context, "again" if n < self.config.get("times", 2) else "done")
        return context


class Gate(Skill):
    skill_meta = SkillMeta(name="t.gate", version="0.1.0")

    def process(self, context: FlowContext) -> FlowContext:
        CALLS.append(self.name)
        data = context.get("resume_data")
        choice = getattr(data, "choice", None) if data is not None else None
        context.set("marks", [*context.get("marks", []), self.name])
        if choice is not None:
            context.set("choice", choice)
            context.delete("resume_data")
            self.set_output_port(context, "chosen")
            return context
        context.suspend(node_id=self.name, reason="waiting")
        return context


class Boom(Skill):
    skill_meta = SkillMeta(name="t.boom", version="0.1.0")

    def process(self, context: FlowContext) -> FlowContext:
        CALLS.append(self.name)
        raise RuntimeError("boom")


# ── instrumentation ──────────────────────────────────────────────────────────

@dataclass
class Probe:
    """Counts invocations and units of work; blocks/reviews by node id."""

    invocations: list[dict] = field(default_factory=list)
    uows: list[NoopUnitOfWork] = field(default_factory=list)
    block_before: set[str] = field(default_factory=set)
    block_after: set[str] = field(default_factory=set)
    review: set[str] = field(default_factory=set)
    warn: set[str] = field(default_factory=set)

    def before(self, skill, context, node_id):
        if node_id in self.block_before:
            return GuardDecision("BLOCK", rule_id="T-BLOCK", message="blocked by test")
        if node_id in self.review:
            review = getattr(context.get("resume_data"), "review", None)
            if review == "allow":
                return GuardDecision("ALLOW")
            return GuardDecision("REVIEW", rule_id="T-REVIEW")
        if node_id in self.warn:
            return GuardDecision("WARN", rule_id="T-WARN")
        return GuardDecision("ALLOW")

    def after(self, skill, context, node_id):
        if node_id in self.block_after:
            return GuardDecision("BLOCK", rule_id="T-AFTER", message="outputs rejected")
        return GuardDecision("ALLOW")

    def unit_of_work(self, context, node_id, skill):
        uow = NoopUnitOfWork()
        self.uows.append(uow)
        return uow

    def policy(self) -> InvocationPolicy:
        return InvocationPolicy(
            guard=self, unit_of_work=self.unit_of_work, observer=self.invocations.append
        )


def _registry(probe: Probe | None = None, *skills) -> SkillRegistry:
    reg = SkillRegistry(invocation=probe.policy() if probe else None)
    for cls in skills or (Mark, AsyncMark, Router, Counter, Gate, Boom):
        reg.register(cls)
    return reg


def _cfg() -> NeuroCoreConfig:
    return NeuroCoreConfig(persistence=PersistenceConfig(enabled=False))


def _graph(nodes, edges, components, settings=None) -> Blueprint:
    return Blueprint(
        name="g",
        components=[BlueprintComponent(**c) for c in components],
        flow=FlowDefinition(
            type="graph",
            settings=settings or {},
            nodes=[FlowGraph(**n) for n in nodes],
            edges=[FlowEdge(**e) for e in edges],
        ),
    )


def _sequential(components, steps, type_="sequential") -> Blueprint:
    return Blueprint(
        name="s",
        components=[BlueprintComponent(**c) for c in components],
        flow=FlowDefinition(type=type_, steps=[FlowStep(**s) for s in steps]),
    )


MARKS = [{"name": "a", "type": "t.mark"}, {"name": "b", "type": "t.mark"}, {"name": "c", "type": "t.mark"}]
AMARKS = [{"name": "a", "type": "t.amark"}, {"name": "b", "type": "t.amark"}]


# ── the registry wrap point ──────────────────────────────────────────────────

def test_registry_wraps_every_instance_and_keeps_async():
    reg = _registry()
    a, b = reg.wrap(Mark("a")), reg.wrap(AsyncMark("b"))
    assert type(a) is Guarded and type(b) is GuardedAsync
    assert isinstance(a, Skill) and a.skill_meta.name == "t.mark" and a.name == "a"
    assert reg.wrap(a) is a  # idempotent
    assert reg.wrapped == 3


# ── coverage: one invocation per executed step on every path ─────────────────

PATHS = {
    "sequential_sync": lambda: _sequential(MARKS, [{"component": "a"}, {"component": "b"}, {"component": "c"}]),
    "sequential_async": lambda: _sequential(AMARKS, [{"component": "a"}, {"component": "b"}]),
    "conditional": lambda: _sequential(
        MARKS, [{"component": "a", "condition": "False"}, {"component": "b", "condition": "True"}], "conditional"
    ),
    "graph_plain_dag": lambda: _graph(
        [{"id": "n_a", "component": "a"}, {"id": "n_b", "component": "b"}, {"id": "n_c", "component": "c"}],
        [{"source": "n_a", "target": "n_b"}, {"source": "n_a", "target": "n_c"}],
        MARKS,
        {"executor": "graph"},
    ),
    "graph_ports_async": lambda: _graph(
        [{"id": "n_r", "component": "r"}, {"id": "n_a", "component": "a"}, {"id": "n_b", "component": "b"}],
        [{"source": "n_r", "target": "n_a", "port": "yes"}, {"source": "n_r", "target": "n_b", "port": "no"}],
        [{"name": "r", "type": "t.router", "config": {"port": "yes"}}, *AMARKS],
    ),
    "graph_cycle_bound": lambda: _graph(
        [
            {"id": "n_count", "component": "count", "max_visits": 5, "bind": {}},
            {"id": "n_done", "component": "a", "bind": {"inputs": {"n": "n_count.n"}}},
        ],
        [{"source": "n_count", "target": "n_count", "port": "again"}, {"source": "n_count", "target": "n_done", "port": "done"}],
        [{"name": "count", "type": "t.counter", "config": {"times": 3}}, {"name": "a", "type": "t.mark"}],
    ),
}


@pytest.fixture(autouse=True)
def _reset_calls():
    CALLS.clear()
    yield
    CALLS.clear()


@pytest.mark.parametrize("path", list(PATHS))
def test_every_path_invokes_the_contract_once_per_executed_step(path):
    probe = Probe()
    ctx = execute_blueprint(PATHS[path](), _registry(probe), _cfg())
    assert len(CALLS) > 0
    assert len(probe.invocations) == len(CALLS), (path, CALLS, probe.invocations)
    assert len(probe.uows) == len(CALLS) and all(u.committed for u in probe.uows)
    assert ctx.metadata.guard_decisions == []  # ALLOW is not recorded


def test_streaming_path_invokes_the_contract():
    probe = Probe()
    bp = _sequential(MARKS, [{"component": "a"}, {"component": "b"}])

    async def run():
        return [e async for e in execute_blueprint_stream(bp, _registry(probe), _cfg())]

    asyncio.run(run())
    assert CALLS == ["a", "b"] and len(probe.invocations) == 2


def test_resume_path_invokes_the_contract_for_the_resumed_node_only():
    probe = Probe()
    store = InMemoryRunStore()
    bp = _graph(
        [{"id": "n_r", "component": "r"}, {"id": "n_g", "component": "g"}, {"id": "n_m", "component": "a"}],
        [{"source": "n_r", "target": "n_g", "port": "yes"}, {"source": "n_g", "target": "n_m", "port": "chosen"}],
        [{"name": "r", "type": "t.router"}, {"name": "g", "type": "t.gate"}, {"name": "a", "type": "t.mark"}],
    )
    execute_blueprint_tracked(bp, _registry(probe), _cfg(), run_store=store)
    assert [i["node_id"] for i in probe.invocations] == ["n_r", "n_g"]
    run = store.list_runs()[0]
    probe.invocations.clear()
    resume_blueprint(run.run_id, _registry(probe), _cfg(), resume_data={"choice": "x"}, run_store=store)
    assert [i["node_id"] for i in probe.invocations] == ["n_g", "n_m"]
    assert store.load_run(run.run_id).status == RunStatus.COMPLETED


def test_unit_of_work_is_rolled_back_when_the_skill_raises():
    probe = Probe()
    bp = _sequential([{"name": "x", "type": "t.boom"}], [{"component": "x"}])
    with pytest.raises(Exception, match="boom"):
        execute_blueprint(bp, _registry(probe), _cfg())
    assert len(probe.uows) == 1 and probe.uows[0].rolled_back and not probe.uows[0].committed


def test_unit_of_work_is_carried_in_the_context():
    ctx = execute_blueprint(_sequential(MARKS[:1], [{"component": "a"}]), _registry(Probe()), _cfg())
    assert ctx.get("a_uow") is True


# ── BLOCK halts fan-out ───────────────────────────────────────────────────────

FAN_OUT = (
    [
        {"id": "n_r", "component": "r", "bind": {}},
        {"id": "n_gate", "component": "gate", "bind": {}},
        {"id": "n_x", "component": "x", "bind": {}},
        {"id": "n_y", "component": "y", "bind": {}},
        {"id": "n_side", "component": "side", "bind": {}},
        {"id": "n_fallback", "component": "fallback", "bind": {}},
    ],
    [
        {"source": "n_r", "target": "n_gate", "port": "yes"},
        {"source": "n_r", "target": "n_side", "port": "yes"},
        {"source": "n_gate", "target": "n_x"},
        {"source": "n_gate", "target": "n_y"},
        {"source": "n_gate", "target": "n_fallback", "port": ERROR_PORT},
    ],
    [
        {"name": "r", "type": "t.router"},
        {"name": "gate", "type": "t.mark"},
        {"name": "x", "type": "t.mark"},
        {"name": "y", "type": "t.mark"},
        {"name": "side", "type": "t.mark"},
        {"name": "fallback", "type": "t.mark"},
    ],
)


def _marks(ctx: FlowContext) -> list[str]:
    # namespaced run: each node's marks live in its namespace; the list is per node
    return [n for n in ("r", "gate", "x", "y", "side", "fallback") if ctx.get(f"n_{n}") is not None and ctx.get(f"n_{n}").get("marks")]


def test_block_before_halts_everything_downstream_and_takes_the_error_edge():
    probe = Probe(block_before={"n_gate"})
    ctx = execute_blueprint(_graph(*FAN_OUT), _registry(probe), _cfg())
    ran = _marks(ctx)
    assert "gate" not in ran and "x" not in ran and "y" not in ran, ran
    assert "side" in ran, "siblings of the blocked node are not downstream of it"
    assert "fallback" in ran, "the implicit error port routes to the wired fallback"
    assert ctx.metadata.guard_decisions == [
        {
            "node_id": "n_gate", "component": "gate", "skill_type": "t.mark", "phase": "before",
            "verdict": "BLOCK", "rule_id": "T-BLOCK", "message": "blocked by test", "details": {},
        }
    ]
    assert not ctx.metadata.suspended


def test_block_without_an_error_edge_runs_nothing_downstream():
    nodes, edges, comps = FAN_OUT
    # no fallback wired: the error edge and its node go away
    nodes = [n for n in nodes if n["id"] != "n_fallback"]
    edges = [e for e in edges if e.get("port") != ERROR_PORT]
    comps = [c for c in comps if c["name"] != "fallback"]
    probe = Probe(block_before={"n_gate"})
    ctx = execute_blueprint(_graph(nodes, edges, comps), _registry(probe), _cfg())
    assert _marks(ctx) == ["r", "side"] and CALLS == ["r", "side"]
    assert {"x", "y"} <= set(ctx.metadata.skipped_components)


def test_block_after_discards_the_step_outputs():
    probe = Probe(block_after={"n_gate"})
    ctx = execute_blueprint(_graph(*FAN_OUT), _registry(probe), _cfg())
    assert "gate" in CALLS and not ctx.get("n_gate"), "the step ran; its writes were discarded"
    assert "fallback" in _marks(ctx) and "x" not in _marks(ctx)
    assert ctx.metadata.guard_decisions[0]["phase"] == "after"


def test_review_suspends_at_the_node_and_the_ops_decision_resumes_it():
    probe = Probe(review={"n_gate"})
    store = InMemoryRunStore()
    ctx = execute_blueprint_tracked(_graph(*FAN_OUT), _registry(probe), _cfg(), run_store=store)
    assert ctx.metadata.suspended and ctx.metadata.suspended_at_node == "n_gate"
    assert ctx.metadata.suspension_reason == "review:T-REVIEW"
    assert "gate" not in _marks(ctx), "the skill does not run before the review"
    run = store.list_runs()[0]
    assert run.status == RunStatus.SUSPENDED
    resumed = resume_blueprint(
        run.run_id, _registry(probe), _cfg(), resume_data={"review": "allow"}, run_store=store
    )
    assert not resumed.metadata.suspended
    assert {"gate", "x", "y"} <= set(_marks(resumed))
    assert [d["verdict"] for d in resumed.metadata.guard_decisions] == ["REVIEW"]


def test_warn_is_recorded_and_the_step_runs():
    probe = Probe(warn={"n_gate"})
    ctx = execute_blueprint(_graph(*FAN_OUT), _registry(probe), _cfg())
    assert "gate" in _marks(ctx) and "x" in _marks(ctx)
    assert [d["verdict"] for d in ctx.metadata.guard_decisions] == ["WARN"]


def test_invoke_skill_directly_without_a_scope_uses_the_instance_name():
    probe = Probe(block_before={"solo"})
    skill = Mark("solo")
    skill.init({})
    ctx = invoke_skill(skill, FlowContext(), policy=probe.policy())
    assert ctx.get_active_port() == ERROR_PORT and ctx.get("marks") is None
