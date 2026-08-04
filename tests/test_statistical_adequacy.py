"""Research-review scenarios for absent statistical evidence."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app import ledger as lg
from app import proof_kernel as pk
from app.decision_packet import CheckStatus
from app.experiment import assess_split_discipline, load_manifest
from app.performance import split_metrics
from app.statistical_adequacy import assess_statistical_adequacy


def test_reviewer_sees_not_run_instead_of_zero_or_significance_claim():
    bars = pk.fixture_bars()
    packets = list(pk.packets(25, bars))
    events = lg.build_ledger(bars, packets)
    points = lg.reduce_curve(events)
    manifest, digest = load_manifest()
    _, tags = assess_split_discipline(
        [point.at_utc for point in points],
        [(packet.strategy.id, packet.strategy.config_hash) for packet in packets],
        digest,
    )
    receipt = assess_statistical_adequacy(
        split_metrics(points, tags, events),
        events,
        manifest,
        expected_ledger_root_hash=lg.ledger_root_hash(events),
    )
    checks = {check.check_id: check for check in receipt.checks}

    assert receipt.episode_count == 2
    assert receipt.excluded_cross_boundary_episodes == 0
    assert not receipt.acceptable
    assert all(check.status is CheckStatus.NOT_RUN for check in checks.values())
    assert checks["test_confidence_interval"].observed == 2
    assert checks["test_confidence_interval"].required == 30
    assert checks["candidate_trial_ledger"].observed is None
    assert checks["deflated_sharpe"].observed is None
    assert checks["probability_of_backtest_overfitting"].observed is None
    assert receipt.evidence_scope == "diagnostic_status_not_promotion_evidence"


def test_cross_boundary_episode_is_excluded_instead_of_inflating_test_sample():
    bars = pk.fixture_bars()
    packets = list(pk.packets(25, bars))
    events = lg.build_ledger(bars, packets)
    points = lg.reduce_curve(events)
    manifest, digest = load_manifest()
    _, tags = assess_split_discipline(
        [point.at_utc for point in points],
        [(packet.strategy.id, packet.strategy.config_hash) for packet in packets],
        digest,
    )
    metrics = split_metrics(points, tags, events)
    test_start = manifest.split_windows[2].start_utc
    test_packet = next(
        event.packet_id
        for event in events
        if event.type is lg.EventType.ORDER_FILL and event.at_utc >= test_start
    )
    attacked = [event.model_copy(deep=True) for event in events]
    first_fill = next(
        event for event in attacked
        if event.type is lg.EventType.ORDER_FILL and event.packet_id == test_packet
    )
    first_fill.at_utc = test_start - timedelta(seconds=1)

    receipt = assess_statistical_adequacy(
        metrics,
        attacked,
        manifest,
        expected_ledger_root_hash=lg.ledger_root_hash(attacked),
    )
    assert receipt.episode_count == 1
    assert receipt.excluded_cross_boundary_episodes == 1


def test_absent_test_metrics_fail_closed():
    manifest, _ = load_manifest()
    with pytest.raises(ValueError, match="requires observed TEST"):
        assess_statistical_adequacy([], [], manifest, expected_ledger_root_hash="sha256:" + "0" * 64)


def test_manifest_and_ledger_substitution_reject_before_status_is_rendered():
    bars = pk.fixture_bars()
    packets = list(pk.packets(25, bars))
    events = lg.build_ledger(bars, packets)
    points = lg.reduce_curve(events)
    manifest, digest = load_manifest()
    _, tags = assess_split_discipline(
        [point.at_utc for point in points],
        [(packet.strategy.id, packet.strategy.config_hash) for packet in packets],
        digest,
    )
    metrics = split_metrics(points, tags, events)
    lowered = manifest.model_copy(update={"minimum_confidence_episodes": 2})
    with pytest.raises(ValueError, match="policy-bound"):
        assess_statistical_adequacy(
            metrics,
            events,
            lowered,
            expected_ledger_root_hash=lg.ledger_root_hash(events),
        )
    with pytest.raises(ValueError, match="bound ledger"):
        assess_statistical_adequacy(
            metrics,
            events,
            manifest,
            expected_ledger_root_hash="sha256:" + "0" * 64,
        )
