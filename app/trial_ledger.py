"""Immutable records for the current frozen robustness batch."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field

from app.robustness import RobustnessReceipt, RobustnessRow
from app.experiment import load_manifest


def _hash(payload) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class TrialRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    trial_id: str
    definition_hash: str
    result_hash: str
    development_net_pnl_micros: int | None
    validation_net_pnl_micros: int | None
    test_net_pnl_micros: int | None
    opened_holdout_net_pnl_micros: int | None
    observed_split_count: int
    diagnostic_only: bool = True


class TrialLedgerReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    records: tuple[TrialRecord, ...] = Field(min_length=9, max_length=32)
    trial_count: int
    batch_complete: bool
    global_completeness_verified: bool = False
    pre_test_registration_verified: bool = False
    opened_holdout_observed_trial_count: int
    robustness_suite_hash: str
    robustness_receipt_hash: str
    trial_ledger_hash: str
    evidence_scope: str = "current_batch_lineage_not_promotion_evidence"

    @property
    def hash_matches(self) -> bool:
        return self.trial_ledger_hash == _hash(
            {
                "records": [record.model_dump(mode="json") for record in self.records],
                "trial_count": self.trial_count,
                "batch_complete": self.batch_complete,
                "global_completeness_verified": self.global_completeness_verified,
                "pre_test_registration_verified": self.pre_test_registration_verified,
                "opened_holdout_observed_trial_count": self.opened_holdout_observed_trial_count,
                "robustness_suite_hash": self.robustness_suite_hash,
                "robustness_receipt_hash": self.robustness_receipt_hash,
                "evidence_scope": self.evidence_scope,
            }
        )

    @property
    def acceptable_for_multiple_testing(self) -> bool:
        return (
            self.hash_matches
            and
            self.batch_complete
            and self.global_completeness_verified
            and self.pre_test_registration_verified
            and self.opened_holdout_observed_trial_count == 0
        )


def _definition(row: RobustnessRow) -> dict:
    return {
        "variant_id": row.variant_id,
        "fast_window": row.fast_window,
        "slow_window": row.slow_window,
        "hold_bars": row.hold_bars,
        "use_uptrend": row.use_uptrend,
        "use_pullback": row.use_pullback,
    }


def _record(row: RobustnessRow) -> TrialRecord:
    definition = _definition(row)
    result = row.model_dump(mode="json")
    values = (
        row.development_net_pnl_micros,
        row.validation_net_pnl_micros,
        row.test_net_pnl_micros,
        row.opened_holdout_net_pnl_micros,
    )
    return TrialRecord(
        trial_id=row.variant_id,
        definition_hash=_hash(definition),
        result_hash=_hash(result),
        development_net_pnl_micros=values[0],
        validation_net_pnl_micros=values[1],
        test_net_pnl_micros=values[2],
        opened_holdout_net_pnl_micros=values[3],
        observed_split_count=sum(value is not None for value in values),
    )


def build_trial_ledger(robustness: RobustnessReceipt) -> TrialLedgerReceipt:
    manifest, manifest_hash = load_manifest()
    from app.promotion import POLICY

    if manifest_hash != POLICY["experiment_manifest_hash"]:
        raise ValueError("trial ledger requires the policy-bound experiment manifest")
    records = tuple(_record(row) for row in robustness.rows)
    ids = [record.trial_id for record in records]
    if len(ids) != len(set(ids)):
        raise ValueError("trial ledger requires unique trial ids")
    definition_suite_hash = _hash([_definition(row) for row in robustness.rows])
    if definition_suite_hash != robustness.suite_hash:
        raise ValueError("trial definitions do not match the frozen robustness suite hash")
    expected_definitions = [variant.model_dump(mode="json") for variant in manifest.robustness_variants]
    if [_definition(row) for row in robustness.rows] != expected_definitions:
        raise ValueError("trial definitions do not match the policy-bound variant universe")
    result_root = _hash([row.model_dump(mode="json") for row in robustness.rows])
    if (
        result_root != robustness.result_root_hash
        or result_root != manifest.robustness_result_root
    ):
        raise ValueError("trial results do not match the policy-bound result universe")
    batch_complete = (
        len(records) == len(manifest.robustness_variants)
        and ids == [variant.variant_id for variant in manifest.robustness_variants]
        and result_root == manifest.robustness_result_root
    )
    robustness_receipt_hash = _hash(robustness.model_dump(mode="json"))
    fields = {
        "records": records,
        "trial_count": len(records),
        "batch_complete": batch_complete,
        "global_completeness_verified": False,
        "pre_test_registration_verified": False,
        "opened_holdout_observed_trial_count": sum(
            record.opened_holdout_net_pnl_micros is not None for record in records
        ),
        "robustness_suite_hash": robustness.suite_hash,
        "robustness_receipt_hash": robustness_receipt_hash,
        "evidence_scope": "current_batch_lineage_not_promotion_evidence",
    }
    hash_payload = {
        **fields,
        "records": [record.model_dump(mode="json") for record in records],
    }
    return TrialLedgerReceipt(
        **fields,
        trial_ledger_hash=_hash(hash_payload),
    )
