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

from app.decision_packet import DecisionPacket, EvidenceClass
from app.ledger import CurveReductionReceipt, LedgerEvent, curve_receipt
from app.independent_oracle import FROZEN_HASHES, FrozenOracleReceipt, verify_frozen_oracle
from app.order_journal import JournalEvent
from app.proof_kernel import Bar
from app.market_conformance import MarketDataConformanceReceipt, assess_market_data
from app.market_data import BarSnapshot
from app.experiment import SplitDisciplineReceipt, assess_split_discipline, load_manifest

TARGET = "PAPER_SHADOW"

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
    independent_oracle: FrozenOracleReceipt | None = None
    market_data: MarketDataConformanceReceipt
    split_discipline: SplitDisciplineReceipt

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
    "mistimed_exits",
    "missing_authorized_fills",
    "capital_event_violations",
    "uncosted_fills",
    "negative_cash_points",
    "negative_equity_points",
    "sequence_gaps",
    "duplicate_event_ids",
    "inconsistent_events",
    "negative_position_points",
    "non_monotonic_timestamps",
    "price_mismatches",
    "unpriced_events",
    "spread_model_mismatches",
    "slippage_model_mismatches",
    "execution_price_mismatches",
    "execution_policy_mismatches",
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
    "frozen_oracle_hashes": FROZEN_HASHES,
    "cost_model_required": {
        "spread": "frozen_adverse_per_fill",
        "slippage": "frozen_adverse_per_fill",
    },
    "experiment_manifest_hash": load_manifest()[1],
    "split_access_provenance_required": True,
    "packet_set_binding_required": True,
    "cross_stream_reconciliation": "PASS",
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
    events: list[LedgerEvent],
    bars: list[Bar] | None,
    packets: list[DecisionPacket] | None,
    journal: list[JournalEvent] | None,
    snapshot: BarSnapshot | None = None,
) -> PromotionGate:
    """Collect every reason this cannot be promoted. All of them, not the first.

    Returning after the first blocker would let someone fix one thing, re-run,
    and discover the next -- which reads as progress when nothing moved.
    """

    # Promotion never accepts counters as caller testimony. Recompute the
    # receipt from the bound artifacts at the gate boundary, so editing a copied
    # receipt cannot clear a blocker.
    receipt = curve_receipt(events, bars, packets, journal)
    market_data = assess_market_data(bars, snapshot)
    split_receipt, _ = assess_split_discipline(
        [event.at_utc for event in events],
        [(packet.strategy.id, packet.strategy.config_hash) for packet in packets or []],
        str(POLICY["experiment_manifest_hash"]),
    )
    blockers: list[Blocker] = []

    verified_packet_hashes = kernel_gate.get("packetHashes")
    verified_bars_hash = kernel_gate.get("barsHash")
    if not isinstance(verified_bars_hash, str):
        blockers.append(
            Blocker(
                code="BAR_SET_NOT_BOUND",
                detail="the verifier gate did not bind the complete bar corpus.",
            )
        )
    elif receipt.bars_hash != verified_bars_hash:
        blockers.append(
            Blocker(
                code="BAR_SET_MISMATCH",
                detail="promotion artifacts were derived from different bars than the kernel gate.",
            )
        )
    if not isinstance(verified_packet_hashes, list) or not verified_packet_hashes:
        blockers.append(
            Blocker(
                code="PACKET_SET_NOT_BOUND",
                detail="the verifier gate did not identify the exact packets it passed.",
            )
        )
    elif receipt.packet_hashes != verified_packet_hashes:
        blockers.append(
            Blocker(
                code="PACKET_SET_MISMATCH",
                detail="the ledger receipt was not reduced from the exact verified packet set.",
            )
        )
    if receipt.cross_stream_reconciliation != "PASS":
        blockers.append(
            Blocker(
                code=(
                    "CROSS_STREAM_RECONCILIATION_NOT_RUN"
                    if receipt.cross_stream_reconciliation == "NOT_RUN"
                    else "CROSS_STREAM_RECONCILIATION_FAILED"
                ),
                detail=(
                    "the independent order lifecycle did not reconcile quantity, "
                    "timestamps, prices, costs, cash, fees, marks and IDs; status="
                    f"{receipt.cross_stream_reconciliation}."
                ),
            )
        )

    if market_data.evidence_class is EvidenceClass.SYNTHETIC_KERNEL:
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
    elif market_data.evidence_class not in PROMOTABLE_EVIDENCE:
        blockers.append(
            Blocker(
                code="CORPUS_NOT_CONFORMED",
                detail=(
                    f"evidence class is {market_data.evidence_class.value}; byte provenance "
                    "does not establish market-data conformance."
                ),
            )
        )
    if not market_data.acceptable:
        blockers.append(
            Blocker(
                code="MARKET_DATA_CONFORMANCE_NOT_RUN",
                detail=f"market-data conformance is {market_data.status.value}: {market_data.limitation}",
            )
        )
    oracle_receipt: FrozenOracleReceipt | None = None
    oracle_error: str | None = None
    try:
        if bars is not None and packets is not None:
            oracle_receipt = verify_frozen_oracle(bars, packets)
    except (OSError, ValueError) as exc:
        oracle_error = str(exc)
    if oracle_receipt is None or not oracle_receipt.acceptable:
        oracle_detail = (
            f": {oracle_error}"
            if oracle_error
            else (
                "; signals match, but separate-agent authorship and access are "
                "self-attested rather than backed by immutable orchestrator provenance."
                if oracle_receipt is not None and oracle_receipt.matched
                else "."
            )
        )
        blockers.append(
            Blocker(
                code="ORACLE_NOT_INDEPENDENT_ENOUGH",
                detail=(
                    "the frozen oracle has not proven the required separate-agent "
                    "independence rung"
                    + oracle_detail
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
    if not receipt.packets_checked:
        blockers.append(
            Blocker(
                code="AUTHORITY_NOT_RUN",
                detail=(
                    "no packets were supplied, so no fill was resolved back to the "
                    "decision that authorised it. A ghost trade satisfies every "
                    "other ledger check."
                ),
            )
        )
    if not receipt.acceptable:
        # A catch-all FIRST. The per-counter loop below uses truthiness, so a
        # counter of None -- an unrun check -- emitted no blocker at all, and an
        # unacceptable receipt promoted with an EMPTY blocker list. Never let the
        # gate's verdict disagree with the receipt's own.
        blockers.append(
            Blocker(
                code="LEDGER_UNACCEPTABLE",
                detail="the curve reduction receipt is not acceptable; see the counters.",
            )
        )
        for name in LEDGER_COUNTERS:
            value = getattr(receipt, name)
            if value is None:
                blockers.append(
                    Blocker(code=f"LEDGER_{name}_NOT_RUN", detail=f"{name} was never computed.")
                )
            elif value:
                blockers.append(Blocker(code=f"LEDGER_{name}", detail=f"{name}={value}"))
        if not receipt.full_curve_matched:
            blockers.append(
                Blocker(code="CURVE_NOT_MATCHED", detail="reducers disagree on the curve")
            )
    # Costs that are absent bias the curve in one direction only: upward. A
    # strategy can look promotable purely because it was never charged the
    # spread it would actually cross, so their absence is a blocker rather than
    # a caveat.
    if (
        not receipt.execution_costs_checked
        or not receipt.spread_cost_micros
        or receipt.spread_model_mismatches != 0
        or receipt.execution_price_mismatches != 0
        or receipt.execution_policy_mismatches != 0
    ):
        blockers.append(
            Blocker(
                code="NO_SPREAD_MODEL",
                detail=(
                    "no verified, positive spread cost was derived from the frozen "
                    "per-fill policy. Modelled costs are required until quote replay exists."
                ),
            )
        )
    if (
        not receipt.execution_costs_checked
        or not receipt.slippage_cost_micros
        or receipt.slippage_model_mismatches != 0
        or receipt.execution_price_mismatches != 0
        or receipt.execution_policy_mismatches != 0
    ):
        blockers.append(
            Blocker(
                code="NO_SLIPPAGE_MODEL",
                detail=(
                    "no verified, positive slippage cost was derived from the frozen "
                    "per-fill policy. This is an adverse model, not observed impact."
                ),
            )
        )
    if not split_receipt.manifest_matches_policy:
        blockers.append(
            Blocker(
                code="EXPERIMENT_MANIFEST_MISMATCH",
                detail=(
                    "the runtime experiment manifest hash differs from the manifest "
                    "sealed into the displayed promotion policy."
                ),
            )
        )
    if not split_receipt.acceptable:
        blockers.append(
            Blocker(
                code="SPLIT_DISCIPLINE_NOT_PROVEN",
                detail=(
                    "the frozen manifest tags the curve and declares the candidate/test "
                    "boundary, but repository-authored metadata cannot prove when the "
                    "candidate froze or which splits the optimizer could access."
                ),
            )
        )

    return PromotionGate(
        target=TARGET,
        policy_hash=policy_hash(),
        verdict=Verdict.BLOCKED if blockers else Verdict.PROMOTABLE,
        blockers=blockers,
        independent_oracle=oracle_receipt,
        market_data=market_data,
        split_discipline=split_receipt,
    )


def demo() -> None:
    from app.ledger import build_ledger, curve_receipt
    from app.order_journal import build_order_journal
    from app.proof_kernel import fixture_bars, packets, run_gate

    bars = fixture_bars()
    pkts = list(packets(25, bars))
    events = build_ledger(bars, pkts)
    journal = build_order_journal(bars, pkts)
    gate = evaluate(
        kernel_gate=run_gate(25, bars),
        events=events,
        bars=bars,
        packets=pkts,
        journal=journal,
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
        "SPLIT_DISCIPLINE_NOT_PROVEN",
    }, codes

    # Repeating caller-visible inputs cannot clear a derived corpus blocker.
    repeated = evaluate(
        kernel_gate=run_gate(25, bars),
        events=events,
        bars=bars,
        packets=pkts,
        journal=journal,
    )
    assert not repeated.promotable

    print(f"promotion: {gate.verdict.value}, {len(gate.blockers)} blocker(s)")
    for b in gate.blockers:
        print(f"  {b.code}")


if __name__ == "__main__":
    demo()
