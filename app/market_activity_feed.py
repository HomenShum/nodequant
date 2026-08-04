"""Compile a bounded, source-receipted U.S. large-cap activity observation.

The provider reports a current cumulative share-volume field. It does not expose
a historically addressable locator for these exact bytes, so this module never
labels the observation as fund flow, directional pressure, relative volume, or
promotion evidence. Raw bytes remain locally replayable by content hash.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.utils import parsedate_to_datetime
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RAW = (
    REPO_ROOT
    / "evidence"
    / "market-activity"
    / "raw"
    / "nasdaq-screener-observed-2026-08-04.json"
)
DEFAULT_RECEIPT = DEFAULT_RAW.with_suffix(".receipt.json")
DEFAULT_MARKET = REPO_ROOT / "web" / "app" / "market-map" / "data.json"
DEFAULT_POLICY = REPO_ROOT / "research" / "market_activity_feed_v0.json"
DEFAULT_OUTPUT = REPO_ROOT / "web" / "app" / "market-map" / "activity.json"
MAX_RAW_BYTES = 5_000_000
MAX_ROWS = 20_000
REQUEST_TIMEOUT_SECONDS = 30
CLAIM_BOUNDARY = (
    "TRADING_ACTIVITY_ONLY_PROVIDER_REPORTED_SHARE_VOLUME_"
    "NOT_FLOW_NOT_RELATIVE_VOLUME_NOT_HISTORICALLY_REFETCHABLE"
)


class MarketActivityFeedError(ValueError):
    """The source observation cannot support the declared activity layer."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _read_bounded(path: Path, maximum: int) -> bytes:
    if not path.is_file():
        raise MarketActivityFeedError(f"required artifact is absent: {path}")
    if path.stat().st_size > maximum:
        raise MarketActivityFeedError(f"artifact exceeds {maximum} bytes: {path}")
    return path.read_bytes()


def _load_object(path: Path, maximum: int) -> tuple[dict[str, Any], bytes]:
    raw = _read_bounded(path, maximum)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MarketActivityFeedError(f"artifact is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise MarketActivityFeedError(f"artifact must be a JSON object: {path}")
    return value, raw


def _utc(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise MarketActivityFeedError(f"{label} is absent")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MarketActivityFeedError(f"{label} is malformed") from exc
    if parsed.tzinfo is None:
        raise MarketActivityFeedError(f"{label} has no timezone")
    return parsed.astimezone(timezone.utc)


def _micros(value: Any, *, prefix: str = "", suffix: str = "", label: str) -> int:
    if not isinstance(value, str):
        raise MarketActivityFeedError(f"{label} is not text")
    clean = value.strip()
    if prefix:
        if not clean.startswith(prefix):
            raise MarketActivityFeedError(f"{label} lacks {prefix!r}")
        clean = clean[len(prefix) :]
    if suffix:
        if not clean.endswith(suffix):
            raise MarketActivityFeedError(f"{label} lacks {suffix!r}")
        clean = clean[: -len(suffix)]
    try:
        return int((Decimal(clean) * 1_000_000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError) as exc:
        raise MarketActivityFeedError(f"{label} is not numeric") from exc


def _reported_volume(value: Any) -> int:
    if not isinstance(value, str) or not value.isdigit():
        raise MarketActivityFeedError("reported volume is not an integer")
    parsed = int(value)
    if parsed < 0:
        raise MarketActivityFeedError("reported volume cannot be negative")
    return parsed


def _structural_holdings(market: dict[str, Any]) -> list[dict[str, Any]]:
    sectors = market.get("sectors")
    if not isinstance(sectors, list) or not sectors:
        raise MarketActivityFeedError("structural market sectors are absent")
    holdings: list[dict[str, Any]] = []
    for sector in sectors:
        if not isinstance(sector, dict) or not isinstance(sector.get("holdings"), list):
            raise MarketActivityFeedError("structural sector holdings are malformed")
        for holding in sector["holdings"]:
            if not isinstance(holding, dict):
                raise MarketActivityFeedError("structural holding is malformed")
            ticker = holding.get("ticker")
            label = holding.get("sector")
            if not isinstance(ticker, str) or not ticker or not isinstance(label, str) or not label:
                raise MarketActivityFeedError("structural holding identity is absent")
            holdings.append(holding)
    tickers = [holding["ticker"] for holding in holdings]
    if len(tickers) != len(set(tickers)):
        raise MarketActivityFeedError("structural universe contains duplicate tickers")
    return holdings


def compile_activity_snapshot(
    raw_path: Path = DEFAULT_RAW,
    receipt_path: Path = DEFAULT_RECEIPT,
    market_path: Path = DEFAULT_MARKET,
    policy_path: Path = DEFAULT_POLICY,
) -> dict[str, Any]:
    payload, raw = _load_object(raw_path, MAX_RAW_BYTES)
    receipt, _ = _load_object(receipt_path, 32_000)
    market, _ = _load_object(market_path, 2_000_000)
    policy, policy_raw = _load_object(policy_path, 64_000)

    if receipt.get("raw_sha256") != _sha256(raw):
        raise MarketActivityFeedError("receipt does not match the raw provider bytes")
    if receipt.get("http_status") != 200:
        raise MarketActivityFeedError("provider receipt is not a successful response")
    provider = policy.get("provider")
    if not isinstance(provider, dict) or receipt.get("url") != provider.get("url"):
        raise MarketActivityFeedError("receipt URL does not match the frozen provider policy")

    observed_at = _utc(receipt.get("provider_date_utc"), "provider observation time")
    structural_source = market.get("source")
    if not isinstance(structural_source, dict):
        raise MarketActivityFeedError("structural source receipt is absent")
    structural_retrieved_at = _utc(
        structural_source.get("retrieved_at_utc"), "structural universe receipt time"
    )
    if observed_at < structural_retrieved_at:
        raise MarketActivityFeedError("activity observation predates the structural universe receipt")

    status = payload.get("status")
    data = payload.get("data")
    if not isinstance(status, dict) or status.get("rCode") != 200 or not isinstance(data, dict):
        raise MarketActivityFeedError("provider payload does not declare success")
    rows = data.get("rows")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_ROWS:
        raise MarketActivityFeedError("provider rows are absent or exceed the row bound")
    by_symbol: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise MarketActivityFeedError("provider row is malformed")
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise MarketActivityFeedError("provider symbol is absent")
        if symbol in by_symbol:
            raise MarketActivityFeedError(f"duplicate provider symbol: {symbol}")
        by_symbol[symbol] = row

    structural = _structural_holdings(market)
    structural_tickers = {holding["ticker"] for holding in structural}
    aliases = policy.get("symbol_aliases")
    inactive = policy.get("inactive_structural_rows")
    if not isinstance(aliases, dict) or not isinstance(inactive, list):
        raise MarketActivityFeedError("symbol policy is malformed")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in aliases.items()):
        raise MarketActivityFeedError("symbol alias must map text to text")
    if not set(aliases).issubset(structural_tickers):
        raise MarketActivityFeedError("symbol policy aliases a ticker outside the structural universe")

    inactive_by_ticker: dict[str, dict[str, Any]] = {}
    for item in inactive:
        if not isinstance(item, dict) or not isinstance(item.get("ticker"), str):
            raise MarketActivityFeedError("inactive structural row is malformed")
        ticker = item["ticker"]
        if ticker in inactive_by_ticker or ticker not in structural_tickers:
            raise MarketActivityFeedError("inactive structural ticker is duplicated or outside the universe")
        effective_at = _utc(item.get("effective_at_utc"), "inactive effective time")
        if effective_at > observed_at:
            raise MarketActivityFeedError("inactive classification uses future information")
        if not isinstance(item.get("reason"), str) or not item["reason"]:
            raise MarketActivityFeedError("inactive classification reason is absent")
        sources = item.get("sources")
        if not isinstance(sources, list) or len(sources) < 1 or not all(
            isinstance(source, str) and source.startswith("https://") for source in sources
        ):
            raise MarketActivityFeedError("inactive classification source is absent")
        inactive_by_ticker[ticker] = item

    eligible = [holding for holding in structural if holding["ticker"] not in inactive_by_ticker]
    missing = [
        holding["ticker"]
        for holding in eligible
        if aliases.get(holding["ticker"], holding["ticker"]) not in by_symbol
    ]
    if missing:
        raise MarketActivityFeedError("missing eligible symbols: " + ", ".join(sorted(missing)))

    observed_holdings: list[dict[str, Any]] = []
    for holding in eligible:
        ticker = holding["ticker"]
        provider_symbol = aliases.get(ticker, ticker)
        row = by_symbol[provider_symbol]
        volume = _reported_volume(row.get("volume"))
        last_sale_micros = _micros(row.get("lastsale"), prefix="$", label="last sale")
        if last_sale_micros <= 0:
            raise MarketActivityFeedError("last sale must be positive")
        observed_holdings.append(
            {
                "ticker": ticker,
                "provider_symbol": provider_symbol,
                "name": holding.get("name", ticker),
                "sector": holding["sector"],
                "reported_share_volume": volume,
                "last_sale_micros": last_sale_micros,
                "net_change_micros": _micros(row.get("netchange"), label="net change"),
                "pct_change_micropct": _micros(
                    row.get("pctchange"), suffix="%", label="percent change"
                ),
            }
        )

    sectors: list[dict[str, Any]] = []
    for sector in market["sectors"]:
        children = sorted(
            (row for row in observed_holdings if row["sector"] == sector["label"]),
            key=lambda row: (-row["reported_share_volume"], row["ticker"]),
        )
        if not children:
            raise MarketActivityFeedError(f"eligible activity sector is empty: {sector['label']}")
        sectors.append(
            {
                "key": sector["key"],
                "label": sector["label"],
                "eligible_constituents": len(children),
                "reported_share_volume": sum(row["reported_share_volume"] for row in children),
                "holdings": children,
            }
        )
    market_volume = sum(sector["reported_share_volume"] for sector in sectors)
    projection: dict[str, Any] = {
        "version": "nodequant-market-activity-observation-v0",
        "status": "OBSERVED_CURRENT_SNAPSHOT_EXACT_ELIGIBLE_COVERAGE",
        "claim_boundary": CLAIM_BOUNDARY,
        "promotion_evidence": False,
        "source": {
            "name": provider.get("name"),
            "url": provider.get("url"),
            "provider_date_utc": receipt["provider_date_utc"],
            "retrieved_at_utc": receipt.get("retrieved_at_utc"),
            "raw_sha256": receipt["raw_sha256"],
            "replayability": provider.get("replayability"),
        },
        "structural_universe": {
            "manifest_hash": market.get("manifest_hash"),
            "as_of": structural_source.get("as_of"),
            "retrieved_at_utc": structural_source.get("retrieved_at_utc"),
        },
        "policy_hash": _sha256(policy_raw),
        "coverage": {
            "structural_rows": len(structural),
            "inactive_structural_rows": len(inactive_by_ticker),
            "eligible_rows": len(eligible),
            "observed_rows": len(observed_holdings),
            "missing_rows": 0,
        },
        "inactive_structural_rows": sorted(
            inactive_by_ticker.values(), key=lambda item: item["ticker"]
        ),
        "market": {"reported_share_volume": market_volume},
        "sectors": sectors,
    }
    projection["snapshot_hash"] = _sha256(_canonical(projection))
    return projection


def _validated_provider_url(policy: dict[str, Any]) -> str:
    provider = policy.get("provider")
    url = provider.get("url") if isinstance(provider, dict) else None
    if not isinstance(url, str):
        raise MarketActivityFeedError("provider URL is absent")
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.nasdaq.com"
        or parsed.username
        or parsed.password
        or parsed.path != "/api/screener/stocks"
    ):
        raise MarketActivityFeedError("provider URL is outside the fixed Nasdaq screener endpoint")
    return url


def fetch_activity_snapshot(
    raw_path: Path = DEFAULT_RAW,
    receipt_path: Path = DEFAULT_RECEIPT,
    policy_path: Path = DEFAULT_POLICY,
) -> dict[str, Any]:
    if raw_path.exists() or receipt_path.exists():
        raise MarketActivityFeedError("write-once provider artifact already exists")
    policy, _ = _load_object(policy_path, 64_000)
    url = _validated_provider_url(policy)
    request = Request(
        url,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.nasdaq.com",
            "Referer": "https://www.nasdaq.com/market-activity/stocks/screener",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36"
            ),
        },
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
            status = response.status
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) > MAX_RAW_BYTES:
                raise MarketActivityFeedError("provider body exceeds the response bound")
            raw = response.read(MAX_RAW_BYTES + 1)
            if len(raw) > MAX_RAW_BYTES:
                raise MarketActivityFeedError("provider body exceeds the response bound")
            provider_date = response.headers.get("Date")
    except (OSError, TimeoutError) as exc:
        raise MarketActivityFeedError(f"provider request failed: {exc}") from exc
    if status != 200:
        raise MarketActivityFeedError(f"provider returned HTTP {status}")
    try:
        date_value = parsedate_to_datetime(provider_date) if provider_date else None
    except (TypeError, ValueError) as exc:
        raise MarketActivityFeedError("provider Date header is malformed") from exc
    if date_value is None:
        raise MarketActivityFeedError("provider Date header is absent")
    provider_date_utc = date_value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    retrieved_at_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    receipt = {
        "http_status": status,
        "provider_date_utc": provider_date_utc,
        "retrieved_at_utc": retrieved_at_utc,
        "raw_sha256": _sha256(raw),
        "url": url,
    }
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    raw_tmp = raw_path.with_suffix(raw_path.suffix + ".tmp")
    receipt_tmp = receipt_path.with_suffix(receipt_path.suffix + ".tmp")
    raw_tmp.write_bytes(raw)
    receipt_tmp.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    raw_tmp.replace(raw_path)
    receipt_tmp.replace(receipt_path)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Freeze and compile the current activity observation.")
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument("--market", type=Path, default=DEFAULT_MARKET)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        if args.fetch:
            fetch_activity_snapshot(args.raw, args.receipt, args.policy)
        projection = compile_activity_snapshot(args.raw, args.receipt, args.market, args.policy)
        encoded = json.dumps(projection, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(args.output)
    except (MarketActivityFeedError, OSError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "detail": str(exc)}))
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "eligible_rows": projection["coverage"]["eligible_rows"],
                "observed_rows": projection["coverage"]["observed_rows"],
                "reported_share_volume": projection["market"]["reported_share_volume"],
                "snapshot_hash": projection["snapshot_hash"],
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
