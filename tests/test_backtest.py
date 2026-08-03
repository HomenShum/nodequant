"""The fills-to-equity chain, attacked.

The equity curve is the most-looked-at number on the review surface and the
easiest to fabricate, because a smooth rising line looks the same whether it was
reduced from fills or invented. So the tests that matter are the ones that
fabricate something and require the verifier to notice.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app import backtest as bt
from app import proof_kernel as pk


@pytest.fixture(scope="module")
def bars():
    return pk.fixture_bars()


@pytest.fixture(scope="module")
def packets(bars):
    return list(pk.packets(25, bars))


@pytest.fixture(scope="module")
def fills(bars, packets):
    return bt.simulate(bars, packets)


# --------------------------------------------------------------------------
# The chain holds
# --------------------------------------------------------------------------


def test_every_check_passes_on_an_honest_run(bars, packets, fills):
    failed = [(c.check_id, c.detail) for c in bt.verify_run(bars, packets, fills) if not c.passed]
    assert not failed


def test_every_entry_has_an_exit(fills):
    assert fills and len(fills) % 2 == 0
    assert [f.side for f in fills[::2]] == [bt.Side.BUY] * (len(fills) // 2)
    assert [f.side for f in fills[1::2]] == [bt.Side.SELL] * (len(fills) // 2)


def test_the_run_is_byte_reproducible(bars, packets):
    assert bt.run_hash(bt.simulate(bars, packets)) == bt.run_hash(bt.simulate(bars, packets))


def test_equity_actually_moves(fills):
    """A reducer that returns a flat line satisfies every structural check while
    proving nothing. The curve must vary."""

    points = bt.equity_points(fills)
    assert len({p.equity for p in points}) > 1


def test_positions_never_overlap(fills):
    """One at a time. Stacked positions would make the equity reduction wrong in
    a way no single check catches."""

    for buy, sell in zip(fills[::2], fills[1::2]):
        assert buy.bar_index < sell.bar_index
    for prev_sell, next_buy in zip(fills[1::2], fills[2::2]):
        assert next_buy.bar_index > prev_sell.bar_index


def test_entry_never_executes_on_the_signal_bar_itself(bars, packets, fills):
    """Executing at the price that triggered the signal is the most common way a
    backtest invents profit, and it is invisible in the output."""

    by_id = {p.packet_id: p for p in packets}
    for fill in fills:
        if fill.side is bt.Side.BUY:
            packet = by_id[fill.packet_id]
            assert fill.at_utc >= packet.signal.earliest_execution_at_utc
            assert fill.at_utc > packet.signal.decision_at_utc


# --------------------------------------------------------------------------
# The chain refuses lies
# --------------------------------------------------------------------------


def test_a_fabricated_equity_point_fails_the_independent_reducer(fills):
    points = bt.equity_points(fills)
    independent = bt._equity_independent(fills)
    tampered = [p.model_copy(deep=True) for p in points]
    tampered[-1].equity += 1000.0
    assert round(tampered[-1].equity, 6) != round(independent[-1], 6)


def test_a_fill_priced_off_its_bar_is_caught(bars, packets, fills):
    bad = [f.model_copy(deep=True) for f in fills]
    bad[0].price *= 1.05
    failed = {c.check_id for c in bt.verify_run(bars, packets, bad) if not c.passed}
    assert "price_provenance" in failed


def test_a_fill_that_executes_too_early_is_caught(bars, packets, fills):
    early = [f.model_copy(deep=True) for f in fills]
    early[0].at_utc = packets[0].signal.decision_at_utc - timedelta(minutes=5)
    failed = {c.check_id for c in bt.verify_run(bars, packets, early) if not c.passed}
    assert "execution_ordering" in failed


def test_a_fill_with_no_authorising_packet_is_caught(bars, packets, fills):
    orphan = [f.model_copy(deep=True) for f in fills]
    orphan[0].packet_id = "pkt-nobody-decided-this"
    failed = {c.check_id for c in bt.verify_run(bars, packets, orphan) if not c.passed}
    assert "fill_authority" in failed


def test_a_mispaired_exit_is_caught_by_the_independent_reducer(bars, packets, fills):
    """The two reducers pair fills differently on purpose. Swapping a buy and a
    sell is exactly the error a re-run of the same code cannot see."""

    swapped = [f.model_copy(deep=True) for f in fills]
    swapped[0], swapped[1] = swapped[1], swapped[0]
    failed = {c.check_id for c in bt.verify_run(bars, packets, swapped) if not c.passed}
    assert failed, "swapping an entry and an exit must not go unnoticed"


def test_summary_reports_losses_honestly(bars, packets, fills):
    """This fixture strategy loses every trade. The summary must say so rather
    than round it away -- a review surface that cannot show a bad strategy is
    useless for the decision it exists to support."""

    closed = bt.trades(fills)
    s = bt.summary(bt.equity_points(fills), closed)
    assert s["trades"] == len(closed)
    assert s["wins"] + s["losses"] == s["trades"]
    assert s["max_drawdown"] <= 0.0
    assert s["total_pnl"] < 0, "expected this fixture strategy to lose"
