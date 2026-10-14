"""T9+T10: Executor and config merge tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from flowengine import FlowContext

from neurocore.config.schema import NeuroCoreConfig
from neurocore.errors import BlueprintError, ExecutionError
from neurocore.runtime.blueprint import (
    Blueprint,
    BlueprintComponent,
    FlowDefinition,
    FlowStep,
)
from neurocore.runtime.executor import (
    _build_flow_config,
    _create_skill_instances,
    execute_blueprint,
    load_and_run,
    merge_skill_config,
)
from neurocore.skills.base import Skill, SkillMeta
from neurocore.skills.registry import SkillRegistry


# --- Test skill classes ---


class EchoSkill(Skill):
    """Simple pass-through skill."""

    skill_meta = SkillMeta(name="echo", version="0.1.0")

    def process(self, context: FlowContext) -> FlowContext:
        msg = context.get("input", "")
        context.set("echo_output", msg)
        return context


class UpperSkill(Skill):
    """Uppercases the echo_output."""

    skill_meta = SkillMeta(name="upper", version="0.1.0")

    def process(self, context: FlowContext) -> FlowContext:
        val = context.get("echo_output", "")
        context.set("upper_output", val.upper())
        return context


class ConfigAwareSkill(Skill):
    """Skill that reads config values and puts them in context."""

    skill_meta = SkillMeta(
        name="config-aware",
        version="0.1.0",
        config_schema={
            "properties": {
                "greeting": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["greeting"],
        },
    )

    def process(self, context: FlowContext) -> FlowContext:
        context.set("greeting", self.config.get("greeting", ""))
        context.set("count", self.config.get("count", 0))
        return context


class FailingSkill(Skill):
    """Skill whose process always raises."""

    skill_meta = SkillMeta(name="failing", version="0.1.0")

    def process(self, context: FlowContext) -> FlowContext:
        raise RuntimeError("Intentional failure")


# --- Fixtures ---


@pytest.fixture
def registry() -> SkillRegistry:
    reg = SkillRegistry()
    reg.register(EchoSkill)
    reg.register(UpperSkill)
    reg.register(ConfigAwareSkill)
    reg.register(FailingSkill)
    return reg


@pytest.fixture
def neurocore_config(tmp_path: Path) -> NeuroCoreConfig:
    return NeuroCoreConfig(project_root=tmp_path)


@pytest.fixture
def neurocore_config_with_skills(tmp_path: Path) -> NeuroCoreConfig:
    """Config that has skill-level settings in neurocore.yaml."""
    return NeuroCoreConfig(
        project_root=tmp_path,
        skills={
            "echo": {"message": "from-yaml", "timeout": 30},
            "config-aware": {"greeting": "yaml-hello", "extra": "yaml-extra"},
        },
    )


def _make_blueprint(
    components: list[BlueprintComponent],
    steps: list[FlowStep] | None = None,
) -> Blueprint:
    """Helper to create a simple sequential blueprint."""
    if steps is None:
        steps = [FlowStep(component=c.name) for c in components]
    return Blueprint(
        name="test-blueprint",
        components=components,
        flow=FlowDefinition(type="sequential", steps=steps),
    )


# --- merge_skill_config tests ---


class TestMergeSkillConfig:
    def test_empty_both(self, neurocore_config: NeuroCoreConfig):
        result = merge_skill_config(neurocore_config, "echo", {})
        assert result == {}

    def test_only_yaml_config(self, neurocore_config_with_skills: NeuroCoreConfig):
        result = merge_skill_config(neurocore_config_with_skills, "echo", {})
        assert result == {"message": "from-yaml", "timeout": 30}

    def test_only_blueprint_config(self, neurocore_config: NeuroCoreConfig):
        result = merge_skill_config(
            neurocore_config, "echo", {"message": "from-blueprint"}
        )
        assert result == {"message": "from-blueprint"}

    def test_blueprint_overrides_yaml(
        self, neurocore_config_with_skills: NeuroCoreConfig
    ):
        result = merge_skill_config(
            neurocore_config_with_skills, "echo", {"message": "blueprint-wins"}
        )
        assert result["message"] == "blueprint-wins"
        assert result["timeout"] == 30  # yaml value preserved

    def test_merge_disjoint_keys(
        self, neurocore_config_with_skills: NeuroCoreConfig
    ):
        result = merge_skill_config(
            neurocore_config_with_skills,
            "echo",
            {"new_key": "new_val"},
        )
        assert result == {"message": "from-yaml", "timeout": 30, "new_key": "new_val"}

    def test_unknown_skill_returns_blueprint_only(
        self, neurocore_config: NeuroCoreConfig
    ):
        result = merge_skill_config(
            neurocore_config, "nonexistent", {"key": "val"}
        )
        assert result == {"key": "val"}


# --- _create_skill_instances tests ---


class TestCreateSkillInstances:
    def test_creates_single_instance(
        self, registry: SkillRegistry, neurocore_config: NeuroCoreConfig
    ):
        bp = _make_blueprint(
            [BlueprintComponent(name="my-echo", type="echo")]
        )
        instances, configs = _create_skill_instances(bp, registry, neurocore_config)
        assert "my-echo" in instances
        assert isinstance(instances["my-echo"].inner, EchoSkill)  # guarded (0.5.0)
        assert instances["my-echo"].name == "my-echo"

    def test_creates_multiple_instances(
        self, registry: SkillRegistry, neurocore_config: NeuroCoreConfig
    ):
        bp = _make_blueprint([
            BlueprintComponent(name="e1", type="echo"),
            BlueprintComponent(name="u1", type="upper"),
        ])
        instances, configs = _create_skill_instances(bp, registry, neurocore_config)
        assert len(instances) == 2
        assert isinstance(instances["e1"].inner, EchoSkill)  # guarded (0.5.0)
        assert isinstance(instances["u1"].inner, UpperSkill)

    def test_instances_are_initialized(
        self, registry: SkillRegistry, neurocore_config: NeuroCoreConfig
    ):
        bp = _make_blueprint(
            [BlueprintComponent(name="e", type="echo")]
        )
        instances, configs = _create_skill_instances(bp, registry, neurocore_config)
        assert instances["e"].is_initialized

    def test_config_merged_into_instance(
        self,
        registry: SkillRegistry,
        neurocore_config_with_skills: NeuroCoreConfig,
    ):
        bp = _make_blueprint([
            BlueprintComponent(
                name="e", type="echo", config={"extra": "bp-val"}
            )
        ])
        instances, configs = _create_skill_instances(
            bp, registry, neurocore_config_with_skills
        )
        # yaml: message=from-yaml, timeout=30
        # blueprint: extra=bp-val
        assert instances["e"].config["message"] == "from-yaml"
        assert instances["e"].config["timeout"] == 30
        assert instances["e"].config["extra"] == "bp-val"

    def test_merged_configs_returned(
        self,
        registry: SkillRegistry,
        neurocore_config_with_skills: NeuroCoreConfig,
    ):
        bp = _make_blueprint([
            BlueprintComponent(
                name="e", type="echo", config={"extra": "bp-val"}
            )
        ])
        instances, configs = _create_skill_instances(
            bp, registry, neurocore_config_with_skills
        )
        assert "e" in configs
        assert configs["e"]["message"] == "from-yaml"
        assert configs["e"]["extra"] == "bp-val"

    def test_unknown_skill_raises(
        self, registry: SkillRegistry, neurocore_config: NeuroCoreConfig
    ):
        bp = _make_blueprint(
            [BlueprintComponent(name="x", type="nonexistent")]
        )
        with pytest.raises(BlueprintError, match="unknown skill.*nonexistent"):
            _create_skill_instances(bp, registry, neurocore_config)

    def test_config_validation_failure_raises(
        self, registry: SkillRegistry, neurocore_config: NeuroCoreConfig
    ):
        """config-aware skill requires 'greeting' — omitting it should raise."""
        bp = _make_blueprint(
            [BlueprintComponent(name="ca", type="config-aware", config={})]
        )
        with pytest.raises(BlueprintError, match="config validation failed"):
            _create_skill_instances(bp, registry, neurocore_config)

    def test_config_validation_passes_with_required(
        self, registry: SkillRegistry, neurocore_config: NeuroCoreConfig
    ):
        bp = _make_blueprint([
            BlueprintComponent(
                name="ca", type="config-aware", config={"greeting": "hi"}
            )
        ])
        instances, configs = _create_skill_instances(bp, registry, neurocore_config)
        assert instances["ca"].config["greeting"] == "hi"


# --- _build_flow_config tests ---


class TestBuildFlowConfig:
    def test_sequential_flow(self):
        bp = _make_blueprint([
            BlueprintComponent(name="a", type="echo"),
            BlueprintComponent(name="b", type="upper"),
        ])
        fc = _build_flow_config(bp)
        assert fc.name == "test-blueprint"
        assert len(fc.components) == 2
        assert fc.components[0].name == "a"
        assert fc.components[0].type == "neurocore.skills.echo"
        assert fc.components[1].name == "b"
        assert fc.flow.type == "sequential"
        assert len(fc.flow.steps) == 2

    def test_component_config_passthrough(self):
        bp = _make_blueprint([
            BlueprintComponent(
                name="a", type="echo", config={"key": "val"}
            )
        ])
        fc = _build_flow_config(bp)
        assert fc.components[0].config == {"key": "val"}

    def test_graph_flow(self):
        bp = Blueprint(
            name="graph-test",
            components=[
                BlueprintComponent(name="a", type="echo"),
                BlueprintComponent(name="b", type="upper"),
            ],
            flow=FlowDefinition(
                type="graph",
                nodes=[
                    {"id": "n1", "component": "a"},
                    {"id": "n2", "component": "b"},
                ],
                edges=[{"source": "n1", "target": "n2"}],
            ),
        )
        fc = _build_flow_config(bp)
        assert fc.flow.type == "graph"
        assert len(fc.flow.nodes) == 2
        assert len(fc.flow.edges) == 1

    def test_preserves_version_and_description(self):
        bp = Blueprint(
            name="versioned",
            version="2.5",
            description="A test flow",
            components=[BlueprintComponent(name="a", type="echo")],
            flow=FlowDefinition(steps=[FlowStep(component="a")]),
        )
        fc = _build_flow_config(bp)
        assert fc.version == "2.5"
        assert fc.description == "A test flow"

    def test_step_properties_preserved(self):
        bp = Blueprint(
            name="test",
            components=[BlueprintComponent(name="a", type="echo")],
            flow=FlowDefinition(
                steps=[
                    FlowStep(
                        component="a",
                        description="Do echo",
                        condition="context.get('run')",
                        on_error="skip",
                    )
                ]
            ),
        )
        fc = _build_flow_config(bp)
        step = fc.flow.steps[0]
        assert step.component == "a"
        assert step.description == "Do echo"
        assert step.condition == "context.get('run')"
        assert step.on_error == "skip"


# --- execute_blueprint tests ---


class TestExecuteBlueprint:
    def test_simple_sequential(
        self, registry: SkillRegistry, neurocore_config: NeuroCoreConfig
    ):
        bp = _make_blueprint([
            BlueprintComponent(name="e1", type="echo"),
        ])
        result = execute_blueprint(
            bp, registry, neurocore_config,
            initial_data={"input": "hello"},
        )
        assert result.get("echo_output") == "hello"

    def test_two_step_pipeline(
        self, registry: SkillRegistry, neurocore_config: NeuroCoreConfig
    ):
        bp = _make_blueprint([
            BlueprintComponent(name="e1", type="echo"),
            BlueprintComponent(name="u1", type="upper"),
        ])
        result = execute_blueprint(
            bp, registry, neurocore_config,
            initial_data={"input": "hello"},
        )
        assert result.get("echo_output") == "hello"
        assert result.get("upper_output") == "HELLO"

    def test_initial_data_in_context(
        self, registry: SkillRegistry, neurocore_config: NeuroCoreConfig
    ):
        bp = _make_blueprint([
            BlueprintComponent(name="e1", type="echo"),
        ])
        result = execute_blueprint(
            bp, registry, neurocore_config,
            initial_data={"input": "test_val", "extra": 42},
        )
        assert result.get("input") == "test_val"
        assert result.get("extra") == 42

    def test_no_initial_data(
        self, registry: SkillRegistry, neurocore_config: NeuroCoreConfig
    ):
        bp = _make_blueprint([
            BlueprintComponent(name="e1", type="echo"),
        ])
        result = execute_blueprint(bp, registry, neurocore_config)
        assert result.get("echo_output") == ""

    def test_validation_failure_raises_blueprint_error(
        self, registry: SkillRegistry, neurocore_config: NeuroCoreConfig
    ):
        bp = _make_blueprint(
            [BlueprintComponent(name="x", type="nonexistent")]
        )
        with pytest.raises(BlueprintError, match="validation failed"):
            execute_blueprint(bp, registry, neurocore_config)

    def test_execution_failure_raises_execution_error(
        self, registry: SkillRegistry, neurocore_config: NeuroCoreConfig
    ):
        bp = _make_blueprint([
            BlueprintComponent(name="f1", type="failing"),
        ])
        with pytest.raises(ExecutionError, match="execution failed"):
            execute_blueprint(bp, registry, neurocore_config)

    def test_config_merge_during_execution(
        self,
        registry: SkillRegistry,
        neurocore_config_with_skills: NeuroCoreConfig,
    ):
        """Verify config-aware skill gets merged config from yaml + blueprint."""
        bp = _make_blueprint([
            BlueprintComponent(
                name="ca",
                type="config-aware",
                config={"greeting": "bp-hello", "count": 5},
            )
        ])
        result = execute_blueprint(
            bp, registry, neurocore_config_with_skills,
        )
        # Blueprint overrides yaml's greeting; count comes from blueprint
        assert result.get("greeting") == "bp-hello"
        assert result.get("count") == 5

    def test_yaml_config_used_when_no_blueprint_override(
        self,
        registry: SkillRegistry,
        neurocore_config_with_skills: NeuroCoreConfig,
    ):
        """Skill gets greeting from neurocore.yaml when blueprint doesn't override."""
        bp = _make_blueprint([
            BlueprintComponent(name="ca", type="config-aware", config={})
        ])
        result = execute_blueprint(
            bp, registry, neurocore_config_with_skills,
        )
        # config-aware yaml config: greeting=yaml-hello, extra=yaml-extra
        assert result.get("greeting") == "yaml-hello"

    def test_returns_flow_context(
        self, registry: SkillRegistry, neurocore_config: NeuroCoreConfig
    ):
        bp = _make_blueprint([
            BlueprintComponent(name="e1", type="echo"),
        ])
        result = execute_blueprint(bp, registry, neurocore_config)
        assert isinstance(result, FlowContext)


# --- load_and_run tests ---


class TestLoadAndRun:
    def test_full_pipeline(self, tmp_path: Path):
        """End-to-end: write YAML blueprint, load_and_run, check output."""
        # Create a minimal neurocore.yaml
        nc_yaml = tmp_path / "neurocore.yaml"
        nc_yaml.write_text("project:\n  name: test\n")

        # Create a blueprint file
        bp_file = tmp_path / "flow.yaml"
        bp_file.write_text(
            "name: e2e-test\n"
            "components:\n"
            "  - name: e1\n"
            "    type: echo\n"
            "flow:\n"
            "  type: sequential\n"
            "  steps:\n"
            "    - component: e1\n"
        )

        # Create skills dir with an echo skill
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "echo_skill.py").write_text(
            "from flowengine import FlowContext\n"
            "from neurocore.skills.base import Skill, SkillMeta\n"
            "\n"
            "class EchoSkill(Skill):\n"
            "    skill_meta = SkillMeta(name='echo', version='0.1.0')\n"
            "    def process(self, context: FlowContext) -> FlowContext:\n"
            "        context.set('echo_output', context.get('input', ''))\n"
            "        return context\n"
        )

        result = load_and_run(
            bp_file,
            project_root=tmp_path,
            initial_data={"input": "e2e"},
        )
        assert result.get("echo_output") == "e2e"

    def test_missing_blueprint_raises(self, tmp_path: Path):
        nc_yaml = tmp_path / "neurocore.yaml"
        nc_yaml.write_text("project:\n  name: test\n")

        with pytest.raises(BlueprintError, match="not found"):
            load_and_run(
                tmp_path / "nonexistent.yaml",
                project_root=tmp_path,
            )

    def test_unresolved_skill_raises(self, tmp_path: Path):
        """Blueprint references a skill not in the skills dir."""
        nc_yaml = tmp_path / "neurocore.yaml"
        nc_yaml.write_text("project:\n  name: test\n")

        # Create skills dir (empty)
        (tmp_path / "skills").mkdir()

        bp_file = tmp_path / "flow.yaml"
        bp_file.write_text(
            "name: bad-test\n"
            "components:\n"
            "  - name: x\n"
            "    type: nonexistent\n"
            "flow:\n"
            "  type: sequential\n"
            "  steps:\n"
            "    - component: x\n"
        )

        with pytest.raises(BlueprintError, match="validation failed"):
            load_and_run(bp_file, project_root=tmp_path)


# --- Edge cases ---


class TestEdgeCases:
    def test_same_skill_type_different_instances(
        self, registry: SkillRegistry, neurocore_config: NeuroCoreConfig
    ):
        """Two components can use the same skill type with different names."""
        bp = _make_blueprint([
            BlueprintComponent(name="echo1", type="echo"),
            BlueprintComponent(name="echo2", type="echo"),
        ])
        result = execute_blueprint(
            bp, registry, neurocore_config,
            initial_data={"input": "test"},
        )
        # Both echos run sequentially; second overwrites first's output
        assert result.get("echo_output") == "test"

    def test_same_skill_different_config(
        self,
        registry: SkillRegistry,
        neurocore_config: NeuroCoreConfig,
    ):
        """Two instances of the same skill with different blueprint configs."""
        bp = _make_blueprint([
            BlueprintComponent(
                name="ca1", type="config-aware", config={"greeting": "hi"}
            ),
            BlueprintComponent(
                name="ca2", type="config-aware", config={"greeting": "bye"}
            ),
        ])
        # Second will overwrite context keys but both should initialize
        result = execute_blueprint(bp, registry, neurocore_config)
        assert result.get("greeting") == "bye"

    def test_empty_initial_data(
        self, registry: SkillRegistry, neurocore_config: NeuroCoreConfig
    ):
        bp = _make_blueprint([
            BlueprintComponent(name="e1", type="echo"),
        ])
        result = execute_blueprint(
            bp, registry, neurocore_config, initial_data={}
        )
        assert result.get("echo_output") == ""

    def test_blueprint_with_flow_settings(
        self, registry: SkillRegistry, neurocore_config: NeuroCoreConfig
    ):
        """FlowDefinition settings are passed through to FlowEngine."""
        bp = Blueprint(
            name="with-settings",
            components=[BlueprintComponent(name="e", type="echo")],
            flow=FlowDefinition(
                steps=[FlowStep(component="e")],
                settings={"fail_fast": True},
            ),
        )
        # Should not raise — settings are valid
        result = execute_blueprint(
            bp, registry, neurocore_config,
            initial_data={"input": "ok"},
        )
        assert result.get("echo_output") == "ok"
