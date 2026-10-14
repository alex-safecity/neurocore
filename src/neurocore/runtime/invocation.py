"""The guarded invocation contract — one function every skill call goes through.

Two wrap points, one contract (moi-v2 D54):

* **Registry wrap point** — when the executor instantiates a component the registry
  returns it wrapped in :class:`Guarded`, whose ``process`` is :func:`invoke_skill`.
  Every execution path (FlowEngine sequential, GraphExecutor sync and async, streaming)
  therefore passes through the contract without knowing it; sub-flows and loop bodies
  resolve through the same registry and are covered the same way.
* **Executor wrap point** — flowengine's ``GraphExecutor`` applies data bindings
  (``BoundComponent``) around the guarded skill, so the guard evaluates on the node's
  scoped context: the inputs the skill will actually read, under the node id.

What :func:`invoke_skill` does for one step::

    uow = policy.unit_of_work(context, node_id, skill)      # per-step unit of work (D55)
    before = policy.guard.before(skill, context, node_id)   # destination, effects, data
                                                            # classes, provenance, budgets,
                                                            # kill switches
    BLOCK  -> record, set the implicit ``error`` port, skill does not run
    REVIEW -> record, suspend at the node (the runtime parks it; resume re-evaluates)
    ALLOW / WARN -> run the skill
    after  = policy.guard.after(skill, context, node_id)    # schema, size, truth gate
    BLOCK  -> record, discard the step's writes (namespaced mode), ``error`` port
    commit on success, rollback on any exception

Verdicts are recorded in ``context.metadata.guard_decisions`` and therefore travel with
the run record. The default policy allows everything and opens a no-op unit of work;
the MOI runtime installs its own guard and SQL unit of work on the registry.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from flowengine import BaseComponent, FlowContext
from flowengine.core.binding import ScopedContext, ScopedData
from flowengine.core.graph import ERROR_PORT  # the port a blocked step routes on

from neurocore.skills.base import Skill, SkillMeta, is_async_skill

Verdict = Literal["ALLOW", "WARN", "BLOCK", "REVIEW"]
Phase = Literal["before", "after"]



@dataclass(frozen=True)
class GuardDecision:
    """One verdict of the skill guard."""

    verdict: Verdict = "ALLOW"
    rule_id: str | None = None
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_record(self, node_id: str, skill: Skill, phase: Phase) -> dict[str, Any]:
        return {
            "node_id": node_id,
            "component": skill.name,
            "skill_type": skill.skill_meta.name,
            "phase": phase,
            "verdict": self.verdict,
            "rule_id": self.rule_id,
            "message": self.message,
            "details": dict(self.details),
        }


ALLOW = GuardDecision("ALLOW")


class SkillGuard(Protocol):
    """Evaluated before and after every skill call."""

    def before(self, skill: Skill, context: FlowContext, node_id: str) -> GuardDecision: ...

    def after(self, skill: Skill, context: FlowContext, node_id: str) -> GuardDecision: ...


class AllowAll:
    """The default guard: every call is allowed."""

    def before(self, skill: Skill, context: FlowContext, node_id: str) -> GuardDecision:
        return ALLOW

    def after(self, skill: Skill, context: FlowContext, node_id: str) -> GuardDecision:
        return ALLOW


class UnitOfWork(Protocol):
    """What one step's writers share (checkpoint, ledger, audit, timers, envelope)."""

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class NoopUnitOfWork:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


UnitOfWorkFactory = Callable[[FlowContext, str, Skill], UnitOfWork]
InvocationObserver = Callable[[dict[str, Any]], None]


@dataclass
class InvocationPolicy:
    """What the registry installs on every guarded skill."""

    guard: SkillGuard = field(default_factory=AllowAll)
    unit_of_work: UnitOfWorkFactory = lambda context, node_id, skill: NoopUnitOfWork()
    observer: InvocationObserver | None = None  # called once per invocation (audit, tests)


DEFAULT_POLICY = InvocationPolicy()

_UOW_ATTR = "_unit_of_work"


def current_unit_of_work(context: FlowContext) -> UnitOfWork | None:
    """The unit of work of the step being executed, carried in the context."""
    return getattr(context, _UOW_ATTR, None)


def node_id_of(context: FlowContext, skill: BaseComponent) -> str:
    """The node a call runs as: the bound node id, the graph executor's current node,
    else the instance name (sequential flows)."""
    if isinstance(context, ScopedContext):
        return context.node_id
    current = getattr(context, "current_node", None)
    return current if current else skill.name


def invoke_skill(
    skill: Skill,
    context: FlowContext,
    *,
    policy: InvocationPolicy = DEFAULT_POLICY,
    node_id: str | None = None,
) -> FlowContext | Awaitable[FlowContext]:
    """Run one skill call through the contract (sync, or a coroutine for async skills)."""
    node = node_id or node_id_of(context, skill)
    uow = policy.unit_of_work(context, node, skill)
    setattr(context, _UOW_ATTR, uow)
    try:
        before = policy.guard.before(skill, context, node)
        _record(context, node, skill, "before", before, policy)
        if before.verdict == "BLOCK":
            context.set_port(ERROR_PORT)
            uow.commit()
            return context
        if before.verdict == "REVIEW":
            context.suspend(node, reason=f"review:{before.rule_id or 'guard'}")
            uow.commit()
            return context
        snapshot = _snapshot(context)
        result = skill.process(context)
        if inspect.isawaitable(result):
            return _finish_async(skill, context, node, policy, uow, snapshot, result)
        return _finish(skill, _as_context(result, context), node, policy, uow, snapshot)
    except BaseException:
        uow.rollback()
        raise


async def _finish_async(
    skill: Skill,
    context: FlowContext,
    node: str,
    policy: InvocationPolicy,
    uow: UnitOfWork,
    snapshot: dict[str, Any] | None,
    pending: Awaitable[FlowContext],
) -> FlowContext:
    try:
        result = await pending
        return _finish(skill, _as_context(result, context), node, policy, uow, snapshot)
    except BaseException:
        uow.rollback()
        raise


def _finish(
    skill: Skill,
    context: FlowContext,
    node: str,
    policy: InvocationPolicy,
    uow: UnitOfWork,
    snapshot: dict[str, Any] | None,
) -> FlowContext:
    after = policy.guard.after(skill, context, node)
    _record(context, node, skill, "after", after, policy)
    if after.verdict == "BLOCK":
        _restore(context, snapshot)
        context.metadata.suspended = False  # a blocked step never parks the run itself
        context.metadata.suspended_at_node = None
        context.metadata.suspension_reason = None
        context.set_port(ERROR_PORT)
    uow.commit()
    return context


def _as_context(result: Any, context: FlowContext) -> FlowContext:
    return result if isinstance(result, FlowContext) else context


def _snapshot(context: FlowContext) -> dict[str, Any] | None:
    """The step's writes can be discarded only when they are isolated (namespaced)."""
    data = context.data
    return dict(data.overlay()) if isinstance(data, ScopedData) else None


def _restore(context: FlowContext, snapshot: dict[str, Any] | None) -> None:
    data = context.data
    if snapshot is None or not isinstance(data, ScopedData):
        return
    data._data.clear()
    data._data.update(snapshot)


def _record(
    context: FlowContext,
    node: str,
    skill: Skill,
    phase: Phase,
    decision: GuardDecision,
    policy: InvocationPolicy,
) -> None:
    record = decision.to_record(node, skill, phase)
    if decision.verdict != "ALLOW":
        context.metadata.guard_decisions.append(record)  # ALLOW is implied by a run step
    if policy.observer is not None and phase == "before":
        policy.observer(record)  # exactly once per invocation


# ── the registry wrap point ───────────────────────────────────────────────────


class Guarded(Skill):
    """A skill instance behind the invocation contract (sync skills).

    It *is* a Skill to every executor and tracker (``skill_meta``, ``llm``, config,
    lifecycle all delegate to the inner instance); only ``process`` differs.
    """

    def __init__(self, inner: Skill, policy: InvocationPolicy) -> None:
        # Not Skill.__init__: it would reset the injected llm on the inner skill
        BaseComponent.__init__(self, inner.name)
        self.inner = inner
        self.policy = policy

    # Skill surface the executors and trackers rely on ------------------------------
    def _get_meta(self) -> SkillMeta:
        return self.inner.skill_meta

    @property
    def skill_meta(self) -> SkillMeta:  # type: ignore[override]
        return self.inner.skill_meta

    @property
    def llm(self) -> Any:
        return self.inner.llm

    @llm.setter
    def llm(self, value: Any) -> None:
        self.inner.llm = value

    @property
    def config(self) -> dict[str, Any]:
        return self.inner.config

    @property
    def is_initialized(self) -> bool:
        return self.inner.is_initialized

    def init(self, config: dict[str, Any]) -> None:
        self.inner.init(config)

    def setup(self, context: FlowContext) -> None:
        self.inner.setup(context)

    def teardown(self, context: FlowContext) -> None:
        self.inner.teardown(context)

    def get_meta(self) -> Any:
        return self.inner.get_meta()

    def validate_config(self) -> list[str]:
        return self.inner.validate_config()

    def health_check(self) -> bool:
        return self.inner.health_check()

    def __repr__(self) -> str:
        return f"Guarded({self.inner!r})"

    # The contract ------------------------------------------------------------------
    def process(self, context: FlowContext) -> FlowContext:
        result = invoke_skill(self.inner, context, policy=self.policy)
        assert isinstance(result, FlowContext)  # noqa: S101 — sync skill
        return result


class GuardedAsync(Guarded):
    """A skill instance behind the invocation contract (async skills)."""

    async def process(self, context: FlowContext) -> FlowContext:  # type: ignore[override]
        result = invoke_skill(self.inner, context, policy=self.policy)
        if inspect.isawaitable(result):
            return await result
        return result


def guard(inner: Skill, policy: InvocationPolicy) -> Guarded:
    """Wrap a skill instance for the registry; async skills stay async."""
    if isinstance(inner, Guarded):
        return inner
    cls = GuardedAsync if is_async_skill(inner) else Guarded
    return cls(inner, policy)


__all__ = [
    "ALLOW",
    "ERROR_PORT",
    "AllowAll",
    "DEFAULT_POLICY",
    "GuardDecision",
    "Guarded",
    "GuardedAsync",
    "InvocationPolicy",
    "NoopUnitOfWork",
    "SkillGuard",
    "UnitOfWork",
    "Verdict",
    "current_unit_of_work",
    "guard",
    "invoke_skill",
    "node_id_of",
]
