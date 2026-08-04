"""Build the declared U.S. large-cap structural market map.

The output is a deterministic projection of an official IVV holdings snapshot.
It intentionally carries no price, volume, directional-pressure, fund-flow, or
rotation values: attaching those labels without qualified time-series inputs
would turn a structural map into a false market-state claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RAW = REPO_ROOT / "evidence" / "market-activity" / "raw" / "ivv-holdings-2026-08-03.json"
DEFAULT_RECEIPT = DEFAULT_RAW.with_suffix(".receipt.json")
DEFAULT_OUTPUT = REPO_ROOT / "web" / "app" / "market-map" / "data.json"
MAX_RAW_BYTES = 1_000_000
EXPECTED_EQUITIES = 504
EXPECTED_SECTORS = {
    "Communication": "Communication Services",
    "Consumer Discretionary": "Consumer Discretionary",
    "Consumer Staples": "Consumer Staples",
    "Energy": "Energy",
    "Financials": "Financials",
    "Health Care": "Health Care",
    "Industrials": "Industrials",
    "Information Technology": "Information Technology",
    "Materials": "Materials",
    "Real Estate": "Real Estate",
    "Utilities": "Utilities",
}


class MarketMapError(ValueError):
    """The provider snapshot cannot support the declared map."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _read_bounded(path: Path, maximum: int) -> bytes:
    if not path.is_file():
        raise MarketMapError(f"required artifact is absent: {path}")
    if path.stat().st_size > maximum:
        raise MarketMapError(f"artifact exceeds {maximum} bytes: {path}")
    return path.read_bytes()


def _datapoints(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return payload["componentsByNameMap"]["holdings"]["containersByNameMap"]["all"][
            "dataPointsByNameMap"
        ]
    except (KeyError, TypeError) as exc:
        raise MarketMapError("official holdings payload shape changed") from exc


def _values(points: dict[str, Any], key: str) -> list[Any]:
    try:
        values = points[key]["value"]
    except (KeyError, TypeError) as exc:
        raise MarketMapError(f"holdings field is absent: {key}") from exc
    if not isinstance(values, list):
        raise MarketMapError(f"holdings field is not an array: {key}")
    return values


def compile_market_map(
    raw_path: Path = DEFAULT_RAW,
    receipt_path: Path = DEFAULT_RECEIPT,
) -> dict[str, Any]:
    raw = _read_bounded(raw_path, MAX_RAW_BYTES)
    receipt_raw = _read_bounded(receipt_path, 32_000)
    try:
        payload = json.loads(raw)
        receipt = json.loads(receipt_raw)
    except json.JSONDecodeError as exc:
        raise MarketMapError("provider artifact is not valid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(receipt, dict):
        raise MarketMapError("provider artifacts must be JSON objects")
    if receipt.get("raw_sha256") != _sha256(raw):
        raise MarketMapError("receipt does not match the raw provider bytes")
    if receipt.get("http_status") != 200:
        raise MarketMapError("provider receipt is not a successful response")

    points = _datapoints(payload)
    fields = {
        key: _values(points, key)
        for key in ("ticker", "issueName", "sectorName", "assetClass", "holdingPercent", "marketValue")
    }
    lengths = {len(values) for values in fields.values()}
    if len(lengths) != 1 or not lengths or next(iter(lengths)) > 1_000:
        raise MarketMapError("holdings arrays are unaligned or exceed the row bound")
    try:
        as_of = str(points["asOfDate"]["value"])
    except (KeyError, TypeError) as exc:
        raise MarketMapError("provider as-of date is absent") from exc
    if len(as_of) != 8 or not as_of.isdigit():
        raise MarketMapError("provider as-of date is malformed")

    holdings: list[dict[str, Any]] = []
    for index in range(next(iter(lengths))):
        if fields["assetClass"][index] != "Equity":
            continue
        ticker = str(fields["ticker"][index]).strip()
        name = str(fields["issueName"][index]).strip()
        provider_sector = str(fields["sectorName"][index]).strip()
        if not ticker or not name or provider_sector not in EXPECTED_SECTORS:
            raise MarketMapError("equity identity or sector is outside the declared universe")
        try:
            weight_micropct = round(float(fields["holdingPercent"][index]) * 1_000_000)
            market_value_micros = round(float(fields["marketValue"][index]) * 1_000_000)
        except (TypeError, ValueError) as exc:
            raise MarketMapError("equity weight or market value is not numeric") from exc
        if weight_micropct < 0 or market_value_micros <= 0:
            raise MarketMapError("equity weight cannot be negative and market value must be positive")
        holdings.append(
            {
                "ticker": ticker,
                "name": name,
                "sector": EXPECTED_SECTORS[provider_sector],
                "weight_micropct": weight_micropct,
                # A provider can retain a de minimis equity row whose displayed
                # weight rounds to zero (HOLX in this snapshot). Keep its true
                # weight at zero for reconciliation, but give it one micropct of
                # layout area so "all constituents visible" remains true.
                "visual_weight_micropct": max(1, weight_micropct),
                "market_value_micros": market_value_micros,
            }
        )

    if len(holdings) != EXPECTED_EQUITIES:
        raise MarketMapError(
            f"declared proxy expected {EXPECTED_EQUITIES} equities, received {len(holdings)}"
        )
    tickers = [holding["ticker"] for holding in holdings]
    if len(set(tickers)) != len(tickers):
        raise MarketMapError("duplicate ticker would double-count the hierarchy")

    sectors: list[dict[str, Any]] = []
    for label in EXPECTED_SECTORS.values():
        children = sorted(
            (holding for holding in holdings if holding["sector"] == label),
            key=lambda item: (-item["weight_micropct"], item["ticker"]),
        )
        if not children:
            raise MarketMapError(f"declared sector has no constituents: {label}")
        sectors.append(
            {
                "key": label.lower().replace(" ", "-"),
                "label": label,
                "constituent_count": len(children),
                "weight_micropct": sum(item["weight_micropct"] for item in children),
                "holdings": children,
            }
        )
    sectors.sort(key=lambda item: (-item["weight_micropct"], item["label"]))
    total_weight = sum(sector["weight_micropct"] for sector in sectors)
    projection: dict[str, Any] = {
        "version": "nodequant-market-activity-structural-v0",
        "claim_boundary": "STRUCTURAL_COVERAGE_ONLY_NO_LIVE_ACTIVITY_OR_FLOW",
        "source": {
            "name": "iShares Core S&P 500 ETF (IVV) official holdings",
            "url": receipt["url"],
            "as_of": f"{as_of[:4]}-{as_of[4:6]}-{as_of[6:]}",
            "retrieved_at_utc": receipt["retrieved_at_utc"],
            "raw_sha256": receipt["raw_sha256"],
        },
        "universe": {
            "label": "U.S. large-cap equities represented by IVV holdings",
            "equities": len(holdings),
            "sectors": len(sectors),
            "weight_micropct": total_weight,
            "unmapped_constituents": 0,
            "duplicate_tickers": 0,
        },
        "sectors": sectors,
    }
    projection["manifest_hash"] = _sha256(_canonical(projection))
    return projection


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile the bounded U.S. large-cap structural map.")
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        projection = compile_market_map(args.raw, args.receipt)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(projection, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(args.output)
    except (MarketMapError, OSError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "detail": str(exc)}))
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "equities": projection["universe"]["equities"],
                "sectors": projection["universe"]["sectors"],
                "manifest_hash": projection["manifest_hash"],
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
