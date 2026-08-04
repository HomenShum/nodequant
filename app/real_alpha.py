"""Preregistered real-daily-data alpha search.

This is a diagnostic research rung. Yahoo adjusted closes are genuine external
observations, but they are not exchange-grade, point-in-time conformance proof.
The frozen policy hash prevents this runner from silently changing the trial
universe after outcomes are observed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import numpy as np


REPO_ROOT = Path(__file__).parents[1]
DEFAULT_POLICY = REPO_ROOT / "research" / "real_alpha_policy.json"
EXPECTED_POLICY_SHA256S = {
    "d58d2e80c14ca0aebf1a4b6ed9a3b49d9143cc130d86cbbe09e925ab2a762387",
    "3504f0b7b23d5de772890698aa60eb197eb48881445079fa0fdd28c62cb5da75",
    "e49996dc51aa67c6d6262a16c6f1f64e2e7de6d198735292ce313c2fb7806033",
}
ALLOWED_PROVIDER_HOST = "query1.finance.yahoo.com"


@dataclass(frozen=True)
class PricePanel:
    dates: tuple[date, ...]
    risky_symbols: tuple[str, ...]
    risky_prices: np.ndarray
    cash_prices: np.ndarray
    quality: dict[str, Any]
    raw_hashes: dict[str, str]


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def load_policy(path: Path = DEFAULT_POLICY) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest not in EXPECTED_POLICY_SHA256S:
        raise ValueError("real-alpha policy differs from the preregistered trust root")
    policy = json.loads(raw)
    data = policy["data"]
    symbols = data["risky_symbols"] + [data["cash_proxy_symbol"]]
    if len(symbols) != len(set(symbols)) or len(symbols) > data["maximum_symbols"]:
        raise ValueError("symbol universe violates the frozen bound")
    if policy["trial_accounting"]["registered_candidate_count"] != len(policy["candidates"]):
        raise ValueError("registered candidate count does not match the candidate universe")
    if policy["trial_accounting"]["registered_control_count"] != len(policy["controls"]):
        raise ValueError("registered control count does not match the control universe")
    return policy, "sha256:" + digest


def _bounded_get(url: str, maximum_bytes: int) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != ALLOWED_PROVIDER_HOST:
        raise ValueError("provider URL is outside the frozen HTTPS host")
    with httpx.Client(timeout=httpx.Timeout(30.0), follow_redirects=False) as client:
        with client.stream("GET", url, headers={"User-Agent": "nodequant-research/0.1"}) as response:
            response.raise_for_status()
            parts: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes(64 * 1024):
                size += len(chunk)
                if size > maximum_bytes:
                    raise ValueError("provider response exceeds the frozen byte cap")
                parts.append(chunk)
    return b"".join(parts)


def _provider_url(symbol: str, policy: dict[str, Any]) -> str:
    start = int(datetime.fromisoformat(policy["data"]["start"] + "T00:00:00+00:00").timestamp())
    end = int(datetime.fromisoformat(policy["data"]["end_exclusive"] + "T00:00:00+00:00").timestamp())
    params = urlencode({
        "period1": start,
        "period2": end,
        "interval": policy["data"]["interval"],
        "events": "div,splits",
        "includeAdjustedClose": "true",
    })
    return f"https://{ALLOWED_PROVIDER_HOST}/v8/finance/chart/{symbol}?{params}"


def _parse_symbol(raw: bytes, symbol: str, maximum_rows: int) -> tuple[dict[date, float], dict[str, int]]:
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
    if not isinstance(prices, list) or len(prices) != len(timestamps):
        raise ValueError(f"provider timestamp/price lengths differ for {symbol}")
    if len(timestamps) > maximum_rows:
        raise ValueError(f"{symbol} exceeds the frozen row cap")
    rows: dict[date, float] = {}
    null_count = 0
    duplicate_count = 0
    invalid_count = 0
    for timestamp, value in zip(timestamps, prices, strict=True):
        if value is None:
            null_count += 1
            continue
        day = datetime.fromtimestamp(timestamp, timezone.utc).date()
        price = float(value)
        if not math.isfinite(price) or price <= 0:
            invalid_count += 1
            continue
        if day in rows:
            duplicate_count += 1
            continue
        rows[day] = price
    if duplicate_count or invalid_count:
        raise ValueError(f"{symbol} contains duplicate or invalid adjusted-close rows")
    return rows, {
        "provider_rows": len(timestamps),
        "usable_rows": len(rows),
        "null_rows": null_count,
        "duplicate_rows": duplicate_count,
        "invalid_rows": invalid_count,
    }


def _panel_hash(panel: PricePanel) -> str:
    return _canonical_hash({
        "dates": [day.isoformat() for day in panel.dates],
        "risky_symbols": panel.risky_symbols,
        "risky_prices": panel.risky_prices.tolist(),
        "cash_prices": panel.cash_prices.tolist(),
    })


def fetch_panel(policy: dict[str, Any], raw_directory: Path, *, refresh: bool = True) -> PricePanel:
    data_policy = policy["data"]
    symbols = tuple(data_policy["risky_symbols"] + [data_policy["cash_proxy_symbol"]])
    raw_directory.mkdir(parents=True, exist_ok=True)
    series: dict[str, dict[date, float]] = {}
    quality_by_symbol: dict[str, dict[str, int]] = {}
    hashes: dict[str, str] = {}
    for symbol in symbols:
        raw_path = raw_directory / f"{symbol}.json"
        raw = (
            _bounded_get(_provider_url(symbol, policy), data_policy["maximum_response_bytes_per_symbol"])
            if refresh else raw_path.read_bytes()
        )
        if refresh:
            raw_path.write_bytes(raw)
        hashes[symbol] = "sha256:" + hashlib.sha256(raw).hexdigest()
        series[symbol], quality_by_symbol[symbol] = _parse_symbol(
            raw, symbol, data_policy["maximum_rows_per_symbol"]
        )
    common = sorted(set.intersection(*(set(rows) for rows in series.values())))
    if len(common) < data_policy["minimum_common_rows"]:
        raise ValueError("common price panel is below the frozen minimum row count")
    risky = tuple(data_policy["risky_symbols"])
    risky_prices = np.asarray([[series[symbol][day] for symbol in risky] for day in common], dtype=float)
    cash_prices = np.asarray([series[data_policy["cash_proxy_symbol"]][day] for day in common], dtype=float)
    quality = {
        "symbols": quality_by_symbol,
        "common_rows": len(common),
        "first_common_date": common[0].isoformat(),
        "last_common_date": common[-1].isoformat(),
        "noncommon_rows_dropped": {
            symbol: len(rows) - len(common) for symbol, rows in series.items()
        },
        "strictly_increasing_dates": all(a < b for a, b in zip(common, common[1:])),
    }
    panel = PricePanel(tuple(common), risky, risky_prices, cash_prices, quality, hashes)
    expected_hashes = data_policy.get("expected_raw_hashes")
    if expected_hashes is not None and hashes != expected_hashes:
        raise ValueError("frozen raw market-data hashes do not match")
    expected_panel_hash = data_policy.get("expected_panel_hash")
    if expected_panel_hash is not None and _panel_hash(panel) != expected_panel_hash:
        raise ValueError("normalized market-data panel does not match the frozen hash")
    return panel


def _signal(prices: np.ndarray, index: int, candidate: dict[str, Any]) -> np.ndarray:
    rule = candidate["rule"]
    lookback = candidate["lookback_days"]
    if rule == "always_long":
        return np.ones(prices.shape[1])
    if index < lookback:
        return np.zeros(prices.shape[1])
    if rule == "lookback_return_positive":
        return (prices[index] / prices[index - lookback] - 1.0 > 0).astype(float)
    if rule == "lookback_return_nonpositive":
        return (prices[index] / prices[index - lookback] - 1.0 <= 0).astype(float)
    if rule == "price_above_sma":
        return (prices[index] > prices[index - lookback + 1:index + 1].mean(axis=0)).astype(float)
    if rule == "two_of_three_lookback_returns_positive":
        votes = sum((prices[index] / prices[index - days] - 1.0 > 0) for days in (63, 126, 252))
        return (votes >= 2).astype(float)
    if rule in {"top_n_lookback", "top_n_positive_lookback", "bottom_n_lookback"}:
        momentum = prices[index] / prices[index - lookback] - 1.0
        eligible = np.ones(len(momentum), dtype=bool)
        descending = rule != "bottom_n_lookback"
        if rule == "top_n_positive_lookback":
            eligible = momentum > 0
        order = np.argsort(momentum, kind="stable")
        if descending:
            order = order[::-1]
        chosen = [asset for asset in order if eligible[asset]][:candidate["top_n"]]
        signal = np.zeros(len(momentum))
        if chosen:
            signal[chosen] = len(momentum) / len(chosen)
        return signal
    if rule == "top_n_composite_positive":
        horizons = (63, 126, 252)
        returns = np.asarray([prices[index] / prices[index - days] - 1.0 for days in horizons])
        ranks = np.empty_like(returns)
        for row_index, row in enumerate(returns):
            order = np.argsort(row, kind="stable")
            ranks[row_index, order] = np.arange(len(row))
        score = ranks.mean(axis=0)
        eligible = returns[-1] > 0
        order = np.argsort(score, kind="stable")[::-1]
        chosen = [asset for asset in order if eligible[asset]][:candidate["top_n"]]
        signal = np.zeros(prices.shape[1])
        if chosen:
            signal[chosen] = prices.shape[1] / len(chosen)
        return signal
    raise ValueError(f"unknown candidate rule: {rule}")


def positions_for(panel: PricePanel, candidate: dict[str, Any]) -> np.ndarray:
    """Monthly decisions, with a full close-to-close bar between observation and exposure."""

    rows, assets = panel.risky_prices.shape
    positions = np.zeros((rows, assets), dtype=float)
    if candidate["rule"] == "always_long":
        positions[1:] = 1.0
        return positions
    for index in range(1, rows - 1):
        positions[index + 1] = positions[index]
        if (panel.dates[index].year, panel.dates[index].month) != (
            panel.dates[index - 1].year, panel.dates[index - 1].month
        ):
            signal_index = index - 1
            positions[index + 1] = _signal(panel.risky_prices, signal_index, candidate)
    return positions


def rebalance_flags_for(panel: PricePanel, candidate: dict[str, Any]) -> np.ndarray:
    """Dates where a target is executed; weights drift on every other date."""

    flags = np.zeros(len(panel.dates), dtype=bool)
    if len(flags) > 1 and candidate["rule"] == "always_long":
        flags[1] = True
    for index in range(1, len(panel.dates) - 1):
        if (panel.dates[index].year, panel.dates[index].month) != (
            panel.dates[index - 1].year,
            panel.dates[index - 1].month,
        ):
            flags[index + 1] = True
    return flags


def returns_for(
    panel: PricePanel,
    positions: np.ndarray,
    one_way_bps: int,
    rebalance_flags: np.ndarray | None = None,
) -> dict[str, np.ndarray]:
    risky_returns = np.zeros_like(panel.risky_prices)
    risky_returns[1:] = panel.risky_prices[1:] / panel.risky_prices[:-1] - 1.0
    cash_returns = np.zeros(len(panel.dates))
    cash_returns[1:] = panel.cash_prices[1:] / panel.cash_prices[:-1] - 1.0
    targets = positions / panel.risky_prices.shape[1]
    if rebalance_flags is None:
        rebalance_flags = np.zeros(len(panel.dates), dtype=bool)
        rebalance_flags[1:] = np.any(np.abs(targets[1:] - targets[:-1]) > 1e-15, axis=1)
    if rebalance_flags.shape != (len(panel.dates),):
        raise ValueError("rebalance flags do not align with the price panel")
    turnover = np.zeros(len(panel.dates))
    gross = np.zeros(len(panel.dates))
    risky_weights = np.zeros(panel.risky_prices.shape[1])
    cash_weight = 1.0
    for index in range(1, len(panel.dates)):
        if rebalance_flags[index]:
            target = targets[index]
            turnover[index] = float(np.abs(target - risky_weights).sum())
            risky_weights = target.copy()
            cash_weight = 1.0 - float(target.sum())
        gross[index] = float(
            np.dot(risky_weights, risky_returns[index]) + cash_weight * cash_returns[index]
        )
        denominator = 1.0 + gross[index]
        if denominator <= 0:
            raise ValueError("portfolio equity became nonpositive during drift accounting")
        risky_weights = risky_weights * (1.0 + risky_returns[index]) / denominator
        cash_weight = cash_weight * (1.0 + cash_returns[index]) / denominator
    net = gross - turnover * (one_way_bps / 10_000.0)
    return {"net": net, "cash": cash_returns, "turnover": turnover}


def _mask(panel: PricePanel, start: str, end: str) -> np.ndarray:
    return np.asarray([start <= day.isoformat() <= end for day in panel.dates], dtype=bool)


def metrics(net: np.ndarray, cash: np.ndarray, turnover: np.ndarray, mask: np.ndarray) -> dict[str, Any]:
    selected = net[mask]
    selected_cash = cash[mask]
    selected_turnover = turnover[mask]
    if len(selected) < 2:
        raise ValueError("metric window has fewer than two observations")
    excess = selected - selected_cash
    standard_deviation = float(np.std(excess, ddof=1))
    sharpe = float(np.mean(excess) / standard_deviation * math.sqrt(252)) if standard_deviation else None
    equity = np.cumprod(1.0 + selected)
    peak = np.maximum.accumulate(equity)
    return {
        "days": len(selected),
        "net_cagr": float(equity[-1] ** (252 / len(selected)) - 1.0),
        "annualized_excess_return": float(np.mean(excess) * 252),
        "excess_sharpe": sharpe,
        "max_drawdown": float(np.min(equity / peak - 1.0)),
        "annualized_turnover": float(np.sum(selected_turnover) * 252 / len(selected)),
    }


def _sharpe_after_removing_best(net: np.ndarray, cash: np.ndarray, mask: np.ndarray, count: int) -> float:
    excess = (net - cash)[mask]
    if len(excess) <= count + 1:
        raise ValueError("not enough observations for best-day removal")
    keep = np.ones(len(excess), dtype=bool)
    keep[np.argpartition(excess, -count)[-count:]] = False
    reduced = excess[keep]
    deviation = float(np.std(reduced, ddof=1))
    return float(np.mean(reduced) / deviation * math.sqrt(252)) if deviation else float("-inf")


def _split_masks(panel: PricePanel, policy: dict[str, Any]) -> dict[str, np.ndarray]:
    return {row["id"]: _mask(panel, row["start"], row["end"]) for row in policy["splits"]}


def _bootstrap_active_lower(active: np.ndarray, policy: dict[str, Any]) -> float:
    config = policy["bootstrap"]
    rng = np.random.default_rng(config["seed"])
    n = len(active)
    block = config["block_days"]
    block_count = math.ceil(n / block)
    estimates = np.empty(config["samples"])
    offsets = np.arange(block)
    for sample in range(config["samples"]):
        starts = rng.integers(0, n, size=block_count)
        indexes = ((starts[:, None] + offsets) % n).reshape(-1)[:n]
        estimates[sample] = float(np.mean(active[indexes]) * 252)
    return float(np.quantile(estimates, 0.025))


def evaluate(panel: PricePanel, policy: dict[str, Any], policy_hash: str) -> dict[str, Any]:
    masks = _split_masks(panel, policy)
    development_validation = masks["DEVELOPMENT"] | masks["VALIDATION"]
    base_cost = policy["costs"]["base_one_way_bps"]
    trials: dict[str, dict[str, Any]] = {}
    streams: dict[str, dict[int, dict[str, np.ndarray]]] = {}
    universe = policy["candidates"] + policy["controls"]
    for candidate in universe:
        positions = positions_for(panel, candidate)
        rebalance_flags = rebalance_flags_for(panel, candidate)
        streams[candidate["id"]] = {}
        for cost in policy["costs"]["stress_one_way_bps"]:
            streams[candidate["id"]][cost] = returns_for(
                panel, positions, cost, rebalance_flags
            )
        base = streams[candidate["id"]][base_cost]
        stressed = streams[candidate["id"]][25]
        split_metrics = {
            split: metrics(base["net"], base["cash"], base["turnover"], mask)
            for split, mask in masks.items() if split in ("DEVELOPMENT", "VALIDATION")
        }
        robust = min(
            split_metrics["DEVELOPMENT"]["excess_sharpe"],
            split_metrics["VALIDATION"]["excess_sharpe"],
            metrics(stressed["net"], stressed["cash"], stressed["turnover"], development_validation)["excess_sharpe"],
            _sharpe_after_removing_best(base["net"], base["cash"], development_validation, 5),
        )
        trials[candidate["id"]] = {
            "candidate": candidate,
            "development_validation": split_metrics,
            "robust_excess_sharpe_floor": robust,
            "annualized_turnover": metrics(base["net"], base["cash"], base["turnover"], development_validation)["annualized_turnover"],
            "status": "REGISTERED_COMPLETE",
        }

    candidates = policy["candidates"]
    ranked = sorted(
        candidates,
        key=lambda row: (
            -trials[row["id"]]["robust_excess_sharpe_floor"],
            trials[row["id"]]["annualized_turnover"],
            row["id"],
        ),
    )
    champion_id = ranked[0]["id"]
    baseline_id = "always_long"
    improvement = (
        trials[champion_id]["robust_excess_sharpe_floor"]
        - trials[baseline_id]["robust_excess_sharpe_floor"]
    )
    selection_pass = improvement >= policy["selection"]["minimum_improvement_over_always_long"]

    champion_base = streams[champion_id][base_cost]
    baseline_base = streams[baseline_id][base_cost]
    champion_test = metrics(champion_base["net"], champion_base["cash"], champion_base["turnover"], masks["TEST"])
    baseline_test = metrics(baseline_base["net"], baseline_base["cash"], baseline_base["turnover"], masks["TEST"])
    stressed_test_stream = streams[champion_id][25]
    stressed_test = metrics(stressed_test_stream["net"], stressed_test_stream["cash"], stressed_test_stream["turnover"], masks["TEST"])

    champion_spec = next(row for row in candidates if row["id"] == champion_id)
    leave_one_out: dict[str, float | None] = {}
    for omitted_index, omitted_symbol in enumerate(panel.risky_symbols):
        keep = [index for index in range(len(panel.risky_symbols)) if index != omitted_index]
        subset = PricePanel(
            panel.dates,
            tuple(panel.risky_symbols[index] for index in keep),
            panel.risky_prices[:, keep],
            panel.cash_prices,
            panel.quality,
            panel.raw_hashes,
        )
        subset_positions = positions_for(subset, champion_spec)
        subset_returns = returns_for(
            subset,
            subset_positions,
            base_cost,
            rebalance_flags_for(subset, champion_spec),
        )
        leave_one_out[omitted_symbol] = metrics(
            subset_returns["net"], subset_returns["cash"], subset_returns["turnover"], masks["TEST"]
        )["excess_sharpe"]

    test_checks = {
        "selection_improves_frozen_floor": selection_pass,
        "test_sharpe_beats_always_long": champion_test["excess_sharpe"] > baseline_test["excess_sharpe"],
        "test_cagr_positive": champion_test["net_cagr"] >= policy["test_gate"]["net_cagr_minimum"],
        "test_drawdown_improves": champion_test["max_drawdown"] > baseline_test["max_drawdown"],
        "test_25bps_sharpe_nonnegative": stressed_test["excess_sharpe"] >= policy["test_gate"]["excess_sharpe_at_25bps_minimum"],
        "leave_one_asset_out_nonnegative": min(value for value in leave_one_out.values() if value is not None) >= policy["test_gate"]["leave_one_risky_asset_out_excess_sharpe_minimum"],
    }
    test_pass = all(test_checks.values())

    holdout: dict[str, Any] = {"status": "NOT_OPENED_TEST_GATE_FAILED"}
    if test_pass:
        champion_holdout = metrics(champion_base["net"], champion_base["cash"], champion_base["turnover"], masks["OPENED_HOLDOUT"])
        baseline_holdout = metrics(baseline_base["net"], baseline_base["cash"], baseline_base["turnover"], masks["OPENED_HOLDOUT"])
        active = (champion_base["net"] - baseline_base["net"])[masks["OPENED_HOLDOUT"]]
        bootstrap_lower = _bootstrap_active_lower(active, policy)
        holdout_checks = {
            "holdout_cagr_positive": champion_holdout["net_cagr"] >= policy["holdout_gate"]["net_cagr_minimum"],
            "holdout_excess_sharpe_nonnegative": champion_holdout["excess_sharpe"] >= policy["holdout_gate"]["excess_sharpe_minimum"],
            "active_return_bootstrap_lower_positive": bootstrap_lower >= policy["holdout_gate"]["paired_active_mean_bootstrap_lower_bound_minimum"],
        }
        holdout = {
            "status": "PASS" if all(holdout_checks.values()) else "FAIL",
            "champion": champion_holdout,
            "always_long": baseline_holdout,
            "paired_active_annual_return_bootstrap_95pct_lower": bootstrap_lower,
            "checks": holdout_checks,
        }

    measured_positive = test_pass and holdout["status"] == "PASS"
    year_slices: dict[str, Any] = {}
    last_open_date = next(
        row["end"] for row in policy["splits"]
        if row["id"] == ("OPENED_HOLDOUT" if test_pass else "TEST")
    )
    for year in sorted({day.year for day in panel.dates if day.isoformat() <= last_open_date}):
        year_mask = np.asarray([
            day.year == year and day.isoformat() <= last_open_date for day in panel.dates
        ], dtype=bool)
        if year_mask.sum() < 200:
            continue
        champion_year = metrics(
            champion_base["net"], champion_base["cash"], champion_base["turnover"], year_mask
        )
        baseline_year = metrics(
            baseline_base["net"], baseline_base["cash"], baseline_base["turnover"], year_mask
        )
        year_slices[str(year)] = {
            "champion_net_cagr": champion_year["net_cagr"],
            "always_long_net_cagr": baseline_year["net_cagr"],
            "active_annual_return": float(
                np.mean((champion_base["net"] - baseline_base["net"])[year_mask]) * 252
            ),
            "champion_excess_sharpe": champion_year["excess_sharpe"],
            "always_long_excess_sharpe": baseline_year["excess_sharpe"],
        }
    positive_active_years = sum(row["active_annual_return"] > 0 for row in year_slices.values())
    result = {
        "version": "nodequant-real-alpha-result-v1",
        "policy_hash": policy_hash,
        "data": {
            "provider_authority": policy["data"]["provider_authority"],
            "raw_hashes": panel.raw_hashes,
            "quality": panel.quality,
            "panel_hash": _panel_hash(panel),
        },
        "trial_count": len(universe),
        "trials": trials,
        "selection": {
            "champion_id": champion_id,
            "baseline_id": baseline_id,
            "robust_floor_improvement": improvement,
            "pass": selection_pass,
        },
        "test": {
            "status": "PASS" if test_pass else "FAIL",
            "champion": champion_test,
            "always_long": baseline_test,
            "champion_at_25bps": stressed_test,
            "leave_one_asset_out_excess_sharpe": leave_one_out,
            "checks": test_checks,
        },
        "opened_holdout": holdout,
        "year_slices": {
            "rows": year_slices,
            "positive_active_years": positive_active_years,
            "observed_years": len(year_slices),
        },
        "verdict": "POSITIVE_MEASURED_DIAGNOSTIC" if measured_positive else "NO_ROBUST_POSITIVE_ALPHA_FOUND",
        "promotion_eligible": False,
        "promotion_ceiling": policy["promotion_ceiling"],
        "limitations": [
            "Yahoo chart data is a secondary unofficial diagnostic source, not exchange-grade conformance evidence.",
            "Adjusted closes are total-return-like observations, not executable quotes.",
            "The cost grid excludes taxes, account-specific fees, borrow, and market impact.",
            "This is research analysis, not investment advice or a return guarantee.",
        ],
    }
    result["result_hash"] = _canonical_hash(result)
    result["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the preregistered genuine-daily-data alpha diagnostic.")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "evidence" / "real-alpha-search" / "result.json")
    parser.add_argument("--raw-directory", type=Path, default=REPO_ROOT / "evidence" / "real-alpha-search" / "raw")
    parser.add_argument("--reuse-raw", action="store_true", help="Replay existing frozen raw bytes instead of refetching.")
    args = parser.parse_args(argv)
    try:
        policy, policy_hash = load_policy(args.policy)
        panel = fetch_panel(policy, args.raw_directory, refresh=not args.reuse_raw)
        result = evaluate(panel, policy, policy_hash)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({
            "verdict": result["verdict"],
            "champion": result["selection"]["champion_id"],
            "test": result["test"]["status"],
            "holdout": result["opened_holdout"]["status"],
            "result_hash": result["result_hash"],
            "output": str(args.output),
        }, sort_keys=True))
        return 0 if result["verdict"] == "POSITIVE_MEASURED_DIAGNOSTIC" else 5
    except (OSError, ValueError, httpx.HTTPError, json.JSONDecodeError) as exc:
        print(f"real-alpha error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
