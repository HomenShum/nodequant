"""Bounded synthetic scenarios for the order-lifecycle state machine.

This proves transition and quantity handling in code.  It is deliberately not
a broker receipt and cannot support promotion or execution-quality claims.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.order_journal import OrderEventType

MAX_LIFECYCLE_SCENARIOS = 32
MAX_LIFECYCLE_STEPS = 32


class ExpectedOutcome(str, Enum):
    COMPLETE = "COMPLETE"
    CANCELLED_REMAINDER = "CANCELLED_REMAINDER"
    REJECTED = "REJECTED"
    EXPIRED_REMAINDER = "EXPIRED_REMAINDER"
    LATENCY_BREACH = "LATENCY_BREACH"


class LifecycleStep(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_type: OrderEventType
    offset_ms: int = Field(ge=0, le=60_000)
    fill_qty: int = Field(default=0, ge=0, le=10_000)


class LifecycleScenario(BaseModel):
    model_config = ConfigDict(frozen=True)

    scenario_id: str = Field(min_length=1, max_length=80)
    requested_qty: int = Field(gt=0, le=10_000)
    latency_budget_ms: int = Field(gt=0, le=60_000)
    expected_outcome: ExpectedOutcome
    steps: tuple[LifecycleStep, ...] = Field(min_length=2, max_length=MAX_LIFECYCLE_STEPS)


class LifecycleObservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    scenario_id: str
    outcome: ExpectedOutcome
    terminal_event: OrderEventType
    filled_qty: int
    remaining_qty: int
    terminal_latency_ms: int
    expectation_matched: bool


class ExecutionLifecycleReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    observations: tuple[LifecycleObservation, ...] = Field(min_length=6, max_length=MAX_LIFECYCLE_SCENARIOS)
    suite_hash: str
    runner_hash: str
    scenario_count: int
    expectation_mismatches: int
    covers_partial_fill: bool
    covers_cancel: bool
    covers_reject: bool
    covers_expiry: bool
    covers_latency_breach: bool
    evidence_scope: str = "synthetic_state_machine_diagnostic_not_broker_evidence"

    @property
    def acceptable(self) -> bool:
        return (
            self.scenario_count == 6
            and self.expectation_mismatches == 0
            and self.covers_partial_fill
            and self.covers_cancel
            and self.covers_reject
            and self.covers_expiry
            and self.covers_latency_breach
        )


SCENARIOS: tuple[LifecycleScenario, ...] = tuple(
    LifecycleScenario.model_validate(
        {
            **row,
            "steps": [
                {"event_type": event_type, "offset_ms": offset_ms, "fill_qty": fill_qty}
                for event_type, offset_ms, fill_qty in row["steps"]
            ],
        }
    )
    for row in (
        {"scenario_id": "full-fill", "requested_qty": 10, "latency_budget_ms": 500,
         "expected_outcome": "COMPLETE", "steps": [("ORDER_CREATED", 0, 0), ("ORDER_APPROVED", 1, 0),
         ("ORDER_SUBMITTED", 5, 0), ("ORDER_FILLED", 50, 10)]},
        {"scenario_id": "partial-then-fill", "requested_qty": 10, "latency_budget_ms": 500,
         "expected_outcome": "COMPLETE", "steps": [("ORDER_CREATED", 0, 0), ("ORDER_APPROVED", 1, 0),
         ("ORDER_SUBMITTED", 5, 0), ("ORDER_PARTIALLY_FILLED", 70, 4), ("ORDER_FILLED", 130, 6)]},
        {"scenario_id": "partial-then-cancel", "requested_qty": 10, "latency_budget_ms": 500,
         "expected_outcome": "CANCELLED_REMAINDER", "steps": [("ORDER_CREATED", 0, 0),
         ("ORDER_APPROVED", 1, 0), ("ORDER_SUBMITTED", 5, 0),
         ("ORDER_PARTIALLY_FILLED", 50, 3), ("ORDER_CANCELLED", 100, 0)]},
        {"scenario_id": "venue-reject", "requested_qty": 10, "latency_budget_ms": 500,
         "expected_outcome": "REJECTED", "steps": [("ORDER_CREATED", 0, 0),
         ("ORDER_APPROVED", 1, 0), ("ORDER_SUBMITTED", 5, 0), ("ORDER_REJECTED", 20, 0)]},
        {"scenario_id": "order-expiry", "requested_qty": 10, "latency_budget_ms": 500,
         "expected_outcome": "EXPIRED_REMAINDER", "steps": [("ORDER_CREATED", 0, 0),
         ("ORDER_APPROVED", 1, 0), ("ORDER_SUBMITTED", 5, 0), ("ORDER_EXPIRED", 500, 0)]},
        {"scenario_id": "latency-budget-breach", "requested_qty": 10, "latency_budget_ms": 500,
         "expected_outcome": "LATENCY_BREACH", "steps": [("ORDER_CREATED", 0, 0),
         ("ORDER_APPROVED", 1, 0), ("ORDER_SUBMITTED", 5, 0), ("ORDER_FILLED", 1500, 10)]},
    )
)


def _hash(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


FROZEN_SUITE_HASH = _hash([scenario.model_dump(mode="json") for scenario in SCENARIOS])


def replay_scenario(scenario: LifecycleScenario) -> LifecycleObservation:
    allowed = {
        None: {OrderEventType.ORDER_CREATED},
        OrderEventType.ORDER_CREATED: {OrderEventType.ORDER_APPROVED, OrderEventType.ORDER_REJECTED},
        OrderEventType.ORDER_APPROVED: {OrderEventType.ORDER_SUBMITTED, OrderEventType.ORDER_REJECTED},
        OrderEventType.ORDER_SUBMITTED: {OrderEventType.ORDER_PARTIALLY_FILLED, OrderEventType.ORDER_FILLED,
                                          OrderEventType.ORDER_CANCELLED, OrderEventType.ORDER_REJECTED,
                                          OrderEventType.ORDER_EXPIRED},
        OrderEventType.ORDER_PARTIALLY_FILLED: {OrderEventType.ORDER_PARTIALLY_FILLED,
                                                OrderEventType.ORDER_FILLED,
                                                OrderEventType.ORDER_CANCELLED,
                                                OrderEventType.ORDER_EXPIRED},
    }
    previous: OrderEventType | None = None
    previous_offset = -1
    filled = 0
    for step in scenario.steps:
        if step.offset_ms < previous_offset:
            raise ValueError("lifecycle offsets must be monotonic")
        if step.event_type not in allowed.get(previous, set()):
            raise ValueError(f"invalid lifecycle transition {previous} -> {step.event_type}")
        is_fill = step.event_type in {OrderEventType.ORDER_PARTIALLY_FILLED, OrderEventType.ORDER_FILLED}
        if is_fill != (step.fill_qty > 0):
            raise ValueError("only fill events may carry a positive fill quantity")
        filled += step.fill_qty
        if filled > scenario.requested_qty:
            raise ValueError("cumulative fill exceeds requested quantity")
        if step.event_type is OrderEventType.ORDER_PARTIALLY_FILLED and filled >= scenario.requested_qty:
            raise ValueError("partial fill must leave a positive remainder")
        if step.event_type is OrderEventType.ORDER_FILLED and filled != scenario.requested_qty:
            raise ValueError("filled terminal must complete requested quantity")
        previous = step.event_type
        previous_offset = step.offset_ms
    terminal = scenario.steps[-1]
    if terminal.event_type not in {OrderEventType.ORDER_FILLED, OrderEventType.ORDER_CANCELLED,
                                   OrderEventType.ORDER_REJECTED, OrderEventType.ORDER_EXPIRED}:
        raise ValueError("scenario must end in a terminal lifecycle event")
    if terminal.event_type is OrderEventType.ORDER_FILLED:
        outcome = (ExpectedOutcome.LATENCY_BREACH if terminal.offset_ms > scenario.latency_budget_ms
                   else ExpectedOutcome.COMPLETE)
    elif terminal.event_type is OrderEventType.ORDER_CANCELLED:
        outcome = ExpectedOutcome.CANCELLED_REMAINDER
    elif terminal.event_type is OrderEventType.ORDER_REJECTED:
        outcome = ExpectedOutcome.REJECTED
    else:
        outcome = ExpectedOutcome.EXPIRED_REMAINDER
    return LifecycleObservation(
        scenario_id=scenario.scenario_id,
        outcome=outcome,
        terminal_event=terminal.event_type,
        filled_qty=filled,
        remaining_qty=scenario.requested_qty - filled,
        terminal_latency_ms=terminal.offset_ms,
        expectation_matched=outcome is scenario.expected_outcome,
    )


def evaluate_execution_lifecycle() -> ExecutionLifecycleReceipt:
    """Run the code-owned suite; callers cannot supply outcomes to self-award."""

    scenarios = SCENARIOS
    suite_hash = _hash([scenario.model_dump(mode="json") for scenario in scenarios])
    if suite_hash != FROZEN_SUITE_HASH:
        raise ValueError("lifecycle scenarios do not match the frozen diagnostic suite")
    observations = tuple(replay_scenario(scenario) for scenario in scenarios)
    event_types = {step.event_type for scenario in scenarios for step in scenario.steps}
    return ExecutionLifecycleReceipt(
        observations=observations,
        suite_hash=suite_hash,
        runner_hash=_hash(inspect.getsource(replay_scenario)),
        scenario_count=len(observations),
        expectation_mismatches=sum(not row.expectation_matched for row in observations),
        covers_partial_fill=OrderEventType.ORDER_PARTIALLY_FILLED in event_types,
        covers_cancel=OrderEventType.ORDER_CANCELLED in event_types,
        covers_reject=OrderEventType.ORDER_REJECTED in event_types,
        covers_expiry=OrderEventType.ORDER_EXPIRED in event_types,
        covers_latency_breach=any(row.outcome is ExpectedOutcome.LATENCY_BREACH for row in observations),
    )
