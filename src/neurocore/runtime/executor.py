"""Blueprint executor — wires skills into FlowEngine and runs them.

The executor is the bridge between NeuroCore's skill system and
FlowEngine's execution engine. It:

1. Resolves skill names from the blueprint to Skill classes via the registry
2. Merges config: neurocore.yaml skills.<name> (base) + blueprint config (overlay)
3. Creates and initializes Skill instances
4. Builds a FlowEngine FlowConfig and component dict
5. Executes via FlowEngine (sync or async)

Usage:
    from neurocore.runtime import execute_blueprint, load_and_run

    # Low-level
    result = execute_blueprint(blueprint, registry, config)

    # High-level (load + discover + execute)
    result = load_and_run(blueprint_path, project_root=Path("."))

    # Streaming
    async for event in execute_blueprint_stream(bp, registry, cfg):
        print(event.event_type, event.step_name)
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from flowengine import (
    ComponentConfig,
    FlowConfig,
    FlowContext,
    FlowDefinition,
    FlowEngine,
    FlowSettings,
    StepConfig,
)

from neurocore.config.loader import load_config
from neurocore.config.schema import NeuroCoreConfig
from neurocore.errors import BlueprintError, ExecutionError
from neurocore.logging import get_logger
from neurocore.persistence import (
    RunRecord,
    RunStatus,
    RunStore,
    StepRecord,
    StepStatus,
    build_run_store,
    checkpoint_store_for,
)
from neurocore.persistence.base import utcnow_iso
from neurocore.runtime.blueprint import Blueprint, load_blueprint, validate_blueprint
from neurocore.skills.base import Skill, is_async_skill
from neurocore.skills.loader import discover_skills
from neurocore.skills.registry import SkillRegistry

log = get_logger("executor")


@dataclass
class _Tracker:
    """Records step-level history for a run during async execution."""

    store: RunStore
    run_id: str
    persist_snapshots: bool = False

    def record_step(
        self,
        *,
        step_index: int,
        component: str,
        skill: Skill,
        status: StepStatus,
        started_iso: str,
        started_perf: float,
        context: FlowContext,
        error: str | None = None,
    ) -> None:
        self.store.save_step(
            StepRecord(
                run_id=self.run_id,
                step_index=step_index,
                component=component,
                skill_type=skill.skill_meta.name,
                status=status,
                started_at=started_iso,
                duration_ms=(time.time() - started_perf) * 1000,
                error=error,
                output_keys=list(skill.skill_meta.provides or []),
                context_snapshot=context.to_dict() if self.persist_snapshots else None,
            )
        )


def merge_skill_config(
    neurocore_config: NeuroCoreConfig,
    skill_name: str,
    blueprint_config: dict[str, Any],
) -> dict[str, Any]:
    """Merge skill configuration from neurocore.yaml and blueprint.

    Priority (highest wins):
        1. Blueprint component config (overlay)
        2. neurocore.yaml skills.<name> (base)

    Args:
        neurocore_config: The project's NeuroCoreConfig.
        skill_name: The skill's registered name (for neurocore.yaml lookup).
        blueprint_config: Config from the blueprint's component definition.

    Returns:
        Merged config dict.
    """
    base = neurocore_config.get_skill_config(skill_name)
    merged = {**base, **blueprint_config}
    return merged


def _create_skill_instances(
    blueprint: Blueprint,
    registry: SkillRegistry,
    neurocore_config: NeuroCoreConfig,
) -> tuple[dict[str, Skill], dict[str, dict[str, Any]]]:
    """Create and initialize Skill instances for a blueprint.

    Args:
        blueprint: Parsed blueprint.
        registry: SkillRegistry with discovered skills.
        neurocore_config: Project config for config merging.

    Returns:
        Tuple of:
        - Dict mapping component instance names to initialized Skill instances.
        - Dict mapping component instance names to their merged configs.

    Raises:
        BlueprintError: If a skill cannot be found or instantiated.
    """
    instances: dict[str, Skill] = {}
    merged_configs: dict[str, dict[str, Any]] = {}

    for comp in blueprint.components:
        # Resolve skill class from registry
        skill_cls = registry.get(comp.type)
        if skill_cls is None:
            raise BlueprintError(
                f"Component '{comp.name}' references unknown skill '{comp.type}'"
            )

        # Create instance with the component's instance name
        try:
            instance = skill_cls(name=comp.name)
        except Exception as e:
            raise BlueprintError(
                f"Failed to instantiate skill '{comp.type}' as '{comp.name}': {e}"
            ) from e

        # Merge config and initialize
        merged_config = merge_skill_config(neurocore_config, comp.type, comp.config)
        instance.init(merged_config)

        # Inject LLM provider if required
        if instance.skill_meta.requires_llm:
            from neurocore.llm.provider import build_provider

            instance.llm = build_provider(merged_config)
            if instance.llm is None:
                raise BlueprintError(
                    f"Skill '{comp.name}' has requires_llm=True but no llm_provider "
                    f"is configured. Set llm_provider in neurocore.yaml or blueprint config."
                )

        # Validate config
        config_errors = instance.validate_config()
        if config_errors:
            raise BlueprintError(
                f"Skill '{comp.name}' ({comp.type}) config validation failed: "
                + "; ".join(config_errors)
            )

        # The registry wrap point: every instance the executors see is guarded
        instances[comp.name] = registry.wrap(instance)
        merged_configs[comp.name] = merged_config

    return instances, merged_configs


def _build_flow_config(
    blueprint: Blueprint,
    merged_configs: dict[str, dict[str, Any]] | None = None,
) -> FlowConfig:
    """Convert a NeuroCore Blueprint into a FlowEngine FlowConfig.

    Maps NeuroCore's blueprint model to FlowEngine's config schema,
    using fully qualified class paths for component types.

    Args:
        blueprint: Parsed NeuroCore blueprint.
        merged_configs: Optional mapping of component names to their
                       merged configs (neurocore.yaml + blueprint overlay).
                       If provided, these are used instead of blueprint-only
                       configs so FlowEngine initializes skills correctly.

    Returns:
        FlowEngine FlowConfig.
    """
    # Build component configs — use the skill class path as the type
    # (FlowEngine needs a type path, but since we provide pre-built
    # components, it won't actually load from the path. We use a
    # placeholder path based on the skill type name.)
    fe_components = []
    for comp in blueprint.components:
        # Use merged config if available, otherwise fall back to blueprint config
        config = (
            merged_configs.get(comp.name, comp.config)
            if merged_configs
            else comp.config
        )
        fe_components.append(
            ComponentConfig(
                name=comp.name,
                type=f"neurocore.skills.{comp.type}",
                config=config,
            )
        )

    # Build flow definition
    fe_flow_kwargs: dict[str, Any] = {
        "type": blueprint.flow.type,
    }

    if blueprint.flow.settings:
        fe_flow_kwargs["settings"] = FlowSettings(**blueprint.flow.settings)

    if blueprint.flow.steps:
        fe_flow_kwargs["steps"] = [
            StepConfig(
                component=step.component,
                description=step.description,
                condition=step.condition,
                on_error=step.on_error,
            )
            for step in blueprint.flow.steps
        ]

    if blueprint.flow.nodes:
        from flowengine import GraphEdgeConfig, GraphNodeConfig, NodeBinding

        fe_flow_kwargs["nodes"] = [
            GraphNodeConfig(
                id=node.id,
                component=node.component,
                description=node.description,
                on_error=node.on_error,
                max_visits=node.max_visits,
                bind=(
                    NodeBinding.model_validate(node.bind.model_dump())
                    if node.bind is not None
                    else None
                ),
                provenance=node.provenance,
            )
            for node in blueprint.flow.nodes
        ]

        if blueprint.flow.edges:
            fe_flow_kwargs["edges"] = [
                GraphEdgeConfig(
                    source=edge.source,
                    target=edge.target,
                    port=edge.port,
                    condition=edge.condition,
                )
                for edge in blueprint.flow.edges
            ]

    fe_flow = FlowDefinition(**fe_flow_kwargs)

    return FlowConfig(
        name=blueprint.name,
        version=blueprint.version,
        description=blueprint.description,
        components=fe_components,
        flow=fe_flow,
    )


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------

async def _run_skill_async(skill: Skill, context: FlowContext) -> FlowContext:
    """Execute a skill's process(), with automatic retry and exponential backoff.

    Retry behaviour is controlled by skill.skill_meta:
        max_retries=0        → no retry (default, backward-compatible)
        max_retries=3        → up to 3 retries after the first failure
        retry_on=()          → retry on any Exception when max_retries > 0
        retry_on=(SomeError,) → retry only those exceptions
        retry_delay_base=1.0 → first retry after ~1s
        retry_delay_max=60.0 → subsequent retries never wait longer than 60s
    """
    meta = skill.skill_meta
    max_retries: int = meta.max_retries
    retry_on: tuple[type[BaseException], ...] = meta.retry_on or (Exception,)
    base: float = meta.retry_delay_base
    max_delay: float = meta.retry_delay_max

    last_exc: BaseException | None = None

    for attempt in range(max_retries + 1):
        try:
            if is_async_skill(skill):
                return await skill.process(context)  # type: ignore[misc, no-any-return]
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, skill.process, context)

        except BaseException as exc:
            if max_retries == 0:
                raise

            if not isinstance(exc, retry_on):
                raise

            last_exc = exc

            if attempt == max_retries:
                break

            raw_delay = base * (2 ** attempt)
            capped_delay = min(raw_delay, max_delay)
            jittered_delay = random.uniform(0, capped_delay)

            log.warning(
                "skill.retry",
                skill=skill.name,
                attempt=attempt + 1,
                max_retries=max_retries,
                delay_seconds=round(jittered_delay, 2),
                exception_type=type(exc).__name__,
                exception_msg=str(exc)[:200],
            )
            await asyncio.sleep(jittered_delay)

    assert last_exc is not None  # noqa: S101
    raise last_exc


def _get_sequential_steps(blueprint: Blueprint) -> list[Any]:
    """Return the ordered list of flow steps for sequential/conditional flows."""
    if blueprint.flow.steps:
        return list(blueprint.flow.steps)
    return []


async def _execute_blueprint_async(
    blueprint: Blueprint,
    instances: dict[str, Skill],
    merged_configs: dict[str, dict[str, Any]],
    initial_data: dict[str, Any] | None,
    *,
    tracker: _Tracker | None = None,
    base_context: FlowContext | None = None,
    skip: set[str] | None = None,
) -> FlowContext:
    """Execute a sequential/conditional blueprint asynchronously.

    Records per-step history (when ``tracker`` is set), appends each completed
    component to ``metadata.completed_nodes``, and stops early if a skill
    suspends the run (e.g. a human-approval gate). ``skip`` lets a resume skip
    already-completed steps; ``base_context`` restores a prior run's state.
    """
    skip = skip or set()
    context = base_context if base_context is not None else FlowContext()
    if initial_data:
        for k, v in initial_data.items():
            context.set(k, v)
    steps = _get_sequential_steps(blueprint)
    for idx, step in enumerate(steps):
        if step.component in skip:
            continue
        skill = instances[step.component]
        started_iso, started_perf = utcnow_iso(), time.time()
        try:
            context = await _run_skill_async(skill, context)
        except Exception as exc:
            if tracker is not None:
                tracker.record_step(
                    step_index=idx, component=step.component, skill=skill,
                    status=StepStatus.FAILED, started_iso=started_iso,
                    started_perf=started_perf, context=context, error=str(exc),
                )
            raise
        if context.metadata.suspended:
            if tracker is not None:
                tracker.record_step(
                    step_index=idx, component=step.component, skill=skill,
                    status=StepStatus.SKIPPED, started_iso=started_iso,
                    started_perf=started_perf, context=context,
                )
            break
        if step.component not in context.metadata.completed_nodes:
            context.metadata.completed_nodes.append(step.component)
        if tracker is not None:
            tracker.record_step(
                step_index=idx, component=step.component, skill=skill,
                status=StepStatus.COMPLETED, started_iso=started_iso,
                started_perf=started_perf, context=context,
            )
    return context


def _check_executor_setting(blueprint: Blueprint) -> None:
    """``flow.settings.executor`` names the single executor path or nothing."""
    executor = (blueprint.flow.settings or {}).get("executor")
    if executor not in (None, "graph"):
        raise BlueprintError(
            f"flow.settings.executor={executor!r} is not available: graph flows run on "
            f"the GraphExecutor only ('graph'); the concurrent executor was removed in 0.5.0."
        )


def _execute_graph_via_engine(
    blueprint: Blueprint,
    instances: dict[str, Skill],
    merged_configs: dict[str, dict[str, Any]],
    initial_data: dict[str, Any] | None,
    *,
    has_async: bool,
    tracker: _Tracker | None,
    base_context: FlowContext | None,
    resume_node: str | None = None,
) -> FlowContext:
    """Run a graph flow through flowengine's GraphExecutor (ports/conditions/cycles).

    Uses the async executor when any skill is async (awaits coroutine ``process``),
    else the sync executor. Records per-step history via the tracker (timings +
    completed_nodes are populated by GraphExecutor). ``resume_node`` re-enters a
    suspended run at that node (flowengine >= 0.7); completed nodes are skipped
    and propagate the ports they set.
    """
    from flowengine import GraphExecutor
    from flowengine.eval.evaluator import ConditionEvaluator

    flow_config = _build_flow_config(blueprint, merged_configs)
    executor = GraphExecutor(
        nodes=flow_config.flow.nodes or [],
        edges=flow_config.flow.edges or [],
        components=instances,  # type: ignore[arg-type]  # Skill is a BaseComponent
        settings=flow_config.flow.settings,
        evaluator=ConditionEvaluator(),
    )
    context = base_context if base_context is not None else FlowContext()
    if initial_data:
        for key, value in initial_data.items():
            context.set(key, value)
    try:
        if has_async:
            context = asyncio.run(
                executor.execute_async(context, resume_node=resume_node)
            )
        else:
            context = executor.execute(context, resume_node=resume_node)
    except Exception:
        if tracker is not None:
            _record_sync_steps(tracker, context, instances, failed=True)
        raise
    if tracker is not None:
        _record_sync_steps(tracker, context, instances)
    return context


def execute_blueprint(
    blueprint: Blueprint,
    registry: SkillRegistry,
    neurocore_config: NeuroCoreConfig,
    *,
    initial_data: dict[str, Any] | None = None,
    _tracker: _Tracker | None = None,
    _base_context: FlowContext | None = None,
    _skip: set[str] | None = None,
    _resume_node: str | None = None,
) -> FlowContext:
    """Execute a blueprint. Supports both sync and async skills.

    This is the core execution function. It:
    1. Validates skill references
    2. Creates and initializes skill instances with merged config
    3. Detects async skills and chooses execution path
    4. For sync-only blueprints, delegates to FlowEngine
    5. For async blueprints, uses asyncio event loop

    Args:
        blueprint: Parsed Blueprint.
        registry: SkillRegistry with discovered skills.
        neurocore_config: Project configuration.
        initial_data: Optional initial context data (key-value pairs).

    Returns:
        FlowContext with execution results.

    Raises:
        BlueprintError: If validation or instantiation fails.
        ExecutionError: If execution fails.

    The leading-underscore params are used internally by
    :func:`execute_blueprint_tracked` and :func:`resume_blueprint`.
    """
    # Validate all skill references
    errors = validate_blueprint(blueprint, registry)
    if errors:
        raise BlueprintError(
            "Blueprint validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        )

    # Create and initialize skill instances
    skill_instances, merged_configs = _create_skill_instances(
        blueprint, registry, neurocore_config
    )

    has_async = any(is_async_skill(s) for s in skill_instances.values())

    # Graph flows: one executor path — flowengine's GraphExecutor (ports,
    # conditions, cycles, bindings, resume). The concurrent layer executor was
    # removed in 0.5.0; ``settings.executor`` may only name "graph".
    if blueprint.flow.type == "graph":
        _check_executor_setting(blueprint)
        try:
            return _execute_graph_via_engine(
                blueprint, skill_instances, merged_configs, initial_data,
                has_async=has_async, tracker=_tracker, base_context=_base_context,
                resume_node=_resume_node,
            )
        except Exception as e:
            if isinstance(e, (BlueprintError, ExecutionError)):
                raise
            raise ExecutionError(f"Blueprint execution failed: {e}") from e

    if has_async:
        # Async sequential path
        try:
            return asyncio.run(
                _execute_blueprint_async(
                    blueprint, skill_instances, merged_configs, initial_data,
                    tracker=_tracker, base_context=_base_context, skip=_skip,
                )
            )
        except Exception as e:
            if isinstance(e, (BlueprintError, ExecutionError)):
                raise
            raise ExecutionError(f"Blueprint execution failed: {e}") from e

    # Sync path: use FlowEngine for backward compatibility
    flow_config = _build_flow_config(blueprint, merged_configs)
    checkpoint_store = (
        checkpoint_store_for(_tracker.store) if _tracker is not None else None
    )
    try:
        engine = FlowEngine(
            flow_config,
            skill_instances,  # type: ignore[arg-type]  # Skill is a BaseComponent
            validate_types=False,
            checkpoint_store=checkpoint_store,
        )
    except Exception as e:
        raise ExecutionError(f"Failed to create FlowEngine: {e}") from e

    context = _base_context if _base_context is not None else FlowContext()
    if initial_data:
        for key, value in initial_data.items():
            context.set(key, value)

    try:
        result = engine.execute(context)
    except Exception as e:
        # FlowEngine mutates `context` in place, so completed steps are recorded
        # in its timings even on failure. Record those plus the failing step.
        if _tracker is not None:
            _record_sync_steps(_tracker, context, skill_instances, failed=True)
        raise ExecutionError(f"Blueprint execution failed: {e}") from e

    if _tracker is not None:
        _record_sync_steps(_tracker, result, skill_instances)
    # FlowEngine doesn't populate completed_nodes for sequential flows; derive
    # it from the step timings so resume/inspection behave consistently.
    for st in result.metadata.step_timings:
        if st.component not in result.metadata.completed_nodes:
            result.metadata.completed_nodes.append(st.component)

    return result


def _record_sync_steps(
    tracker: _Tracker,
    context: FlowContext,
    skill_instances: dict[str, Skill],
    *,
    failed: bool = False,
) -> None:
    """Persist StepRecords for a FlowEngine (sync) execution from its timings.

    FlowEngine records a timing for the failing step too, so the set of failed
    components is read from ``metadata.errors`` rather than inferred.
    """
    failed_components = {
        e.get("component") for e in context.metadata.errors
    } if failed else set()
    for st in context.metadata.step_timings:
        skill = skill_instances.get(st.component)
        status = (
            StepStatus.FAILED
            if st.component in failed_components
            else StepStatus.COMPLETED
        )
        tracker.store.save_step(
            StepRecord(
                run_id=tracker.run_id,
                step_index=st.step_index,
                component=st.component,
                skill_type=skill.skill_meta.name if skill else None,
                status=status,
                started_at=st.started_at.isoformat(),
                duration_ms=st.duration * 1000,
                output_keys=list(skill.skill_meta.provides or []) if skill else [],
            )
        )


def _finalize_run(
    store: RunStore,
    record: RunRecord,
    context: FlowContext,
    started_perf: float,
) -> None:
    """Persist the terminal state of a run from its final context."""
    record.final_context = context.to_dict()
    record.duration_ms = (time.time() - started_perf) * 1000
    record.updated_at = utcnow_iso()
    if context.metadata.suspended:
        record.status = RunStatus.SUSPENDED
        record.suspended_at_node = context.metadata.suspended_at_node
        record.suspension_reason = context.metadata.suspension_reason
    else:
        record.status = RunStatus.COMPLETED
    cid = context.get("checkpoint_id")
    if cid:
        record.checkpoint_id = cid
    store.save_run(record)


def execute_blueprint_tracked(
    blueprint: Blueprint,
    registry: SkillRegistry,
    neurocore_config: NeuroCoreConfig,
    *,
    initial_data: dict[str, Any] | None = None,
    run_store: RunStore | None = None,
    run_id: str | None = None,
    blueprint_path: Path | None = None,
) -> FlowContext:
    """Execute a blueprint and persist a durable run record + step history.

    Falls back to :func:`execute_blueprint` when persistence is disabled.

    Returns:
        The final FlowContext (may have ``metadata.suspended`` set if the run
        paused at a human-approval gate — resume with :func:`resume_blueprint`).
    """
    store = run_store if run_store is not None else build_run_store(neurocore_config)
    if store is None:
        return execute_blueprint(
            blueprint, registry, neurocore_config, initial_data=initial_data
        )

    rid = run_id or uuid4().hex
    tracker = _Tracker(
        store, rid, neurocore_config.persistence.persist_step_snapshots
    )
    record = RunRecord(
        run_id=rid,
        blueprint_name=blueprint.name,
        blueprint_version=blueprint.version,
        blueprint_path=str(blueprint_path) if blueprint_path else None,
        blueprint_snapshot=blueprint.model_dump(),
        flow_type=blueprint.flow.type,
        status=RunStatus.RUNNING,
        initial_data=initial_data or {},
    )
    store.save_run(record)
    started_perf = time.time()
    try:
        context = execute_blueprint(
            blueprint, registry, neurocore_config,
            initial_data=initial_data, _tracker=tracker,
        )
    except Exception as exc:
        record.status = RunStatus.FAILED
        record.error = str(exc)
        record.duration_ms = (time.time() - started_perf) * 1000
        record.updated_at = utcnow_iso()
        store.save_run(record)
        raise
    _finalize_run(store, record, context, started_perf)
    return context


def resume_blueprint(
    run_id: str,
    registry: SkillRegistry,
    neurocore_config: NeuroCoreConfig,
    *,
    resume_data: dict[str, Any] | None = None,
    run_store: RunStore | None = None,
) -> FlowContext:
    """Resume a suspended or failed run, optionally injecting ``resume_data``.

    Suspended runs (e.g. paused at an approval gate) continue from where they
    stopped; failed runs re-run from the failed step (completed steps are
    skipped). Works for both the sync (flowengine checkpoint) and async/DAG
    (restored FlowContext) execution paths.
    """
    store = run_store if run_store is not None else build_run_store(neurocore_config)
    if store is None:
        raise ExecutionError("Persistence is disabled; cannot resume runs.")
    run = store.load_run(run_id)
    if run is None:
        raise ExecutionError(f"Run not found: {run_id}")
    if run.status not in (RunStatus.SUSPENDED, RunStatus.FAILED):
        raise ExecutionError(
            f"Run {run_id} has status {run.status}; only suspended or failed "
            f"runs can be resumed."
        )

    blueprint = Blueprint(**run.blueprint_snapshot)
    tracker = _Tracker(
        store, run_id, neurocore_config.persistence.persist_step_snapshots
    )
    started_perf = time.time()

    # Sync path: flowengine owns the checkpoint.
    if run.checkpoint_id:
        skill_instances, merged_configs = _create_skill_instances(
            blueprint, registry, neurocore_config
        )
        flow_config = _build_flow_config(blueprint, merged_configs)
        engine = FlowEngine(
            flow_config,
            skill_instances,  # type: ignore[arg-type]  # Skill is a BaseComponent
            validate_types=False,
            checkpoint_store=checkpoint_store_for(store),
        )
        try:
            context = engine.resume(run.checkpoint_id, resume_data)
        except Exception as exc:
            run.status = RunStatus.FAILED
            run.error = str(exc)
            run.updated_at = utcnow_iso()
            store.save_run(run)
            raise ExecutionError(f"Resume failed: {exc}") from exc
        for st in context.metadata.step_timings:
            skill = skill_instances.get(st.component)
            store.save_step(
                StepRecord(
                    run_id=run_id, step_index=st.step_index, component=st.component,
                    skill_type=skill.skill_meta.name if skill else None,
                    status=StepStatus.COMPLETED, started_at=st.started_at.isoformat(),
                    duration_ms=st.duration * 1000,
                    output_keys=list(skill.skill_meta.provides or []) if skill else [],
                )
            )
        run.checkpoint_id = None
        _finalize_run(store, run, context, started_perf)
        return context

    # Async/DAG path: the stored final_context IS the checkpoint. The
    # GraphExecutor re-enters at the suspended node; the other paths skip
    # completed nodes.
    context = FlowContext.from_dict(run.final_context or {})
    resume_node = context.metadata.suspended_at_node
    context.metadata.suspended = False
    context.metadata.suspended_at_node = None
    context.metadata.suspension_reason = None
    if resume_data is not None:
        context.set("resume_data", resume_data)
    skip = set(context.metadata.completed_nodes)
    try:
        context = execute_blueprint(
            blueprint, registry, neurocore_config,
            _tracker=tracker, _base_context=context, _skip=skip,
            _resume_node=resume_node,
        )
    except Exception as exc:
        run.status = RunStatus.FAILED
        run.error = str(exc)
        run.updated_at = utcnow_iso()
        store.save_run(run)
        raise
    run.error = None
    _finalize_run(store, run, context, started_perf)
    return context


async def execute_blueprint_stream(
    blueprint: Blueprint,
    registry: SkillRegistry,
    config: NeuroCoreConfig,
    initial_data: dict[str, Any] | None = None,
) -> AsyncIterator[Any]:
    """Execute a blueprint, yielding FlowEvents as each step runs.

    Usage:
        async for event in execute_blueprint_stream(bp, registry, cfg):
            print(event.event_type, event.step_name)
    """
    from neurocore.runtime.events import FlowEvent, FlowEventType

    instances, merged_configs = _create_skill_instances(blueprint, registry, config)
    steps = _get_sequential_steps(blueprint)

    flow_start = time.time()
    yield FlowEvent(FlowEventType.FLOW_STARTED, step_name="", data={"blueprint": blueprint.name})

    context = FlowContext()
    if initial_data:
        for k, v in initial_data.items():
            context.set(k, v)
    for step in steps:
        skill = instances[step.component]
        step_start = time.time()
        yield FlowEvent(FlowEventType.STEP_STARTED, step_name=step.component)
        try:
            context = await _run_skill_async(skill, context)
            duration = (time.time() - step_start) * 1000
            yield FlowEvent(
                FlowEventType.STEP_COMPLETED,
                step_name=step.component,
                duration_ms=duration,
                data={k: v for k, v in context.data.items()
                      if k in (skill.skill_meta.provides or [])},
            )
        except Exception as exc:
            duration = (time.time() - step_start) * 1000
            yield FlowEvent(
                FlowEventType.STEP_FAILED,
                step_name=step.component,
                duration_ms=duration,
                error=str(exc),
            )
            yield FlowEvent(
                FlowEventType.FLOW_FAILED,
                step_name="",
                duration_ms=(time.time() - flow_start) * 1000,
                error=str(exc),
            )
            raise

    yield FlowEvent(
        FlowEventType.FLOW_COMPLETED,
        step_name="",
        duration_ms=(time.time() - flow_start) * 1000,
    )


def load_and_run(
    blueprint_path: Path,
    *,
    project_root: Path | None = None,
    initial_data: dict[str, Any] | None = None,
    track: bool = True,
) -> FlowContext:
    """High-level function: load config, discover skills, execute blueprint.

    Convenience wrapper that does everything:
    1. Load neurocore.yaml config
    2. Discover all skills (directory + entry points)
    3. Load and parse the blueprint
    4. Execute via FlowEngine, persisting run history (when ``track`` and
       persistence are enabled)

    Args:
        blueprint_path: Path to the blueprint YAML file.
        project_root: Optional project root (auto-detected if not provided).
        initial_data: Optional initial context data.
        track: Persist a run record + step history (default True).

    Returns:
        FlowContext with execution results.
    """
    # Load config
    neurocore_config = load_config(project_root=project_root)

    # Discover skills
    registry = discover_skills(neurocore_config)

    # Load blueprint
    blueprint = load_blueprint(blueprint_path)

    # Execute
    if track:
        return execute_blueprint_tracked(
            blueprint,
            registry,
            neurocore_config,
            initial_data=initial_data,
            blueprint_path=blueprint_path,
        )
    return execute_blueprint(
        blueprint,
        registry,
        neurocore_config,
        initial_data=initial_data,
    )
