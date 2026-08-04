"""Purged pre-TEST fold matrix for the frozen post-hoc variant suite."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field

from app.benchmarks import _long_episode_pnl
from app.decision_packet import DecisionPacket, canonical_hash
from app.experiment import ExperimentManifest, WalkForwardFold, load_manifest
from app.proof_kernel import Bar, bars_hash
from app.robustness import RobustnessReceipt, _signals, robustness_result_root

MAX_WALK_FORWARD_BARS = 100_000
MAX_WALK_FORWARD_PACKETS = 10_000


def _hash(payload) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class FoldObservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    fold_id: str
    net_pnl_micros: int | None
    episode_count: int


class WalkForwardRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    variant_id: str
    folds: tuple[FoldObservation, ...] = Field(min_length=3, max_length=12)


class WalkForwardReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    rows: tuple[WalkForwardRow, ...] = Field(min_length=9, max_length=32)
    fold_plan_hash: str
    matrix_root_hash: str
    manifest_hash: str
    bars_hash: str
    robustness_result_root: str
    folds_end_before_test: bool
    purge_verified: bool
    access_provenance_verified: bool = False
    evidence_scope: str = "post_hoc_synthetic_fold_diagnostic_not_promotion_evidence"


def _observe_fold(bars: list[Bar], variant, strategy, fold: WalkForwardFold) -> FoldObservation:
    """Allocate positions inside one fold so activity outside it cannot suppress a trade."""

    busy_through = -1
    values: list[int] = []
    for signal_index in _signals(bars, variant):
        entry_index = signal_index + 1
        exit_index = entry_index + variant.hold_bars
        if exit_index >= len(bars):
            continue
        if not (
            fold.evaluation_start_utc <= bars[entry_index].ts
            and bars[exit_index].ts <= fold.evaluation_end_utc
        ):
            continue
        if entry_index <= busy_through:
            continue
        busy_through = exit_index
        values.append(
            _long_episode_pnl(
                bars[entry_index],
                bars[exit_index],
                strategy.position_qty,
                strategy.execution_cost_policy,
            )
        )
    return FoldObservation(
        fold_id=fold.fold_id,
        net_pnl_micros=sum(values) if values else None,
        episode_count=len(values),
    )


def evaluate_walk_forward(
    bars: list[Bar],
    packets: list[DecisionPacket],
    manifest: ExperimentManifest,
    robustness: RobustnessReceipt,
    *,
    expected_bars_hash: str,
    expected_packet_hashes: list[str],
) -> WalkForwardReceipt:
    if len(bars) > MAX_WALK_FORWARD_BARS or len(packets) > MAX_WALK_FORWARD_PACKETS:
        raise ValueError("walk-forward corpus exceeds bound")
    if not packets:
        raise ValueError("walk-forward requires canonical packets")
    frozen_manifest, manifest_hash = load_manifest()
    from app.promotion import POLICY

    if manifest != frozen_manifest or manifest_hash != POLICY["experiment_manifest_hash"]:
        raise ValueError("walk-forward requires the policy-bound experiment manifest")
    actual_bars_hash = bars_hash(bars)
    if actual_bars_hash != expected_bars_hash or actual_bars_hash != robustness.bars_hash:
        raise ValueError("walk-forward bars do not match the bound robustness corpus")
    if [canonical_hash(packet) for packet in packets] != expected_packet_hashes:
        raise ValueError("walk-forward packets do not match the verified kernel packet set")
    recomputed_robustness_root = robustness_result_root(robustness.rows)
    if (
        robustness.result_root_hash != recomputed_robustness_root
        or recomputed_robustness_root != manifest.robustness_result_root
    ):
        raise ValueError("walk-forward variants do not match the policy-bound result universe")
    row_ids = tuple(row.variant_id for row in robustness.rows)
    manifest_ids = tuple(variant.variant_id for variant in manifest.robustness_variants)
    if row_ids != manifest_ids:
        raise ValueError("walk-forward robustness rows do not match the ordered variant universe")
    identities = {(packet.strategy.id, packet.strategy.config_hash) for packet in packets}
    if identities != {(manifest.strategy_id, manifest.strategy_config_hash)}:
        raise ValueError("walk-forward strategy does not match the experiment manifest")

    rows = tuple(
        WalkForwardRow(
            variant_id=variant.variant_id,
            folds=tuple(
                _observe_fold(bars, variant, packets[0].strategy, fold)
                for fold in manifest.walk_forward_folds
            ),
        )
        for variant in manifest.robustness_variants
    )
    fold_payload = [fold.model_dump(mode="json") for fold in manifest.walk_forward_folds]
    matrix_payload = [row.model_dump(mode="json") for row in rows]
    matrix_root_hash = _hash(matrix_payload)
    if matrix_root_hash != manifest.walk_forward_result_root:
        raise ValueError("walk-forward matrix does not match the policy-bound result root")
    test_start = next(window.start_utc for window in manifest.split_windows if window.split_id.value == "TEST")
    purge_verified = all(
        int((fold.evaluation_start_utc - fold.train_end_utc).total_seconds() // 60) - 1
        >= fold.purge_bars
        for fold in manifest.walk_forward_folds
    )
    return WalkForwardReceipt(
        rows=rows,
        fold_plan_hash=_hash(fold_payload),
        matrix_root_hash=matrix_root_hash,
        manifest_hash=manifest_hash,
        bars_hash=actual_bars_hash,
        robustness_result_root=robustness.result_root_hash,
        folds_end_before_test=all(
            fold.evaluation_end_utc < test_start for fold in manifest.walk_forward_folds
        ),
        purge_verified=purge_verified,
    )
