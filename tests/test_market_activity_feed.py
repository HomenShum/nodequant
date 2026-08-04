import hashlib
import json
from pathlib import Path

import pytest

from app.market_activity_feed import MarketActivityFeedError, compile_activity_snapshot


RAW = Path("evidence/market-activity/raw/nasdaq-screener-observed-2026-08-04.json")
RECEIPT = RAW.with_suffix(".receipt.json")


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _write_case(
    tmp_path: Path,
    *,
    rows: list[dict[str, str]],
    provider_date_utc: str = "2026-08-04T22:08:17Z",
) -> tuple[Path, Path, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    market = {
        "manifest_hash": "sha256:" + "1" * 64,
        "source": {
            "as_of": "2026-08-03",
            "retrieved_at_utc": "2026-08-04T20:36:41Z",
        },
        "universe": {"equities": 3, "sectors": 1},
        "sectors": [
            {
                "key": "test-sector",
                "label": "Test Sector",
                "holdings": [
                    {"ticker": "A", "name": "Alpha", "sector": "Test Sector"},
                    {"ticker": "B", "name": "Beta", "sector": "Test Sector"},
                    {"ticker": "OLD", "name": "Old Co", "sector": "Test Sector"},
                ],
            }
        ],
    }
    policy = {
        "version": "test-policy-v0",
        "provider": {
            "name": "Test current observation",
            "url": "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&download=true",
            "replayability": "RAW_BYTES_REPLAYABLE_PROVIDER_LOCATOR_NOT_HISTORICALLY_REFETCHABLE",
        },
        "symbol_aliases": {"B": "B/B"},
        "inactive_structural_rows": [
            {
                "ticker": "OLD",
                "effective_at_utc": "2026-01-01T14:30:00Z",
                "reason": "TEST_DELISTING",
                "sources": ["https://example.test/official-delisting"],
            }
        ],
    }
    payload = {"data": {"rows": rows}, "status": {"rCode": 200}}
    encoded = _canonical(payload)
    receipt = {
        "http_status": 200,
        "provider_date_utc": provider_date_utc,
        "raw_sha256": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "retrieved_at_utc": "2026-08-04T22:08:18Z",
        "url": policy["provider"]["url"],
    }
    paths = [tmp_path / name for name in ("raw.json", "receipt.json", "market.json", "policy.json")]
    for path, value in zip(paths, (payload, receipt, market, policy), strict=True):
        path.write_bytes(_canonical(value))
    return tuple(paths)  # type: ignore[return-value]


def _row(symbol: str, volume: str, last_sale: str = "$10.00") -> dict[str, str]:
    return {
        "symbol": symbol,
        "name": symbol,
        "lastsale": last_sale,
        "netchange": "0.10",
        "pctchange": "1.00%",
        "marketCap": "1000000.00",
        "country": "United States",
        "ipoyear": "2000",
        "volume": volume,
        "sector": "Test Sector",
        "industry": "Test Industry",
        "url": f"/market-activity/stocks/{symbol.lower()}",
    }


def test_market_researcher_gets_exact_eligible_coverage_without_calling_a_delisted_residual_flow():
    snapshot = compile_activity_snapshot()
    holdings = [row for sector in snapshot["sectors"] for row in sector["holdings"]]

    assert snapshot["claim_boundary"] == (
        "TRADING_ACTIVITY_ONLY_PROVIDER_REPORTED_SHARE_VOLUME_"
        "NOT_FLOW_NOT_RELATIVE_VOLUME_NOT_HISTORICALLY_REFETCHABLE"
    )
    assert snapshot["coverage"] == {
        "structural_rows": 504,
        "inactive_structural_rows": 1,
        "eligible_rows": 503,
        "observed_rows": 503,
        "missing_rows": 0,
    }
    assert snapshot["inactive_structural_rows"][0]["ticker"] == "HOLX"
    assert {row["ticker"] for row in holdings}.isdisjoint({"HOLX"})
    provider_symbols = {row["ticker"]: row["provider_symbol"] for row in holdings}
    assert provider_symbols["BRKB"] == "BRK/B"
    assert provider_symbols["BFB"] == "BF/B"
    assert sum(sector["reported_share_volume"] for sector in snapshot["sectors"]) == snapshot[
        "market"
    ]["reported_share_volume"]
    assert snapshot["market"]["reported_share_volume"] == sum(
        row["reported_share_volume"] for row in holdings
    )
    assert snapshot["status"] == "OBSERVED_CURRENT_SNAPSHOT_EXACT_ELIGIBLE_COVERAGE"
    assert snapshot["promotion_evidence"] is False
    assert snapshot["snapshot_hash"].startswith("sha256:")
    assert compile_activity_snapshot() == snapshot


def test_activity_compiler_fails_closed_on_missing_duplicate_or_malformed_provider_rows(
    tmp_path: Path,
):
    honest_rows = [_row("A", "100"), _row("B/B", "200")]

    raw, receipt, market, policy = _write_case(tmp_path / "missing", rows=honest_rows[:1])
    with pytest.raises(MarketActivityFeedError, match="missing eligible symbols: B"):
        compile_activity_snapshot(raw, receipt, market, policy)

    raw, receipt, market, policy = _write_case(
        tmp_path / "duplicate", rows=[*honest_rows, _row("A", "300")]
    )
    with pytest.raises(MarketActivityFeedError, match="duplicate provider symbol"):
        compile_activity_snapshot(raw, receipt, market, policy)

    raw, receipt, market, policy = _write_case(
        tmp_path / "malformed", rows=[_row("A", "100"), _row("B/B", "N/A")]
    )
    with pytest.raises(MarketActivityFeedError, match="reported volume is not an integer"):
        compile_activity_snapshot(raw, receipt, market, policy)


def test_activity_compiler_rejects_false_receipts_and_future_information(tmp_path: Path):
    rows = [_row("A", "100"), _row("B/B", "200")]
    raw, receipt, market, policy = _write_case(tmp_path / "receipt", rows=rows)
    false_receipt = json.loads(receipt.read_text())
    false_receipt["raw_sha256"] = "sha256:" + "0" * 64
    receipt.write_bytes(_canonical(false_receipt))
    with pytest.raises(MarketActivityFeedError, match="receipt does not match"):
        compile_activity_snapshot(raw, receipt, market, policy)

    raw, receipt, market, policy = _write_case(
        tmp_path / "future", rows=rows, provider_date_utc="2026-08-04T19:00:00Z"
    )
    with pytest.raises(MarketActivityFeedError, match="predates the structural universe receipt"):
        compile_activity_snapshot(raw, receipt, market, policy)


def test_activity_compiler_preserves_absence_instead_of_turning_it_into_zero(tmp_path: Path):
    rows = [_row("A", "0")]
    raw, receipt, market, policy = _write_case(tmp_path, rows=rows)
    with pytest.raises(MarketActivityFeedError, match="missing eligible symbols: B"):
        compile_activity_snapshot(raw, receipt, market, policy)
