"""A researcher can audit every record in the current variant batch."""

from __future__ import annotations

from app import ledger as lg
from app import proof_kernel as pk
from app.decision_packet import canonical_hash
from app.experiment import load_manifest
from app.order_journal import build_order_journal
from app.robustness import evaluate_robustness
from app.trial_ledger import _definition, _hash, build_trial_ledger
import pytest
from pydantic import ValidationError


def test_current_batch_preserves_definitions_results_and_holdout_access():
    bars = pk.fixture_bars()
    packets = list(pk.packets(25, bars))
    robustness = evaluate_robustness(
        bars,
        packets,
        lg.build_ledger(bars, packets),
        build_order_journal(bars, packets),
        load_manifest()[0],
        expected_bars_hash=pk.bars_hash(bars),
        expected_packet_hashes=[canonical_hash(packet) for packet in packets],
    )
    receipt = build_trial_ledger(robustness)

    assert receipt.trial_count == 9
    assert receipt.batch_complete
    assert not receipt.global_completeness_verified
    assert not receipt.pre_test_registration_verified
    assert receipt.opened_holdout_observed_trial_count == 2
    assert not receipt.acceptable_for_multiple_testing
    assert receipt.hash_matches
    assert len({record.definition_hash for record in receipt.records}) == 9
    assert len({record.result_hash for record in receipt.records}) == 9
    assert [record.trial_id for record in receipt.records] == [
        row.variant_id for row in robustness.rows
    ]
    assert all(record.diagnostic_only for record in receipt.records)

    forged = robustness.model_copy(update={"suite_hash": "sha256:" + "0" * 64})
    with pytest.raises(ValueError, match="frozen robustness suite"):
        build_trial_ledger(forged)
    with pytest.raises(ValidationError):
        receipt.records[0].trial_id = "rewritten"
    forged_complete = receipt.model_copy(update={"global_completeness_verified": True})
    assert not forged_complete.hash_matches
    assert not forged_complete.acceptable_for_multiple_testing

    changed_rows = list(robustness.rows)
    changed_rows[0] = changed_rows[0].model_copy(update={"test_net_pnl_micros": 999_999_999})
    forged_result = robustness.model_copy(
        update={
            "rows": tuple(changed_rows),
            "result_root_hash": _hash([row.model_dump(mode="json") for row in changed_rows]),
        }
    )
    with pytest.raises(ValueError, match="policy-bound result universe"):
        build_trial_ledger(forged_result)

    changed_rows = list(robustness.rows)
    changed_rows[0] = changed_rows[0].model_copy(update={"fast_window": 19})
    forged_definition = robustness.model_copy(
        update={
            "rows": tuple(changed_rows),
            "suite_hash": _hash([_definition(row) for row in changed_rows]),
        }
    )
    with pytest.raises(ValueError, match="policy-bound variant universe"):
        build_trial_ledger(forged_definition)
