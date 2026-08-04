import inspect
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app.crypto_rebalancing import _evaluator_dependencies, _evaluator_hash, evaluate, fetch_product


def _fixture():
    products = ["SOL-USD", "LINK-USD"]
    start = date(2021, 1, 1); days = [start + timedelta(days=i) for i in range(800)]
    raw = {}
    for product_index, product in enumerate(products):
        candles, price = [], 10.0 + product_index
        for index, day in enumerate(days):
            price *= 1 + (0.025 if (index + product_index) % 7 < 3 else -0.015)
            stamp = int(datetime.combine(day, datetime.min.time(), timezone.utc).timestamp())
            candles.append([stamp, price * .99, price * 1.01, price, price, 1000])
        raw[product] = [json.dumps(candles[index:index + 250]).encode() for index in range(0, len(candles), 250)]
    policy = {
        "data": {"products": products, "start_date": days[0].isoformat(), "end_exclusive": (days[-1] + timedelta(days=1)).isoformat(), "minimum_common_days": 700},
        "costs": {"one_way_bps_sensitivity": [0, 25], "primary_one_way_bps": 25},
        "bootstrap": {"seed": 1, "samples": 200, "block_days": 10, "quantile": 0.05},
        "folds": [{"id": "first", "start": days[1].isoformat(), "end": days[399].isoformat()}, {"id": "second", "start": days[400].isoformat(), "end": days[-1].isoformat()}],
        "gate": {"remove_best_days": 10, "minimum_positive_folds": 2}, "promotion_ceiling": "DIAGNOSTIC_ONLY",
    }
    return policy, raw


def test_allocator_researcher_gets_sustained_drifting_weights_costs_and_two_folds():
    policy, raw = _fixture(); result = evaluate(policy, raw, "sha256:policy")
    assert result["data"]["common_days"] == 800
    assert result["primary"]["folds"]["first"]["days"] == 399
    assert result["primary"]["folds"]["second"]["days"] == 400
    assert result["cost_sensitivities"]["25"]["strategy_cagr"] < result["cost_sensitivities"]["0"]["strategy_cagr"]
    assert result["primary"]["annual_one_way_turnover"] > 0
    assert result["promotion_eligible"] is False


def test_data_steward_fails_closed_on_one_missing_utc_bucket():
    policy, raw = _fixture(); rows = json.loads(raw["SOL-USD"][-1]); raw["SOL-USD"][-1] = json.dumps(rows[:-1]).encode()
    with pytest.raises(ValueError, match="required UTC daily opens"):
        evaluate(policy, raw, "sha256:policy")


def test_provider_overlap_is_deduplicated_only_when_the_open_is_identical():
    policy, raw = _fixture(); repeated = json.loads(raw["SOL-USD"][1])[0]
    first = json.loads(raw["SOL-USD"][0]); first.append(repeated); raw["SOL-USD"][0] = json.dumps(first).encode()
    assert evaluate(policy, raw, "sha256:policy")["data"]["common_days"] == 800
    repeated[3] += 1; first[-1] = repeated; raw["SOL-USD"][0] = json.dumps(first).encode()
    with pytest.raises(ValueError, match="conflicting opens"):
        evaluate(policy, raw, "sha256:policy")


def test_operator_cannot_overwrite_frozen_coinbase_chunk(tmp_path):
    policy = {"data": {"start_date": "2024-01-01", "end_exclusive": "2024-01-02"}, "provider": {"timeout_seconds": 1, "maximum_response_bytes": 100}}
    directory = tmp_path / "SOL-USD"; directory.mkdir(); (directory / "2024-01-01_2024-01-02.json").write_text("[]")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        fetch_product(policy, "SOL-USD", directory, refresh=True)


def test_evaluator_identity_covers_transitive_dependencies(monkeypatch):
    baseline, original = _evaluator_hash(), inspect.getsource
    for target in _evaluator_dependencies():
        with monkeypatch.context() as current:
            current.setattr(inspect, "getsource", lambda value, target=target: original(value) + ("\n# mutation" if value is target else ""))
            assert _evaluator_hash() != baseline
