"""Sealed U.S. factor-premia discovery to developed-ex-U.S. replication."""

from __future__ import annotations

import argparse
import csv
import hashlib
import inspect
import io
import itertools
import json
import math
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import numpy as np

from .real_alpha import _canonical_hash


REPO_ROOT = Path(__file__).parents[1]
DEFAULT_POLICY = REPO_ROOT / "research" / "factor_premia_policy_v2.json"
EXPECTED_POLICY_SHA256 = "6257c0f77c86da6ed352a27e9a126eb4253f066e2ea3bfd69e2affebea43c44c"
EXPECTED_REPLICATION_AUTHORITY_SHA256 = "b2c9768d3b845749c2ff3810fc5b00e35de02e76de49ef98ff1700da8410264e"


def expand_candidates(policy: dict[str, Any]) -> list[dict[str, Any]]:
    factors = policy["candidate_factors"]
    return [
        {"id": "equal__" + "_".join(subset), "factors": list(subset), "weight": 1.0 / len(subset)}
        for size in range(1, len(factors) + 1)
        for subset in itertools.combinations(factors, size)
    ]


def _bounded_get(url: str, provider: dict[str, Any]) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != provider["host"] or parsed.query or parsed.fragment:
        raise ValueError("factor provider URL is outside the frozen HTTPS host")
    with httpx.Client(timeout=provider["timeout_seconds"], follow_redirects=False) as client:
        with client.stream("GET", url, headers={"User-Agent": "nodequant-research/0.1"}) as response:
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_bytes(64 * 1024):
                size += len(chunk)
                if size > provider["maximum_response_bytes"]:
                    raise ValueError("factor archive exceeds frozen byte cap")
                chunks.append(chunk)
            return b"".join(chunks)


def fetch_archives(policy: dict[str, Any], data_key: str, raw_directory: Path, *, refresh: bool, roles: set[str] | None = None) -> dict[str, bytes]:
    raw_directory.mkdir(parents=True, exist_ok=True)
    rows: dict[str, bytes] = {}
    for source in policy[data_key]["sources"]:
        if roles is not None and source["role"] not in roles:
            continue
        path = raw_directory / source["filename"]
        if refresh:
            raw = _bounded_get(source["url"], policy["provider"])
            try:
                with path.open("xb") as handle:
                    handle.write(raw)
            except FileExistsError as exc:
                raise ValueError(f"refusing to overwrite frozen factor archive: {path.name}") from exc
        else:
            raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if source.get("expected_sha256") and digest != source["expected_sha256"]:
            raise ValueError(f"factor archive differs from frozen bytes: {path.name}")
        rows[source["role"]] = raw
    return rows


def _csv_text(raw: bytes, maximum_uncompressed_bytes: int) -> str:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = [row for row in archive.infolist() if not row.is_dir()]
        if len(members) != 1 or members[0].file_size > maximum_uncompressed_bytes:
            raise ValueError("factor ZIP violates frozen member bounds")
        return archive.read(members[0]).decode("utf-8-sig")


def parse_monthly_csv(text: str, required_columns: list[str]) -> dict[str, dict[str, float]]:
    lines = text.replace("\r\n", "\n").splitlines()
    header_index = None
    for index, line in enumerate(lines):
        values = [value.strip() for value in next(csv.reader([line]))]
        if values and values[0] == "" and all(column in values for column in required_columns):
            header_index = index
            break
    if header_index is None:
        raise ValueError("factor CSV lacks required monthly header")
    header = [value.strip() or "date" for value in next(csv.reader([lines[header_index]]))]
    indexes = {column: header.index(column) for column in required_columns}
    rows: dict[str, dict[str, float]] = {}
    for values in csv.reader(lines[header_index + 1 :]):
        if not values or len(values[0].strip()) != 6 or not values[0].strip().isdigit():
            if rows:
                break
            continue
        month = values[0].strip()
        if month in rows:
            raise ValueError("factor CSV contains duplicate month")
        parsed: dict[str, float] = {}
        for column, index in indexes.items():
            value = float(values[index].strip()) / 100.0
            if not math.isfinite(value) or value <= -0.99:
                raise ValueError("factor CSV contains invalid return")
            parsed[column] = value
        rows[month] = parsed
    if not rows:
        raise ValueError("factor CSV contains no monthly observations")
    return rows


def build_panel(policy: dict[str, Any], data_key: str, archives: dict[str, bytes], required_factors: list[str]) -> dict[str, Any]:
    maximum = policy["provider"]["maximum_uncompressed_bytes"]
    series: dict[str, dict[str, float]] = {}
    if any(factor in required_factors for factor in ("HML", "RMW", "CMA")):
        five = parse_monthly_csv(_csv_text(archives["five_factor"], maximum), ["HML", "RMW", "CMA"])
        for factor in ("HML", "RMW", "CMA"):
            series[factor] = {month: row[factor] for month, row in five.items()}
    momentum_column = None
    if "MOM" in required_factors:
        text = _csv_text(archives["momentum"], maximum)
        momentum_column = "Mom" if ",Mom" in text else "WML" if ",WML" in text else None
        if momentum_column is None:
            raise ValueError("momentum archive contains neither allowed Mom nor WML header")
        momentum = parse_monthly_csv(text, [momentum_column])
        series["MOM"] = {month: row[momentum_column] for month, row in momentum.items()}
    config = policy[data_key]
    months = sorted(set.intersection(*(set(series[factor]) for factor in required_factors)))
    months = [month for month in months if config["start_month"] <= month <= config["end_month"]]
    expected = []
    year, month = map(int, (config["start_month"][:4], config["start_month"][4:]))
    end = config["end_month"]
    while f"{year:04d}{month:02d}" <= end:
        expected.append(f"{year:04d}{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    if months != expected:
        missing = sorted(set(expected) - set(months))
        raise ValueError(f"factor panel lacks frozen months: {missing[:5]}")
    values = np.asarray([[series[factor][row] for factor in required_factors] for row in months])
    if values.shape != (len(months), len(required_factors)) or not np.isfinite(values).all():
        raise ValueError("factor panel shape or values are invalid")
    raw_hashes = {role: "sha256:" + hashlib.sha256(raw).hexdigest() for role, raw in sorted(archives.items())}
    panel = {"months": months, "columns": required_factors, "values": values, "raw_hashes": raw_hashes}
    panel["panel_hash"] = _panel_hash(panel)
    panel["quality"] = {"months": len(months), "first_month": months[0], "last_month": months[-1], "missing_months": 0, "duplicate_months": 0, "invalid_values": 0, "momentum_source_column": momentum_column}
    return panel


def _panel_hash(panel: dict[str, Any]) -> str:
    return _canonical_hash({"months": panel["months"], "columns": panel["columns"], "values": panel["values"].tolist()})


def candidate_returns(panel: dict[str, Any], candidate: dict[str, Any], annual_drag_bps_by_factor: dict[str, int]) -> np.ndarray:
    indexes = [panel["columns"].index(factor) for factor in candidate["factors"]]
    gross = panel["values"][:, indexes].mean(axis=1)
    weighted_drag = sum(annual_drag_bps_by_factor[factor] for factor in candidate["factors"]) / len(candidate["factors"])
    return gross - weighted_drag / 10_000.0 / 12.0


def _mask(months: list[str], start: str, end: str) -> np.ndarray:
    return np.asarray([start <= month <= end for month in months])


def metrics(returns: np.ndarray, mask: np.ndarray) -> dict[str, Any]:
    selected = returns[mask]
    if len(selected) < 12:
        raise ValueError("factor metric window is too short")
    deviation = float(np.std(selected, ddof=1))
    standard_error = deviation / math.sqrt(len(selected))
    return {
        "months": len(selected),
        "annualized_mean": float(np.mean(selected) * 12),
        "annualized_volatility": deviation * math.sqrt(12),
        "sharpe": float(np.mean(selected) / deviation * math.sqrt(12)) if deviation else None,
        "mean_t_statistic": float(np.mean(selected) / standard_error) if standard_error else None,
    }


def _remove_best_sharpe(returns: np.ndarray, mask: np.ndarray, count: int) -> float:
    selected = returns[mask]
    keep = np.ones(len(selected), dtype=bool)
    keep[np.argpartition(selected, -count)[-count:]] = False
    reduced = selected[keep]
    deviation = float(np.std(reduced, ddof=1))
    return float(np.mean(reduced) / deviation * math.sqrt(12)) if deviation else float("-inf")


def evaluate_candidate(panel: dict[str, Any], candidate: dict[str, Any], policy: dict[str, Any], periods: list[dict[str, str]]) -> tuple[dict[str, Any], np.ndarray]:
    base = candidate_returns(panel, candidate, policy["haircuts"]["stress_annual_bps_by_factor"])
    masks = {row["id"]: _mask(panel["months"], row["start"], row["end"]) for row in periods}
    all_mask = np.logical_or.reduce(list(masks.values()))
    folds = {name: metrics(base, mask) for name, mask in masks.items()}
    components = {
        "minimum_fold_sharpe": min(row["sharpe"] for row in folds.values()),
        "remove_best_months_sharpe": _remove_best_sharpe(base, all_mask, policy["selection"]["remove_best_months"]),
    }
    return {"candidate": candidate, "folds": folds, "all_period": metrics(base, all_mask), "robust_sharpe_floor": min(components.values()), "score_components": components}, base


def evaluate_discovery(panel: dict[str, Any], policy: dict[str, Any], policy_hash: str) -> dict[str, Any]:
    trials = {}
    for candidate in expand_candidates(policy):
        trials[candidate["id"]], _ = evaluate_candidate(panel, candidate, policy, policy["discovery_periods"])
    winner = sorted((row["candidate"] for row in trials.values()), key=lambda row: (-trials[row["id"]]["robust_sharpe_floor"], len(row["factors"]), row["id"]))[0]
    positive_folds = sum(row["annualized_mean"] > 0 for row in trials[winner["id"]]["folds"].values())
    eligible = trials[winner["id"]]["robust_sharpe_floor"] > 0 and positive_folds >= policy["selection"]["minimum_positive_folds"]
    selected = winner if eligible else None
    seal = {"version": "nodequant-factor-primary-selection-v1", "status": "PRIMARY_SELECTED" if eligible else "NO_PRIMARY", "policy_hash": policy_hash, "evaluator_hash": _evaluator_hash(), "discovery_panel_hash": panel["panel_hash"], "candidate": selected, "candidate_id": selected["id"] if selected else None, "winning_candidate_before_no_go": winner, "winning_positive_folds": positive_folds, "selection_rule": policy["selection"], "trial_count": len(trials)}
    seal["seal_hash"] = _canonical_hash(seal)
    result = {"version": "nodequant-factor-discovery-v1", "policy_hash": policy_hash, "evaluator_hash": _evaluator_hash(), "data": {"panel_hash": panel["panel_hash"], "raw_hashes": panel["raw_hashes"], "quality": panel["quality"]}, "trials": trials, "primary_selection": seal, "promotion_eligible": False}
    result["result_hash"] = _canonical_hash(result)
    result["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    return result


def _bootstrap_lower(active: np.ndarray, policy: dict[str, Any]) -> float:
    config, length = policy["bootstrap"], len(active)
    rng = np.random.default_rng(config["seed"])
    block = config["block_months"]
    count = math.ceil(length / block)
    statistics = np.empty(config["samples"])
    offsets = np.arange(block)
    observed_mean = float(np.mean(active))
    observed_se = float(np.std(active, ddof=1) / math.sqrt(length))
    for sample in range(config["samples"]):
        starts = rng.integers(0, length, size=count)
        indexes = ((starts[:, None] + offsets) % length).reshape(-1)[:length]
        resampled = active[indexes]
        sample_se = float(np.std(resampled, ddof=1) / math.sqrt(length))
        statistics[sample] = (float(np.mean(resampled)) - observed_mean) / sample_se if sample_se else 0.0
    return float((observed_mean - np.quantile(statistics, 0.95) * observed_se) * 12)


def evaluate_replication(panel: dict[str, Any], candidate: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    trial, returns = evaluate_candidate(panel, candidate, policy, policy["replication_periods"])
    folds_positive = sum(row["annualized_mean"] > 0 for row in trial["folds"].values())
    lower = _bootstrap_lower(returns, policy)
    primary_gate = lower > policy["replication_gate"]["annualized_mean_studentized_bootstrap_lower_must_exceed"]
    return {"primary_candidate_id": candidate["id"], "trial": trial, "folds_positive": folds_positive, "annualized_mean_studentized_moving_block_bootstrap_onesided_95pct_lower": lower, "primary_gate": {"name": "annualized_mean_studentized_moving_block_bootstrap_lower_positive", "passed": primary_gate}, "status": "PASS" if primary_gate else "FAIL"}


def _load_discovery(path: Path, policy_hash: str) -> dict[str, Any]:
    result = json.loads(path.read_text(encoding="utf-8"))
    body = {key: value for key, value in result.items() if key not in {"result_hash", "created_at_utc"}}
    if result.get("policy_hash") != policy_hash or result.get("evaluator_hash") != _evaluator_hash() or result.get("result_hash") != _canonical_hash(body):
        raise ValueError("factor discovery artifact differs from frozen identity")
    return result


def _load_authority(path: Path, policy_hash: str, discovery: dict[str, Any]) -> dict[str, Any]:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != EXPECTED_REPLICATION_AUTHORITY_SHA256:
        raise ValueError("factor replication authority differs from trust root")
    authority = json.loads(raw)
    if authority.get("policy_hash") != policy_hash or authority.get("evaluator_hash") != _evaluator_hash() or authority.get("discovery_result_hash") != discovery["result_hash"] or authority.get("selection_seal_hash") != discovery["primary_selection"]["seal_hash"] or authority.get("primary_candidate_id") != discovery["primary_selection"]["candidate_id"]:
        raise ValueError("factor authority is bound to another experiment")
    return authority


def _evaluator_dependencies() -> tuple[Any, ...]:
    return (_bounded_get, fetch_archives, _csv_text, parse_monthly_csv, build_panel, _panel_hash, expand_candidates, candidate_returns, _mask, metrics, _remove_best_sharpe, evaluate_candidate, evaluate_discovery, _bootstrap_lower, evaluate_replication, _load_discovery, _load_authority, _canonical_hash)


def _evaluator_hash() -> str:
    return "sha256:" + hashlib.sha256("\n".join(inspect.getsource(value) for value in _evaluator_dependencies()).encode()).hexdigest()


def load_policy(path: Path = DEFAULT_POLICY) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_POLICY_SHA256:
        raise ValueError("factor policy differs from preregistered trust root")
    policy = json.loads(raw)
    if policy.get("evaluator_hash") != _evaluator_hash() or len(expand_candidates(policy)) != policy["trial_accounting"]["discovery_candidates"]:
        raise ValueError("factor policy is bound to another evaluator or candidate slate")
    return policy, "sha256:" + digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run sealed U.S. factor discovery then developed-ex-U.S. replication.")
    parser.add_argument("--stage", choices=("discovery", "replication"), required=True)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--raw-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--discovery-result", type=Path)
    parser.add_argument("--authority", type=Path, default=REPO_ROOT / "research" / "factor_premia_replication_authority_v1.json")
    parser.add_argument("--reuse-raw", action="store_true")
    args = parser.parse_args(argv)
    try:
        policy, policy_hash = load_policy(args.policy)
        if args.stage == "discovery":
            archives = fetch_archives(policy, "discovery_data", args.raw_directory, refresh=not args.reuse_raw)
            panel = build_panel(policy, "discovery_data", archives, policy["candidate_factors"])
            result = evaluate_discovery(panel, policy, policy_hash)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(json.dumps({"stage": "discovery", "candidates": len(result["trials"]), "primary": result["primary_selection"]["candidate_id"], "status": result["primary_selection"]["status"], "result_hash": result["result_hash"], "selection_seal": result["primary_selection"]["seal_hash"]}, sort_keys=True))
            return 0 if result["primary_selection"]["status"] == "PRIMARY_SELECTED" else 5
        if args.discovery_result is None:
            raise ValueError("factor replication requires --discovery-result")
        discovery = _load_discovery(args.discovery_result, policy_hash)
        if discovery["primary_selection"]["status"] != "PRIMARY_SELECTED":
            raise ValueError("factor discovery sealed NO_PRIMARY; replication access is prohibited")
        _load_authority(args.authority, policy_hash, discovery)
        primary = discovery["primary_selection"]["candidate"]
        roles = {"momentum"} if primary["factors"] == ["MOM"] else {"five_factor"} if "MOM" not in primary["factors"] else {"five_factor", "momentum"}
        archives = fetch_archives(policy, "replication_data", args.raw_directory, refresh=not args.reuse_raw, roles=roles)
        panel = build_panel(policy, "replication_data", archives, primary["factors"])
        replication = evaluate_replication(panel, primary, policy)
        result = {"version": "nodequant-factor-premia-result-v1", "policy_hash": policy_hash, "evaluator_hash": _evaluator_hash(), "discovery_result_hash": discovery["result_hash"], "selection_seal_hash": discovery["primary_selection"]["seal_hash"], "data": {"replication_panel_hash": panel["panel_hash"], "replication_raw_hashes": panel["raw_hashes"], "replication_quality": panel["quality"]}, "search_breadth": {"discovery_candidates": len(discovery["trials"]), "replication_candidates": 1, "total_measured_trials": len(discovery["trials"]) + 1}, "replication": replication, "verdict": "FACTOR_PREMIUM_PERSISTS" if replication["status"] == "PASS" else "NO_FACTOR_PREMIUM_PERSISTENCE", "promotion_eligible": False, "promotion_ceiling": policy["promotion_ceiling"], "limitations": ["French library series are reconstructed research returns and may change after CRSP revisions.", "Long-short factor returns omit implementation-specific turnover, borrow, financing, taxes, and market impact; fixed per-factor deductions are implementation-stress haircuts, not measured costs.", "Equal-weight factor sleeves reset notionals monthly; CAGR and wealth claims are not valid for this zero-cost spread diagnostic.", "U.S. to developed-ex-U.S. changes geography, period, breakpoints, and security universe but shares a research-data producer, USD conversion, and global shocks.", "Public research returns cannot establish executable alpha or authorize live capital."]}
        result["result_hash"] = _canonical_hash(result)
        result["created_at_utc"] = datetime.now(timezone.utc).isoformat()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"stage": "replication", "verdict": result["verdict"], "primary": replication["primary_candidate_id"], "result_hash": result["result_hash"]}, sort_keys=True))
        return 0 if replication["status"] == "PASS" else 5
    except (OSError, ValueError, KeyError, json.JSONDecodeError, zipfile.BadZipFile, httpx.HTTPError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
