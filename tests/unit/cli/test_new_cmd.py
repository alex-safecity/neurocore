"""Tests for B1 — `neurocore new` template scaffolding."""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from neurocore.cli.app import app
from neurocore.scaffold.registry import TEMPLATES, list_templates

runner = CliRunner()


def test_new_list_shows_all_templates():
    result = runner.invoke(app, ["new", "--list"])
    assert result.exit_code == 0
    for spec in list_templates():
        assert spec.name in result.output


def test_new_unknown_template_exits_nonzero():
    result = runner.invoke(app, ["new", "does-not-exist", "proj"])
    assert result.exit_code != 0
    assert "Unknown template" in result.output


def test_new_requires_name(tmp_path: Path):
    result = runner.invoke(app, ["new", "ollama-agent"])
    # No name given → usage/error.
    assert result.exit_code != 0


def test_new_scaffolds_and_renders(tmp_path: Path):
    result = runner.invoke(
        app, ["new", "ollama-agent", "local", "--dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    proj = tmp_path / "local"
    assert (proj / "neurocore.yaml").is_file()
    assert (proj / "blueprints" / "chat.flow.yaml").is_file()
    assert (proj / "skills" / "chat.py").is_file()
    # {{ project_name }} rendered
    cfg = (proj / "neurocore.yaml").read_text()
    assert "local" in cfg
    assert "{{ project_name }}" not in cfg
    bp = (proj / "blueprints" / "chat.flow.yaml").read_text()
    assert "local-chat" in bp
    # conventional dirs created
    assert (proj / "data").is_dir()
    assert (proj / "logs").is_dir()


def test_new_refuses_existing_dir(tmp_path: Path):
    (tmp_path / "exists").mkdir()
    result = runner.invoke(
        app, ["new", "ollama-agent", "exists", "--dir", str(tmp_path)]
    )
    assert result.exit_code != 0
    # Rich wraps the message at the terminal width; a long tmp path can split the
    # phrase across lines, so compare on collapsed whitespace.
    assert "already exists" in " ".join(result.output.split())


@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_every_template_scaffolds(template: str, tmp_path: Path):
    result = runner.invoke(app, ["new", template, "proj", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    proj = tmp_path / "proj"
    assert (proj / "neurocore.yaml").is_file()
    assert (proj / "README.md").is_file()
    # Exactly one blueprint per starter template.
    blueprints = list((proj / "blueprints").glob("*.flow.yaml"))
    assert len(blueprints) == 1
    # No unrendered placeholders anywhere.
    for f in proj.rglob("*"):
        if f.is_file():
            assert "{{ project_name }}" not in f.read_text(errors="ignore")


def test_self_contained_template_validates(tmp_path: Path):
    """ollama-agent uses only a bundled skill, so validate should pass."""
    runner.invoke(app, ["new", "ollama-agent", "oa", "--dir", str(tmp_path)])
    proj = tmp_path / "oa"
    result = runner.invoke(
        app, ["validate", str(proj / "blueprints" / "chat.flow.yaml"),
              "--project-root", str(proj)],
    )
    assert result.exit_code == 0, result.output


def test_external_skill_template_validate_fails_gracefully(tmp_path: Path):
    """research-agent references marketplace skills not installed here."""
    runner.invoke(app, ["new", "research-agent", "ra", "--dir", str(tmp_path)])
    proj = tmp_path / "ra"
    result = runner.invoke(
        app, ["validate", str(proj / "blueprints" / "research.flow.yaml"),
              "--project-root", str(proj)],
    )
    # Should fail cleanly (unknown skill), not crash.
    assert result.exit_code != 0
    assert "tavily" in result.output.lower() or "unknown" in result.output.lower()
