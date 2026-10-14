"""Durable run history for NeuroCore.

Persist every blueprint execution as an inspectable, resumable, replayable run.

    from neurocore.persistence import build_run_store, RunStatus

    store = build_run_store(config)
    runs = store.list_runs(status=RunStatus.SUSPENDED)
"""
from neurocore.persistence.base import (
    EnvelopeRecord,
    ResumeOutcome,
    RunRecord,
    RunStatus,
    RunStore,
    StepRecord,
    StepStatus,
    UnitOfWork,
)
from neurocore.persistence.checkpoint_adapter import (
    SQLiteCheckpointStore,
    checkpoint_store_for,
)
from neurocore.persistence.factory import build_run_store
from neurocore.persistence.memory_store import InMemoryRunStore
from neurocore.persistence.sqlite_store import SQLiteRunStore

__all__ = [
    "RunStore",
    "RunRecord",
    "StepRecord",
    "RunStatus",
    "StepStatus",
    "EnvelopeRecord",
    "ResumeOutcome",
    "UnitOfWork",
    "InMemoryRunStore",
    "SQLiteRunStore",
    "SQLiteCheckpointStore",
    "checkpoint_store_for",
    "build_run_store",
]
