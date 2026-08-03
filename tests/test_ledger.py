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
def events(bars):
    return lg.build_ledger(bars, list(pk.packets(25, bars)))


# --------------------------------------------------------------------------
# The reduction holds
# --------------------------------------------------------------------------


def test_the_receipt_is_acceptable(events, bars):
    receipt = lg.curve_receipt(events, bars)
    assert receipt.acceptable, receipt.model_dump()
    assert receipt.full_curve_matched
    assert receipt.primary_curve_hash == receipt.oracle_curve_hash


def test_the_accounting_identity_holds_at_every_point_exactly(events):
    """Not just at the end. Two reducers can agree on the final number while
    disagreeing about the path, and a drawdown is measured from the path."""

    for p in lg.reduce_curve(events):
        assert p.equity_micros == p.cash_micros + p.position_value_micros


def test_sequence_is_dense_and_ids_unique(events):
    receipt = lg.curve_receipt(events)
    assert receipt.sequence_gaps == 0
    assert receipt.duplicate_event_ids == 0
    assert receipt.missing_event_receipts == 0


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

    packet = next(iter(pk.packets(1, bars)))
    flat = lg.build_ledger(bars, [packet], hold_bars=0) if False else None
    # hold_bars=0 would exit on the entry bar; build it explicitly instead so the
    # entry and exit prices are identical by construction rather than by luck.
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
    assert flat is None
    points = lg.reduce_curve(events)
    assert points[-1].equity_micros == lg.STARTING_CAPITAL_MICROS - 2 * lg.FEE_MICROS
    assert points[-1].realised_pnl_micros == 0
    assert lg.curve_receipt(events, bars).acceptable


def test_drawdown_is_never_positive(events):
    assert all(d <= 0 for d in lg.drawdown_micros(lg.reduce_curve(events)))


# --------------------------------------------------------------------------
# The reduction refuses lies
# --------------------------------------------------------------------------


def test_a_tampered_cash_delta_is_caught_by_conservation_not_by_the_oracle(events, bars):
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

    receipt = lg.curve_receipt(tampered, bars)
    honest = lg.curve_receipt(events, bars)

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
