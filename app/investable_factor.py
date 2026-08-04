"""Frozen investability falsifier for the published-factor persistence result.

This is deliberately one portfolio, not another optimizer.  It asks whether an
equal MTUM/QUAL/VLUE implementation beat SPY after observable fund returns and
explicit turnover charges.  Yahoo adjusted closes are secondary research data;
the result cannot promote capital or certify execution.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import numpy as np

from .real_alpha import _canonical_hash


REPO_ROOT = Path(__file__).parents[1]
DEFAULT_POLICY = REPO_ROOT / "research" / "investable_factor_policy_v1.json"
DEFAULT_AUTHORITY = REPO_ROOT / "research" / "investable_factor_authority_v1.json"
EXPECTED_POLICY_SHA256 = "90d50e7f96ceceb791ba3f3574d7fea57a7c10393143af0a1569b80380034aa2"
EXPECTED_AUTHORITY_SHA256 = "b5883eeb0b436176575e7c2af36fd42ee3a9a1d7781b84b293ea5eb96456debb"


def _bounded_get(url: str, provider: dict[str, Any]) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != provider["host"] or parsed.fragment:
        raise ValueError("ETF provider URL is outside the frozen HTTPS host")
    with httpx.Client(timeout=httpx.Timeout(provider["timeout_seconds"]), follow_redirects=False) as client:
        with client.stream("GET", url, headers={"User-Agent": "nodequant-research/0.1"}) as response:
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_bytes(64 * 1024):
                size += len(chunk)
                if size > provider["maximum_response_bytes_per_symbol"]:
                    raise ValueError("ETF provider response exceeds the frozen byte cap")
                chunks.append(chunk)
    return b"".join(chunks)


def _provider_url(symbol: str, policy: dict[str, Any]) -> str:
    data = policy["data"]
    start = int(datetime.fromisoformat(data["download_start"] + "T00:00:00+00:00").timestamp())
    end = int(datetime.fromisoformat(data["download_end_exclusive"] + "T00:00:00+00:00").timestamp())
    query = urlencode({
        "period1": start,
        "period2": end,
        "interval": "1d",
        "events": "div,splits",
        "includeAdjustedClose": "true",
    })
    return f"https://{policy['provider']['host']}/v8/finance/chart/{symbol}?{query}"


def _parse_adjusted_close(raw: bytes, symbol: str, maximum_rows: int) -> tuple[dict[date, float], dict[str, int]]:
    payload = json.loads(raw)
    chart = payload.get("chart", {})
    if chart.get("error") is not None:
        raise ValueError(f"provider returned an error for {symbol}: {chart['error']}")
    results = chart.get("result")
    if not isinstance(results, list) or len(results) != 1:
        raise ValueError(f"provider returned an unexpected result count for {symbol}")
    result = results[0]
    timestamps = result.get("timestamp")
    indicators = result.get("indicators", {}).get("adjclose")
    if not isinstance(timestamps, list) or not isinstance(indicators, list) or len(indicators) != 1:
        raise ValueError(f"provider omitted adjusted-close arrays for {symbol}")
    prices = indicators[0].get("adjclose")
    if not isinstance(prices, list) or len(prices) != len(timestamps) or len(prices) > maximum_rows:
        raise ValueError(f"provider returned invalid or excessive rows for {symbol}")
    rows: dict[date, float] = {}
    quality = {"provider_rows": len(prices), "usable_rows": 0, "null_rows": 0, "duplicate_rows": 0, "invalid_rows": 0}
    for stamp, value in zip(timestamps, prices, strict=True):
        if value is None:
            quality["null_rows"] += 1
            continue
        day, price = datetime.fromtimestamp(stamp, timezone.utc).date(), float(value)
        if not math.isfinite(price) or price <= 0:
            quality["invalid_rows"] += 1
            continue
        if day in rows:
            quality["duplicate_rows"] += 1
            continue
        rows[day] = price
    quality["usable_rows"] = len(rows)
    if quality["duplicate_rows"] or quality["invalid_rows"]:
        raise ValueError(f"{symbol} contains duplicate or invalid adjusted-close rows")
    return rows, quality


def fetch_raw(policy: dict[str, Any], raw_directory: Path, *, refresh: bool) -> dict[str, bytes]:
    raw_directory.mkdir(parents=True, exist_ok=True)
    rows: dict[str, bytes] = {}
    for symbol in policy["data"]["symbols"]:
        path = raw_directory / f"{symbol}.json"
        if refresh:
            raw = _bounded_get(_provider_url(symbol, policy), policy["provider"])
            try:
                with path.open("xb") as handle:
                    handle.write(raw)
            except FileExistsError as exc:
                raise ValueError(f"refusing to overwrite frozen ETF bytes: {path.name}") from exc
        else:
            raw = path.read_bytes()
        rows[symbol] = raw
    return rows


def build_monthly_panel(policy: dict[str, Any], raw: dict[str, bytes]) -> dict[str, Any]:
    series, quality, raw_hashes = {}, {}, {}
    for symbol in policy["data"]["symbols"]:
        raw_hashes[symbol] = "sha256:" + hashlib.sha256(raw[symbol]).hexdigest()
        series[symbol], quality[symbol] = _parse_adjusted_close(
            raw[symbol], symbol, policy["provider"]["maximum_rows_per_symbol"]
        )
    common = sorted(set.intersection(*(set(values) for values in series.values())))
    month_ends: dict[str, date] = {}
    for day in common:
        month_ends[f"{day.year:04d}{day.month:02d}"] = day
    start, end = policy["evaluation"]["required_first_month_end"], policy["evaluation"]["end_month"]
    months = [month for month in sorted(month_ends) if start <= month <= end]
    expected = []
    year, month = int(start[:4]), int(start[4:])
    while f"{year:04d}{month:02d}" <= end:
        expected.append(f"{year:04d}{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    if months != expected:
        raise ValueError(f"ETF panel lacks frozen month ends: {sorted(set(expected) - set(months))[:5]}")
    symbols = policy["data"]["symbols"]
    values = np.asarray([[series[symbol][month_ends[row]] for symbol in symbols] for row in months], dtype=float)
    if not np.isfinite(values).all() or np.any(values <= 0):
        raise ValueError("ETF panel contains invalid normalized prices")
    panel = {
        "months": months,
        "month_end_dates": [month_ends[row].isoformat() for row in months],
        "symbols": symbols,
        "prices": values,
        "raw_hashes": raw_hashes,
        "quality": {
            "symbols": quality,
            "common_daily_rows": len(common),
            "monthly_rows": len(months),
            "first_month_end": month_ends[months[0]].isoformat(),
            "last_month_end": month_ends[months[-1]].isoformat(),
            "missing_months": 0,
        },
    }
    panel["panel_hash"] = _canonical_hash({key: panel[key] for key in ("months", "month_end_dates", "symbols")} | {"prices": values.tolist()})
    return panel


def _metrics(returns: np.ndarray) -> dict[str, float | int | None]:
    if len(returns) < 12:
        raise ValueError("ETF metric window is too short")
    deviation = float(np.std(returns, ddof=1))
    wealth = float(np.prod(1.0 + returns))
    return {
        "months": len(returns),
        "annualized_mean": float(np.mean(returns) * 12),
        "annualized_volatility": deviation * math.sqrt(12),
        "sharpe": float(np.mean(returns) / deviation * math.sqrt(12)) if deviation else None,
        "cagr": wealth ** (12 / len(returns)) - 1,
    }


def _remove_best_sharpe(active: np.ndarray, count: int) -> float:
    keep = np.ones(len(active), dtype=bool)
    keep[np.argpartition(active, -count)[-count:]] = False
    reduced = active[keep]
    deviation = float(np.std(reduced, ddof=1))
    return float(np.mean(reduced) / deviation * math.sqrt(12)) if deviation else float("-inf")


def _bootstrap_lower(active: np.ndarray, policy: dict[str, Any]) -> float:
    config, length = policy["bootstrap"], len(active)
    rng = np.random.default_rng(config["seed"])
    block, observed = config["block_months"], float(np.mean(active))
    count = math.ceil(length / block)
    statistics = np.empty(config["samples"])
    for sample in range(config["samples"]):
        starts = rng.integers(0, length, size=count)
        indexes = ((starts[:, None] + np.arange(block)) % length).reshape(-1)[:length]
        statistics[sample] = float(np.mean(active[indexes]))
    return float(np.quantile(statistics, 0.05) * 12), observed * 12


def _portfolio_returns(panel: dict[str, Any], policy: dict[str, Any], one_way_bps: int) -> dict[str, Any]:
    factor_symbols = policy["portfolio"]["factor_symbols"]
    indexes = [panel["symbols"].index(symbol) for symbol in factor_symbols]
    benchmark_index = panel["symbols"].index(policy["portfolio"]["benchmark_symbol"])
    target = np.full(len(indexes), 1.0 / len(indexes))
    months, factor_rows, benchmark_rows, active_rows, turnovers = [], [], [], [], []
    cost_rate = one_way_bps / 10_000.0
    for index in range(1, len(panel["months"])):
        asset_returns = panel["prices"][index, indexes] / panel["prices"][index - 1, indexes] - 1.0
        gross_factor = float(np.dot(target, asset_returns))
        gross_value = 1.0 + gross_factor
        drifted = target * (1.0 + asset_returns) / gross_value
        end_turnover = 1.0 if index == len(panel["months"]) - 1 else float(0.5 * np.abs(target - drifted).sum())
        initial_turnover = 1.0 if index == 1 else 0.0
        total_turnover = initial_turnover + end_turnover
        net_factor = (1.0 - initial_turnover * cost_rate) * gross_value * (1.0 - end_turnover * cost_rate) - 1.0
        gross_benchmark = float(panel["prices"][index, benchmark_index] / panel["prices"][index - 1, benchmark_index] - 1.0)
        benchmark_turnover = initial_turnover + (1.0 if index == len(panel["months"]) - 1 else 0.0)
        net_benchmark = (1.0 - initial_turnover * cost_rate) * (1.0 + gross_benchmark) * (1.0 - (benchmark_turnover - initial_turnover) * cost_rate) - 1.0
        months.append(panel["months"][index])
        factor_rows.append(net_factor)
        benchmark_rows.append(net_benchmark)
        active_rows.append(net_factor - net_benchmark)
        turnovers.append(total_turnover)
    return {
        "months": months,
        "factor": np.asarray(factor_rows),
        "benchmark": np.asarray(benchmark_rows),
        "active": np.asarray(active_rows),
        "turnover": np.asarray(turnovers),
    }


def evaluate(panel: dict[str, Any], policy: dict[str, Any], policy_hash: str) -> dict[str, Any]:
    sensitivities = {}
    for cost in policy["costs"]["one_way_bps_sensitivity"]:
        returns = _portfolio_returns(panel, policy, cost)
        sensitivities[str(cost)] = {
            "factor": _metrics(returns["factor"]),
            "benchmark": _metrics(returns["benchmark"]),
            "active": _metrics(returns["active"]),
            "annual_turnover": float(np.mean(returns["turnover"]) * 12),
        }
    primary_cost = policy["costs"]["primary_one_way_bps"]
    primary = _portfolio_returns(panel, policy, primary_cost)
    folds = {}
    for fold in policy["folds"]:
        mask = np.asarray([fold["start"] <= month <= fold["end"] for month in primary["months"]])
        folds[fold["id"]] = _metrics(primary["active"][mask])
    positive_folds = sum(row["annualized_mean"] > 0 for row in folds.values())
    lower, observed = _bootstrap_lower(primary["active"], policy)
    remove_best = _remove_best_sharpe(primary["active"], policy["gate"]["remove_best_months"])
    checks = {
        "block_bootstrap_lower_positive": lower > policy["gate"]["annualized_active_mean_lower_must_exceed"],
        "minimum_positive_folds": positive_folds >= policy["gate"]["minimum_positive_folds"],
        "remove_best_months_sharpe_positive": remove_best > policy["gate"]["remove_best_months_sharpe_must_exceed"],
    }
    records, factor_wealth, benchmark_wealth = [], 1.0, 1.0
    for month, factor, benchmark, active, turnover in zip(primary["months"], primary["factor"], primary["benchmark"], primary["active"], primary["turnover"], strict=True):
        factor_wealth *= 1.0 + float(factor)
        benchmark_wealth *= 1.0 + float(benchmark)
        records.append({"month": month, "factor_return": float(factor), "benchmark_return": float(benchmark), "active_return": float(active), "factor_wealth": factor_wealth, "benchmark_wealth": benchmark_wealth, "turnover": float(turnover)})
    result = {
        "version": "nodequant-investable-factor-result-v1",
        "policy_hash": policy_hash,
        "evaluator_hash": _evaluator_hash(),
        "data": {"panel_hash": panel["panel_hash"], "raw_hashes": panel["raw_hashes"], "quality": panel["quality"]},
        "portfolio": policy["portfolio"],
        "cost_sensitivities": sensitivities,
        "primary": {
            "one_way_bps": primary_cost,
            "active_annualized_mean": observed,
            "active_annualized_mean_circular_block_bootstrap_onesided_95pct_lower": lower,
            "positive_folds": positive_folds,
            "folds": folds,
            "remove_best_12_months_active_sharpe": remove_best,
            "checks": checks,
        },
        "monthly_records": records,
        "verdict": "INVESTABLE_FACTOR_PROXY_SURVIVES" if all(checks.values()) else "INVESTABLE_FACTOR_PROXY_FAILS",
        "promotion_eligible": False,
        "promotion_ceiling": policy["promotion_ceiling"],
        "limitations": [
            "Yahoo adjusted closes are secondary, revisable observations rather than exchange-grade point-in-time records.",
            "Adjusted-close returns already reflect fund operating expenses; issuer expense ratios are disclosures and are not subtracted twice.",
            "The ETF evaluation period overlaps the published-factor research period, so this tests translation and costs rather than independent alpha persistence.",
            "Close-to-close turnover charges do not measure spreads, slippage, taxes, market impact, or capacity.",
            "One equal-weight proxy cannot represent every implementable value, quality, and momentum construction.",
        ],
    }
    result["result_hash"] = _canonical_hash(result)
    result["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    return result


def _load_authority(path: Path, policy_hash: str) -> dict[str, Any]:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != EXPECTED_AUTHORITY_SHA256:
        raise ValueError("ETF experiment authority differs from the frozen trust root")
    authority = json.loads(raw)
    if authority.get("policy_hash") != policy_hash or authority.get("evaluator_hash") != _evaluator_hash() or authority.get("return_bytes_state") != "UNREAD_AT_AUTHORITY_FREEZE":
        raise ValueError("ETF experiment authority is not bound to this unopened experiment")
    return authority


def _evaluator_dependencies() -> tuple[Any, ...]:
    return (_bounded_get, _provider_url, _parse_adjusted_close, fetch_raw, build_monthly_panel, _metrics, _remove_best_sharpe, _bootstrap_lower, _portfolio_returns, evaluate, _load_authority, _canonical_hash)


def _evaluator_hash() -> str:
    source = "\n".join(inspect.getsource(value) for value in _evaluator_dependencies())
    return "sha256:" + hashlib.sha256(source.encode()).hexdigest()


def load_policy(path: Path = DEFAULT_POLICY) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_POLICY_SHA256:
        raise ValueError("ETF experiment policy differs from the preregistered trust root")
    policy = json.loads(raw)
    if policy.get("evaluator_hash") != _evaluator_hash() or policy["trial_accounting"]["candidate_count"] != 1:
        raise ValueError("ETF experiment policy is bound to another evaluator or trial slate")
    return policy, "sha256:" + digest


def render_svg(result: dict[str, Any]) -> str:
    records = result["monthly_records"]
    width, height, left, top, plot_width, plot_height = 1200, 720, 90, 205, 1040, 380
    values = [row["factor_wealth"] for row in records] + [row["benchmark_wealth"] for row in records]
    low, high = min(values), max(values)
    span = high - low or 1.0
    def points(field: str) -> str:
        return " ".join(f"{left + index * plot_width / (len(records) - 1):.1f},{top + (high - row[field]) * plot_height / span:.1f}" for index, row in enumerate(records))
    primary = result["primary"]
    status_color = "#2dd4bf" if result["verdict"].endswith("SURVIVES") else "#fb7185"
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="1200" height="720" fill="#07111f"/><text x="70" y="58" fill="#e6f1ff" font-family="Segoe UI" font-size="30" font-weight="700">Investable factor proxy · frozen one-primary falsifier</text>
<text x="70" y="92" fill="#8ea6bf" font-family="Segoe UI" font-size="17">Equal MTUM / QUAL / VLUE vs SPY · 2014–2025 · adjusted close · 10 bp one-way</text>
<rect x="70" y="118" width="1060" height="62" rx="10" fill="#0d2034" stroke="{status_color}"/><text x="92" y="157" fill="{status_color}" font-family="Consolas" font-size="22" font-weight="700">{result['verdict']}</text>
<text x="790" y="145" fill="#d4e4f3" font-family="Segoe UI" font-size="15">active mean {primary['active_annualized_mean']:.2%}</text><text x="790" y="168" fill="#d4e4f3" font-family="Segoe UI" font-size="15">95% block lower {primary['active_annualized_mean_circular_block_bootstrap_onesided_95pct_lower']:.2%}</text>
<rect x="70" y="190" width="1060" height="420" rx="10" fill="#091827" stroke="#17324c"/><polyline points="{points('factor_wealth')}" fill="none" stroke="#2dd4bf" stroke-width="4"/><polyline points="{points('benchmark_wealth')}" fill="none" stroke="#fbbf24" stroke-width="3"/>
<text x="92" y="232" fill="#2dd4bf" font-family="Segoe UI" font-size="16">factor proxy</text><text x="220" y="232" fill="#fbbf24" font-family="Segoe UI" font-size="16">SPY</text><text x="92" y="635" fill="#8ea6bf" font-family="Segoe UI" font-size="14">Secondary/revisable data · translation diagnostic only · not promotion or investment advice</text>
<text x="92" y="674" fill="#d4e4f3" font-family="Segoe UI" font-size="15">Folds positive {primary['positive_folds']}/4 · remove-best-12 Sharpe {primary['remove_best_12_months_active_sharpe']:.2f} · result {result['result_hash'][7:23]}</text>
</svg>'''


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the frozen one-primary investable-factor falsifier.")
    parser.add_argument("--raw-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chart", type=Path)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--authority", type=Path, default=DEFAULT_AUTHORITY)
    parser.add_argument("--reuse-raw", action="store_true")
    args = parser.parse_args(argv)
    try:
        policy, policy_hash = load_policy(args.policy)
        _load_authority(args.authority, policy_hash)
        raw = fetch_raw(policy, args.raw_directory, refresh=not args.reuse_raw)
        result = evaluate(build_monthly_panel(policy, raw), policy, policy_hash)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if args.chart:
            args.chart.parent.mkdir(parents=True, exist_ok=True)
            args.chart.write_text(render_svg(result), encoding="utf-8")
        print(json.dumps({"verdict": result["verdict"], "result_hash": result["result_hash"], "promotion_eligible": False}, sort_keys=True))
        return 0 if result["verdict"].endswith("SURVIVES") else 5
    except (OSError, ValueError, KeyError, json.JSONDecodeError, httpx.HTTPError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
