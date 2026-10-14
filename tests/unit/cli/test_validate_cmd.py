"""T14: Tests for `neurocore validate` command."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from neurocore.cli.app import app

runner = CliRunner()


def _create_project_with_echo(tmp_path: Path) -> Path:
    """Create a project with an echo skill."""
    (tmp_path / "neurocore.yaml").write_text("project:\n  name: test\n")

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "echo_skill.py").write_text(
        "from flowengine import FlowContext\n"
        "from neurocore.skills.base import Skill, SkillMeta\n"
        "\n"
        "class EchoSkill(Skill):\n"
        "    skill_meta = SkillMeta(name='echo', version='0.1.0')\n"
        "    def process(self, context: FlowContext) -> FlowContext:\n"
        "        return context\n"
    )
    return tmp_path


class TestValidateCommand:
    def test_valid_blueprint(self, tmp_path: Path):
        _create_project_with_echo(tmp_path)
        bp_file = tmp_path / "flow.yaml"
        bp_file.write_text(
            "name: valid-flow\n"
            "components:\n"
            "  - name: e1\n"
            "    type: echo\n"
            "flow:\n"
            "  type: sequential\n"
            "  steps:\n"
            "    - component: e1\n"
        )
        result = runner.invoke(
            app, ["validate", str(bp_file), "--project-root", str(tmp_path)]
        )
        assert result.exit_code == 0
        assert "Blueprint is valid" in result.output

    def test_shows_parse_ok(self, tmp_path: Path):
        _create_project_with_echo(tmp_path)
        bp_file = tmp_path / "flow.yaml"
        bp_file.write_text(
            "name: test\n"
            "components:\n"
            "  - name: e\n"
            "    type: echo\n"
            "flow:\n"
            "  type: sequential\n"
            "  steps:\n"
            "    - component: e\n"
        )
        result = runner.invoke(
            app, ["validate", str(bp_file), "--project-root", str(tmp_path)]
        )
        assert "YAML parsing OK" in result.output
        assert "Blueprint structure valid" in result.output
        assert "All skill references resolved" in result.output

    def test_shows_blueprint_info(self, tmp_path: Path):
        _create_project_with_echo(tmp_path)
        bp_file = tmp_path / "flow.yaml"
        bp_file.write_text(
            "name: my-flow\n"
            "components:\n"
            "  - name: e1\n"
            "    type: echo\n"
            "  - name: e2\n"
            "    type: echo\n"
            "flow:\n"
            "  type: sequential\n"
            "  steps:\n"
            "    - component: e1\n"
            "    - component: e2\n"
        )
        result = runner.invoke(
            app, ["validate", str(bp_file), "--project-root", str(tmp_path)]
        )
        assert "my-flow" in result.output
        assert "Components: 2" in result.output
        assert "sequential" in result.output

    def test_invalid_yaml_fails(self, tmp_path: Path):
        _create_project_with_echo(tmp_path)
        bp_file = tmp_path / "bad.yaml"
        bp_file.write_text("{{{{invalid")
        result = runner.invoke(
            app, ["validate", str(bp_file), "--project-root", str(tmp_path)]
        )
        assert result.exit_code == 1
        assert "Parse error" in result.output

    def test_missing_components_fails(self, tmp_path: Path):
        _create_project_with_echo(tmp_path)
        bp_file = tmp_path / "incomplete.yaml"
        bp_file.write_text("name: incomplete\n")
        result = runner.invoke(
            app, ["validate", str(bp_file), "--project-root", str(tmp_path)]
        )
        assert result.exit_code == 1

    def test_unknown_skill_reference_fails(self, tmp_path: Path):
        _create_project_with_echo(tmp_path)
        bp_file = tmp_path / "bad-ref.yaml"
        bp_file.write_text(
            "name: bad-ref\n"
            "components:\n"
            "  - name: x\n"
            "    type: nonexistent\n"
            "flow:\n"
            "  type: sequential\n"
            "  steps:\n"
            "    - component: x\n"
        )
        result = runner.invoke(
            app, ["validate", str(bp_file), "--project-root", str(tmp_path)]
        )
        assert result.exit_code == 1
        assert "Skill validation failed" in result.output
        assert "nonexistent" in result.output

    def test_nonexistent_file_fails(self, tmp_path: Path):
        result = runner.invoke(
            app, ["validate", str(tmp_path / "nonexistent.yaml")]
        )
        assert result.exit_code != 0


class TestVersionFlag:
    def test_version_output(self):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "0.5.0" in result.output

    def test_help_output(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "init" in result.output
        assert "run" in result.output
        assert "skill" in result.output
        assert "validate" in result.output
