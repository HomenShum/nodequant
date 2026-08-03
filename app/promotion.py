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


# The counter names the gate actually reads. These are the real field names on
# the kernel gate dict and the CurveReductionReceipt -- an adversarial pass found
# 6 of 13 names in the old POLICY document referred to fields that do not exist,
# so the hash covered a policy nobody executed.
KERNEL_COUNTERS = (
    "factMismatches",
    "postAsOfInputs",
    "futureDeleteChanges",
    "futurePoisonChanges",
    "signalMismatches",
    "unsupportedFacts",
)

LEDGER_COUNTERS = (
    "curve_point_mismatches",
    "unauthorized_fills",
    "early_entries",
    "capital_event_violations",
    "uncosted_fills",
    "negative_cash_points",
    "negative_equity_points",
    "flat_pnl_residual_micros",
    "sequence_gaps",
    "duplicate_event_ids",
    "inconsistent_events",
    "negative_position_points",
    "non_monotonic_timestamps",
    "price_mismatches",
    "unpriced_events",
)

# Evidence classes that may promote. An ALLOWLIST: the previous version blocked
# only SYNTHETIC_KERNEL, so any future class defaulted to acceptable.
PROMOTABLE_EVIDENCE = {EvidenceClass.REAL_BAR_REPLAY, EvidenceClass.POINT_IN_TIME_MARKET_REPLAY}

POLICY = {
    "target": TARGET,
    "real_market_data_required": True,
    "market_data_conformance": "PASS",
    "verifier_integrity": "PASS",
    "promotable_evidence_classes": sorted(x.value for x in PROMOTABLE_EVIDENCE),
    "kernel_zero_tolerance_counters": list(KERNEL_COUNTERS),
    "ledger_zero_tolerance_counters": list(LEDGER_COUNTERS),
    "minimum_oracle_independence": sorted(x.value for x in ACCEPTABLE_INDEPENDENCE),
    "cost_model_required": ["spread", "slippage"],
    "split_discipline": {
        "candidate_frozen_before_test": True,
        "optimizer_had_test_access": False,
    },
}


def policy_hash() -> str:
    """Hash the thresholds AND the function that applies them.

    POLICY alone was a description nobody executed: `evaluate()` read zero keys
    from it, so setting `POLICY = {}` left all sixteen verdicts byte-identical,
    and an audit changed the gate's verdict ten different ways without moving
    this hash. A seal over a document that does not drive the code seals
    nothing. Including the source means editing a threshold moves the hash,
    which is the whole point of freezing it before seeing the test split.
    """

    import inspect

    encoded = json.dumps(POLICY, sort_keys=True, separators=(",", ":"))
    body = inspect.getsource(evaluate)
    digest = hashlib.sha256((encoded + body).encode("utf-8")).hexdigest()
    return "sha256:" + digest


def evaluate(
    kernel_gate: dict,
    receipt: CurveReductionReceipt,
    evidence_class: EvidenceClass,
    oracle_independence: OracleIndependence,
    market_data_conformance: CheckStatus,
    candidate_frozen_before_test: bool = True,
    optimizer_had_test_access: bool = False,
    models_spread: bool = False,
    models_slippage: bool = False,
) -> PromotionGate:
    """Collect every reason this cannot be promoted. All of them, not the first.

    Returning after the first blocker would let someone fix one thing, re-run,
    and discover the next -- which reads as progress when nothing moved.
    """

    blockers: list[Blocker] = []

    if evidence_class not in PROMOTABLE_EVIDENCE:
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
    # Absence is a blocker, not a zero. `kernel_gate.get(field, 0)` let a bare
    # {"status": "PASS"} clear the gate by omitting every counter -- an
    # adversarial pass promoted on exactly that.
    for field in KERNEL_COUNTERS:
        if field not in kernel_gate:
            blockers.append(
                Blocker(
                    code="KERNEL_COUNTER_MISSING",
                    detail=f"{field} absent from the kernel gate; absence is not zero.",
                )
            )
        elif kernel_gate[field] != 0:
            blockers.append(
                Blocker(code=f"KERNEL_{field}", detail=f"{field}={kernel_gate[field]}")
            )
    expected = kernel_gate.get("packetsExpected")
    passed = kernel_gate.get("packetsPassed")
    if not isinstance(passed, int) or passed <= 0:
        blockers.append(
            Blocker(
                code="NO_PACKETS_VERIFIED",
                detail="zero packets passed; a strategy that never decided cannot promote.",
            )
        )
    elif expected != passed:
        # packetsExpected was transmitted, displayed, and never read: 1-of-25
        # with status PASS returned PROMOTABLE with no blockers at all.
        blockers.append(
            Blocker(
                code="PACKETS_INCOMPLETE",
                detail=f"{passed} of {expected} packets verified; every packet must pass.",
            )
        )
    if receipt.event_count == 0:
        blockers.append(
            Blocker(
                code="EMPTY_LEDGER",
                detail="the ledger is empty; a strategy that never traded cannot promote.",
            )
        )
    if not receipt.bars_checked:
        blockers.append(
            Blocker(
                code="PRICE_PROVENANCE_NOT_RUN",
                detail=(
                    "no bars were supplied, so no event price was resolved back to "
                    "the bar it claims to come from. A fabricated price satisfies "
                    "every other ledger check."
                ),
            )
        )
    if not receipt.acceptable:
        for name in LEDGER_COUNTERS:
            value = getattr(receipt, name)
            if value:
                blockers.append(Blocker(code=f"LEDGER_{name}", detail=f"{name}={value}"))
        if not receipt.full_curve_matched:
            blockers.append(
                Blocker(code="CURVE_NOT_MATCHED", detail="reducers disagree on the curve")
            )
    # Costs that are absent bias the curve in one direction only: upward. A
    # strategy can look promotable purely because it was never charged the
    # spread it would actually cross, so their absence is a blocker rather than
    # a caveat.
    if not models_spread:
        blockers.append(
            Blocker(
                code="NO_SPREAD_MODEL",
                detail=(
                    "fills execute at the bar close, so the strategy never crosses "
                    "a spread. Every trade is priced better than it could have been."
                ),
            )
        )
    if not models_slippage:
        blockers.append(
            Blocker(
                code="NO_SLIPPAGE_MODEL",
                detail=(
                    "no market impact or queue position is modelled; a signal that "
                    "fires because price moved assumes it can trade at the price "
                    "that moved."
                ),
            )
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
    pkts = list(packets(25, bars))
    receipt = curve_receipt(build_ledger(bars, pkts), bars, pkts)
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
        "NO_SPREAD_MODEL",
        "NO_SLIPPAGE_MODEL",
    }, codes

    # With real bars, a conformance run and a stronger oracle, it clears -- so
    # the gate is not permanently red by construction.
    cleared = evaluate(
        kernel_gate=run_gate(25, bars),
        receipt=receipt,
        evidence_class=EvidenceClass.REAL_BAR_REPLAY,
        oracle_independence=OracleIndependence.SEPARATE_AGENT_FROM_FROZEN_SPEC,
        market_data_conformance=CheckStatus.PASS,
        models_spread=True,
        models_slippage=True,
    )
    assert cleared.promotable, [b.code for b in cleared.blockers]

    # And a peeked test split blocks it again.
    peeked = evaluate(
        kernel_gate=run_gate(25, bars),
        receipt=receipt,
        evidence_class=EvidenceClass.REAL_BAR_REPLAY,
        oracle_independence=OracleIndependence.SEPARATE_AGENT_FROM_FROZEN_SPEC,
        market_data_conformance=CheckStatus.PASS,
        models_spread=True,
        models_slippage=True,
        optimizer_had_test_access=True,
    )
    assert not peeked.promotable

    print(f"promotion: {gate.verdict.value}, {len(gate.blockers)} blocker(s)")
    for b in gate.blockers:
        print(f"  {b.code}")


if __name__ == "__main__":
    demo()
