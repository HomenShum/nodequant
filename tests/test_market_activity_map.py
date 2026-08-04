import copy
import hashlib
import json
from pathlib import Path

import pytest

from app.market_activity_map import MarketMapError, compile_market_map


RAW = Path("evidence/market-activity/raw/ivv-holdings-2026-08-03.json")
RECEIPT = RAW.with_suffix(".receipt.json")


def _write_case(tmp_path: Path, payload: dict, receipt: dict | None = None) -> tuple[Path, Path]:
    raw = tmp_path / "snapshot.json"
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    raw.write_bytes(encoded)
    authority = copy.deepcopy(receipt or json.loads(RECEIPT.read_text()))
    authority["raw_sha256"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    receipt_path = raw.with_suffix(".receipt.json")
    receipt_path.write_text(json.dumps(authority), encoding="utf-8")
    return raw, receipt_path


def _points(payload: dict) -> dict:
    return payload["componentsByNameMap"]["holdings"]["containersByNameMap"]["all"][
        "dataPointsByNameMap"
    ]


def test_market_researcher_sees_every_declared_large_cap_equity_once_and_reconciled():
    market = compile_market_map()
    holdings = [holding for sector in market["sectors"] for holding in sector["holdings"]]
    assert market["claim_boundary"] == "STRUCTURAL_COVERAGE_ONLY_NO_LIVE_ACTIVITY_OR_FLOW"
    assert market["universe"]["equities"] == 504
    assert market["universe"]["sectors"] == 11
    assert len(holdings) == len({holding["ticker"] for holding in holdings}) == 504
    assert sum(sector["constituent_count"] for sector in market["sectors"]) == 504
    assert sum(sector["weight_micropct"] for sector in market["sectors"]) == market["universe"][
        "weight_micropct"
    ]
    assert market["universe"]["unmapped_constituents"] == 0
    rounded_zero = [holding for holding in holdings if holding["weight_micropct"] == 0]
    assert [(holding["ticker"], holding["visual_weight_micropct"]) for holding in rounded_zero] == [
        ("HOLX", 1)
    ]
    assert market["manifest_hash"].startswith("sha256:")
    assert compile_market_map() == market


def test_provider_overlap_cannot_double_count_an_equity_in_the_map(tmp_path: Path):
    payload = json.loads(RAW.read_text())
    points = _points(payload)
    for field in ("ticker", "issueName", "sectorName", "assetClass", "holdingPercent", "marketValue"):
        points[field]["value"].append(points[field]["value"][0])
    raw, receipt = _write_case(tmp_path, payload)
    with pytest.raises(MarketMapError, match="expected 504"):
        compile_market_map(raw, receipt)


def test_degraded_provider_shape_and_false_receipt_fail_closed(tmp_path: Path):
    payload = json.loads(RAW.read_text())
    del _points(payload)["sectorName"]
    raw, receipt = _write_case(tmp_path, payload)
    with pytest.raises(MarketMapError, match="field is absent"):
        compile_market_map(raw, receipt)

    authority = json.loads(RECEIPT.read_text())
    authority["raw_sha256"] = "sha256:" + "0" * 64
    bad_receipt = tmp_path / "bad.receipt.json"
    bad_receipt.write_text(json.dumps(authority), encoding="utf-8")
    with pytest.raises(MarketMapError, match="does not match"):
        compile_market_map(RAW, bad_receipt)


def test_non_equity_derivatives_never_leak_into_constituent_tiles():
    market = compile_market_map()
    tickers = {holding["ticker"] for sector in market["sectors"] for holding in sector["holdings"]}
    assert {"USD", "ESU6", "XTSLA", "SGAFT"}.isdisjoint(tickers)
