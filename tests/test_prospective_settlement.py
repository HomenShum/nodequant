from datetime import date

import numpy as np
import pytest

from app.prospective_settlement import OutcomeNotReady, _fixed_allocation_return, settle
from app.real_alpha import PricePanel


def _panel(include_september: bool = True) -> PricePanel:
    dates = [date(2026, 7, 31), date(2026, 8, 4), date(2026, 8, 5), date(2026, 8, 31)]
    if include_september:
        dates.append(date(2026, 9, 1))
    rows = len(dates)
    base = np.asarray([100.0, 100.0, 100.0, 100.0, 100.0])
    risky = np.vstack([base, base, base * 1.01, base * 1.09, base * 1.10][:rows])
    cash = np.asarray([100, 100, 100.01, 100.02, 100.03][:rows], dtype=float)
    return PricePanel(tuple(dates), ("A", "B", "C", "D", "E"), risky, cash,
                      {"common_rows": rows}, {"A": "sha256:a"})


def _packet() -> dict:
    primary = {"strategy_id": "candidate", "role": "PRIMARY", "risky_weights": {s: 0.2 for s in "ABCDE"}, "cash_weight": 0.0}
    benchmark = {"strategy_id": "always_long", "role": "BENCHMARK", "risky_weights": {s: 0.0 for s in "ABCDE"}, "cash_weight": 1.0}
    return {"packet_hash": "sha256:packet", "outcome_boundary": {"sealed_at_utc": "2026-08-04T06:33:44+00:00"}, "forecasts": [primary, benchmark]}


def _policy() -> dict:
    return {"frozen_at_utc": "2026-08-04T06:38:00Z", "settlement": {
        "exchange_timezone": "America/New_York", "regular_close_local_time": "16:00:00",
        "regular_close_utc_time_for_frozen_window": "20:00:00",
        "entry_one_way_bps_on_risky_notional": 10, "exit_one_way_bps_on_risky_notional": 10,
    }, "promotion_ceiling": "DIAGNOSTIC"}


def test_future_researcher_cannot_settle_before_the_next_month_close_exists():
    with pytest.raises(OutcomeNotReady, match="next calendar month"):
        settle(_panel(False), _packet(), _policy(), "sha256:policy")


def test_monthly_investor_is_measured_only_after_entry_close_with_both_side_costs():
    result = settle(_panel(), _packet(), _policy(), "sha256:policy")
    assert result["window"] == {
        "authority_time_utc": "2026-08-04T06:38:00+00:00", "entry_close_date": "2026-08-04",
        "evaluation_end_close_date": "2026-09-01", "measured_close_to_close_intervals": 3,
    }
    expected = (1 - 0.001) * 1.10 * (1 - 0.001) - 1
    assert result["outcomes"][0]["net_return"] == pytest.approx(expected)
    assert result["promotion_eligible"] is False


def test_cost_accounting_handles_part_cash_without_charging_cash_turnover():
    panel = _panel()
    weights = np.full(5, 0.1)
    actual = _fixed_allocation_return(panel, 1, 4, weights, 0.5, 10, 10)
    investable = 1 - 0.5 * 0.001
    risky_value = investable * 0.5 * 1.10
    cash_value = investable * 0.5 * 1.0003
    expected = risky_value + cash_value - risky_value * 0.001 - 1
    assert actual == pytest.approx(expected)
