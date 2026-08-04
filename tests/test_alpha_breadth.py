from datetime import date, timedelta

import numpy as np
import pytest

from app.alpha_breadth import expand_candidates, load_policy, load_replication_snapshot, positions_for
from app.real_alpha import PricePanel


def _weekdays(start: date, end: date) -> tuple[date, ...]:
    rows = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            rows.append(current)
        current += timedelta(days=1)
    return tuple(rows)


def _multi_regime_panel() -> PricePanel:
    """A long-horizon allocator sees bull, reversal, correlation, and volatility regimes."""

    dates = _weekdays(date(2007, 1, 1), date(2025, 12, 31))
    index = np.arange(len(dates))
    paths = []
    for asset in range(5):
        drift = 0.00016 + asset * 0.000025
        cycle = 0.0018 * np.sin(index / (31 + asset * 7) + asset)
        crisis = np.where((index > 800) & (index < 1050), -0.0012 + asset * 0.00012, 0.0)
        paths.append(100 * np.cumprod(1 + drift + cycle + crisis))
    risky = np.asarray(paths).T
    cash = 100 * np.cumprod(np.full(len(dates), 1.00005))
    return PricePanel(
        dates,
        ("A", "B", "C", "D", "E"),
        risky,
        cash,
        {"common_rows": len(dates)},
        {symbol: "sha256:test" for symbol in ("A", "B", "C", "D", "E", "CASH")},
    )


def test_research_director_gets_a_broad_frozen_family_graph_not_another_nine_trial_batch():
    policy, _ = load_policy()
    candidates = expand_candidates(policy)
    assert len(candidates) == 100
    assert len({row["family"] for row in candidates if row["family"] != "baseline"}) == 13
    assert {row["rule"] for row in candidates} >= {
        "absolute_trend", "relative_momentum", "dual_momentum", "inverse_volatility",
        "volatility_managed_equal", "short_term_reversal", "correlation_gated_inverse_volatility",
        "turn_of_month", "option_expiration_week", "halloween",
    }


def test_sustained_multi_regime_investor_never_receives_short_or_leveraged_weights():
    panel = _multi_regime_panel()
    policy, _ = load_policy()
    for candidate in expand_candidates(policy):
        positions = positions_for(panel, candidate)
        weights = positions / len(panel.risky_symbols)
        assert np.all(np.isfinite(weights)), candidate["id"]
        assert np.min(weights) >= 0, candidate["id"]
        assert np.max(weights.sum(axis=1)) <= 1.0 + 1e-12, candidate["id"]


def test_replication_benchmark_is_always_long_not_delayed_long():
    panel = _multi_regime_panel()
    positions = positions_for(panel, {"id": "always_long", "family": "baseline", "rule": "always_long"})
    assert np.all(positions[0] == 0)
    assert np.all(positions[1:] == 1)


def test_future_market_shock_cannot_rewrite_any_candidates_past_allocations():
    panel = _multi_regime_panel()
    policy, _ = load_policy()
    cutoff = panel.dates.index(date(2018, 1, 1))
    poisoned_prices = panel.risky_prices.copy()
    poisoned_prices[cutoff:] *= np.linspace(0.2, 8.0, len(poisoned_prices) - cutoff)[:, None]
    poisoned = PricePanel(
        panel.dates, panel.risky_symbols, poisoned_prices, panel.cash_prices,
        panel.quality, panel.raw_hashes,
    )
    for candidate in expand_candidates(policy):
        original = positions_for(panel, candidate)
        replay = positions_for(poisoned, candidate)
        assert np.allclose(original[:cutoff], replay[:cutoff], rtol=0, atol=2e-15), candidate["id"]


def test_new_fund_history_cannot_be_ranked_before_its_declared_lookback_exists():
    panel = _multi_regime_panel()
    policy, _ = load_policy()
    for candidate in expand_candidates(policy):
        if candidate["rule"] not in {"relative_momentum", "dual_momentum", "short_term_reversal"}:
            continue
        positions = positions_for(panel, candidate)
        assert np.all(positions[:candidate["lookback_days"] + 1] == 0), candidate["id"]


def test_adversarial_researcher_cannot_unfreeze_costs_or_candidate_grid(tmp_path):
    policy_path = load_policy.__globals__["DEFAULT_POLICY"]
    changed = tmp_path / "policy.json"
    changed.write_text(policy_path.read_text(encoding="utf-8").replace('"base_one_way_bps": 10', '"base_one_way_bps": 0'), encoding="utf-8")
    with pytest.raises(ValueError, match="trust root"):
        load_policy(changed)


def test_data_custodian_rejects_a_replication_snapshot_rebound_to_another_selection(tmp_path):
    policy, policy_hash = load_policy()
    source = load_replication_snapshot.__globals__["REPO_ROOT"] / "research" / "alpha_breadth_replication_snapshot.json"
    changed = tmp_path / "snapshot.json"
    changed.write_text(source.read_text(encoding="utf-8").replace("22aeaaa0", "02aeaaa0"), encoding="utf-8")
    with pytest.raises(ValueError, match="trust root"):
        load_replication_snapshot(changed, policy_hash, "sha256:22aeaaa04678e138d362cb90e98b7d578f40b98234c5d49aeb147445c66e7828")


def test_research_generation_can_inherit_candidate_graph_but_not_silently_change_it():
    policy, policy_hash = load_policy(load_policy.__globals__["REPO_ROOT"] / "research" / "alpha_breadth_policy_v2.json")
    assert policy_hash.startswith("sha256:")
    assert len(expand_candidates(policy)) == 100
    assert policy["replication_data"]["risky_symbols"] == ["VTI", "VGK", "LQD", "GSG", "IYR"]
    assert policy["prior_result_status"] == "INVALID_BASELINE_DELAY_ASYMMETRY"


def test_corrected_generation_binds_a_third_unread_replication_universe():
    policy, _ = load_policy(load_policy.__globals__["REPO_ROOT"] / "research" / "alpha_breadth_policy_v3.json")
    assert len(expand_candidates(policy)) == 100
    assert policy["replication_data"]["risky_symbols"] == ["DIA", "EWJ", "EMB", "SLV", "RWR"]
    assert policy["prior_result_status"] == "INVALID_INSUFFICIENT_LOOKBACK_RANKING"


def test_drift_aware_generation_binds_the_final_unread_replication_universe():
    policy, _ = load_policy(load_policy.__globals__["REPO_ROOT"] / "research" / "alpha_breadth_policy_v4.json")
    assert len(expand_candidates(policy)) == 100
    assert policy["replication_data"]["risky_symbols"] == ["SPTM", "EWC", "BWX", "DBB", "ICF"]
    assert policy["prior_result_status"] == "INVALID_TARGET_WEIGHT_TURNOVER_INCONSISTENCY"
