"""Sealed CFTC producer-hedging-pressure portfolio falsifier.

The CFTC panel and ETF prices are opened in separate stages.  A manifest made
only from the official positioning rows must be externally authorized before
the evaluator is allowed to fetch any return bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import numpy as np

from .real_alpha import _canonical_hash


REPO_ROOT = Path(__file__).parents[1]
DEFAULT_POLICY = REPO_ROOT / "research" / "cftc_flow_policy_v1.json"
DEFAULT_AUTHORITY = REPO_ROOT / "research" / "cftc_flow_price_authority_v1.json"
EXPECTED_POLICY_SHA256 = "1e912eb74cda6a64b78b511d36aae9a48f56b688182c4e7fd4f6751dcf1df190"
EXPECTED_AUTHORITY_SHA256 = "a31dacbdc6c5d8e270985df34fa9bdac990a31bd385823f568a016c69e676b41"


def _bounded_get(url: str, provider: dict[str, Any]) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != provider["host"] or parsed.fragment:
        raise ValueError("provider URL is outside the frozen HTTPS host")
    headers = {"User-Agent": provider.get("user_agent", "nodequant-research/0.1")}
    with httpx.Client(timeout=httpx.Timeout(provider["timeout_seconds"]), follow_redirects=False) as client:
        with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_bytes(64 * 1024):
                size += len(chunk)
                if size > provider["maximum_response_bytes"]:
                    raise ValueError("provider response exceeds the frozen byte cap")
                chunks.append(chunk)
    return b"".join(chunks)


def _cftc_url(policy: dict[str, Any]) -> str:
    data = policy["cftc_data"]
    fields = [
        "report_date_as_yyyy_mm_dd", "cftc_contract_market_code", "contract_market_name",
        "open_interest_all", "prod_merc_positions_long", "prod_merc_positions_short",
    ]
    codes = ",".join(f"'{row['contract_market_code']}'" for row in policy["universe"])
    where = (
        f"report_date_as_yyyy_mm_dd between '{data['report_start']}T00:00:00.000' "
        f"and '{data['report_end']}T00:00:00.000' and cftc_contract_market_code in ({codes})"
    )
    query = urlencode({
        "$select": ",".join(fields), "$where": where,
        "$order": "report_date_as_yyyy_mm_dd,cftc_contract_market_code",
        "$limit": data["maximum_rows"],
    })
    return f"https://{policy['cftc_provider']['host']}/resource/{data['dataset_id']}.json?{query}"


def fetch_cftc(policy: dict[str, Any], path: Path, *, refresh: bool) -> bytes:
    if refresh:
        if path.exists():
            raise ValueError(f"refusing to overwrite frozen CFTC bytes: {path.name}")
        raw = _bounded_get(_cftc_url(policy), policy["cftc_provider"])
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as handle:
                handle.write(raw)
        except FileExistsError as exc:
            raise ValueError(f"refusing to overwrite frozen CFTC bytes: {path.name}") from exc
        return raw
    return path.read_bytes()


def _number(row: dict[str, Any], key: str) -> float:
    try:
        value = float(row[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"CFTC row has invalid {key}") from exc
    if not math.isfinite(value):
        raise ValueError(f"CFTC row has non-finite {key}")
    return value


def compile_manifest(policy: dict[str, Any], raw: bytes, policy_hash: str) -> dict[str, Any]:
    payload = json.loads(raw)
    if not isinstance(payload, list) or len(payload) > policy["cftc_data"]["maximum_rows"]:
        raise ValueError("CFTC payload is not a bounded row list")
    identities = {row["contract_market_code"]: row for row in policy["universe"]}
    by_day: dict[date, dict[str, dict[str, Any]]] = {}
    for source in payload:
        if not isinstance(source, dict):
            raise ValueError("CFTC payload contains a non-object row")
        code = str(source.get("cftc_contract_market_code", ""))
        if code not in identities:
            raise ValueError(f"CFTC payload contains an unregistered contract code: {code}")
        try:
            report_day = date.fromisoformat(str(source["report_date_as_yyyy_mm_dd"])[:10])
        except (KeyError, ValueError) as exc:
            raise ValueError("CFTC row has an invalid report date") from exc
        if code in by_day.setdefault(report_day, {}):
            raise ValueError(f"CFTC payload duplicates {code} on {report_day}")
        open_interest = _number(source, "open_interest_all")
        if open_interest <= 0:
            raise ValueError("CFTC open interest must be positive")
        long, short = _number(source, "prod_merc_positions_long"), _number(source, "prod_merc_positions_short")
        by_day[report_day][code] = {
            "symbol": identities[code]["etf_symbol"],
            "contract_market_code": code,
            "contract_market_name": str(source.get("contract_market_name", "")),
            "producer_hedging_pressure": (short - long) / open_interest,
        }
    expected, signals = set(identities), []
    for report_day, rows in sorted(by_day.items()):
        if set(rows) != expected:
            continue
        ranked = sorted(rows.values(), key=lambda row: (-row["producer_hedging_pressure"], row["symbol"]))
        selected = [row["symbol"] for row in ranked[: policy["signal"]["top_count"]]]
        signal = {
            "report_date": report_day.isoformat(),
            "available_after_date": (report_day + timedelta(days=policy["signal"]["publication_embargo_calendar_days"])).isoformat(),
            "selected_symbols": selected,
            "ranking": [{"symbol": row["symbol"], "contract_market_code": row["contract_market_code"], "producer_hedging_pressure": row["producer_hedging_pressure"]} for row in ranked],
        }
        signal["signal_id"] = _canonical_hash(signal)
        signals.append(signal)
    if len(signals) < policy["signal"]["minimum_complete_reports"]:
        raise ValueError(f"only {len(signals)} complete CFTC reports; frozen minimum not met")
    manifest = {
        "version": "nodequant-cftc-flow-manifest-v1", "policy_hash": policy_hash,
        "cftc_raw_hash": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "signals": signals,
        "quality": {"source_rows": len(payload), "complete_reports": len(signals), "first_report": signals[0]["report_date"], "last_report": signals[-1]["report_date"]},
        "limitations": ["The CFTC does not publish a complete historical release-date series; every report is embargoed by the preregistered conservative lag.", "CFTC trader classifications derive from Form 40 and can change.", "Incomplete eight-contract weeks are omitted before prices are observed."],
    }
    manifest["manifest_hash"] = _canonical_hash(manifest)
    manifest["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    return manifest


def _load_manifest(path: Path, policy_hash: str) -> dict[str, Any]:
    manifest = json.loads(path.read_bytes())
    if manifest.get("policy_hash") != policy_hash:
        raise ValueError("CFTC manifest belongs to another policy")
    claimed = manifest.get("manifest_hash")
    body = {key: value for key, value in manifest.items() if key not in {"manifest_hash", "created_at_utc"}}
    if claimed != _canonical_hash(body):
        raise ValueError("CFTC manifest body does not match its declared hash")
    return manifest


def _load_authority(path: Path, policy_hash: str, manifest: dict[str, Any]) -> dict[str, Any]:
    raw = path.read_bytes(); digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_AUTHORITY_SHA256:
        raise ValueError("CFTC price authority differs from the external trust root")
    authority = json.loads(raw)
    if authority.get("policy_hash") != policy_hash or authority.get("manifest_hash") != manifest["manifest_hash"]:
        raise ValueError("CFTC price authority does not bind this policy and manifest")
    return authority


def _price_url(symbol: str, policy: dict[str, Any]) -> str:
    data = policy["price_data"]
    start = int(datetime.fromisoformat(data["download_start"] + "T00:00:00+00:00").timestamp())
    end = int(datetime.fromisoformat(data["download_end_exclusive"] + "T00:00:00+00:00").timestamp())
    query = urlencode({"period1": start, "period2": end, "interval": "1d", "events": "div,splits", "includeAdjustedClose": "true"})
    return f"https://{policy['price_provider']['host']}/v8/finance/chart/{symbol}?{query}"


def _parse_prices(raw: bytes, symbol: str, maximum_rows: int) -> dict[date, float]:
    chart = json.loads(raw).get("chart", {})
    if chart.get("error") is not None:
        raise ValueError(f"price provider returned an error for {symbol}")
    result = chart.get("result")
    if not isinstance(result, list) or len(result) != 1:
        raise ValueError(f"price provider has no single result for {symbol}")
    stamps, arrays = result[0].get("timestamp"), result[0].get("indicators", {}).get("adjclose")
    if not isinstance(stamps, list) or not isinstance(arrays, list) or len(arrays) != 1 or len(stamps) > maximum_rows:
        raise ValueError(f"price provider arrays are invalid for {symbol}")
    values = arrays[0].get("adjclose")
    if not isinstance(values, list) or len(values) != len(stamps):
        raise ValueError(f"price provider lengths differ for {symbol}")
    rows = {}
    for stamp, value in zip(stamps, values, strict=True):
        if value is None:
            continue
        day, price = datetime.fromtimestamp(stamp, timezone.utc).date(), float(value)
        if day in rows or not math.isfinite(price) or price <= 0:
            raise ValueError(f"price provider contains duplicate or invalid data for {symbol}")
        rows[day] = price
    return rows


def fetch_prices(policy: dict[str, Any], raw_directory: Path, *, refresh: bool) -> dict[str, bytes]:
    symbols = sorted(row["etf_symbol"] for row in policy["universe"])
    raw_directory.mkdir(parents=True, exist_ok=True)
    output = {}
    for symbol in symbols:
        path = raw_directory / f"{symbol}.json"
        if refresh and path.exists():
            raise ValueError(f"refusing to overwrite frozen price bytes: {path.name}")
        if refresh:
            raw = _bounded_get(_price_url(symbol, policy), policy["price_provider"])
            try:
                with path.open("xb") as handle:
                    handle.write(raw)
            except FileExistsError as exc:
                raise ValueError(f"refusing to overwrite frozen price bytes: {path.name}") from exc
        else:
            raw = path.read_bytes()
        _parse_prices(raw, symbol, policy["price_data"]["maximum_rows_per_symbol"])
        output[symbol] = raw
    return output


def _metrics(values: np.ndarray) -> dict[str, Any]:
    if len(values) < 252:
        raise ValueError("CFTC metric window is too short")
    deviation = float(np.std(values, ddof=1)); wealth = float(np.prod(1 + values))
    return {"days": len(values), "annualized_mean": float(np.mean(values) * 252), "annualized_volatility": deviation * math.sqrt(252), "sharpe": float(np.mean(values) / deviation * math.sqrt(252)) if deviation else None, "cagr": wealth ** (252 / len(values)) - 1}


def _bootstrap_lower(values: np.ndarray, policy: dict[str, Any]) -> float:
    config, length = policy["bootstrap"], len(values)
    if length < config["block_days"]:
        raise ValueError("CFTC return series is shorter than one bootstrap block")
    rng = np.random.default_rng(config["seed"]); indexes = np.arange(length); stats = np.empty(config["samples"])
    blocks = math.ceil(length / config["block_days"])
    for sample in range(config["samples"]):
        starts = rng.integers(0, length, size=blocks)
        chosen = np.concatenate([indexes[(start + np.arange(config["block_days"])) % length] for start in starts])[:length]
        stats[sample] = float(np.mean(values[chosen]) * 252)
    return float(np.quantile(stats, 0.05))


def evaluate(policy: dict[str, Any], manifest: dict[str, Any], raw: dict[str, bytes], policy_hash: str) -> dict[str, Any]:
    symbols = sorted(row["etf_symbol"] for row in policy["universe"])
    series = {symbol: _parse_prices(raw[symbol], symbol, policy["price_data"]["maximum_rows_per_symbol"]) for symbol in symbols}
    common = sorted(set.intersection(*(set(rows) for rows in series.values())))
    if len(common) < policy["price_data"]["minimum_common_rows"]:
        raise ValueError("CFTC ETF panel has too few common trading rows")
    available = sorted((date.fromisoformat(row["available_after_date"]), row) for row in manifest["signals"])
    signal_index, current = 0, None
    portfolios: dict[date, tuple[str, ...]] = {}
    for day in common:
        changed = False
        while signal_index < len(available) and day > available[signal_index][0]:
            current = available[signal_index][1]; signal_index += 1; changed = True
        if changed and current is not None:
            portfolios[day] = tuple(sorted(current["selected_symbols"]))
    if len(portfolios) < policy["evaluation"]["minimum_rebalances"]:
        raise ValueError("CFTC manifest produces too few tradable rebalances")
    sensitivities, primary = {}, None
    for cost_bps in policy["costs"]["one_way_bps_sensitivity"]:
        cost, selected, strategy, benchmark, dates = cost_bps / 10_000.0, tuple(), [], [], []
        strategy_weights = {symbol: 0.0 for symbol in symbols}; benchmark_weights = {symbol: 1 / len(symbols) for symbol in symbols}
        for index in range(1, len(common)):
            left, right = common[index - 1], common[index]
            turnover = 0.0
            if left in portfolios:
                selected = portfolios[left]
                target = {symbol: (1 / len(selected) if symbol in selected else 0.0) for symbol in symbols}
                turnover = sum(abs(target[symbol] - strategy_weights[symbol]) for symbol in symbols) / 2
                strategy_weights = target
            if not selected:
                continue
            returns = {symbol: series[symbol][right] / series[symbol][left] - 1 for symbol in symbols}
            strategy_day = sum(strategy_weights[symbol] * returns[symbol] for symbol in symbols) - turnover * cost
            benchmark_day = sum(benchmark_weights[symbol] * returns[symbol] for symbol in symbols)
            strategy.append(strategy_day); benchmark.append(benchmark_day); dates.append(right)
        strategy_array, benchmark_array = np.asarray(strategy), np.asarray(benchmark); active = strategy_array - benchmark_array
        sensitivities[str(cost_bps)] = {"strategy": _metrics(strategy_array), "benchmark": _metrics(benchmark_array), "active": _metrics(active)}
        if cost_bps == policy["costs"]["primary_one_way_bps"]:
            primary = (dates, active)
    if primary is None:
        raise ValueError("primary CFTC cost was not evaluated")
    dates, active = primary
    folds = {}
    for fold in policy["folds"]:
        mask = np.asarray([date.fromisoformat(fold["start"]) <= day <= date.fromisoformat(fold["end"]) for day in dates])
        folds[fold["id"]] = _metrics(active[mask])
    positive_folds = sum(row["annualized_mean"] > 0 for row in folds.values())
    lower = _bootstrap_lower(active, policy); count = policy["gate"]["remove_best_days"]
    keep = np.ones(len(active), dtype=bool); keep[np.argpartition(active, -count)[-count:]] = False
    reduced = _metrics(active[keep])["sharpe"]
    checks = {"bootstrap_lower_positive": lower > 0, "minimum_positive_folds": positive_folds >= policy["gate"]["minimum_positive_folds"], "remove_best_days_sharpe_positive": reduced is not None and reduced > 0}
    result = {
        "version": "nodequant-cftc-flow-result-v1", "policy_hash": policy_hash, "evaluator_hash": _evaluator_hash(), "manifest_hash": manifest["manifest_hash"],
        "data": {"raw_hashes": {key: "sha256:" + hashlib.sha256(value).hexdigest() for key, value in sorted(raw.items())}, "common_rows": len(common), "rebalance_count": len(portfolios)},
        "cost_sensitivities": sensitivities,
        "primary": {"active_annualized_mean_block_bootstrap_onesided_95pct_lower": lower, "positive_folds": positive_folds, "folds": folds, "remove_best_days_active_sharpe": reduced, "checks": checks},
        "verdict": "CFTC_HEDGING_PRESSURE_SURVIVES" if all(checks.values()) else "CFTC_HEDGING_PRESSURE_FAILS", "promotion_eligible": False, "promotion_ceiling": policy["promotion_ceiling"],
        "limitations": ["The 70-day publication embargo is conservative because the CFTC does not provide complete historical release timestamps.", "ETF adjusted closes are secondary, revisable proxies for futures and embed roll and fund effects.", "The equal-weight ETF universe is a research benchmark, not a collateralized futures implementation.", "Historical evidence cannot replace the sealed prospective packet."],
    }
    result["result_hash"] = _canonical_hash(result); result["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    return result


def _evaluator_dependencies() -> tuple[Any, ...]:
    return (_bounded_get, _cftc_url, fetch_cftc, _number, compile_manifest, _load_manifest, _load_authority, _price_url, _parse_prices, fetch_prices, _metrics, _bootstrap_lower, evaluate, _canonical_hash)


def _evaluator_hash() -> str:
    return "sha256:" + hashlib.sha256("\n".join(inspect.getsource(value) for value in _evaluator_dependencies()).encode()).hexdigest()


def load_policy(path: Path = DEFAULT_POLICY) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes(); digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_POLICY_SHA256:
        raise ValueError("CFTC flow policy differs from the preregistered trust root")
    policy = json.loads(raw)
    if policy.get("evaluator_hash") != _evaluator_hash() or policy["trial_accounting"]["candidate_count"] != 1:
        raise ValueError("CFTC flow policy is bound to another evaluator or trial slate")
    return policy, "sha256:" + digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile or evaluate the sealed CFTC hedging-pressure falsifier.")
    parser.add_argument("--stage", required=True, choices=("manifest", "evaluate")); parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--cftc-raw", type=Path, required=True); parser.add_argument("--price-raw-directory", type=Path); parser.add_argument("--output", type=Path, required=True); parser.add_argument("--manifest", type=Path); parser.add_argument("--authority", type=Path, default=DEFAULT_AUTHORITY); parser.add_argument("--reuse-raw", action="store_true")
    args = parser.parse_args(argv)
    try:
        policy, policy_hash = load_policy(args.policy)
        if args.stage == "manifest":
            result = compile_manifest(policy, fetch_cftc(policy, args.cftc_raw, refresh=not args.reuse_raw), policy_hash)
            status = {"stage": "manifest", "signals": len(result["signals"]), "manifest_hash": result["manifest_hash"]}
        else:
            if args.manifest is None or args.price_raw_directory is None:
                raise ValueError("evaluate stage requires --manifest and --price-raw-directory")
            manifest = _load_manifest(args.manifest, policy_hash); _load_authority(args.authority, policy_hash, manifest)
            result = evaluate(policy, manifest, fetch_prices(policy, args.price_raw_directory, refresh=not args.reuse_raw), policy_hash)
            status = {"stage": "evaluate", "verdict": result["verdict"], "result_hash": result["result_hash"]}
        args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(status, sort_keys=True)); return 0 if args.stage == "manifest" or status["verdict"].endswith("SURVIVES") else 5
    except (OSError, ValueError, KeyError, json.JSONDecodeError, httpx.HTTPError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, sort_keys=True), file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
