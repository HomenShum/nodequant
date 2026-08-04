"""Independent order-lifecycle authority and cross-stream reconciliation.

The financial ledger and this journal are produced from the same frozen packets
and bars by separate implementations. Neither consumes the other, so a mutation
to one stream is detectable instead of being copied mechanically to both sides.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Iterable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, computed_field

from app.decision_packet import DecisionPacket, FactReceipt, canonical_hash
from app.proof_kernel import Bar

MICROS = 1_000_000


class OrderEventType(str, Enum):
    ORDER_CREATED = "ORDER_CREATED"
    ORDER_APPROVED = "ORDER_APPROVED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_PARTIALLY_FILLED = "ORDER_PARTIALLY_FILLED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    ORDER_REJECTED = "ORDER_REJECTED"
    ORDER_EXPIRED = "ORDER_EXPIRED"


class JournalEvent(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    seq: int
    event_id: Annotated[str, Field(min_length=1, max_length=200)]
    type: OrderEventType
    at_utc: datetime
    order_id: Annotated[str, Field(min_length=1, max_length=180)]
    packet_id: Annotated[str, Field(min_length=1, max_length=160)]
    side: str
    requested_qty: int
    filled_qty: int = 0
    reference_price_micros: int = 0
    execution_price_micros: int = 0
    spread_cost_micros: int = 0
    slippage_cost_micros: int = 0
    commission_micros: int = 0
    cash_delta_micros: int = 0
    position_delta: int = 0
    execution_policy_hash: str
    receipt: FactReceipt


class OrderJournalReceipt(BaseModel):
    journal_root_hash: str
    journal_event_count: int
    filled_orders: int
    lifecycle_mismatches: int
    malformed_journal_events: int
    lifecycle_field_mismatches: int
    journal_sequence_mismatches: int
    duplicate_journal_event_ids: int
    duplicate_order_fills: int
    duplicate_ledger_fills: int
    ledger_event_id_mismatches: int
    ledger_receipt_mismatches: int
    missing_journal_fills: int
    orphan_journal_fills: int
    order_link_mismatches: int
    quantity_mismatches: int
    timestamp_mismatches: int
    reference_price_mismatches: int
    execution_price_mismatches: int
    spread_cost_mismatches: int
    slippage_cost_mismatches: int
    policy_hash_mismatches: int
    cash_mismatches: int
    position_mismatches: int
    fee_mismatches: int
    mark_mismatches: int

    @computed_field
    @property
    def total_mismatches(self) -> int:
        excluded = {"journal_event_count", "filled_orders", "total_mismatches"}
        return sum(
            value
            for name, value in self.model_dump(exclude={"total_mismatches"}).items()
            if name not in excluded and isinstance(value, int)
        )

    @property
    def acceptable(self) -> bool:
        return self.journal_event_count > 0 and self.filled_orders > 0 and self.total_mismatches == 0


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def journal_root_hash(events: list[JournalEvent]) -> str:
    return _hash([event.model_dump(mode="json") for event in events])


def _to_micros(value: float) -> int:
    return int(round(value * MICROS))


def _round_half_even(numerator: int) -> int:
    quotient, remainder = divmod(numerator, MICROS)
    if remainder * 2 > MICROS or (remainder * 2 == MICROS and quotient % 2):
        quotient += 1
    return max(quotient, 1)


def build_order_journal(
    bars: list[Bar], packets: Iterable[DecisionPacket]
) -> list[JournalEvent]:
    """Produce lifecycle events without reading the financial ledger."""

    by_time = {bar.ts: bar for bar in bars}
    out: list[JournalEvent] = []
    busy_through = -1

    def append(
        event_type: OrderEventType,
        at_utc: datetime,
        order_id: str,
        packet: DecisionPacket,
        side: str,
        receipt: FactReceipt,
        **values: Any,
    ) -> None:
        out.append(
            JournalEvent(
                seq=len(out),
                event_id=f"journal-{order_id}-{event_type.value.lower()}",
                type=event_type,
                at_utc=at_utc,
                order_id=order_id,
                packet_id=packet.packet_id,
                side=side,
                requested_qty=packet.strategy.position_qty,
                execution_policy_hash=canonical_hash(packet.strategy.execution_cost_policy),
                receipt=receipt,
                **values,
            )
        )

    for packet in sorted(packets, key=lambda item: item.signal.decision_at_utc):
        entry = by_time.get(packet.signal.earliest_execution_at_utc)
        if entry is None or entry.index <= busy_through:
            continue
        exit_index = entry.index + packet.strategy.hold_bars
        if exit_index >= len(bars):
            continue
        exit_bar = bars[exit_index]
        for side, fill_bar in (("buy", entry), ("sell", exit_bar)):
            order_id = f"ord-{packet.packet_id}-{side}"
            created_at = packet.signal.decision_at_utc if side == "buy" else entry.ts
            authority_receipt = packet.facts[0].receipt or fill_bar.receipt
            append(OrderEventType.ORDER_CREATED, created_at, order_id, packet, side, authority_receipt)
            append(OrderEventType.ORDER_APPROVED, created_at, order_id, packet, side, authority_receipt)
            append(OrderEventType.ORDER_SUBMITTED, fill_bar.ts, order_id, packet, side, fill_bar.receipt)

            policy = packet.strategy.execution_cost_policy
            reference = _to_micros(fill_bar.close)
            spread_unit = _round_half_even(reference * policy.half_spread_ppm)
            slippage_unit = _round_half_even(reference * policy.slippage_ppm)
            adverse = spread_unit + slippage_unit
            execution = reference + adverse if side == "buy" else reference - adverse
            position = packet.strategy.position_qty if side == "buy" else -packet.strategy.position_qty
            append(
                OrderEventType.ORDER_FILLED,
                fill_bar.ts,
                order_id,
                packet,
                side,
                fill_bar.receipt,
                filled_qty=packet.strategy.position_qty,
                reference_price_micros=reference,
                execution_price_micros=execution,
                spread_cost_micros=spread_unit * packet.strategy.position_qty,
                slippage_cost_micros=slippage_unit * packet.strategy.position_qty,
                commission_micros=policy.commission_per_fill_micros,
                cash_delta_micros=-position * execution,
                position_delta=position,
            )
        busy_through = exit_index
    return out


def reconcile(
    journal: list[JournalEvent],
    ledger_events: list[Any],
    bars: list[Bar],
    packets: list[DecisionPacket],
) -> OrderJournalReceipt:
    """Compare both streams against packet, bar, and cardinality authority."""

    normalized_journal: list[JournalEvent] = []
    malformed_journal = 0
    for raw_event in journal:
        try:
            payload = dict(raw_event.__dict__) if isinstance(raw_event, BaseModel) else raw_event
            normalized_journal.append(JournalEvent.model_validate(payload))
        except (AttributeError, TypeError, ValidationError):
            malformed_journal += 1
    journal = normalized_journal

    bars_by_time = {bar.ts: bar for bar in bars}
    expected_rows: list[dict[str, Any]] = []
    expected_fill_by_order: dict[str, dict[str, Any]] = {}
    busy_through = -1

    def add_expected(
        event_type: OrderEventType,
        at_utc: datetime,
        order_id: str,
        packet: DecisionPacket,
        side: str,
        receipt: FactReceipt,
        **values: Any,
    ) -> None:
        row = {
            "seq": len(expected_rows),
            "event_id": f"journal-{order_id}-{event_type.value.lower()}",
            "type": event_type,
            "at_utc": at_utc,
            "order_id": order_id,
            "packet_id": packet.packet_id,
            "side": side,
            "requested_qty": packet.strategy.position_qty,
            "filled_qty": 0,
            "reference_price_micros": 0,
            "execution_price_micros": 0,
            "spread_cost_micros": 0,
            "slippage_cost_micros": 0,
            "commission_micros": 0,
            "cash_delta_micros": 0,
            "position_delta": 0,
            "execution_policy_hash": canonical_hash(packet.strategy.execution_cost_policy),
            "receipt_hash": _hash(receipt.model_dump(mode="json")),
            **values,
        }
        expected_rows.append(row)
        if event_type is OrderEventType.ORDER_FILLED:
            expected_fill_by_order[order_id] = row

    # This is intentionally a second construction, not a call to
    # build_order_journal: lifecycle testimony must be checked by another path.
    for packet in sorted(packets, key=lambda item: item.signal.decision_at_utc):
        entry = bars_by_time.get(packet.signal.earliest_execution_at_utc)
        if entry is None or entry.index <= busy_through:
            continue
        exit_index = entry.index + packet.strategy.hold_bars
        if exit_index >= len(bars):
            continue
        exit_bar = bars[exit_index]
        for side, fill_bar in (("buy", entry), ("sell", exit_bar)):
            order_id = f"ord-{packet.packet_id}-{side}"
            created_at = packet.signal.decision_at_utc if side == "buy" else entry.ts
            authority_receipt = packet.facts[0].receipt or fill_bar.receipt
            add_expected(OrderEventType.ORDER_CREATED, created_at, order_id, packet, side, authority_receipt)
            add_expected(OrderEventType.ORDER_APPROVED, created_at, order_id, packet, side, authority_receipt)
            add_expected(OrderEventType.ORDER_SUBMITTED, fill_bar.ts, order_id, packet, side, fill_bar.receipt)

            policy = packet.strategy.execution_cost_policy
            reference = _to_micros(fill_bar.close)
            spread_unit = _round_half_even(reference * policy.half_spread_ppm)
            slippage_unit = _round_half_even(reference * policy.slippage_ppm)
            execution = reference + spread_unit + slippage_unit
            if side == "sell":
                execution = reference - spread_unit - slippage_unit
            position = packet.strategy.position_qty if side == "buy" else -packet.strategy.position_qty
            add_expected(
                OrderEventType.ORDER_FILLED,
                fill_bar.ts,
                order_id,
                packet,
                side,
                fill_bar.receipt,
                filled_qty=packet.strategy.position_qty,
                reference_price_micros=reference,
                execution_price_micros=execution,
                spread_cost_micros=spread_unit * packet.strategy.position_qty,
                slippage_cost_micros=slippage_unit * packet.strategy.position_qty,
                commission_micros=policy.commission_per_fill_micros,
                cash_delta_micros=-position * execution,
                position_delta=position,
            )
        busy_through = exit_index

    expected_keys = Counter((row["order_id"], row["type"]) for row in expected_rows)
    actual_keys = Counter((event.order_id, event.type) for event in journal)
    lifecycle_bad = sum((expected_keys - actual_keys).values()) + sum(
        (actual_keys - expected_keys).values()
    )
    expected_by_key = {(row["order_id"], row["type"]): row for row in expected_rows}
    lifecycle_fields = 0
    for event in journal:
        expected = expected_by_key.get((event.order_id, event.type))
        if expected is None:
            continue
        actual = event.model_dump()
        actual["receipt_hash"] = _hash(event.receipt.model_dump(mode="json"))
        actual.pop("receipt")
        lifecycle_fields += actual != expected

    fills = [event for event in journal if event.type is OrderEventType.ORDER_FILLED]
    duplicate_fills = len(fills) - len({event.order_id for event in fills})
    journal_by_order = {event.order_id: event for event in fills}
    ledger_fills = [event for event in ledger_events if event.type.value == "ORDER_FILL"]
    duplicate_ledger_fills = len(ledger_fills) - len(
        {event.order_id for event in ledger_fills if event.order_id}
    )
    ledger_by_order = {event.order_id: event for event in ledger_fills if event.order_id}

    missing = len(set(ledger_by_order) - set(journal_by_order))
    orphan = len(set(journal_by_order) - set(ledger_by_order))
    order_link = 0
    for event in ledger_fills:
        canonical = (
            f"ord-{event.packet_id}-{event.side.value}"
            if event.packet_id and event.side is not None
            else None
        )
        order_link += event.order_id != canonical
    quantity = timestamps = references = executions = spreads = slippages = policies = 0
    cash = positions = ledger_ids = ledger_receipts = 0
    for order_id in set(journal_by_order) & set(ledger_by_order):
        expected = journal_by_order[order_id]
        actual = ledger_by_order[order_id]
        quantity += actual.qty != expected.filled_qty
        timestamps += actual.at_utc != expected.at_utc
        references += actual.reference_price_micros != expected.reference_price_micros
        executions += actual.price_micros != expected.execution_price_micros
        spreads += actual.spread_cost_micros != expected.spread_cost_micros
        slippages += actual.slippage_cost_micros != expected.slippage_cost_micros
        policies += actual.execution_policy_hash != expected.execution_policy_hash
        cash += actual.cash_delta_micros != expected.cash_delta_micros
        positions += actual.position_delta != expected.position_delta
        canonical_row = expected_by_key.get((order_id, OrderEventType.ORDER_FILLED))
        if canonical_row is None:
            ledger_ids += 1
            ledger_receipts += 1
        else:
            ledger_ids += actual.event_id != (
                f"evt-fill-{canonical_row['packet_id']}-{canonical_row['side']}"
            )
            ledger_receipts += (
                _hash(actual.receipt.model_dump(mode="json"))
                != canonical_row["receipt_hash"]
            )

    expected_fees = Counter(
        (
            order_id,
            row["at_utc"],
            -row["commission_micros"],
            f"evt-fee-{row['packet_id']}-{row['side']}",
            row["receipt_hash"],
        )
        for order_id, row in expected_fill_by_order.items()
    )
    actual_fee_events = [event for event in ledger_events if event.type.value == "FEE"]
    for event in actual_fee_events:
        row = expected_fill_by_order.get(event.order_id or "")
        if row is not None:
            ledger_ids += event.event_id != f"evt-fee-{row['packet_id']}-{row['side']}"
            ledger_receipts += (
                _hash(event.receipt.model_dump(mode="json")) != row["receipt_hash"]
            )
    actual_fees = Counter(
        (
            event.order_id,
            event.at_utc,
            event.cash_delta_micros,
            event.event_id,
            _hash(event.receipt.model_dump(mode="json")),
        )
        for event in actual_fee_events
    )
    fees = sum((expected_fees - actual_fees).values()) + sum(
        (actual_fees - expected_fees).values()
    )
    expected_order_ids = set(expected_fill_by_order)
    order_link += sum(event.order_id not in expected_order_ids for event in actual_fee_events)

    expected_marks: Counter[tuple[str, datetime, int, str, str]] = Counter()
    expected_fills_by_packet: dict[str, list[dict[str, Any]]] = {}
    for row in expected_fill_by_order.values():
        expected_fills_by_packet.setdefault(row["packet_id"], []).append(row)
    for packet_id, episode in expected_fills_by_packet.items():
        buy = next(row for row in episode if row["side"] == "buy")
        sell = next(row for row in episode if row["side"] == "sell")
        for bar in bars:
            if buy["at_utc"] < bar.ts <= sell["at_utc"]:
                expected_marks[
                    (
                        packet_id,
                        bar.ts,
                        _to_micros(bar.close),
                        f"evt-mark-{packet_id}-{bar.index}",
                        _hash(bar.receipt.model_dump(mode="json")),
                    )
                ] += 1
    actual_marks = Counter(
        (
            event.packet_id or "",
            event.at_utc,
            event.price_micros,
            event.event_id,
            _hash(event.receipt.model_dump(mode="json")),
        )
        for event in ledger_events
        if event.type.value == "MARK"
    )
    for event in (item for item in ledger_events if item.type.value == "MARK"):
        bar = bars_by_time.get(event.at_utc)
        if bar is not None and event.packet_id in expected_fills_by_packet:
            ledger_ids += event.event_id != f"evt-mark-{event.packet_id}-{bar.index}"
            ledger_receipts += (
                _hash(event.receipt.model_dump(mode="json"))
                != _hash(bar.receipt.model_dump(mode="json"))
            )
    marks = sum((expected_marks - actual_marks).values()) + sum(
        (actual_marks - expected_marks).values()
    )

    ids = [event.event_id for event in journal]
    return OrderJournalReceipt(
        journal_root_hash=journal_root_hash(journal),
        journal_event_count=len(journal),
        filled_orders=len(fills),
        lifecycle_mismatches=lifecycle_bad,
        malformed_journal_events=malformed_journal,
        lifecycle_field_mismatches=lifecycle_fields,
        journal_sequence_mismatches=sum(event.seq != i for i, event in enumerate(journal)),
        duplicate_journal_event_ids=len(ids) - len(set(ids)),
        duplicate_order_fills=duplicate_fills,
        duplicate_ledger_fills=duplicate_ledger_fills,
        ledger_event_id_mismatches=ledger_ids,
        ledger_receipt_mismatches=ledger_receipts,
        missing_journal_fills=missing,
        orphan_journal_fills=orphan,
        order_link_mismatches=order_link,
        quantity_mismatches=quantity,
        timestamp_mismatches=timestamps,
        reference_price_mismatches=references,
        execution_price_mismatches=executions,
        spread_cost_mismatches=spreads,
        slippage_cost_mismatches=slippages,
        policy_hash_mismatches=policies,
        cash_mismatches=cash,
        position_mismatches=positions,
        fee_mismatches=fees,
        mark_mismatches=marks,
    )
