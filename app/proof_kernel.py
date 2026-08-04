"""Proof Kernel v0: produce decision packets and prove every one of them.

This is the smallest thing that can honestly claim the design's central
property. It takes a frozen bar series, runs one deterministic rule over it,
emits a `DecisionPacket` per signal, and then attacks each packet four ways:

  1. recompute every fact with a second implementation,
  2. check no input postdates the decision it informed,
  3. rebuild the packet with all post-decision bars deleted,
  4. rebuild it again with all post-decision bars corrupted.

(3) and (4) are the ones that catch lookahead. If the strategy reads a bar it
should not have, removing or poisoning that bar moves the packet hash, and the
gate fails. A strategy that only reads its own past is hash-stable under both.

Two honest limitations, stated rather than buried:

- **The bars are a synthetic fixture, not market data.** They come from a closed
  formula with no randomness, so every run on every machine produces identical
  packets. That is what makes the gate meaningful as a *proof of the mechanism*.
  It proves the verifier works. It does not prove anything about SPY.

- **The oracle is independent in implementation, not in authorship.** It walks
  the series differently and accumulates differently, so it catches transcription
  slips, off-by-one windows, and accumulation-order drift. It cannot catch a
  misconception shared by both implementations. Real independence needs a second
  author; this is the floor, not the ceiling.
"""

from __future__ import annotations

import math
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from app.decision_packet import (
    CheckStatus,
    DecisionPacket,
    Direction,
    EvidenceClass,
    ExecutionCostPolicy,
    Fact,
    FactReceipt,
    MutationEvidence,
    OracleResult,
    Rule,
    Signal,
    Strategy,
    canonical_hash,
    verify,
)

SYMBOL = "SPY"
STRATEGY = Strategy(
    id="spy-pullback",
    version="0.1.0",
    config_hash="fast20-slow50-cost-v1",
    execution_cost_policy=ExecutionCostPolicy(
        version="fixed-adverse-v1", half_spread_ppm=50, slippage_ppm=25
    ),
)
FAST, SLOW = 20, 50
EPOCH = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)  # a Monday open, UTC
PROBE = "bars.spy.1m.fixture"


@dataclass(frozen=True)
class Bar:
    """One completed bar. `ts` is the bar's CLOSE time.

    Frozen because a bar that can be mutated in place is a lookahead bug waiting
    to happen -- the poison run below has to build new objects, not edit these.
    """

    index: int
    ts: datetime
    close: float
    receipt: FactReceipt


def bars_hash(bars: list[Bar]) -> str:
    """Bind every bar byte-equivalent field into the verifier gate."""

    payload = [
        {
            "index": bar.index,
            "timestamp_utc": bar.ts.isoformat(),
            "close": bar.close,
            "receipt": bar.receipt.model_dump(mode="json"),
        }
        for bar in bars
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def fixture_bars(count: int = 2400) -> list[Bar]:
    """A deterministic series with no RNG anywhere.

    Two sine components at different periods so the fast and slow averages cross
    repeatedly. The slow component's period is ~107 bars and a signal fires on
    roughly one edge per cycle, so 2400 bars is what it takes to clear 25 with
    margin -- sized from the measured yield, not guessed.
    """

    bars: list[Bar] = []
    for i in range(count):
        close = 600.0 + 30.0 * math.sin(i / 17.0) + 8.0 * math.sin(i / 3.3)
        ts = EPOCH + timedelta(minutes=i)
        bars.append(
            Bar(
                index=i,
                ts=ts,
                close=round(close, 4),
                receipt=FactReceipt(
                    probe_id=PROBE,
                    source="fixture",
                    retrieved_at_utc=ts,
                    probe_inputs=(("formula", "closed-sine-v1"), ("index", str(i))),
                ),
            )
        )
    return bars


# --------------------------------------------------------------------------
# Primary implementation
# --------------------------------------------------------------------------


def _sma(bars: list[Bar], end_index: int, window: int) -> float:
    """Mean close over the `window` bars ending at and including `end_index`."""

    lo = end_index - window + 1
    if lo < 0:
        raise ValueError("not enough history")
    return round(sum(b.close for b in bars[lo : end_index + 1]) / window, 9)


def _facts_and_rules(bars: list[Bar], i: int) -> tuple[list[Fact], list[Rule]] | None:
    """The strategy, stated once, as facts plus rules over those facts.

    This is the single definition. `_fires_at` evaluates it and `build_packet`
    serialises it, so the firing decision and the packet cannot describe
    different strategies.

    That mattered: an adversarial pass found the two used to be independent.
    `build_packet` computed the three fact values while `_fires_at` decided
    separately, so a strategy that read tomorrow's bar in its ENTRY LOGIC while
    leaving its fact values honest was invisible to the delete and poison runs --
    they only ever saw the facts. Routing both through here forces any input the
    decision uses to appear as a fact, which is what the mutation runs check.

    ponytail: this makes the correct implementation the easy one; it does not
    make the wrong one impossible. Someone editing `_fires_at` to consult a bar
    directly would still escape. The gate against that is `signal_agreement`
    plus review, not this structure.
    """

    if i < SLOW:
        return None

    close = bars[i].close
    fast = _sma(bars, i, FAST)
    slow = _sma(bars, i, SLOW)
    known_at = bars[i].ts
    receipt = bars[i].receipt

    facts = [
        Fact(fact_id="close", label=f"{SYMBOL} close", value=close, unit="USD",
             known_at_utc=known_at, receipt=receipt),
        Fact(fact_id="sma_fast", label=f"SMA({FAST})", value=fast, unit="USD",
             known_at_utc=known_at, receipt=receipt),
        Fact(fact_id="sma_slow", label=f"SMA({SLOW})", value=slow, unit="USD",
             known_at_utc=known_at, receipt=receipt),
    ]
    rules = [
        Rule(rule_id="uptrend", label=f"SMA({FAST}) > SMA({SLOW})",
             lhs="sma_fast", op=">", rhs="sma_slow", result=fast > slow),
        Rule(rule_id="pullback", label=f"close < SMA({FAST})",
             lhs="close", op="<", rhs="sma_fast", result=close < fast),
    ]
    return facts, rules


def _fires_at(bars: list[Bar], i: int) -> bool:
    """Fires when every rule holds. Reads bars[:i+1] only, via `_facts_and_rules`."""

    built = _facts_and_rules(bars, i)
    return built is not None and all(rule.result for rule in built[1])


def detect(bars: list[Bar]) -> list[int]:
    """Indices where the rule fires on this bar but did not on the previous one."""

    out: list[int] = []
    prev = False
    for i in range(len(bars)):
        now = _fires_at(bars, i)
        if now and not prev:
            out.append(i)
        prev = now
    return out


def build_packet(bars: list[Bar], i: int) -> DecisionPacket:
    """Build the packet for a signal at bar `i`, reading only bars up to `i`.

    The decision is stamped one second after the bar closes and execution is not
    permitted until the next bar, so the packet can never claim it acted on the
    same instant it learned something.
    """

    bar = bars[i]
    decision_at = bar.ts + timedelta(seconds=1)

    built = _facts_and_rules(bars, i)
    if built is None:
        raise ValueError(f"not enough history to build a packet at bar {i}")
    facts, rules = built

    return DecisionPacket(
        packet_id=f"pkt-{SYMBOL.lower()}-{i}",
        strategy=STRATEGY,
        signal=Signal(
            signal_id=f"sig-{i}", symbol=SYMBOL, direction=Direction.LONG,
            decision_at_utc=decision_at,
            earliest_execution_at_utc=bars[i + 1].ts if i + 1 < len(bars)
            else decision_at + timedelta(minutes=1),
        ),
        rules=rules,
        facts=facts,
    )


# --------------------------------------------------------------------------
# Independent implementation
# --------------------------------------------------------------------------


def oracle(bars: list[Bar], i: int) -> OracleResult:
    """Recompute the same claims by a different route.

    Deliberately not sharing `_sma`: this walks a running window forward and
    divides once at the end, so an off-by-one in either window or an accumulation
    that drifts shows up as a mismatch instead of cancelling out.
    """

    def mean_to(end: int, window: int) -> float:
        total = 0.0
        for k in range(window):
            total += bars[end - k].close
        return round(total / window, 9)

    def fired(idx: int) -> bool:
        if idx < SLOW:
            return False
        return mean_to(idx, FAST) > mean_to(idx, SLOW) and bars[idx].close < mean_to(idx, FAST)

    return OracleResult(
        values={
            "close": bars[i].close,
            "sma_fast": mean_to(i, FAST),
            "sma_slow": mean_to(i, SLOW),
        },
        fired=fired(i) and not fired(i - 1),
        signal_at_utc=bars[i].ts + timedelta(seconds=1),
        direction=Direction.LONG,
        entry_eligible=i + 1 < len(bars),
    )


# --------------------------------------------------------------------------
# Mutation runs: the lookahead traps
# --------------------------------------------------------------------------


def mutation_evidence(bars: list[Bar], i: int) -> MutationEvidence:
    """Rebuild the packet twice with the future removed, then corrupted.

    Corruption is a large, sign-flipping perturbation rather than a nudge,
    because a poison the strategy could round away would let a real leak pass.
    """

    truncated = bars[: i + 1]
    poisoned = truncated + [
        Bar(
            index=b.index,
            ts=b.ts,
            close=round(-b.close * 13.7 + 4242.0, 4),
            receipt=b.receipt,
        )
        for b in bars[i + 1 :]
    ]
    return MutationEvidence(
        hash_with_future_deleted=canonical_hash(_rebuild(truncated, i)),
        hash_with_future_poisoned=canonical_hash(_rebuild(poisoned, i)),
    )


def _rebuild(series: list[Bar], i: int) -> DecisionPacket:
    """Rebuild at `i`, padding a synthetic next bar when the tail was truncated.

    The pad exists so the truncated run still has a bar `i+1` to name as the
    execution time; without it the packet falls back to a different value and the
    hash moves for a reason unrelated to lookahead -- a false failure, which is
    as damaging as a false pass.

    There used to be a line here forcing `earliest_execution_at_utc` to
    `EPOCH + (i+1) minutes`. It was pure liability: correct only for a series
    starting at this fixture's exact epoch with exact 1-minute spacing, and an
    audit showed the same closes shifted seven days produced
    futureDeleteChanges=25 -- a false FAIL on every packet. It would have
    detonated on REAL_BAR_REPLAY, which is the only path to promotion. Deleting
    it leaves the fixture passing and fixes the shifted case.
    """

    return build_packet(
        series
        if i + 1 < len(series)
        else series
        + [
            Bar(
                index=i + 1,
                ts=series[i].ts + timedelta(minutes=1),
                close=series[i].close,
                receipt=series[i].receipt,
            )
        ],
        i,
    )


# --------------------------------------------------------------------------
# The release gate
# --------------------------------------------------------------------------


def run_gate(
    limit: int = 25,
    bars: list[Bar] | None = None,
    evidence_class: EvidenceClass = EvidenceClass.SYNTHETIC_KERNEL,
) -> dict:
    """Verify the first `limit` signals and return the gate result.

    Reports counts per failure mode rather than one number, because "24 of 25"
    tells you nothing about whether the one failure was a rounding difference or
    a strategy reading tomorrow's price.
    """

    if evidence_class not in {
        EvidenceClass.SYNTHETIC_KERNEL,
        EvidenceClass.BYTE_ADDRESSED_BAR_SNAPSHOT,
    }:
        raise ValueError(
            "the generic kernel cannot self-award promotable evidence; use a "
            "market-data conformance runner"
        )
    bars = bars or fixture_bars()
    indices = detect(bars)[:limit]
    tally = {
        "factMismatches": 0, "postAsOfInputs": 0, "futureDeleteChanges": 0,
        "futurePoisonChanges": 0, "signalMismatches": 0, "unsupportedFacts": 0,
        "ruleMismatches": 0, "unmappedCheckFailures": 0,
    }
    by_check = {
        "differential_replay": "factMismatches", "admissibility": "postAsOfInputs",
        "future_delete": "futureDeleteChanges", "future_poison": "futurePoisonChanges",
        "signal_agreement": "signalMismatches", "unsupported_facts": "unsupportedFacts",
        "fact_receipts": "unsupportedFacts", "rule_evaluation": "ruleMismatches",
    }

    passed = 0
    packet_hashes: list[str] = []
    for i in indices:
        packet = build_packet(bars, i)
        packet_hashes.append(canonical_hash(packet))
        result = verify(
            packet,
            oracle(bars, i),
            mutation_evidence(bars, i),
            evidence_class,
        )
        if result.status is CheckStatus.PASS:
            passed += 1
        for check in result.failed:
            # A check the gate does not know how to count must not vanish, and
            # must not crash the gate either. `rule_evaluation` was added to the
            # verifier and never to this map: the release gate raised KeyError on
            # the one check that catches a rule contradicting its own facts, and
            # no test reached the line because they all call verify() directly.
            tally[by_check.get(check.check_id, "unmappedCheckFailures")] += 1

    return {
        "status": "PASS" if passed == len(indices) == limit else "FAIL",
        "packetsExpected": limit,
        "packetsPassed": passed,
        "packetHashes": packet_hashes,
        "barsHash": bars_hash(bars),
        **tally,
    }


def packets(limit: int = 25, bars: list[Bar] | None = None) -> Iterable[DecisionPacket]:
    bars = bars or fixture_bars()
    for i in detect(bars)[:limit]:
        yield build_packet(bars, i)


def verified_panel(
    index: int,
    bars: list[Bar] | None = None,
    evidence_class: EvidenceClass = EvidenceClass.SYNTHETIC_KERNEL,
) -> dict:
    """One packet rendered to its SSE panel envelope, evidence attached."""

    from app.decision_packet import to_panel

    if evidence_class not in {
        EvidenceClass.SYNTHETIC_KERNEL,
        EvidenceClass.BYTE_ADDRESSED_BAR_SNAPSHOT,
    }:
        raise ValueError(
            "the generic panel cannot self-award promotable evidence; use a "
            "market-data conformance runner"
        )
    bars = bars or fixture_bars()
    return to_panel(
        build_packet(bars, index),
        oracle(bars, index),
        mutation_evidence(bars, index),
        evidence_class,
    )


# --------------------------------------------------------------------------
# Self-check
# --------------------------------------------------------------------------


def demo() -> None:
    bars = fixture_bars()
    signals = detect(bars)
    assert len(signals) >= 25, f"fixture yields only {len(signals)} signals"

    gate = run_gate(25, bars)
    assert gate["status"] == "PASS", gate
    assert gate["packetsPassed"] == 25, gate

    # Determinism: same fixture, same hashes, run to run.
    a = [canonical_hash(p) for p in packets(25)]
    b = [canonical_hash(p) for p in packets(25)]
    assert a == b, "packet hashes are not reproducible"

    # The gate must catch a strategy that READS the future, which is not the same
    # as catching a packet that merely differs from a rebuild. The earlier version
    # of this check appended an extra Fact and asserted the mutation runs caught
    # it -- but an adversarial pass showed a cosmetic relabel fails identically,
    # so that assertion demonstrated rebuild-inequality and nothing about
    # lookahead. Real attack instead: make the fact computation itself read one
    # bar ahead, leaving the packet's shape untouched.
    i = signals[0]
    real_sma = globals()["_sma"]

    def peeking_sma(series: list[Bar], end_index: int, window: int) -> float:
        ahead = min(end_index + 1, len(series) - 1)
        return real_sma(series, ahead, window)

    globals()["_sma"] = peeking_sma
    try:
        leaked = verify(build_packet(bars, i), oracle(bars, i), mutation_evidence(bars, i))
    finally:
        globals()["_sma"] = real_sma

    assert leaked.status is CheckStatus.FAIL, "a strategy reading tomorrow must not pass"
    assert {c.check_id for c in leaked.failed} & {"future_delete", "future_poison"}, (
        f"the mutation runs must catch a genuine future read; failed: "
        f"{[c.check_id for c in leaked.failed]}"
    )

    # And the rules must recompute. A packet asserting a result its own facts
    # contradict is the one displayed value that used to go unchecked.
    lying = build_packet(bars, i)
    lying.rules[0].result = not lying.rules[0].result
    assert verify(lying, oracle(bars, i), mutation_evidence(bars, i)).status is CheckStatus.FAIL
    assert "rule_evaluation" in {
        c.check_id for c in verify(lying, oracle(bars, i), mutation_evidence(bars, i)).failed
    }

    # An input stamped after the decision is inadmissible.
    late = build_packet(bars, i)
    late.facts[0].known_at_utc = late.signal.decision_at_utc + timedelta(seconds=1)
    assert verify(late, oracle(bars, i), mutation_evidence(bars, i)).status is CheckStatus.FAIL

    print(f"proof_kernel: {len(signals)} signals detected, gate {gate['status']}")
    print(f"  {gate}")


if __name__ == "__main__":
    demo()
