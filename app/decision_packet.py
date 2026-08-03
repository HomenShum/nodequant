"""The decision packet: the first and only panel of Proof Kernel v0.

It proves exactly one claim:

    This signal was produced from these exact facts, every fact was knowable at
    the declared timestamp, and a differential implementation reproduced it.

The packet is the unit of authority. Everything downstream -- trade intent, risk
gate, execution -- references a packet by id. Nothing downstream may restate a
number the packet did not measure.

Two rules govern this module and neither is negotiable:

1. No value in here is ever produced by a language model. `facts` are measured by
   deterministic probes and each carries a receipt naming the probe that produced
   it. The model may decide *which* signal is worth investigating; it may not
   decide what SPY traded at.

2. A check with no evidence is NOT_RUN, never PASS. Four of the eight release
   conditions cannot be judged from the packet alone -- they need a differential
   implementation and two mutation runs. When that evidence is absent the packet
   reports NOT_RUN and the overall status is FAIL. A gate that passes when it did
   not run is worse than no gate, because it launders absence into approval.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = 1


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"


class CheckStatus(str, Enum):
    """Three states, deliberately. `NOT_RUN` is not a kind of pass."""

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"


class FactReceipt(BaseModel):
    """Where a number came from. Without one, a fact is unsupported."""

    probe_id: Annotated[str, Field(min_length=1, max_length=200)]
    source: Annotated[str, Field(min_length=1, max_length=200)]
    retrieved_at_utc: datetime


class Fact(BaseModel):
    """One measured input to a decision.

    `known_at_utc` is the moment this value became knowable to the market, which
    is not the moment we retrieved it. Lookahead bias hides in that gap: a bar
    stamped 10:30 is not knowable until 10:31, and a filing dated today may have
    been published tonight. The admissibility check compares `known_at_utc`, not
    `retrieved_at_utc`, against the decision time.
    """

    fact_id: Annotated[str, Field(min_length=1, max_length=120)]
    label: Annotated[str, Field(min_length=1, max_length=200)]
    value: float | str
    unit: str | None = None
    known_at_utc: datetime
    receipt: FactReceipt | None = None


Comparison = Literal["<", "<=", ">", ">=", "==", "!="]

_OPS: dict[str, Any] = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


class Rule(BaseModel):
    """One clause of the strategy, expressed so it can be RECOMPUTED.

    `result` used to be a free boolean the builder asserted. It was the one
    displayed value in the packet with no receipt and no check -- a packet could
    render "uptrend: true" while its own facts said the fast average was below
    the slow one, and nothing caught it.

    So a rule now carries its operands and its operator. `result` is still on the
    wire, because the renderer needs it, but it is no longer testimony: the
    verifier re-evaluates the comparison against the packet's own facts and fails
    the packet when the two disagree.

    `rhs` is a fact id when it names one, otherwise a literal.
    """

    rule_id: Annotated[str, Field(min_length=1, max_length=120)]
    label: Annotated[str, Field(min_length=1, max_length=200)]
    lhs: Annotated[str, Field(min_length=1, max_length=120)]
    op: Comparison
    rhs: str | float
    result: bool

    @property
    def fact_refs(self) -> list[str]:
        """Operands that name facts. Derived, so it cannot drift from the rule."""

        refs = [self.lhs]
        if isinstance(self.rhs, str):
            refs.append(self.rhs)
        return refs


class Strategy(BaseModel):
    id: Annotated[str, Field(min_length=1, max_length=120)]
    version: Annotated[str, Field(min_length=1, max_length=60)]
    config_hash: Annotated[str, Field(min_length=1, max_length=80)]


class Signal(BaseModel):
    signal_id: Annotated[str, Field(min_length=1, max_length=120)]
    symbol: Annotated[str, Field(min_length=1, max_length=20)]
    direction: Direction
    decision_at_utc: datetime
    earliest_execution_at_utc: datetime

    @model_validator(mode="after")
    def _execution_not_before_decision(self) -> Signal:
        if self.earliest_execution_at_utc < self.decision_at_utc:
            raise ValueError(
                "earliest_execution_at_utc precedes decision_at_utc: a fill cannot "
                "happen before the decision that caused it"
            )
        return self


class OracleIndependence(str, Enum):
    """How much a matching replay is actually worth.

    Ordered weakest to strongest. The distinction is the whole point: a second
    implementation by the same author catches transcription and arithmetic
    drift, and cannot catch a misconception both copies share. Recording which
    rung the evidence sits on stops a green check from implying the top one.
    """

    SAME_AUTHOR_DIVERSE_IMPLEMENTATION = "same_author_diverse_implementation"
    SEPARATE_AGENT_FROM_FROZEN_SPEC = "separate_agent_from_frozen_spec"
    INDEPENDENT_AUTHOR = "independent_author"
    EXTERNAL_REFERENCE = "external_reference"


class EvidenceClass(str, Enum):
    """What corpus the verdict was earned against.

    A kernel that passes on a closed-form fixture has proved the verifier. It
    has proved nothing about market data, which arrives with gaps, halts,
    splits, late prints and revisions. Naming the corpus on the wire keeps those
    two claims from being read as one.
    """

    SYNTHETIC_KERNEL = "SYNTHETIC_KERNEL"
    REAL_BAR_REPLAY = "REAL_BAR_REPLAY"
    POINT_IN_TIME_MARKET_REPLAY = "POINT_IN_TIME_MARKET_REPLAY"


class OracleResult(BaseModel):
    """What a second implementation concluded.

    Deliberately NOT called independent. It is a differential replay: written to
    a different shape, by the same author, against the same understanding of
    what the numbers mean. `independence` records that honestly rather than
    letting a matching result imply more than it earned.
    """

    values: dict[str, float | str]
    fired: bool
    signal_at_utc: datetime
    direction: Direction
    entry_eligible: bool
    independence: OracleIndependence = OracleIndependence.SAME_AUTHOR_DIVERSE_IMPLEMENTATION


class MutationEvidence(BaseModel):
    """Hashes from rebuilding the packet with post-decision data removed, and
    again with it corrupted.

    If either changes the hash, the strategy read the future.
    """

    hash_with_future_deleted: str
    hash_with_future_poisoned: str


class Check(BaseModel):
    check_id: str
    label: str
    status: CheckStatus
    detail: str = ""


class Assurance(BaseModel):
    """What class of claim the verdict actually supports.

    A single green PASS is the overclaim this exists to prevent. Three separate
    statuses, because they can and do differ: the verifier can be sound while
    the corpus is synthetic and the replay is only differential. Today that is
    exactly the state -- verifier PASS, market-data conformance NOT_RUN,
    semantic independence LIMITED -- and a reader is entitled to see it.
    """

    verifier_integrity: CheckStatus
    market_data_conformance: CheckStatus
    semantic_independence: Literal["STRONG", "LIMITED", "NOT_RUN"]
    evidence_class: EvidenceClass

    @property
    def headline(self) -> str:
        """What the badge should say. Never a bare PASS."""

        scope = {
            EvidenceClass.SYNTHETIC_KERNEL: "VERIFIER KERNEL",
            EvidenceClass.REAL_BAR_REPLAY: "REAL BAR REPLAY",
            EvidenceClass.POINT_IN_TIME_MARKET_REPLAY: "POINT-IN-TIME REPLAY",
        }[self.evidence_class]
        return f"{scope} {self.verifier_integrity.value}"


class Verification(BaseModel):
    status: CheckStatus
    checks: list[Check]
    assurance: Assurance

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status is not CheckStatus.PASS]


class DecisionPacket(BaseModel):
    """The panel payload. `packet_hash` is derived, never supplied."""

    schema_version: int = SCHEMA_VERSION
    packet_id: Annotated[str, Field(min_length=1, max_length=120)]
    strategy: Strategy
    signal: Signal
    rules: list[Rule]
    facts: list[Fact]

    @model_validator(mode="after")
    def _fact_ids_unique(self) -> DecisionPacket:
        seen = [f.fact_id for f in self.facts]
        if len(seen) != len(set(seen)):
            raise ValueError("duplicate fact_id: a fact must have one value, not two")
        return self


# --------------------------------------------------------------------------
# Hashing
# --------------------------------------------------------------------------


def canonical_hash(packet: DecisionPacket) -> str:
    """Content hash over sorted dict KEYS. List order is significant.

    Keys are sorted because field ordering is not part of the packet's meaning.
    List order deliberately is: reordering `facts` changes the hash, because the
    sequence a strategy read its inputs in is part of what happened. Do not
    describe this as "order-independent" -- an adversarial pass caught that
    phrasing, and a hash whose contract is misdescribed is one somebody will
    eventually rely on wrongly.

    Datetimes serialise to ISO-8601 so two runs on different machines agree.

    `packet_hash` is deliberately not a field on `DecisionPacket` -- a hash that
    covered itself could not be computed. It is attached at the panel boundary.
    """

    payload = packet.model_dump(mode="json")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# The seven release conditions
# --------------------------------------------------------------------------


def _check_receipts(packet: DecisionPacket) -> Check:
    missing = [f.fact_id for f in packet.facts if f.receipt is None]
    return Check(
        check_id="fact_receipts",
        label="Every displayed value has a receipt",
        status=CheckStatus.FAIL if missing else CheckStatus.PASS,
        detail=f"{len(missing)} fact(s) without a receipt: {missing}" if missing else "",
    )


def _check_no_dangling_refs(packet: DecisionPacket) -> Check:
    known = {f.fact_id for f in packet.facts}
    dangling = sorted(
        {ref for rule in packet.rules for ref in rule.fact_refs if ref not in known}
    )
    return Check(
        check_id="unsupported_facts",
        label="No rule cites a fact the packet does not carry",
        status=CheckStatus.FAIL if dangling else CheckStatus.PASS,
        detail=f"unsupported fact refs: {dangling}" if dangling else "",
    )


def _check_rule_evaluation(packet: DecisionPacket) -> Check:
    """Re-evaluate every rule against the packet's own facts.

    This is the check that stops `result` from being testimony. Without it a
    packet can display "uptrend: true" beside facts that say otherwise, which is
    exactly the state the adversarial pass found at bar 200.
    """

    values = {f.fact_id: f.value for f in packet.facts}
    wrong: list[str] = []
    for rule in packet.rules:
        if rule.lhs not in values:
            wrong.append(f"{rule.rule_id} (lhs {rule.lhs} absent)")
            continue
        rhs = values.get(rule.rhs) if isinstance(rule.rhs, str) else rule.rhs
        if rhs is None:
            wrong.append(f"{rule.rule_id} (rhs {rule.rhs} absent)")
            continue
        try:
            recomputed = bool(_OPS[rule.op](values[rule.lhs], rhs))
        except TypeError:
            wrong.append(f"{rule.rule_id} (operands not comparable)")
            continue
        if recomputed != rule.result:
            wrong.append(f"{rule.rule_id} (claims {rule.result}, recomputes {recomputed})")
    return Check(
        check_id="rule_evaluation",
        label="Every rule result recomputes from the packet's own facts",
        status=CheckStatus.FAIL if wrong else CheckStatus.PASS,
        detail="; ".join(wrong),
    )


def _check_admissibility(packet: DecisionPacket) -> Check:
    """No input may postdate the decision it informed."""

    decision_at = packet.signal.decision_at_utc
    late = sorted(f.fact_id for f in packet.facts if f.known_at_utc >= decision_at)
    return Check(
        check_id="admissibility",
        label="Every input was knowable before the decision",
        status=CheckStatus.FAIL if late else CheckStatus.PASS,
        detail=(
            f"fact(s) not knowable at {decision_at.isoformat()}: {late}" if late else ""
        ),
    )


def _check_recomputation(packet: DecisionPacket, oracle: OracleResult | None) -> Check:
    if oracle is None:
        return Check(
            check_id="differential_replay",
            label="A differential implementation reproduced every value",
            status=CheckStatus.NOT_RUN,
            detail="no oracle supplied",
        )
    mismatches: list[str] = []
    for fact in packet.facts:
        if fact.fact_id not in oracle.values:
            mismatches.append(f"{fact.fact_id} (absent from oracle)")
        elif not _values_agree(fact.value, oracle.values[fact.fact_id]):
            mismatches.append(
                f"{fact.fact_id} ({fact.value!r} vs {oracle.values[fact.fact_id]!r})"
            )
    return Check(
        check_id="differential_replay",
        label="A differential implementation reproduced every value",
        status=CheckStatus.FAIL if mismatches else CheckStatus.PASS,
        detail=f"{len(mismatches)} mismatch(es): {mismatches}" if mismatches else "",
    )


def _values_agree(primary: float | str, independent: float | str) -> bool:
    """Canonical rounding: numbers agree to 9 decimal places, strings exactly.

    Not `==` on floats. Two correct implementations of the same arithmetic differ
    in the last bits often enough that exact equality would fail honest runs and
    train everyone to ignore the check.
    """

    # bool is an int subclass, so without this guard True would agree with 1.0.
    # A boolean and a price are never the same measurement.
    if isinstance(primary, bool) or isinstance(independent, bool):
        return primary is independent
    if isinstance(primary, (int, float)) and isinstance(independent, (int, float)):
        return round(float(primary), 9) == round(float(independent), 9)
    return primary == independent


def _check_agreement(packet: DecisionPacket, oracle: OracleResult | None) -> Check:
    if oracle is None:
        return Check(
            check_id="signal_agreement",
            label="Both implementations agree the signal fired, when, and which way",
            status=CheckStatus.NOT_RUN,
            detail="no oracle supplied",
        )
    disagreements: list[str] = []
    if not oracle.fired:
        disagreements.append("oracle says the signal did not fire")
    if oracle.signal_at_utc != packet.signal.decision_at_utc:
        disagreements.append(
            f"timestamp {oracle.signal_at_utc.isoformat()} "
            f"vs {packet.signal.decision_at_utc.isoformat()}"
        )
    if oracle.direction is not packet.signal.direction:
        disagreements.append(
            f"direction {oracle.direction.value} vs {packet.signal.direction.value}"
        )
    if not oracle.entry_eligible:
        disagreements.append("oracle says entry was not eligible")
    return Check(
        check_id="signal_agreement",
        label="Both implementations agree the signal fired, when, and which way",
        status=CheckStatus.FAIL if disagreements else CheckStatus.PASS,
        detail="; ".join(disagreements),
    )


def _check_future_isolation(packet_hash: str, mutation: MutationEvidence | None) -> list[Check]:
    if mutation is None:
        return [
            Check(
                check_id="future_delete",
                label="Deleting post-decision data leaves the packet unchanged",
                status=CheckStatus.NOT_RUN,
                detail="no mutation evidence supplied",
            ),
            Check(
                check_id="future_poison",
                label="Corrupting post-decision data leaves the packet unchanged",
                status=CheckStatus.NOT_RUN,
                detail="no mutation evidence supplied",
            ),
        ]
    deleted_ok = mutation.hash_with_future_deleted == packet_hash
    poisoned_ok = mutation.hash_with_future_poisoned == packet_hash
    return [
        Check(
            check_id="future_delete",
            label="Deleting post-decision data leaves the packet unchanged",
            status=CheckStatus.PASS if deleted_ok else CheckStatus.FAIL,
            detail="" if deleted_ok else "packet hash moved when future data was removed",
        ),
        Check(
            check_id="future_poison",
            label="Corrupting post-decision data leaves the packet unchanged",
            status=CheckStatus.PASS if poisoned_ok else CheckStatus.FAIL,
            detail="" if poisoned_ok else "packet hash moved when future data was corrupted",
        ),
    ]


def verify(
    packet: DecisionPacket,
    oracle: OracleResult | None = None,
    mutation: MutationEvidence | None = None,
    evidence_class: EvidenceClass = EvidenceClass.SYNTHETIC_KERNEL,
) -> Verification:
    """Run all eight conditions. PASS only when every one of them passed.

    Note what this deliberately does not do: it does not average, score, or
    weight. One FAIL or one NOT_RUN is a FAIL overall, because the panel's whole
    claim is that nothing was assumed.
    """

    packet_hash = canonical_hash(packet)
    checks = [
        _check_receipts(packet),
        _check_no_dangling_refs(packet),
        _check_rule_evaluation(packet),
        _check_admissibility(packet),
        _check_recomputation(packet, oracle),
        _check_agreement(packet, oracle),
        *_check_future_isolation(packet_hash, mutation),
    ]
    overall = (
        CheckStatus.PASS
        if all(c.status is CheckStatus.PASS for c in checks)
        else CheckStatus.FAIL
    )
    strong = {
        OracleIndependence.INDEPENDENT_AUTHOR,
        OracleIndependence.EXTERNAL_REFERENCE,
    }
    return Verification(
        status=overall,
        checks=checks,
        assurance=Assurance(
            verifier_integrity=overall,
            # There is no market-data conformance run yet: no gap, halt, split,
            # late-print or revision handling exists to check. NOT_RUN is the
            # honest value and must not be quietly promoted when real bars land.
            market_data_conformance=CheckStatus.NOT_RUN,
            semantic_independence=(
                "NOT_RUN"
                if oracle is None
                else ("STRONG" if oracle.independence in strong else "LIMITED")
            ),
            evidence_class=evidence_class,
        ),
    )


def to_panel(
    packet: DecisionPacket,
    oracle: OracleResult | None = None,
    mutation: MutationEvidence | None = None,
) -> dict[str, Any]:
    """Render to the SSE panel envelope consumed as `data-decision-packet`.

    The hash and the verdict are computed here rather than accepted from a
    caller, so a packet cannot arrive at the frontend claiming a PASS it was
    handed.

    The oracle values and the two mutation hashes go on the wire as well. They
    are what the verdict was computed FROM, and a surface whose claim is "an
    independent replay says PASS" is worthless if the reviewer has to take that
    on trust -- shipping the verdict without its evidence just relocates the
    assertion. With them present a client can recompute the comparison itself.
    """

    result = verify(packet, oracle, mutation)
    return {
        "name": "decision-packet",
        "data": {
            **packet.model_dump(mode="json"),
            "packet_hash": canonical_hash(packet),
            "verification": {
                **result.model_dump(mode="json"),
                # Precomputed so the renderer cannot invent a friendlier headline
                # than the evidence supports.
                "assurance_headline": result.assurance.headline,
            },
            "evidence": {
                "oracle": oracle.model_dump(mode="json") if oracle else None,
                "mutation": mutation.model_dump(mode="json") if mutation else None,
            },
        },
    }


# --------------------------------------------------------------------------
# Self-check
# --------------------------------------------------------------------------


def demo() -> None:
    """Smallest thing that fails if the authority rules break."""

    from datetime import timedelta, timezone

    t0 = datetime(2026, 8, 3, 14, 33, tzinfo=timezone.utc)
    receipt = FactReceipt(probe_id="bars.spy.1m", source="polygon", retrieved_at_utc=t0)
    packet = DecisionPacket(
        packet_id="pkt-1",
        strategy=Strategy(id="spy-v0", version="0.1.0", config_hash="cfg-abc"),
        signal=Signal(
            signal_id="sig-427",
            symbol="SPY",
            direction=Direction.LONG,
            decision_at_utc=t0,
            earliest_execution_at_utc=t0 + timedelta(seconds=60),
        ),
        rules=[
            Rule(rule_id="r1", label="last below cap", lhs="f1", op="<", rhs=700.0,
                 result=True)
        ],
        facts=[
            Fact(
                fact_id="f1",
                label="SPY last",
                value=631.42,
                known_at_utc=t0 - timedelta(minutes=1),
                receipt=receipt,
            )
        ],
    )

    # Hash is stable and order-independent.
    assert canonical_hash(packet) == canonical_hash(packet.model_copy(deep=True))

    # With no oracle and no mutation runs, four checks are NOT_RUN -- and the
    # verdict is FAIL, not PASS. This is the assertion that matters most.
    v = verify(packet)
    assert v.status is CheckStatus.FAIL, "unrun checks must never yield PASS"
    not_run = {c.check_id for c in v.checks if c.status is CheckStatus.NOT_RUN}
    assert not_run == {"differential_replay", "signal_agreement", "future_delete", "future_poison"}

    # Full evidence -> PASS.
    oracle = OracleResult(
        values={"f1": 631.42},
        fired=True,
        signal_at_utc=t0,
        direction=Direction.LONG,
        entry_eligible=True,
    )
    h = canonical_hash(packet)
    mutation = MutationEvidence(hash_with_future_deleted=h, hash_with_future_poisoned=h)
    assert verify(packet, oracle, mutation).status is CheckStatus.PASS

    # A fact knowable only after the decision is inadmissible.
    late = packet.model_copy(deep=True)
    late.facts[0].known_at_utc = t0 + timedelta(seconds=1)
    assert _check_admissibility(late).status is CheckStatus.FAIL

    # A rule citing a fact that is not present is unsupported.
    dangling = packet.model_copy(deep=True)
    dangling.rules[0].lhs = "f-missing"
    assert _check_no_dangling_refs(dangling).status is CheckStatus.FAIL

    # A rule whose result its own facts contradict must fail. 631.42 < 700 is
    # true, so claiming false is a lie the packet carries about itself.
    lying = packet.model_copy(deep=True)
    lying.rules[0].result = False
    assert _check_rule_evaluation(lying).status is CheckStatus.FAIL
    assert verify(lying, oracle, mutation).status is CheckStatus.FAIL

    # A fact with no receipt cannot be displayed.
    unreceipted = packet.model_copy(deep=True)
    unreceipted.facts[0].receipt = None
    assert _check_receipts(unreceipted).status is CheckStatus.FAIL

    # Reading the future moves the hash, and that must fail.
    moved = MutationEvidence(
        hash_with_future_deleted="sha256:different", hash_with_future_poisoned=h
    )
    assert verify(packet, oracle, moved).status is CheckStatus.FAIL

    # The panel computes its own verdict rather than trusting a caller.
    panel = to_panel(packet, oracle, mutation)
    assert panel["name"] == "decision-packet"
    assert panel["data"]["packet_hash"] == h
    assert panel["data"]["verification"]["status"] == "PASS"

    print("decision_packet: all checks passed")


if __name__ == "__main__":
    demo()
