import json
from datetime import date, timedelta

import numpy as np
import pytest

from app.real_alpha import PricePanel, _parse_symbol, evaluate, load_policy, positions_for, returns_for


def _weekdays(start: date, end: date) -> tuple[date, ...]:
    rows = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            rows.append(current)
        current += timedelta(days=1)
    return tuple(rows)


def _regime_panel() -> PricePanel:
    """A researcher's multi-cycle panel: trends, reversals, and sustained history."""

    dates = _weekdays(date(2007, 6, 1), date(2025, 12, 31))
    count = len(dates)
    index = np.arange(count)
    # Five distinct paths prevent one duplicated series from manufacturing robustness.
    drifts = np.asarray([0.00030, 0.00034, 0.00025, 0.00014, 0.00020])
    cycles = np.asarray([
        0.0018 * np.sin(index / 47 + offset) - 0.0010 * np.sin(index / 233 + offset / 2)
        for offset in range(5)
    ]).T
    daily = drifts + cycles
    risky = 100 * np.cumprod(1 + daily, axis=0)
    cash = 100 * np.cumprod(np.full(count, 1 + 0.00006))
    return PricePanel(
        dates,
        ("SPY", "QQQ", "IWM", "TLT", "GLD"),
        risky,
        cash,
        {"common_rows": count},
        {symbol: "sha256:test" for symbol in ("SPY", "QQQ", "IWM", "TLT", "GLD", "BIL")},
    )


def test_portfolio_researcher_cannot_change_past_positions_with_future_prices():
    panel = _regime_panel()
    candidate = {"id": "tsmom_252", "rule": "lookback_return_positive", "lookback_days": 252}
    original = positions_for(panel, candidate)
    cutoff = panel.dates.index(date(2016, 1, 1))
    poisoned_prices = panel.risky_prices.copy()
    poisoned_prices[cutoff:] *= np.linspace(10, 100, len(poisoned_prices) - cutoff)[:, None]
    poisoned = PricePanel(
        panel.dates, panel.risky_symbols, poisoned_prices, panel.cash_prices,
        panel.quality, panel.raw_hashes,
    )
    replay = positions_for(poisoned, candidate)
    assert np.array_equal(original[:cutoff], replay[:cutoff])


def test_research_committee_gets_complete_trials_and_an_honest_holdout_state():
    policy, policy_hash = load_policy()
    result = evaluate(_regime_panel(), policy, policy_hash)
    assert result["trial_count"] == 9
    assert set(result["trials"]) == {
        "always_long", "tsmom_63", "tsmom_126", "tsmom_252", "sma_126",
        "sma_210", "sma_252", "consensus_63_126_252", "inverted_tsmom_252",
    }
    assert result["test"]["status"] in {"PASS", "FAIL"}
    expected_holdout = "PASS" if result["test"]["status"] == "PASS" and all(
        result["opened_holdout"]["checks"].values()
    ) else ("FAIL" if result["test"]["status"] == "PASS" else "NOT_OPENED_TEST_GATE_FAILED")
    assert result["opened_holdout"]["status"] == expected_holdout
    assert result["year_slices"]["observed_years"] >= 12
    assert 0 <= result["year_slices"]["positive_active_years"] <= result["year_slices"]["observed_years"]
    assert result["promotion_eligible"] is False


def test_data_engineer_rejects_duplicate_rows_and_counts_missing_prices():
    timestamps = [1_600_000_000, 1_600_000_000, 1_600_172_800]
    payload = {
        "chart": {"error": None, "result": [{
            "timestamp": timestamps,
            "indicators": {"adjclose": [{"adjclose": [100.0, 101.0, None]}]},
        }]}
    }
    with pytest.raises(ValueError, match="duplicate or invalid"):
        _parse_symbol(json.dumps(payload).encode(), "SPY", 10)


def test_adversarial_optimizer_cannot_edit_the_frozen_policy(tmp_path):
    policy = json.loads((load_policy.__globals__["DEFAULT_POLICY"]).read_text(encoding="utf-8"))
    policy["costs"]["base_one_way_bps"] = 0
    changed = tmp_path / "policy.json"
    changed.write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(ValueError, match="trust root"):
        load_policy(changed)


def test_rotation_investor_gets_full_capital_without_hidden_leverage():
    panel = _regime_panel()
    candidate = {"id": "top2", "rule": "top_n_lookback", "lookback_days": 126, "top_n": 2}
    positions = positions_for(panel, candidate)
    active_rows = positions.sum(axis=1) > 0
    weights = positions[active_rows] / len(panel.risky_symbols)
    assert np.allclose(weights.sum(axis=1), 1.0)
    assert np.max(weights) <= 0.5


def test_benchmark_investor_gets_the_always_long_exposure_from_the_first_usable_return():
    panel = _regime_panel()
    positions = positions_for(panel, {"id": "always_long", "rule": "always_long", "lookback_days": 0})
    assert np.all(positions[0] == 0)
    assert np.all(positions[1:] == 1)


def test_long_horizon_allocator_drifts_between_rebalances_and_pays_to_restore_targets():
    """Two divergent assets make hidden daily rebalancing impossible to disguise."""

    panel = PricePanel(
        (date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)),
        ("FAST", "SLOW"),
        np.asarray([[100.0, 100.0], [200.0, 100.0], [200.0, 200.0]]),
        np.asarray([100.0, 100.0, 100.0]),
        {"common_rows": 3},
        {"FAST": "sha256:test", "SLOW": "sha256:test", "CASH": "sha256:test"},
    )
    targets = np.ones((3, 2))
    targets[0] = 0
    monthly = np.asarray([False, True, False])
    daily = np.asarray([False, True, True])

    drifted = returns_for(panel, targets, 0, monthly)
    restored = returns_for(panel, targets, 0, daily)

    assert drifted["net"][1] == pytest.approx(0.5)
    assert drifted["net"][2] == pytest.approx(1 / 3)
    assert drifted["turnover"][2] == 0
    assert restored["net"][2] == pytest.approx(0.5)
    assert restored["turnover"][2] == pytest.approx(1 / 3)
