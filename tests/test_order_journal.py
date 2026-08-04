"""Scenario attacks against the independent order-lifecycle stream."""

from __future__ import annotations

import pytest

from app import ledger as lg
from app import proof_kernel as pk
from app.order_journal import OrderEventType, build_order_journal


@pytest.fixture(scope="module")
def artifacts():
    bars = pk.fixture_bars()
    packets = list(pk.packets(25, bars))
    ledger = lg.build_ledger(bars, packets)
    journal = build_order_journal(bars, packets)
    return bars, packets, ledger, journal


def test_reviewer_gets_a_complete_independent_lifecycle(artifacts):
    bars, packets, ledger, journal = artifacts
    receipt = lg.curve_receipt(ledger, bars, packets, journal)

    assert {event.type for event in journal} == {
        OrderEventType.ORDER_CREATED,
        OrderEventType.ORDER_APPROVED,
        OrderEventType.ORDER_SUBMITTED,
        OrderEventType.ORDER_FILLED,
    }
    assert receipt.cross_stream_reconciliation == "PASS"
    assert receipt.order_journal_receipt is not None
    assert receipt.order_journal_receipt.total_mismatches == 0
    assert receipt.order_journal_receipt.filled_orders == sum(
        event.type is lg.EventType.ORDER_FILL for event in ledger
    )


def test_coherent_ledger_quantity_forgery_disagrees_with_journal(artifacts):
    bars, packets, ledger, journal = artifacts
    forged = [event.model_copy(deep=True) for event in ledger]
    fill = next(event for event in forged if event.type is lg.EventType.ORDER_FILL)
    fill.qty += 5
    fill.position_delta = fill.qty if fill.side is lg.Side.BUY else -fill.qty
    fill.cash_delta_micros = -fill.position_delta * fill.price_micros

    receipt = lg.curve_receipt(forged, bars, packets, journal)
    cross = receipt.order_journal_receipt
    assert cross is not None
    assert cross.quantity_mismatches == 1
    assert cross.cash_mismatches == 1
    assert receipt.cross_stream_reconciliation == "FAIL"


def test_missing_fill_cannot_hide_inside_a_clean_remaining_journal(artifacts):
    bars, packets, ledger, journal = artifacts
    damaged = [event.model_copy(deep=True) for event in journal]
    damaged.pop(next(i for i, event in enumerate(damaged) if event.type is OrderEventType.ORDER_FILLED))
    for seq, event in enumerate(damaged):
        event.seq = seq

    receipt = lg.curve_receipt(ledger, bars, packets, damaged)
    cross = receipt.order_journal_receipt
    assert cross is not None
    assert cross.lifecycle_mismatches == 1
    assert cross.missing_journal_fills == 1
    assert receipt.cross_stream_reconciliation == "FAIL"


def test_fee_and_mark_mutations_are_cross_stream_failures(artifacts):
    bars, packets, ledger, journal = artifacts
    damaged = [event.model_copy(deep=True) for event in ledger]
    fee = next(event for event in damaged if event.type is lg.EventType.FEE)
    fee.cash_delta_micros = -1
    mark = next(event for event in damaged if event.type is lg.EventType.MARK)
    mark.price_micros += 1

    receipt = lg.curve_receipt(damaged, bars, packets, journal)
    cross = receipt.order_journal_receipt
    assert cross is not None
    assert cross.fee_mismatches == 2
    assert cross.mark_mismatches == 2
    assert receipt.cross_stream_reconciliation == "FAIL"


def test_order_ids_are_one_to_one_across_a_sustained_run(artifacts):
    _, _, ledger, journal = artifacts
    ledger_ids = [
        event.order_id for event in ledger if event.type is lg.EventType.ORDER_FILL
    ]
    journal_ids = [
        event.order_id for event in journal if event.type is OrderEventType.ORDER_FILLED
    ]
    assert len(ledger_ids) > 20
    assert len(ledger_ids) == len(set(ledger_ids))
    assert set(ledger_ids) == set(journal_ids)


def test_co_mutating_a_shared_order_id_is_not_independent_authority(artifacts):
    bars, packets, ledger, journal = artifacts
    changed_ledger = [event.model_copy(deep=True) for event in ledger]
    changed_journal = [event.model_copy(deep=True) for event in journal]
    old = next(event.order_id for event in changed_journal if event.type is OrderEventType.ORDER_FILLED)
    new = f"{old}-forged"
    for event in changed_journal:
        if event.order_id == old:
            event.order_id = new
    for event in changed_ledger:
        if event.order_id == old:
            event.order_id = new

    receipt = lg.curve_receipt(changed_ledger, bars, packets, changed_journal)
    cross = receipt.order_journal_receipt
    assert cross is not None
    assert cross.lifecycle_mismatches > 0
    assert cross.order_link_mismatches > 0
    assert receipt.cross_stream_reconciliation == "FAIL"


@pytest.mark.parametrize("kind", ["fee", "mark"])
def test_surplus_ledger_rows_cannot_disappear_in_a_mapping(artifacts, kind):
    bars, packets, ledger, journal = artifacts
    changed = [event.model_copy(deep=True) for event in ledger]
    event_type = lg.EventType.FEE if kind == "fee" else lg.EventType.MARK
    original = next(event for event in changed if event.type is event_type)
    duplicate = original.model_copy(
        update={"event_id": f"{original.event_id}-extra", "seq": len(changed)}
    )
    changed.append(duplicate)

    receipt = lg.curve_receipt(changed, bars, packets, journal)
    cross = receipt.order_journal_receipt
    assert cross is not None
    counter = cross.fee_mismatches if kind == "fee" else cross.mark_mismatches
    assert counter == 1
    assert receipt.cross_stream_reconciliation == "FAIL"


@pytest.mark.parametrize(
    "field,value",
    [
        ("requested_qty", 999),
        ("packet_id", "pkt-forged"),
        ("execution_policy_hash", "sha256:forged"),
    ],
)
def test_non_fill_lifecycle_testimony_is_verified(artifacts, field, value):
    bars, packets, ledger, journal = artifacts
    changed = [event.model_copy(deep=True) for event in journal]
    row = next(event for event in changed if event.type is OrderEventType.ORDER_APPROVED)
    setattr(row, field, value)

    receipt = lg.curve_receipt(ledger, bars, packets, changed)
    cross = receipt.order_journal_receipt
    assert cross is not None
    assert cross.lifecycle_field_mismatches == 1
    assert receipt.cross_stream_reconciliation == "FAIL"


def test_global_journal_sequence_is_dense_and_zero_based(artifacts):
    bars, packets, ledger, journal = artifacts
    changed = [event.model_copy(update={"seq": event.seq + 1}) for event in journal]
    receipt = lg.curve_receipt(ledger, bars, packets, changed)
    assert receipt.order_journal_receipt is not None
    assert receipt.order_journal_receipt.journal_sequence_mismatches == len(changed)
    assert receipt.cross_stream_reconciliation == "FAIL"


@pytest.mark.parametrize(
    "event_type", [lg.EventType.ORDER_FILL, lg.EventType.FEE, lg.EventType.MARK]
)
@pytest.mark.parametrize("mutation", ["event_id", "receipt"])
def test_ledger_identity_and_provenance_are_not_free_testimony(
    artifacts, event_type, mutation
):
    bars, packets, ledger, journal = artifacts
    changed = [event.model_copy(deep=True) for event in ledger]
    event = next(item for item in changed if item.type is event_type)
    if mutation == "event_id":
        event.event_id = f"{event.event_id}-fabricated"
    else:
        event.receipt = lg.FactReceipt(
            probe_id="fabricated", source="fabricated", retrieved_at_utc=event.at_utc
        )

    receipt = lg.curve_receipt(changed, bars, packets, journal)
    cross = receipt.order_journal_receipt
    assert cross is not None
    named = (
        cross.ledger_event_id_mismatches
        if mutation == "event_id"
        else cross.ledger_receipt_mismatches
    )
    assert named == 1
    assert receipt.cross_stream_reconciliation == "FAIL"


def test_unhashable_post_parse_journal_id_blocks_instead_of_crashing(artifacts):
    bars, packets, ledger, journal = artifacts
    changed = [event.model_copy(deep=True) for event in journal]
    fill = next(event for event in changed if event.type is OrderEventType.ORDER_FILLED)
    object.__setattr__(fill, "order_id", [])

    receipt = lg.curve_receipt(ledger, bars, packets, changed)
    assert receipt.order_journal_receipt is not None
    assert receipt.order_journal_receipt.malformed_journal_events == 1
    assert receipt.cross_stream_reconciliation == "FAIL"
