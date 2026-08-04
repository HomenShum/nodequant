"""Scenario tests for immutable split geometry and honest access provenance."""

from __future__ import annotations

import inspect
import json
from datetime import timedelta

from app import promotion as pm
from app import proof_kernel as pk
from app.experiment import SplitId, assess_split_discipline, load_manifest


def test_reviewer_gets_four_chronological_tags_but_no_false_access_proof():
    bars = pk.fixture_bars()
    manifest, digest = load_manifest()
    receipt, tags = assess_split_discipline(
        [bar.ts for bar in bars],
        [(pk.STRATEGY.id, pk.STRATEGY.config_hash)],
        digest,
    )

    assert digest.startswith("sha256:")
    assert {tag for tag in tags} == set(SplitId)
    assert tags == sorted(tags, key=list(SplitId).index)
    assert receipt.untagged_points == 0
    assert receipt.manifest_matches_policy
    assert receipt.candidate_frozen_before_test_declared
    assert not receipt.optimizer_test_access_declared
    assert not receipt.provenance_verified
    assert not receipt.acceptable
    assert manifest.provenance == "self_attested_repository_artifact"


def test_events_outside_the_frozen_corpus_fail_closed_as_untagged():
    bars = pk.fixture_bars()
    timestamps = [bars[0].ts - timedelta(minutes=1), bars[-1].ts + timedelta(minutes=1)]
    receipt, tags = assess_split_discipline(
        timestamps, [(pk.STRATEGY.id, pk.STRATEGY.config_hash)]
    )
    assert tags == [None, None]
    assert receipt.untagged_points == 2
    assert not receipt.acceptable


def test_split_discipline_has_no_caller_booleans_at_the_promotion_boundary():
    signature = inspect.signature(pm.evaluate)
    assert "candidate_frozen_before_test" not in signature.parameters
    assert "optimizer_had_test_access" not in signature.parameters


def test_every_packet_strategy_and_the_policy_seal_are_bound():
    bars = pk.fixture_bars()
    _, digest = load_manifest()
    receipt, _ = assess_split_discipline(
        [bar.ts for bar in bars],
        [
            (pk.STRATEGY.id, pk.STRATEGY.config_hash),
            (pk.STRATEGY.id, "changed-after-first-packet"),
        ],
        digest,
    )
    assert not receipt.strategy_matches
    assert receipt.manifest_matches_policy
    assert not receipt.acceptable

    receipt, _ = assess_split_discipline(
        [bar.ts for bar in bars],
        [(pk.STRATEGY.id, pk.STRATEGY.config_hash)],
        "sha256:" + "0" * 64,
    )
    assert not receipt.manifest_matches_policy
    assert not receipt.acceptable


def test_naive_freeze_timestamp_fails_during_manifest_validation(tmp_path):
    manifest, _ = load_manifest()
    payload = manifest.model_dump(mode="json")
    payload["candidate_frozen_at_utc"] = "2026-01-06T18:29:00"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    import pytest

    with pytest.raises(ValueError, match="timezone-aware"):
        load_manifest(path)
