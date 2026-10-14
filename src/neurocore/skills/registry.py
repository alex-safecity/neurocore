"""Skill registry — holds discovered skill classes and creates instances.

The SkillRegistry is the central index of all available skills. It is
populated by the discovery mechanisms (directory scan + entry points)
and used by the runtime to instantiate skills referenced in blueprints.

Usage:
    from neurocore.skills.registry import SkillRegistry

    registry = SkillRegistry()
    registry.register(EchoSkill)
    skill_cls = registry.get("echo")
    all_skills = registry.list_skills()
"""

from __future__ import annotations

from typing import Any

from neurocore.errors import SkillError
from neurocore.skills.base import Skill, SkillMeta


class SkillRegistry:
    """Registry of discovered skill classes.

    Skills are keyed by their `skill_meta.name`. The registry enforces
    uniqueness — registering a second skill with the same name raises
    SkillError unless `replace=True` is passed (used for entry point
    precedence over directory scan).

    Attributes:
        _skills: Mapping from skill name to skill class.
    """

    def __init__(self, *, strict_meta: bool = False) -> None:
        """
        Args:
            strict_meta: Refuse skills that do not declare ``risk_level`` (a
                governed catalogue needs every component classified).
        """
        self._skills: dict[str, type[Skill]] = {}
        self.strict_meta = strict_meta

    def register(self, skill_class: type[Skill], *, replace: bool = False) -> None:
        """Register a skill class.

        Args:
            skill_class: A Skill subclass with a valid skill_meta.
            replace: If True, overwrites existing registration
                     (used for entry point precedence).

        Raises:
            SkillError: If skill_class is not a valid Skill subclass,
                        or if a skill with the same name is already registered
                        and replace is False.
        """
        # Validate it's actually a Skill subclass
        if not isinstance(skill_class, type) or not issubclass(skill_class, Skill):
            raise SkillError(
                f"Cannot register {skill_class!r}: not a Skill subclass"
            )

        # Validate it has skill_meta
        meta = getattr(skill_class, "skill_meta", None)
        if not isinstance(meta, SkillMeta):
            raise SkillError(
                f"Cannot register {skill_class.__name__}: missing or invalid skill_meta"
            )

        name = meta.name

        if self.strict_meta and not meta.declares_risk:
            raise SkillError(
                f"Cannot register {skill_class.__name__}: skill_meta.risk_level is "
                f"undeclared and this registry requires it (strict_meta=True)"
            )

        if name in self._skills and not replace:
            existing = self._skills[name]
            raise SkillError(
                f"Skill '{name}' already registered by {existing.__name__}. "
                f"Cannot register {skill_class.__name__} with the same name. "
                f"Use replace=True to override."
            )

        self._skills[name] = skill_class

    def get(self, name: str) -> type[Skill] | None:
        """Get a registered skill class by name.

        Args:
            name: The skill's registered name (from skill_meta.name).

        Returns:
            The Skill subclass, or None if not found.
        """
        return self._skills.get(name)

    def get_or_raise(self, name: str) -> type[Skill]:
        """Get a registered skill class by name, or raise.

        Args:
            name: The skill's registered name.

        Returns:
            The Skill subclass.

        Raises:
            SkillError: If no skill is registered with this name.
        """
        cls = self._skills.get(name)
        if cls is None:
            available = ", ".join(sorted(self._skills.keys())) or "(none)"
            raise SkillError(
                f"Skill '{name}' not found. Available skills: {available}"
            )
        return cls

    def list_skills(self) -> list[str]:
        """List all registered skill names, sorted alphabetically.

        Returns:
            Sorted list of skill names.
        """
        return sorted(self._skills.keys())

    def list_skill_metas(self) -> list[SkillMeta]:
        """List metadata for all registered skills.

        Returns:
            List of SkillMeta instances, sorted by name.
        """
        return [
            cls.skill_meta
            for cls in sorted(self._skills.values(), key=lambda c: c.skill_meta.name)
        ]

    def component_catalog(self) -> list[dict[str, Any]]:
        """Return a FlowEngine-style component catalog for all registered skills.

        Each entry is the skill's :class:`SkillMeta` mapped to a FlowEngine
        ``ComponentMeta`` catalog entry (inputs/outputs/tags/risk/...), so
        NeuroCore skills surface alongside native FlowEngine components for
        agent-driven flow composition. Skills are included by their registered
        name (the ``type`` an agent uses in YAML). Returns an empty list if the
        installed FlowEngine predates the v0.5.0 agent API.
        """
        from neurocore.skills.base import skillmeta_to_componentmeta

        catalog: list[dict[str, Any]] = []
        for name in self.list_skills():
            cmeta = skillmeta_to_componentmeta(self._skills[name].skill_meta)
            if cmeta is None:
                return []
            entry = cmeta.to_catalog_entry()
            entry["type"] = name
            catalog.append(entry)
        return catalog

    def create(self, name: str, *, instance_name: str | None = None) -> Skill:
        """Create an instance of a registered skill.

        Args:
            name: The skill's registered name.
            instance_name: Optional name for the instance
                          (defaults to skill_meta.name).

        Returns:
            A new Skill instance.

        Raises:
            SkillError: If no skill is registered with this name.
        """
        cls = self.get_or_raise(name)
        return cls(name=instance_name)

    def __len__(self) -> int:
        return len(self._skills)

    def __contains__(self, name: str) -> bool:
        return name in self._skills

    def __repr__(self) -> str:
        names = ", ".join(self.list_skills())
        return f"SkillRegistry([{names}])"
