"""One preregistered Coinbase crypto rebalancing-premium falsifier."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import os
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = REPO_ROOT / "research" / "crypto_rebalancing_policy_v2.json"
EXPECTED_POLICY_SHA256 = "f7dc55096fbe29dc46e0e3587910aae5f4504849f7d4129632a505fc518a120b"
ALLOWED_HOST = "api.exchange.coinbase.com"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _canonical_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _bounded_get(url: str, timeout: int, maximum_bytes: int) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST or parsed.port not in (None, 443):
        raise ValueError("Coinbase URL is outside the frozen host")
    if not parsed.path.startswith("/products/") or not parsed.path.endswith("/candles"):
        raise ValueError("Coinbase URL is outside the candle endpoint")
    request = urllib.request.Request(url, headers={"User-Agent": "nodequant-research/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError(f"Coinbase returned HTTP {response.status}")
        raw = response.read(maximum_bytes + 1)
    if len(raw) > maximum_bytes:
        raise ValueError("Coinbase response exceeds byte bound")
    return raw


def _chunk_url(product: str, start: date, end: date) -> str:
    query = urllib.parse.urlencode({
        "granularity": 86400,
        "start": datetime.combine(start, datetime.min.time(), timezone.utc).isoformat().replace("+00:00", "Z"),
        "end": datetime.combine(end, datetime.min.time(), timezone.utc).isoformat().replace("+00:00", "Z"),
    })
    return f"https://{ALLOWED_HOST}/products/{urllib.parse.quote(product, safe='-')}/candles?{query}"


def fetch_product(policy: dict[str, Any], product: str, directory: Path, refresh: bool) -> list[bytes]:
    directory.mkdir(parents=True, exist_ok=True)
    start = date.fromisoformat(policy["data"]["start_date"])
    stop = date.fromisoformat(policy["data"]["end_exclusive"])
    chunks, cursor = [], start
    while cursor < stop:
        right = min(cursor + timedelta(days=250), stop)
        path = directory / f"{cursor.isoformat()}_{right.isoformat()}.json"
        if path.exists():
            if refresh:
                raise ValueError(f"refusing to overwrite frozen Coinbase bytes: {path.name}")
            raw = path.read_bytes()
        else:
            if not refresh:
                raise ValueError(f"frozen Coinbase chunk is absent: {path.name}")
            raw = _bounded_get(_chunk_url(product, cursor, right), policy["provider"]["timeout_seconds"], policy["provider"]["maximum_response_bytes"])
            temporary = path.with_suffix(".json.tmp")
            temporary.write_bytes(raw); os.replace(temporary, path)
        chunks.append(raw); cursor = right
    return chunks


def _parse_product(policy: dict[str, Any], chunks: list[bytes]) -> tuple[dict[date, float], list[str]]:
    start, stop = date.fromisoformat(policy["data"]["start_date"]), date.fromisoformat(policy["data"]["end_exclusive"])
    rows: dict[date, float] = {}; hashes = []
    for raw in chunks:
        hashes.append("sha256:" + hashlib.sha256(raw).hexdigest())
        value = json.loads(raw)
        if not isinstance(value, list) or len(value) > 300:
            raise ValueError("Coinbase candle response is invalid or excessive")
        for candle in value:
            if not isinstance(candle, list) or len(candle) != 6:
                raise ValueError("Coinbase candle schema differs")
            stamp, opened = int(candle[0]), float(candle[3])
            if stamp % 86400:
                raise ValueError("daily Coinbase bucket is not UTC-aligned")
            day = datetime.fromtimestamp(stamp, timezone.utc).date()
            if start <= day < stop:
                if not math.isfinite(opened) or opened <= 0:
                    raise ValueError("Coinbase contains an invalid daily open")
                if day in rows:
                    if rows[day] != opened:
                        raise ValueError("Coinbase repeated a daily bucket with conflicting opens")
                    continue
                rows[day] = opened
    return rows, hashes


def _summary(values: np.ndarray) -> dict[str, Any]:
    if len(values) < 100:
        raise ValueError("crypto active sample is too short")
    deviation = float(np.std(values, ddof=1)); mean = float(np.mean(values))
    return {"days": len(values), "annual_mean": mean * 365, "annual_volatility": deviation * math.sqrt(365),
            "annual_sharpe": mean / deviation * math.sqrt(365) if deviation else None,
            "positive_fraction": float(np.mean(values > 0))}


def _bootstrap_lower(values: np.ndarray, policy: dict[str, Any]) -> float:
    config, length = policy["bootstrap"], len(values); indexes = np.arange(length)
    rng = np.random.default_rng(config["seed"]); stats = np.empty(config["samples"])
    blocks = math.ceil(length / config["block_days"])
    for sample in range(config["samples"]):
        starts = rng.integers(0, length, size=blocks)
        chosen = np.concatenate([indexes[(start + np.arange(config["block_days"])) % length] for start in starts])[:length]
        stats[sample] = float(np.mean(values[chosen]) * 365)
    return float(np.quantile(stats, config["quantile"]))


def _path(prices: np.ndarray, dates: list[date], one_way_bps: int) -> tuple[np.ndarray, np.ndarray, float, float, float]:
    count = prices.shape[1]; equal = np.full(count, 1 / count); strategy = equal.copy(); benchmark = equal.copy()
    strategy_value = benchmark_value = 1.0; active, turnovers = [], []
    for index in range(len(dates) - 1):
        if index and dates[index].month != dates[index - 1].month:
            turnover = float(np.abs(strategy - equal).sum() / 2); strategy = equal.copy()
        else:
            turnover = 0.0
        relatives = prices[index + 1] / prices[index]
        strategy_return = float(strategy @ relatives - 1) - turnover * one_way_bps / 10_000
        benchmark_return = float(benchmark @ relatives - 1)
        active.append(strategy_return - benchmark_return); turnovers.append(turnover)
        strategy_value *= 1 + strategy_return; benchmark_value *= 1 + benchmark_return
        strategy = strategy * relatives; strategy /= strategy.sum()
        benchmark = benchmark * relatives; benchmark /= benchmark.sum()
    years = (dates[-1] - dates[0]).days / 365.2425
    return np.asarray(active), np.asarray(turnovers), strategy_value ** (1 / years) - 1, benchmark_value ** (1 / years) - 1, float(np.sum(turnovers) / years)


def evaluate(policy: dict[str, Any], raw_by_product: dict[str, list[bytes]], policy_hash: str) -> dict[str, Any]:
    parsed = {}; raw_hashes = {}
    for product in policy["data"]["products"]:
        parsed[product], raw_hashes[product] = _parse_product(policy, raw_by_product[product])
    common = sorted(set.intersection(*(set(rows) for rows in parsed.values())))
    expected = (date.fromisoformat(policy["data"]["end_exclusive"]) - date.fromisoformat(policy["data"]["start_date"])).days
    if len(common) != expected or len(common) < policy["data"]["minimum_common_days"]:
        raise ValueError(f"only {len(common)} of {expected} required UTC daily opens are common")
    prices = np.asarray([[parsed[product][day] for product in policy["data"]["products"]] for day in common])
    sensitivities, primary = {}, None
    for cost in policy["costs"]["one_way_bps_sensitivity"]:
        active, turnover, strategy_cagr, benchmark_cagr, annual_turnover = _path(prices, common, cost)
        row = {"active": _summary(active), "strategy_cagr": strategy_cagr, "buy_hold_cagr": benchmark_cagr,
               "annual_one_way_turnover": annual_turnover}
        sensitivities[str(cost)] = row
        if cost == policy["costs"]["primary_one_way_bps"]: primary = (active, turnover, row)
    if primary is None: raise ValueError("primary crypto cost was not evaluated")
    active, _, primary_row = primary; outcome_dates = common[1:]
    folds = {}
    for fold in policy["folds"]:
        mask = np.asarray([date.fromisoformat(fold["start"]) <= day <= date.fromisoformat(fold["end"]) for day in outcome_dates])
        folds[fold["id"]] = _summary(active[mask])
    remove = policy["gate"]["remove_best_days"]; keep = np.ones(len(active), dtype=bool)
    keep[np.argpartition(active, -remove)[-remove:]] = False
    reduced = _summary(active[keep])["annual_sharpe"]; lower = _bootstrap_lower(active, policy)
    positive_folds = sum(row["annual_mean"] > 0 for row in folds.values())
    checks = {"bootstrap_lower_positive": lower > 0, "minimum_positive_folds": positive_folds >= policy["gate"]["minimum_positive_folds"],
              "remove_best_active_sharpe_positive": reduced is not None and reduced > 0}
    result = {"version": "nodequant-crypto-rebalancing-result-v1", "policy_hash": policy_hash,
              "evaluator_hash": _evaluator_hash(), "raw_hashes": raw_hashes,
              "data": {"products": policy["data"]["products"], "common_days": len(common), "first_open": common[0].isoformat(), "last_open": common[-1].isoformat()},
              "cost_sensitivities": sensitivities,
              "primary": {"annual_active_mean_block_onesided_95pct_lower": lower, "folds": folds, "positive_folds": positive_folds,
                          "remove_best_10_active_sharpe": reduced, "checks": checks, **primary_row},
              "verdict": "CRYPTO_REBALANCING_PREMIUM_SURVIVES" if all(checks.values()) else "CRYPTO_REBALANCING_PREMIUM_FAILS",
              "promotion_eligible": False, "promotion_ceiling": policy["promotion_ceiling"],
              "limitations": ["Current-availability fixed universe creates survivorship and listing-selection bias.", "Coinbase daily opens are first trades, not executable quotes.", "Fees, spread, slippage, taxes, custody, staking yield, capacity and delistings are not fully represented."]}
    result["result_hash"] = _canonical_hash(result); result["created_at_utc"] = datetime.now(timezone.utc).isoformat(); return result


def _evaluator_dependencies() -> tuple[Any, ...]:
    return (_bounded_get, _chunk_url, fetch_product, _parse_product, _summary, _bootstrap_lower, _path, evaluate, _canonical_hash)


def _evaluator_hash() -> str:
    return "sha256:" + hashlib.sha256("\n".join(inspect.getsource(value) for value in _evaluator_dependencies()).encode()).hexdigest()


def load_policy(path: Path = DEFAULT_POLICY) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes(); digest = hashlib.sha256(raw).hexdigest()
    if EXPECTED_POLICY_SHA256 != "PENDING" and digest != EXPECTED_POLICY_SHA256:
        raise ValueError("crypto rebalancing policy differs from preregistered trust root")
    value = json.loads(raw)
    if value["evaluator_hash"] != _evaluator_hash() or value["trial_accounting"]["candidate_count"] != 1:
        raise ValueError("crypto rebalancing policy belongs to another evaluator or trial slate")
    return value, "sha256:" + digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--raw-directory", type=Path, required=True); parser.add_argument("--output", type=Path, required=True); parser.add_argument("--reuse-raw", action="store_true")
    args = parser.parse_args(argv)
    try:
        policy, policy_hash = load_policy(args.policy); raw = {}
        for product in policy["data"]["products"]:
            raw[product] = fetch_product(policy, product, args.raw_directory / product, refresh=not args.reuse_raw)
        result = evaluate(policy, raw, policy_hash)
        if args.output.exists(): raise ValueError("refusing to overwrite crypto rebalancing result")
        args.output.parent.mkdir(parents=True, exist_ok=True); temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8"); os.replace(temporary, args.output)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "detail": str(exc)})); return 2
    print(json.dumps({"ok": True, "verdict": result["verdict"], "result_hash": result["result_hash"]})); return 0


if __name__ == "__main__":
    raise SystemExit(main())
