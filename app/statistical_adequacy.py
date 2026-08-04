"""Honest status for statistical controls; absence is never a clean zero."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.decision_packet import CheckStatus
from app.experiment import ExperimentManifest, SplitId, load_manifest
from app.ledger import EventType, LedgerEvent, Side, ledger_root_hash
from app.performance import SplitMetrics
from app.trial_ledger import TrialLedgerReceipt
from app.walk_forward import WalkForwardReceipt

MAX_STATISTICAL_EVENTS = 500_000


class StatisticalCheck(BaseModel):
    check_id: str
    status: CheckStatus
    observed: int | None = None
    required: int | None = None
    detail: str


class StatisticalAdequacyReceipt(BaseModel):
    split_id: SplitId
    episode_count: int
    excluded_cross_boundary_episodes: int
    ledger_root_hash: str
    manifest_hash: str
    checks: tuple[StatisticalCheck, ...] = Field(min_length=4, max_length=4)
    evidence_scope: str = "diagnostic_status_not_promotion_evidence"

    @property
    def acceptable(self) -> bool:
        return all(check.status is CheckStatus.PASS for check in self.checks)


def assess_statistical_adequacy(
    metrics: list[SplitMetrics],
    events: list[LedgerEvent],
    manifest: ExperimentManifest,
    *,
    expected_ledger_root_hash: str,
    trial_ledger: TrialLedgerReceipt | None = None,
    walk_forward: WalkForwardReceipt | None = None,
) -> StatisticalAdequacyReceipt:
    if len(events) > MAX_STATISTICAL_EVENTS:
        raise ValueError(f"statistical event rows exceed {MAX_STATISTICAL_EVENTS}")
    test = next((row for row in metrics if row.split_id is SplitId.TEST), None)
    if test is None:
        raise ValueError("statistical adequacy requires observed TEST metrics")
    frozen_manifest, manifest_hash = load_manifest()
    from app.promotion import POLICY

    if manifest != frozen_manifest or manifest_hash != POLICY["experiment_manifest_hash"]:
        raise ValueError("statistical adequacy requires the policy-bound experiment manifest")
    actual_ledger_root = ledger_root_hash(events)
    if actual_ledger_root != expected_ledger_root_hash:
        raise ValueError("statistical adequacy requires the bound ledger event stream")
    test_window = next(window for window in manifest.split_windows if window.split_id is SplitId.TEST)
    fill_sides: dict[str, set[Side]] = {}
    fill_membership: dict[str, list[bool]] = {}
    for event in events:
        if event.type is not EventType.ORDER_FILL or not event.packet_id or event.side is None:
            continue
        inside = test_window.start_utc <= event.at_utc <= test_window.end_utc
        fill_membership.setdefault(event.packet_id, []).append(inside)
        if inside:
            fill_sides.setdefault(event.packet_id, set()).add(event.side)
    cross_boundary = {
        packet_id
        for packet_id, membership in fill_membership.items()
        if any(membership) and not all(membership)
    }
    count = sum(
        sides == {Side.BUY, Side.SELL} and packet_id not in cross_boundary
        for packet_id, sides in fill_sides.items()
    )
    minimum = manifest.minimum_confidence_episodes
    confidence_detail = (
        f"TEST has {count} episodes; frozen minimum is {minimum}."
        if count < minimum
        else "episode-level outcome samples are not yet carried on this receipt."
    )
    return StatisticalAdequacyReceipt(
        split_id=SplitId.TEST,
        episode_count=count,
        excluded_cross_boundary_episodes=len(cross_boundary),
        ledger_root_hash=actual_ledger_root,
        manifest_hash=manifest_hash,
        checks=(
            StatisticalCheck(
                check_id="test_confidence_interval",
                status=CheckStatus.NOT_RUN,
                observed=count,
                required=minimum,
                detail=confidence_detail,
            ),
            StatisticalCheck(
                check_id="candidate_trial_ledger",
                status=(CheckStatus.FAIL if trial_ledger else CheckStatus.NOT_RUN),
                observed=(trial_ledger.trial_count if trial_ledger else None),
                detail=(
                    f"current batch preserves {trial_ledger.trial_count} trials, but global "
                    "completeness and pre-test registration are unverified."
                    if trial_ledger
                    else "no immutable ledger of tried candidates and failures exists."
                ),
            ),
            StatisticalCheck(
                check_id="deflated_sharpe",
                status=CheckStatus.NOT_RUN,
                detail="requires return sampling plus the complete candidate-trial ledger.",
            ),
            StatisticalCheck(
                check_id="probability_of_backtest_overfitting",
                status=CheckStatus.NOT_RUN,
                observed=(len(walk_forward.rows[0].folds) if walk_forward else None),
                detail=(
                    "a 3-fold post-hoc matrix exists, but global trial completeness and "
                    "pre-test registration are unverified."
                    if walk_forward
                    else "requires a frozen candidate-by-purged-fold performance matrix."
                ),
            ),
        ),
    )
