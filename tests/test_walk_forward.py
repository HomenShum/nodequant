"""Purged pre-TEST matrix scenarios and authority attacks."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app import ledger as lg
from app import proof_kernel as pk
from app.decision_packet import canonical_hash
from app.experiment import load_manifest
from app.order_journal import build_order_journal
from app.robustness import evaluate_robustness
from app.walk_forward import evaluate_walk_forward


@pytest.fixture(scope="module")
def artifacts():
    bars = pk.fixture_bars()
    packets = list(pk.packets(25, bars))
    manifest, _ = load_manifest()
    robustness = evaluate_robustness(
        bars,
        packets,
        lg.build_ledger(bars, packets),
        build_order_journal(bars, packets),
        manifest,
        expected_bars_hash=pk.bars_hash(bars),
        expected_packet_hashes=[canonical_hash(packet) for packet in packets],
    )
    return bars, packets, manifest, robustness


def evaluate(artifacts, bars=None, packets=None):
    canonical_bars, canonical_packets, manifest, robustness = artifacts
    return evaluate_walk_forward(
        bars or canonical_bars,
        packets or canonical_packets,
        manifest,
        robustness,
        expected_bars_hash=pk.bars_hash(canonical_bars),
        expected_packet_hashes=[canonical_hash(packet) for packet in canonical_packets],
    )


def test_researcher_gets_three_purged_pretest_folds_with_falsifying_matrix(artifacts):
    receipt = evaluate(artifacts)
    rows = {row.variant_id: row for row in receipt.rows}

    assert receipt.folds_end_before_test
    assert receipt.purge_verified
    assert not receipt.access_provenance_verified
    assert [fold.fold_id for fold in rows["baseline"].folds] == ["WF1", "WF2", "WF3"]
    assert [fold.net_pnl_micros for fold in rows["baseline"].folds] == [
        -21_923_424,
        -67_245_910,
        -44_222_072,
    ]
    assert [fold.net_pnl_micros for fold in rows["without-pullback"].folds] == [
        74_829_795,
        32_466_414,
        92_738_335,
    ]
    assert receipt.matrix_root_hash == load_manifest()[0].walk_forward_result_root
    assert "not_promotion" in receipt.evidence_scope


def test_walk_forward_is_deterministic_and_rejects_bar_packet_or_plan_substitution(artifacts):
    assert evaluate(artifacts) == evaluate(artifacts)
    bars, packets, manifest, robustness = artifacts
    changed_bars = list(bars)
    changed_bars[400] = replace(changed_bars[400], close=changed_bars[400].close + 1)
    with pytest.raises(ValueError, match="bound robustness corpus"):
        evaluate(artifacts, bars=changed_bars)

    changed_packets = list(packets)
    changed_packets[0] = changed_packets[0].model_copy(
        update={"strategy": changed_packets[0].strategy.model_copy(update={"position_qty": 2})}
    )
    with pytest.raises(ValueError, match="verified kernel packet set"):
        evaluate(artifacts, packets=changed_packets)

    folds = list(manifest.walk_forward_folds)
    folds[0] = folds[0].model_copy(update={"purge_bars": 1})
    changed_manifest = manifest.model_copy(update={"walk_forward_folds": tuple(folds)})
    with pytest.raises(ValueError, match="policy-bound experiment manifest"):
        evaluate_walk_forward(
            bars,
            packets,
            changed_manifest,
            robustness,
            expected_bars_hash=pk.bars_hash(bars),
            expected_packet_hashes=[canonical_hash(packet) for packet in packets],
        )


@pytest.mark.parametrize("attack", ["reverse", "missing", "duplicate", "result"])
def test_upstream_robustness_rows_are_recomputed_not_trusted(artifacts, attack):
    bars, packets, manifest, robustness = artifacts
    rows = list(robustness.rows)
    if attack == "reverse":
        rows.reverse()
    elif attack == "missing":
        rows.pop()
    elif attack == "duplicate":
        rows[-1] = rows[0]
    else:
        rows[0] = rows[0].model_copy(update={"development_net_pnl_micros": 999_999_999})
    forged = robustness.model_copy(update={"rows": tuple(rows)})

    with pytest.raises(ValueError, match="policy-bound result universe"):
        evaluate_walk_forward(
            bars,
            packets,
            manifest,
            forged,
            expected_bars_hash=pk.bars_hash(bars),
            expected_packet_hashes=[canonical_hash(packet) for packet in packets],
        )
