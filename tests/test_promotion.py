"""The promotion gate, and the review surface that leads with it.

The gate exists to be failed. These tests mostly assert that it says no, and
that it says no for the right reasons -- a gate that blocks for the wrong reason
is as useless as one that passes.
"""

from __future__ import annotations

import json
import inspect
from dataclasses import replace

import pytest

from app import ledger as lg
from app import promotion as pm
from app import proof_kernel as pk
from app.decision_packet import CheckStatus, EvidenceClass
from app.order_journal import build_order_journal


@pytest.fixture(scope="module")
def bars():
    return pk.fixture_bars()


@pytest.fixture(scope="module")
def artifacts(bars):
    packets = list(pk.packets(25, bars))
    return {
        "events": lg.build_ledger(bars, packets),
        "bars": bars,
        "packets": packets,
        "journal": build_order_journal(bars, packets),
    }


@pytest.fixture(scope="module")
def receipt(artifacts):
    return lg.curve_receipt(**artifacts)


@pytest.fixture(scope="module")
def kernel_gate(bars):
    return pk.run_gate(25, bars)


def _evaluate(artifacts, kernel_gate, **kw):
    args = dict(
        kernel_gate=kernel_gate,
        **artifacts,
    )
    args.update(kw)
    return pm.evaluate(**args)


def test_todays_honest_answer_is_blocked(receipt, artifacts, kernel_gate):
    """Every kernel check is green and the strategy still cannot be promoted.
    That gap is the entire point of the gate."""

    gate = _evaluate(artifacts, kernel_gate)
    assert kernel_gate["status"] == "PASS"
    assert receipt.acceptable
    assert gate.verdict is pm.Verdict.BLOCKED
    assert {b.code for b in gate.blockers} == {
        "SYNTHETIC_CORPUS",
        "MARKET_DATA_CONFORMANCE_NOT_RUN",
        "ORACLE_NOT_INDEPENDENT_ENOUGH",
        "SPLIT_DISCIPLINE_NOT_PROVEN",
    }


def test_cost_blockers_have_no_caller_self_award_parameters():
    signature = inspect.signature(pm.evaluate)
    assert "models_spread" not in signature.parameters
    assert "models_slippage" not in signature.parameters


def test_a_copied_receipt_cannot_self_award_cost_evidence(receipt, artifacts, kernel_gate):
    forged = receipt.model_copy(
        update={
            "spread_cost_micros": 1,
            "slippage_cost_micros": 1,
            "spread_model_mismatches": 0,
            "slippage_model_mismatches": 0,
            "execution_price_mismatches": 0,
            "execution_policy_mismatches": 0,
        }
    )
    gate = _evaluate(artifacts, kernel_gate)
    codes = {blocker.code for blocker in gate.blockers}
    assert "NO_SPREAD_MODEL" not in codes
    assert "NO_SLIPPAGE_MODEL" not in codes
    assert forged.model_dump() != receipt.model_dump()


def test_external_inputs_cannot_self_award_absent_execution_authenticity(bars, kernel_gate):
    """Green caller claims cannot replace an order-journal artifact."""

    packets = list(pk.packets(25, bars))
    events = lg.build_ledger(bars, packets)
    gate = _evaluate(
        {"events": events, "bars": bars, "packets": packets, "journal": None},
        kernel_gate,
    )
    assert not gate.promotable
    assert "CROSS_STREAM_RECONCILIATION_NOT_RUN" in {
        blocker.code for blocker in gate.blockers
    }


def test_frozen_oracle_is_derived_from_the_bundle(artifacts, kernel_gate):
    gate = _evaluate(
        artifacts,
        kernel_gate,
    )
    assert gate.independent_oracle is not None
    assert gate.independent_oracle.matched
    assert not gate.independent_oracle.acceptable
    assert not gate.independent_oracle.provenance_verified
    assert "ORACLE_NOT_INDEPENDENT_ENOUGH" in {b.code for b in gate.blockers}


def test_split_access_cannot_be_self_awarded_by_a_caller(artifacts, kernel_gate):
    signature = inspect.signature(pm.evaluate)
    assert "optimizer_had_test_access" not in signature.parameters
    assert "candidate_frozen_before_test" not in signature.parameters
    gate = _evaluate(artifacts, kernel_gate)
    assert not gate.promotable
    assert not gate.split_discipline.provenance_verified
    assert "SPLIT_DISCIPLINE_NOT_PROVEN" in {b.code for b in gate.blockers}


def test_an_unacceptable_ledger_blocks_promotion(bars, kernel_gate):
    packets = list(pk.packets(25, bars))
    events = lg.build_ledger(bars, packets)
    broken = [e.model_copy(deep=True) for e in events]
    fill = next(i for i, e in enumerate(broken) if e.type is lg.EventType.ORDER_FILL)
    broken[fill].position_delta += 5

    gate = _evaluate(
        {"events": broken, "bars": bars, "packets": packets, "journal": None},
        kernel_gate,
    )
    assert not gate.promotable
    assert any(b.code.startswith("LEDGER_") for b in gate.blockers)


def test_coherent_quantity_inflation_cannot_reach_promotion(bars, kernel_gate):
    """A forged but internally conserving 6x position must not inherit the
    unchanged decision packet's authority, even when every external gate passes.
    """

    packets = list(pk.packets(25, bars))
    forged = [e.model_copy(deep=True) for e in lg.build_ledger(bars, packets)]
    packet_id = next(e.packet_id for e in forged if e.type is lg.EventType.ORDER_FILL)
    for event in forged:
        if event.type is lg.EventType.ORDER_FILL and event.packet_id == packet_id:
            event.qty += 5
            event.position_delta = event.qty if event.side is lg.Side.BUY else -event.qty
            event.cash_delta_micros = -event.position_delta * event.price_micros

    gate = _evaluate(
        {
            "events": forged,
            "bars": bars,
            "packets": packets,
            "journal": build_order_journal(bars, packets),
        },
        kernel_gate,
    )

    assert not gate.promotable
    assert "LEDGER_unauthorized_fills" in {b.code for b in gate.blockers}


def test_packet_and_fill_co_mutation_breaks_verified_packet_binding(bars, kernel_gate):
    """An attacker changes both sides of the local quantity comparison. The
    receipt can remain internally acceptable, but it is no longer the packet set
    that the verifier passed and therefore cannot promote.
    """

    packets = list(pk.packets(25, bars))
    events = lg.build_ledger(bars, packets)
    packets[0] = packets[0].model_copy(
        update={"strategy": packets[0].strategy.model_copy(update={"position_qty": 6})}
    )
    packet_id = packets[0].packet_id
    for event in events:
        if event.type is lg.EventType.ORDER_FILL and event.packet_id == packet_id:
            event.qty = 6
            event.position_delta = 6 if event.side is lg.Side.BUY else -6
            event.spread_cost_micros *= 6
            event.slippage_cost_micros *= 6
            event.cash_delta_micros = -event.position_delta * event.price_micros

    receipt = lg.curve_receipt(events, bars, packets)
    gate = _evaluate(
        {
            "events": events,
            "bars": bars,
            "packets": packets,
            "journal": build_order_journal(bars, packets),
        },
        kernel_gate,
    )

    assert receipt.acceptable, "the local packet and ledger agree with each other"
    assert not gate.promotable
    assert "PACKET_SET_MISMATCH" in {b.code for b in gate.blockers}


def test_deleting_a_packet_and_its_whole_trade_breaks_completeness_binding(bars, kernel_gate):
    """Deleting the decision and every event it authorized used to leave only
    clean counters. The original 25-packet gate must not certify a 24-packet ledger.
    """

    packets = list(pk.packets(25, bars))
    events = lg.build_ledger(bars, packets)
    deleted_packet_id = packets[0].packet_id
    packets = packets[1:]
    events = [event for event in events if event.packet_id != deleted_packet_id]
    for seq, event in enumerate(events):
        event.seq = seq

    receipt = lg.curve_receipt(events, bars, packets)
    gate = _evaluate(
        {
            "events": events,
            "bars": bars,
            "packets": packets,
            "journal": build_order_journal(bars, packets),
        },
        kernel_gate,
    )

    assert receipt.acceptable, "the remaining 24 episodes are internally complete"
    assert not gate.promotable
    assert "PACKET_SET_MISMATCH" in {b.code for b in gate.blockers}


def test_all_blockers_are_reported_not_just_the_first(artifacts, kernel_gate):
    """Returning after the first would let someone fix one thing, re-run, and
    discover the next -- which reads as progress when nothing moved."""

    assert len(_evaluate(artifacts, kernel_gate).blockers) == 4


def test_the_policy_hash_is_stable_and_covers_the_thresholds():
    first = pm.policy_hash()
    assert first == pm.policy_hash()
    original = pm.POLICY["frozen_oracle_hashes"]
    try:
        pm.POLICY["frozen_oracle_hashes"] = {"implementation": "sha256:forged"}
        assert pm.policy_hash() != first, "the hash must cover the thresholds"
    finally:
        pm.POLICY["frozen_oracle_hashes"] = original
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
    # The cost blockers clear only from verified, positive adverse fill costs;
    # Real-corpus, market-conformance and oracle-provenance gaps remain.
    assert len(data["promotion"]["blockers"]) == 4
    assert data["promotion"]["independent_oracle"]["matched"] is True
    assert data["promotion"]["independent_oracle"]["provenance_verified"] is False
    assert data["receipt"]["full_curve_matched"] is True
    assert "flat_pnl_residual_micros" not in data["receipt"]
    assert data["receipt"]["cross_stream_reconciliation"] == "PASS"
    assert data["receipt"]["order_journal_receipt"]["total_mismatches"] == 0
    assert data["curve"] and len(data["curve"]) == data["receipt"]["event_count"]
    assert data["summary"]["cumulative_spread_cost_micros"] > 0
    assert data["summary"]["cumulative_slippage_cost_micros"] > 0
    assert {row["split_id"] for row in data["split_metrics"]} == {
        "DEVELOPMENT",
        "VALIDATION",
        "TEST",
    }
    assert "OPENED_HOLDOUT" not in {row["split_id"] for row in data["split_metrics"]}
    assert all(row["sampled_at_event_times"] for row in data["split_metrics"])
    assert all(
        {
            "peak_at_utc",
            "trough_at_utc",
            "recovered_at_utc",
            "decline_duration_seconds",
            "recovery_duration_seconds",
            "worst_drawdown_underwater_seconds",
            "episode_ids_in_drawdown_interval",
        }
        <= row.keys()
        for row in data["split_metrics"]
    )
    stress = {row["scenario_id"]: row for row in data["execution_stress"]}
    assert set(stress) == {"baseline", "cost-2x", "cost-4x", "delay-1", "delay-5"}
    assert stress["cost-4x"]["final_equity_micros"] < stress["cost-2x"]["final_equity_micros"]
    assert stress["cost-2x"]["final_equity_micros"] < stress["baseline"]["final_equity_micros"]
    assert stress["baseline"]["attempted_packet_count"] == 25
    assert stress["baseline"]["executed_packet_count"] == 17
    assert stress["baseline"]["collision_skipped_packet_count"] == 8
    assert stress["baseline"]["tail_dropped_packet_count"] == 0
    assert all(
        row["assumption"] == "modelled_counterfactual_not_observed_execution"
        for row in stress.values()
    )
    nulls = data["null_benchmarks"]
    assert nulls["split_id"] == "TEST"
    assert nulls["matched_random_trial_count"] == 128
    assert nulls["matched_random_seed"] == 240803
    assert nulls["matched_episode_count"] == 2
    assert nulls["evidence_scope"] == "synthetic_timing_diagnostic_not_promotion_evidence"
    assert nulls["manifest_hash"] == data["promotion"]["split_discipline"]["manifest_hash"]
    assert nulls["bars_hash"] == data["receipt"]["bars_hash"]
    assert nulls["journal_root_hash"] == data["receipt"]["order_journal_receipt"]["journal_root_hash"]
    assert nulls["packet_set_hash"].startswith("sha256:")
    adequacy = data["statistical_adequacy"]
    assert adequacy["episode_count"] == 2
    assert adequacy["excluded_cross_boundary_episodes"] == 0
    assert adequacy["ledger_root_hash"] == data["receipt"]["ledger_root_hash"]
    assert adequacy["manifest_hash"] == data["promotion"]["split_discipline"]["manifest_hash"]
    adequacy_checks = {row["check_id"]: row for row in adequacy["checks"]}
    assert adequacy_checks["candidate_trial_ledger"]["status"] == "FAIL"
    assert adequacy_checks["candidate_trial_ledger"]["observed"] == 9
    assert {
        row["status"] for key, row in adequacy_checks.items() if key != "candidate_trial_ledger"
    } == {"NOT_RUN"}
    assert next(
        row for row in adequacy["checks"] if row["check_id"] == "test_confidence_interval"
    )["required"] == 30
    robustness = data["robustness"]
    assert robustness["baseline_matches_bound_ledger"] is True
    assert robustness["access_provenance_verified"] is False
    assert len(robustness["rows"]) == 9
    assert robustness["opened_holdout_observed_variants"] == 2
    baseline = robustness["rows"][0]
    assert baseline["variant_id"] == "baseline"
    assert baseline["test_net_pnl_micros"] == nulls["strategy_net_pnl_micros"]
    assert next(row for row in robustness["rows"] if row["variant_id"] == "without-uptrend")[
        "test_net_pnl_micros"
    ] is None
    trials = data["trial_ledger"]
    assert trials["trial_count"] == 9
    assert trials["batch_complete"] is True
    assert trials["global_completeness_verified"] is False
    assert trials["pre_test_registration_verified"] is False
    assert trials["opened_holdout_observed_trial_count"] == 2
    assert len(trials["records"]) == 9
    walk = data["walk_forward"]
    assert walk["folds_end_before_test"] is True
    assert walk["purge_verified"] is True
    assert walk["access_provenance_verified"] is False
    assert len(walk["rows"]) == 9
    assert walk["matrix_root_hash"] == __import__("app.experiment", fromlist=["load_manifest"]).load_manifest()[0].walk_forward_result_root
    assert adequacy_checks["probability_of_backtest_overfitting"]["status"] == "NOT_RUN"
    assert adequacy_checks["probability_of_backtest_overfitting"]["observed"] == 3
    lifecycle = data["execution_lifecycle"]
    assert lifecycle["scenario_count"] == 6
    assert lifecycle["expectation_mismatches"] == 0
    assert all(
        lifecycle[name]
        for name in (
            "covers_partial_fill", "covers_cancel", "covers_reject",
            "covers_expiry", "covers_latency_breach",
        )
    )
    assert "not_broker_evidence" in lifecycle["evidence_scope"]
    cancel = next(row for row in lifecycle["observations"] if row["scenario_id"] == "partial-then-cancel")
    assert (cancel["filled_qty"], cancel["remaining_qty"]) == (3, 7)
    options = data["option_readiness"]
    assert options["status"] == "NOT_RUN"
    assert options["eligible_quote_count"] is None
    assert options["crossed_quote_mismatches"] is None
    assert options["expired_definition_mismatches"] is None
    assert options["duplicate_definition_rows"] is None
    assert options["duplicate_quote_rows"] is None
    assert options["provider_replay_verified"] is False
    assert options["point_in_time_definitions_verified"] is False
    assert options["selector_ready"] is False
    source = data["source_compiler"]
    assert source["source_status"] == "NOT_RUN"
    assert source["source_hash"] is None
    assert source["clause_count"] is None
    assert source["snapshot_root_hash"] is None
    assert source["unresolved_ambiguity_count"] is None
    assert source["human_lock_verified"] is False
    assert source["literal_version_ready"] is False
    assert source["reconstructed_version_ready"] is False
    assert source["repaired_version_ready"] is False

    # Money crosses the wire as integer micros, never a float.
    for point in data["curve"]:
        assert isinstance(point["equity_micros"], int)

    # The spoken summary must not claim promotability.
    text = "".join(f.get("delta", "") for f in frames if f.get("type") == "text-delta")
    assert "BLOCKED" in text


# --------------------------------------------------------------------------
# Attacks that previously reached PROMOTABLE
# --------------------------------------------------------------------------


def test_a_bare_kernel_gate_dict_cannot_promote(artifacts):
    """`{"status": "PASS"}` with no counters used to clear the gate, because
    `.get(field, 0)` treated every missing counter as zero. Absence is not
    evidence of zero."""

    gate = pm.evaluate(
        kernel_gate={"status": "PASS"},
        **artifacts,
    )
    assert not gate.promotable
    assert "KERNEL_COUNTER_MISSING" in {b.code for b in gate.blockers}


def test_an_empty_ledger_cannot_promote(kernel_gate, bars):
    """A strategy that never traded used to clear every check vacuously."""

    gate = pm.evaluate(
        kernel_gate=kernel_gate,
        events=[],
        bars=bars,
        packets=None,
        journal=None,
    )
    assert not gate.promotable
    assert "EMPTY_LEDGER" in {b.code for b in gate.blockers}


def test_a_ledger_whose_prices_were_never_checked_cannot_promote(bars, kernel_gate):
    """The P0: a fabricated ledger worth $1,098,843 passed because no price was
    ever resolved back to its bar, and the unrun check reported clean."""

    events = lg.build_ledger(bars, list(pk.packets(25, bars)))
    gate = pm.evaluate(
        kernel_gate=kernel_gate,
        events=events,
        bars=None,
        packets=None,
        journal=None,
    )
    assert not gate.promotable
    assert "PRICE_PROVENANCE_NOT_RUN" in {b.code for b in gate.blockers}


def test_a_forged_price_blocks_promotion(bars, kernel_gate):
    packets = list(pk.packets(25, bars))
    events = lg.build_ledger(bars, packets)
    forged = [e.model_copy(deep=True) for e in events]
    i = next(j for j, e in enumerate(forged) if e.type is lg.EventType.ORDER_FILL)
    forged[i].price_micros *= 3
    forged[i].cash_delta_micros = -forged[i].position_delta * forged[i].price_micros

    gate = pm.evaluate(
        kernel_gate=kernel_gate,
        events=forged,
        bars=bars,
        packets=packets,
        journal=build_order_journal(bars, packets),
    )
    assert not gate.promotable
    assert "LEDGER_execution_price_mismatches" in {b.code for b in gate.blockers}


def test_downstream_streams_cannot_substitute_bars_under_an_old_kernel_gate(
    bars, kernel_gate
):
    packets = list(pk.packets(25, bars))
    original_events = lg.build_ledger(bars, packets)
    first_fill = next(event for event in original_events if event.type is lg.EventType.ORDER_FILL)
    changed_bars = [
        replace(bar, close=bar.close - 100)
        if bar.ts == first_fill.at_utc
        else bar
        for bar in bars
    ]
    changed_events = lg.build_ledger(changed_bars, packets)
    gate = pm.evaluate(
        kernel_gate=kernel_gate,
        events=changed_events,
        bars=changed_bars,
        packets=packets,
        journal=build_order_journal(changed_bars, packets),
    )
    assert not gate.promotable
    assert "BAR_SET_MISMATCH" in {blocker.code for blocker in gate.blockers}


def test_evidence_class_is_an_allowlist_not_a_denylist(artifacts, kernel_gate):
    """A caller cannot relabel synthetic bars or self-award conformance."""

    signature = inspect.signature(pm.evaluate)
    assert "evidence_class" not in signature.parameters
    assert "market_data_conformance" not in signature.parameters
    gate = pm.evaluate(kernel_gate=kernel_gate, **artifacts)
    assert gate.market_data.evidence_class is EvidenceClass.SYNTHETIC_KERNEL
    assert not gate.market_data.acceptable


def test_zero_packets_verified_cannot_promote(artifacts):
    gate = pm.evaluate(
        kernel_gate={
            "status": "PASS", "packetsPassed": 0,
            **{k: 0 for k in pm.KERNEL_COUNTERS},
        },
        **artifacts,
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
    original = pm.POLICY["frozen_oracle_hashes"]
    try:
        pm.POLICY["frozen_oracle_hashes"] = {"implementation": "sha256:forged"}
        assert pm.policy_hash() != first, "loosening a threshold must move the seal"
    finally:
        pm.POLICY["frozen_oracle_hashes"] = original
    assert pm.policy_hash() == first


def test_a_partial_packet_run_cannot_promote(artifacts):
    """1-of-25 with status PASS returned PROMOTABLE with no blockers at all.
    packetsExpected was transmitted, displayed, and never read."""

    gate = pm.evaluate(
        kernel_gate={
            "status": "PASS", "packetsExpected": 25, "packetsPassed": 1,
            **{k: 0 for k in pm.KERNEL_COUNTERS},
        },
        **artifacts,
    )
    assert not gate.promotable
    assert "PACKETS_INCOMPLETE" in {b.code for b in gate.blockers}


def test_a_null_counter_does_not_promote(artifacts):
    """`elif kernel_gate[field]:` was a truthiness test one line under a comment
    saying absence is a blocker, not a zero. None is falsey."""

    counters = {k: 0 for k in pm.KERNEL_COUNTERS}
    counters["factMismatches"] = None
    gate = pm.evaluate(
        kernel_gate={"status": "PASS", "packetsExpected": 25, "packetsPassed": 25, **counters},
        **artifacts,
    )
    assert not gate.promotable


def test_an_unacceptable_reduction_can_never_promote(kernel_gate, bars):
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
        events=events,
        bars=bars,
        packets=None,
        journal=None,
    )
    assert not gate.promotable
    assert "LEDGER_UNACCEPTABLE" in {b.code for b in gate.blockers}
    assert "AUTHORITY_NOT_RUN" in {b.code for b in gate.blockers}


def test_the_gate_recomputes_instead_of_accepting_receipt_testimony(
    receipt, artifacts, kernel_gate
):
    """Changing every favorable counter on a copy cannot enter the gate."""

    forged = receipt.model_copy(
        update={name: 0 for name in pm.LEDGER_COUNTERS}
    )
    assert forged is not receipt
    gate = _evaluate(
        artifacts,
        kernel_gate,
    )
    assert not gate.promotable
    assert {blocker.code for blocker in gate.blockers} == {
        "SYNTHETIC_CORPUS",
        "MARKET_DATA_CONFORMANCE_NOT_RUN",
        "ORACLE_NOT_INDEPENDENT_ENOUGH",
        "SPLIT_DISCIPLINE_NOT_PROVEN",
    }
