"""Skill base class and SkillMeta — the core abstraction of NeuroCore.

A Skill is a FlowEngine BaseComponent enhanced with declarative metadata
(SkillMeta). The metadata enables discovery, validation, documentation,
configuration injection, and dependency tracking.

Usage:
    from neurocore.skills import Skill, SkillMeta

    class EchoSkill(Skill):
        skill_meta = SkillMeta(
            name="echo",
            version="0.1.0",
            description="Echoes input to output",
            provides=["echo_output"],
            consumes=["echo_input"],
        )

        def process(self, context):
            value = context.get("echo_input", "")
            context.set("echo_output", value)
            return context
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

from flowengine import BaseComponent, FlowContext

from neurocore.errors import SkillError


@dataclass(frozen=True)
class SkillMeta:
    """Declarative metadata for a Skill.

    Frozen dataclass — immutable once created. Describes what a skill
    is, what it needs, and what it provides.

    Attributes:
        name: Unique skill identifier (e.g. "neuroweave", "web-search").
        version: Semantic version string (e.g. "0.1.0").
        description: Human-readable description.
        author: Author or maintainer name.
        requires: pip package dependencies (e.g. ["anthropic>=0.42"]).
        provides: Context keys this skill produces.
        consumes: Context keys this skill reads.
        config_schema: JSON Schema dict for validating skill config.
        tags: Categorization tags (e.g. ["memory", "graph", "llm"]).
        ports: Control-out ports the skill may activate (default ``("done",)``).
        risk_level: Worst-case risk (``low``/``medium``/``high``/``critical``).
            ``None`` means undeclared; a registry built with ``strict_meta=True``
            refuses such skills.
        effects: Real-world effects (``flowengine.agent.meta.KNOWN_EFFECTS`` plus
            ``write_db``, ``send_message``, ``call_external``).
        requires_approval: A human must approve before the skill runs.
        cost: Rough cost per call.
        data_classes: Data classes the skill may emit or handle.
        inputs: Typed input specs (``{key: {"type": ..., "required": ...}}``);
            ``consumes`` is kept for compatibility and merged in.
        outputs: Typed output specs; ``provides`` is merged in.
    """

    name: str
    version: str
    description: str = ""
    author: str = ""
    requires: list[str] = field(default_factory=list)
    provides: list[str] = field(default_factory=list)
    consumes: list[str] = field(default_factory=list)
    config_schema: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    requires_llm: bool = False
    max_retries: int = 0
    retry_delay_base: float = 1.0
    retry_delay_max: float = 60.0
    retry_on: tuple[type[BaseException], ...] = field(default_factory=tuple)
    # Extended metadata (0.5.0) — maps 1:1 onto flowengine's ComponentMeta
    ports: tuple[str, ...] = ("done",)
    risk_level: Literal["low", "medium", "high", "critical"] | None = None
    effects: tuple[str, ...] = ()
    requires_approval: bool = False
    cost: Literal["low", "medium", "high"] | None = None
    data_classes: tuple[str, ...] = ()
    inputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def declares_risk(self) -> bool:
        return self.risk_level is not None


class Skill(BaseComponent):
    """Base class for all NeuroCore skills.

    Extends FlowEngine's BaseComponent with:
    - Declarative metadata via `skill_meta` (SkillMeta)
    - Config validation against JSON Schema
    - Health check with initialization guard

    Subclasses MUST:
    1. Define `skill_meta` as a class attribute (SkillMeta instance)
    2. Implement `process(context) -> context`

    Subclasses MAY override:
    - `init(config)` — for one-time setup (call super().init(config))
    - `setup(context)` — per-run preparation
    - `teardown(context)` — per-run cleanup
    - `validate_config()` — additional validation beyond schema
    - `health_check()` — custom health checking

    Lifecycle:
        1. __init__(name)       — instance creation (name defaults to skill_meta.name)
        2. init(config)         — configuration (once)
        3. setup(context)       — pre-processing (each run)
        4. process(context)     — main logic (each run)
        5. teardown(context)    — cleanup (each run)

    Example::

        class GreetSkill(Skill):
            skill_meta = SkillMeta(
                name="greet",
                version="1.0.0",
                description="Greets the user by name",
                provides=["greeting"],
                consumes=["user_name"],
            )

            def process(self, context: FlowContext) -> FlowContext:
                name = context.get("user_name", "World")
                context.set("greeting", f"Hello, {name}!")
                return context
    """

    skill_meta: ClassVar[SkillMeta]

    def get_meta(self) -> Any:
        """Derive a FlowEngine ``ComponentMeta`` from this skill's ``SkillMeta``.

        This is the NeuroCore → FlowEngine bridge: it lets skills participate in
        FlowEngine's agent-native tooling (semantic validation, the component
        catalog, planning) using the same metadata they already declare. The
        mapping is::

            SkillMeta.provides     -> ComponentMeta.outputs (keys)
            SkillMeta.consumes     -> ComponentMeta.inputs  (keys)
            SkillMeta.tags         -> ComponentMeta.tags
            SkillMeta.config_schema-> ComponentMeta.config_schema
            SkillMeta.requires_llm -> ComponentMeta.requires_llm

        Returns ``None`` if FlowEngine is too old to expose ``ComponentMeta``
        (i.e. it pre-dates the v0.5.0 agent API).
        """
        return skillmeta_to_componentmeta(self._get_meta())

    def __init__(self, name: str | None = None) -> None:
        """Initialize skill with a name.

        Args:
            name: Component name. Defaults to skill_meta.name if not provided.

        Raises:
            SkillError: If the subclass hasn't defined skill_meta.
        """
        meta = self._get_meta()
        component_name = name or meta.name
        super().__init__(component_name)
        self.llm: Any = None  # injected by executor if requires_llm=True

    def _get_meta(self) -> SkillMeta:
        """Retrieve skill_meta, raising SkillError if not defined.

        Returns:
            The SkillMeta instance.

        Raises:
            SkillError: If skill_meta is not defined on the subclass.
        """
        meta = getattr(self.__class__, "skill_meta", None)
        if meta is None:
            raise SkillError(
                f"{self.__class__.__name__} must define 'skill_meta' as a class attribute. "
                f"Example: skill_meta = SkillMeta(name='my-skill', version='0.1.0')"
            )
        if not isinstance(meta, SkillMeta):
            raise SkillError(
                f"{self.__class__.__name__}.skill_meta must be a SkillMeta instance, "
                f"got {type(meta).__name__}"
            )
        return meta

    def validate_config(self) -> list[str]:
        """Validate component configuration.

        Checks required keys from config_schema if provided.
        Subclasses can override to add custom validation.

        Returns:
            List of validation error messages (empty if valid).
        """
        errors: list[str] = []
        schema = self.skill_meta.config_schema

        # Check required fields from JSON Schema
        required = schema.get("required", [])
        properties = schema.get("properties", {})

        for key in required:
            if key not in self.config:
                errors.append(f"Missing required config key: '{key}'")

        # Check type constraints for provided values
        for key, value in self.config.items():
            if key in properties:
                expected_type = properties[key].get("type")
                if expected_type and not _check_json_type(value, expected_type):
                    errors.append(
                        f"Config key '{key}': expected type '{expected_type}', "
                        f"got '{type(value).__name__}'"
                    )

        return errors

    def health_check(self) -> bool:
        """Check if skill is healthy.

        Returns True if the skill has been initialized.
        Subclasses can override for custom health checks
        (e.g., checking external service connectivity).

        Returns:
            True if skill is operational.
        """
        return self.is_initialized

    def __repr__(self) -> str:
        meta = getattr(self.__class__, "skill_meta", None)
        if meta:
            return f"{self.__class__.__name__}(name={self.name!r}, version={meta.version!r})"
        return f"{self.__class__.__name__}(name={self.name!r})"


def skillmeta_to_componentmeta(meta: SkillMeta) -> Any:
    """Map a :class:`SkillMeta` to a FlowEngine ``ComponentMeta``.

    Returns ``None`` if the installed FlowEngine predates the agent-native API
    (``flowengine.agent.meta`` / v0.5.0), so callers degrade gracefully.
    """
    try:
        from flowengine.agent.meta import ComponentMeta, IOFieldSpec, PortSpec
    except ImportError:
        return None

    inputs = {key: IOFieldSpec(type="any") for key in meta.consumes}
    inputs.update({key: IOFieldSpec(**spec) for key, spec in meta.inputs.items()})
    outputs = {key: IOFieldSpec(type="any") for key in meta.provides}
    outputs.update({key: IOFieldSpec(**spec) for key, spec in meta.outputs.items()})
    return ComponentMeta(
        name=meta.name,
        description=meta.description,
        version=meta.version,
        inputs=inputs,
        outputs=outputs,
        ports=[PortSpec(name=p) for p in meta.ports],
        tags=list(meta.tags),
        config_schema=dict(meta.config_schema),
        cost=meta.cost,
        risk_level=meta.risk_level or "low",
        effects=list(meta.effects),
        requires_approval=meta.requires_approval,
        requires_llm=meta.requires_llm,
        data_classes=list(meta.data_classes),
    )


def _check_json_type(value: Any, json_type: str) -> bool:
    """Check if a Python value matches a JSON Schema type.

    Args:
        value: The Python value to check.
        json_type: JSON Schema type string.

    Returns:
        True if the value matches the type.
    """
    type_map: dict[str, type | tuple[type, ...]] = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    expected = type_map.get(json_type)
    if expected is None:
        return True  # Unknown type — don't reject
    # bool is subclass of int in Python, handle explicitly
    if json_type == "integer" and isinstance(value, bool):
        return False
    return isinstance(value, expected)


class AsyncSkill(Skill):
    """Skill whose process() is an async coroutine.

    Subclass this instead of Skill when your process() needs to await.
    The executor detects coroutines automatically via inspect.iscoroutinefunction.
    """

    async def process(self, context: FlowContext) -> FlowContext:  # type: ignore[override]
        raise NotImplementedError


def is_async_skill(skill_instance: Skill) -> bool:
    """Return True if the skill's process() is a coroutine function."""
    return inspect.iscoroutinefunction(skill_instance.process)
