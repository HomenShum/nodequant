"""Freeze future-time alpha forecasts before their return windows exist."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from .alpha_breadth import signal_for
from .real_alpha import PricePanel, _canonical_hash, _panel_hash, fetch_panel


REPO_ROOT = Path(__file__).parents[1]
DEFAULT_POLICY = REPO_ROOT / "research" / "prospective_alpha_policy_v3.json"
EXPECTED_POLICY_SHA256S = {
    "2f8ac40f8b978345ab3d0653849bf2dcd8f7776c0253511b76967fc780aa715b",
    "09576975af708610c90a9b489c0ebb6cf09401ae18a7253164b040c5570d300d",
    "d163fe7a4d68e9a91fea32c1497081595f3dc4086ed7217db9bada35d235c451",
}


def load_policy(path: Path = DEFAULT_POLICY) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest not in EXPECTED_POLICY_SHA256S:
        raise ValueError("prospective policy differs from the preregistered trust root")
    policy = json.loads(raw)
    ids = [policy["forecast"]["primary_strategy_id"], *policy["forecast"]["comparison_strategy_ids"]]
    if len(ids) != len(set(ids)):
        raise ValueError("prospective strategy ids are not unique")
    return policy, "sha256:" + digest


def load_source_result(policy: dict[str, Any]) -> dict[str, Any]:
    binding = policy["source_result"]
    path = REPO_ROOT / binding["path"]
    result = json.loads(path.read_text(encoding="utf-8"))
    expected = binding["semantic_result_hash"]
    claimed = result.get("result_hash")
    unhashed = {key: value for key, value in result.items() if key not in {"result_hash", "created_at_utc"}}
    if claimed != expected or _canonical_hash(unhashed) != expected:
        raise ValueError("source breadth result differs from its frozen semantic identity")
    if result.get("promotion_eligible") is not False:
        raise ValueError("source result must remain non-promotable")
    return result


def _signal_cutoff(panel: PricePanel, policy: dict[str, Any]) -> int:
    cutoff_day = date.fromisoformat(policy["forecast"]["signal_cutoff_date"])
    try:
        cutoff = panel.dates.index(cutoff_day)
    except ValueError as exc:
        raise ValueError("frozen signal cutoff is absent from the common panel") from exc
    later_same_month = [day for day in panel.dates[cutoff + 1:] if (day.year, day.month) == (cutoff_day.year, cutoff_day.month)]
    if later_same_month:
        raise ValueError("frozen signal cutoff is not the final common session of its month")
    return cutoff


def _candidate_map(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    candidates = result["discovery"]["selection_seal"]["candidates"]
    return {row["id"]: row for row in candidates}


def _weights_for(
    prices: np.ndarray,
    cutoff: int,
    candidate: dict[str, Any],
    candidates: dict[str, dict[str, Any]],
) -> np.ndarray:
    if candidate["rule"] == "equal_weight_parent_positions":
        weights = np.mean([
            _weights_for(prices, cutoff, candidates[parent], candidates)
            for parent in candidate["parents"]
        ], axis=0)
    else:
        weights = signal_for(prices, cutoff, candidate)
    if np.any(~np.isfinite(weights)) or np.any(weights < 0) or weights.sum() > 1.0 + 1e-12:
        raise ValueError(f"candidate {candidate['id']} produced invalid prospective weights")
    return weights


def _evaluator_hash() -> str:
    source = "\n".join(inspect.getsource(value) for value in (
        load_source_result, _signal_cutoff, _candidate_map, _weights_for, build_packet
    )).encode()
    return "sha256:" + hashlib.sha256(source).hexdigest()


def build_packet(
    panel: PricePanel,
    policy: dict[str, Any],
    policy_hash: str,
    source_result: dict[str, Any],
) -> dict[str, Any]:
    sealed_at_utc = datetime.now(timezone.utc).isoformat()
    policy_frozen = datetime.fromisoformat(policy["frozen_at_utc"].replace("Z", "+00:00"))
    sealed = datetime.fromisoformat(sealed_at_utc)
    if policy_frozen > sealed:
        raise ValueError("policy freeze time follows the prospective packet seal")
    cutoff = _signal_cutoff(panel, policy)
    candidates = _candidate_map(source_result)
    strategy_ids = [policy["forecast"]["primary_strategy_id"], *policy["forecast"]["comparison_strategy_ids"]]
    missing = [candidate_id for candidate_id in strategy_ids if candidate_id not in candidates]
    if missing:
        raise ValueError(f"prospective strategies absent from frozen source selection: {missing}")
    forecasts: list[dict[str, Any]] = []
    for candidate_id in strategy_ids:
        weights = _weights_for(panel.risky_prices, cutoff, candidates[candidate_id], candidates)
        forecasts.append({
            "strategy_id": candidate_id,
            "role": "PRIMARY" if candidate_id == policy["forecast"]["primary_strategy_id"] else "COMPARISON",
            "risky_weights": dict(zip(panel.risky_symbols, weights.tolist(), strict=True)),
            "cash_weight": float(1.0 - weights.sum()),
        })
    equal = 1.0 / len(panel.risky_symbols)
    forecasts.append({
        "strategy_id": policy["forecast"]["benchmark_id"],
        "role": "BENCHMARK",
        "risky_weights": {symbol: equal for symbol in panel.risky_symbols},
        "cash_weight": 0.0,
    })
    packet = {
        "version": "nodequant-prospective-alpha-packet-v1",
        "status": "FROZEN_AWAITING_FUTURE_OUTCOMES",
        "policy_hash": policy_hash,
        "evaluator_hash": _evaluator_hash(),
        "source_result_hash": source_result["result_hash"],
        "data": {
            "panel_hash": _panel_hash(panel),
            "raw_hashes": panel.raw_hashes,
            "quality": panel.quality,
            "signal_cutoff": panel.dates[cutoff].isoformat(),
            "observed_through": panel.dates[-1].isoformat(),
        },
        "outcome_boundary": {
            "outcomes_present": False,
            "sealed_at_utc": sealed_at_utc,
            "entry_close_rule": policy["forecast"]["entry_close_rule"],
            "first_return_rule": policy["forecast"]["first_return_rule"],
            "evaluation_end_rule": policy["forecast"]["evaluation_end_rule"],
        },
        "forecasts": forecasts,
        "future_gate": policy["future_gate"],
        "promotion_eligible": False,
        "promotion_ceiling": policy["promotion_ceiling"],
        "created_at_utc": sealed_at_utc,
    }
    packet["packet_hash"] = _canonical_hash(packet)
    return packet


def write_once(path: Path, packet: dict[str, Any]) -> dict[str, Any]:
    def validated_existing() -> dict[str, Any]:
        existing = json.loads(path.read_text(encoding="utf-8"))
        declared = existing.get("packet_hash")
        body = {key: value for key, value in existing.items() if key != "packet_hash"}
        if declared != _canonical_hash(body):
            raise ValueError("existing prospective packet body differs from its declared hash")
        if declared != packet["packet_hash"]:
            raise ValueError("prospective packet path already contains a different forecast")
        return existing
    if path.exists():
        return validated_existing()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(packet, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except FileExistsError:
        return validated_existing()
    return validated_existing()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Freeze a future-time alpha forecast packet.")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--raw-directory", type=Path, default=REPO_ROOT / "evidence" / "prospective-alpha" / "raw")
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "evidence" / "prospective-alpha-v3" / "packet-2026-08.json")
    parser.add_argument("--reuse-raw", action="store_true")
    args = parser.parse_args(argv)
    try:
        policy, policy_hash = load_policy(args.policy)
        source_result = load_source_result(policy)
        panel = fetch_panel({"data": policy["data"]}, args.raw_directory, refresh=not args.reuse_raw)
        packet = write_once(args.output, build_packet(panel, policy, policy_hash, source_result))
        print(json.dumps({
            "status": packet["status"],
            "packet_hash": packet["packet_hash"],
            "signal_cutoff": packet["data"]["signal_cutoff"],
            "observed_through": packet["data"]["observed_through"],
        }, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, httpx.HTTPError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
