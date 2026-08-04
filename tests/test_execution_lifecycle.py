"""Realistic lifecycle personas plus adversarial state-machine mutations."""

from __future__ import annotations

import pytest

from app.execution_lifecycle import ExpectedOutcome, SCENARIOS, evaluate_execution_lifecycle, replay_scenario


def test_execution_reviewer_sees_every_nonhappy_path_without_broker_theatre():
    receipt = evaluate_execution_lifecycle()
    outcomes = {row.scenario_id: row for row in receipt.observations}

    assert receipt.acceptable
    assert receipt.scenario_count == 6
    assert all((receipt.covers_partial_fill, receipt.covers_cancel, receipt.covers_reject,
                receipt.covers_expiry, receipt.covers_latency_breach))
    assert outcomes["partial-then-cancel"].filled_qty == 3
    assert outcomes["partial-then-cancel"].remaining_qty == 7
    assert outcomes["latency-budget-breach"].terminal_latency_ms == 1500
    assert "not_broker_evidence" in receipt.evidence_scope


@pytest.mark.parametrize("attack", ["overfill", "time-reversal", "post-terminal", "false-partial"])
def test_malformed_venue_sequences_fail_closed(attack):
    scenario = SCENARIOS[1]
    steps = list(scenario.steps)
    if attack == "overfill":
        steps[-1] = steps[-1].model_copy(update={"fill_qty": 7})
    elif attack == "time-reversal":
        steps[-1] = steps[-1].model_copy(update={"offset_ms": 3})
    elif attack == "post-terminal":
        steps.append(steps[-1].model_copy(update={"offset_ms": 140}))
    else:
        steps[-2] = steps[-2].model_copy(update={"fill_qty": 10})
    with pytest.raises(ValueError):
        replay_scenario(scenario.model_copy(update={"steps": tuple(steps)}))


def test_application_evaluator_exposes_no_caller_suite_or_expected_outcome_input():
    import inspect

    assert inspect.signature(evaluate_execution_lifecycle).parameters == {}


def test_latency_budget_boundary_is_exact_and_not_a_score_floor():
    scenario = SCENARIOS[0]
    at_budget = scenario.model_copy(
        update={"steps": tuple(
            step.model_copy(update={"offset_ms": 500}) if index == len(scenario.steps) - 1 else step
            for index, step in enumerate(scenario.steps)
        )}
    )
    assert replay_scenario(at_budget).outcome is ExpectedOutcome.COMPLETE
    beyond = at_budget.model_copy(
        update={
            "expected_outcome": ExpectedOutcome.LATENCY_BREACH,
            "steps": tuple(
                step.model_copy(update={"offset_ms": 501}) if index == len(at_budget.steps) - 1 else step
                for index, step in enumerate(at_budget.steps)
            ),
        }
    )
    assert replay_scenario(beyond).outcome is ExpectedOutcome.LATENCY_BREACH
