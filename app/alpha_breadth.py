"""Breadth-first daily-price research with a pre-fetch replication seal.

The opened SPY/QQQ/IWM/TLT/GLD panel is a discovery arena only.  Candidate
selection is content-addressed before a second ETF universe is fetched.  The
result is still diagnostic: adjusted closes are not executable quotes and a
current-constituent ETF universe is not point-in-time market conformance.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import itertools
import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from .real_alpha import (
    PricePanel,
    _bootstrap_active_lower,
    _canonical_hash,
    _panel_hash,
    _sharpe_after_removing_best,
    fetch_panel,
    metrics,
    rebalance_flags_for,
    returns_for,
)


REPO_ROOT = Path(__file__).parents[1]
DEFAULT_POLICY = REPO_ROOT / "research" / "alpha_breadth_policy.json"
EXPECTED_POLICY_SHA256S = {
    "4e2ac00927f9d24e3ae7a1dbceae4d964a463bf62c1db93a1479ae11022b28ec",
    "f7642f0dedece5681a171d1ad5b0ac13150d22fec7ccab23a26307fb76a96a77",
    "1ccd9da806c431538b5591222bdfc2b3e7f3fee4593800422c2c772ed7e1cb04",
    "f51991e81396b24cb8ea69891358da01269cfc423b78e1a79af2e9a85c5a9e48",
}
EXPECTED_SNAPSHOT_SHA256S = {
    "8b4878c3b05c3af796028bc36472c3a7391b7ff956357cee4b2d1d0634f784cf",
    "844f83dfef833358d113f535017e4436cd5ed07dc54b7769d5102843e7fcb717",
    "be150a07d6d3fa2a7f9b486a4813de01032b3ef636532d113a7559e29487ad68",
    "e564d1ae2b5c01872454efb438a430310ab865359b6119f97d1dc62925249f68",
}


def load_policy(path: Path = DEFAULT_POLICY) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest not in EXPECTED_POLICY_SHA256S:
        raise ValueError("alpha-breadth policy differs from the preregistered trust root")
    policy = json.loads(raw)
    if "inherits" in policy:
        base_path = path.parent / policy["inherits"]
        base_raw = base_path.read_bytes()
        if "sha256:" + hashlib.sha256(base_raw).hexdigest() != policy["inherits_sha256"]:
            raise ValueError("inherited alpha-breadth policy differs from its frozen hash")
        base = json.loads(base_raw)
        policy = {
            **base,
            **{key: value for key, value in policy.items() if key not in {"inherits", "inherits_sha256"}},
        }
    candidates = expand_candidates(policy)
    if len(candidates) != policy["trial_accounting"]["registered_discovery_candidates"]:
        raise ValueError("expanded discovery count differs from the frozen trial count")
    if len({row["id"] for row in candidates}) != len(candidates):
        raise ValueError("candidate ids are not unique")
    for key in ("discovery_data", "replication_data"):
        data = policy[key]
        symbols = data["risky_symbols"] + [data["cash_proxy_symbol"]]
        if len(symbols) != len(set(symbols)) or len(symbols) > data["maximum_symbols"]:
            raise ValueError(f"{key} symbol universe violates the frozen bound")
    return policy, "sha256:" + digest


def load_replication_snapshot(path: Path, policy_hash: str, selection_hash: str) -> dict[str, Any]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest not in EXPECTED_SNAPSHOT_SHA256S:
        raise ValueError("replication snapshot differs from the frozen lineage trust root")
    snapshot = json.loads(raw)
    if snapshot["policy_hash"] != policy_hash or snapshot["selection_seal_hash"] != selection_hash:
        raise ValueError("replication snapshot is bound to a different policy or selection")
    if snapshot.get("evaluator_hash") not in {None, _evaluator_hash()}:
        raise ValueError("replication snapshot is bound to a different evaluator")
    return snapshot


def _grid(values: dict[str, list[Any]]) -> list[dict[str, Any]]:
    keys = list(values)
    return [dict(zip(keys, row, strict=True)) for row in itertools.product(*(values[key] for key in keys))]


def expand_candidates(policy: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = [{"id": "always_long", "family": "baseline", "rule": "always_long"}]
    for family in policy["candidate_families"]:
        for parameters in _grid(family["grid"]):
            suffix = "_".join(f"{key}-{value}" for key, value in parameters.items())
            candidates.append({
                "id": f"{family['id']}__{suffix}",
                "family": family["id"],
                "rule": family["rule"],
                "source_ids": family["source_ids"],
                **parameters,
            })
    return candidates


def _lookback_return(prices: np.ndarray, index: int, days: int) -> np.ndarray:
    if index < days:
        return np.zeros(prices.shape[1])
    return prices[index] / prices[index - days] - 1.0


def _inverse_volatility(prices: np.ndarray, index: int, days: int) -> np.ndarray:
    if index < days:
        return np.zeros(prices.shape[1])
    window = prices[index - days:index + 1]
    daily = window[1:] / window[:-1] - 1.0
    vol = np.std(daily, axis=0, ddof=1) * math.sqrt(252)
    eligible = np.isfinite(vol) & (vol > 0)
    if not eligible.any():
        return np.zeros(prices.shape[1])
    inverse = np.zeros_like(vol)
    inverse[eligible] = 1.0 / vol[eligible]
    return inverse / inverse.sum()


def _select(values: np.ndarray, top_n: int, *, descending: bool, eligible: np.ndarray | None = None) -> np.ndarray:
    allowed = np.ones(len(values), dtype=bool) if eligible is None else eligible
    order = np.argsort(values, kind="stable")
    if descending:
        order = order[::-1]
    chosen = [asset for asset in order if allowed[asset]][:top_n]
    weights = np.zeros(len(values))
    if chosen:
        weights[chosen] = 1.0 / len(chosen)
    return weights


def signal_for(prices: np.ndarray, index: int, candidate: dict[str, Any]) -> np.ndarray:
    assets = prices.shape[1]
    rule = candidate["rule"]
    if rule == "always_long":
        return np.full(assets, 1.0 / assets)
    if rule == "absolute_trend":
        momentum = _lookback_return(prices, index, candidate["lookback_days"])
        active = momentum > 0
        return active.astype(float) / assets
    if rule == "moving_average":
        days = candidate["lookback_days"]
        if index < days:
            return np.zeros(assets)
        active = prices[index] > prices[index - days + 1:index + 1].mean(axis=0)
        return active.astype(float) / assets
    if rule in {"relative_momentum", "dual_momentum", "short_term_reversal"}:
        if index < candidate["lookback_days"]:
            return np.zeros(assets)
        momentum = _lookback_return(prices, index, candidate["lookback_days"])
        eligible = momentum > 0 if rule == "dual_momentum" else None
        return _select(
            momentum,
            candidate["top_n"],
            descending=rule != "short_term_reversal",
            eligible=eligible,
        )
    if rule == "composite_momentum":
        horizons = (63, 126, 252)
        if index < max(horizons):
            return np.zeros(assets)
        horizon_returns = np.asarray([_lookback_return(prices, index, days) for days in horizons])
        ranks = np.empty_like(horizon_returns)
        for row_index, row in enumerate(horizon_returns):
            order = np.argsort(row, kind="stable")
            ranks[row_index, order] = np.arange(assets)
        return _select(
            ranks.mean(axis=0),
            candidate["top_n"],
            descending=True,
            eligible=horizon_returns[-1] > 0,
        )
    if rule == "inverse_volatility":
        return _inverse_volatility(prices, index, candidate["volatility_days"])
    if rule == "inverse_volatility_trend":
        weights = _inverse_volatility(prices, index, candidate["volatility_days"])
        active = _lookback_return(prices, index, candidate["lookback_days"]) > 0
        weights = weights * active
        return weights / max(1.0, weights.sum())
    if rule == "volatility_managed_equal":
        days = candidate["volatility_days"]
        if index < days:
            return np.zeros(assets)
        window = prices[index - days:index + 1]
        portfolio = np.mean(window[1:] / window[:-1] - 1.0, axis=1)
        realized = float(np.std(portfolio, ddof=1) * math.sqrt(252))
        exposure = min(1.0, candidate["target_volatility_bps"] / 10_000.0 / realized) if realized else 0.0
        return np.full(assets, exposure / assets)
    if rule == "correlation_gated_inverse_volatility":
        days = candidate["correlation_days"]
        if index < days:
            return np.zeros(assets)
        window = prices[index - days:index + 1]
        daily = window[1:] / window[:-1] - 1.0
        correlation = np.corrcoef(daily, rowvar=False)
        off_diagonal = correlation[np.triu_indices(assets, 1)]
        average = float(np.nanmean(off_diagonal))
        exposure = 1.0 if average <= candidate["maximum_correlation_bps"] / 10_000.0 else 0.5
        return _inverse_volatility(prices, index, candidate["volatility_days"]) * exposure
    raise ValueError(f"unknown breadth candidate rule: {rule}")


def positions_for(panel: PricePanel, candidate: dict[str, Any]) -> np.ndarray:
    rows, assets = panel.risky_prices.shape
    positions = np.zeros((rows, assets), dtype=float)
    if candidate["rule"] == "always_long":
        positions[1:] = 1.0
        return positions
    if candidate["rule"] in {"turn_of_month", "option_expiration_week", "halloween"}:
        for index in range(1, rows):
            active = _calendar_active(panel.dates, index, candidate)
            positions[index] = (np.full(assets, 1.0 / assets) if active else 0.0) * assets
        return positions
    for index in range(1, rows - 1):
        positions[index + 1] = positions[index]
        if (panel.dates[index].year, panel.dates[index].month) != (
            panel.dates[index - 1].year,
            panel.dates[index - 1].month,
        ):
            weights = signal_for(panel.risky_prices, index - 1, candidate)
            if np.any(~np.isfinite(weights)) or np.any(weights < 0) or weights.sum() > 1.0 + 1e-12:
                raise ValueError(f"candidate {candidate['id']} produced invalid or leveraged weights")
            positions[index + 1] = weights * assets
    return positions


def rebalances_for(panel: PricePanel, candidate: dict[str, Any]) -> np.ndarray:
    if candidate["rule"] in {"turn_of_month", "option_expiration_week", "halloween"}:
        flags = np.zeros(len(panel.dates), dtype=bool)
        flags[1:] = True
        return flags
    return rebalance_flags_for(panel, candidate)


def _third_friday(day: date) -> date:
    first = day.replace(day=1)
    first_friday = first + timedelta(days=(4 - first.weekday()) % 7)
    return first_friday + timedelta(days=14)


def _calendar_active(dates: tuple[date, ...], index: int, candidate: dict[str, Any]) -> bool:
    day = dates[index]
    rule = candidate["rule"]
    if rule == "halloween":
        risky_season = day.month >= 11 or day.month <= 4
        return risky_season if candidate["window"] == "november_april" else not risky_season
    if rule == "option_expiration_week":
        expiry = _third_friday(day)
        offset = candidate["week_offset"]
        target = expiry + timedelta(days=offset * 7)
        return day.isocalendar()[:2] == target.isocalendar()[:2]
    month_indexes = [
        row_index for row_index, row_day in enumerate(dates)
        if row_day.year == day.year and row_day.month == day.month
    ]
    ordinal = month_indexes.index(index)
    if candidate["window"] == "turn":
        return ordinal <= 2 or ordinal == len(month_indexes) - 1
    return 9 <= ordinal <= 12


def _mask(panel: PricePanel, start: str, end: str) -> np.ndarray:
    return np.asarray([start <= day.isoformat() <= end for day in panel.dates], dtype=bool)


def _evaluate_candidate(
    panel: PricePanel,
    candidate: dict[str, Any],
    positions: np.ndarray,
    rebalance_flags: np.ndarray,
    policy: dict[str, Any],
    periods: list[dict[str, str]],
) -> tuple[dict[str, Any], dict[int, dict[str, np.ndarray]]]:
    streams = {
        cost: returns_for(panel, positions, cost, rebalance_flags)
        for cost in policy["costs"]["stress_one_way_bps"]
    }
    base = streams[policy["costs"]["base_one_way_bps"]]
    stressed = streams[policy["selection"]["stress_one_way_bps"]]
    masks = {row["id"]: _mask(panel, row["start"], row["end"]) for row in periods}
    all_mask = np.logical_or.reduce(list(masks.values()))
    fold_metrics = {
        fold_id: metrics(base["net"], base["cash"], base["turnover"], mask)
        for fold_id, mask in masks.items()
    }
    fold_sharpes = np.asarray([row["excess_sharpe"] for row in fold_metrics.values()], dtype=float)
    components = {
        "fold_sharpe_25th_percentile": float(np.quantile(fold_sharpes, 0.25)),
        "stressed_excess_sharpe": metrics(
            stressed["net"], stressed["cash"], stressed["turnover"], all_mask
        )["excess_sharpe"],
        "remove_best_days_excess_sharpe": _sharpe_after_removing_best(
            base["net"], base["cash"], all_mask, policy["selection"]["remove_best_days"]
        ),
    }
    return ({
        "candidate": candidate,
        "folds": fold_metrics,
        "all_period": metrics(base["net"], base["cash"], base["turnover"], all_mask),
        "robust_excess_sharpe_floor": min(components.values()),
        "score_components": components,
        "status": "REGISTERED_COMPLETE",
    }, streams)


def _evaluator_hash() -> str:
    load_bearing = (
        expand_candidates,
        signal_for,
        positions_for,
        rebalances_for,
        _evaluate_candidate,
        evaluate_replication,
        rebalance_flags_for,
        returns_for,
        metrics,
        _sharpe_after_removing_best,
        _bootstrap_active_lower,
    )
    source = "\n".join(inspect.getsource(value) for value in load_bearing).encode()
    return "sha256:" + hashlib.sha256(source).hexdigest()


def evaluate_discovery(panel: PricePanel, policy: dict[str, Any], policy_hash: str) -> dict[str, Any]:
    candidates = expand_candidates(policy)
    trials: dict[str, Any] = {}
    positions: dict[str, np.ndarray] = {}
    rebalances: dict[str, np.ndarray] = {}
    for candidate in candidates:
        candidate_positions = positions_for(panel, candidate)
        positions[candidate["id"]] = candidate_positions
        candidate_rebalances = rebalances_for(panel, candidate)
        rebalances[candidate["id"]] = candidate_rebalances
        trials[candidate["id"]], _ = _evaluate_candidate(
            panel, candidate, candidate_positions, candidate_rebalances,
            policy, policy["discovery_periods"]
        )

    baseline = trials["always_long"]
    minimum_delta = policy["selection"]["minimum_floor_improvement"]
    family_winners: list[dict[str, Any]] = []
    for family in sorted({row["family"] for row in candidates if row["family"] != "baseline"}):
        rows = [row for row in candidates if row["family"] == family]
        winner = sorted(rows, key=lambda row: (-trials[row["id"]]["robust_excess_sharpe_floor"], row["id"]))[0]
        fold_wins = sum(
            trials[winner["id"]]["folds"][fold]["excess_sharpe"]
            > baseline["folds"][fold]["excess_sharpe"]
            for fold in baseline["folds"]
        )
        family_winners.append({
            "family": family,
            "candidate_id": winner["id"],
            "robust_excess_sharpe_floor": trials[winner["id"]]["robust_excess_sharpe_floor"],
            "floor_improvement": trials[winner["id"]]["robust_excess_sharpe_floor"] - baseline["robust_excess_sharpe_floor"],
            "folds_beating_baseline": fold_wins,
            "discovery_survivor": (
                trials[winner["id"]]["robust_excess_sharpe_floor"]
                >= baseline["robust_excess_sharpe_floor"] + minimum_delta
                and fold_wins >= policy["selection"]["minimum_folds_beating_baseline"]
            ),
        })

    eligible = [row for row in family_winners if row["discovery_survivor"]]
    synthesis_trials: dict[str, Any] = {}
    candidate_by_id = {row["id"]: row for row in candidates}
    for left, right in itertools.combinations(eligible, 2):
        synthesis_id = f"synthesis__{left['family']}__{right['family']}"
        spec = {
            "id": synthesis_id,
            "family": "synthesis",
            "rule": "equal_weight_parent_positions",
            "parents": [left["candidate_id"], right["candidate_id"]],
        }
        combined = (positions[left["candidate_id"]] + positions[right["candidate_id"]]) / 2.0
        combined_rebalances = (
            rebalances[left["candidate_id"]] | rebalances[right["candidate_id"]]
        )
        synthesis_trials[synthesis_id], _ = _evaluate_candidate(
            panel, spec, combined, combined_rebalances,
            policy, policy["discovery_periods"]
        )
        candidate_by_id[synthesis_id] = spec

    ranked_syntheses = sorted(
        synthesis_trials,
        key=lambda candidate_id: (-synthesis_trials[candidate_id]["robust_excess_sharpe_floor"], candidate_id),
    )[:policy["selection"]["maximum_syntheses_for_replication"]]
    replication_ids = [row["candidate_id"] for row in family_winners] + ranked_syntheses
    replication_candidates = [candidate_by_id[candidate_id] for candidate_id in replication_ids]
    seal = {
        "version": "nodequant-alpha-replication-selection-v1",
        "policy_hash": policy_hash,
        "evaluator_hash": _evaluator_hash(),
        "discovery_panel_hash": _panel_hash(panel),
        "selection_rule": policy["selection"],
        "candidate_ids": replication_ids,
        "candidates": replication_candidates,
        "discovery_trial_count": len(trials) + len(synthesis_trials),
    }
    seal["seal_hash"] = _canonical_hash(seal)
    return {
        "trials": trials,
        "synthesis_trials": synthesis_trials,
        "family_winners": family_winners,
        "baseline": baseline,
        "selection_seal": seal,
    }


def evaluate_replication(
    panel: PricePanel,
    candidates: list[dict[str, Any]],
    policy: dict[str, Any],
) -> dict[str, Any]:
    baseline_spec = {"id": "always_long", "family": "baseline", "rule": "always_long"}
    all_candidates = [baseline_spec] + candidates
    trials: dict[str, Any] = {}
    streams: dict[str, dict[int, dict[str, np.ndarray]]] = {}
    for candidate in all_candidates:
        candidate_positions = positions_for(panel, candidate) if candidate["rule"] != "equal_weight_parent_positions" else None
        candidate_rebalances = rebalances_for(panel, candidate) if candidate_positions is not None else None
        if candidate_positions is None:
            parent_specs = [next(row for row in candidates if row["id"] == parent) for parent in candidate["parents"]]
            parent_positions = [positions_for(panel, row) for row in parent_specs]
            parent_rebalances = [rebalances_for(panel, row) for row in parent_specs]
            candidate_positions = np.mean(parent_positions, axis=0)
            candidate_rebalances = np.logical_or.reduce(parent_rebalances)
        trials[candidate["id"]], streams[candidate["id"]] = _evaluate_candidate(
            panel, candidate, candidate_positions, candidate_rebalances,
            policy, policy["replication_periods"]
        )

    baseline = trials["always_long"]
    base_cost = policy["costs"]["base_one_way_bps"]
    all_mask = np.logical_or.reduce([
        _mask(panel, row["start"], row["end"]) for row in policy["replication_periods"]
    ])
    ranked = sorted(
        candidates,
        key=lambda row: (-trials[row["id"]]["robust_excess_sharpe_floor"], row["id"]),
    )
    champion = ranked[0]
    active = (
        streams[champion["id"]][base_cost]["net"] - streams["always_long"][base_cost]["net"]
    )[all_mask]
    lower = _bootstrap_active_lower(active, policy)
    fold_wins = sum(
        trials[champion["id"]]["folds"][fold]["excess_sharpe"]
        > baseline["folds"][fold]["excess_sharpe"]
        for fold in baseline["folds"]
    )
    checks = {
        "robust_floor_improves": (
            trials[champion["id"]]["robust_excess_sharpe_floor"]
            >= baseline["robust_excess_sharpe_floor"] + policy["replication_gate"]["minimum_floor_improvement"]
        ),
        "minimum_fold_breadth": fold_wins >= policy["replication_gate"]["minimum_folds_beating_baseline"],
        "positive_net_cagr": trials[champion["id"]]["all_period"]["net_cagr"] > 0,
        "paired_active_bootstrap_lower_positive": lower > 0,
    }
    return {
        "trials": trials,
        "champion_id": champion["id"],
        "baseline_id": "always_long",
        "folds_beating_baseline": fold_wins,
        "paired_active_annual_return_bootstrap_95pct_lower": lower,
        "checks": checks,
        "status": "PASS" if all(checks.values()) else "FAIL",
    }


def run(
    policy: dict[str, Any],
    policy_hash: str,
    discovery_panel: PricePanel,
    replication_panel: PricePanel,
) -> dict[str, Any]:
    discovery = evaluate_discovery(discovery_panel, policy, policy_hash)
    replication = evaluate_replication(
        replication_panel, discovery["selection_seal"]["candidates"], policy
    )
    result = {
        "version": "nodequant-alpha-breadth-result-v1",
        "policy_hash": policy_hash,
        "data": {
            "discovery_panel_hash": _panel_hash(discovery_panel),
            "discovery_quality": discovery_panel.quality,
            "replication_panel_hash": _panel_hash(replication_panel),
            "replication_raw_hashes": replication_panel.raw_hashes,
            "replication_quality": replication_panel.quality,
        },
        "search_breadth": {
            "registered_discovery_candidates": len(discovery["trials"]),
            "strategy_families": len(policy["candidate_families"]),
            "synthesis_trials": len(discovery["synthesis_trials"]),
            "replication_candidates": len(discovery["selection_seal"]["candidates"]),
            "total_measured_trials": (
                len(discovery["trials"])
                + len(discovery["synthesis_trials"])
                + len(replication["trials"])
            ),
        },
        "discovery": discovery,
        "replication": replication,
        "verdict": "REPLICATION_SURVIVOR_FOUND" if replication["status"] == "PASS" else "NO_REPLICATION_SURVIVOR",
        "promotion_eligible": False,
        "promotion_ceiling": policy["promotion_ceiling"],
        "limitations": [
            "The first panel is fully opened and is used only for discovery, never as a holdout.",
            "The replication universe was selected before fetching its bytes, but both universes use a secondary unofficial adjusted-close provider.",
            "Adjusted closes are not executable quotes; impact, taxes, borrow and account-specific costs are absent.",
            "ETF availability and the current symbol list do not establish point-in-time universe membership.",
            "This is paper-only research, not investment advice or a return guarantee.",
        ],
    }
    result["result_hash"] = _canonical_hash(result)
    result["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    return result


def _panel_policy(policy: dict[str, Any], key: str) -> dict[str, Any]:
    return {"data": policy[key]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run breadth-first daily-price discovery and sealed replication.")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "evidence" / "breadth-first-alpha" / "result.json")
    parser.add_argument("--raw-directory", type=Path, default=REPO_ROOT / "evidence" / "real-alpha-search" / "raw")
    parser.add_argument("--replication-raw-directory", type=Path, default=REPO_ROOT / "evidence" / "breadth-first-alpha" / "raw-replication")
    parser.add_argument("--replication-snapshot", type=Path, default=REPO_ROOT / "research" / "alpha_breadth_replication_snapshot.json")
    parser.add_argument("--reuse-raw", action="store_true")
    parser.add_argument("--reuse-replication-raw", action="store_true")
    parser.add_argument("--refresh-replication", action="store_true")
    args = parser.parse_args(argv)
    try:
        policy, policy_hash = load_policy(args.policy)
        discovery_panel = fetch_panel(_panel_policy(policy, "discovery_data"), args.raw_directory, refresh=not args.reuse_raw)
        discovery = evaluate_discovery(discovery_panel, policy, policy_hash)
        selection_path = args.output.parent / "replication-selection-seal.json"
        selection_path.parent.mkdir(parents=True, exist_ok=True)
        selection_path.write_text(json.dumps(discovery["selection_seal"], indent=2, sort_keys=True) + "\n", encoding="utf-8")
        replication_panel = fetch_panel(
            _panel_policy(policy, "replication_data"),
            args.replication_raw_directory,
            refresh=args.refresh_replication or not (args.reuse_raw or args.reuse_replication_raw),
        )
        snapshot = load_replication_snapshot(
            args.replication_snapshot, policy_hash, discovery["selection_seal"]["seal_hash"]
        )
        if replication_panel.raw_hashes != snapshot["replication_raw_hashes"]:
            raise ValueError("replication raw hashes differ from the frozen snapshot")
        if _panel_hash(replication_panel) != snapshot["replication_panel_hash"]:
            raise ValueError("replication panel differs from the frozen snapshot")
        result = run(policy, policy_hash, discovery_panel, replication_panel)
        if result["discovery"]["selection_seal"]["seal_hash"] != discovery["selection_seal"]["seal_hash"]:
            raise ValueError("replication selection changed after the data fetch")
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({
            "verdict": result["verdict"],
            "families": result["search_breadth"]["strategy_families"],
            "discovery_candidates": result["search_breadth"]["registered_discovery_candidates"],
            "total_measured_trials": result["search_breadth"]["total_measured_trials"],
            "replication_champion": result["replication"]["champion_id"],
            "replication": result["replication"]["status"],
            "selection_seal": result["discovery"]["selection_seal"]["seal_hash"],
            "result_hash": result["result_hash"],
        }, sort_keys=True))
        return 0 if result["replication"]["status"] == "PASS" else 5
    except (OSError, ValueError, httpx.HTTPError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
