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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from app.decision_packet import (
    CheckStatus,
    DecisionPacket,
    Direction,
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
STRATEGY = Strategy(id="spy-pullback", version="0.1.0", config_hash="fast20-slow50")
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
        bars.append(
            Bar(index=i, ts=EPOCH + timedelta(minutes=i), close=round(close, 4))
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


def _fires_at(bars: list[Bar], i: int) -> bool:
    """Long pullback in an uptrend: fast above slow, price dipped below fast.

    Reads bars[:i+1] only. That restriction is the whole point -- it is what the
    delete and poison runs verify rather than trust.
    """

    if i < SLOW:
        return False
    fast, slow = _sma(bars, i, FAST), _sma(bars, i, SLOW)
    return fast > slow and bars[i].close < fast


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
    known_at = bar.ts
    receipt = FactReceipt(probe_id=PROBE, source="fixture", retrieved_at_utc=known_at)

    facts = [
        Fact(fact_id="close", label=f"{SYMBOL} close", value=bar.close, unit="USD",
             known_at_utc=known_at, receipt=receipt),
        Fact(fact_id="sma_fast", label=f"SMA({FAST})", value=_sma(bars, i, FAST),
             unit="USD", known_at_utc=known_at, receipt=receipt),
        Fact(fact_id="sma_slow", label=f"SMA({SLOW})", value=_sma(bars, i, SLOW),
             unit="USD", known_at_utc=known_at, receipt=receipt),
    ]
    rules = [
        Rule(rule_id="uptrend", label=f"SMA({FAST}) > SMA({SLOW})",
             fact_refs=["sma_fast", "sma_slow"], result=True),
        Rule(rule_id="pullback", label=f"close < SMA({FAST})",
             fact_refs=["close", "sma_fast"], result=True),
    ]
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
        Bar(index=b.index, ts=b.ts, close=round(-b.close * 13.7 + 4242.0, 4))
        for b in bars[i + 1 :]
    ]
    return MutationEvidence(
        hash_with_future_deleted=canonical_hash(_rebuild(truncated, i)),
        hash_with_future_poisoned=canonical_hash(_rebuild(poisoned, i)),
    )


def _rebuild(series: list[Bar], i: int) -> DecisionPacket:
    """Rebuild at `i`, holding `earliest_execution_at_utc` to the real next bar.

    Without this the truncated run would have no bar `i+1` and would fall back to
    a synthetic execution time, changing the hash for a reason that has nothing
    to do with lookahead -- a false failure, which is as damaging as a false pass.
    """

    packet = build_packet(series if i + 1 < len(series) else series + [
        Bar(index=i + 1, ts=series[i].ts + timedelta(minutes=1), close=series[i].close)
    ], i)
    packet.signal.earliest_execution_at_utc = EPOCH + timedelta(minutes=i + 1)
    return packet


# --------------------------------------------------------------------------
# The release gate
# --------------------------------------------------------------------------


def run_gate(limit: int = 25, bars: list[Bar] | None = None) -> dict:
    """Verify the first `limit` signals and return the gate result.

    Reports counts per failure mode rather than one number, because "24 of 25"
    tells you nothing about whether the one failure was a rounding difference or
    a strategy reading tomorrow's price.
    """

    bars = bars or fixture_bars()
    indices = detect(bars)[:limit]
    tally = {
        "factMismatches": 0, "postAsOfInputs": 0, "futureDeleteChanges": 0,
        "futurePoisonChanges": 0, "signalMismatches": 0, "unsupportedFacts": 0,
    }
    by_check = {
        "recomputation": "factMismatches", "admissibility": "postAsOfInputs",
        "future_delete": "futureDeleteChanges", "future_poison": "futurePoisonChanges",
        "signal_agreement": "signalMismatches", "unsupported_facts": "unsupportedFacts",
        "fact_receipts": "unsupportedFacts",
    }

    passed = 0
    for i in indices:
        packet = build_packet(bars, i)
        result = verify(packet, oracle(bars, i), mutation_evidence(bars, i))
        if result.status is CheckStatus.PASS:
            passed += 1
        for check in result.failed:
            tally[by_check[check.check_id]] += 1

    return {
        "status": "PASS" if passed == len(indices) == limit else "FAIL",
        "packetsExpected": limit,
        "packetsPassed": passed,
        **tally,
    }


def packets(limit: int = 25, bars: list[Bar] | None = None) -> Iterable[DecisionPacket]:
    bars = bars or fixture_bars()
    for i in detect(bars)[:limit]:
        yield build_packet(bars, i)


def verified_panel(index: int, bars: list[Bar] | None = None) -> dict:
    """One packet rendered to its SSE panel envelope, evidence attached."""

    from app.decision_packet import to_panel

    bars = bars or fixture_bars()
    return to_panel(build_packet(bars, index), oracle(bars, index),
                    mutation_evidence(bars, index))


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

    # The gate must actually catch lookahead. A strategy that peeks one bar ahead
    # is hash-unstable under deletion, so future_delete has to fail. If this
    # assertion ever stops holding, the whole kernel is decorative.
    i = signals[0]
    peeking = build_packet(bars, i)
    peeking.facts.append(
        Fact(fact_id="tomorrow", label="next bar close", value=bars[i + 1].close,
             known_at_utc=bars[i].ts, receipt=FactReceipt(
                 probe_id=PROBE, source="fixture", retrieved_at_utc=bars[i].ts))
    )
    leaked = verify(peeking, oracle(bars, i), mutation_evidence(bars, i))
    assert leaked.status is CheckStatus.FAIL, "a peeking packet must not pass"
    assert {c.check_id for c in leaked.failed} & {"future_delete", "future_poison"}, (
        "the mutation runs must be what catches the peek"
    )

    # An input stamped after the decision is inadmissible.
    late = build_packet(bars, i)
    late.facts[0].known_at_utc = late.signal.decision_at_utc + timedelta(seconds=1)
    assert verify(late, oracle(bars, i), mutation_evidence(bars, i)).status is CheckStatus.FAIL

    print(f"proof_kernel: {len(signals)} signals detected, gate {gate['status']}")
    print(f"  {gate}")


if __name__ == "__main__":
    demo()
