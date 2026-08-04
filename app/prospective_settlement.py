"""Settle a frozen prospective allocation only after its future window closes."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import sys
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from .real_alpha import PricePanel, _canonical_hash, _panel_hash, fetch_panel


REPO_ROOT = Path(__file__).parents[1]
DEFAULT_POLICY = REPO_ROOT / "research" / "prospective_settlement_policy_v1.json"
EXPECTED_POLICY_SHA256 = "9b231159cc7a1afb1071fd754d05ee3e084fc0b2f050d12c03194f6e41d65acf"


class OutcomeNotReady(ValueError):
    pass


def load_policy(path: Path = DEFAULT_POLICY) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_POLICY_SHA256:
        raise ValueError("prospective settlement policy differs from its preregistered trust root")
    policy = json.loads(raw)
    if policy.get("evaluator_hash") != _evaluator_hash():
        raise ValueError("prospective settlement policy is bound to another evaluator")
    return policy, "sha256:" + digest


def load_packet(policy: dict[str, Any]) -> dict[str, Any]:
    binding = policy["packet"]
    packet = json.loads((REPO_ROOT / binding["path"]).read_text(encoding="utf-8"))
    body = {key: value for key, value in packet.items() if key != "packet_hash"}
    if packet.get("packet_hash") != binding["packet_hash"] or _canonical_hash(body) != binding["packet_hash"]:
        raise ValueError("prospective packet differs from the frozen settlement binding")
    if packet.get("status") != "FROZEN_AWAITING_FUTURE_OUTCOMES" or packet.get("promotion_eligible") is not False:
        raise ValueError("settlement requires a frozen non-promotable packet")
    return packet


def _close_utc(day, config: dict[str, Any]) -> datetime:
    utc_close = time.fromisoformat(config["regular_close_utc_time_for_frozen_window"])
    return datetime.combine(day, utc_close, timezone.utc)


def _window(panel: PricePanel, packet: dict[str, Any], policy: dict[str, Any]) -> tuple[int, int, datetime]:
    packet_seal = datetime.fromisoformat(packet["outcome_boundary"]["sealed_at_utc"])
    policy_freeze = datetime.fromisoformat(policy["frozen_at_utc"].replace("Z", "+00:00"))
    authority = max(packet_seal, policy_freeze)
    config = policy["settlement"]
    entry = next((index for index, day in enumerate(panel.dates) if _close_utc(day, config) > authority), None)
    if entry is None:
        raise OutcomeNotReady("no common session close exists after the frozen authority time")
    entry_day = panel.dates[entry]
    end = next((index for index in range(entry + 1, len(panel.dates))
                if (panel.dates[index].year, panel.dates[index].month) != (entry_day.year, entry_day.month)), None)
    if end is None:
        raise OutcomeNotReady("the first common close in the next calendar month is not available")
    return entry, end, authority


def _fixed_allocation_return(
    panel: PricePanel, entry: int, end: int, risky_weights: np.ndarray, cash_weight: float, entry_bps: int, exit_bps: int
) -> float:
    if np.any(risky_weights < 0) or risky_weights.sum() + cash_weight > 1.0 + 1e-12:
        raise ValueError("settlement allocation is invalid or leveraged")
    entry_cost = risky_weights.sum() * entry_bps / 10_000.0
    investable = 1.0 - entry_cost
    risky_values = investable * risky_weights * panel.risky_prices[end] / panel.risky_prices[entry]
    cash_value = investable * cash_weight * panel.cash_prices[end] / panel.cash_prices[entry]
    unallocated = investable * max(0.0, 1.0 - risky_weights.sum() - cash_weight)
    exit_cost = risky_values.sum() * exit_bps / 10_000.0
    return float(risky_values.sum() + cash_value + unallocated - exit_cost - 1.0)


def settle(panel: PricePanel, packet: dict[str, Any], policy: dict[str, Any], policy_hash: str) -> dict[str, Any]:
    entry, end, authority = _window(panel, packet, policy)
    config = policy["settlement"]
    outcomes = []
    for forecast in packet["forecasts"]:
        weights = np.asarray([forecast["risky_weights"][symbol] for symbol in panel.risky_symbols], dtype=float)
        outcome = _fixed_allocation_return(
            panel, entry, end, weights, float(forecast["cash_weight"]),
            config["entry_one_way_bps_on_risky_notional"], config["exit_one_way_bps_on_risky_notional"],
        )
        outcomes.append({"strategy_id": forecast["strategy_id"], "role": forecast["role"], "net_return": outcome})
    primary = next(row for row in outcomes if row["role"] == "PRIMARY")
    benchmark = next(row for row in outcomes if row["role"] == "BENCHMARK")
    result = {
        "version": "nodequant-prospective-settlement-result-v1",
        "status": "COMPLETE_PACKET_OUTCOME",
        "policy_hash": policy_hash,
        "packet_hash": packet["packet_hash"],
        "evaluator_hash": _evaluator_hash(),
        "data": {"panel_hash": _panel_hash(panel), "raw_hashes": panel.raw_hashes, "quality": panel.quality},
        "window": {
            "authority_time_utc": authority.isoformat(), "entry_close_date": panel.dates[entry].isoformat(),
            "evaluation_end_close_date": panel.dates[end].isoformat(), "measured_close_to_close_intervals": end - entry,
        },
        "outcomes": outcomes,
        "primary_net_active_return": primary["net_return"] - benchmark["net_return"],
        "promotion_eligible": False,
        "promotion_ceiling": policy["promotion_ceiling"],
    }
    result["result_hash"] = _canonical_hash(result)
    result["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    return result


def _evaluator_hash() -> str:
    source = "\n".join(inspect.getsource(value) for value in (load_packet, _close_utc, _window, _fixed_allocation_return, settle)).encode()
    return "sha256:" + hashlib.sha256(source).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Settle a frozen prospective packet after its future window closes.")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--raw-directory", type=Path, default=REPO_ROOT / "evidence" / "prospective-settlement" / "raw")
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "evidence" / "prospective-settlement" / "result-2026-08.json")
    parser.add_argument("--reuse-raw", action="store_true")
    args = parser.parse_args(argv)
    try:
        policy, policy_hash = load_policy(args.policy)
        packet = load_packet(policy)
        panel = fetch_panel({"data": policy["data"]}, args.raw_directory, refresh=not args.reuse_raw)
        result = settle(panel, packet, policy, policy_hash)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"status": result["status"], "result_hash": result["result_hash"]}, sort_keys=True))
        return 0
    except OutcomeNotReady as exc:
        print(json.dumps({"status": "AWAITING_FUTURE_OUTCOMES", "reason": str(exc)}, sort_keys=True))
        return 5
    except (OSError, ValueError, json.JSONDecodeError, httpx.HTTPError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
