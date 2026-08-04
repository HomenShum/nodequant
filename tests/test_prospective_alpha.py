from datetime import date, timedelta
import json

import numpy as np
import pytest

from app.prospective_alpha import build_packet, load_source_result, write_once
from app.real_alpha import PricePanel, _canonical_hash


def _panel() -> PricePanel:
    dates = []
    day = date(2024, 1, 1)
    while day <= date(2026, 8, 3):
        if day.weekday() < 5:
            dates.append(day)
        day += timedelta(days=1)
    index = np.arange(len(dates))
    prices = np.column_stack([
        100 * np.cumprod(1 + 0.0002 + 0.0001 * np.sin(index / (9 + asset)))
        for asset in range(5)
    ])
    return PricePanel(tuple(dates), ("A", "B", "C", "D", "E"), prices, np.ones(len(dates)),
                      {"common_rows": len(dates)}, {"A": "sha256:a"})


def _source() -> dict:
    candidates = [
        {"id": "composite_momentum__top_n-3", "rule": "composite_momentum", "top_n": 3},
        {"id": "inverse_volatility__volatility_days-63", "rule": "inverse_volatility", "volatility_days": 63},
        {"id": "correlation_gated_inverse_volatility__volatility_days-63_correlation_days-126_maximum_correlation_bps-5000", "rule": "correlation_gated_inverse_volatility", "volatility_days": 63, "correlation_days": 126, "maximum_correlation_bps": 5000},
        {"id": "synthesis__correlation_gated_inverse_volatility__inverse_volatility", "rule": "equal_weight_parent_positions", "parents": ["correlation_gated_inverse_volatility__volatility_days-63_correlation_days-126_maximum_correlation_bps-5000", "inverse_volatility__volatility_days-63"]},
    ]
    return {"result_hash": "sha256:source", "discovery": {"selection_seal": {"candidates": candidates}}}


def _policy() -> dict:
    return {
        "frozen_at_utc": "2026-08-04T06:00:00Z",
        "forecast": {
            "primary_strategy_id": "composite_momentum__top_n-3",
            "comparison_strategy_ids": ["inverse_volatility__volatility_days-63", "correlation_gated_inverse_volatility__volatility_days-63_correlation_days-126_maximum_correlation_bps-5000", "synthesis__correlation_gated_inverse_volatility__inverse_volatility"],
            "benchmark_id": "always_long", "entry_close_rule": "first_future_close",
            "signal_cutoff_date": "2026-07-31",
            "first_return_rule": "future_only", "evaluation_end_rule": "next_month",
        },
        "future_gate": {"minimum_complete_packets": 24},
        "promotion_ceiling": "DIAGNOSTIC",
    }


def test_future_investor_receives_a_bounded_packet_with_no_outcome_smuggled_into_it():
    packet = build_packet(_panel(), _policy(), "sha256:policy", _source())
    assert packet["status"] == "FROZEN_AWAITING_FUTURE_OUTCOMES"
    assert packet["data"]["signal_cutoff"] == "2026-07-31"
    assert packet["data"]["observed_through"] == "2026-08-03"
    assert packet["outcome_boundary"]["outcomes_present"] is False
    assert packet["outcome_boundary"]["sealed_at_utc"] == packet["created_at_utc"]
    unhashed = {key: value for key, value in packet.items() if key != "packet_hash"}
    assert packet["packet_hash"] == _canonical_hash(unhashed)
    assert "return" not in json.dumps(packet["forecasts"]).lower()
    assert [row["role"] for row in packet["forecasts"]].count("PRIMARY") == 1
    for forecast in packet["forecasts"]:
        assert sum(forecast["risky_weights"].values()) + forecast["cash_weight"] == pytest.approx(1)


def test_future_price_poison_cannot_rewrite_the_frozen_month_end_forecast():
    panel = _panel()
    original = build_packet(panel, _policy(), "sha256:policy", _source())
    poisoned_prices = panel.risky_prices.copy()
    poisoned_prices[-1] *= np.asarray([0.1, 8, 0.2, 7, 0.3])
    poisoned = PricePanel(panel.dates, panel.risky_symbols, poisoned_prices, panel.cash_prices, panel.quality, panel.raw_hashes)
    replay = build_packet(poisoned, _policy(), "sha256:policy", _source())
    assert original["forecasts"] == replay["forecasts"]


def test_researcher_cannot_replace_a_frozen_packet_at_the_same_path(tmp_path):
    packet = build_packet(_panel(), _policy(), "sha256:policy", _source())
    path = tmp_path / "packet.json"
    first = write_once(path, packet)
    changed = {**packet, "packet_hash": "sha256:changed"}
    with pytest.raises(ValueError, match="different forecast"):
        write_once(path, changed)
    assert json.loads(path.read_text())["packet_hash"] == first["packet_hash"]


def test_stored_body_tampering_is_rejected_even_when_attacker_keeps_the_old_declared_hash(tmp_path):
    packet = build_packet(_panel(), _policy(), "sha256:policy", _source())
    path = tmp_path / "packet.json"
    write_once(path, packet)
    stored = json.loads(path.read_text())
    stored["forecasts"][0]["risky_weights"]["A"] = 0.999
    path.write_text(json.dumps(stored))
    with pytest.raises(ValueError, match="body differs"):
        write_once(path, packet)


def test_weekend_month_end_uses_the_exact_frozen_last_session_not_the_previous_month():
    panel = _panel()
    august_30 = panel.dates.index(date(2024, 8, 30))
    truncated = PricePanel(panel.dates[:august_30 + 1], panel.risky_symbols, panel.risky_prices[:august_30 + 1],
                           panel.cash_prices[:august_30 + 1], panel.quality, panel.raw_hashes)
    policy = _policy()
    policy["forecast"]["signal_cutoff_date"] = "2024-08-30"
    packet = build_packet(truncated, policy, "sha256:policy", _source())
    assert packet["data"]["signal_cutoff"] == "2024-08-30"


def test_packet_cannot_claim_to_precede_its_own_policy():
    policy = _policy()
    policy["frozen_at_utc"] = "2999-01-01T00:00:00Z"
    with pytest.raises(ValueError, match="freeze time follows"):
        build_packet(_panel(), policy, "sha256:policy", _source())


def test_source_result_tampering_is_rejected_even_if_claimed_hash_is_left_behind(tmp_path, monkeypatch):
    result = {"promotion_eligible": False, "value": 1}
    result["result_hash"] = _canonical_hash(result)
    result["created_at_utc"] = "later"
    path = tmp_path / "result.json"
    path.write_text(json.dumps(result))
    policy = {"source_result": {"path": "result.json", "semantic_result_hash": result["result_hash"]}}
    monkeypatch.setitem(load_source_result.__globals__, "REPO_ROOT", tmp_path)
    loaded = load_source_result(policy)
    assert loaded["value"] == 1
    path.write_text(path.read_text().replace('"value": 1', '"value": 2'))
    with pytest.raises(ValueError, match="semantic identity"):
        load_source_result(policy)
