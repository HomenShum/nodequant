"""Scenario tests for bounded execution counterfactuals."""

from __future__ import annotations

import hashlib
import json

import pytest

from app import ledger as lg
from app import proof_kernel as pk
from app.stress import (
    MAX_STRESS_BARS,
    MAX_STRESS_PACKETS,
    MAX_STRESS_SCENARIOS,
    StressScenario,
    run_execution_stress,
)


@pytest.fixture(scope="module")
def artifacts():
    bars = pk.fixture_bars()
    return bars, list(pk.packets(25, bars))


def test_reviewer_can_compare_cost_and_delay_stress(artifacts):
    bars, packets = artifacts
    results = run_execution_stress(
        bars,
        packets,
        [
            StressScenario(scenario_id="baseline"),
            StressScenario(scenario_id="cost-2x", cost_multiplier=2),
            StressScenario(scenario_id="delay-5", entry_delay_bars=5),
        ],
    )
    by_id = {result.scenario_id: result for result in results}

    canonical = lg.reduce_curve(lg.build_ledger(bars, packets))
    canonical_events = lg.build_ledger(bars, packets)
    assert by_id["baseline"].final_equity_micros == canonical[-1].equity_micros
    assert by_id["baseline"].max_drawdown_micros == min(lg.drawdown_micros(canonical))
    assert by_id["baseline"].attempted_packet_count == len(packets)
    assert by_id["baseline"].executed_packet_count == 17
    assert by_id["baseline"].tail_dropped_packet_count == 0
    assert by_id["baseline"].collision_skipped_packet_count == 8
    assert by_id["baseline"].ledger_root_hash == lg.ledger_root_hash(canonical_events)
    assert by_id["baseline"].curve_hash == "sha256:" + hashlib.sha256(
        json.dumps(
            [point.model_dump(mode="json") for point in canonical],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert by_id["cost-2x"].final_equity_micros < by_id["baseline"].final_equity_micros
    assert by_id["delay-5"].entry_delay_bars == 5
    assert all(result.assumption == "modelled_counterfactual_not_observed_execution" for result in results)
    assert all(result.max_drawdown_micros <= 0 for result in results)


def test_stress_is_deterministic_during_repeated_and_sustained_runs(artifacts):
    bars, packets = artifacts
    scenarios = [
        StressScenario(scenario_id=f"cost-{value}x", cost_multiplier=value)
        for value in range(1, 11)
    ]
    first = run_execution_stress(bars, packets, scenarios)
    for _ in range(5):
        assert run_execution_stress(bars, packets, scenarios) == first


def test_unbounded_or_ambiguous_stress_requests_fail_closed(artifacts):
    bars, packets = artifacts
    with pytest.raises(ValueError, match="1-32"):
        run_execution_stress(
            bars,
            packets,
            [StressScenario(scenario_id=f"s-{index}") for index in range(MAX_STRESS_SCENARIOS + 1)],
        )
    with pytest.raises(ValueError, match="unique"):
        run_execution_stress(
            bars,
            packets,
            [StressScenario(scenario_id="same"), StressScenario(scenario_id="same")],
        )
    with pytest.raises(ValueError, match="bars exceed"):
        run_execution_stress(
            [bars[0]] * (MAX_STRESS_BARS + 1),
            packets,
            [StressScenario(scenario_id="bounded")],
        )
    with pytest.raises(ValueError, match="packets exceed"):
        run_execution_stress(
            bars,
            [packets[0]] * (MAX_STRESS_PACKETS + 1),
            [StressScenario(scenario_id="bounded")],
        )


def test_unresolvable_source_packet_cannot_disappear_as_improved_performance(artifacts):
    from datetime import timedelta

    bars, packets = artifacts
    broken = list(packets)
    broken[0] = broken[0].model_copy(
        update={
            "signal": broken[0].signal.model_copy(
                update={
                    "earliest_execution_at_utc": (
                        broken[0].signal.earliest_execution_at_utc + timedelta(seconds=1)
                    )
                }
            )
        }
    )
    with pytest.raises(ValueError, match="does not resolve"):
        run_execution_stress(
            bars, broken, [StressScenario(scenario_id="malformed-baseline")]
        )


def test_gapped_bar_geometry_cannot_change_the_baseline_silently(artifacts):
    bars, packets = artifacts
    gapped = bars[:1] + bars[2:]
    with pytest.raises(ValueError, match="dense bar indices"):
        run_execution_stress(
            gapped, packets, [StressScenario(scenario_id="malformed-bars")]
        )


def test_duplicate_packet_ids_cannot_corrupt_execution_counts(artifacts):
    bars, packets = artifacts
    duplicated = list(packets)
    duplicated[1] = duplicated[1].model_copy(
        update={"packet_id": duplicated[0].packet_id}
    )
    with pytest.raises(ValueError, match="unique packet ids"):
        run_execution_stress(
            bars, duplicated, [StressScenario(scenario_id="duplicate-packets")]
        )
