"""The ledger and the curve reduced from it.

Money is integer microdollars here, so every assertion is exact equality. If any
test in this file needs a tolerance, the ledger has grown a float somewhere and
that is the bug, not the test.
"""

from __future__ import annotations

import pytest
from dataclasses import replace

from app import ledger as lg
from app import proof_kernel as pk


@pytest.fixture(scope="module")
def bars():
    return pk.fixture_bars()


@pytest.fixture(scope="module")
def packets(bars):
    return list(pk.packets(25, bars))


@pytest.fixture(scope="module")
def events(bars, packets):
    return lg.build_ledger(bars, packets)


def _reprice_from_frozen_policy(event, bar, packet):
    """Move a fill to another genuine bar without introducing a cost-model error."""

    policy = packet.strategy.execution_cost_policy
    reference = lg.to_micros(bar.close)
    spread_per_unit = lg._round_half_even_ratio(reference * policy.half_spread_ppm)
    slippage_per_unit = lg._round_half_even_ratio(reference * policy.slippage_ppm)
    adverse = spread_per_unit + slippage_per_unit
    event.reference_price_micros = reference
    event.spread_cost_micros = spread_per_unit * event.qty
    event.slippage_cost_micros = slippage_per_unit * event.qty
    event.execution_policy_hash = lg.canonical_hash(policy)
    event.price_micros = reference + adverse if event.side is lg.Side.BUY else reference - adverse
    event.cash_delta_micros = -event.position_delta * event.price_micros


# --------------------------------------------------------------------------
# The reduction holds
# --------------------------------------------------------------------------


def test_the_receipt_is_acceptable(events, bars, packets):
    receipt = lg.curve_receipt(events, bars, packets)
    assert receipt.acceptable, receipt.model_dump()
    assert receipt.full_curve_matched
    assert receipt.primary_curve_hash == receipt.oracle_curve_hash


def test_the_accounting_identity_holds_at_every_point_exactly(events):
    """Not just at the end. Two reducers can agree on the final number while
    disagreeing about the path, and a drawdown is measured from the path."""

    for p in lg.reduce_curve(events):
        assert p.equity_micros == p.cash_micros + p.position_value_micros


def test_sequence_is_dense_and_ids_unique(events, bars, packets):
    """`missing_event_receipts` used to be asserted here. It was deleted:
    `receipt` is a required non-Optional field, so the counter could never be
    non-zero, and an assertion that cannot fail was inflating the check count
    from 8 to 9 and being hashed into the frozen policy."""

    receipt = lg.curve_receipt(events, bars, packets)
    assert receipt.sequence_gaps == 0
    assert receipt.duplicate_event_ids == 0


def test_every_point_carries_its_lineage(events):
    points = lg.reduce_curve(events)
    assert [p.through_event_seq for p in points] == list(range(len(events)))
    assert len({p.ledger_prefix_hash for p in points}) == len(points)


def test_the_ledger_is_byte_reproducible(bars):
    a = lg.build_ledger(bars, list(pk.packets(25, bars)))
    b = lg.build_ledger(bars, list(pk.packets(25, bars)))
    assert lg.ledger_root_hash(a) == lg.ledger_root_hash(b)


# --------------------------------------------------------------------------
# Properties the thread named
# --------------------------------------------------------------------------


def test_no_fills_leaves_equity_at_initial_capital(events):
    genesis = lg.reduce_curve(events[:1])
    assert genesis[0].equity_micros == lg.STARTING_CAPITAL_MICROS
    assert genesis[0].position_value_micros == 0


def test_a_fee_reduces_cash_and_equity_equally(events):
    fee_index = next(i for i, e in enumerate(events) if e.type is lg.EventType.FEE)
    before = lg.reduce_curve(events[:fee_index])[-1]
    after = lg.reduce_curve(events[: fee_index + 1])[-1]
    assert before.cash_micros - after.cash_micros == lg.FEE_MICROS
    assert before.equity_micros - after.equity_micros == lg.FEE_MICROS


def test_modeled_costs_are_positive_visible_and_adverse(events, bars, packets):
    """A reviewer sees real dollar impact, and the first fill cannot mark an
    immediate profit by executing and valuing at the same favorable price."""

    receipt = lg.curve_receipt(events, bars, packets)
    points = lg.reduce_curve(events)
    first_fill_index = next(i for i, e in enumerate(events) if e.type is lg.EventType.ORDER_FILL)
    first_fill = events[first_fill_index]
    after_fill = points[first_fill_index]

    assert receipt.spread_cost_micros and receipt.spread_cost_micros > 0
    assert receipt.slippage_cost_micros and receipt.slippage_cost_micros > 0
    assert after_fill.equity_micros == (
        lg.STARTING_CAPITAL_MICROS
        - first_fill.spread_cost_micros
        - first_fill.slippage_cost_micros
    )
    assert points[-1].cumulative_spread_cost_micros == receipt.spread_cost_micros
    assert points[-1].cumulative_slippage_cost_micros == receipt.slippage_cost_micros


def test_harsher_frozen_cost_policy_cannot_improve_equity(bars, packets):
    harsher = []
    for packet in packets:
        policy = packet.strategy.execution_cost_policy
        harsher_policy = policy.model_copy(
            update={
                "half_spread_ppm": policy.half_spread_ppm * 2,
                "slippage_ppm": policy.slippage_ppm * 2,
            }
        )
        harsher.append(
            packet.model_copy(
                update={
                    "strategy": packet.strategy.model_copy(
                        update={"execution_cost_policy": harsher_policy}
                    )
                }
            )
        )

    baseline = lg.reduce_curve(lg.build_ledger(bars, packets))[-1]
    stressed = lg.reduce_curve(lg.build_ledger(bars, harsher))[-1]
    assert stressed.equity_micros < baseline.equity_micros


def test_claimed_cost_cannot_certify_itself(events, bars, packets):
    forged = [event.model_copy(deep=True) for event in events]
    fill = next(event for event in forged if event.type is lg.EventType.ORDER_FILL)
    fill.spread_cost_micros += 1

    receipt = lg.curve_receipt(forged, bars, packets)
    assert receipt.spread_model_mismatches == 1
    assert not receipt.acceptable


def test_positive_policy_never_rounds_a_fill_component_to_zero(events, bars, packets):
    first_fill = next(event for event in events if event.type is lg.EventType.ORDER_FILL)
    tiny_bars = [
        replace(bar, close=0.005) if bar.ts == first_fill.at_utc else bar
        for bar in bars
    ]
    tiny_events = lg.build_ledger(tiny_bars, packets)
    tiny_fill = next(event for event in tiny_events if event.type is lg.EventType.ORDER_FILL)
    receipt = lg.curve_receipt(tiny_events, tiny_bars, packets)

    assert tiny_fill.reference_price_micros == 5_000
    assert tiny_fill.spread_cost_micros >= tiny_fill.qty
    assert tiny_fill.slippage_cost_micros >= tiny_fill.qty
    assert receipt.spread_model_mismatches == 0
    assert receipt.slippage_model_mismatches == 0


def test_buy_then_sell_at_the_same_price_costs_exactly_the_fees(bars):
    """The sharpest property: a round trip at an unchanged price must lose the
    fees and nothing else. Any drift in the reducer shows up here as a residual
    that is not a whole number of fees."""

    # Built explicitly so the entry and exit prices are identical by
    # construction rather than by luck.
    entry = bars[300]
    receipt = lg.FactReceipt(
        probe_id="test", source="fixture", retrieved_at_utc=entry.ts
    )
    price = lg.to_micros(entry.close)
    events = [
        lg.LedgerEvent(seq=0, event_id="e0", type=lg.EventType.INITIAL_CAPITAL,
                       at_utc=entry.ts, cash_delta_micros=lg.STARTING_CAPITAL_MICROS,
                       receipt=receipt),
        lg.LedgerEvent(seq=1, event_id="e1", type=lg.EventType.ORDER_FILL,
                       at_utc=entry.ts, side=lg.Side.BUY, qty=1, price_micros=price,
                       cash_delta_micros=-price, position_delta=1, receipt=receipt),
        lg.LedgerEvent(seq=2, event_id="e2", type=lg.EventType.FEE, at_utc=entry.ts,
                       cash_delta_micros=-lg.FEE_MICROS, receipt=receipt),
        lg.LedgerEvent(seq=3, event_id="e3", type=lg.EventType.ORDER_FILL,
                       at_utc=entry.ts, side=lg.Side.SELL, qty=1, price_micros=price,
                       cash_delta_micros=price, position_delta=-1, receipt=receipt),
        lg.LedgerEvent(seq=4, event_id="e4", type=lg.EventType.FEE, at_utc=entry.ts,
                       cash_delta_micros=-lg.FEE_MICROS, receipt=receipt),
    ]
    points = lg.reduce_curve(events)
    assert points[-1].equity_micros == lg.STARTING_CAPITAL_MICROS - 2 * lg.FEE_MICROS
    assert points[-1].realised_pnl_micros == 0
    # Hand-built and packet-less on purpose, so `acceptable` is not the claim
    # here -- the round-trip arithmetic is. Authority is covered by the fixture
    # ledger tests, which do supply packets.
    assert lg.curve_receipt(events, bars).unauthorized_fills is None


def test_drawdown_is_never_positive(events):
    assert all(d <= 0 for d in lg.drawdown_micros(lg.reduce_curve(events)))


# --------------------------------------------------------------------------
# The reduction refuses lies
# --------------------------------------------------------------------------


def test_a_tampered_cash_delta_is_caught_by_conservation_not_by_the_oracle(events, bars, packets):
    """States precisely which mechanism catches this, because the obvious guess
    is wrong.

    Both reducers consume the same events, so a tampered cash delta is reduced
    identically by both and the curve hashes still match. The oracle is blind to
    it. What catches it is that the fill no longer conserves -- cash moved by an
    amount that is not qty x price.
    """

    tampered = [e.model_copy(deep=True) for e in events]
    fill = next(i for i, e in enumerate(tampered) if e.type is lg.EventType.ORDER_FILL)
    tampered[fill].cash_delta_micros += 5_000_000

    receipt = lg.curve_receipt(tampered, bars, packets)
    honest = lg.curve_receipt(events, bars, packets)

    assert receipt.full_curve_matched, "the oracle cannot see ledger tampering"
    assert receipt.inconsistent_events > 0, "conservation must see it"
    assert not receipt.acceptable
    assert receipt.ledger_root_hash != honest.ledger_root_hash


def test_a_sequence_gap_is_caught(events):
    gapped = [e.model_copy(deep=True) for e in events]
    gapped[5].seq += 10
    assert lg.curve_receipt(gapped).sequence_gaps > 0
    assert not lg.curve_receipt(gapped).acceptable


def test_a_duplicate_event_id_is_caught(events):
    duped = [e.model_copy(deep=True) for e in events]
    duped[4].event_id = duped[3].event_id
    assert lg.curve_receipt(duped).duplicate_event_ids > 0
    assert not lg.curve_receipt(duped).acceptable


def test_shares_appearing_from_nowhere_are_caught(events):
    """The finding that produced the conservation check.

    Injecting position with no cash movement passes the accounting identity --
    the reducer computes equity as cash + position x mark, so a bigger position
    just means bigger equity, consistently. It also passes the oracle, because
    both reducers read the same fabricated event. Before conservation existed,
    this returned acceptable=True.
    """

    broken = [e.model_copy(deep=True) for e in events]
    fill = next(i for i, e in enumerate(broken) if e.type is lg.EventType.ORDER_FILL)
    broken[fill].position_delta += 5

    receipt = lg.curve_receipt(broken)

    assert receipt.full_curve_matched, "the oracle cannot see this either"
    assert receipt.inconsistent_events > 0, "conservation is what must catch it"
    assert not receipt.acceptable


def test_a_position_that_goes_short_is_caught(events):
    """Long-only by design. A negative running position means an exit executed
    without an entry, which the sequence checks alone would not notice."""

    broken = [e.model_copy(deep=True) for e in events]
    fill = next(i for i, e in enumerate(broken) if e.type is lg.EventType.ORDER_FILL)
    broken[fill].side = lg.Side.SELL
    broken[fill].position_delta = -1
    broken[fill].cash_delta_micros = broken[fill].price_micros

    receipt = lg.curve_receipt(broken)
    assert receipt.negative_position_points > 0
    assert not receipt.acceptable


def test_money_never_becomes_a_float(events):
    """Guards the reason this module exists. A float in the ledger reintroduces
    tolerance into money, and a tolerance on money hides discrepancies."""

    for e in events:
        assert isinstance(e.cash_delta_micros, int)
        assert isinstance(e.price_micros, int)
        assert not isinstance(e.cash_delta_micros, bool)
    for p in lg.reduce_curve(events):
        assert isinstance(p.equity_micros, int)
        assert isinstance(p.cash_micros, int)


def test_a_fabricated_execution_price_is_caught_by_cost_conformance(events, bars, packets):
    """The P0 an adversarial pass found.

    Inventing a price and re-deriving cash_delta satisfies conservation exactly
    -- cash still moved by qty x price. Both reducers agree, the identity holds,
    every counter is zero. A ledger worth $1,098,843 passed. What catches it is
    resolving the price back to the bar the event claims to come from.
    """

    forged = [e.model_copy(deep=True) for e in events]
    i = next(j for j, e in enumerate(forged) if e.type is lg.EventType.ORDER_FILL)
    forged[i].price_micros *= 3
    forged[i].cash_delta_micros = -forged[i].position_delta * forged[i].price_micros

    blind = lg.curve_receipt(forged)            # no bars supplied
    seeing = lg.curve_receipt(forged, bars, packets)

    assert blind.inconsistent_events == 0, "conservation cannot see a forged price"
    assert blind.full_curve_matched, "the oracle cannot see it either"
    assert seeing.price_mismatches == 0, "the claimed reference still matches the bar"
    assert seeing.execution_price_mismatches == 1
    assert not seeing.acceptable


def test_a_receipt_without_bars_is_never_acceptable(events):
    """A check that did not run must not read as clean. This is exactly how a
    fabricated ledger reached PROMOTABLE."""

    blind = lg.curve_receipt(events)
    assert blind.price_mismatches is None
    assert not blind.bars_checked
    assert not blind.acceptable


def test_timestamps_running_backwards_are_caught(events, bars):
    from datetime import timedelta

    shuffled = [e.model_copy(deep=True) for e in events]
    shuffled[10].at_utc = shuffled[0].at_utc - timedelta(days=1)
    assert lg.curve_receipt(shuffled, bars).non_monotonic_timestamps > 0
    assert not lg.curve_receipt(shuffled, bars).acceptable


# --------------------------------------------------------------------------
# Attacks a 13-agent audit found. Each passed every counter before these.
# --------------------------------------------------------------------------


def test_a_ghost_fill_with_no_packet_is_caught(bars, packets):
    """THE finding. The agent may never author a number -- but it could author
    the whole trade. A perfect-foresight BUY at the low and SELL at the high,
    packet_id=None, returned acceptable=True with every counter zero, and the
    promotion blocker set was byte-identical to an honest run."""

    window = bars[1000:2300]
    low = min(window, key=lambda b: b.close)
    high = max([b for b in window if b.index > low.index], key=lambda b: b.close)
    rc = lambda b: lg.FactReceipt(  # noqa: E731
        probe_id="bars.spy.1m.fixture", source="fixture", retrieved_at_utc=b.ts
    )
    ghost = [
        lg.LedgerEvent(seq=0, event_id="g0", type=lg.EventType.INITIAL_CAPITAL,
                       at_utc=bars[0].ts, cash_delta_micros=lg.STARTING_CAPITAL_MICROS,
                       receipt=rc(bars[0])),
        lg.LedgerEvent(seq=1, event_id="g1", type=lg.EventType.ORDER_FILL, at_utc=low.ts,
                       side=lg.Side.BUY, qty=1, reference_price_micros=lg.to_micros(low.close),
                       price_micros=lg.to_micros(low.close),
                       cash_delta_micros=-lg.to_micros(low.close), position_delta=1,
                       receipt=rc(low)),
        lg.LedgerEvent(seq=2, event_id="g2", type=lg.EventType.ORDER_FILL, at_utc=high.ts,
                       side=lg.Side.SELL, qty=1, reference_price_micros=lg.to_micros(high.close),
                       price_micros=lg.to_micros(high.close),
                       cash_delta_micros=lg.to_micros(high.close), position_delta=-1,
                       receipt=rc(high)),
    ]

    receipt = lg.curve_receipt(ghost, bars, packets)
    assert receipt.price_mismatches == 0, "every price is a real bar close"
    assert receipt.inconsistent_events == 0, "conservation is satisfied"
    assert receipt.unauthorized_fills == 2, "authority is what must catch it"
    assert not receipt.acceptable


def test_an_entry_on_the_signal_bar_itself_is_caught(bars, packets, events):
    """build_ledger's docstring calls this the classic way a backtest invents
    profit and guards against it -- but the guard lived only in the producer.
    Bar conformance cannot see it: a fill moved onto any other real bar is
    perfectly conformant."""

    shifted = [e.model_copy(deep=True) for e in events]
    by_ts = {b.ts: b for b in bars}
    for e in shifted:
        if e.type is lg.EventType.ORDER_FILL and e.side is lg.Side.BUY:
            prev = bars[by_ts[e.at_utc].index - 1]
            e.at_utc = prev.ts
            packet = next(packet for packet in packets if packet.packet_id == e.packet_id)
            _reprice_from_frozen_policy(e, prev, packet)

    receipt = lg.curve_receipt(shifted, bars, packets)
    assert receipt.price_mismatches == 0, "bar conformance is blind to relocation"
    assert receipt.early_entries > 0, "authority is what must catch it"
    assert not receipt.acceptable


def test_a_second_initial_capital_is_caught(bars, packets, events):
    """It conjured $1,000,000, passed every counter, and IMPROVED reported max
    drawdown from -$553.40 to -$289.24 by resetting the running peak --
    fabrication that makes the risk number look better."""

    injected = [e.model_copy(deep=True) for e in events]
    mid = len(injected) // 2
    injected.insert(mid, lg.LedgerEvent(
        seq=0, event_id="inj", type=lg.EventType.INITIAL_CAPITAL,
        at_utc=injected[mid].at_utc, cash_delta_micros=1_000_000 * lg.MICROS,
        receipt=injected[mid].receipt))
    for i, e in enumerate(injected):
        e.seq = i

    receipt = lg.curve_receipt(injected, bars, packets)
    assert receipt.capital_event_violations > 0
    assert not receipt.acceptable


def test_an_insolvent_ledger_is_caught(bars, packets, events):
    """Long-only was enforced in shares and unenforced in dollars: qty=500 drove
    cash to -$460,911 on $100,000 of capital with inconsistent_events=0."""

    big = [e.model_copy(deep=True) for e in events]
    for e in big:
        if e.type is lg.EventType.ORDER_FILL:
            e.qty = 500
            e.position_delta = 500 if e.side is lg.Side.BUY else -500
            e.cash_delta_micros = -e.position_delta * e.price_micros

    receipt = lg.curve_receipt(big, bars, packets)
    assert receipt.inconsistent_events == 0, "each event conserves internally"
    assert receipt.negative_cash_points > 0
    assert not receipt.acceptable


def test_deleting_every_fee_is_caught(bars, packets, events):
    """Inflated equity by $34.00 and improved drawdown by $32.43. The flat P&L
    residual could not see it: it reads cumulative_fees from the reduced curve,
    which drops to zero with the events, so both sides moved together."""

    nofee = [e.model_copy(deep=True) for e in events if e.type is not lg.EventType.FEE]
    for i, e in enumerate(nofee):
        e.seq = i

    receipt = lg.curve_receipt(nofee, bars, packets)
    assert receipt.uncosted_fills > 0, "completeness is what must catch it"
    assert not receipt.acceptable


def test_a_coherently_inflated_authorized_round_trip_is_rejected(bars, packets, events):
    """An operator reviews 25 signal episodes after an adversarial export mutates
    one BUY/SELL pair from one share to six. Cash and position postings remain
    internally consistent, so only the frozen packet quantity can catch it.

    The reducer is pure and has no concurrent/shared state; burst and sustained
    coverage come from checking the complete multi-episode ledger, not threads.
    """

    forged = [e.model_copy(deep=True) for e in events]
    packet_id = next(e.packet_id for e in forged if e.type is lg.EventType.ORDER_FILL)
    for event in forged:
        if event.type is lg.EventType.ORDER_FILL and event.packet_id == packet_id:
            event.qty += 5
            event.position_delta = event.qty if event.side is lg.Side.BUY else -event.qty
            event.cash_delta_micros = -event.position_delta * event.price_micros

    receipt = lg.curve_receipt(forged, bars, packets)

    assert receipt.inconsistent_events == 0, "the forged postings conserve internally"
    assert receipt.unauthorized_fills == 2, "both sides exceed frozen packet authority"
    assert receipt.cross_stream_reconciliation == "NOT_RUN"
    assert not receipt.acceptable


def test_strategy_authority_is_immutable_and_not_shared_mutable_state(packets):
    """Two concurrent reviewers may retain packets from the same strategy. One
    packet must not be able to rewrite sizing policy underneath every other run.
    """

    with pytest.raises(Exception):
        packets[0].strategy.position_qty = 7
    assert all(packet.strategy.position_qty == 1 for packet in packets)


def test_a_sell_relocated_to_another_real_bar_is_rejected(bars, packets, events):
    """A hindsight attacker moves an exit and its fee to the next genuine bar,
    reprices consistently, and preserves sequence order. Bar validity alone must
    not be mistaken for exit authorization.
    """

    moved = [e.model_copy(deep=True) for e in events]
    sell = next(e for e in moved if e.type is lg.EventType.ORDER_FILL and e.side is lg.Side.SELL)
    next_bar = bars[next(i for i, bar in enumerate(bars) if bar.ts == sell.at_utc) + 1]
    old_time = sell.at_utc
    sell.at_utc = next_bar.ts
    packet = next(packet for packet in packets if packet.packet_id == sell.packet_id)
    _reprice_from_frozen_policy(sell, next_bar, packet)
    for fee in moved:
        if fee.type is lg.EventType.FEE and fee.packet_id == sell.packet_id and fee.at_utc == old_time:
            fee.at_utc = next_bar.ts

    moved.sort(key=lambda event: (event.at_utc, event.seq))
    for seq, event in enumerate(moved):
        event.seq = seq

    receipt = lg.curve_receipt(moved, bars, packets)
    assert receipt.price_mismatches == 0, "the relocated price belongs to a real bar"
    assert receipt.mistimed_exits == 1, "the frozen hold policy must catch hindsight relocation"
    assert not receipt.acceptable


def test_a_receipt_without_packets_is_never_acceptable(events, bars):
    """An unrun authority check is not a clean one."""

    blind = lg.curve_receipt(events, bars)
    assert blind.unauthorized_fills is None
    assert not blind.packets_checked
    assert not blind.acceptable


def test_a_malformed_fill_is_rejected_without_crashing_the_receipt(bars, packets, events):
    """A reviewer receives schema-valid but incomplete external evidence. A
    missing side must become explicit negative evidence rather than taking the
    verification surface down before it can issue a receipt.
    """

    malformed = [event.model_copy(deep=True) for event in events]
    fill = next(event for event in malformed if event.type is lg.EventType.ORDER_FILL)
    fill.side = None

    receipt = lg.curve_receipt(malformed, bars, packets)

    assert receipt.inconsistent_events > 0
    assert receipt.unauthorized_fills > 0
    assert not receipt.acceptable


def test_a_claimed_posting_cannot_move_the_curve(bars, packets, events):
    """`qty` and `position_delta` were two representations of one quantity
    driving two different authoritative calculations -- the position reducer
    consumed position_delta, the cost-basis reducer consumed qty. Same family as
    the strategy-predicate and Rule.result bugs: duplicate authority, one of
    which can drift.

    Postings are now DERIVED from side/qty/price, so a fabricated position_delta
    is both ignored by the reduction and flagged by conservation."""

    forged = [e.model_copy(deep=True) for e in events]
    fill = next(i for i, e in enumerate(forged) if e.type is lg.EventType.ORDER_FILL)
    forged[fill].position_delta += 5

    honest_curve = lg.reduce_curve(events)
    forged_curve = lg.reduce_curve(forged)

    assert [p.equity_micros for p in forged_curve] == [
        p.equity_micros for p in honest_curve
    ], "a claimed posting must not be able to move equity"
    assert lg.curve_receipt(forged, bars, packets).inconsistent_events > 0


def test_a_token_fee_is_caught(bars, packets, events):
    """A fee of -1 microdollar satisfied "is it negative" while charging nothing.
    The amount matters, not just the sign."""

    cheap = [e.model_copy(deep=True) for e in events]
    for e in cheap:
        if e.type is lg.EventType.FEE:
            e.cash_delta_micros = -1

    receipt = lg.curve_receipt(cheap, bars, packets)
    assert receipt.uncosted_fills == 0, "a FEE event does exist for every fill"
    assert receipt.inconsistent_events > 0, "the amount is what must catch it"
    assert not receipt.acceptable


def test_the_two_reducers_do_not_share_their_derivation(bars, packets, events):
    """Guards a regression I introduced and an external review caught.

    Routing both reducers through `posting()` made them agree BY CONSTRUCTION
    about the one thing they exist to cross-check. The oracle now derives
    independently, so the agreement is evidence rather than a tautology.
    """

    import inspect

    # Strip comments: the first version of this assertion matched the comment
    # explaining the fix and would have failed forever on prose.
    code = "\n".join(
        line.split("#")[0] for line in inspect.getsource(lg._reduce_curve_oracle).splitlines()
    )
    assert "posting(" not in code, "the oracle must not share the primary's derivation"
    assert "posting(" in "\n".join(
        line.split("#")[0] for line in inspect.getsource(lg.reduce_curve).splitlines()
    ), "the primary must use the canonical derivation"
    assert lg.curve_receipt(events, bars, packets).full_curve_matched
