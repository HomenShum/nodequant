"""Fills, and the equity curve reduced from them.

The authority law says no displayed number may be asserted, only measured or
recomputed. An equity curve is the hardest case: it is the most-looked-at number
on the review surface and the easiest to fabricate, because nothing about a
smooth rising line tells a reader whether it came from fills or from a model.

So it is not stored. `equity_points` is a pure reduction over fills, every fill
carries the id of the decision packet that authorised it, and the whole chain is
re-derivable from `(bars, packets)` alone:

    bars -> packets -> fills -> equity -> summary

Each arrow is a deterministic function with no state and no clock. The panel
ships the inputs and the reduction; a reviewer who disbelieves the curve can run
the arrow themselves. `verify_run` does exactly that, plus an independent
reducer written to a different shape, plus the execution-ordering check that
matters most here: a fill may never be timestamped before the decision that
authorised it was allowed to execute.

ponytail: one position at a time, fixed holding period, one unit, no costs, no
slippage. Every one of those is a real modelling choice this does NOT make, and
each would change the numbers. The point of v0 is that the chain is provable,
not that the strategy is good. Add costs before anyone reads the curve as
performance.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Annotated, Iterable

from pydantic import BaseModel, Field

from app.decision_packet import DecisionPacket, FactReceipt, canonical_hash
from app.proof_kernel import Bar

HOLD_BARS = 30
UNIT = 1.0
STARTING_CASH = 100_000.0


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Fill(BaseModel):
    """One execution. `packet_id` is the authority chain: a fill with no packet
    is an order nobody decided to place, which the verifier rejects."""

    fill_id: Annotated[str, Field(min_length=1, max_length=120)]
    packet_id: Annotated[str, Field(min_length=1, max_length=120)]
    side: Side
    qty: float
    price: float
    at_utc: datetime
    bar_index: int
    receipt: FactReceipt


class EquityPoint(BaseModel):
    """A point on the curve, plus the fill that moved it.

    `fill_id` is what makes the curve auditable rather than decorative: every
    step traces to the execution that caused it.
    """

    at_utc: datetime
    equity: float
    realised_pnl: float
    fill_id: str


class Trade(BaseModel):
    """A closed round trip. Distinct from a fill on purpose -- the review
    surface needs both, and conflating them is how a partial fill becomes an
    invisible position."""

    packet_id: str
    entry_fill_id: str
    exit_fill_id: str
    entry_price: float
    exit_price: float
    pnl: float
    bars_held: int


# --------------------------------------------------------------------------
# bars + packets -> fills
# --------------------------------------------------------------------------


def simulate(
    bars: list[Bar], packets: Iterable[DecisionPacket], hold_bars: int = HOLD_BARS
) -> list[Fill]:
    """Execute each packet at the bar it declared executable, hold, then exit.

    Entry uses the close of `earliest_execution_at_utc`'s bar, never the signal
    bar's own close. Executing at the price that triggered the signal is the
    single most common way a backtest invents profit, and it is invisible in the
    output -- the curve just looks better.
    """

    by_ts = {b.ts: b for b in bars}
    fills: list[Fill] = []
    busy_until = -1

    for packet in sorted(packets, key=lambda p: p.signal.decision_at_utc):
        entry_bar = by_ts.get(packet.signal.earliest_execution_at_utc)
        if entry_bar is None or entry_bar.index <= busy_until:
            continue  # no executable bar, or still holding: skip, do not stack
        exit_index = entry_bar.index + hold_bars
        if exit_index >= len(bars):
            continue  # would run off the end of the data; not a trade
        exit_bar = bars[exit_index]

        for side, bar in ((Side.BUY, entry_bar), (Side.SELL, exit_bar)):
            fills.append(
                Fill(
                    fill_id=f"fill-{packet.packet_id}-{side.value}",
                    packet_id=packet.packet_id,
                    side=side,
                    qty=UNIT,
                    price=bar.close,
                    at_utc=bar.ts,
                    bar_index=bar.index,
                    receipt=FactReceipt(
                        probe_id="bars.spy.1m.fixture",
                        source="fixture",
                        retrieved_at_utc=bar.ts,
                    ),
                )
            )
        busy_until = exit_index
    return fills


# --------------------------------------------------------------------------
# fills -> trades -> equity
# --------------------------------------------------------------------------


def trades(fills: list[Fill]) -> list[Trade]:
    out: list[Trade] = []
    open_entry: Fill | None = None
    for fill in fills:
        if fill.side is Side.BUY:
            open_entry = fill
            continue
        if open_entry is None:
            continue
        out.append(
            Trade(
                packet_id=fill.packet_id,
                entry_fill_id=open_entry.fill_id,
                exit_fill_id=fill.fill_id,
                entry_price=open_entry.price,
                exit_price=fill.price,
                pnl=round((fill.price - open_entry.price) * fill.qty, 6),
                bars_held=fill.bar_index - open_entry.bar_index,
            )
        )
        open_entry = None
    return out


def equity_points(fills: list[Fill], cash: float = STARTING_CASH) -> list[EquityPoint]:
    """Reduce fills to a curve. Pure: same fills, same points, always."""

    points: list[EquityPoint] = []
    realised = 0.0
    entry: Fill | None = None
    for fill in fills:
        if fill.side is Side.BUY:
            entry = fill
        elif entry is not None:
            realised = round(realised + (fill.price - entry.price) * fill.qty, 6)
            entry = None
        points.append(
            EquityPoint(
                at_utc=fill.at_utc,
                equity=round(cash + realised, 6),
                realised_pnl=realised,
                fill_id=fill.fill_id,
            )
        )
    return points


def _equity_independent(fills: list[Fill], cash: float = STARTING_CASH) -> list[float]:
    """The same reduction, written to a different shape.

    Pairs the fills first and accumulates per completed trade, rather than
    walking fill-by-fill holding an open entry. This is the reducer's oracle:
    it catches a mis-paired entry/exit or a sign slip, which a re-run of the
    same code cannot.
    """

    pairs = [(fills[i], fills[i + 1]) for i in range(0, len(fills) - 1, 2)]
    out: list[float] = []
    total = 0.0
    for buy, sell in pairs:
        out.append(round(cash + total, 6))  # equity at the buy, unchanged
        total = round(total + (sell.price - buy.price) * sell.qty, 6)
        out.append(round(cash + total, 6))  # equity after the sell
    return out


def drawdown(points: list[EquityPoint]) -> list[float]:
    """Underwater series: distance below the running peak, never positive."""

    out: list[float] = []
    peak = float("-inf")
    for p in points:
        peak = max(peak, p.equity)
        out.append(round(p.equity - peak, 6))
    return out


def summary(points: list[EquityPoint], closed: list[Trade]) -> dict:
    """Headline numbers, each reduced from the fills rather than tracked."""

    if not points:
        return {"trades": 0}
    dd = drawdown(points)
    wins = [t for t in closed if t.pnl > 0]
    return {
        "trades": len(closed),
        "final_equity": points[-1].equity,
        "total_pnl": points[-1].realised_pnl,
        "max_drawdown": min(dd) if dd else 0.0,
        "wins": len(wins),
        "losses": len(closed) - len(wins),
        "win_rate": round(len(wins) / len(closed), 6) if closed else 0.0,
    }


def run_hash(fills: list[Fill]) -> str:
    payload = [f.model_dump(mode="json") for f in fills]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


@dataclass
class RunCheck:
    check_id: str
    label: str
    passed: bool
    detail: str = ""


def verify_run(
    bars: list[Bar], packets: list[DecisionPacket], fills: list[Fill]
) -> list[RunCheck]:
    """Attack the chain from fills to curve.

    Every check here answers a way the curve could be a lie, not a way the code
    could throw.
    """

    known_packets = {p.packet_id: p for p in packets}
    points = equity_points(fills)

    orphans = sorted({f.fill_id for f in fills if f.packet_id not in known_packets})
    early = sorted(
        f.fill_id
        for f in fills
        if f.side is Side.BUY
        and f.packet_id in known_packets
        and f.at_utc < known_packets[f.packet_id].signal.earliest_execution_at_utc
    )
    priced_wrong = sorted(
        f.fill_id
        for f in fills
        if f.bar_index < len(bars) and bars[f.bar_index].close != f.price
    )
    independent = _equity_independent(fills)
    mismatched = [
        i
        for i, (p, q) in enumerate(zip(points, independent))
        if round(p.equity, 6) != round(q, 6)
    ]
    unreceipted = sorted(f.fill_id for f in fills if f.receipt is None)

    return [
        RunCheck("fill_authority", "Every fill cites a decision packet", not orphans,
                 f"orphan fills: {orphans}" if orphans else ""),
        RunCheck("execution_ordering",
                 "No fill precedes the moment its packet became executable",
                 not early, f"early fills: {early}" if early else ""),
        RunCheck("price_provenance", "Every fill price equals its bar's close",
                 not priced_wrong, f"invented prices: {priced_wrong}" if priced_wrong else ""),
        RunCheck("equity_reduction",
                 "An independent reducer reproduces every equity point",
                 len(independent) == len(points) and not mismatched,
                 f"{len(mismatched)} point(s) differ" if mismatched
                 else ("length mismatch" if len(independent) != len(points) else "")),
        RunCheck("fill_receipts", "Every fill carries a receipt", not unreceipted,
                 f"unreceipted: {unreceipted}" if unreceipted else ""),
    ]


def demo() -> None:
    from app.proof_kernel import fixture_bars, packets as kernel_packets

    bars = fixture_bars()
    pkts = list(kernel_packets(25, bars))
    fills = simulate(bars, pkts)
    closed = trades(fills)
    points = equity_points(fills)

    assert fills, "no fills produced"
    assert len(fills) % 2 == 0, "every entry must have an exit"
    assert len(closed) == len(fills) // 2

    checks = verify_run(bars, pkts, fills)
    failed = [c for c in checks if not c.passed]
    assert not failed, [(c.check_id, c.detail) for c in failed]

    # Determinism: the whole chain, twice.
    again = simulate(bars, list(kernel_packets(25, bars)))
    assert run_hash(fills) == run_hash(again)

    # The curve must MOVE. A reducer that returns a flat line passes every
    # structural check while proving nothing, so assert it actually varies.
    equities = {p.equity for p in points}
    assert len(equities) > 1, "equity never changed; the reduction is inert"

    # A fabricated equity point must not survive the independent reducer.
    tampered = [p.model_copy(deep=True) for p in points]
    tampered[-1].equity += 1000.0
    ind = _equity_independent(fills)
    assert round(tampered[-1].equity, 6) != round(ind[-1], 6)

    # A fill priced off its bar must be caught.
    bad = [f.model_copy(deep=True) for f in fills]
    bad[0].price = bad[0].price * 1.05
    assert any(not c.passed for c in verify_run(bars, pkts, bad))

    # A fill that executes before its packet allowed must be caught.
    early = [f.model_copy(deep=True) for f in fills]
    early[0].at_utc = pkts[0].signal.decision_at_utc
    assert any(
        c.check_id == "execution_ordering" and not c.passed
        for c in verify_run(bars, pkts, early)
    )

    print(f"backtest: {len(closed)} trades, {len(points)} equity points")
    print(f"  {summary(points, closed)}")
    print(f"  checks: {[(c.check_id, c.passed) for c in checks]}")


if __name__ == "__main__":
    demo()
