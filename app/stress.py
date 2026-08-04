"""Bounded counterfactual execution stress; diagnostic, never observed evidence."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field

from app.decision_packet import DecisionPacket
from app.ledger import EventType, Side, build_ledger, drawdown_micros, ledger_root_hash, reduce_curve
from app.proof_kernel import Bar

MAX_STRESS_SCENARIOS = 32
MAX_STRESS_BARS = 100_000
MAX_STRESS_PACKETS = 10_000


class StressScenario(BaseModel):
    model_config = ConfigDict(frozen=True)

    scenario_id: str = Field(min_length=1, max_length=80)
    entry_delay_bars: int = Field(default=0, ge=0, le=60)
    cost_multiplier: int = Field(default=1, ge=1, le=10)


class StressResult(BaseModel):
    scenario_id: str
    entry_delay_bars: int
    cost_multiplier: int
    final_equity_micros: int
    max_drawdown_micros: int
    fill_count: int
    completed_episode_count: int
    attempted_packet_count: int
    executed_packet_count: int
    tail_dropped_packet_count: int
    collision_skipped_packet_count: int
    ledger_root_hash: str
    curve_hash: str
    assumption: str = "modelled_counterfactual_not_observed_execution"


def _curve_hash(points) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(
            [point.model_dump(mode="json") for point in points],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _stressed_packets(
    bars: list[Bar], packets: list[DecisionPacket], scenario: StressScenario
) -> tuple[list[DecisionPacket], int]:
    index_by_time = {bar.ts: bar.index for bar in bars}
    stressed: list[DecisionPacket] = []
    tail_dropped = 0
    for packet in packets:
        entry_index = index_by_time.get(packet.signal.earliest_execution_at_utc)
        if entry_index is None:
            raise ValueError(
                f"packet {packet.packet_id} execution timestamp does not resolve to a bar"
            )
        shifted_index = entry_index + scenario.entry_delay_bars
        if shifted_index + packet.strategy.hold_bars >= len(bars):
            tail_dropped += 1
            continue
        shifted_at = bars[shifted_index].ts
        policy = packet.strategy.execution_cost_policy
        stressed_policy = (
            policy
            if scenario.cost_multiplier == 1
            else policy.model_copy(
                update={
                    "version": f"{policy.version}-x{scenario.cost_multiplier}",
                    "half_spread_ppm": policy.half_spread_ppm * scenario.cost_multiplier,
                    "slippage_ppm": policy.slippage_ppm * scenario.cost_multiplier,
                    "commission_per_fill_micros": (
                        policy.commission_per_fill_micros * scenario.cost_multiplier
                    ),
                }
            )
        )
        stressed_strategy = packet.strategy.model_copy(
            update={
                "execution_cost_policy": stressed_policy,
            }
        )
        stressed.append(
            packet.model_copy(
                update={
                    "strategy": stressed_strategy,
                    "signal": packet.signal.model_copy(
                        update={"earliest_execution_at_utc": shifted_at}
                    ),
                }
            )
        )
    return stressed, tail_dropped


def run_execution_stress(
    bars: list[Bar],
    packets: list[DecisionPacket],
    scenarios: list[StressScenario],
) -> list[StressResult]:
    if not bars or len(bars) < 2:
        raise ValueError("execution stress requires at least two bars")
    if len(bars) > MAX_STRESS_BARS:
        raise ValueError(f"execution stress bars exceed {MAX_STRESS_BARS}")
    if any(bar.index != position for position, bar in enumerate(bars)):
        raise ValueError("execution stress requires dense bar indices matching list position")
    if any(left.ts >= right.ts for left, right in zip(bars, bars[1:])):
        raise ValueError("execution stress requires strictly increasing unique bar timestamps")
    if not packets:
        raise ValueError("execution stress requires decision packets")
    if len(packets) > MAX_STRESS_PACKETS:
        raise ValueError(f"execution stress packets exceed {MAX_STRESS_PACKETS}")
    packet_ids = [packet.packet_id for packet in packets]
    if len(packet_ids) != len(set(packet_ids)):
        raise ValueError("execution stress requires unique packet ids")
    if not 1 <= len(scenarios) <= MAX_STRESS_SCENARIOS:
        raise ValueError(f"stress scenarios must contain 1-{MAX_STRESS_SCENARIOS} rows")
    ids = [scenario.scenario_id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("stress scenario ids must be unique")

    out: list[StressResult] = []
    for scenario in scenarios:
        stressed_packets, tail_dropped = _stressed_packets(bars, packets, scenario)
        events = build_ledger(bars, stressed_packets)
        points = reduce_curve(events)
        fills = [event for event in events if event.type is EventType.ORDER_FILL]
        drawdowns = drawdown_micros(points)
        curve_hash = _curve_hash(points)
        executed_ids = {
            event.packet_id
            for event in fills
            if event.side is Side.BUY and event.packet_id
        }
        collision_skipped = len(stressed_packets) - len(executed_ids)
        result = StressResult(
                scenario_id=scenario.scenario_id,
                entry_delay_bars=scenario.entry_delay_bars,
                cost_multiplier=scenario.cost_multiplier,
                final_equity_micros=points[-1].equity_micros,
                max_drawdown_micros=min(drawdowns, default=0),
                fill_count=len(fills),
                completed_episode_count=len(fills) // 2,
                attempted_packet_count=len(packets),
                executed_packet_count=len(executed_ids),
                tail_dropped_packet_count=tail_dropped,
                collision_skipped_packet_count=collision_skipped,
                ledger_root_hash=ledger_root_hash(events),
                curve_hash=curve_hash,
        )
        if scenario.entry_delay_bars == 0 and scenario.cost_multiplier == 1:
            canonical_events = build_ledger(bars, packets)
            canonical_points = reduce_curve(canonical_events)
            if (
                result.ledger_root_hash != ledger_root_hash(canonical_events)
                or result.curve_hash != _curve_hash(canonical_points)
            ):
                raise ValueError("zero-delay 1x stress baseline diverged from canonical reduction")
        out.append(result)
    return out
