"""Trust-boundary scenarios for the no-source-access replay bundle."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from app import proof_kernel as pk
from app.independent_oracle import MAX_ORACLE_FILE_BYTES, verify_frozen_oracle


ROOT = Path(__file__).parents[1] / "independent_oracle"


@pytest.fixture(scope="module")
def artifacts():
    bars = pk.fixture_bars()
    return bars, list(pk.packets(25, bars))


def test_reviewer_can_replay_twentyfive_signals_from_frozen_inputs(artifacts):
    bars, packets = artifacts
    receipt = verify_frozen_oracle(bars, packets)
    assert not receipt.acceptable
    assert receipt.claimed_independence.value == "separate_agent_from_frozen_spec"
    assert receipt.independence.value == "same_author_diverse_implementation"
    assert not receipt.provenance_verified
    assert receipt.signal_count == 25
    assert receipt.signal_mismatches == 0
    assert receipt.matched


def test_self_attested_authorship_cannot_upgrade_the_provenance_rung(artifacts):
    bars, packets = artifacts
    receipt = verify_frozen_oracle(bars, packets)
    assert receipt.authorship_mismatches == 0
    assert receipt.input_hash_mismatches == 0
    assert receipt.matched
    assert not receipt.provenance_verified
    assert not receipt.acceptable


def test_changed_oracle_output_cannot_keep_the_independence_rung(tmp_path, artifacts):
    bars, packets = artifacts
    bundle = tmp_path / "oracle"
    shutil.copytree(ROOT, bundle)
    output_path = bundle / "implementation" / "oracle-output.json"
    output = json.loads(output_path.read_text(encoding="utf-8"))
    output["signals"][0]["close"] += 1
    output_path.write_text(json.dumps(output), encoding="utf-8")

    receipt = verify_frozen_oracle(bars, packets, bundle)
    assert not receipt.acceptable
    assert receipt.input_hash_mismatches > 0
    assert receipt.signal_mismatches > 0


def test_authorship_claim_and_forbidden_primary_import_are_checked(tmp_path, artifacts):
    bars, packets = artifacts
    bundle = tmp_path / "oracle"
    shutil.copytree(ROOT, bundle)
    authorship_path = bundle / "implementation" / "authorship.json"
    authorship = json.loads(authorship_path.read_text(encoding="utf-8"))
    authorship["canonical_task_name"] = "/root/primary"
    authorship_path.write_text(json.dumps(authorship), encoding="utf-8")
    replay = bundle / "implementation" / "replay.py"
    replay.write_text(replay.read_text(encoding="utf-8") + "\nimport app.proof_kernel\n", encoding="utf-8")

    receipt = verify_frozen_oracle(bars, packets, bundle)
    assert receipt.authorship_mismatches == 1
    assert receipt.forbidden_imports > 0
    assert not receipt.acceptable


def test_current_bars_must_be_the_exact_frozen_oracle_input(artifacts):
    bars, packets = artifacts
    changed = [replace(bar, close=bar.close + 1) if bar.index == 100 else bar for bar in bars]
    receipt = verify_frozen_oracle(changed, packets)
    assert receipt.input_hash_mismatches > 0
    assert not receipt.acceptable


def test_artifact_reads_are_bounded(tmp_path, artifacts):
    bars, packets = artifacts
    bundle = tmp_path / "oracle"
    shutil.copytree(ROOT, bundle)
    (bundle / "implementation" / "oracle-output.json").write_bytes(
        b"x" * (MAX_ORACLE_FILE_BYTES + 1)
    )
    with pytest.raises(ValueError, match="exceeds"):
        verify_frozen_oracle(bars, packets, bundle)
