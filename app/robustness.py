"""Frozen post-hoc ablations and parameter neighbors; diagnostic only."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, Field

from app.benchmarks import _long_episode_pnl
from app.decision_packet import DecisionPacket
from app.experiment import ExperimentManifest, RobustnessVariant, SplitId, load_manifest, split_for_timestamp
from app.ledger import LedgerEvent, curve_receipt, reduce_curve
from app.order_journal import JournalEvent
from app.performance import split_metrics
from app.proof_kernel import Bar

MAX_ROBUSTNESS_BARS = 100_000
MAX_ROBUSTNESS_PACKETS = 10_000
MAX_ROBUSTNESS_EVENTS = 500_000
PACKET_LIMIT = 25


class RobustnessRow(BaseModel):
    variant_id: str
    fast_window: int
    slow_window: int
    hold_bars: int
    use_uptrend: bool
    use_pullback: bool
    development_net_pnl_micros: int | None
    validation_net_pnl_micros: int | None
    test_net_pnl_micros: int | None
    opened_holdout_net_pnl_micros: int | None
    development_episode_count: int
    validation_episode_count: int
    test_episode_count: int
    opened_holdout_episode_count: int
    attempted_signals: int
    collision_skips: int
    tail_drops: int
    cross_boundary_exclusions: int


class RobustnessReceipt(BaseModel):
    rows: tuple[RobustnessRow, ...] = Field(min_length=9, max_length=32)
    suite_hash: str
    manifest_hash: str
    bars_hash: str
    ledger_root_hash: str
    journal_root_hash: str
    packet_set_hash: str
    result_root_hash: str
    baseline_matches_bound_ledger: bool
    opened_holdout_observed_variants: int
    access_provenance_verified: bool = False
    evidence_scope: str = "post_hoc_synthetic_diagnostic_not_promotion_evidence"


def robustness_result_root(rows: tuple[RobustnessRow, ...] | list[RobustnessRow]) -> str:
    """Seal the ordered observations; callers must recompute, never trust a carried label."""

    return "sha256:" + hashlib.sha256(
        json.dumps(
            [row.model_dump(mode="json") for row in rows],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _sma(bars: list[Bar], index: int, window: int) -> float:
    return round(sum(bar.close for bar in bars[index - window + 1 : index + 1]) / window, 9)


def _signals(bars: list[Bar], variant: RobustnessVariant) -> list[int]:
    out: list[int] = []
    previous = False
    for index, bar in enumerate(bars):
        if index < variant.slow_window:
            current = False
        else:
            fast = _sma(bars, index, variant.fast_window)
            slow = _sma(bars, index, variant.slow_window)
            current = (
                (not variant.use_uptrend or fast > slow)
                and (not variant.use_pullback or bar.close < fast)
            )
        if current and not previous:
            out.append(index)
        previous = current
    return out[:PACKET_LIMIT]


def _run_variant(
    bars: list[Bar], variant: RobustnessVariant, manifest: ExperimentManifest, strategy
) -> RobustnessRow:
    pnl: dict[SplitId, int] = {}
    counts: dict[SplitId, int] = {}
    busy_through = -1
    collisions = tails = cross_boundary = 0
    signals = _signals(bars, variant)
    for signal_index in signals:
        entry_index = signal_index + 1
        exit_index = entry_index + variant.hold_bars
        if entry_index <= busy_through:
            collisions += 1
            continue
        if exit_index >= len(bars):
            tails += 1
            continue
        busy_through = exit_index
        entry_split = split_for_timestamp(bars[entry_index].ts, manifest)
        exit_split = split_for_timestamp(bars[exit_index].ts, manifest)
        if entry_split is None or entry_split is not exit_split:
            cross_boundary += 1
            continue
        pnl[entry_split] = pnl.get(entry_split, 0) + _long_episode_pnl(
            bars[entry_index],
            bars[exit_index],
            strategy.position_qty,
            strategy.execution_cost_policy,
        )
        counts[entry_split] = counts.get(entry_split, 0) + 1
    return RobustnessRow(
        variant_id=variant.variant_id,
        fast_window=variant.fast_window,
        slow_window=variant.slow_window,
        hold_bars=variant.hold_bars,
        use_uptrend=variant.use_uptrend,
        use_pullback=variant.use_pullback,
        development_net_pnl_micros=pnl.get(SplitId.DEVELOPMENT),
        validation_net_pnl_micros=pnl.get(SplitId.VALIDATION),
        test_net_pnl_micros=pnl.get(SplitId.TEST),
        opened_holdout_net_pnl_micros=pnl.get(SplitId.OPENED_HOLDOUT),
        development_episode_count=counts.get(SplitId.DEVELOPMENT, 0),
        validation_episode_count=counts.get(SplitId.VALIDATION, 0),
        test_episode_count=counts.get(SplitId.TEST, 0),
        opened_holdout_episode_count=counts.get(SplitId.OPENED_HOLDOUT, 0),
        attempted_signals=len(signals),
        collision_skips=collisions,
        tail_drops=tails,
        cross_boundary_exclusions=cross_boundary,
    )


def evaluate_robustness(
    bars: list[Bar],
    packets: list[DecisionPacket],
    events: list[LedgerEvent],
    journal: list[JournalEvent],
    manifest: ExperimentManifest,
    *,
    expected_bars_hash: str,
    expected_packet_hashes: list[str],
) -> RobustnessReceipt:
    if len(bars) > MAX_ROBUSTNESS_BARS or len(packets) > MAX_ROBUSTNESS_PACKETS:
        raise ValueError("robustness corpus exceeds bounded bars or packets")
    if len(events) > MAX_ROBUSTNESS_EVENTS or len(journal) > MAX_ROBUSTNESS_EVENTS:
        raise ValueError("robustness event rows exceed bound")
    if not packets:
        raise ValueError("robustness requires canonical packets")
    frozen_manifest, manifest_hash = load_manifest()
    from app.promotion import POLICY

    if manifest != frozen_manifest or manifest_hash != POLICY["experiment_manifest_hash"]:
        raise ValueError("robustness requires the policy-bound experiment manifest")
    receipt = curve_receipt(events, bars, packets, journal)
    if (
        not receipt.acceptable
        or receipt.cross_stream_reconciliation != "PASS"
        or receipt.order_journal_receipt is None
        or not receipt.order_journal_receipt.acceptable
    ):
        raise ValueError("robustness requires acceptable ledger and journal receipts")
    if receipt.bars_hash != expected_bars_hash or receipt.packet_hashes != expected_packet_hashes:
        raise ValueError("robustness corpus does not match the verified kernel artifacts")
    identities = {(packet.strategy.id, packet.strategy.config_hash) for packet in packets}
    if identities != {(manifest.strategy_id, manifest.strategy_config_hash)}:
        raise ValueError("robustness strategy does not match the experiment manifest")

    points = reduce_curve(events)
    tags = [split_for_timestamp(point.at_utc, manifest) for point in points]
    canonical = {row.split_id: row.net_pnl_micros for row in split_metrics(points, tags, events)}
    rows = tuple(
        _run_variant(bars, variant, manifest, packets[0].strategy)
        for variant in manifest.robustness_variants
    )
    result_root_hash = robustness_result_root(rows)
    if result_root_hash != manifest.robustness_result_root:
        raise ValueError("robustness results do not match the policy-bound result root")
    baseline = rows[0]
    baseline_values = {
        SplitId.DEVELOPMENT: baseline.development_net_pnl_micros,
        SplitId.VALIDATION: baseline.validation_net_pnl_micros,
        SplitId.TEST: baseline.test_net_pnl_micros,
    }
    baseline_matches = all(baseline_values[split] == value for split, value in canonical.items())
    if not baseline_matches:
        raise ValueError("robustness baseline does not reproduce the bound ledger by split")
    suite_payload = [variant.model_dump(mode="json") for variant in manifest.robustness_variants]
    suite_hash = "sha256:" + hashlib.sha256(
        json.dumps(suite_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    packet_set_hash = "sha256:" + hashlib.sha256(
        json.dumps(receipt.packet_hashes, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return RobustnessReceipt(
        rows=rows,
        suite_hash=suite_hash,
        manifest_hash=manifest_hash,
        bars_hash=receipt.bars_hash,
        ledger_root_hash=receipt.ledger_root_hash,
        journal_root_hash=receipt.order_journal_receipt.journal_root_hash,
        packet_set_hash=packet_set_hash,
        result_root_hash=result_root_hash,
        baseline_matches_bound_ledger=True,
        opened_holdout_observed_variants=sum(
            row.opened_holdout_net_pnl_micros is not None for row in rows
        ),
    )
