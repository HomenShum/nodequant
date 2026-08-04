# Scientific information plane — temporal authority contract

## Decision

NodeQuant treats registry records and publications as versioned scientific
evidence, separate from market data. A historical decision may use only the
version available through its configured source at that time. Event date,
sponsor submission, public posting, source availability and local ingestion are
not interchangeable.

The first implemented slice is deterministic ClinicalTrials.gov consecutive-
version reconciliation. It does not fetch the undocumented internal history API
and cannot promote synthetic or caller-labelled archive bytes.

## Source clocks

For a ClinicalTrials.gov version:

```text
known_at = max(
  posted_at,
  api_available_at + configured_latency,
  ingested_at when present
)
```

ClinicalTrials.gov documents weekday API refreshes, generally by 09:00 ET, and
instructs clients to inspect `/api/v2/version::dataTimestamp` before treating a
refresh as current. Its documented v2 endpoint serves the current study record;
record history is visible on the study website, but no supported history
operation appears in the published v2 OpenAPI specification.

The API's August 26, 2025 modernization changed markup formatting and some
location/geographic processing, so every version records a source-pipeline
version. Sources:

- https://clinicaltrials.gov/data-api/api
- https://clinicaltrials.gov/api/oas/v2/ctg-oas-v2.yaml
- https://clinicaltrials.gov/data-api/about-api/api-migration
- https://clinicaltrials.gov/study/NCT03472833?a=4&tab=history

For a publication, `known_at` is source-specific. PubMed distinguishes entry,
electronic, print, ahead-of-print, MeSH and modification dates; its daily update
files can add, revise or delete citations. Crossref update relations identify
corrections and retractions but do not by themselves establish earliest public
availability.

- https://pubmed.ncbi.nlm.nih.gov/help/
- https://www.ncbi.nlm.nih.gov/books/NBK3828/
- https://pubmed.ncbi.nlm.nih.gov/download/
- https://www.crossref.org/documentation/retrieve-metadata/rest-api/rest-api-filters/

## Implemented contract

`app/scientific_events.py` provides bounded strict models for:

- `TrialRecordVersion` with five clocks, exact record hash, schema/pipeline
  versions, prior-version linkage and archive-replay status;
- `PublicationVersion` with source-specific availability;
- `ScientificEvidenceEvent` with separate event/public/known/ingestion clocks;
- `ScientificFactReceipt` with exact JSON path, page, paragraph or table cell;
- deterministic diffs for status, enrollment, phase and primary outcomes;
- market outcome labels that must occur after the frozen decision;
- a derived replay gate with absence represented as blockers, never zero/PASS.

The 25-event shape fixture produces 25 replayed events and 25 exact diffs, but
returns `BLOCKED` because it is synthetic, has no provider re-fetch authority,
and has no subsequent market labels.

## Evidence ceiling

`PUBLIC_HISTORY_ARCHIVE` is a descriptive input classification, not an authority
switch. Even 25 caller-labelled public versions plus 25 market labels remain
blocked by `ARCHIVE_REFETCH_AUTHORITY_NOT_IMPLEMENTED`. Clearing it requires a
separate bounded controller that:

1. reads a frozen 25-event manifest;
2. re-fetches each public locator through a supported provider surface or checks
   supplied immutable archive bytes against an external checkpoint;
3. binds public/API/ingestion timestamps and payload hashes;
4. independently replays each market label;
5. emits an externally anchored receipt unavailable to the strategy optimizer.

Named historical LLM interpretation remains prospective-shadow only. Entity-
blinded comparison is not implemented and point-in-time retrieval alone cannot
remove parametric knowledge of later scientific outcomes.

## Reproduction

```bash
uv run nodequant-science --demo
uv run pytest -q tests/test_scientific_events.py
```
