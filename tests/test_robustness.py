"""Post-hoc robustness scenarios with a canonical baseline anchor."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app import ledger as lg
from app import proof_kernel as pk
from app.decision_packet import canonical_hash
from app.experiment import load_manifest
from app.order_journal import build_order_journal
from app.robustness import evaluate_robustness


@pytest.fixture(scope="module")
def artifacts():
    bars = pk.fixture_bars()
    packets = list(pk.packets(25, bars))
    return bars, packets, lg.build_ledger(bars, packets), build_order_journal(bars, packets), load_manifest()[0]


def evaluate(artifacts, *, authority_artifacts=None):
    bars, packets, events, journal, manifest = artifacts
    authority_artifacts = authority_artifacts or artifacts
    return evaluate_robustness(
        bars,
        packets,
        events,
        journal,
        manifest,
        expected_bars_hash=pk.bars_hash(authority_artifacts[0]),
        expected_packet_hashes=[canonical_hash(packet) for packet in authority_artifacts[1]],
    )


def test_researcher_gets_frozen_ablations_and_neighbors_with_exact_baseline(artifacts):
    receipt = evaluate(artifacts)
    rows = {row.variant_id: row for row in receipt.rows}

    assert list(rows) == [
        "baseline",
        "without-uptrend",
        "without-pullback",
        "fast-15",
        "fast-25",
        "slow-40",
        "slow-60",
        "hold-15",
        "hold-45",
    ]
    baseline = rows["baseline"]
    assert baseline.development_net_pnl_micros == -310_041_297
    assert baseline.validation_net_pnl_micros == -199_681_445
    assert baseline.test_net_pnl_micros == -42_701_743
    assert (baseline.development_episode_count, baseline.validation_episode_count, baseline.test_episode_count) == (10, 5, 2)
    assert receipt.baseline_matches_bound_ledger
    assert not receipt.access_provenance_verified
    assert receipt.evidence_scope == "post_hoc_synthetic_diagnostic_not_promotion_evidence"
    assert receipt.journal_root_hash.startswith("sha256:")
    assert len({row.test_net_pnl_micros for row in receipt.rows}) > 1
    assert receipt.opened_holdout_observed_variants == 2
    assert rows["without-pullback"].opened_holdout_net_pnl_micros == 52_123_192
    assert rows["fast-25"].opened_holdout_net_pnl_micros == -65_448_440
    assert rows["baseline"].opened_holdout_net_pnl_micros is None


def test_robustness_is_deterministic_and_absent_split_results_are_not_zero(artifacts):
    first = evaluate(artifacts)
    assert evaluate(artifacts) == first
    assert all(row.test_net_pnl_micros is None or isinstance(row.test_net_pnl_micros, int) for row in first.rows)
    assert all(row.attempted_signals <= 25 for row in first.rows)


def test_manifest_bar_and_journal_substitution_fail_closed(artifacts):
    bars, packets, events, journal, manifest = artifacts
    changed_manifest = manifest.model_copy(
        update={"robustness_variants": tuple(reversed(manifest.robustness_variants))}
    )
    with pytest.raises(ValueError, match="policy-bound"):
        evaluate((bars, packets, events, journal, changed_manifest), authority_artifacts=artifacts)

    changed_bars = list(bars)
    changed_bars[1800] = replace(changed_bars[1800], close=changed_bars[1800].close + 50)
    with pytest.raises(ValueError, match="verified kernel"):
        evaluate((changed_bars, packets, events, journal, manifest), authority_artifacts=artifacts)

    with pytest.raises(ValueError, match="ledger and journal"):
        evaluate((bars, packets, events, journal[:-1], manifest), authority_artifacts=artifacts)
