"""User-facing split metric scenarios, including missing and malformed splits."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app import ledger as lg
from app import proof_kernel as pk
from app.experiment import SplitId, assess_split_discipline, load_manifest
from app.performance import split_metrics


@pytest.fixture(scope="module")
def curve_artifacts():
    bars = pk.fixture_bars()
    packets = list(pk.packets(25, bars))
    events = lg.build_ledger(bars, packets)
    points = lg.reduce_curve(events)
    _, digest = load_manifest()
    _, tags = assess_split_discipline(
        [point.at_utc for point in points],
        [(packet.strategy.id, packet.strategy.config_hash) for packet in packets],
        digest,
    )
    return events, points, tags


def test_reviewer_sees_separate_metrics_and_no_blended_holdout(curve_artifacts):
    events, points, tags = curve_artifacts
    metrics = split_metrics(points, tags, events)

    assert [metric.split_id for metric in metrics] == [
        SplitId.DEVELOPMENT,
        SplitId.VALIDATION,
        SplitId.TEST,
    ]
    assert all(metric.point_count > 0 for metric in metrics)
    assert all(metric.max_drawdown_micros <= 0 for metric in metrics)
    assert all(metric.sampled_at_event_times for metric in metrics)
    assert all(
        metric.episodes_in_drawdown_interval
        == len(metric.episode_ids_in_drawdown_interval)
        for metric in metrics
    )
    for metric in metrics:
        if metric.recovered_at_utc is not None:
            assert metric.worst_drawdown_underwater_seconds == (
                metric.decline_duration_seconds + metric.recovery_duration_seconds
            )
        else:
            assert metric.recovery_duration_seconds is None
            assert metric.worst_drawdown_underwater_seconds >= metric.decline_duration_seconds
    assert SplitId.OPENED_HOLDOUT not in {metric.split_id for metric in metrics}
    assert sum(metric.point_count for metric in metrics) == len(points)
    assert sum(metric.net_pnl_micros for metric in metrics) == (
        points[-1].equity_micros - points[0].equity_micros
    )


def test_one_test_split_loss_changes_test_metrics_without_rewriting_development(
    curve_artifacts,
):
    events, points, tags = curve_artifacts
    baseline = {metric.split_id: metric for metric in split_metrics(points, tags, events)}
    changed = [point.model_copy(deep=True) for point in points]
    test_index = next(i for i, tag in enumerate(tags) if tag is SplitId.TEST)
    changed[test_index].equity_micros -= 100_000_000
    attacked = {metric.split_id: metric for metric in split_metrics(changed, tags, events)}

    assert attacked[SplitId.TEST] != baseline[SplitId.TEST]
    assert attacked[SplitId.DEVELOPMENT] == baseline[SplitId.DEVELOPMENT]
    assert attacked[SplitId.VALIDATION] == baseline[SplitId.VALIDATION]


def test_missing_or_extra_tags_fail_closed(curve_artifacts):
    events, points, tags = curve_artifacts
    with pytest.raises(ValueError, match="exactly one split tag"):
        split_metrics(points, tags[:-1], events)

    untagged = list(tags)
    untagged[0] = None
    with pytest.raises(ValueError, match="NOT_RUN"):
        split_metrics(points, untagged, events)


def test_first_test_event_cannot_disappear_into_the_split_reset(curve_artifacts):
    events, points, tags = curve_artifacts
    changed = [point.model_copy(deep=True) for point in points]
    test_indices = [i for i, tag in enumerate(tags) if tag is SplitId.TEST]
    carry_in = points[test_indices[0] - 1].equity_micros
    for index in test_indices:
        changed[index].equity_micros = carry_in - 100_000_000

    test = next(
        metric
        for metric in split_metrics(changed, tags, events)
        if metric.split_id is SplitId.TEST
    )
    assert test.net_pnl_micros == -100_000_000
    assert test.max_drawdown_micros == -100_000_000
    assert test.had_drawdown
    assert test.observation_censored
    assert test.recovered_at_utc is None
    assert test.worst_drawdown_underwater_seconds == (
        changed[test_indices[-1]].at_utc - test.peak_at_utc
    ).total_seconds()


def test_flat_split_has_no_drawdown_interval_or_episode_count(curve_artifacts):
    events, points, tags = curve_artifacts
    changed = [point.model_copy(deep=True) for point in points]
    test_indices = [i for i, tag in enumerate(tags) if tag is SplitId.TEST]
    carry_in = points[test_indices[0] - 1].equity_micros
    for index in test_indices:
        changed[index].equity_micros = carry_in

    test = next(
        metric
        for metric in split_metrics(changed, tags, events)
        if metric.split_id is SplitId.TEST
    )
    assert not test.had_drawdown
    assert test.max_drawdown_micros == 0
    assert test.episodes_in_drawdown_interval == 0
    assert test.episode_ids_in_drawdown_interval == ()
    assert test.recovered_at_utc is None
    assert test.worst_drawdown_underwater_seconds == 0


def test_episode_bracketing_recovered_drawdown_is_counted(curve_artifacts):
    events, source_points, _ = curve_artifacts
    base = source_points[0].at_utc
    points = [source_points[index].model_copy(deep=True) for index in range(3)]
    for index, (point, equity) in enumerate(zip(points, (100_000_000, 80_000_000, 100_000_000))):
        point.at_utc = base + timedelta(seconds=index * 10)
        point.equity_micros = equity
    fills = [event for event in events if event.type is lg.EventType.ORDER_FILL][:2]
    bracket = [
        fills[0].model_copy(update={"at_utc": base - timedelta(seconds=10), "packet_id": "bracket"}),
        fills[1].model_copy(update={"at_utc": base + timedelta(seconds=30), "packet_id": "bracket"}),
    ]
    metric = split_metrics(points, [SplitId.TEST] * 3, bracket)[0]
    assert metric.recovered_at_utc == base + timedelta(seconds=20)
    assert metric.worst_drawdown_underwater_seconds == 20
    assert metric.episode_ids_in_drawdown_interval == ("bracket",)
