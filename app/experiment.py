"""Frozen split geometry without pretending a repository file proves access control."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_MANIFEST_BYTES = 64 * 1024


class SplitId(str, Enum):
    DEVELOPMENT = "DEVELOPMENT"
    VALIDATION = "VALIDATION"
    TEST = "TEST"
    OPENED_HOLDOUT = "OPENED_HOLDOUT"


class SplitWindow(BaseModel):
    model_config = ConfigDict(frozen=True)

    split_id: SplitId
    start_utc: datetime
    end_utc: datetime
    promotion_eligible: bool

    @model_validator(mode="after")
    def valid_window(self) -> "SplitWindow":
        if self.start_utc.tzinfo is None or self.end_utc.tzinfo is None:
            raise ValueError("split timestamps must be timezone-aware")
        if self.end_utc < self.start_utc:
            raise ValueError("split end precedes start")
        return self


class RobustnessVariant(BaseModel):
    model_config = ConfigDict(frozen=True)

    variant_id: str = Field(min_length=1, max_length=80)
    fast_window: int = Field(ge=2, le=1_000)
    slow_window: int = Field(ge=3, le=2_000)
    hold_bars: int = Field(ge=1, le=100_000)
    use_uptrend: bool
    use_pullback: bool

    @model_validator(mode="after")
    def valid_variant(self) -> "RobustnessVariant":
        if self.fast_window >= self.slow_window:
            raise ValueError("robustness fast window must be below slow window")
        if not self.use_uptrend and not self.use_pullback:
            raise ValueError("robustness variant must retain at least one rule")
        return self


class WalkForwardFold(BaseModel):
    model_config = ConfigDict(frozen=True)

    fold_id: str = Field(min_length=1, max_length=40)
    train_start_utc: datetime
    train_end_utc: datetime
    evaluation_start_utc: datetime
    evaluation_end_utc: datetime
    purge_bars: int = Field(ge=1, le=10_000)

    @model_validator(mode="after")
    def valid_fold(self) -> "WalkForwardFold":
        stamps = (
            self.train_start_utc,
            self.train_end_utc,
            self.evaluation_start_utc,
            self.evaluation_end_utc,
        )
        if any(stamp.tzinfo is None or stamp.utcoffset() is None for stamp in stamps):
            raise ValueError("walk-forward timestamps must be timezone-aware")
        if not self.train_start_utc <= self.train_end_utc < self.evaluation_start_utc <= self.evaluation_end_utc:
            raise ValueError("walk-forward train/evaluation windows are not chronological")
        purged_minutes = int(
            (self.evaluation_start_utc - self.train_end_utc).total_seconds() // 60
        ) - 1
        if purged_minutes < self.purge_bars:
            raise ValueError("walk-forward gap is smaller than declared purge bars")
        return self


class ExperimentManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str
    strategy_id: str
    strategy_config_hash: str
    created_at_utc: datetime
    candidate_frozen_at_utc: datetime
    optimizer_accessible_splits: tuple[SplitId, ...] = Field(max_length=4)
    split_windows: tuple[SplitWindow, ...] = Field(min_length=4, max_length=4)
    rolling_sharpe_window: int | None = None
    null_trial_count: int = Field(ge=1, le=10_000)
    null_seed: int
    minimum_confidence_episodes: int = Field(ge=2, le=100_000)
    robustness_result_root: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    walk_forward_result_root: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    robustness_variants: tuple[RobustnessVariant, ...] = Field(min_length=9, max_length=32)
    walk_forward_folds: tuple[WalkForwardFold, ...] = Field(min_length=3, max_length=12)
    provenance: str

    @model_validator(mode="after")
    def valid_splits(self) -> "ExperimentManifest":
        for label, value in (
            ("created_at_utc", self.created_at_utc),
            ("candidate_frozen_at_utc", self.candidate_frozen_at_utc),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{label} must be timezone-aware")
        expected = list(SplitId)
        if [window.split_id for window in self.split_windows] != expected:
            raise ValueError("split windows must contain the four canonical splits in order")
        for left, right in zip(self.split_windows, self.split_windows[1:]):
            if left.end_utc >= right.start_utc:
                raise ValueError("split windows overlap or are not chronological")
        eligible = [window.split_id for window in self.split_windows if window.promotion_eligible]
        if eligible != [SplitId.TEST]:
            raise ValueError("TEST must be the only promotion-eligible split")
        ids = [variant.variant_id for variant in self.robustness_variants]
        if len(ids) != len(set(ids)):
            raise ValueError("robustness variant ids must be unique")
        if ids[0] != "baseline":
            raise ValueError("baseline must be the first robustness variant")
        fold_ids = [fold.fold_id for fold in self.walk_forward_folds]
        if len(fold_ids) != len(set(fold_ids)):
            raise ValueError("walk-forward fold ids must be unique")
        test_start = next(
            window.start_utc for window in self.split_windows if window.split_id is SplitId.TEST
        )
        if any(fold.evaluation_end_utc >= test_start for fold in self.walk_forward_folds):
            raise ValueError("walk-forward folds must end before TEST opens")
        if any(
            left.evaluation_end_utc >= right.evaluation_start_utc
            for left, right in zip(self.walk_forward_folds, self.walk_forward_folds[1:])
        ):
            raise ValueError("walk-forward evaluation windows overlap")
        return self


class SplitDisciplineReceipt(BaseModel):
    manifest_hash: str
    manifest_matches_policy: bool
    strategy_matches: bool
    chronological: bool
    point_count: int
    untagged_points: int
    candidate_frozen_before_test_declared: bool
    optimizer_test_access_declared: bool
    provenance_verified: bool

    @property
    def acceptable(self) -> bool:
        return (
            self.strategy_matches
            and self.manifest_matches_policy
            and self.chronological
            and self.point_count > 0
            and self.untagged_points == 0
            and self.candidate_frozen_before_test_declared
            and not self.optimizer_test_access_declared
            and self.provenance_verified
        )


def load_manifest(path: Path | None = None) -> tuple[ExperimentManifest, str]:
    path = path or Path(__file__).parents[1] / "experiment" / "manifest.json"
    with path.open("rb") as handle:
        raw = handle.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError(f"experiment manifest exceeds {MAX_MANIFEST_BYTES} bytes")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid experiment manifest JSON") from exc
    return ExperimentManifest.model_validate(payload), "sha256:" + hashlib.sha256(raw).hexdigest()


def split_for_timestamp(at_utc: datetime, manifest: ExperimentManifest) -> SplitId | None:
    for window in manifest.split_windows:
        if window.start_utc <= at_utc <= window.end_utc:
            return window.split_id
    return None


def assess_split_discipline(
    timestamps: list[datetime],
    strategy_identities: list[tuple[str, str]],
    expected_manifest_hash: str | None = None,
) -> tuple[SplitDisciplineReceipt, list[SplitId | None]]:
    manifest, digest = load_manifest()
    tags = [split_for_timestamp(timestamp, manifest) for timestamp in timestamps]
    test_start = next(
        window.start_utc for window in manifest.split_windows if window.split_id is SplitId.TEST
    )
    receipt = SplitDisciplineReceipt(
        manifest_hash=digest,
        manifest_matches_policy=(
            expected_manifest_hash is None or digest == expected_manifest_hash
        ),
        strategy_matches=bool(strategy_identities)
        and all(
            manifest.strategy_id == strategy_id
            and manifest.strategy_config_hash == strategy_config_hash
            for strategy_id, strategy_config_hash in strategy_identities
        ),
        chronological=all(left <= right for left, right in zip(timestamps, timestamps[1:])),
        point_count=len(timestamps),
        untagged_points=sum(tag is None for tag in tags),
        candidate_frozen_before_test_declared=manifest.candidate_frozen_at_utc < test_start,
        optimizer_test_access_declared=SplitId.TEST in manifest.optimizer_accessible_splits,
        # A repository-authored timestamp/access list cannot prove what a human
        # or optimizer actually saw. This stays false until an external run
        # controller issues a signed/immutable access record.
        provenance_verified=False,
    )
    return receipt, tags
