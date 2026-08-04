"""Scenario coverage for point-in-time scientific registry replay."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.scientific_events import (
    ArchiveProvenance, MarketOutcomeLabel, PublicationVersion, ReplayStatus,
    ScientificEntityRefs, ScientificEvidenceEvent,
    TrialRecordVersion, TrialVersionEvent, content_hash, demo_events,
    evaluate_registry_backtest, load_trial_record_version_bytes,
    canonical_bytes,
)


def _replace(model, **updates):
    payload = model.model_dump(mode="python")
    payload.update(updates)
    return type(model).model_validate(payload)


def test_biotech_researcher_gets_25_replayed_diffs_without_a_laundered_pass():
    """A researcher can inspect the first 25-event artifact while every external absence stays visible."""

    run = evaluate_registry_backtest(demo_events())
    gate = run.gate
    assert gate.status == "BLOCKED"
    assert gate.trial_version_events_expected == gate.trial_version_events_replayed == 25
    assert gate.field_diffs_expected == gate.field_diffs_reproduced == 25
    assert gate.unsupported_scientific_facts == gate.post_as_of_documents == 0
    assert gate.market_outcome_labels_reproduced == 0
    assert gate.parametric_leakage_test == "NOT_APPLICABLE_TO_DETERMINISTIC_DIFF"
    assert set(gate.blockers) == {
        "ARCHIVE_REFETCH_AUTHORITY_NOT_IMPLEMENTED",
        "MARKET_OUTCOME_LABELS_INCOMPLETE",
        "PUBLIC_HISTORY_ARCHIVE_NOT_PROVEN",
    }
    assert all(diff.before_receipt.oracle_status == "reproduced" and
               diff.after_receipt.extraction_method == "deterministic_field"
               for diff in run.diffs)
    forged = gate.model_dump(mode="python")
    forged.update({"status": "PASS", "blockers": (), "promotion_ceiling": "RESEARCH_DIAGNOSTIC"})
    with pytest.raises(ValidationError, match="cannot award archive"):
        type(gate).model_validate(forged)


def test_historical_analyst_uses_public_api_and_ingestion_clocks_not_event_date():
    """A delayed client cannot move knowledge time backward to the sponsor's underlying event."""

    event = demo_events(1)[0]
    delayed = _replace(event.current, ingested_at_utc=event.current.api_available_at_utc + timedelta(hours=6))
    replay = _replace(event, current=delayed,
                      decision_at_utc=delayed.known_at_utc - timedelta(minutes=1))
    run = evaluate_registry_backtest([replay])
    assert delayed.known_at_utc == delayed.ingested_at_utc
    assert run.gate.post_as_of_documents == 1
    assert "POST_AS_OF_DOCUMENTS_PRESENT" in run.gate.blockers


def test_archive_operator_cannot_relabel_or_tamper_record_bytes_into_authority():
    """A malformed archive export fails before a field fact or version event can be emitted."""

    version = demo_events(1)[0].current
    payload = version.model_dump(mode="python")
    payload["record"]["protocolSection"]["statusModule"]["overallStatus"] = "WITHDRAWN"
    with pytest.raises(ValidationError, match="record_hash"):
        TrialRecordVersion.model_validate(payload)

    raw = b'{ "protocolSection": {"statusModule": {"overallStatus": "RECRUITING"}} }'
    metadata = version.model_dump(mode="python", exclude={"record", "record_hash", "source_document_hash"})
    loaded = load_trial_record_version_bytes(raw, **metadata)
    assert loaded.source_document_hash != loaded.record_hash
    with pytest.raises(ValueError, match="byte bound"):
        load_trial_record_version_bytes(b"x" * (1024 * 1024 + 1), **metadata)
    with pytest.raises(ValueError, match="repeats JSON key"):
        load_trial_record_version_bytes(b'{"a":1,"a":2}', **metadata)
    with pytest.raises(ValueError, match="Unicode normalization"):
        canonical_bytes({"é": 1, "e\u0301": 2})

    public_versions = [
        _replace(event, previous=_replace(event.previous, archive_provenance=ArchiveProvenance.PUBLIC_HISTORY_ARCHIVE),
                 current=_replace(event.current, archive_provenance=ArchiveProvenance.PUBLIC_HISTORY_ARCHIVE))
        for event in demo_events()
    ]
    labels = [_label(event) for event in public_versions]
    gate = evaluate_registry_backtest(public_versions, labels).gate
    assert gate.status == "BLOCKED"
    assert gate.blockers == ("ARCHIVE_REFETCH_AUTHORITY_NOT_IMPLEMENTED",)

    mismatch = _replace(public_versions[0], current=_replace(
        public_versions[0].current, archive_replay_status=ReplayStatus.MISMATCH))
    mismatch_gate = evaluate_registry_backtest([mismatch]).gate
    assert mismatch_gate.version_hash_mismatches == 1
    assert mismatch_gate.trial_version_events_replayed == 0
    assert mismatch_gate.field_diffs_reproduced == 0
    assert {"VERSION_HASH_MISMATCH", "TRIAL_VERSION_REPLAY_INCOMPLETE"} <= set(mismatch_gate.blockers)


def test_reconciliation_reviewer_sees_unsupported_changes_instead_of_silent_cleaning():
    """A title change outside the frozen four-field contract is counted and blocks the replay."""

    event = demo_events(1)[0]
    prior_record = event.previous.model_dump(mode="python")["record"]
    current_record = event.current.model_dump(mode="python")["record"]
    prior_record["protocolSection"]["identificationModule"] = {"briefTitle": "Before"}
    current_record["protocolSection"]["identificationModule"] = {"briefTitle": "After"}
    previous = _replace(event.previous, record=prior_record, record_hash=content_hash(prior_record))
    current = _replace(event.current, record=current_record, record_hash=content_hash(current_record))
    run = evaluate_registry_backtest([_replace(event, previous=previous, current=current)])
    assert run.gate.unsupported_scientific_facts == 1
    assert run.unsupported_paths == (
        f"{event.event_id}:/protocolSection/identificationModule/briefTitle",
    )
    assert "UNSUPPORTED_SCIENTIFIC_FACTS_PRESENT" in run.gate.blockers


def _label(event: TrialVersionEvent) -> MarketOutcomeLabel:
    return MarketOutcomeLabel(
        event_id=event.event_id, one_day_abnormal_return=0.01,
        five_day_abnormal_return=-0.02,
        one_day_observed_at_utc=event.decision_at_utc + timedelta(days=1),
        five_day_observed_at_utc=event.decision_at_utc + timedelta(days=5),
        benchmark_symbol="SPY", market_source_hash="sha256:" + "a" * 64,
        replay_status=ReplayStatus.REPRODUCED,
    )


def test_market_analyst_cannot_attach_foreknowledge_or_duplicate_outcomes():
    """Outcome labels exist after packet lock and refer one-to-one to registered events."""

    event = demo_events(1)[0]
    early = _label(event).model_dump(mode="python")
    early["one_day_observed_at_utc"] = event.decision_at_utc
    with pytest.raises(ValueError, match="strictly after"):
        evaluate_registry_backtest([event], [MarketOutcomeLabel.model_validate(early)])
    label = _label(event)
    with pytest.raises(ValueError, match="uniquely"):
        evaluate_registry_backtest([event], [label, label])


def test_registry_chain_rejects_cross_trial_or_nonconsecutive_versions():
    """A curator cannot compare convenient nonadjacent versions and call the result an event."""

    left, right = demo_events(2)
    with pytest.raises(ValidationError, match="share one NCT"):
        _replace(left, current=right.current)
    skipped = _replace(left.current, prior_version_id="version-zero")
    with pytest.raises(ValidationError, match="consecutive"):
        _replace(left, current=skipped)


def test_sustained_250_event_replay_is_bounded_and_byte_deterministic():
    """A long-running curator batch accumulates state without changing roots across replays."""

    events = demo_events(250)
    first = evaluate_registry_backtest(events)
    second = evaluate_registry_backtest(events)
    assert first.gate.event_root_hash == second.gate.event_root_hash
    assert first.gate.diff_root_hash == second.gate.diff_root_hash
    assert len(first.diffs) == 250
    with pytest.raises(ValueError, match="1-1000"):
        evaluate_registry_backtest(demo_events(1_000) + (demo_events(1)[0],))


def test_publication_monitor_uses_source_availability_not_one_ambiguous_publication_date():
    """A PubMed-only policy waits for the configured source even when the publisher was earlier."""

    event = demo_events(1)[0]
    publication = PublicationVersion(
        source="PUBMED", source_id="PMID-1", version_id="v1",
        version_kind="AHEAD_OF_PRINT", public_at_utc=event.previous.posted_at_utc,
        source_available_at_utc=event.previous.posted_at_utc + timedelta(days=2),
        ingested_at_utc=event.previous.posted_at_utc + timedelta(days=2, hours=3),
        document_hash="sha256:" + "b" * 64,
        source_locator="https://pubmed.ncbi.nlm.nih.gov/1/",
    )
    assert publication.known_at_utc == publication.ingested_at_utc
    evidence = ScientificEvidenceEvent(
        event_id="pubmed-indexed-1", source="pubmed",
        entity_refs=ScientificEntityRefs(nct_ids=(event.previous.nct_id,), pmids=("PMID-1",)),
        event_type="pubmed_indexed", event_time_utc=event.previous.posted_at_utc,
        public_at_utc=publication.public_at_utc, known_at_utc=publication.known_at_utc,
        ingested_at_utc=publication.ingested_at_utc, version_id=publication.version_id,
        payload_hash=publication.document_hash, source_schema_version="pubmed-xml-2026",
    )
    assert evidence.known_at_utc > evidence.public_at_utc
