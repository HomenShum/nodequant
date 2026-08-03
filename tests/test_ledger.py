"""The ledger and the curve reduced from it.

Money is integer microdollars here, so every assertion is exact equality. If any
test in this file needs a tolerance, the ledger has grown a float somewhere and
that is the bug, not the test.
"""

from __future__ import annotations

import pytest

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


def test_a_fabricated_price_is_caught_only_by_bar_conformance(events, bars):
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
    seeing = lg.curve_receipt(forged, bars)     # bars supplied

    assert blind.inconsistent_events == 0, "conservation cannot see a forged price"
    assert blind.full_curve_matched, "the oracle cannot see it either"
    assert seeing.price_mismatches == 1
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
                       side=lg.Side.BUY, qty=1, price_micros=lg.to_micros(low.close),
                       cash_delta_micros=-lg.to_micros(low.close), position_delta=1,
                       receipt=rc(low)),
        lg.LedgerEvent(seq=2, event_id="g2", type=lg.EventType.ORDER_FILL, at_utc=high.ts,
                       side=lg.Side.SELL, qty=1, price_micros=lg.to_micros(high.close),
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
            e.price_micros = lg.to_micros(prev.close)
            e.cash_delta_micros = -e.price_micros

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
    assert receipt.flat_pnl_residual_micros == 0, "the residual is blind to this"
    assert receipt.uncosted_fills > 0, "completeness is what must catch it"
    assert not receipt.acceptable


def test_a_receipt_without_packets_is_never_acceptable(events, bars):
    """An unrun authority check is not a clean one."""

    blind = lg.curve_receipt(events, bars)
    assert blind.unauthorized_fills is None
    assert not blind.packets_checked
    assert not blind.acceptable
