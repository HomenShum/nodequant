"""Sealed BTC discovery to ETH replication for UTC cryptocurrency seasonality."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import sys
import time as time_module
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import numpy as np

from .real_alpha import _canonical_hash


REPO_ROOT = Path(__file__).parents[1]
DEFAULT_POLICY = REPO_ROOT / "research" / "crypto_seasonality_policy_v2.json"
EXPECTED_POLICY_SHA256 = "9748f3b15e755642192ea7595777edb1e1e949d8f5455f8d25842fa7a5499433"
EXPECTED_REPLICATION_AUTHORITY_SHA256 = "b0f44558da11639eda00fe50a65809f87491f751646ba0635312fa94d54c63ca"


@dataclass(frozen=True)
class HourlyPanel:
    timestamps: tuple[datetime, ...]
    closes: np.ndarray
    quality: dict[str, Any]
    raw_hashes: dict[str, str]
    product_id: str


def load_policy(path: Path = DEFAULT_POLICY) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_POLICY_SHA256:
        raise ValueError("crypto seasonality policy differs from its preregistered trust root")
    policy = json.loads(raw)
    if policy.get("evaluator_hash") != _evaluator_hash():
        raise ValueError("crypto seasonality policy is bound to another evaluator")
    if len(expand_candidates(policy)) != policy["trial_accounting"]["registered_discovery_candidates_including_baseline"]:
        raise ValueError("crypto candidate count differs from frozen trial accounting")
    return policy, "sha256:" + digest


def expand_candidates(policy: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [{"id": "always_long", "family": "baseline", "rule": "always_long"}]
    for family in policy["candidate_families"]:
        if family["rule"] == "utc_session":
            for duration in family["duration_hours"]:
                for start in family["start_hours"]:
                    rows.append({"id": f"{family['id']}__start-{start}_duration-{duration}", "family": family["id"], "rule": "utc_session", "start_hour": start, "duration_hours": duration})
        elif family["rule"] == "weekday":
            rows.extend({"id": f"weekday__day-{day}", "family": family["id"], "rule": "weekday", "weekday": day} for day in family["weekdays"])
        elif family["rule"] == "week_part":
            rows.extend({"id": f"week_part__{part}", "family": family["id"], "rule": "week_part", "part": part} for part in family["parts"])
        else:
            raise ValueError(f"unknown crypto family rule: {family['rule']}")
    return rows


def _bounded_get(url: str, provider: dict[str, Any]) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != provider["host"]:
        raise ValueError("crypto provider URL is outside the frozen HTTPS host")
    last_error: Exception | None = None
    for attempt in range(provider["maximum_retries"]):
        try:
            with httpx.Client(timeout=provider["timeout_seconds"], follow_redirects=False) as client:
                with client.stream("GET", url, headers={"User-Agent": "nodequant-research/0.1"}) as response:
                    if response.status_code == 429:
                        raise httpx.HTTPStatusError("rate limited", request=response.request, response=response)
                    response.raise_for_status()
                    parts, size = [], 0
                    for chunk in response.iter_bytes(64 * 1024):
                        size += len(chunk)
                        if size > provider["maximum_response_bytes"]:
                            raise ValueError("crypto provider response exceeds frozen byte cap")
                        parts.append(chunk)
                    return b"".join(parts)
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt + 1 == provider["maximum_retries"]:
                raise
            time_module.sleep(min(4.0, 0.5 * (2 ** attempt)))
    raise RuntimeError("unreachable crypto retry state") from last_error


def _chunks(data: dict[str, Any], hours: int) -> list[tuple[datetime, datetime]]:
    start = datetime.fromisoformat(data["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(data["end_exclusive"].replace("Z", "+00:00"))
    rows = []
    cursor = start
    while cursor < end:
        following = min(end, cursor + timedelta(hours=hours))
        rows.append((cursor, following))
        cursor = following
    return rows


def _url(product: str, start: datetime, end: datetime, provider: dict[str, Any]) -> str:
    params = urlencode({"granularity": provider["granularity_seconds"], "start": start.isoformat().replace("+00:00", "Z"), "end": end.isoformat().replace("+00:00", "Z")})
    return f"https://{provider['host']}{provider['endpoint'].format(product_id=product)}?{params}"


def fetch_panel(policy: dict[str, Any], data_key: str, raw_directory: Path, *, refresh: bool) -> HourlyPanel:
    provider, data = policy["provider"], policy[data_key]
    raw_directory.mkdir(parents=True, exist_ok=True)
    candles: dict[int, float] = {}
    hashes: dict[str, str] = {}
    duplicate_rows = invalid_rows = outside_rows = 0
    chunks = _chunks(data, provider["chunk_hours"])
    for index, (start, end) in enumerate(chunks):
        path = raw_directory / f"{index:04d}.json"
        raw = _bounded_get(_url(data["product_id"], start, end, provider), provider) if refresh else path.read_bytes()
        if refresh:
            path.write_bytes(raw)
            time_module.sleep(provider["minimum_request_interval_seconds"])
        hashes[path.name] = "sha256:" + hashlib.sha256(raw).hexdigest()
        payload = json.loads(raw)
        if not isinstance(payload, list):
            raise ValueError("crypto provider response is not a candle array")
        lower, upper = int(start.timestamp()), int(end.timestamp())
        for row in payload:
            if not isinstance(row, list) or len(row) != 6:
                invalid_rows += 1
                continue
            stamp, close = int(row[0]), float(row[4])
            if stamp < lower or stamp >= upper:
                outside_rows += 1
                continue
            if stamp % provider["granularity_seconds"] or not math.isfinite(close) or close <= 0:
                invalid_rows += 1
                continue
            if stamp in candles:
                duplicate_rows += 1
                continue
            candles[stamp] = close
    expected_start = int(datetime.fromisoformat(data["start"].replace("Z", "+00:00")).timestamp())
    expected_end = int(datetime.fromisoformat(data["end_exclusive"].replace("Z", "+00:00")).timestamp())
    expected = list(range(expected_start, expected_end, provider["granularity_seconds"]))
    missing = [stamp for stamp in expected if stamp not in candles]
    completeness = 1.0 - len(missing) / len(expected)
    if invalid_rows or duplicate_rows or completeness < data["minimum_completeness"]:
        raise ValueError("crypto candle panel violates frozen quality bounds")
    usable = [stamp for stamp in expected if stamp in candles]
    panel = HourlyPanel(
        tuple(datetime.fromtimestamp(stamp + provider["granularity_seconds"], timezone.utc) for stamp in usable),
        np.asarray([candles[stamp] for stamp in usable], dtype=float),
        {"timestamp_semantics": "candle_close_instant_derived_from_bucket_start_plus_granularity", "expected_hours": len(expected), "usable_hours": len(usable), "missing_hours": len(missing), "completeness": completeness, "duplicate_rows": duplicate_rows, "invalid_rows": invalid_rows, "outside_rows_ignored": outside_rows, "first_bucket_start": datetime.fromtimestamp(usable[0], timezone.utc).isoformat(), "last_bucket_start": datetime.fromtimestamp(usable[-1], timezone.utc).isoformat(), "first_timestamp": datetime.fromtimestamp(usable[0] + provider["granularity_seconds"], timezone.utc).isoformat(), "last_timestamp": datetime.fromtimestamp(usable[-1] + provider["granularity_seconds"], timezone.utc).isoformat(), "chunk_count": len(chunks)},
        hashes,
        data["product_id"],
    )
    return panel


def _panel_hash(panel: HourlyPanel) -> str:
    return _canonical_hash({"product_id": panel.product_id, "timestamps": [row.isoformat() for row in panel.timestamps], "closes": panel.closes.tolist()})


def positions_for(panel: HourlyPanel, candidate: dict[str, Any]) -> np.ndarray:
    positions = np.zeros(len(panel.timestamps))
    if candidate["rule"] == "always_long":
        positions[1:] = 1.0
        return positions
    for index in range(1, len(panel.timestamps)):
        start = panel.timestamps[index - 1]
        if panel.timestamps[index] - start != timedelta(hours=1):
            continue
        if candidate["rule"] == "utc_session":
            offset = (start.hour - candidate["start_hour"]) % 24
            positions[index] = float(offset < candidate["duration_hours"])
        elif candidate["rule"] == "weekday":
            positions[index] = float(start.weekday() == candidate["weekday"])
        elif candidate["rule"] == "week_part":
            weekend = start.weekday() >= 5
            positions[index] = float(weekend == (candidate["part"] == "weekend"))
        else:
            raise ValueError(f"unknown crypto candidate rule: {candidate['rule']}")
    return positions


def returns_for(panel: HourlyPanel, positions: np.ndarray, one_way_bps: int) -> tuple[np.ndarray, np.ndarray]:
    gross = np.zeros(len(panel.timestamps))
    consecutive = np.asarray([False] + [b - a == timedelta(hours=1) for a, b in zip(panel.timestamps, panel.timestamps[1:])])
    gross[1:] = np.where(consecutive[1:], panel.closes[1:] / panel.closes[:-1] - 1.0, 0.0) * positions[1:]
    turnover = np.abs(positions - np.roll(positions, 1))
    turnover[0] = positions[0]
    turnover[~consecutive] = positions[~consecutive]
    return gross - turnover * one_way_bps / 10_000.0, turnover


def _daily(panel: HourlyPanel, hourly: np.ndarray, turnover: np.ndarray) -> tuple[tuple[str, ...], np.ndarray, np.ndarray]:
    groups: dict[str, list[int]] = {}
    for index in range(1, len(panel.timestamps)):
        start, end = panel.timestamps[index - 1], panel.timestamps[index]
        if end - start == timedelta(hours=1):
            groups.setdefault(start.date().isoformat(), []).append(index)
    eligible: dict[str, list[int]] = {}
    for day, indexes in groups.items():
        start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
        expected_starts = tuple(start + timedelta(hours=hour) for hour in range(24))
        observed_starts = tuple(panel.timestamps[index - 1] for index in indexes)
        if observed_starts == expected_starts:
            eligible[day] = indexes
    days = tuple(sorted(eligible))
    returns = np.asarray([np.prod(1.0 + hourly[eligible[day]]) - 1.0 for day in days])
    turns = np.asarray([turnover[eligible[day]].sum() for day in days])
    return days, returns, turns


def _mask(days: tuple[str, ...], start: str, end: str) -> np.ndarray:
    return np.asarray([start <= day <= end for day in days])


def metrics(returns: np.ndarray, turnover: np.ndarray, mask: np.ndarray) -> dict[str, Any]:
    selected, selected_turnover = returns[mask], turnover[mask]
    if len(selected) < 2:
        raise ValueError("crypto metric window is too short")
    deviation = float(np.std(selected, ddof=1))
    equity = np.cumprod(1.0 + selected)
    peak = np.maximum.accumulate(equity)
    return {"days": len(selected), "net_cagr": float(equity[-1] ** (365 / len(selected)) - 1), "net_sharpe": float(np.mean(selected) / deviation * math.sqrt(365)) if deviation else None, "max_drawdown": float(np.min(equity / peak - 1)), "annualized_turnover": float(selected_turnover.sum() * 365 / len(selected))}


def _remove_best_sharpe(returns: np.ndarray, mask: np.ndarray, count: int) -> float:
    selected = returns[mask]
    keep = np.ones(len(selected), dtype=bool)
    keep[np.argpartition(selected, -count)[-count:]] = False
    reduced = selected[keep]
    deviation = float(np.std(reduced, ddof=1))
    return float(np.mean(reduced) / deviation * math.sqrt(365)) if deviation else float("-inf")


def _evaluate(panel: HourlyPanel, candidate: dict[str, Any], policy: dict[str, Any], periods: list[dict[str, str]]) -> tuple[dict[str, Any], dict[int, np.ndarray]]:
    positions = positions_for(panel, candidate)
    streams, daily_turnover, days = {}, {}, None
    for cost in policy["costs"]["stress_one_way_bps"]:
        hourly, turns = returns_for(panel, positions, cost)
        current_days, daily_returns, daily_turns = _daily(panel, hourly, turns)
        days = current_days
        streams[cost], daily_turnover[cost] = daily_returns, daily_turns
    masks = {row["id"]: _mask(days, row["start"], row["end"]) for row in periods}
    all_mask = np.logical_or.reduce(list(masks.values()))
    base_cost = policy["costs"]["base_one_way_bps"]
    base = streams[base_cost]
    folds = {name: metrics(base, daily_turnover[base_cost], mask) for name, mask in masks.items()}
    components = {
        "fold_sharpe_25th_percentile": float(np.quantile([row["net_sharpe"] for row in folds.values()], 0.25)),
        "stressed_25bps_sharpe": metrics(streams[25], daily_turnover[25], all_mask)["net_sharpe"],
        "remove_best_days_sharpe": _remove_best_sharpe(base, all_mask, policy["selection"]["remove_best_days"]),
    }
    return {"candidate": candidate, "folds": folds, "all_period": metrics(base, daily_turnover[base_cost], all_mask), "robust_net_sharpe_floor": min(components.values()), "score_components": components, "status": "REGISTERED_COMPLETE"}, streams


def _evaluator_dependencies() -> tuple[Any, ...]:
    return (
        fetch_panel,
        _panel_hash,
        expand_candidates,
        positions_for,
        returns_for,
        _daily,
        _mask,
        metrics,
        _remove_best_sharpe,
        _evaluate,
        _bootstrap_lower,
        evaluate_discovery,
        evaluate_replication,
        _load_discovery,
        _load_authority,
        _canonical_hash,
    )


def _evaluator_hash() -> str:
    source = "\n".join(inspect.getsource(value) for value in _evaluator_dependencies()).encode()
    return "sha256:" + hashlib.sha256(source).hexdigest()


def evaluate_discovery(panel: HourlyPanel, policy: dict[str, Any], policy_hash: str) -> dict[str, Any]:
    trials = {}
    candidates = expand_candidates(policy)
    for candidate in candidates:
        trials[candidate["id"]], _ = _evaluate(panel, candidate, policy, policy["discovery_periods"])
    baseline = trials["always_long"]
    winners = []
    for family in sorted({row["family"] for row in candidates if row["family"] != "baseline"}):
        rows = [row for row in candidates if row["family"] == family]
        winner = sorted(rows, key=lambda row: (-trials[row["id"]]["robust_net_sharpe_floor"], trials[row["id"]]["all_period"]["annualized_turnover"], row["id"]))[0]
        fold_wins = sum(trials[winner["id"]]["folds"][fold]["net_sharpe"] > baseline["folds"][fold]["net_sharpe"] for fold in baseline["folds"])
        winners.append({"family": family, "candidate": winner, "robust_net_sharpe_floor": trials[winner["id"]]["robust_net_sharpe_floor"], "floor_improvement": trials[winner["id"]]["robust_net_sharpe_floor"] - baseline["robust_net_sharpe_floor"], "folds_beating_baseline": fold_wins, "discovery_survivor": trials[winner["id"]]["robust_net_sharpe_floor"] >= baseline["robust_net_sharpe_floor"] + policy["selection"]["minimum_floor_improvement"] and fold_wins >= policy["selection"]["minimum_folds_beating_baseline"]})
    selected = [row["candidate"] for row in winners]
    seal = {"version": "nodequant-crypto-replication-selection-v1", "policy_hash": policy_hash, "evaluator_hash": _evaluator_hash(), "discovery_panel_hash": _panel_hash(panel), "selection_rule": policy["selection"], "candidate_ids": [row["id"] for row in selected], "candidates": selected, "discovery_trial_count": len(trials)}
    seal["seal_hash"] = _canonical_hash(seal)
    result = {"version": "nodequant-crypto-discovery-result-v1", "policy_hash": policy_hash, "evaluator_hash": _evaluator_hash(), "data": {"panel_hash": _panel_hash(panel), "raw_hashes": panel.raw_hashes, "quality": panel.quality}, "trials": trials, "family_winners": winners, "selection_seal": seal, "promotion_eligible": False}
    result["result_hash"] = _canonical_hash(result)
    result["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    return result


def _bootstrap_lower(active: np.ndarray, policy: dict[str, Any]) -> float:
    cfg, n = policy["bootstrap"], len(active)
    rng = np.random.default_rng(cfg["seed"])
    block, count = cfg["block_days"], math.ceil(n / cfg["block_days"])
    estimates = np.empty(cfg["samples"])
    offsets = np.arange(block)
    for sample in range(cfg["samples"]):
        starts = rng.integers(0, n, size=count)
        indexes = ((starts[:, None] + offsets) % n).reshape(-1)[:n]
        estimates[sample] = np.mean(active[indexes]) * 365
    return float(np.quantile(estimates, 0.025))


def evaluate_replication(panel: HourlyPanel, candidates: list[dict[str, Any]], policy: dict[str, Any]) -> dict[str, Any]:
    all_candidates = [{"id": "always_long", "family": "baseline", "rule": "always_long"}, *candidates]
    trials, streams = {}, {}
    for candidate in all_candidates:
        trials[candidate["id"]], streams[candidate["id"]] = _evaluate(panel, candidate, policy, policy["replication_periods"])
    baseline = trials["always_long"]
    champion = sorted(candidates, key=lambda row: (-trials[row["id"]]["robust_net_sharpe_floor"], row["id"]))[0]
    base_cost = policy["costs"]["base_one_way_bps"]
    active = streams[champion["id"]][base_cost] - streams["always_long"][base_cost]
    fold_wins = sum(trials[champion["id"]]["folds"][fold]["net_sharpe"] > baseline["folds"][fold]["net_sharpe"] for fold in baseline["folds"])
    lower = _bootstrap_lower(active, policy)
    checks = {"robust_floor_improves": trials[champion["id"]]["robust_net_sharpe_floor"] >= baseline["robust_net_sharpe_floor"] + policy["replication_gate"]["minimum_floor_improvement"], "minimum_fold_breadth": fold_wins >= policy["replication_gate"]["minimum_folds_beating_baseline"], "positive_net_cagr": trials[champion["id"]]["all_period"]["net_cagr"] > 0, "paired_active_bootstrap_lower_positive": lower > policy["replication_gate"]["paired_bootstrap_lower_must_exceed"]}
    return {"trials": trials, "champion_id": champion["id"], "baseline_id": "always_long", "folds_beating_baseline": fold_wins, "paired_active_annual_return_bootstrap_95pct_lower": lower, "checks": checks, "status": "PASS" if all(checks.values()) else "FAIL"}


def _load_discovery(path: Path, policy_hash: str) -> dict[str, Any]:
    result = json.loads(path.read_text(encoding="utf-8"))
    body = {key: value for key, value in result.items() if key not in {"result_hash", "created_at_utc"}}
    if result.get("policy_hash") != policy_hash or result.get("evaluator_hash") != _evaluator_hash() or result.get("result_hash") != _canonical_hash(body):
        raise ValueError("crypto discovery artifact differs from frozen identity")
    return result


def _load_authority(path: Path, policy_hash: str, discovery: dict[str, Any]) -> dict[str, Any]:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != EXPECTED_REPLICATION_AUTHORITY_SHA256:
        raise ValueError("crypto replication authority differs from trust root")
    authority = json.loads(raw)
    expected_ids = [row["id"] for row in discovery["selection_seal"]["candidates"]]
    if (
        authority["policy_hash"] != policy_hash
        or authority["selection_seal_hash"] != discovery["selection_seal"]["seal_hash"]
        or authority["discovery_result_hash"] != discovery["result_hash"]
        or authority["discovery_panel_hash"] != discovery["data"]["panel_hash"]
        or authority["selected_candidate_ids"] != expected_ids
        or authority["evaluator_hash"] != _evaluator_hash()
    ):
        raise ValueError("crypto replication authority is bound to another experiment")
    return authority


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run sealed BTC discovery then ETH replication for crypto seasonality.")
    parser.add_argument("--stage", choices=("discovery", "replication"), required=True)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--raw-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--discovery-result", type=Path)
    parser.add_argument("--authority", type=Path, default=REPO_ROOT / "research" / "crypto_seasonality_replication_authority_v2.json")
    parser.add_argument("--reuse-raw", action="store_true")
    args = parser.parse_args(argv)
    try:
        policy, policy_hash = load_policy(args.policy)
        if args.stage == "discovery":
            panel = fetch_panel(policy, "discovery_data", args.raw_directory, refresh=not args.reuse_raw)
            result = evaluate_discovery(panel, policy, policy_hash)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(json.dumps({"stage": "discovery", "candidates": len(result["trials"]), "selection_seal": result["selection_seal"]["seal_hash"], "result_hash": result["result_hash"]}, sort_keys=True))
            return 0
        if args.discovery_result is None:
            raise ValueError("replication stage requires --discovery-result")
        discovery = _load_discovery(args.discovery_result, policy_hash)
        _load_authority(args.authority, policy_hash, discovery)
        panel = fetch_panel(policy, "replication_data", args.raw_directory, refresh=not args.reuse_raw)
        replication = evaluate_replication(panel, discovery["selection_seal"]["candidates"], policy)
        result = {"version": "nodequant-crypto-seasonality-result-v1", "policy_hash": policy_hash, "evaluator_hash": _evaluator_hash(), "discovery_result_hash": discovery["result_hash"], "selection_seal_hash": discovery["selection_seal"]["seal_hash"], "data": {"replication_panel_hash": _panel_hash(panel), "replication_raw_hashes": panel.raw_hashes, "replication_quality": panel.quality}, "search_breadth": {"discovery_candidates": len(discovery["trials"]), "families": len(discovery["family_winners"]), "replication_candidates_including_baseline": len(replication["trials"]), "total_measured_trials": len(discovery["trials"]) + len(replication["trials"])}, "discovery_family_winners": discovery["family_winners"], "replication": replication, "verdict": "REPLICATION_SURVIVOR_FOUND" if replication["status"] == "PASS" else "NO_REPLICATION_SURVIVOR", "promotion_eligible": False, "promotion_ceiling": policy["promotion_ceiling"], "limitations": ["Coinbase documents that historical candles may be incomplete and are not executable quotes.", "BTC discovery to ETH replication changes asset and time but not venue.", "UTC windows are falsifiers; they do not reproduce exchange-local or daylight-saving session definitions.", "This is paper-only research and not investment advice."]}
        result["result_hash"] = _canonical_hash(result)
        result["created_at_utc"] = datetime.now(timezone.utc).isoformat()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"stage": "replication", "verdict": result["verdict"], "champion": replication["champion_id"], "result_hash": result["result_hash"]}, sort_keys=True))
        return 0 if replication["status"] == "PASS" else 5
    except (OSError, ValueError, json.JSONDecodeError, httpx.HTTPError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
