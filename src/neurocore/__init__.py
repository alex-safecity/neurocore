"""NeuroCore: Pluggable, YAML-driven framework for building agentic AI applications.

NeuroCore is the chassis for agentic AI — it wires together:
- FlowEngine for workflow orchestration (DAG, sequential, conditional, cyclic)
- Skills as discoverable, configurable components with metadata
- YAML-based configuration with environment variable overlay
- A CLI for scaffolding, running, and inspecting projects

Example::

    from neurocore import Skill, SkillMeta

    class MySkill(Skill):
        skill_meta = SkillMeta(
            name="my-skill",
            version="0.1.0",
            description="A custom skill",
        )

        def process(self, context):
            context.set("result", "hello from my skill")
            return context
"""

__version__ = "0.5.0"

from neurocore.runtime.events import FlowEvent, FlowEventType
from neurocore.skills.base import AsyncSkill, Skill, SkillMeta, is_async_skill

__all__ = [
    "__version__",
    "AsyncSkill",
    "FlowEvent",
    "FlowEventType",
    "Skill",
    "SkillMeta",
    "is_async_skill",
]
