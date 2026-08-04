"""Proof Kernel v0: the gate, the traps it must catch, and the wire format.

The tests that matter here are the negative ones. A kernel that verifies honest
packets and nothing else is decoration -- what makes it worth shipping is that
it refuses the dishonest ones, so most of this file is about making it fail.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app import proof_kernel as pk
from app.decision_packet import CheckStatus, Fact, FactReceipt, canonical_hash, verify


@pytest.fixture(scope="module")
def bars():
    return pk.fixture_bars()


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def test_gate_passes_all_twentyfive_with_zero_of_every_failure_mode(bars):
    gate = pk.run_gate(25, bars)
    assert gate["packetHashes"] == [canonical_hash(packet) for packet in pk.packets(25, bars)]
    assert gate["barsHash"] == pk.bars_hash(bars)
    assert {key: value for key, value in gate.items() if key not in {"packetHashes", "barsHash"}} == {
        "status": "PASS",
        "packetsExpected": 25,
        "packetsPassed": 25,
        "factMismatches": 0,
        "postAsOfInputs": 0,
        "futureDeleteChanges": 0,
        "futurePoisonChanges": 0,
        "signalMismatches": 0,
        "unsupportedFacts": 0,
        "ruleMismatches": 0,
        # A check the gate cannot map must be counted, not dropped and not
        # crashed on: rule_evaluation was unmapped and raised KeyError.
        "unmappedCheckFailures": 0,
    }


def test_packets_are_byte_reproducible_across_runs():
    """Two independent constructions must hash identically.

    If this fails, every other guarantee is void: a hash that moves on its own
    cannot witness that data did not move.
    """

    first = [canonical_hash(p) for p in pk.packets(25)]
    second = [canonical_hash(p) for p in pk.packets(25)]
    assert first == second
    assert len(set(first)) == 25, "packets must be distinguishable from each other"


def test_fixture_is_not_market_data_and_says_so():
    """Guards the honesty of the claim, not the arithmetic."""

    assert "synthetic fixture, not market data" in pk.__doc__


# --------------------------------------------------------------------------
# The traps
# --------------------------------------------------------------------------


def test_a_strategy_that_reads_tomorrow_is_caught_by_the_mutation_runs(bars, monkeypatch):
    """The central claim, tested by actually reading the future.

    An earlier version of this test appended an extra Fact and asserted the
    mutation runs caught it. An adversarial pass showed a purely cosmetic change
    -- relabelling a fact, reading nothing -- fails those checks identically. So
    that test demonstrated "this packet differs from a rebuild" and established
    nothing whatsoever about lookahead.

    This one moves the future read into the fact computation itself, leaving the
    packet's shape untouched, which is the thing the checks are supposed to be
    for.
    """

    i = pk.detect(bars)[0]
    real_sma = pk._sma

    def peeking_sma(series, end_index, window):
        return real_sma(series, min(end_index + 1, len(series) - 1), window)

    monkeypatch.setattr(pk, "_sma", peeking_sma)
    result = verify(
        pk.build_packet(bars, i), pk.oracle(bars, i), pk.mutation_evidence(bars, i)
    )

    assert result.status is CheckStatus.FAIL
    caught_by = {c.check_id for c in result.failed}
    assert {"future_delete", "future_poison"} & caught_by, (
        f"the mutation runs must catch a genuine future read, got {caught_by}"
    )


def test_a_cosmetic_change_is_not_mistaken_for_lookahead(bars):
    """The complement, and the reason the test above was rewritten.

    Relabelling a fact changes the packet without reading anything. It still
    fails, because the mutation runs compare against a fresh rebuild -- but this
    pins the fact that hash-inequality is what they detect, so nobody reads a
    future_delete failure as proof of a future read again.
    """

    i = pk.detect(bars)[0]
    relabelled = pk.build_packet(bars, i)
    relabelled.facts[0].label = "SPY close (relabelled)"

    failed = {
        c.check_id
        for c in verify(
            relabelled, pk.oracle(bars, i), pk.mutation_evidence(bars, i)
        ).failed
    }
    assert {"future_delete", "future_poison"} & failed


def test_a_rule_result_its_own_facts_contradict_is_caught(bars):
    """`result` used to be an unchecked boolean rendered straight to the user."""

    i = pk.detect(bars)[0]
    lying = pk.build_packet(bars, i)
    lying.rules[0].result = not lying.rules[0].result

    result = verify(lying, pk.oracle(bars, i), pk.mutation_evidence(bars, i))

    assert result.status is CheckStatus.FAIL
    assert "rule_evaluation" in {c.check_id for c in result.failed}


def test_every_rule_recomputes_on_every_shipped_packet(bars):
    """Not just the tampered one: the honest packets must recompute too, or the
    check is passing for the wrong reason."""

    for packet in pk.packets(25, bars):
        values = {f.fact_id: f.value for f in packet.facts}
        for rule in packet.rules:
            rhs = values[rule.rhs] if isinstance(rule.rhs, str) else rule.rhs
            expected = {"<": values[rule.lhs] < rhs, ">": values[rule.lhs] > rhs}[rule.op]
            assert rule.result == expected, f"{packet.packet_id}/{rule.rule_id}"


def test_an_input_stamped_after_the_decision_is_inadmissible(bars):
    i = pk.detect(bars)[0]
    late = pk.build_packet(bars, i)
    late.facts[0].known_at_utc = late.signal.decision_at_utc + timedelta(seconds=1)

    result = verify(late, pk.oracle(bars, i), pk.mutation_evidence(bars, i))

    assert result.status is CheckStatus.FAIL
    assert "admissibility" in {c.check_id for c in result.failed}


def test_a_disagreeing_oracle_fails_the_packet(bars):
    """A second implementation that computes a different number must block it."""

    i = pk.detect(bars)[0]
    packet = pk.build_packet(bars, i)
    wrong = pk.oracle(bars, i)
    wrong.values["sma_fast"] = wrong.values["sma_fast"] + 0.01

    result = verify(packet, wrong, pk.mutation_evidence(bars, i))

    assert result.status is CheckStatus.FAIL
    assert "differential_replay" in {c.check_id for c in result.failed}


def test_missing_evidence_is_never_a_pass(bars):
    """NOT_RUN must fail. A gate that passes when it did not run is worse than
    no gate, because it converts absence into approval."""

    i = pk.detect(bars)[0]
    packet = pk.build_packet(bars, i)

    result = verify(packet)  # no oracle, no mutation runs

    assert result.status is CheckStatus.FAIL
    assert {c.check_id for c in result.checks if c.status is CheckStatus.NOT_RUN} == {
        "differential_replay",
        "signal_agreement",
        "future_delete",
        "future_poison",
    }


def test_a_fact_without_a_receipt_cannot_be_displayed(bars):
    i = pk.detect(bars)[0]
    packet = pk.build_packet(bars, i)
    packet.facts[0].receipt = None

    result = verify(packet, pk.oracle(bars, i), pk.mutation_evidence(bars, i))

    assert result.status is CheckStatus.FAIL
    assert "fact_receipts" in {c.check_id for c in result.failed}


def test_every_rule_cites_facts_the_packet_actually_carries(bars):
    for packet in pk.packets(25, bars):
        known = {f.fact_id for f in packet.facts}
        for rule in packet.rules:
            assert set(rule.fact_refs) <= known, (
                f"{packet.packet_id}/{rule.rule_id} cites a fact it does not carry"
            )


def test_execution_can_never_precede_the_decision(bars):
    for packet in pk.packets(25, bars):
        assert packet.signal.earliest_execution_at_utc > packet.signal.decision_at_utc


# --------------------------------------------------------------------------
# The wire
# --------------------------------------------------------------------------


def _frames(raw: str) -> list[dict]:
    return [
        json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")
    ]


def test_route_emits_a_decision_packet_panel_carrying_its_own_verdict():
    """The panel name is the contract the frontend renderer binds to."""

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as http:
        frames = _frames(http.post("/api/kernel/chat", json={}).text)

    panels = [f for f in frames if f.get("type") == "data-decision-packet"]
    assert len(panels) == 1, [f.get("type") for f in frames]

    data = panels[0]["data"]
    assert data["packet_hash"].startswith("sha256:")
    assert data["verification"]["status"] == "PASS"
    assert data["signal"]["symbol"] == "SPY"
    assert len(data["facts"]) == 3
    assert data["evidence"]["oracle"] is not None
    assert data["evidence"]["mutation"] is not None
    assert all(f["receipt"] is not None for f in data["facts"])

    # Only one panel type exists in v0. If a second appears, the closed
    # vocabulary has been widened without the design saying so.
    emitted = {f["type"] for f in frames if str(f.get("type", "")).startswith("data-")}
    assert emitted == {"data-decision-packet"}


def test_route_refuses_a_bar_that_is_not_a_signal():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as http:
        frames = _frames(http.post("/api/kernel/chat", json={"index": 3}).text)

    assert any(f.get("type") == "error" for f in frames)
    assert not any(f.get("type") == "data-decision-packet" for f in frames)
