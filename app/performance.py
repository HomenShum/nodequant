"""Split-local performance facts; never blend development and test metrics."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.experiment import SplitId
from app.ledger import EquityPoint, EventType, LedgerEvent

MAX_PERFORMANCE_ROWS = 500_000


class SplitMetrics(BaseModel):
    split_id: SplitId
    point_count: int
    start_equity_micros: int
    end_equity_micros: int
    net_pnl_micros: int
    max_drawdown_micros: int
    peak_at_utc: datetime
    trough_at_utc: datetime
    recovered_at_utc: datetime | None
    decline_duration_seconds: int
    recovery_duration_seconds: int | None
    worst_drawdown_underwater_seconds: int
    total_time_underwater_seconds: int
    episodes_in_drawdown_interval: int
    episode_ids_in_drawdown_interval: tuple[str, ...] = Field(max_length=10_000)
    had_drawdown: bool
    observation_censored: bool
    sampled_at_event_times: bool = True


def split_metrics(
    points: list[EquityPoint],
    tags: list[SplitId | None],
    events: list[LedgerEvent],
) -> list[SplitMetrics]:
    """Measure every populated split with peaks reset at each boundary."""

    if len(points) != len(tags):
        raise ValueError("each equity point must have exactly one split tag")
    if len(points) > MAX_PERFORMANCE_ROWS or len(events) > MAX_PERFORMANCE_ROWS:
        raise ValueError(f"split performance rows exceed {MAX_PERFORMANCE_ROWS}")
    if any(tag is None for tag in tags):
        raise ValueError("split metrics are NOT_RUN when any equity point is untagged")
    out: list[SplitMetrics] = []
    for split_id in SplitId:
        indexed = [
            (index, point)
            for index, (point, tag) in enumerate(zip(points, tags))
            if tag is split_id
        ]
        if not indexed:
            continue
        split_points = [point for _, point in indexed]
        first_index = indexed[0][0]
        # Carry the last observed equity across the boundary. Resetting to the
        # first post-event point erased an entry fill at both validation and test.
        baseline_value = (
            points[first_index - 1].equity_micros
            if first_index > 0
            else split_points[0].equity_micros
        )
        peak_value = baseline_value
        peak_at = trough_at = split_points[0].at_utc
        max_drawdown = 0
        recovery_threshold = peak_value
        recovery_search_from = 0
        underwater = 0
        running_peak = peak_value
        running_peak_at = split_points[0].at_utc
        previous_at = split_points[0].at_utc
        previous_equity = baseline_value
        for position, point in enumerate(split_points):
            if previous_equity < running_peak:
                underwater += max(0, int((point.at_utc - previous_at).total_seconds()))
            if point.equity_micros > running_peak:
                running_peak = point.equity_micros
                running_peak_at = point.at_utc
            drawdown = point.equity_micros - running_peak
            if drawdown < max_drawdown:
                max_drawdown = drawdown
                trough_at = point.at_utc
                recovery_threshold = running_peak
                peak_at = running_peak_at
                recovery_search_from = position + 1
            previous_at = point.at_utc
            previous_equity = point.equity_micros
        recovered_at = (
            next(
                (
                    point.at_utc
                    for point in split_points[recovery_search_from:]
                    if point.equity_micros >= recovery_threshold
                ),
                None,
            )
            if max_drawdown < 0
            else None
        )
        interval_end = recovered_at or split_points[-1].at_utc
        fill_times: dict[str, list[datetime]] = {}
        for event in events:
            if event.type is EventType.ORDER_FILL and event.packet_id:
                fill_times.setdefault(event.packet_id, []).append(event.at_utc)
        # An episode can create the whole drawdown while neither fill lands
        # inside it (entry before the peak, exit after recovery). Count episode
        # spans that overlap the worst-drawdown interval, not merely fill stamps.
        episode_ids = (
            {
                packet_id
                for packet_id, stamps in fill_times.items()
                if min(stamps) <= interval_end and max(stamps) >= peak_at
            }
            if max_drawdown < 0
            else set()
        )
        if len(episode_ids) > 10_000:
            raise ValueError("drawdown episode ids exceed 10000")
        out.append(
            SplitMetrics(
                split_id=split_id,
                point_count=len(split_points),
                start_equity_micros=baseline_value,
                end_equity_micros=split_points[-1].equity_micros,
                net_pnl_micros=(
                    split_points[-1].equity_micros - baseline_value
                ),
                max_drawdown_micros=max_drawdown,
                peak_at_utc=peak_at,
                trough_at_utc=trough_at,
                recovered_at_utc=recovered_at,
                decline_duration_seconds=max(
                    0, int((trough_at - peak_at).total_seconds())
                ),
                recovery_duration_seconds=(
                    int((recovered_at - trough_at).total_seconds())
                    if recovered_at is not None
                    else None
                ),
                worst_drawdown_underwater_seconds=(
                    max(0, int((interval_end - peak_at).total_seconds()))
                    if max_drawdown < 0
                    else 0
                ),
                total_time_underwater_seconds=underwater,
                episodes_in_drawdown_interval=len(episode_ids),
                episode_ids_in_drawdown_interval=tuple(sorted(episode_ids)),
                had_drawdown=max_drawdown < 0,
                observation_censored=max_drawdown < 0 and recovered_at is None,
            )
        )
    return out
