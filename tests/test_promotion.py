"""The promotion gate, and the review surface that leads with it.

The gate exists to be failed. These tests mostly assert that it says no, and
that it says no for the right reasons -- a gate that blocks for the wrong reason
is as useless as one that passes.
"""

from __future__ import annotations

import json

import pytest

from app import ledger as lg
from app import promotion as pm
from app import proof_kernel as pk
from app.decision_packet import CheckStatus, EvidenceClass, OracleIndependence


@pytest.fixture(scope="module")
def bars():
    return pk.fixture_bars()


@pytest.fixture(scope="module")
def receipt(bars):
    pkts = list(pk.packets(25, bars))
    return lg.curve_receipt(lg.build_ledger(bars, pkts), bars, pkts)


@pytest.fixture(scope="module")
def kernel_gate(bars):
    return pk.run_gate(25, bars)


def _evaluate(receipt, kernel_gate, **kw):
    args = dict(
        kernel_gate=kernel_gate,
        receipt=receipt,
        evidence_class=EvidenceClass.SYNTHETIC_KERNEL,
        oracle_independence=OracleIndependence.SAME_AUTHOR_DIVERSE_IMPLEMENTATION,
        market_data_conformance=CheckStatus.NOT_RUN,
        models_spread=True,
        models_slippage=True,
    )
    args.update(kw)
    return pm.evaluate(**args)


def test_todays_honest_answer_is_blocked(receipt, kernel_gate):
    """Every kernel check is green and the strategy still cannot be promoted.
    That gap is the entire point of the gate."""

    gate = _evaluate(receipt, kernel_gate)
    assert kernel_gate["status"] == "PASS"
    assert receipt.acceptable
    assert gate.verdict is pm.Verdict.BLOCKED
    assert {b.code for b in gate.blockers} == {
        "SYNTHETIC_CORPUS",
        "MARKET_DATA_CONFORMANCE_NOT_RUN",
        "ORACLE_NOT_INDEPENDENT_ENOUGH",
    }


def test_the_gate_is_not_permanently_red(receipt, kernel_gate):
    """A gate that can never pass teaches people to ignore it."""

    gate = _evaluate(
        receipt,
        kernel_gate,
        evidence_class=EvidenceClass.REAL_BAR_REPLAY,
        oracle_independence=OracleIndependence.SEPARATE_AGENT_FROM_FROZEN_SPEC,
        market_data_conformance=CheckStatus.PASS,
    )
    assert gate.promotable, [b.code for b in gate.blockers]


def test_same_author_diversity_is_not_enough(receipt, kernel_gate):
    gate = _evaluate(
        receipt,
        kernel_gate,
        evidence_class=EvidenceClass.REAL_BAR_REPLAY,
        market_data_conformance=CheckStatus.PASS,
        oracle_independence=OracleIndependence.SAME_AUTHOR_DIVERSE_IMPLEMENTATION,
    )
    assert not gate.promotable
    assert "ORACLE_NOT_INDEPENDENT_ENOUGH" in {b.code for b in gate.blockers}


def test_a_peeked_test_split_blocks_promotion(receipt, kernel_gate):
    gate = _evaluate(
        receipt,
        kernel_gate,
        evidence_class=EvidenceClass.REAL_BAR_REPLAY,
        oracle_independence=OracleIndependence.INDEPENDENT_AUTHOR,
        market_data_conformance=CheckStatus.PASS,
        optimizer_had_test_access=True,
    )
    assert not gate.promotable
    assert "OPTIMIZER_SAW_TEST" in {b.code for b in gate.blockers}


def test_an_unacceptable_ledger_blocks_promotion(bars, kernel_gate):
    events = lg.build_ledger(bars, list(pk.packets(25, bars)))
    broken = [e.model_copy(deep=True) for e in events]
    fill = next(i for i, e in enumerate(broken) if e.type is lg.EventType.ORDER_FILL)
    broken[fill].position_delta += 5

    gate = _evaluate(
        lg.curve_receipt(broken, bars),
        kernel_gate,
        evidence_class=EvidenceClass.REAL_BAR_REPLAY,
        oracle_independence=OracleIndependence.INDEPENDENT_AUTHOR,
        market_data_conformance=CheckStatus.PASS,
        models_spread=True,
        models_slippage=True,
    )
    assert not gate.promotable
    assert any(b.code.startswith("LEDGER_") for b in gate.blockers)


def test_all_blockers_are_reported_not_just_the_first(receipt, kernel_gate):
    """Returning after the first would let someone fix one thing, re-run, and
    discover the next -- which reads as progress when nothing moved."""

    assert len(_evaluate(receipt, kernel_gate).blockers) == 3


def test_the_policy_hash_is_stable_and_covers_the_thresholds():
    first = pm.policy_hash()
    assert first == pm.policy_hash()
    original = pm.POLICY["minimum_oracle_independence"]
    try:
        pm.POLICY["minimum_oracle_independence"] = ["anything_goes"]
        assert pm.policy_hash() != first, "the hash must cover the thresholds"
    finally:
        pm.POLICY["minimum_oracle_independence"] = original
    assert pm.policy_hash() == first


# --------------------------------------------------------------------------
# The wire
# --------------------------------------------------------------------------


def test_review_route_leads_with_the_gate():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as http:
        raw = http.post("/api/kernel/review", json={}).text

    frames = [json.loads(l[6:]) for l in raw.splitlines() if l.startswith("data: ")]
    panels = [f for f in frames if f.get("type") == "data-backtest-review"]
    assert len(panels) == 1

    data = panels[0]["data"]
    assert data["promotion"]["verdict"] == "BLOCKED"
    # Five now: the two cost-model blockers joined the original three.
    assert len(data["promotion"]["blockers"]) == 5
    assert data["receipt"]["full_curve_matched"] is True
    assert data["curve"] and len(data["curve"]) == data["receipt"]["event_count"]

    # Money crosses the wire as integer micros, never a float.
    for point in data["curve"]:
        assert isinstance(point["equity_micros"], int)

    # The spoken summary must not claim promotability.
    text = "".join(f.get("delta", "") for f in frames if f.get("type") == "text-delta")
    assert "BLOCKED" in text


# --------------------------------------------------------------------------
# Attacks that previously reached PROMOTABLE
# --------------------------------------------------------------------------


def test_a_bare_kernel_gate_dict_cannot_promote(receipt):
    """`{"status": "PASS"}` with no counters used to clear the gate, because
    `.get(field, 0)` treated every missing counter as zero. Absence is not
    evidence of zero."""

    gate = pm.evaluate(
        kernel_gate={"status": "PASS"},
        receipt=receipt,
        evidence_class=EvidenceClass.REAL_BAR_REPLAY,
        oracle_independence=OracleIndependence.INDEPENDENT_AUTHOR,
        market_data_conformance=CheckStatus.PASS,
        models_spread=True,
        models_slippage=True,
    )
    assert not gate.promotable
    assert "KERNEL_COUNTER_MISSING" in {b.code for b in gate.blockers}


def test_an_empty_ledger_cannot_promote(kernel_gate, bars):
    """A strategy that never traded used to clear every check vacuously."""

    gate = pm.evaluate(
        kernel_gate=kernel_gate,
        receipt=lg.curve_receipt([], bars),
        evidence_class=EvidenceClass.REAL_BAR_REPLAY,
        oracle_independence=OracleIndependence.INDEPENDENT_AUTHOR,
        market_data_conformance=CheckStatus.PASS,
        models_spread=True,
        models_slippage=True,
    )
    assert not gate.promotable
    assert "EMPTY_LEDGER" in {b.code for b in gate.blockers}


def test_a_ledger_whose_prices_were_never_checked_cannot_promote(bars, kernel_gate):
    """The P0: a fabricated ledger worth $1,098,843 passed because no price was
    ever resolved back to its bar, and the unrun check reported clean."""

    events = lg.build_ledger(bars, list(pk.packets(25, bars)))
    gate = pm.evaluate(
        kernel_gate=kernel_gate,
        receipt=lg.curve_receipt(events),  # no bars
        evidence_class=EvidenceClass.REAL_BAR_REPLAY,
        oracle_independence=OracleIndependence.INDEPENDENT_AUTHOR,
        market_data_conformance=CheckStatus.PASS,
        models_spread=True,
        models_slippage=True,
    )
    assert not gate.promotable
    assert "PRICE_PROVENANCE_NOT_RUN" in {b.code for b in gate.blockers}


def test_a_forged_price_blocks_promotion(bars, kernel_gate):
    events = lg.build_ledger(bars, list(pk.packets(25, bars)))
    forged = [e.model_copy(deep=True) for e in events]
    i = next(j for j, e in enumerate(forged) if e.type is lg.EventType.ORDER_FILL)
    forged[i].price_micros *= 3
    forged[i].cash_delta_micros = -forged[i].position_delta * forged[i].price_micros

    gate = pm.evaluate(
        kernel_gate=kernel_gate,
        receipt=lg.curve_receipt(forged, bars),
        evidence_class=EvidenceClass.REAL_BAR_REPLAY,
        oracle_independence=OracleIndependence.INDEPENDENT_AUTHOR,
        market_data_conformance=CheckStatus.PASS,
        models_spread=True,
        models_slippage=True,
    )
    assert not gate.promotable
    assert "LEDGER_price_mismatches" in {b.code for b in gate.blockers}


def test_evidence_class_is_an_allowlist_not_a_denylist(receipt, kernel_gate):
    """Only named classes may promote. The previous version blocked exactly one,
    so anything new defaulted to acceptable."""

    for cls in EvidenceClass:
        gate = pm.evaluate(
            kernel_gate=kernel_gate,
            receipt=receipt,
            evidence_class=cls,
            oracle_independence=OracleIndependence.INDEPENDENT_AUTHOR,
            market_data_conformance=CheckStatus.PASS,
            models_spread=True,
            models_slippage=True,
        )
        assert gate.promotable is (cls in pm.PROMOTABLE_EVIDENCE), cls


def test_zero_packets_verified_cannot_promote(receipt):
    gate = pm.evaluate(
        kernel_gate={
            "status": "PASS", "packetsPassed": 0,
            **{k: 0 for k in pm.KERNEL_COUNTERS},
        },
        receipt=receipt,
        evidence_class=EvidenceClass.REAL_BAR_REPLAY,
        oracle_independence=OracleIndependence.INDEPENDENT_AUTHOR,
        market_data_conformance=CheckStatus.PASS,
        models_spread=True,
        models_slippage=True,
    )
    assert not gate.promotable
    assert "NO_PACKETS_VERIFIED" in {b.code for b in gate.blockers}


def test_every_policy_counter_names_a_field_that_exists(receipt):
    """6 of 13 names in the old POLICY referred to fields that did not exist, so
    the hash covered a policy nobody executed."""

    for name in pm.LEDGER_COUNTERS:
        assert hasattr(receipt, name), f"POLICY names a missing receipt field: {name}"


def test_the_policy_hash_covers_the_gate_itself_not_just_the_document():
    """POLICY was a description nobody executed: evaluate() read zero keys from
    it, so an audit changed the gate's verdict ten ways without moving this
    hash. The hash now covers evaluate's source as well."""

    import inspect

    assert "inspect.getsource(evaluate)" in inspect.getsource(pm.policy_hash)
    first = pm.policy_hash()
    assert first == pm.policy_hash()
    # POLICY snapshots the thresholds at import, so editing a constant in the
    # source -- the actual threat, someone loosening a bound after seeing the
    # test split -- shows up as a changed POLICY. Runtime mutation of the set
    # object does NOT move the hash, which is a real limit worth stating: this
    # seal defends against edits, not against monkeypatching a running process.
    original = pm.POLICY["minimum_oracle_independence"]
    try:
        pm.POLICY["minimum_oracle_independence"] = ["anything_goes"]
        assert pm.policy_hash() != first, "loosening a threshold must move the seal"
    finally:
        pm.POLICY["minimum_oracle_independence"] = original
    assert pm.policy_hash() == first


def test_a_partial_packet_run_cannot_promote(receipt):
    """1-of-25 with status PASS returned PROMOTABLE with no blockers at all.
    packetsExpected was transmitted, displayed, and never read."""

    gate = pm.evaluate(
        kernel_gate={
            "status": "PASS", "packetsExpected": 25, "packetsPassed": 1,
            **{k: 0 for k in pm.KERNEL_COUNTERS},
        },
        receipt=receipt,
        evidence_class=EvidenceClass.REAL_BAR_REPLAY,
        oracle_independence=OracleIndependence.INDEPENDENT_AUTHOR,
        market_data_conformance=CheckStatus.PASS,
        models_spread=True,
        models_slippage=True,
    )
    assert not gate.promotable
    assert "PACKETS_INCOMPLETE" in {b.code for b in gate.blockers}


def test_a_null_counter_does_not_promote(receipt):
    """`elif kernel_gate[field]:` was a truthiness test one line under a comment
    saying absence is a blocker, not a zero. None is falsey."""

    counters = {k: 0 for k in pm.KERNEL_COUNTERS}
    counters["factMismatches"] = None
    gate = pm.evaluate(
        kernel_gate={"status": "PASS", "packetsExpected": 25, "packetsPassed": 25, **counters},
        receipt=receipt,
        evidence_class=EvidenceClass.REAL_BAR_REPLAY,
        oracle_independence=OracleIndependence.INDEPENDENT_AUTHOR,
        market_data_conformance=CheckStatus.PASS,
        models_spread=True,
        models_slippage=True,
    )
    assert not gate.promotable


def test_an_unacceptable_receipt_can_never_promote(receipt, kernel_gate, bars):
    """The worst gap of the session, found by an external reviewer's code trace.

    `if not receipt.acceptable:` entered the per-counter loop, but that loop used
    truthiness -- and None is falsey. A receipt whose authority counters were
    never computed emitted NO blocker at all, so an unacceptable receipt returned
    promotable=True with an EMPTY blocker list. The gate's verdict silently
    disagreed with the receipt's own.
    """

    events = lg.build_ledger(bars, list(pk.packets(25, bars)))
    blind = lg.curve_receipt(events, bars)  # bars but no packets
    assert not blind.acceptable

    gate = pm.evaluate(
        kernel_gate=kernel_gate,
        receipt=blind,
        evidence_class=EvidenceClass.REAL_BAR_REPLAY,
        oracle_independence=OracleIndependence.INDEPENDENT_AUTHOR,
        market_data_conformance=CheckStatus.PASS,
        models_spread=True,
        models_slippage=True,
    )
    assert not gate.promotable
    assert "LEDGER_UNACCEPTABLE" in {b.code for b in gate.blockers}
    assert "AUTHORITY_NOT_RUN" in {b.code for b in gate.blockers}


def test_the_gate_never_disagrees_with_the_receipt(receipt, kernel_gate):
    """Invariant, not an instance: acceptable=False must always yield blockers."""

    for name in ("unauthorized_fills", "early_entries", "price_mismatches"):
        broken = receipt.model_copy(deep=True)
        setattr(broken, name, None)
        gate = pm.evaluate(
            kernel_gate=kernel_gate,
            receipt=broken,
            evidence_class=EvidenceClass.REAL_BAR_REPLAY,
            oracle_independence=OracleIndependence.INDEPENDENT_AUTHOR,
            market_data_conformance=CheckStatus.PASS,
            models_spread=True,
            models_slippage=True,
        )
        assert not broken.acceptable
        assert gate.blockers, f"{name}=None produced no blocker"
        assert not gate.promotable
