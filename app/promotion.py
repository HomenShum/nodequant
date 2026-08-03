"""The promotion gate: may this strategy version run on paper?

The gate exists to be FAILED. Today NodeQuant cannot promote anything, for two
reasons that no amount of green checks elsewhere can substitute for:

  * every packet was verified against a synthetic fixture, so market-data
    conformance has never been run;
  * the replay oracle shares an author with the implementation it checks.

A surface that showed a rising equity curve and a row of green ticks without
saying those two things would be inviting exactly the deference this whole
design is against. So the review panel leads with the gate, the gate leads with
its blockers, and `PROMOTABLE` is unreachable until the blockers are gone.

The policy is hashed and frozen. A gate whose thresholds can be edited after
seeing the test split is not a gate, it is a negotiation.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum

from pydantic import BaseModel

from app.decision_packet import CheckStatus, EvidenceClass, OracleIndependence
from app.ledger import CurveReductionReceipt

TARGET = "PAPER_SHADOW"

# Anything at or above this rung is acceptable. Same-author diversity is not.
ACCEPTABLE_INDEPENDENCE = {
    OracleIndependence.SEPARATE_AGENT_FROM_FROZEN_SPEC,
    OracleIndependence.INDEPENDENT_AUTHOR,
    OracleIndependence.EXTERNAL_REFERENCE,
}


class Verdict(str, Enum):
    PROMOTABLE = "PROMOTABLE"
    BLOCKED = "BLOCKED"


class Blocker(BaseModel):
    code: str
    detail: str


class PromotionGate(BaseModel):
    target: str
    policy_hash: str
    verdict: Verdict
    blockers: list[Blocker]

    @property
    def promotable(self) -> bool:
        return self.verdict is Verdict.PROMOTABLE and not self.blockers


POLICY = {
    "target": TARGET,
    "real_market_data_required": True,
    "market_data_conformance": "PASS",
    "verifier_integrity": "PASS",
    "zero_tolerance_counters": [
        "decision_packet_failures",
        "unsupported_facts",
        "post_as_of_inputs",
        "future_delete_changes",
        "future_poison_changes",
        "signal_mismatches",
        "missing_event_receipts",
        "ledger_sequence_gaps",
        "duplicate_event_ids",
        "curve_point_mismatches",
        "inconsistent_events",
        "negative_position_points",
        "maximum_accounting_residual_micros",
    ],
    "minimum_oracle_independence": sorted(x.value for x in ACCEPTABLE_INDEPENDENCE),
    "split_discipline": {
        "candidate_frozen_before_test": True,
        "optimizer_had_test_access": False,
    },
}


def policy_hash() -> str:
    encoded = json.dumps(POLICY, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def evaluate(
    kernel_gate: dict,
    receipt: CurveReductionReceipt,
    evidence_class: EvidenceClass,
    oracle_independence: OracleIndependence,
    market_data_conformance: CheckStatus,
    candidate_frozen_before_test: bool = True,
    optimizer_had_test_access: bool = False,
) -> PromotionGate:
    """Collect every reason this cannot be promoted. All of them, not the first.

    Returning after the first blocker would let someone fix one thing, re-run,
    and discover the next -- which reads as progress when nothing moved.
    """

    blockers: list[Blocker] = []

    if evidence_class is EvidenceClass.SYNTHETIC_KERNEL:
        blockers.append(
            Blocker(
                code="SYNTHETIC_CORPUS",
                detail=(
                    "Every packet was verified against a closed-form fixture. That "
                    "proves the verifier, not the strategy. Real bars introduce gaps, "
                    "halts, splits, late prints and revisions, none of which this "
                    "corpus contains."
                ),
            )
        )
    if market_data_conformance is not CheckStatus.PASS:
        blockers.append(
            Blocker(
                code="MARKET_DATA_CONFORMANCE_NOT_RUN",
                detail=f"market-data conformance is {market_data_conformance.value}.",
            )
        )
    if oracle_independence not in ACCEPTABLE_INDEPENDENCE:
        blockers.append(
            Blocker(
                code="ORACLE_NOT_INDEPENDENT_ENOUGH",
                detail=(
                    f"replay independence is {oracle_independence.value}; a matching "
                    "result cannot rule out a misconception shared by both "
                    "implementations. Minimum is separate_agent_from_frozen_spec."
                ),
            )
        )
    if kernel_gate.get("status") != "PASS":
        blockers.append(
            Blocker(code="KERNEL_GATE_FAILED", detail=f"kernel gate: {kernel_gate}")
        )
    for field in (
        "factMismatches", "postAsOfInputs", "futureDeleteChanges",
        "futurePoisonChanges", "signalMismatches", "unsupportedFacts",
    ):
        if kernel_gate.get(field, 0):
            blockers.append(
                Blocker(code=f"KERNEL_{field}", detail=f"{field}={kernel_gate[field]}")
            )
    if not receipt.acceptable:
        for name in (
            "curve_point_mismatches", "missing_event_receipts", "sequence_gaps",
            "duplicate_event_ids", "inconsistent_events", "negative_position_points",
            "max_accounting_residual_micros",
        ):
            value = getattr(receipt, name)
            if value:
                blockers.append(Blocker(code=f"LEDGER_{name}", detail=f"{name}={value}"))
        if not receipt.full_curve_matched:
            blockers.append(
                Blocker(code="CURVE_NOT_MATCHED", detail="reducers disagree on the curve")
            )
    if not candidate_frozen_before_test:
        blockers.append(
            Blocker(
                code="CANDIDATE_NOT_FROZEN",
                detail="the candidate was changed after the test split was visible.",
            )
        )
    if optimizer_had_test_access:
        blockers.append(
            Blocker(
                code="OPTIMIZER_SAW_TEST",
                detail="the optimizer had access to the test split; the result is in-sample.",
            )
        )

    return PromotionGate(
        target=TARGET,
        policy_hash=policy_hash(),
        verdict=Verdict.BLOCKED if blockers else Verdict.PROMOTABLE,
        blockers=blockers,
    )


def demo() -> None:
    from app.ledger import build_ledger, curve_receipt
    from app.proof_kernel import fixture_bars, packets, run_gate

    bars = fixture_bars()
    receipt = curve_receipt(build_ledger(bars, list(packets(25, bars))))
    gate = evaluate(
        kernel_gate=run_gate(25, bars),
        receipt=receipt,
        evidence_class=EvidenceClass.SYNTHETIC_KERNEL,
        oracle_independence=OracleIndependence.SAME_AUTHOR_DIVERSE_IMPLEMENTATION,
        market_data_conformance=CheckStatus.NOT_RUN,
    )

    # Today's honest answer: everything the kernel checks is green, and the
    # strategy still cannot be promoted.
    assert gate.verdict is Verdict.BLOCKED
    assert not gate.promotable
    codes = {b.code for b in gate.blockers}
    assert codes == {
        "SYNTHETIC_CORPUS",
        "MARKET_DATA_CONFORMANCE_NOT_RUN",
        "ORACLE_NOT_INDEPENDENT_ENOUGH",
    }, codes

    # With real bars, a conformance run and a stronger oracle, it clears -- so
    # the gate is not permanently red by construction.
    cleared = evaluate(
        kernel_gate=run_gate(25, bars),
        receipt=receipt,
        evidence_class=EvidenceClass.REAL_BAR_REPLAY,
        oracle_independence=OracleIndependence.SEPARATE_AGENT_FROM_FROZEN_SPEC,
        market_data_conformance=CheckStatus.PASS,
    )
    assert cleared.promotable, [b.code for b in cleared.blockers]

    # And a peeked test split blocks it again.
    peeked = evaluate(
        kernel_gate=run_gate(25, bars),
        receipt=receipt,
        evidence_class=EvidenceClass.REAL_BAR_REPLAY,
        oracle_independence=OracleIndependence.SEPARATE_AGENT_FROM_FROZEN_SPEC,
        market_data_conformance=CheckStatus.PASS,
        optimizer_had_test_access=True,
    )
    assert not peeked.promotable

    print(f"promotion: {gate.verdict.value}, {len(gate.blockers)} blocker(s)")
    for b in gate.blockers:
        print(f"  {b.code}")


if __name__ == "__main__":
    demo()
