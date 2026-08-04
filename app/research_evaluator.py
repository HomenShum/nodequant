"""Independent episode sampler for the non-authoritative research-loop diagnostic."""

from __future__ import annotations

import math
from statistics import fmean, stdev

from pydantic import BaseModel, ConfigDict, model_validator

from app.decision_packet import ExecutionCostPolicy, Strategy
from app.experiment import RobustnessVariant, SplitId, SplitWindow, WalkForwardFold
from app.ledger import MICROS, to_micros
from app.proof_kernel import Bar

PACKET_LIMIT = 25


class AccessibleDiagnosticPlan(BaseModel):
    """The complete child input plan; forbidden split geometry cannot fit."""

    model_config = ConfigDict(frozen=True)
    split_windows: tuple[SplitWindow, ...]
    walk_forward_folds: tuple[WalkForwardFold, ...]
    expected_development_net_pnl_micros: int | None
    expected_validation_net_pnl_micros: int | None
    expected_development_episode_count: int
    expected_validation_episode_count: int

    @model_validator(mode="after")
    def accessible_only(self) -> "AccessibleDiagnosticPlan":
        if {window.split_id for window in self.split_windows} != {SplitId.DEVELOPMENT, SplitId.VALIDATION}:
            raise ValueError("diagnostic plan may contain only DEVELOPMENT and VALIDATION")
        return self


def _accessible_split(at_utc, plan: AccessibleDiagnosticPlan) -> SplitId | None:
    for window in plan.split_windows:
        if window.start_utc <= at_utc <= window.end_utc:
            return window.split_id
    return None


class DiagnosticScore(BaseModel):
    model_config = ConfigDict(frozen=True)

    variant_id: str
    fold_sharpes: tuple[float | None, ...]
    fold_sharpe_p25: float | None
    two_times_cost_sharpe: float | None
    remove_best_five_sharpe: float | None
    diagnostic_robust_sharpe_floor: float | None
    accessible_episode_count: int
    development_episode_count: int
    validation_episode_count: int
    aggregate_matches_frozen_receipt: bool
    annualization: str = "none_episode_level"
    evidence_scope: str = "synthetic_diagnostic_not_authoritative_score"


def _sma(bars: list[Bar], index: int, window: int) -> float:
    return round(sum(bar.close for bar in bars[index - window + 1 : index + 1]) / window, 9)


def signal_indices(bars: list[Bar], variant: RobustnessVariant) -> list[int]:
    """Second implementation of the frozen candidate predicate."""

    out: list[int] = []
    previous = False
    for index, bar in enumerate(bars):
        if index < variant.slow_window:
            current = False
        else:
            fast = _sma(bars, index, variant.fast_window)
            slow = _sma(bars, index, variant.slow_window)
            current = (
                (not variant.use_uptrend or fast > slow)
                and (not variant.use_pullback or bar.close < fast)
            )
        if current and not previous:
            out.append(index)
        previous = current
    return out[:PACKET_LIMIT]


def _round_adverse(reference_micros: int, ppm: int) -> int:
    quotient, remainder = divmod(reference_micros * ppm, MICROS)
    if remainder * 2 > MICROS or (remainder * 2 == MICROS and quotient % 2):
        quotient += 1
    return max(quotient, 1)


def episode_pnl(entry: Bar, exit: Bar, qty: int, policy: ExecutionCostPolicy) -> int:
    entry_ref = to_micros(entry.close)
    exit_ref = to_micros(exit.close)
    entry_price = entry_ref + _round_adverse(entry_ref, policy.half_spread_ppm) + _round_adverse(
        entry_ref, policy.slippage_ppm
    )
    exit_price = exit_ref - _round_adverse(exit_ref, policy.half_spread_ppm) - _round_adverse(
        exit_ref, policy.slippage_ppm
    )
    return (exit_price - entry_price) * qty - 2 * policy.commission_per_fill_micros


def _cost_policy(policy: ExecutionCostPolicy, multiplier: int) -> ExecutionCostPolicy:
    return policy.model_copy(
        update={
            "version": f"{policy.version}-x{multiplier}",
            "half_spread_ppm": policy.half_spread_ppm * multiplier,
            "slippage_ppm": policy.slippage_ppm * multiplier,
            "commission_per_fill_micros": policy.commission_per_fill_micros * multiplier,
        }
    )


def _split_episodes(
    bars: list[Bar],
    variant: RobustnessVariant,
    plan: AccessibleDiagnosticPlan,
    strategy: Strategy,
    policy: ExecutionCostPolicy,
) -> dict[SplitId, list[int]]:
    values: dict[SplitId, list[int]] = {}
    busy_through = -1
    for signal_index in signal_indices(bars, variant):
        entry_index = signal_index + 1
        exit_index = entry_index + variant.hold_bars
        if entry_index <= busy_through or exit_index >= len(bars):
            continue
        busy_through = exit_index
        entry_split = _accessible_split(bars[entry_index].ts, plan)
        exit_split = _accessible_split(bars[exit_index].ts, plan)
        if entry_split is None or entry_split is not exit_split:
            continue
        values.setdefault(entry_split, []).append(
            episode_pnl(bars[entry_index], bars[exit_index], strategy.position_qty, policy)
        )
    return values


def _fold_episodes(
    bars: list[Bar],
    variant: RobustnessVariant,
    strategy: Strategy,
    start,
    end,
) -> list[int]:
    values: list[int] = []
    busy_through = -1
    for signal_index in signal_indices(bars, variant):
        entry_index = signal_index + 1
        exit_index = entry_index + variant.hold_bars
        if exit_index >= len(bars):
            continue
        if not (start <= bars[entry_index].ts and bars[exit_index].ts <= end):
            continue
        if entry_index <= busy_through:
            continue
        busy_through = exit_index
        values.append(
            episode_pnl(
                bars[entry_index],
                bars[exit_index],
                strategy.position_qty,
                strategy.execution_cost_policy,
            )
        )
    return values


def sample_sharpe(values: list[int], minimum: int) -> float | None:
    if len(values) < minimum:
        return None
    deviation = stdev(values)
    if deviation == 0:
        return None
    value = fmean(values) / deviation
    return round(value, 12) if math.isfinite(value) else None


def percentile_25(values: list[float]) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * 0.25
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 12)


def evaluate_diagnostic_score(
    bars: list[Bar],
    variant: RobustnessVariant,
    plan: AccessibleDiagnosticPlan,
    strategy: Strategy,
    *,
    minimum_episodes_per_fold: int,
    cost_multiplier: int,
    remove_best_episode_count: int,
) -> DiagnosticScore:
    base = _split_episodes(bars, variant, plan, strategy, strategy.execution_cost_policy)
    doubled = _split_episodes(
        bars,
        variant,
        plan,
        strategy,
        _cost_policy(strategy.execution_cost_policy, cost_multiplier),
    )
    accessible = base.get(SplitId.DEVELOPMENT, []) + base.get(SplitId.VALIDATION, [])
    accessible_doubled = doubled.get(SplitId.DEVELOPMENT, []) + doubled.get(SplitId.VALIDATION, [])
    fold_sharpes = tuple(
        sample_sharpe(
            _fold_episodes(
                bars,
                variant,
                strategy,
                fold.evaluation_start_utc,
                fold.evaluation_end_utc,
            ),
            minimum_episodes_per_fold,
        )
        for fold in plan.walk_forward_folds
    )
    fold_p25 = (
        percentile_25([value for value in fold_sharpes if value is not None])
        if all(value is not None for value in fold_sharpes)
        else None
    )
    doubled_sharpe = sample_sharpe(accessible_doubled, minimum_episodes_per_fold)
    reduced = sorted(accessible, reverse=True)[remove_best_episode_count:]
    reduced_sharpe = sample_sharpe(reduced, minimum_episodes_per_fold)
    components = (fold_p25, doubled_sharpe, reduced_sharpe)
    floor = round(min(components), 12) if all(value is not None for value in components) else None
    development = base.get(SplitId.DEVELOPMENT, [])
    validation = base.get(SplitId.VALIDATION, [])
    aggregate_matches = (
        (sum(development) if development else None) == plan.expected_development_net_pnl_micros
        and (sum(validation) if validation else None) == plan.expected_validation_net_pnl_micros
        and len(development) == plan.expected_development_episode_count
        and len(validation) == plan.expected_validation_episode_count
    )
    return DiagnosticScore(
        variant_id=variant.variant_id,
        fold_sharpes=fold_sharpes,
        fold_sharpe_p25=fold_p25,
        two_times_cost_sharpe=doubled_sharpe,
        remove_best_five_sharpe=reduced_sharpe,
        diagnostic_robust_sharpe_floor=floor,
        accessible_episode_count=len(accessible),
        development_episode_count=len(development),
        validation_episode_count=len(validation),
        aggregate_matches_frozen_receipt=aggregate_matches,
    )
