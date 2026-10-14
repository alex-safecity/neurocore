"""Flow execution events for streaming mode."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class FlowEventType(StrEnum):
    FLOW_STARTED = "flow_started"
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"
    DATA_PRODUCED = "data_produced"
    FLOW_COMPLETED = "flow_completed"
    FLOW_FAILED = "flow_failed"
    # 0.5.0 — the runtime's own events on a run's stream
    ENVELOPE = "envelope"  # the platform sent something to the outside (data: the envelope)
    GUARD_DECISION = "guard_decision"  # a non-ALLOW verdict of the skill guard
    TIMER_FIRED = "timer_fired"  # a run timer resumed or escalated the run


@dataclass(frozen=True, slots=True)
class FlowEvent:
    event_type: FlowEventType
    step_name: str  # "" for flow-level events
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    duration_ms: float | None = None  # set on STEP_COMPLETED / FLOW_COMPLETED
    error: str | None = None  # set on STEP_FAILED / FLOW_FAILED

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type.value,
            "step_name": self.step_name,
            "data": self.data,
            "timestamp": self.timestamp,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }
