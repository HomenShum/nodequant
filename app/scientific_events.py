"""Point-in-time scientific evidence contracts and deterministic registry replay.

The model may choose questions and explain already-sealed differences.  It may
not author a registry value, a timestamp, a field difference, or a market
outcome.  Historical versions enter through frozen snapshots; the undocumented
ClinicalTrials.gov internal history endpoint is deliberately not a source of
authority here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator

MAX_VERSION_BYTES = 1024 * 1024
MAX_EVENTS = 1_000
MAX_DIFFS_PER_EVENT = 256
MAX_LOCATION_TEXT = 500
Hash = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
NctId = Annotated[str, Field(pattern=r"^NCT[0-9]{8}$")]
Ref = Annotated[str, Field(min_length=1, max_length=200)]
FactValue = StrictBool | StrictInt | StrictFloat | StrictStr | None | list[Any] | dict[str, Any]

SUPPORTED_TRIAL_FIELDS = (
    "/protocolSection/statusModule/overallStatus",
    "/protocolSection/designModule/enrollmentInfo/count",
    "/protocolSection/designModule/phases",
    "/protocolSection/outcomesModule/primaryOutcomes",
)


class StrictModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _normalise(value: Any) -> Any:
    if isinstance(value, dict):
        normalised: dict[str, Any] = {}
        for key, item in value.items():
            canonical_key = unicodedata.normalize("NFC", str(key))
            if canonical_key in normalised:
                raise ValueError("scientific evidence keys collide after Unicode normalization")
            normalised[canonical_key] = _normalise(item)
        return dict(sorted(normalised.items()))
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    if isinstance(value, datetime):
        return _aware(value, "canonical timestamp").isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("scientific evidence cannot contain non-finite numbers")
        return 0.0 if value == 0.0 else value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, BaseModel):
        return _normalise(value.model_dump(mode="python"))
    raise ValueError(f"unsupported scientific evidence value: {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(_normalise(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def content_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


class ArchiveProvenance(str, Enum):
    PUBLIC_HISTORY_ARCHIVE = "PUBLIC_HISTORY_ARCHIVE"
    CAPTURED_PROVIDER_EXPORT = "CAPTURED_PROVIDER_EXPORT"
    SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"
    UNSUPPORTED_INTERNAL_ENDPOINT = "UNSUPPORTED_INTERNAL_ENDPOINT"


class ReplayStatus(str, Enum):
    REPRODUCED = "REPRODUCED"
    MISMATCH = "MISMATCH"
    NOT_RUN = "NOT_RUN"


class TrialRecordVersion(StrictModel):
    nct_id: NctId
    version_id: Ref
    submitted_at_utc: datetime | None = None
    posted_at_utc: datetime
    api_available_at_utc: datetime
    ingested_at_utc: datetime | None = None
    configured_latency_seconds: int = Field(default=0, ge=0, le=7 * 24 * 60 * 60)
    record_verification_date: str | None = Field(default=None, max_length=20)
    record: dict[str, Any]
    prior_version_id: Ref | None = None
    source_document_hash: Hash
    record_hash: Hash
    schema_version: Ref
    source_pipeline_version: Ref
    source_locator: str = Field(min_length=1, max_length=500)
    archive_provenance: ArchiveProvenance
    archive_replay_status: ReplayStatus

    @property
    def known_at_utc(self) -> datetime:
        available = self.api_available_at_utc + timedelta(seconds=self.configured_latency_seconds)
        candidates = [self.posted_at_utc, available]
        if self.ingested_at_utc is not None:
            candidates.append(self.ingested_at_utc)
        return max(candidates).astimezone(timezone.utc)

    @model_validator(mode="after")
    def version_integrity(self) -> "TrialRecordVersion":
        posted = _aware(self.posted_at_utc, "posted_at_utc")
        available = _aware(self.api_available_at_utc, "api_available_at_utc")
        if available < posted:
            raise ValueError("API availability cannot precede public posting")
        if self.submitted_at_utc is not None and _aware(self.submitted_at_utc, "submitted_at_utc") > posted:
            raise ValueError("submission cannot follow public posting")
        if self.ingested_at_utc is not None and _aware(self.ingested_at_utc, "ingested_at_utc") < available:
            raise ValueError("ingestion cannot precede API availability")
        encoded = canonical_bytes(self.record)
        if len(encoded) > MAX_VERSION_BYTES:
            raise ValueError("trial record version exceeds the byte bound")
        if self.record_hash != content_hash(self.record):
            raise ValueError("trial record bytes do not match record_hash")
        if self.nct_id not in self.source_locator or not self.source_locator.startswith("https://clinicaltrials.gov/"):
            raise ValueError("trial source locator must identify its ClinicalTrials.gov record")
        return self


def load_trial_record_version_bytes(raw: bytes, **metadata: Any) -> TrialRecordVersion:
    """Parse one bounded UTF-8 JSON snapshot and bind both raw and canonical hashes."""

    if len(raw) > MAX_VERSION_BYTES:
        raise ValueError("trial record source document exceeds the byte bound")
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"trial record source document repeats JSON key: {key}")
            value[key] = item
        return value
    try:
        record = json.loads(raw, object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("trial record source document must be UTF-8 JSON") from exc
    if not isinstance(record, dict):
        raise ValueError("trial record source document must contain one JSON object")
    forbidden = {"record", "record_hash", "source_document_hash"} & set(metadata)
    if forbidden:
        raise ValueError("record bytes, canonical hash and source hash are loader-derived")
    return TrialRecordVersion.model_validate({
        **metadata, "record": record, "record_hash": content_hash(record),
        "source_document_hash": "sha256:" + hashlib.sha256(raw).hexdigest(),
    })


class PublicationSource(str, Enum):
    PUBMED = "PUBMED"
    PMC = "PMC"
    CROSSREF = "CROSSREF"
    PREPRINT = "PREPRINT"
    PUBLISHER = "PUBLISHER"


class PublicationVersion(StrictModel):
    source: PublicationSource
    source_id: Ref
    version_id: Ref
    version_kind: Literal["PREPRINT", "AHEAD_OF_PRINT", "EPUBLISH", "VERSION_OF_RECORD", "CORRECTION", "RETRACTION"]
    public_at_utc: datetime
    source_available_at_utc: datetime
    ingested_at_utc: datetime | None = None
    configured_latency_seconds: int = Field(default=0, ge=0, le=7 * 24 * 60 * 60)
    prior_version_id: Ref | None = None
    document_hash: Hash
    source_locator: str = Field(min_length=1, max_length=500)

    @property
    def known_at_utc(self) -> datetime:
        available = self.source_available_at_utc + timedelta(seconds=self.configured_latency_seconds)
        return max([available, self.ingested_at_utc] if self.ingested_at_utc else [available]).astimezone(timezone.utc)

    @model_validator(mode="after")
    def publication_clocks(self) -> "PublicationVersion":
        public = _aware(self.public_at_utc, "public_at_utc")
        available = _aware(self.source_available_at_utc, "source_available_at_utc")
        if available < public:
            raise ValueError("configured source availability cannot precede public availability")
        if self.ingested_at_utc is not None and _aware(self.ingested_at_utc, "ingested_at_utc") < available:
            raise ValueError("publication ingestion cannot precede source availability")
        return self


class ScientificSource(str, Enum):
    CLINICALTRIALS = "clinicaltrials"
    PUBMED = "pubmed"
    PMC = "pmc"
    CROSSREF = "crossref"
    PREPRINT = "preprint"
    PUBLISHER = "publisher"


class ScientificEntityRefs(StrictModel):
    nct_ids: tuple[NctId, ...] = Field(default=(), max_length=100)
    dois: tuple[Ref, ...] = Field(default=(), max_length=100)
    pmids: tuple[Ref, ...] = Field(default=(), max_length=100)
    interventions: tuple[Ref, ...] = Field(default=(), max_length=100)
    sponsors: tuple[Ref, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def unique_refs(self) -> "ScientificEntityRefs":
        for values in (self.nct_ids, self.dois, self.pmids, self.interventions, self.sponsors):
            if len(values) != len(set(values)):
                raise ValueError("scientific entity references must be unique")
        return self


class ScientificEvidenceEvent(StrictModel):
    event_id: Ref
    source: ScientificSource
    entity_refs: ScientificEntityRefs
    event_type: Literal["trial_registered", "trial_record_updated", "trial_results_posted",
                        "preprint_posted", "article_ahead_of_print", "article_published",
                        "pubmed_indexed", "article_corrected", "article_retracted"]
    event_time_utc: datetime | None = None
    public_at_utc: datetime
    known_at_utc: datetime
    ingested_at_utc: datetime | None = None
    version_id: Ref
    supersedes_version_id: Ref | None = None
    payload_hash: Hash
    source_schema_version: Ref

    @model_validator(mode="after")
    def disclosure_time_precedes_knowledge(self) -> "ScientificEvidenceEvent":
        public, known = _aware(self.public_at_utc, "public_at_utc"), _aware(self.known_at_utc, "known_at_utc")
        if known < public:
            raise ValueError("scientific evidence cannot be known before public disclosure")
        if self.ingested_at_utc is not None:
            ingested = _aware(self.ingested_at_utc, "ingested_at_utc")
            if ingested < public or known < ingested:
                raise ValueError("scientific ingestion must lie between disclosure and knowledge time")
        if self.event_time_utc is not None:
            _aware(self.event_time_utc, "event_time_utc")
        return self


class JsonPathLocation(StrictModel):
    kind: Literal["JSON_PATH"] = "JSON_PATH"
    json_path: str = Field(min_length=1, max_length=MAX_LOCATION_TEXT)


class PageLocation(StrictModel):
    kind: Literal["PAGE"] = "PAGE"
    page: int = Field(ge=1, le=100_000)
    bounding_box: tuple[float, float, float, float] | None = None


class ParagraphLocation(StrictModel):
    kind: Literal["PARAGRAPH"] = "PARAGRAPH"
    section: str = Field(min_length=1, max_length=200)
    paragraph: int = Field(ge=1, le=100_000)


class TableLocation(StrictModel):
    kind: Literal["TABLE"] = "TABLE"
    table: str = Field(min_length=1, max_length=100)
    row: str = Field(min_length=1, max_length=100)
    column: str = Field(min_length=1, max_length=100)


class ScientificFactReceipt(StrictModel):
    fact_id: Ref
    claim_type: Literal["trial_design", "endpoint", "enrollment", "effect_size", "confidence_interval", "p_value", "adverse_event", "trial_status", "publication_status"]
    value: FactValue
    unit: str | None = Field(default=None, max_length=40)
    source_type: Literal["clinicaltrials_record", "clinicaltrials_results", "protocol_pdf", "sap_pdf", "preprint", "journal_article", "pubmed_metadata", "pmc_full_text"]
    source_id: Ref
    source_version_id: Ref
    document_hash: Hash
    location: JsonPathLocation | PageLocation | ParagraphLocation | TableLocation
    public_at_utc: datetime
    known_at_utc: datetime
    extraction_method: Literal["deterministic_field", "table_parser", "model_extraction"]
    oracle_status: Literal["reproduced", "disputed", "not_independently_checked"]

    @model_validator(mode="after")
    def temporal_receipt(self) -> "ScientificFactReceipt":
        if _aware(self.known_at_utc, "known_at_utc") < _aware(self.public_at_utc, "public_at_utc"):
            raise ValueError("a scientific fact cannot be known before it is public")
        if len(canonical_bytes(self.value)) > MAX_VERSION_BYTES:
            raise ValueError("scientific fact value exceeds the byte bound")
        return self


class TrialFieldDiff(StrictModel):
    event_id: Ref
    json_path: str = Field(min_length=1, max_length=MAX_LOCATION_TEXT)
    before_value: FactValue
    after_value: FactValue
    before_receipt: ScientificFactReceipt
    after_receipt: ScientificFactReceipt
    diff_hash: Hash

    @model_validator(mode="after")
    def diff_integrity(self) -> "TrialFieldDiff":
        if self.before_value == self.after_value:
            raise ValueError("a field difference must change the value")
        payload = {"event_id": self.event_id, "json_path": self.json_path,
                   "before_value": self.before_value, "after_value": self.after_value,
                   "before_fact_id": self.before_receipt.fact_id,
                   "after_fact_id": self.after_receipt.fact_id}
        if self.diff_hash != content_hash(payload):
            raise ValueError("field difference does not match diff_hash")
        return self


class TrialVersionEvent(StrictModel):
    event_id: Ref
    previous: TrialRecordVersion
    current: TrialRecordVersion
    decision_at_utc: datetime
    linked_symbol: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9.-]{0,19}$")

    @model_validator(mode="after")
    def consecutive_versions(self) -> "TrialVersionEvent":
        decision = _aware(self.decision_at_utc, "decision_at_utc")
        if self.previous.nct_id != self.current.nct_id:
            raise ValueError("trial event versions must share one NCT id")
        if self.current.prior_version_id != self.previous.version_id:
            raise ValueError("trial event versions must be consecutive")
        if self.current.known_at_utc <= self.previous.known_at_utc:
            raise ValueError("trial versions must advance public knowledge time")
        if decision < self.previous.known_at_utc:
            raise ValueError("decision cannot precede the prior public version")
        return self


class MarketOutcomeLabel(StrictModel):
    event_id: Ref
    one_day_abnormal_return: float
    five_day_abnormal_return: float
    one_day_observed_at_utc: datetime
    five_day_observed_at_utc: datetime
    benchmark_symbol: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,19}$")
    market_source_hash: Hash
    replay_status: ReplayStatus

    @model_validator(mode="after")
    def finite_returns(self) -> "MarketOutcomeLabel":
        if not math.isfinite(self.one_day_abnormal_return) or not math.isfinite(self.five_day_abnormal_return):
            raise ValueError("market outcome returns must be finite")
        _aware(self.one_day_observed_at_utc, "one_day_observed_at_utc")
        _aware(self.five_day_observed_at_utc, "five_day_observed_at_utc")
        if self.five_day_observed_at_utc <= self.one_day_observed_at_utc:
            raise ValueError("five-day observation must follow one-day observation")
        return self


class ScientificReplayGate(StrictModel):
    status: Literal["PASS", "BLOCKED"]
    trial_version_events_expected: int
    trial_version_events_replayed: int
    field_diffs_expected: int
    field_diffs_reproduced: int
    unsupported_scientific_facts: int
    post_as_of_documents: int
    version_hash_mismatches: int
    market_outcome_labels_reproduced: int
    parametric_leakage_test: Literal["NOT_APPLICABLE_TO_DETERMINISTIC_DIFF"]
    promotion_ceiling: Literal["RESEARCH_DIAGNOSTIC", "PROSPECTIVE_SHADOW"]
    blockers: tuple[str, ...]
    event_root_hash: Hash
    diff_root_hash: Hash

    @model_validator(mode="after")
    def derived_status(self) -> "ScientificReplayGate":
        counters = (self.trial_version_events_expected, self.trial_version_events_replayed,
                    self.field_diffs_expected, self.field_diffs_reproduced,
                    self.unsupported_scientific_facts, self.post_as_of_documents,
                    self.version_hash_mismatches, self.market_outcome_labels_reproduced)
        if any(value < 0 for value in counters):
            raise ValueError("scientific replay counters cannot be negative")
        if self.trial_version_events_expected != 25:
            raise ValueError("the first scientific replay contract is frozen at 25 events")
        if self.field_diffs_reproduced > self.field_diffs_expected:
            raise ValueError("reproduced field differences cannot exceed expected differences")
        if "ARCHIVE_REFETCH_AUTHORITY_NOT_IMPLEMENTED" not in self.blockers:
            raise ValueError("local code cannot award archive re-fetch authority")
        if (self.status == "PASS") != (not self.blockers):
            raise ValueError("scientific replay status must be derived from blockers")
        if self.status != "BLOCKED" or self.promotion_ceiling != "PROSPECTIVE_SHADOW":
            raise ValueError("scientific replay cannot exceed prospective shadow locally")
        return self


class ScientificBacktestRun(StrictModel):
    gate: ScientificReplayGate
    diffs: tuple[TrialFieldDiff, ...] = Field(max_length=MAX_EVENTS * MAX_DIFFS_PER_EVENT)
    unsupported_paths: tuple[str, ...] = Field(max_length=MAX_EVENTS * MAX_DIFFS_PER_EVENT)


_MISSING = object()


def _pointer(record: dict[str, Any], path: str) -> Any:
    value: Any = record
    for part in path.strip("/").split("/"):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _leaf_paths(value: Any, prefix: str = "") -> set[str]:
    if isinstance(value, dict):
        out: set[str] = set()
        for key, item in value.items():
            out.update(_leaf_paths(item, f"{prefix}/{key}"))
        return out or {prefix or "/"}
    if isinstance(value, list):
        return {prefix or "/"}
    return {prefix or "/"}


def _claim_type(path: str) -> str:
    if path.endswith("overallStatus"):
        return "trial_status"
    if path.endswith("count"):
        return "enrollment"
    if path.endswith("primaryOutcomes"):
        return "endpoint"
    return "trial_design"


def _fact(event: TrialVersionEvent, version: TrialRecordVersion, path: str, value: Any,
          side: str) -> ScientificFactReceipt:
    return ScientificFactReceipt(
        fact_id=f"{event.event_id}.{side}.{hashlib.sha256(path.encode()).hexdigest()[:16]}",
        claim_type=_claim_type(path), value=None if value is _MISSING else value,
        source_type="clinicaltrials_record", source_id=version.nct_id,
        source_version_id=version.version_id, document_hash=version.source_document_hash,
        location=JsonPathLocation(json_path=path), public_at_utc=version.posted_at_utc,
        known_at_utc=version.known_at_utc, extraction_method="deterministic_field",
        oracle_status="reproduced" if version.archive_replay_status is ReplayStatus.REPRODUCED else "not_independently_checked",
    )


def diff_trial_event(event: TrialVersionEvent) -> tuple[tuple[TrialFieldDiff, ...], tuple[str, ...]]:
    diffs: list[TrialFieldDiff] = []
    for path in SUPPORTED_TRIAL_FIELDS:
        before = _pointer(event.previous.record, path)
        after = _pointer(event.current.record, path)
        if before is _MISSING and after is _MISSING:
            continue
        before_value, after_value = (None if before is _MISSING else before), (None if after is _MISSING else after)
        if before_value == after_value:
            continue
        before_receipt = _fact(event, event.previous, path, before, "before")
        after_receipt = _fact(event, event.current, path, after, "after")
        payload = {"event_id": event.event_id, "json_path": path,
                   "before_value": before_value, "after_value": after_value,
                   "before_fact_id": before_receipt.fact_id, "after_fact_id": after_receipt.fact_id}
        diffs.append(TrialFieldDiff(event_id=event.event_id, json_path=path,
                                    before_value=before_value, after_value=after_value,
                                    before_receipt=before_receipt, after_receipt=after_receipt,
                                    diff_hash=content_hash(payload)))
    if len(diffs) > MAX_DIFFS_PER_EVENT:
        raise ValueError("trial event exceeds the per-event difference bound")
    all_paths = _leaf_paths(event.previous.record) | _leaf_paths(event.current.record)
    changed = {path for path in all_paths
               if _pointer(event.previous.record, path) != _pointer(event.current.record, path)}
    unsupported = tuple(sorted(path for path in changed
                               if not any(path == supported or path.startswith(supported + "/")
                                          for supported in SUPPORTED_TRIAL_FIELDS)))
    return tuple(diffs), unsupported


def evaluate_registry_backtest(
    events: tuple[TrialVersionEvent, ...] | list[TrialVersionEvent],
    market_labels: tuple[MarketOutcomeLabel, ...] | list[MarketOutcomeLabel] = (),
) -> ScientificBacktestRun:
    events, market_labels = tuple(events), tuple(market_labels)
    if not 1 <= len(events) <= MAX_EVENTS:
        raise ValueError(f"scientific backtest requires 1-{MAX_EVENTS} events")
    event_ids = [event.event_id for event in events]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("scientific backtest event ids must be unique")
    label_ids = [label.event_id for label in market_labels]
    if len(label_ids) != len(set(label_ids)) or not set(label_ids) <= set(event_ids):
        raise ValueError("market labels must uniquely reference registered events")
    events_by_id = {event.event_id: event for event in events}
    for label in market_labels:
        decision = events_by_id[label.event_id].decision_at_utc
        if label.one_day_observed_at_utc <= decision or label.five_day_observed_at_utc <= decision:
            raise ValueError("market outcomes must be observed strictly after the frozen decision")

    all_diffs: list[TrialFieldDiff] = []
    unsupported: list[str] = []
    for event in events:
        diffs, paths = diff_trial_event(event)
        all_diffs.extend(diffs)
        unsupported.extend(f"{event.event_id}:{path}" for path in paths)
    reproduced_labels = sum(label.replay_status is ReplayStatus.REPRODUCED for label in market_labels)
    replayed_events = sum(
        event.previous.archive_replay_status is ReplayStatus.REPRODUCED and
        event.current.archive_replay_status is ReplayStatus.REPRODUCED
        for event in events
    )
    version_mismatches = sum(
        version.archive_replay_status is ReplayStatus.MISMATCH
        for event in events for version in (event.previous, event.current)
    )
    reproduced_diff_count = sum(
        diff.before_receipt.oracle_status == "reproduced" and
        diff.after_receipt.oracle_status == "reproduced" for diff in all_diffs
    )
    post_as_of = sum(event.current.known_at_utc > event.decision_at_utc for event in events)
    public_archive = all(
        version.archive_provenance is ArchiveProvenance.PUBLIC_HISTORY_ARCHIVE and
        version.archive_replay_status is ReplayStatus.REPRODUCED
        for event in events for version in (event.previous, event.current)
    )
    blockers: list[str] = []
    if len(events) != 25: blockers.append("TRIAL_VERSION_EVENT_COUNT_NOT_25")
    if replayed_events != len(events): blockers.append("TRIAL_VERSION_REPLAY_INCOMPLETE")
    if version_mismatches: blockers.append("VERSION_HASH_MISMATCH")
    if not public_archive: blockers.append("PUBLIC_HISTORY_ARCHIVE_NOT_PROVEN")
    # A caller-carried provenance label cannot certify that archive bytes can be
    # re-fetched.  This remains unreachable until a separate provider replay
    # controller binds locator, response bytes, and availability timestamp.
    blockers.append("ARCHIVE_REFETCH_AUTHORITY_NOT_IMPLEMENTED")
    if unsupported: blockers.append("UNSUPPORTED_SCIENTIFIC_FACTS_PRESENT")
    if post_as_of: blockers.append("POST_AS_OF_DOCUMENTS_PRESENT")
    if reproduced_labels != len(events): blockers.append("MARKET_OUTCOME_LABELS_INCOMPLETE")
    gate = ScientificReplayGate(
        status="PASS" if not blockers else "BLOCKED",
        trial_version_events_expected=25,
        trial_version_events_replayed=replayed_events,
        field_diffs_expected=len(all_diffs), field_diffs_reproduced=reproduced_diff_count,
        unsupported_scientific_facts=len(unsupported), post_as_of_documents=post_as_of,
        version_hash_mismatches=version_mismatches, market_outcome_labels_reproduced=reproduced_labels,
        parametric_leakage_test="NOT_APPLICABLE_TO_DETERMINISTIC_DIFF",
        promotion_ceiling="PROSPECTIVE_SHADOW",
        blockers=tuple(sorted(blockers)),
        event_root_hash=content_hash([event.model_dump(mode="json") for event in events]),
        diff_root_hash=content_hash([diff.model_dump(mode="json") for diff in all_diffs]),
    )
    return ScientificBacktestRun(gate=gate, diffs=tuple(all_diffs),
                                 unsupported_paths=tuple(sorted(unsupported)))


def _record(status: str, enrollment: int, phase: str, endpoint: str) -> dict[str, Any]:
    return {"protocolSection": {"statusModule": {"overallStatus": status},
            "designModule": {"enrollmentInfo": {"count": enrollment}, "phases": [phase]},
            "outcomesModule": {"primaryOutcomes": [{"measure": endpoint}]}}}


def demo_events(count: int = 25) -> tuple[TrialVersionEvent, ...]:
    """Deterministic shape fixture; never represented as historical evidence."""
    start = datetime(2020, 1, 1, 14, tzinfo=timezone.utc)
    events = []
    for index in range(count):
        nct = f"NCT{index + 1:08d}"
        prior_record = _record("RECRUITING", 100 + index, "PHASE2", "Endpoint A")
        current_record = _record("COMPLETED", 100 + index, "PHASE2", "Endpoint A")
        posted = start + timedelta(days=index * 2)
        common = {"nct_id": nct, "schema_version": "ctgov-v2",
                  "source_pipeline_version": "modernized-2025-08",
                  "source_locator": f"https://clinicaltrials.gov/study/{nct}?tab=history",
                  "archive_provenance": ArchiveProvenance.SYNTHETIC_FIXTURE,
                  "archive_replay_status": ReplayStatus.REPRODUCED}
        previous = TrialRecordVersion(**common, version_id="1", posted_at_utc=posted,
                    api_available_at_utc=posted + timedelta(hours=1), record=prior_record,
                    source_document_hash=content_hash(prior_record),
                    record_hash=content_hash(prior_record))
        current = TrialRecordVersion(**common, version_id="2", prior_version_id="1",
                    posted_at_utc=posted + timedelta(days=1),
                    api_available_at_utc=posted + timedelta(days=1, hours=1),
                    ingested_at_utc=posted + timedelta(days=1, hours=2), record=current_record,
                    source_document_hash=content_hash(current_record),
                    record_hash=content_hash(current_record))
        events.append(TrialVersionEvent(event_id=f"trial-version-{index + 1:03d}",
                      previous=previous, current=current,
                      decision_at_utc=current.known_at_utc + timedelta(minutes=1)))
    return tuple(events)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="run the 25-event synthetic shape fixture")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.demo:
        build_parser().error("--demo is required until an external version corpus is supplied")
    run = evaluate_registry_backtest(demo_events())
    print(json.dumps(run.gate.model_dump(mode="json"), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
