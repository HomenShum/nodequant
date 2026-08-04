"""The ledger, and the equity curve reduced from it.

This replaces a float-based fills list. The change matters for one reason: with
floats, `0.1 + 0.2 != 0.3` becomes part of the financial truth, and every
equality check downstream needs a tolerance. A tolerance on money is a place for
a discrepancy to hide. Everything here is **integer microdollars**, so equality
is equality and the accounting identity holds exactly or not at all.

The shape follows from the authority law. An equity point is not a number the
backtest asserts; it is a reduction over an append-only ledger of typed events,
each of which carries a receipt. A reader who disbelieves the curve replays the
ledger.

    bars -> packets -> ledger events -> equity curve -> summary

Two things make the curve checkable rather than merely computed:

- **Per-point lineage.** Each point records the event sequence it was computed
  through and a hash of the ledger prefix up to that event. A point cannot be
  moved without moving a hash.
- **One reduction receipt** covering the whole artifact, not a receipt per
  point. Per-point receipts would be theatre: the risk is not that one point is
  wrong in isolation, it is that the whole reduction is wrong the same way
  everywhere, which only a second reducer over the same ledger can catch.

The accounting identity is checked at EVERY point, not just at the end:

    equity = cash + marked position value

Ending equity matching is a much weaker claim -- two reducers can agree on the
final number while disagreeing about the path, and the path is what a drawdown
is measured from.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Annotated, Iterable, Literal

from pydantic import BaseModel, Field

from app.decision_packet import DecisionPacket, FactReceipt, canonical_hash
from app.order_journal import JournalEvent, OrderJournalReceipt, reconcile
from app.proof_kernel import Bar, bars_hash

MICROS = 1_000_000
STARTING_CAPITAL_MICROS = 100_000 * MICROS
# $1.00 per fill. The first value here was $0.0001 -- non-zero, so the accounting
# tests passed, but it rendered as "$0.00" in the review panel. A real number
# displayed as zero is the precise failure this project exists to prevent, and a
# formatter tweak would have hidden it rather than fixed it. A commission that
# rounds away is also not a commission: costs are the difference between a
# strategy that looks profitable and one that is.
FEE_MICROS = 1 * MICROS


def to_micros(value: float) -> int:
    """Round-half-even into integer microdollars. One place, so a price cannot
    enter the ledger through a path that rounds differently."""

    return int(round(value * MICROS))


class EventType(str, Enum):
    INITIAL_CAPITAL = "INITIAL_CAPITAL"
    ORDER_FILL = "ORDER_FILL"
    FEE = "FEE"
    MARK = "MARK"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class LedgerEvent(BaseModel):
    """One append-only entry. `seq` is dense and gap-free by construction."""

    seq: int
    event_id: Annotated[str, Field(min_length=1, max_length=160)]
    type: EventType
    at_utc: datetime
    packet_id: str | None = None
    order_id: str | None = None
    side: Side | None = None
    qty: int = 0
    reference_price_micros: int = 0
    price_micros: int = 0
    spread_cost_micros: int = 0
    slippage_cost_micros: int = 0
    execution_policy_hash: str | None = None
    cash_delta_micros: int = 0
    position_delta: int = 0
    receipt: FactReceipt


class EquityPoint(BaseModel):
    at_utc: datetime
    equity_micros: int
    cash_micros: int
    position_value_micros: int
    realised_pnl_micros: int
    cumulative_fees_micros: int
    cumulative_spread_cost_micros: int
    cumulative_slippage_cost_micros: int
    through_event_seq: int
    ledger_prefix_hash: str


class CurveReductionReceipt(BaseModel):
    """What the whole reduction is worth, in one record."""

    ledger_root_hash: str
    bars_hash: str | None = None
    event_count: int
    primary_curve_hash: str
    oracle_curve_hash: str
    oracle_independence: str
    curve_point_count: int
    full_curve_matched: bool
    curve_point_mismatches: int
    sequence_gaps: int
    duplicate_event_ids: int
    inconsistent_events: int
    negative_position_points: int
    non_monotonic_timestamps: int
    # None means the check did not run -- never silently zero. An adversarial
    # pass promoted a ledger with $1,000,000 of fabricated equity because a
    # missing counter defaulted to 0 and read as "clean".
    price_mismatches: int | None = None
    unpriced_events: int | None = None
    spread_cost_micros: int | None = None
    slippage_cost_micros: int | None = None
    spread_model_mismatches: int | None = None
    slippage_model_mismatches: int | None = None
    execution_price_mismatches: int | None = None
    execution_policy_mismatches: int | None = None

    # Capital and solvency. A second INITIAL_CAPITAL conjured $1,000,000 mid-run,
    # passed every counter, and IMPROVED reported max drawdown by $264 because it
    # reset the running peak -- fabrication that makes the risk number look better.
    capital_event_violations: int = 0
    uncosted_fills: int = 0
    negative_cash_points: int = 0
    negative_equity_points: int = 0
    # Authority. None means the packets were never supplied, so the check did
    # not run -- and an unrun check is never clean.
    unauthorized_fills: int | None = None
    early_entries: int | None = None
    mistimed_exits: int | None = None
    missing_authorized_fills: int | None = None
    packet_hashes: list[str] | None = None

    # There is no independent order-lifecycle stream yet. Say that explicitly;
    # absence is not zero and an internal ledger reduction cannot certify
    # execution authenticity.
    cross_stream_reconciliation: Literal["PASS", "FAIL", "NOT_RUN"] = "NOT_RUN"
    order_journal_receipt: OrderJournalReceipt | None = None

    @property
    def bars_checked(self) -> bool:
        return self.price_mismatches is not None

    @property
    def packets_checked(self) -> bool:
        return self.unauthorized_fills is not None

    @property
    def execution_costs_checked(self) -> bool:
        return self.spread_model_mismatches is not None

    @property
    def acceptable(self) -> bool:
        """Acceptable requires the bar check to have RUN.

        `max_accounting_residual_micros` used to be here and was removed: the
        reducer computes `equity = cash + position * mark` and the residual was
        `abs(equity - (cash + position_value))` from the same three locals, so it
        was zero by construction. 200 fully randomised ledgers produced a maximum
        residual of 0. Displaying it as evidence was the tautology this project
        is supposed to catch.
        """

        return (
            self.full_curve_matched
            and self.curve_point_mismatches == 0
            and self.sequence_gaps == 0
            and self.duplicate_event_ids == 0
            and self.inconsistent_events == 0
            and self.negative_position_points == 0
            and self.non_monotonic_timestamps == 0
            and self.event_count > 0
            and self.price_mismatches == 0
            and self.unpriced_events == 0
            and self.spread_cost_micros is not None
            and self.spread_cost_micros > 0
            and self.slippage_cost_micros is not None
            and self.slippage_cost_micros > 0
            and self.spread_model_mismatches == 0
            and self.slippage_model_mismatches == 0
            and self.execution_price_mismatches == 0
            and self.execution_policy_mismatches == 0
            and self.capital_event_violations == 0
            and self.uncosted_fills == 0
            and self.negative_cash_points == 0
            and self.negative_equity_points == 0
            and self.unauthorized_fills == 0
            and self.early_entries == 0
            and self.mistimed_exits == 0
            and self.missing_authorized_fills == 0
        )


def posting(event: LedgerEvent) -> tuple[int, int]:
    """The canonical (position_delta, cash_delta) DERIVED from side, qty, price.

    `qty` and `position_delta` were two representations of one quantity driving
    two different authoritative calculations: the position reducer consumed
    `position_delta` while the cost-basis reducer consumed `qty`. That is the
    same structural family as the two bugs already fixed here -- the strategy
    predicate versus the packet's rules, and `Rule.result` versus its own
    operands. Duplicate authority, one of which can drift.

    So the fields on the wire are now CLAIMED postings, kept for auditability.
    Everything authoritative derives from this function, and `_inconsistent`
    rejects any event whose claim disagrees with it. Derivation beats
    validation: a fabricated `position_delta` is both ignored and flagged,
    rather than only flagged.
    """

    if event.type is not EventType.ORDER_FILL:
        return 0, event.cash_delta_micros
    signed = event.qty if event.side is Side.BUY else -event.qty
    return signed, -signed * event.price_micros


def _inconsistent(event: LedgerEvent) -> bool:
    """Is this event internally impossible?

    This check exists because the curve-versus-oracle comparison cannot find
    fabrication. Both reducers consume the SAME events, so an event that invents
    five shares with no cash movement is reduced identically by both and their
    hashes still match. The oracle catches reducer drift; only conservation
    catches a fabricated event.

    Found by a test that injected exactly that and watched the receipt come back
    acceptable.
    """

    if event.type is EventType.ORDER_FILL:
        # Buying moves position up and cash down by the same notional.
        return (
            event.side not in (Side.BUY, Side.SELL)
            or event.position_delta == 0
            or event.qty <= 0
            or event.reference_price_micros <= 0
            or event.spread_cost_micros < 0
            or event.slippage_cost_micros < 0
            or abs(event.position_delta) != event.qty
            or event.cash_delta_micros != -event.position_delta * event.price_micros
        )
    if event.type is EventType.FEE:
        # The AMOUNT matters, not just the sign. A fee of -1 microdollar
        # satisfied "is it negative" while charging nothing -- costs are the
        # difference between a strategy that looks profitable and one that is.
        return (
            event.position_delta != 0
            or event.cash_delta_micros >= 0
            or -event.cash_delta_micros < FEE_MICROS
        )
    if event.type is EventType.MARK:
        return event.position_delta != 0 or event.cash_delta_micros != 0
    if event.type is EventType.INITIAL_CAPITAL:
        return event.position_delta != 0 or event.cash_delta_micros <= 0
    return False


# --------------------------------------------------------------------------
# bars + packets -> ledger
# --------------------------------------------------------------------------


def _round_half_even_ratio(numerator: int, denominator: int = MICROS) -> int:
    """Integer-only round-half-even for a positive fixed-point ratio."""

    quotient, remainder = divmod(numerator, denominator)
    if remainder * 2 > denominator or (
        remainder * 2 == denominator and quotient % 2
    ):
        quotient += 1
    # A positive policy component must remain adverse even for sub-cent assets.
    # Without this floor a $0.005 reference rounded both positive ppm costs to
    # zero and an aggregate-positive receipt still passed.
    return max(quotient, 1)


def build_ledger(
    bars: list[Bar], packets: Iterable[DecisionPacket]
) -> list[LedgerEvent]:
    """Emit the event stream. Entry is at the bar the packet declared executable.

    Executing at the signal bar's own close is the classic way a backtest invents
    profit, so the entry bar is looked up by `earliest_execution_at_utc` rather
    than computed from the signal index.
    """

    by_ts = {b.ts: b for b in bars}
    events: list[LedgerEvent] = []
    seq = 0

    def emit(**kw) -> None:
        nonlocal seq
        events.append(LedgerEvent(seq=seq, **kw))
        seq += 1

    origin = bars[0].ts
    base_receipt = FactReceipt(
        probe_id="ledger.genesis", source="fixture", retrieved_at_utc=origin
    )
    emit(
        event_id="evt-initial-capital",
        type=EventType.INITIAL_CAPITAL,
        at_utc=origin,
        cash_delta_micros=STARTING_CAPITAL_MICROS,
        receipt=base_receipt,
    )

    busy_until = -1
    for packet in sorted(packets, key=lambda p: p.signal.decision_at_utc):
        entry_bar = by_ts.get(packet.signal.earliest_execution_at_utc)
        if entry_bar is None or entry_bar.index <= busy_until:
            continue
        exit_index = entry_bar.index + packet.strategy.hold_bars
        if exit_index >= len(bars):
            continue
        exit_bar = bars[exit_index]

        def bar_receipt(bar: Bar) -> FactReceipt:
            return bar.receipt

        for side, bar in ((Side.BUY, entry_bar), (Side.SELL, exit_bar)):
            reference_price = to_micros(bar.close)
            qty = packet.strategy.position_qty
            signed = -1 if side is Side.BUY else 1
            receipt = bar_receipt(bar)
            cost_policy = packet.strategy.execution_cost_policy
            spread_per_unit = _round_half_even_ratio(
                reference_price * cost_policy.half_spread_ppm
            )
            slippage_per_unit = _round_half_even_ratio(
                reference_price * cost_policy.slippage_ppm
            )
            adverse_per_unit = spread_per_unit + slippage_per_unit
            execution_price = (
                reference_price + adverse_per_unit
                if side is Side.BUY
                else reference_price - adverse_per_unit
            )
            if side is Side.SELL:
                # Mark on EVERY bar the position is held, not only before the
                # exit. Marking at exit alone left the position valued at its
                # entry price for the whole hold, so the curve had no points
                # between trades and drawdown was measured from a sample that
                # skipped the excursion. Measured on this fixture: max drawdown
                # was understated by $2.52 (0.46%), and that error scales with
                # position size and holding period.
                for held in bars[entry_bar.index + 1 : exit_bar.index + 1]:
                    emit(
                        event_id=f"evt-mark-{packet.packet_id}-{held.index}",
                        type=EventType.MARK,
                        at_utc=held.ts,
                        packet_id=packet.packet_id,
                        price_micros=to_micros(held.close),
                        receipt=bar_receipt(held),
                    )
            emit(
                event_id=f"evt-fill-{packet.packet_id}-{side.value}",
                type=EventType.ORDER_FILL,
                at_utc=bar.ts,
                packet_id=packet.packet_id,
                order_id=f"ord-{packet.packet_id}-{side.value}",
                side=side,
                qty=qty,
                reference_price_micros=reference_price,
                price_micros=execution_price,
                spread_cost_micros=spread_per_unit * qty,
                slippage_cost_micros=slippage_per_unit * qty,
                execution_policy_hash=canonical_hash(cost_policy),
                cash_delta_micros=signed * qty * execution_price,
                position_delta=-signed * qty,
                receipt=receipt,
            )
            emit(
                event_id=f"evt-fee-{packet.packet_id}-{side.value}",
                type=EventType.FEE,
                at_utc=bar.ts,
                packet_id=packet.packet_id,
                order_id=f"ord-{packet.packet_id}-{side.value}",
                cash_delta_micros=-cost_policy.commission_per_fill_micros,
                receipt=receipt,
            )
        busy_until = exit_index
    return events


def ledger_root_hash(events: list[LedgerEvent]) -> str:
    return _hash([e.model_dump(mode="json") for e in events])


def _hash(payload) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# ledger -> curve
# --------------------------------------------------------------------------


def reduce_curve(events: list[LedgerEvent]) -> list[EquityPoint]:
    """Primary reduction. Walks events once, carrying running state."""

    points: list[EquityPoint] = []
    cash = position = fees = realised = spread_cost = slippage_cost = 0
    mark = 0
    cost_basis = 0
    prefix: list[dict] = []

    for event in events:
        prefix.append(event.model_dump(mode="json"))
        derived_position, derived_cash = posting(event)
        cash += derived_cash
        if event.type is EventType.FEE:
            fees += -event.cash_delta_micros
        if event.type is EventType.MARK and event.price_micros:
            mark = event.price_micros
        if event.type is EventType.ORDER_FILL:
            mark = event.reference_price_micros or event.price_micros
            spread_cost += event.spread_cost_micros
            slippage_cost += event.slippage_cost_micros
            if event.side is Side.BUY:
                cost_basis = event.price_micros
            else:
                realised += (event.price_micros - cost_basis) * event.qty
            position += derived_position
        points.append(
            EquityPoint(
                at_utc=event.at_utc,
                equity_micros=cash + position * mark,
                cash_micros=cash,
                position_value_micros=position * mark,
                realised_pnl_micros=realised,
                cumulative_fees_micros=fees,
                cumulative_spread_cost_micros=spread_cost,
                cumulative_slippage_cost_micros=slippage_cost,
                through_event_seq=event.seq,
                ledger_prefix_hash=_hash(prefix),
            )
        )
    return points


def _reduce_curve_oracle(events: list[LedgerEvent]) -> list[int]:
    """The same reduction by a different route: recompute each point from the
    whole prefix rather than carrying running state.

    O(n^2) and deliberately so. Carrying state is exactly where a reducer drifts,
    and a second implementation that also carries state would drift with it.
    """

    out: list[int] = []
    for i in range(len(events)):
        window = events[: i + 1]
        # Derived here rather than via `posting()`. Sharing that helper with the
        # primary reducer was a regression introduced while fixing the
        # duplicate-quantity bug: it made the two reducers agree BY
        # CONSTRUCTION about the one thing they exist to cross-check. An
        # external review caught it. Kept deliberately verbose so the two
        # derivations can diverge if either is wrong.
        cash = 0
        position = 0
        for e in window:
            if e.type is EventType.ORDER_FILL:
                units = e.qty if e.side is Side.BUY else -e.qty
                position += units
                cash -= units * e.price_micros
            else:
                cash += e.cash_delta_micros
        prices = [
            (e.price_micros if e.type is EventType.MARK else e.reference_price_micros or e.price_micros)
            for e in window
            if e.type in (EventType.ORDER_FILL, EventType.MARK) and e.price_micros
        ]
        mark = prices[-1] if prices else 0
        out.append(cash + position * mark)
    return out


def _bar_conformance(
    events: list[LedgerEvent], bars: list[Bar] | None
) -> tuple[int | None, int | None]:
    """Does every priced event quote the bar it claims to come from?

    This is the check whose absence let an adversarial pass fabricate a ledger
    worth $1,098,843 that passed every other test. `_inconsistent` validates an
    event against ITSELF -- cash moved by qty x price -- so re-deriving
    `cash_delta` after inventing a price satisfies it perfectly. Nothing resolved
    the receipt's `probe_id` back to the bars.

    Returns (mismatches, unpriced) or (None, None) when no bars were supplied,
    because a check that did not run must never report zero.
    """

    if bars is None:
        return None, None
    by_ts = {b.ts: b for b in bars}
    mismatches = unpriced = 0
    for event in events:
        if event.type not in (EventType.ORDER_FILL, EventType.MARK):
            continue
        bar = by_ts.get(event.at_utc)
        if bar is None:
            unpriced += 1
        elif (
            event.reference_price_micros
            if event.type is EventType.ORDER_FILL
            else event.price_micros
        ) != to_micros(bar.close):
            mismatches += 1
    return mismatches, unpriced


def _execution_cost_conformance(
    events: list[LedgerEvent],
    bars: list[Bar] | None,
    packets: list[DecisionPacket] | None,
) -> tuple[int | None, int | None, int | None, int | None, int | None, int | None]:
    """Recompute every modeled fill cost from frozen packet policy and bars.

    The ledger's claimed cost fields are not evidence for themselves.  This
    verifier independently resolves the packet and reference bar, recomputes
    the two adverse components, then checks the execution price and policy seal.
    """

    if bars is None or packets is None:
        return (None, None, None, None, None, None)
    by_ts = {bar.ts: bar for bar in bars}
    by_id = {packet.packet_id: packet for packet in packets}
    spread_total = slippage_total = 0
    spread_bad = slippage_bad = execution_bad = policy_bad = 0
    for event in events:
        if event.type is not EventType.ORDER_FILL:
            continue
        spread_total += event.spread_cost_micros
        slippage_total += event.slippage_cost_micros
        packet = by_id.get(event.packet_id or "")
        bar = by_ts.get(event.at_utc)
        if packet is None or bar is None or event.side not in (Side.BUY, Side.SELL):
            spread_bad += 1
            slippage_bad += 1
            execution_bad += 1
            policy_bad += 1
            continue
        policy = packet.strategy.execution_cost_policy
        reference = to_micros(bar.close)
        # Intentionally not shared with the producer helper: the receipt must be
        # able to catch a rounding regression in that helper.
        def verified_component(ppm: int) -> int:
            quotient, remainder = divmod(reference * ppm, MICROS)
            if remainder * 2 > MICROS or (remainder * 2 == MICROS and quotient % 2):
                quotient += 1
            return max(quotient, 1)

        expected_spread = verified_component(policy.half_spread_ppm) * event.qty
        expected_slippage = verified_component(policy.slippage_ppm) * event.qty
        spread_bad += event.spread_cost_micros != expected_spread
        slippage_bad += event.slippage_cost_micros != expected_slippage
        per_unit_cost = (expected_spread + expected_slippage) // event.qty
        expected_execution = (
            reference + per_unit_cost
            if event.side is Side.BUY
            else reference - per_unit_cost
        )
        execution_bad += event.price_micros != expected_execution
        policy_bad += event.execution_policy_hash != canonical_hash(policy)
    return (
        spread_total,
        slippage_total,
        spread_bad,
        slippage_bad,
        execution_bad,
        policy_bad,
    )


def _authority(
    events: list[LedgerEvent],
    packets: list[DecisionPacket] | None,
    bars: list[Bar] | None,
) -> tuple[int | None, int | None, int | None, int | None]:
    """Was every fill authorised by a decision, and did it wait for its bar?

    THE finding of the audit, and the product claim failing directly. The agent
    may never author a number -- but it could author the entire trade. A
    perfect-foresight BUY at the series low and SELL at the high, with
    `packet_id=None`, returned acceptable=True with every counter at zero, and
    the promotion blocker set was byte-identical to an honest run.

    `build_ledger` already looks the entry bar up via
    `earliest_execution_at_utc`, and its docstring calls executing at the signal
    bar's own close "the classic way a backtest invents profit". But that guard
    lives only in the PRODUCER. The receipt is the artifact a stranger checks,
    and it contained zero occurrences of the word `packet`, so the guard
    protected nothing anyone could verify. Bar conformance did not help: it
    resolves each event's own timestamp to a bar, so a fill relocated onto any
    other real bar is perfectly conformant.
    """

    if packets is None:
        return None, None, None, None
    by_id = {p.packet_id: p for p in packets}
    bar_positions = {bar.ts: i for i, bar in enumerate(bars)} if bars is not None else None
    expected_packet_ids: set[str] = set()
    if bar_positions is not None:
        busy_until = -1
        for packet in sorted(packets, key=lambda item: item.signal.decision_at_utc):
            entry_position = bar_positions.get(packet.signal.earliest_execution_at_utc)
            if entry_position is None or entry_position <= busy_until:
                continue
            exit_position = entry_position + packet.strategy.hold_bars
            if exit_position >= len(bars):
                continue
            expected_packet_ids.add(packet.packet_id)
            busy_until = exit_position
    fill_counts = {
        packet_id: {Side.BUY: 0, Side.SELL: 0}
        for packet_id in expected_packet_ids
    }
    unauthorized = early = mistimed_exits = 0
    for event in events:
        if event.type is not EventType.ORDER_FILL:
            continue
        packet = by_id.get(event.packet_id or "")
        if packet is None:
            unauthorized += 1
        else:
            if event.side not in (Side.BUY, Side.SELL):
                # LedgerEvent accepts partially populated rows so the receipt
                # can classify external artifacts. Malformed evidence is an
                # honest rejection, never a verifier crash.
                unauthorized += 1
                continue
            if bar_positions is not None and packet.packet_id not in expected_packet_ids:
                unauthorized += 1
                continue
            if packet.packet_id in fill_counts:
                fill_counts[packet.packet_id][event.side] += 1
            if event.qty != packet.strategy.position_qty:
                unauthorized += 1
            if event.side is Side.BUY and event.at_utc != packet.signal.earliest_execution_at_utc:
                early += 1
            if event.side is Side.SELL and bar_positions is not None:
                entry_position = bar_positions.get(packet.signal.earliest_execution_at_utc)
                exit_position = (
                    entry_position + packet.strategy.hold_bars
                    if entry_position is not None
                    else len(bars)
                )
                expected = bars[exit_position].ts if exit_position < len(bars) else None
                if event.at_utc != expected:
                    mistimed_exits += 1
    missing = sum(
        abs(1 - counts[Side.BUY]) + abs(1 - counts[Side.SELL])
        for counts in fill_counts.values()
    )
    return unauthorized, early, (mistimed_exits if bars is not None else None), missing


def _uncosted_fills(events: list[LedgerEvent]) -> int:
    """Every ORDER_FILL must be accompanied by a FEE at the same instant.

    Deleting every FEE inflated equity by $34.00 and improved drawdown by
    $32.43 while passing every counter, including the flat P&L residual --
    because that residual reads `cumulative_fees_micros` from the reduced curve,
    which drops to zero along with the events. Both sides moved together, which
    is the same failure mode as the two tautologies already deleted.

    Completeness cannot be checked from the reduction. It has to be checked
    against the event stream.
    """

    fee_stamps: dict = {}
    for e in events:
        if e.type is EventType.FEE:
            fee_stamps[(e.at_utc, e.packet_id)] = fee_stamps.get((e.at_utc, e.packet_id), 0) + 1
    uncosted = 0
    for e in events:
        if e.type is not EventType.ORDER_FILL:
            continue
        key = (e.at_utc, e.packet_id)
        if fee_stamps.get(key, 0) <= 0:
            uncosted += 1
        else:
            fee_stamps[key] -= 1
    return uncosted


def _capital_violations(events: list[LedgerEvent]) -> int:
    """Exactly one INITIAL_CAPITAL, and it opens the ledger."""

    capital = [i for i, e in enumerate(events) if e.type is EventType.INITIAL_CAPITAL]
    if capital == [0]:
        return 0
    return max(len(capital), 1)


def curve_receipt(
    events: list[LedgerEvent],
    bars: list[Bar] | None = None,
    packets: list[DecisionPacket] | None = None,
    journal: list[JournalEvent] | None = None,
) -> CurveReductionReceipt:
    points = reduce_curve(events)
    oracle = _reduce_curve_oracle(events)

    mismatches = sum(
        1 for p, o in zip(points, oracle) if p.equity_micros != o
    ) + abs(len(points) - len(oracle))

    price_mismatches, unpriced = _bar_conformance(events, bars)
    (
        spread_cost,
        slippage_cost,
        spread_mismatches,
        slippage_mismatches,
        execution_mismatches,
        policy_mismatches,
    ) = _execution_cost_conformance(events, bars, packets)
    stamps = [e.at_utc for e in events]
    non_monotonic = sum(1 for a, b in zip(stamps, stamps[1:]) if b < a)
    seqs = [e.seq for e in events]
    ids = [e.event_id for e in events]

    running = 0
    negative = 0
    for e in events:
        running += posting(e)[0]
        if running < 0:
            negative += 1

    unauthorized, early, mistimed_exits, missing_fills = _authority(events, packets, bars)

    # Long-only was enforced in SHARES and unenforced in DOLLARS: rewriting every
    # fill to qty=500 drove cash to -$460,911 on $100,000 of capital with
    # inconsistent_events=0.
    negative_cash = sum(1 for p in points if p.cash_micros < 0)
    negative_equity = sum(1 for p in points if p.equity_micros < 0)
    journal_receipt = (
        reconcile(journal, events, bars, packets)
        if journal is not None and bars is not None and packets is not None
        else None
    )

    receipt = CurveReductionReceipt(
        ledger_root_hash=ledger_root_hash(events),
        bars_hash=bars_hash(bars) if bars is not None else None,
        event_count=len(events),
        primary_curve_hash=_hash([p.equity_micros for p in points]),
        oracle_curve_hash=_hash(oracle),
        oracle_independence="same_author_diverse_implementation",
        curve_point_count=len(points),
        full_curve_matched=mismatches == 0,
        curve_point_mismatches=mismatches,
        non_monotonic_timestamps=non_monotonic,
        price_mismatches=price_mismatches,
        unpriced_events=unpriced,
        spread_cost_micros=spread_cost,
        slippage_cost_micros=slippage_cost,
        spread_model_mismatches=spread_mismatches,
        slippage_model_mismatches=slippage_mismatches,
        execution_price_mismatches=execution_mismatches,
        execution_policy_mismatches=policy_mismatches,
        capital_event_violations=_capital_violations(events),
        uncosted_fills=_uncosted_fills(events),
        negative_cash_points=negative_cash,
        negative_equity_points=negative_equity,
        unauthorized_fills=unauthorized,
        early_entries=early,
        mistimed_exits=mistimed_exits,
        missing_authorized_fills=missing_fills,
        packet_hashes=(
            [canonical_hash(packet) for packet in packets]
            if packets is not None
            else None
        ),
        sequence_gaps=sum(1 for a, b in zip(seqs, seqs[1:]) if b != a + 1),
        duplicate_event_ids=len(ids) - len(set(ids)),
        inconsistent_events=sum(1 for e in events if _inconsistent(e)),
        negative_position_points=negative,
        cross_stream_reconciliation=(
            "PASS"
            if journal_receipt and journal_receipt.acceptable
            else "FAIL"
            if journal_receipt
            else "NOT_RUN"
        ),
        order_journal_receipt=journal_receipt,
    )
    return receipt


def drawdown_micros(points: list[EquityPoint]) -> list[int]:
    out: list[int] = []
    peak = None
    for p in points:
        peak = p.equity_micros if peak is None else max(peak, p.equity_micros)
        out.append(p.equity_micros - peak)
    return out


def summary(points: list[EquityPoint]) -> dict:
    if not points:
        return {"events": 0}
    dd = drawdown_micros(points)
    return {
        "events": len(points),
        "final_equity_micros": points[-1].equity_micros,
        "realised_pnl_micros": points[-1].realised_pnl_micros,
        "cumulative_fees_micros": points[-1].cumulative_fees_micros,
        "cumulative_spread_cost_micros": points[-1].cumulative_spread_cost_micros,
        "cumulative_slippage_cost_micros": points[-1].cumulative_slippage_cost_micros,
        "max_drawdown_micros": min(dd),
    }


def demo() -> None:
    from app.proof_kernel import fixture_bars, packets as kernel_packets

    bars = fixture_bars()
    pkts = list(kernel_packets(25, bars))
    events = build_ledger(bars, pkts)
    points = reduce_curve(events)
    receipt = curve_receipt(events, bars, pkts)

    assert events and points
    assert receipt.acceptable, receipt.model_dump()

    # The accounting identity, at every point, exactly -- integers, no tolerance.
    for p in points:
        assert p.equity_micros == p.cash_micros + p.position_value_micros

    # Property: no fills leaves equity at initial capital.
    genesis = events[:1]
    assert reduce_curve(genesis)[0].equity_micros == STARTING_CAPITAL_MICROS

    # Property: a fee reduces cash and equity equally.
    before = reduce_curve(events[:2])[-1]
    fee_only = [e for e in events[:3]]
    after = reduce_curve(fee_only)[-1]
    if fee_only[-1].type is EventType.FEE:
        assert before.cash_micros - after.cash_micros == FEE_MICROS
        assert before.equity_micros - after.equity_micros == FEE_MICROS

    print(f"ledger: {len(events)} events, {len(points)} points")
    print(f"  {summary(points)}")
    print(f"  acceptable={receipt.acceptable} price_mismatches={receipt.price_mismatches}")


if __name__ == "__main__":
    demo()
