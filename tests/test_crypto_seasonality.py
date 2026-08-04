import inspect
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from app.crypto_seasonality import (
    HourlyPanel,
    _daily,
    _evaluator_dependencies,
    _evaluator_hash,
    expand_candidates,
    fetch_panel,
    positions_for,
    returns_for,
)


def _panel(days: int = 14) -> HourlyPanel:
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    timestamps = tuple(start + timedelta(hours=hour) for hour in range(days * 24))
    closes = 100 * np.cumprod(1 + np.asarray([0.002 if stamp.hour == 3 else -0.00003 for stamp in timestamps]))
    return HourlyPanel(timestamps, closes, {"completeness": 1.0}, {"0000.json": "sha256:test"}, "TEST-USD")


def test_research_director_gets_all_frozen_session_and_calendar_falsifiers():
    policy = {"candidate_families": [
        {"id": "utc_session", "rule": "utc_session", "duration_hours": [4, 8, 12], "start_hours": list(range(24))},
        {"id": "utc_hour", "rule": "utc_session", "duration_hours": [1], "start_hours": list(range(24))},
        {"id": "weekday", "rule": "weekday", "weekdays": list(range(7))},
        {"id": "week_part", "rule": "week_part", "parts": ["weekday", "weekend"]},
    ]}
    candidates = expand_candidates(policy)
    assert len(candidates) == 106
    assert len({row["id"] for row in candidates}) == 106


def test_hourly_trader_pays_both_sides_each_day_and_daily_compounding_is_exact():
    panel = _panel()
    candidate = {"id": "hour3", "family": "utc_hour", "rule": "utc_session", "start_hour": 2, "duration_hours": 1}
    positions = positions_for(panel, candidate)
    net, turnover = returns_for(panel, positions, 10)
    days, daily, daily_turnover = _daily(panel, net, turnover)
    assert len(days) == 13
    assert np.all(daily_turnover[1:-1] == 2)
    expected = (1 + 0.002 - 0.001) * (1 - 0.001) - 1
    assert daily[1] == pytest.approx(expected)


def test_future_price_poison_cannot_rewrite_any_past_calendar_positions():
    panel = _panel()
    candidate = {"id": "session", "family": "utc_session", "rule": "utc_session", "start_hour": 20, "duration_hours": 8}
    original = positions_for(panel, candidate)
    poisoned = HourlyPanel(panel.timestamps, np.linspace(1, 1_000_000, len(panel.closes)), panel.quality, panel.raw_hashes, panel.product_id)
    assert np.array_equal(original, positions_for(poisoned, candidate))


def test_missing_hour_never_creates_a_multi_hour_return_mislabeled_as_one_hour():
    panel = _panel(2)
    timestamps = panel.timestamps[:10] + panel.timestamps[11:]
    closes = np.concatenate([panel.closes[:10], panel.closes[11:]])
    broken = HourlyPanel(timestamps, closes, panel.quality, panel.raw_hashes, panel.product_id)
    positions = np.ones(len(timestamps))
    net, _ = returns_for(broken, positions, 0)
    assert net[10] == 0


def test_researcher_scores_only_days_with_all_observed_hourly_returns():
    panel = _panel(4)
    missing_index = 47  # Jan 2 23:00 UTC breaks that day's 22→23 and 23→00 intervals.
    timestamps = panel.timestamps[:missing_index] + panel.timestamps[missing_index + 1:]
    closes = np.concatenate([panel.closes[:missing_index], panel.closes[missing_index + 1:]])
    broken = HourlyPanel(timestamps, closes, panel.quality, panel.raw_hashes, panel.product_id)
    positions = np.ones(len(timestamps))
    net, turnover = returns_for(broken, positions, 0)

    days, _, _ = _daily(broken, net, turnover)

    assert days == ("2024-01-01", "2024-01-03")


def test_coinbase_bucket_start_is_converted_to_close_instant_before_session_assignment(tmp_path):
    raw = [
        [1704067200, 99, 101, 100, 100, 1],
        [1704070800, 100, 111, 100, 110, 1],
        [1704074400, 109, 122, 110, 121, 1],
    ]
    (tmp_path / "0000.json").write_text(json.dumps(raw), encoding="utf-8")
    policy = {
        "provider": {"host": "api.exchange.coinbase.com", "endpoint": "unused", "granularity_seconds": 3600, "chunk_hours": 24},
        "fixture": {"product_id": "TEST-USD", "start": "2024-01-01T00:00:00Z", "end_exclusive": "2024-01-01T03:00:00Z", "minimum_completeness": 1.0},
    }

    panel = fetch_panel(policy, "fixture", tmp_path, refresh=False)
    candidate = {"id": "hour1", "family": "utc_hour", "rule": "utc_session", "start_hour": 1, "duration_hours": 1}
    net, _ = returns_for(panel, positions_for(panel, candidate), 0)

    assert [stamp.hour for stamp in panel.timestamps] == [1, 2, 3]
    assert panel.quality["first_bucket_start"] == "2024-01-01T00:00:00+00:00"
    assert panel.quality["first_timestamp"] == "2024-01-01T01:00:00+00:00"
    assert net[1] == pytest.approx(0.10)


def test_evaluator_identity_changes_when_any_transitive_semantic_dependency_changes(monkeypatch):
    baseline = _evaluator_hash()
    original = inspect.getsource
    dependencies = _evaluator_dependencies()

    for target in dependencies:
        with monkeypatch.context() as current:
            current.setattr(inspect, "getsource", lambda value, target=target: original(value) + ("\n# semantic mutation" if value is target else ""))
            assert _evaluator_hash() != baseline, getattr(target, "__name__", repr(target))
