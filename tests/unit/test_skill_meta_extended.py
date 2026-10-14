"""Extended SkillMeta (0.5.0): ports, risk, effects, typed I/O; strict registries."""

from __future__ import annotations

import pytest
from flowengine import FlowContext

from neurocore import Skill, SkillMeta
from neurocore.errors import SkillError
from neurocore.skills.base import skillmeta_to_componentmeta
from neurocore.skills.registry import SkillRegistry


class Classified(Skill):
    skill_meta = SkillMeta(
        name="t.classified",
        version="0.1.0",
        consumes=["subject_id"],
        provides=["fines"],
        ports=("verified", "unknown"),
        risk_level="high",
        effects=("call_external", "write_db"),
        requires_approval=True,
        cost="medium",
        data_classes=("personal", "financial"),
        inputs={"subject_id": {"type": "string", "required": True}},
        outputs={"fines": {"type": "array"}},
    )

    def process(self, context: FlowContext) -> FlowContext:
        return context


class Unclassified(Skill):
    skill_meta = SkillMeta(name="t.unclassified", version="0.1.0")

    def process(self, context: FlowContext) -> FlowContext:
        return context


def test_maps_onto_component_meta():
    cm = skillmeta_to_componentmeta(Classified.skill_meta)
    assert cm.port_names == ["verified", "unknown"]
    assert cm.risk_level == "high" and cm.requires_approval and cm.cost == "medium"
    assert cm.effects == ["call_external", "write_db"]
    assert cm.data_classes == ["personal", "financial"]
    assert cm.inputs["subject_id"].type == "string" and cm.inputs["subject_id"].required
    assert cm.outputs["fines"].type == "array"
    assert cm.is_safe_for_agents is False


def test_defaults_keep_legacy_skills_working():
    meta = Unclassified.skill_meta
    assert meta.ports == ("done",) and meta.risk_level is None and not meta.declares_risk
    cm = skillmeta_to_componentmeta(meta)
    assert cm.risk_level == "low" and cm.port_names == ["done"] and cm.data_classes == []


def test_lenient_registry_accepts_undeclared_risk():
    reg = SkillRegistry()
    reg.register(Unclassified)
    assert "t.unclassified" in reg


def test_strict_registry_refuses_undeclared_risk():
    reg = SkillRegistry(strict_meta=True)
    reg.register(Classified)
    with pytest.raises(SkillError, match="risk_level is undeclared"):
        reg.register(Unclassified)
    assert "t.unclassified" not in reg
