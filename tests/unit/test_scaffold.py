"""T1: Project scaffold & packaging tests.

Verify that the package installs correctly, imports work,
version is accessible, CLI entry point exists, and the
project structure is complete.
"""

from __future__ import annotations

from pathlib import Path


def test_version_is_string():
    """Package exposes a version string."""
    from neurocore import __version__

    assert isinstance(__version__, str)
    assert __version__ == "0.5.0"


def test_import_neurocore():
    """Top-level package is importable."""
    import neurocore

    assert hasattr(neurocore, "__version__")


def test_import_errors_module():
    """Error types are importable."""
    from neurocore.errors import (
        BlueprintError,
        ConfigError,
        ExecutionError,
        NeuroCoreError,
        SkillError,
    )

    # Verify hierarchy
    assert issubclass(ConfigError, NeuroCoreError)
    assert issubclass(SkillError, NeuroCoreError)
    assert issubclass(BlueprintError, NeuroCoreError)
    assert issubclass(ExecutionError, NeuroCoreError)
    assert issubclass(NeuroCoreError, Exception)


def test_import_subpackages():
    """All sub-packages are importable."""
    import neurocore.config
    import neurocore.logging
    import neurocore.skills
    import neurocore.runtime
    import neurocore.cli
    import neurocore.scaffold


def test_cli_app_importable():
    """CLI Typer app is importable."""
    from neurocore.cli.app import app

    assert app is not None


def test_py_typed_marker_exists():
    """PEP 561 py.typed marker exists."""
    import neurocore

    package_dir = Path(neurocore.__file__).parent
    py_typed = package_dir / "py.typed"
    assert py_typed.exists(), "py.typed marker missing"


def test_scaffold_templates_exist():
    """Scaffold templates are bundled with the package."""
    import neurocore.scaffold

    templates_dir = Path(neurocore.scaffold.__file__).parent / "templates"
    assert templates_dir.is_dir(), "templates/ directory missing"

    expected_files = ["neurocore.yaml", ".env.example", "agent.flow.yaml"]
    for filename in expected_files:
        assert (templates_dir / filename).exists(), f"Template {filename} missing"


def test_error_instances_catchable():
    """Error instances can be raised and caught."""
    from neurocore.errors import ConfigError, NeuroCoreError

    try:
        raise ConfigError("test error")
    except NeuroCoreError as e:
        assert str(e) == "test error"
    else:
        raise AssertionError("ConfigError was not caught by NeuroCoreError")
