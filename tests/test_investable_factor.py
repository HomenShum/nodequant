import inspect
import json
from datetime import datetime, timezone

import numpy as np
import pytest

from app.investable_factor import (
    _evaluator_dependencies,
    _evaluator_hash,
    _parse_adjusted_close,
    _portfolio_returns,
    build_monthly_panel,
    evaluate,
)


def _raw(days, values):
    return json.dumps({"chart": {"error": None, "result": [{"timestamp": [int(datetime.fromisoformat(day + "T21:00:00+00:00").timestamp()) for day in days], "indicators": {"adjclose": [{"adjclose": values}]}}]}}).encode()


def _policy():
    return {
        "data": {"symbols": ["MTUM", "QUAL", "VLUE", "SPY"]},
        "provider": {"maximum_rows_per_symbol": 100},
        "evaluation": {"required_first_month_end": "202312", "end_month": "202412"},
        "portfolio": {"factor_symbols": ["MTUM", "QUAL", "VLUE"], "benchmark_symbol": "SPY"},
        "costs": {"one_way_bps_sensitivity": [10], "primary_one_way_bps": 10},
        "folds": [{"id": "ALL", "start": "202401", "end": "202412"}],
        "bootstrap": {"seed": 7, "samples": 200, "block_months": 3},
        "gate": {"annualized_active_mean_lower_must_exceed": 0.0, "minimum_positive_folds": 1, "remove_best_months": 1, "remove_best_months_sharpe_must_exceed": 0.0},
        "promotion_ceiling": "TEST_ONLY",
    }


def test_portfolio_operator_sees_realistic_monthly_drift_entry_exit_and_cost_stress():
    months = ["202312"] + [f"2024{month:02d}" for month in range(1, 13)]
    factor = np.asarray([[100 * 1.01 ** index, 100 * 1.005 ** index, 100 * 1.015 ** index] for index in range(13)])
    spy = np.asarray([100 * 1.007 ** index for index in range(13)])[:, None]
    panel = {"months": months, "symbols": ["MTUM", "QUAL", "VLUE", "SPY"], "prices": np.hstack([factor, spy])}
    free = _portfolio_returns(panel, _policy(), 0)
    stressed = _portfolio_returns(panel, _policy(), 25)
    assert len(free["active"]) == 12
    assert stressed["factor"].sum() < free["factor"].sum()
    assert stressed["turnover"][0] > 1 and stressed["turnover"][-1] == pytest.approx(1.0)
    assert np.all(stressed["turnover"][1:-1] > 0)


def test_research_director_gets_one_closed_monthly_panel_and_rejects_missing_months():
    days = ["2023-12-29"] + [f"2024-{month:02d}-28" for month in range(1, 13)]
    raw = {symbol: _raw(days, [100 + index for index in range(13)]) for symbol in ("MTUM", "QUAL", "VLUE", "SPY")}
    panel = build_monthly_panel(_policy(), raw)
    assert panel["quality"]["monthly_rows"] == 13
    broken = dict(raw)
    broken["SPY"] = _raw(days[:5] + days[6:], [100 + index for index in range(12)])
    with pytest.raises(ValueError, match="lacks frozen month ends"):
        build_monthly_panel(_policy(), broken)


def test_adversarial_provider_rows_and_same_outcomes_cannot_forge_a_pass():
    duplicate = _raw(["2024-01-31", "2024-01-31"], [100, 101])
    with pytest.raises(ValueError, match="duplicate"):
        _parse_adjusted_close(duplicate, "MTUM", 10)
    months = ["202312"] + [f"2024{month:02d}" for month in range(1, 13)]
    prices = np.asarray([[100 * 1.01 ** index] * 4 for index in range(13)])
    panel = {"months": months, "month_end_dates": months, "symbols": ["MTUM", "QUAL", "VLUE", "SPY"], "prices": prices, "raw_hashes": {}, "quality": {}, "panel_hash": "sha256:test"}
    result = evaluate(panel, _policy(), "sha256:policy")
    assert result["verdict"] == "INVESTABLE_FACTOR_PROXY_FAILS"
    assert result["promotion_eligible"] is False


def test_evaluator_identity_covers_every_transitive_semantic_dependency(monkeypatch):
    baseline, original = _evaluator_hash(), inspect.getsource
    for target in _evaluator_dependencies():
        with monkeypatch.context() as current:
            current.setattr(inspect, "getsource", lambda value, target=target: original(value) + ("\n# mutation" if value is target else ""))
            assert _evaluator_hash() != baseline
