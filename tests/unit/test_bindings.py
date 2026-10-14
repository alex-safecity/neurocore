"""Bindings pass through the blueprint into flowengine's namespaced mode (0.5.0)."""

from __future__ import annotations

from flowengine import FlowContext

from neurocore import Skill, SkillMeta
from neurocore.config.schema import NeuroCoreConfig, PersistenceConfig
from neurocore.runtime.blueprint import (
    Blueprint,
    BlueprintComponent,
    FlowDefinition,
    FlowEdge,
    FlowGraph,
)
from neurocore.runtime.executor import _build_flow_config, execute_blueprint
from neurocore.skills.registry import SkillRegistry


class Source(Skill):
    skill_meta = SkillMeta(name="t.source", version="0.1.0", provides=["text"])

    def process(self, context: FlowContext) -> FlowContext:
        context.set("text", self.config.get("text", "hello"))
        self.set_output_port(context, "done")
        return context


class Echo(Skill):
    skill_meta = SkillMeta(name="t.echo", version="0.1.0", consumes=["text"], provides=["echo"])

    def process(self, context: FlowContext) -> FlowContext:
        context.set("echo", f"{self.name}:{context.get('text')}")
        return context


def _registry() -> SkillRegistry:
    reg = SkillRegistry()
    reg.register(Source)
    reg.register(Echo)
    return reg


def _bp() -> Blueprint:
    return Blueprint(
        name="bound",
        components=[
            BlueprintComponent(name="src_a", type="t.source", config={"text": "A"}),
            BlueprintComponent(name="src_b", type="t.source", config={"text": "B"}),
            BlueprintComponent(name="echo", type="t.echo"),
        ],
        flow=FlowDefinition(
            type="graph",
            nodes=[
                FlowGraph(id="n_a", component="src_a"),
                FlowGraph(id="n_b", component="src_b", max_visits=3),
                FlowGraph(
                    id="n_e1",
                    component="echo",
                    bind={"inputs": {"text": "n_a.text"}},
                    provenance={"text": "system_of_record"},
                ),
                FlowGraph(id="n_e2", component="echo", bind={"inputs": {"text": "n_b.text"}}),
            ],
            edges=[
                FlowEdge(source="n_a", target="n_b", port="done"),
                FlowEdge(source="n_b", target="n_e1", port="done"),
                FlowEdge(source="n_b", target="n_e2", port="done"),
            ],
        ),
    )


def test_bind_provenance_and_max_visits_reach_the_engine():
    cfg = _build_flow_config(_bp(), {})
    nodes = {n.id: n for n in cfg.flow.nodes}
    assert nodes["n_e1"].bind.inputs == {"text": "n_a.text"}
    assert nodes["n_e1"].provenance == {"text": "system_of_record"}
    assert nodes["n_b"].max_visits == 3
    assert nodes["n_a"].bind is None


def test_two_instances_of_one_skill_keep_separate_namespaces():
    ctx = execute_blueprint(
        _bp(), _registry(), NeuroCoreConfig(persistence=PersistenceConfig(enabled=False))
    )
    assert ctx.get("n_a").text == "A" and ctx.get("n_b").text == "B"
    assert ctx.get("n_e1").echo == "echo:A"
    assert ctx.get("n_e2").echo == "echo:B"
    assert ctx.get("text") == "B"  # unbound sources are still mirrored to raw keys
    assert ctx.get("echo") is None  # bound consumers write only into their namespace
