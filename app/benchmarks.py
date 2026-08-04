"""Frozen null comparisons for timing skill; synthetic diagnostics only."""

from __future__ import annotations

import hashlib
import json
import random
from pydantic import BaseModel, Field

from app.decision_packet import DecisionPacket, ExecutionCostPolicy
from app.experiment import ExperimentManifest, SplitId, load_manifest, split_for_timestamp
from app.ledger import EventType, LedgerEvent, MICROS, Side, curve_receipt, reduce_curve, to_micros
from app.order_journal import JournalEvent
from app.performance import split_metrics
from app.proof_kernel import Bar

MAX_NULL_TRIALS = 10_000
MAX_BENCHMARK_BARS = 100_000
MAX_BENCHMARK_PACKETS = 10_000
MAX_BENCHMARK_EVENTS = 500_000


class NullBenchmarkReceipt(BaseModel):
    split_id: SplitId
    strategy_net_pnl_micros: int
    buy_hold_net_pnl_micros: int
    matched_random_trial_count: int
    matched_random_seed: int
    matched_episode_count: int
    random_median_pnl_micros: int
    random_p95_pnl_micros: int
    strategy_percentile_bps: int = Field(ge=0, le=10_000)
    ledger_root_hash: str
    bars_hash: str
    packet_set_hash: str
    journal_root_hash: str
    manifest_hash: str
    evidence_scope: str = "synthetic_timing_diagnostic_not_promotion_evidence"


def _round_adverse(reference_micros: int, ppm: int) -> int:
    quotient, remainder = divmod(reference_micros * ppm, MICROS)
    if remainder * 2 > MICROS or (remainder * 2 == MICROS and quotient % 2):
        quotient += 1
    return max(quotient, 1)


def _long_episode_pnl(
    entry: Bar,
    exit: Bar,
    qty: int,
    policy: ExecutionCostPolicy,
) -> int:
    entry_ref = to_micros(entry.close)
    exit_ref = to_micros(exit.close)
    entry_price = entry_ref + _round_adverse(entry_ref, policy.half_spread_ppm) + _round_adverse(
        entry_ref, policy.slippage_ppm
    )
    exit_price = exit_ref - _round_adverse(exit_ref, policy.half_spread_ppm) - _round_adverse(
        exit_ref, policy.slippage_ppm
    )
    return (exit_price - entry_price) * qty - 2 * policy.commission_per_fill_micros


def _matched_starts(
    eligible: list[int], count: int, hold_bars: int, rng: random.Random
) -> list[int]:
    candidates = list(eligible)
    rng.shuffle(candidates)
    chosen: list[int] = []
    for candidate in candidates:
        if all(abs(candidate - prior) > hold_bars for prior in chosen):
            chosen.append(candidate)
            if len(chosen) == count:
                return sorted(chosen)
    raise ValueError("split cannot fit the matched random episode count without overlap")


def _median_half_even(values: list[int]) -> int:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    total = ordered[middle - 1] + ordered[middle]
    sign = -1 if total < 0 else 1
    quotient, remainder = divmod(abs(total), 2)
    if remainder and quotient % 2:
        quotient += 1
    return sign * quotient


def evaluate_null_benchmarks(
    bars: list[Bar],
    packets: list[DecisionPacket],
    events: list[LedgerEvent],
    journal: list[JournalEvent],
    manifest: ExperimentManifest,
    *,
    trial_count: int,
    seed: int,
    expected_manifest_hash: str,
    expected_bars_hash: str,
    expected_packet_hashes: list[str],
) -> NullBenchmarkReceipt:
    if len(bars) > MAX_BENCHMARK_BARS:
        raise ValueError(f"null benchmark bars exceed {MAX_BENCHMARK_BARS}")
    if len(packets) > MAX_BENCHMARK_PACKETS:
        raise ValueError(f"null benchmark packets exceed {MAX_BENCHMARK_PACKETS}")
    if len(events) > MAX_BENCHMARK_EVENTS or len(journal) > MAX_BENCHMARK_EVENTS:
        raise ValueError(f"null benchmark event rows exceed {MAX_BENCHMARK_EVENTS}")
    if not 1 <= trial_count <= MAX_NULL_TRIALS:
        raise ValueError(f"null trial_count must be between 1 and {MAX_NULL_TRIALS}")
    if trial_count != manifest.null_trial_count or seed != manifest.null_seed:
        raise ValueError("null benchmark parameters must match the frozen experiment manifest")
    if not packets:
        raise ValueError("null benchmarks require decision packets")
    identities = {(packet.strategy.id, packet.strategy.config_hash) for packet in packets}
    if len(identities) != 1:
        raise ValueError("null benchmarks require one strategy identity")
    if identities != {(manifest.strategy_id, manifest.strategy_config_hash)}:
        raise ValueError("null benchmark strategy must match the frozen experiment manifest")
    packet_ids = [packet.packet_id for packet in packets]
    if len(packet_ids) != len(set(packet_ids)):
        raise ValueError("null benchmarks require unique packet ids")
    if any(bar.index != index for index, bar in enumerate(bars)):
        raise ValueError("null benchmarks require dense bar indices")
    if any(left.ts >= right.ts for left, right in zip(bars, bars[1:])):
        raise ValueError("null benchmarks require strictly increasing bar timestamps")
    receipt = curve_receipt(events, bars, packets, journal)
    if not receipt.acceptable:
        raise ValueError("null benchmarks require an acceptable bound ledger receipt")
    if (
        receipt.cross_stream_reconciliation != "PASS"
        or receipt.order_journal_receipt is None
        or not receipt.order_journal_receipt.acceptable
    ):
        raise ValueError("null benchmarks require an acceptable order journal receipt")
    from app.promotion import POLICY

    frozen_manifest, actual_manifest_hash = load_manifest()
    if manifest != frozen_manifest:
        raise ValueError("null benchmark manifest must equal the frozen repository manifest")
    if (
        expected_manifest_hash != POLICY["experiment_manifest_hash"]
        or actual_manifest_hash != expected_manifest_hash
    ):
        raise ValueError("null benchmark manifest hash must match promotion policy")
    if receipt.bars_hash != expected_bars_hash:
        raise ValueError("null benchmark bars must match the verified kernel corpus")
    if receipt.packet_hashes != expected_packet_hashes:
        raise ValueError("null benchmark packets must match the verified kernel packet set")
    packet_set_hash = "sha256:" + hashlib.sha256(
        json.dumps(receipt.packet_hashes, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    points = reduce_curve(events)
    tags = [split_for_timestamp(point.at_utc, manifest) for point in points]
    test_metric = next(
        (metric for metric in split_metrics(points, tags, events) if metric.split_id is SplitId.TEST),
        None,
    )
    if test_metric is None:
        raise ValueError("TEST split has no strategy observations")

    test_window = next(window for window in manifest.split_windows if window.split_id is SplitId.TEST)
    test_indices = [
        bar.index for bar in bars if test_window.start_utc <= bar.ts <= test_window.end_utc
    ]
    if not test_indices:
        raise ValueError("TEST split has no bars")
    strategy = packets[0].strategy
    fill_sides: dict[str, set[Side]] = {}
    cross_boundary_ids: set[str] = set()
    for event in events:
        if event.type is not EventType.ORDER_FILL or not event.packet_id or not event.side:
            continue
        inside = test_window.start_utc <= event.at_utc <= test_window.end_utc
        if inside:
            fill_sides.setdefault(event.packet_id, set()).add(event.side)
        elif event.packet_id in fill_sides:
            cross_boundary_ids.add(event.packet_id)
    # Also catch entries before TEST whose exits land inside it.
    all_fill_times: dict[str, list[bool]] = {}
    for event in events:
        if event.type is EventType.ORDER_FILL and event.packet_id:
            all_fill_times.setdefault(event.packet_id, []).append(
                test_window.start_utc <= event.at_utc <= test_window.end_utc
            )
    cross_boundary_ids |= {
        packet_id
        for packet_id, flags in all_fill_times.items()
        if any(flags) and not all(flags)
    }
    if cross_boundary_ids:
        raise ValueError("TEST null benchmark does not allow cross-boundary episodes")
    completed_ids = {
        packet_id
        for packet_id, sides in fill_sides.items()
        if sides == {Side.BUY, Side.SELL}
    }
    episode_count = len(completed_ids)
    if episode_count < 1:
        raise ValueError("TEST split has no completed strategy episodes")

    start, end = test_indices[0], test_indices[-1]
    buy_hold = _long_episode_pnl(
        bars[start], bars[end], strategy.position_qty, strategy.execution_cost_policy
    )
    eligible = list(range(start, end - strategy.hold_bars + 1))
    trial_pnls: list[int] = []
    for trial in range(trial_count):
        rng = random.Random(seed + trial)
        starts = _matched_starts(eligible, episode_count, strategy.hold_bars, rng)
        trial_pnls.append(
            sum(
                _long_episode_pnl(
                    bars[index],
                    bars[index + strategy.hold_bars],
                    strategy.position_qty,
                    strategy.execution_cost_policy,
                )
                for index in starts
            )
        )
    ordered = sorted(trial_pnls)
    p95_index = min(len(ordered) - 1, (95 * len(ordered) + 99) // 100 - 1)
    percentile_bps = (
        10_000
        * sum(value <= test_metric.net_pnl_micros for value in trial_pnls)
        // len(trial_pnls)
    )
    return NullBenchmarkReceipt(
        split_id=SplitId.TEST,
        strategy_net_pnl_micros=test_metric.net_pnl_micros,
        buy_hold_net_pnl_micros=buy_hold,
        matched_random_trial_count=trial_count,
        matched_random_seed=seed,
        matched_episode_count=episode_count,
        random_median_pnl_micros=_median_half_even(trial_pnls),
        random_p95_pnl_micros=ordered[p95_index],
        strategy_percentile_bps=percentile_bps,
        ledger_root_hash=receipt.ledger_root_hash,
        bars_hash=receipt.bars_hash,
        packet_set_hash=packet_set_hash,
        journal_root_hash=receipt.order_journal_receipt.journal_root_hash,
        manifest_hash=actual_manifest_hash,
    )
