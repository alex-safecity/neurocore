"""Tests for NC-002 — Concurrent DAG Execution."""

import asyncio
import time

import pytest
from flowengine import FlowContext

from neurocore import AsyncSkill, SkillMeta
from neurocore.config.schema import NeuroCoreConfig
from neurocore.runtime.blueprint import (
    Blueprint,
    BlueprintComponent,
    FlowDefinition,
    FlowEdge,
    FlowGraph,
)
from neurocore.runtime.executor import execute_blueprint
from neurocore.skills.registry import SkillRegistry


class SlowSkillA(AsyncSkill):
    skill_meta = SkillMeta(name="slow-a", version="0.1.0", provides=["a_done"])

    async def process(self, context: FlowContext) -> FlowContext:
        await asyncio.sleep(0.1)
        context.set("a_done", True)
        return context


class SlowSkillB(AsyncSkill):
    skill_meta = SkillMeta(name="slow-b", version="0.1.0", provides=["b_done"])

    async def process(self, context: FlowContext) -> FlowContext:
        await asyncio.sleep(0.1)
        context.set("b_done", True)
        return context


class MergeSkill(AsyncSkill):
    skill_meta = SkillMeta(name="merge", version="0.1.0", provides=["merged"])

    async def process(self, context: FlowContext) -> FlowContext:
        context.set("merged", True)
        return context


class WriterSkillX(AsyncSkill):
    skill_meta = SkillMeta(name="writer-x", version="0.1.0", provides=["value"])

    async def process(self, context: FlowContext) -> FlowContext:
        context.set("value", "x")
        return context


class WriterSkillY(AsyncSkill):
    skill_meta = SkillMeta(name="writer-y", version="0.1.0", provides=["value"])

    async def process(self, context: FlowContext) -> FlowContext:
        context.set("value", "y")
        return context


def _make_registry(*skills):
    reg = SkillRegistry()
    for s in skills:
        reg.register(s)
    return reg


def test_independent_nodes_run_sequentially_in_topological_order():
    """0.5.0: one executor path (GraphExecutor). Independent roots run one after the
    other in a deterministic order — two 100ms skills take ~200ms, and a sibling
    that blocks halts nothing but its own downstream."""
    registry = _make_registry(SlowSkillA, SlowSkillB)
    # Use a diamond pattern where a and b are independent roots merging into m
    bp2 = Blueprint(
        name="concurrent",
        version="1.0",
        components=[
            BlueprintComponent(name="a", type="slow-a"),
            BlueprintComponent(name="b", type="slow-b"),
            BlueprintComponent(name="m", type="merge"),
        ],
        flow=FlowDefinition(
            type="graph",
            nodes=[
                FlowGraph(id="a", component="a"),
                FlowGraph(id="b", component="b"),
                FlowGraph(id="m", component="m"),
            ],
            edges=[
                FlowEdge(source="a", target="m"),
                FlowEdge(source="b", target="m"),
            ],
        ),
    )
    registry.register(MergeSkill)
    start = time.time()
    result = execute_blueprint(bp2, registry, NeuroCoreConfig())
    elapsed = time.time() - start
    assert result.get("a_done") is True
    assert result.get("b_done") is True
    assert result.get("merged") is True
    assert elapsed >= 0.18  # sequential: ~200ms
    assert result.metadata.completed_nodes == ["a", "b", "m"]


def test_dependent_nodes_run_in_order():
    registry = _make_registry(SlowSkillA, SlowSkillB)
    bp = Blueprint(
        name="sequential-dag",
        version="1.0",
        components=[
            BlueprintComponent(name="a", type="slow-a"),
            BlueprintComponent(name="b", type="slow-b"),
        ],
        flow=FlowDefinition(
            type="graph",
            nodes=[
                FlowGraph(id="a", component="a"),
                FlowGraph(id="b", component="b"),
            ],
            edges=[FlowEdge(source="a", target="b")],
        ),
    )
    start = time.time()
    result = execute_blueprint(bp, registry, NeuroCoreConfig())
    elapsed = time.time() - start
    assert result.get("a_done") is True
    assert result.get("b_done") is True
    assert elapsed >= 0.18  # Should be ~200ms (sequential)


def test_single_node_dag_works():
    registry = _make_registry(SlowSkillA, MergeSkill)
    bp = Blueprint(
        name="single",
        version="1.0",
        components=[
            BlueprintComponent(name="a", type="slow-a"),
            BlueprintComponent(name="m", type="merge"),
        ],
        flow=FlowDefinition(
            type="graph",
            nodes=[
                FlowGraph(id="a", component="a"),
                FlowGraph(id="m", component="m"),
            ],
            edges=[FlowEdge(source="a", target="m")],
        ),
    )
    result = execute_blueprint(bp, registry, NeuroCoreConfig())
    assert result.get("a_done") is True


def test_dag_with_diamond_pattern():
    """A→B, A→C, B→D, C→D"""
    registry = _make_registry(SlowSkillA, SlowSkillB, MergeSkill, WriterSkillX)

    class SkillC(AsyncSkill):
        skill_meta = SkillMeta(name="skill-c", version="0.1.0", provides=["c_done"])

        async def process(self, context: FlowContext) -> FlowContext:
            context.set("c_done", True)
            return context

    class SkillD(AsyncSkill):
        skill_meta = SkillMeta(name="skill-d", version="0.1.0", provides=["d_done"])

        async def process(self, context: FlowContext) -> FlowContext:
            context.set("d_done", True)
            return context

    registry.register(SkillC)
    registry.register(SkillD)

    bp = Blueprint(
        name="diamond",
        version="1.0",
        components=[
            BlueprintComponent(name="a", type="slow-a"),
            BlueprintComponent(name="b", type="slow-b"),
            BlueprintComponent(name="c", type="skill-c"),
            BlueprintComponent(name="d", type="skill-d"),
        ],
        flow=FlowDefinition(
            type="graph",
            nodes=[
                FlowGraph(id="a", component="a"),
                FlowGraph(id="b", component="b"),
                FlowGraph(id="c", component="c"),
                FlowGraph(id="d", component="d"),
            ],
            edges=[
                FlowEdge(source="a", target="b"),
                FlowEdge(source="a", target="c"),
                FlowEdge(source="b", target="d"),
                FlowEdge(source="c", target="d"),
            ],
        ),
    )
    result = execute_blueprint(bp, registry, NeuroCoreConfig())
    assert result.get("a_done") is True
    assert result.get("b_done") is True
    assert result.get("c_done") is True
    assert result.get("d_done") is True


def test_dag_shared_key_last_in_order_wins():
    registry = _make_registry(WriterSkillX, WriterSkillY, MergeSkill)
    bp = Blueprint(
        name="merge-test",
        version="1.0",
        components=[
            BlueprintComponent(name="x", type="writer-x"),
            BlueprintComponent(name="y", type="writer-y"),
            BlueprintComponent(name="m", type="merge"),
        ],
        flow=FlowDefinition(
            type="graph",
            nodes=[
                FlowGraph(id="x", component="x"),
                FlowGraph(id="y", component="y"),
                FlowGraph(id="m", component="m"),
            ],
            edges=[
                FlowEdge(source="x", target="m"),
                FlowEdge(source="y", target="m"),
            ],
        ),
    )
    result = execute_blueprint(bp, registry, NeuroCoreConfig())
    # Both write to "value" on the shared blackboard; topological order makes the
    # last writer deterministic (y). Bindings give each node its own namespace.
    assert result.get("value") == "y"


def test_dag_missing_edges_raises_blueprint_error():
    """Graph flow without edges should raise validation error."""
    with pytest.raises(ValueError, match="Graph flow requires"):
        Blueprint(
            name="no-edges",
            version="1.0",
            components=[BlueprintComponent(name="a", type="slow-a")],
            flow=FlowDefinition(
                type="graph",
                nodes=[FlowGraph(id="a", component="a")],
                edges=[],
            ),
        )


def test_dag_layer_computation_correct_for_linear_chain():
    """A→B→C should produce 3 layers."""
    registry = _make_registry(SlowSkillA, SlowSkillB, MergeSkill)
    bp = Blueprint(
        name="chain",
        version="1.0",
        components=[
            BlueprintComponent(name="a", type="slow-a"),
            BlueprintComponent(name="b", type="slow-b"),
            BlueprintComponent(name="c", type="merge"),
        ],
        flow=FlowDefinition(
            type="graph",
            nodes=[
                FlowGraph(id="a", component="a"),
                FlowGraph(id="b", component="b"),
                FlowGraph(id="c", component="c"),
            ],
            edges=[
                FlowEdge(source="a", target="b"),
                FlowEdge(source="b", target="c"),
            ],
        ),
    )
    result = execute_blueprint(bp, registry, NeuroCoreConfig())
    assert result.get("a_done") is True
    assert result.get("b_done") is True
    assert result.get("merged") is True
