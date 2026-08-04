"""Matched null benchmark scenarios; synthetic and explicitly non-promotional."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app import ledger as lg
from app import proof_kernel as pk
from app.benchmarks import (
    MAX_BENCHMARK_BARS,
    MAX_NULL_TRIALS,
    evaluate_null_benchmarks,
)
from app.experiment import load_manifest
from app.decision_packet import canonical_hash
from app.order_journal import build_order_journal


@pytest.fixture(scope="module")
def artifacts():
    bars = pk.fixture_bars()
    packets = list(pk.packets(25, bars))
    return (
        bars,
        packets,
        lg.build_ledger(bars, packets),
        build_order_journal(bars, packets),
        load_manifest()[0],
    )


def evaluate(artifacts, *, authority_artifacts=None, **overrides):
    bars, packets, events, journal, manifest = artifacts
    authority_artifacts = authority_artifacts or artifacts
    arguments = {
        "trial_count": 128,
        "seed": 240803,
        "expected_manifest_hash": load_manifest()[1],
        "expected_bars_hash": pk.bars_hash(authority_artifacts[0]),
        "expected_packet_hashes": [
            canonical_hash(packet) for packet in authority_artifacts[1]
        ],
    }
    arguments.update(overrides)
    return evaluate_null_benchmarks(
        bars, packets, events, journal, manifest, **arguments
    )


def test_researcher_gets_test_only_buy_hold_and_matched_random_nulls(artifacts):
    bars, packets, events, journal, manifest = artifacts
    receipt = evaluate(artifacts)
    assert receipt.split_id.value == "TEST"
    assert receipt.matched_random_trial_count == 128
    assert receipt.matched_episode_count == 2
    assert 0 <= receipt.strategy_percentile_bps <= 10_000
    assert receipt.evidence_scope == "synthetic_timing_diagnostic_not_promotion_evidence"
    assert receipt.manifest_hash == load_manifest()[1]
    assert receipt.bars_hash == pk.bars_hash(bars)
    assert receipt.journal_root_hash.startswith("sha256:")
    assert receipt.packet_set_hash.startswith("sha256:")


def test_null_trials_are_byte_stable_and_caller_cannot_change_frozen_parameters(artifacts):
    bars, packets, events, journal, manifest = artifacts
    first = evaluate(artifacts)
    assert evaluate(artifacts) == first
    with pytest.raises(ValueError, match="frozen experiment manifest"):
        evaluate(artifacts, seed=240804)
    with pytest.raises(ValueError, match="frozen experiment manifest"):
        evaluate(artifacts, trial_count=127)


def test_trial_budget_and_mixed_strategy_fail_closed(artifacts):
    bars, packets, events, journal, manifest = artifacts
    with pytest.raises(ValueError, match="between 1"):
        evaluate(artifacts, trial_count=MAX_NULL_TRIALS + 1, seed=1)
    mixed = list(packets)
    mixed[1] = mixed[1].model_copy(
        update={
            "strategy": mixed[1].strategy.model_copy(update={"config_hash": "other"})
        }
    )
    with pytest.raises(ValueError, match="one strategy"):
        evaluate((bars, mixed, events, journal, manifest))


def test_null_benchmark_rejects_manifest_identity_and_time_order_attacks(artifacts):
    bars, packets, events, journal, manifest = artifacts
    wrong_manifest = manifest.model_copy(update={"strategy_config_hash": "other"})
    with pytest.raises(ValueError, match="strategy must match"):
        evaluate((bars, packets, events, journal, wrong_manifest))
    malformed_bars = list(bars)
    malformed_bars[1] = replace(malformed_bars[1], ts=malformed_bars[0].ts)
    with pytest.raises(ValueError, match="strictly increasing"):
        evaluate((malformed_bars, packets, events, journal, manifest))


def test_null_benchmark_rejects_unbounded_caller_payload(artifacts):
    bars, packets, events, journal, manifest = artifacts
    with pytest.raises(ValueError, match="bars exceed"):
        evaluate(([bars[0]] * (MAX_BENCHMARK_BARS + 1), packets, events, journal, manifest))


def test_null_benchmark_rejects_safe_gap_split_substitution(artifacts):
    bars, packets, events, journal, manifest = artifacts
    windows = list(manifest.split_windows)
    windows[2] = windows[2].model_copy(
        update={"start_utc": windows[2].start_utc.replace(minute=12)}
    )
    shifted = manifest.model_copy(update={"split_windows": tuple(windows)})
    with pytest.raises(ValueError, match="frozen repository manifest"):
        evaluate((bars, packets, events, journal, shifted), authority_artifacts=artifacts)


def test_null_benchmark_rejects_unused_test_bar_and_truncated_journal(artifacts):
    bars, packets, events, journal, manifest = artifacts
    changed_bars = list(bars)
    test_index = next(
        bar.index for bar in bars if bar.ts >= manifest.split_windows[2].start_utc
    )
    changed_bars[test_index] = replace(
        changed_bars[test_index], close=changed_bars[test_index].close + 50
    )
    with pytest.raises(ValueError, match="verified kernel corpus"):
        evaluate(
            (changed_bars, packets, events, journal, manifest),
            authority_artifacts=artifacts,
        )
    with pytest.raises(ValueError, match="order journal"):
        evaluate(
            (bars, packets, events, journal[:-1], manifest),
            authority_artifacts=artifacts,
        )
