"""Entity-resolution invariants.

The system's one rule is that the model plans WHICH questions to ask and never
answers them: every number in a response is an API ``totalCount``. A retrieval
index adds a second way to break that rule — a bm25 rank and a cosine are both
floats, and a float is one careless assignment away from being rendered as a
count.

These tests are the fence. They are written as scenarios a competitive-
intelligence analyst actually runs ("how many Keytruda trials in melanoma"),
not as unit pokes, because the failure mode being guarded is a *pipeline*
failure: a plausible name proposed by retrieval flowing through to a chart
without ever having been counted.

Everything here is offline. The stub index is built with the REAL schema
imported from scripts/build_entity_index.py, so a schema change that breaks
the loader breaks these tests rather than only breaking production.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from app.ctgov import CTGovError, Filter
from app.entities import (
    INDEX_DIR, KINDS, MAX_SEMANTIC_ONLY, MAX_VERIFY_PROBES, Candidate,
    EntityIndex, IndexUnavailable, _fts_match_query, candidate_filter,
    clear_cache, content_tokens, fts5_available, index_status, load_index,
    normalize, resolve, resolve_verified, verify_candidates,
)

_SPEC = importlib.util.spec_from_file_location(
    "build_entity_index",
    Path(__file__).resolve().parent.parent / "scripts" / "build_entity_index.py",
)
_BUILD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_BUILD)


# ----------------------------------------------------------- stubbed fixtures


def _write_index(directory: Path, rows: list[dict]) -> None:
    """Build a miniature index with the production schema and no embeddings.

    Vectors are left NULL so these tests never need model2vec: the lexical
    retriever is the load-bearing one and must be provable on its own.
    """
    directory.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(directory / "entities.db")
    connection.executescript(_BUILD.SCHEMA)
    for rowid, entity in enumerate(rows, start=1):
        kind = entity["kind"]
        norm_canonical = normalize(entity["label"], kind)
        norm_aliases = [normalize(a, kind) for a in entity.get("aliases", [])]
        connection.execute(
            "INSERT INTO entities (rowid, kind, canonical, other_names, "
            "norm_canonical, norm_all, doc_freq, vector) VALUES (?,?,?,?,?,?,?,NULL)",
            (rowid, kind, entity["label"], " | ".join(entity.get("aliases", [])),
             norm_canonical, " ".join([norm_canonical, *norm_aliases]),
             entity.get("doc_freq", 1)),
        )
        for alias in {a for a in norm_aliases if a and a != norm_canonical}:
            connection.execute(
                "INSERT INTO entity_aliases (entity_rowid, kind, norm_alias) VALUES (?,?,?)",
                (rowid, kind, alias),
            )
    connection.commit()
    connection.close()
    (directory / "manifest.json").write_text(json.dumps({
        "source": "clinicaltrials.gov/api/v2",
        "backend": "fts-only",
        "retrieval": "sqlite fts5 + bm25 (external content)",
        "caps": {"intervention": 4, "sponsor": 3},
        "cap_report": {
            "intervention": {"distinct_seen": 120, "eligible": 5, "cap": 4,
                             "kept": 4, "dropped_to_cap": 1, "capped": True},
            "sponsor": {"distinct_seen": 55, "eligible": 4, "cap": 3,
                        "kept": 3, "dropped_to_cap": 1, "capped": True},
        },
    }), encoding="utf-8")


@pytest.fixture
def stub_index(tmp_path: Path) -> Path:
    """A small registry-shaped index: real alias structure, fake scale."""
    _write_index(tmp_path, [
        # Aliases here are the shape ClinicalTrials.gov actually returns in
        # `InterventionOtherName` — registry data, not model assertion.
        {"kind": "intervention", "label": "Pembrolizumab", "doc_freq": 2915,
         "aliases": ["Keytruda", "MK-3475", "Lambrolizumab"]},
        {"kind": "intervention", "label": "Nivolumab", "doc_freq": 1400,
         "aliases": ["Opdivo", "BMS-936558"]},
        {"kind": "intervention", "label": "Carboplatin", "doc_freq": 1697,
         "aliases": ["Paraplatin"]},
        {"kind": "intervention", "label": "Placebo", "doc_freq": 9000, "aliases": []},
        # Sponsor set built to reproduce the IDF failure: one rare decisive
        # token ("guy") against a very common one ("hospital").
        {"kind": "sponsor", "label": "Guy's and St Thomas' NHS Foundation Trust",
         "doc_freq": 289, "aliases": []},
        {"kind": "sponsor", "label": "Massachusetts General Hospital",
         "doc_freq": 2554, "aliases": []},
        {"kind": "sponsor", "label": "Seoul National University Hospital",
         "doc_freq": 1884, "aliases": []},
        {"kind": "sponsor", "label": "Merck Sharp & Dohme LLC", "doc_freq": 900,
         "aliases": []},
        {"kind": "sponsor", "label": "Merz Pharmaceuticals GmbH", "doc_freq": 40,
         "aliases": []},
    ])
    return tmp_path


class FakeProbeClient:
    """Stands in for CTGovClient.count. Scripted totals, every call recorded."""

    def __init__(self, counts: dict[str, int], fail_on: set[str] | None = None):
        self._counts = counts
        self._fail_on = fail_on or set()
        self.probes: list[str] = []

    async def count(self, filt: Filter) -> int:
        clause = filt.params.get("filter.advanced", "")
        self.probes.append(clause)
        for term in self._fail_on:
            if f'"{term}"' in clause:
                raise CTGovError("simulated upstream failure")
        for term, total in self._counts.items():
            if f'"{term}"' in clause:
                return total
        # Unscripted entity means "the registry has no such record here",
        # which is the case these tests care most about.
        return 0


def _candidates(*specs: tuple[str, float]) -> list[Candidate]:
    return [Candidate(label=lbl, kind="intervention", score=s) for lbl, s in specs]


# ------------------------------------------------------------- 0. FTS5 itself


def test_fts5_is_available_in_this_interpreter():
    """Everything below is meaningless if SQLite lacks FTS5, so assert it
    directly rather than letting the failure surface as a confusing error."""
    assert fts5_available(), "this Python's SQLite has no FTS5 module"


def test_external_content_index_is_populated_by_triggers(stub_index: Path):
    """External-content FTS5 stores no copy of the text; the AFTER INSERT
    trigger is the only thing that makes it searchable at all."""
    connection = sqlite3.connect(stub_index / "entities.db")
    fts_rows = connection.execute("SELECT COUNT(*) FROM entities_fts").fetchone()[0]
    src_rows = connection.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    connection.close()
    assert fts_rows == src_rows == 9


def test_delete_and_update_triggers_keep_the_index_in_sync(stub_index: Path):
    """The ('delete', ...) form is required for external content — a plain
    DELETE against the FTS table would corrupt it."""
    connection = sqlite3.connect(stub_index / "entities.db")
    connection.execute("DELETE FROM entities WHERE canonical = 'Placebo'")
    connection.execute(
        "UPDATE entities SET canonical = 'Nivolumab updated' WHERE canonical = 'Nivolumab'"
    )
    connection.commit()
    assert connection.execute(
        "SELECT COUNT(*) FROM entities_fts WHERE entities_fts MATCH 'Placebo'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM entities_fts WHERE entities_fts MATCH 'updated'"
    ).fetchone()[0] == 1
    connection.close()


# ------------------------------------------------ 1. an alias reaches canonical


def test_brand_alias_resolves_to_the_generic_entity(stub_index: Path):
    """Scenario: analyst types the brand name they saw in a press release.

    "Keytruda" must land on "Pembrolizumab" — one entity, not two — because
    that fragmentation is the limitation this module exists to close.
    """
    index = load_index("intervention", directory=stub_index)
    candidates = resolve("Keytruda", "intervention", top_k=3, index=index)

    assert candidates, "the brand name resolved to nothing"
    assert candidates[0].label == "Pembrolizumab"
    assert candidates[0].via == "alias", "should resolve through the registry alias list"
    assert "Keytruda" in candidates[0].aliases


def test_alias_resolution_is_case_and_punctuation_insensitive(stub_index: Path):
    index = load_index("intervention", directory=stub_index)
    for spelling in ("KEYTRUDA", "keytruda", "Keytruda®", " Keytruda "):
        top = resolve(spelling, "intervention", top_k=1, index=index)
        assert top and top[0].label == "Pembrolizumab", f"{spelling!r} did not resolve"


def test_sponsor_legal_suffixes_and_dangling_conjunctions_are_folded():
    """Regression: 'Merck & Co., Inc.' normalised to 'merck and'.

    '&' expands to ' and ' before legal-form stripping, so removing 'co' and
    'inc' left a trailing conjunction. Its retrieval was then dominated by
    'and', and the top sponsor proposal for Merck was an orthopaedic hospital.
    """
    assert normalize("Merck & Co., Inc.", "sponsor") == "merck"
    assert normalize("Merck Sharp & Dohme LLC", "sponsor") == "merck sharp and dohme"
    assert "and" not in content_tokens(normalize("Merck & Co.", "sponsor"))


def test_a_query_with_no_lexical_footing_proposes_nothing(stub_index: Path):
    """Proposing the nearest unrelated string is worse than proposing nothing.

    A probe would happily confirm that any real sponsor exists, so a junk
    proposal surviving verification is a real risk. The token gate stops it at
    the proposal stage instead. "MSD" is the live example: it shares no token
    with "Merck Sharp & Dohme LLC", so nothing is proposed.
    """
    index = load_index("sponsor", directory=stub_index)
    assert resolve("MSD", "sponsor", top_k=5, index=index) == []
    assert resolve("qqqzzzxyz", "sponsor", top_k=5, index=index) == []


# -------------------------------------------- 2. FTS5 failure -> LIKE fallback


@pytest.mark.parametrize("query,reason", [
    ("Guy's Hospital", "apostrophe"),
    ("Merck & Co.", "ampersand"),
    # NOTE: a BALANCED pair of double quotes parses fine — FTS5 reads it as a
    # phrase query. Only an unbalanced one is a syntax error, which is exactly
    # the shape a user typo takes.
    ('unbalanced " quote', "unterminated string"),
    ("MK-3475", "hyphen followed by digits"),
    ("St. Jude", "full stop"),
])
def test_ordinary_user_text_really_does_break_fts_match(query, reason, stub_index: Path):
    """The fallback is not defensive decoration — MATCH genuinely rejects all
    of these, which is why the query text is not pre-sanitised."""
    connection = sqlite3.connect(stub_index / "entities.db")
    with pytest.raises(sqlite3.OperationalError):
        connection.execute(
            "SELECT 1 FROM entities_fts WHERE entities_fts MATCH ?",
            (_fts_match_query(query),),
        ).fetchall()
    connection.close()


def test_apostrophe_query_falls_back_to_like_and_still_resolves(stub_index: Path):
    """Scenario: a UK trials analyst types a hospital name with an apostrophe."""
    index = load_index("sponsor", directory=stub_index)
    candidates = resolve("Guy's Hospital", "sponsor", top_k=3, index=index)

    assert candidates, "the fallback returned nothing"
    assert candidates[0].label == "Guy's and St Thomas' NHS Foundation Trust"
    assert candidates[0].via == "like-fallback"


def test_like_fallback_ranks_by_rare_tokens_not_by_popularity(stub_index: Path):
    """Regression, measured on the shipped index.

    The fallback originally ordered by ``doc_freq``. For "Guy's Hospital" the
    token 'hospital' appears in 513 of 2,500 sponsors and 'guy' in exactly 1 —
    and the 1 was correct. Popularity ordering buried it under Massachusetts
    General Hospital and five other large hospitals. Summed IDF fixes it on
    the same principle bm25 itself uses.
    """
    index = load_index("sponsor", directory=stub_index)
    labels = [c.label for c in resolve("Guy's Hospital", "sponsor", top_k=5, index=index)]

    assert labels[0] == "Guy's and St Thomas' NHS Foundation Trust"
    # Massachusetts General has ~9x the doc_freq and still must not outrank it.
    assert labels.index("Guy's and St Thomas' NHS Foundation Trust") < labels.index(
        "Massachusetts General Hospital"
    )


def test_ampersand_query_falls_back_and_finds_the_right_sponsor(stub_index: Path):
    index = load_index("sponsor", directory=stub_index)
    top = resolve("Merck & Co.", "sponsor", top_k=3, index=index)
    assert top and top[0].label == "Merck Sharp & Dohme LLC"
    assert top[0].via == "like-fallback"


# ---------------------------------- 3. an unverified candidate never gets through


def test_resolve_returns_only_unverified_candidates_with_no_count(stub_index: Path):
    """Retrieval must not be able to hand anyone a number."""
    index = load_index("intervention", directory=stub_index)
    for candidate in resolve("Keytruda", "intervention", top_k=5, index=index):
        assert candidate.verified is False
        assert candidate.count is None


def test_a_candidate_cannot_carry_a_count_without_verification():
    """Structural, not conventional: the dataclass refuses the invalid state."""
    with pytest.raises(ValueError):
        Candidate(label="Pembrolizumab", kind="intervention", score=-1.0,
                  verified=False, count=2915)
    with pytest.raises(ValueError):
        Candidate(label="Pembrolizumab", kind="intervention", score=-1.0,
                  verified=True, count=None)
    with pytest.raises(ValueError):
        # A zero-count "verified" candidate is the discarded case; it must not
        # be representable, or a caller could resurrect it as a real bucket.
        Candidate(label="Ghost", kind="intervention", score=-1.0,
                  verified=True, count=0)


def test_candidates_are_immutable_so_verification_cannot_be_forged():
    candidate = Candidate(label="Pembrolizumab", kind="intervention", score=-1.0)
    with pytest.raises(FrozenInstanceError):
        candidate.verified = True  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        candidate.count = 2915  # type: ignore[misc]


async def test_every_survivor_of_the_pipeline_is_probe_verified(stub_index: Path):
    """End-to-end: nothing reaches a result without a count behind it."""
    index = load_index("intervention", directory=stub_index)
    client = FakeProbeClient({"Pembrolizumab": 339, "Nivolumab": 210, "Carboplatin": 0})
    results = await resolve_verified(
        "Keytruda", "intervention", Filter({"query.cond": "melanoma"}),
        client, top_k=4, index=index,
    )
    assert results, "the pipeline dropped everything"
    for candidate in results:
        assert candidate.verified is True
        assert isinstance(candidate.count, int) and candidate.count > 0
    # Ordered by the measurement, not by the rank that nominated it.
    assert [c.count for c in results] == sorted((c.count for c in results), reverse=True)


# --------------------------------------- 4. a zero-count candidate is discarded


async def test_candidate_whose_probe_returns_zero_is_dropped():
    """The correction that makes retrieval safe to use at all."""
    client = FakeProbeClient({"Pembrolizumab": 2915})  # everything else -> 0
    proposals = _candidates(("Pembrolizumab", -9.0), ("Omalizumab", -1.2),
                            ("Sarilumab", -1.1))
    survivors = await verify_candidates(proposals, Filter({}), client)

    assert [c.label for c in survivors] == ["Pembrolizumab"]
    assert survivors[0].count == 2915
    # The dropped ones were genuinely asked about, not filtered on a hunch.
    assert any("Omalizumab" in p for p in client.probes)
    assert any("Sarilumab" in p for p in client.probes)


async def test_a_strong_rank_does_not_rescue_a_zero_count():
    """Rank is never allowed to outrank the measurement."""
    client = FakeProbeClient({})  # the registry knows none of them
    survivors = await verify_candidates(
        _candidates(("Pembrolizumab", -99.0), ("Nivolumab", -98.0)), Filter({}), client
    )
    assert survivors == []


async def test_a_failed_probe_drops_the_candidate_rather_than_admitting_it():
    """A transport failure must never resolve in favour of the candidate.

    The probe did not complete, so the candidate is unverified, so it cannot
    reach a result. Degrading the shortlist is correct; admitting an uncounted
    entity is the one thing this module forbids.
    """
    client = FakeProbeClient({"Nivolumab": 210}, fail_on={"Pembrolizumab"})
    survivors = await verify_candidates(
        _candidates(("Pembrolizumab", -9.0), ("Nivolumab", -8.0)), Filter({}), client
    )
    assert [c.label for c in survivors] == ["Nivolumab"]


async def test_verification_respects_the_caller_base_filter():
    """A candidate is confirmed under the SAME scope the question asked about.

    AREA clauses, not query.intr — a plain key would be overwritten by
    Filter.merged_with, silently widening the probe to a different question
    than the one the count is reported against.
    """
    client = FakeProbeClient({"Pembrolizumab": 339})
    base = Filter({"query.cond": "melanoma", "filter.advanced": "AREA[Phase]PHASE3"})
    await verify_candidates(_candidates(("Pembrolizumab", -9.0)), base, client)

    clause = client.probes[0]
    assert "AREA[Phase]PHASE3" in clause, "base constraint was dropped"
    assert 'AREA[InterventionName]"Pembrolizumab"' in clause
    assert " AND " in clause, "clauses must intersect, not replace"


async def test_registry_alias_is_probed_when_the_canonical_label_has_no_records():
    """Fallback is still a probe — nothing is assumed from the alias list."""
    client = FakeProbeClient({"Keytruda": 41})  # canonical spelling has none
    proposals = [Candidate(label="Pembrolizumab", kind="intervention", score=-9.0,
                           aliases=("Keytruda", "MK-3475"))]
    survivors = await verify_candidates(proposals, Filter({}), client)

    assert len(survivors) == 1
    assert survivors[0].count == 41
    assert survivors[0].matched_term == "Keytruda"
    assert survivors[0].label == "Pembrolizumab", "canonical identity is preserved"


async def test_probe_budget_drops_candidates_rather_than_passing_them_unverified():
    """Running out of budget must degrade to FEWER entities, never unchecked ones."""
    client = FakeProbeClient({f"Drug{i}": 10 for i in range(50)})
    proposals = [Candidate(label=f"Drug{i}", kind="intervention", score=-1.0)
                 for i in range(50)]
    survivors = await verify_candidates(proposals, Filter({}), client, max_probes=5)

    assert len(client.probes) <= 5
    assert len(survivors) <= 5
    assert all(c.verified and c.count for c in survivors)


async def test_a_non_integer_probe_result_is_rejected_loudly():
    """A None or float leaking in from a malformed response must not become a count."""
    class BadClient:
        async def count(self, filt): return None

    with pytest.raises(TypeError):
        await verify_candidates(_candidates(("X", -1.0)), Filter({}), BadClient())


async def test_which_retriever_proposed_a_candidate_does_not_change_its_treatment():
    """Adding a second proposer must not add a second trust level."""
    client = FakeProbeClient({"Nivolumab": 210})
    proposals = [
        Candidate(label="Nivolumab", kind="intervention", score=0.0,
                  via="semantic", source="semantic", semantic_score=0.9),
        Candidate(label="Ghost", kind="intervention", score=0.0,
                  via="semantic", source="semantic", semantic_score=0.99),
    ]
    survivors = await verify_candidates(proposals, Filter({}), client)
    assert [c.label for c in survivors] == ["Nivolumab"]
    assert survivors[0].source == "semantic", "source is preserved, not laundered"


# ------------------------------------------- 5. the index is bounded and reported


def test_stub_index_reports_its_cap(stub_index: Path):
    index = load_index("intervention", directory=stub_index)
    assert index.capped is True
    report = index.cap_report()
    assert report["cap"] == 4
    assert report["dropped_to_cap"] == 1
    assert report["distinct_seen"] > report["kept"]


@pytest.mark.skipif(not (INDEX_DIR / "entities.db").exists(),
                    reason="built index not present; run scripts/build_entity_index.py")
def test_shipped_index_is_bounded_and_states_what_it_dropped():
    """The committed artifact must be small, and honest about being small."""
    clear_cache()
    status = index_status()
    for kind in KINDS:
        info = status[kind]
        assert info["available"], info
        assert info["entities"] <= info["cap"], "index exceeded its own cap"
        assert info["entities"] <= 5000, "corpus is meant to be a few thousand rows"
        report = info["cap_report"]
        for key in ("distinct_seen", "eligible", "kept", "dropped_to_cap", "capped"):
            assert key in report, f"{kind} cap report omits {key}"
        assert report["kept"] == info["entities"]
        if report["capped"]:
            assert report["dropped_to_cap"] > 0, "claimed capped but dropped nothing"


@pytest.mark.skipif(not (INDEX_DIR / "entities.db").exists(),
                    reason="built index not present")
def test_shipped_artifact_stays_small_enough_to_commit():
    """Someone clones this repo. 40 MB of raw vectors was not acceptable;
    one SQLite file with an external-content index is a few MB."""
    size_mb = (INDEX_DIR / "entities.db").stat().st_size / 1e6
    assert size_mb < 15, f"committed index grew to {size_mb:.1f} MB"


def test_a_missing_artifact_raises_instead_of_degrading_silently(tmp_path: Path):
    """Half-resolving is worse than not resolving: the count would be scoped
    to a different entity than the caller believes."""
    with pytest.raises(IndexUnavailable):
        load_index("intervention", directory=tmp_path)


def test_an_index_without_the_expected_tables_is_rejected(tmp_path: Path):
    connection = sqlite3.connect(tmp_path / "entities.db")
    connection.execute("CREATE TABLE junk(x)")
    connection.commit()
    connection.close()
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(IndexUnavailable):
        load_index("intervention", directory=tmp_path)


# ----------------------------------- 6. retrieval alone never produces a number


def test_bm25_ranks_are_non_positive_so_they_cannot_read_as_counts(stub_index: Path):
    """SQLite's bm25 is negative (lower is better). A trial count is a
    non-negative integer, so the sign alone keeps the two from being
    confused — a free structural guard worth asserting."""
    index = load_index("intervention", directory=stub_index)
    for candidate in resolve("Pembrolizumab", "intervention", top_k=4, index=index):
        assert candidate.score <= 0.0
        assert isinstance(candidate.score, float)
        assert candidate.count is None


def test_resolve_performs_no_network_io(stub_index: Path, monkeypatch):
    """Proof by construction: break the sockets, resolution still works.

    If resolve() cannot reach the network it cannot obtain a totalCount, and
    therefore cannot present one. This is the invariant enforced by absence.
    """
    import httpx

    def _explode(*args, **kwargs):
        raise AssertionError("resolve() attempted network I/O")

    monkeypatch.setattr(httpx.Client, "request", _explode)
    monkeypatch.setattr(httpx.AsyncClient, "request", _explode)

    index = load_index("intervention", directory=stub_index)
    assert resolve("Keytruda", "intervention", top_k=3, index=index)[0].label == "Pembrolizumab"


def test_resolve_works_with_no_embeddings_present(stub_index: Path):
    """The stub index has vector IS NULL on every row. FTS-only must be a
    fully supported configuration, not a degraded one — the service has to run
    with no model and no network."""
    index = load_index("sponsor", directory=stub_index)
    assert index.has_semantic is False
    top = resolve("Merck & Co.", "sponsor", top_k=2, index=index)
    assert top and top[0].label == "Merck Sharp & Dohme LLC"
    assert all(c.source == "fts" and c.semantic_score is None for c in top)


def test_no_count_appears_anywhere_in_a_rendered_proposal(stub_index: Path):
    """The formatter must say UNVERIFIED, not print a number-shaped rank."""
    from app.entities import format_candidates

    index = load_index("intervention", directory=stub_index)
    rendered = format_candidates(resolve("Keytruda", "intervention", top_k=3, index=index))
    assert "UNVERIFIED (no count)" in rendered
    assert "count=" not in rendered


async def test_the_rank_never_becomes_the_count(stub_index: Path):
    """Direct guard on the substitution this whole module is built to prevent."""
    index = load_index("intervention", directory=stub_index)
    client = FakeProbeClient({"Pembrolizumab": 2915, "Nivolumab": 1400})
    results = await resolve_verified("Keytruda", "intervention", Filter({}),
                                     client, top_k=4, index=index)
    assert results
    for candidate in results:
        assert candidate.count != candidate.score
        assert candidate.count in (2915, 1400)


async def test_retrieval_ordering_does_not_survive_contradicting_counts(stub_index: Path):
    """When the index's ranking disagrees with the registry, the registry wins."""
    index = load_index("intervention", directory=stub_index)
    client = FakeProbeClient({"Pembrolizumab": 5, "MK-3475": 900})
    proposals = [
        *resolve("Keytruda", "intervention", top_k=4, index=index),
        Candidate(label="MK-3475", kind="intervention", score=-1.0),
    ]
    survivors = await verify_candidates(proposals, Filter({}), client)
    survivors.sort(key=lambda c: -(c.count or 0))
    assert survivors[0].label == "MK-3475"
    assert survivors[0].count == 900


def test_candidate_filter_escapes_quotes_so_a_label_cannot_break_the_clause():
    candidate = Candidate(label='Weird" OR AREA[Phase]PHASE3', kind="intervention", score=-1.0)
    clause = candidate_filter(candidate, Filter({})).params["filter.advanced"]
    assert clause.count('"') == 2, f"label escaped its AREA clause: {clause}"


def test_module_constants_keep_the_probe_fan_out_bounded():
    """An agent loop re-entering resolution must not be able to melt the API."""
    assert 0 < MAX_VERIFY_PROBES <= 60, "verify budget must stay well under the probe cap"
    assert MAX_SEMANTIC_ONLY >= 0


def test_resolve_rejects_a_nonpositive_top_k(stub_index: Path):
    index = load_index("intervention", directory=stub_index)
    assert resolve("Keytruda", "intervention", top_k=0, index=index) == []


def test_index_handle_length_matches_its_rows(stub_index: Path):
    index = load_index("sponsor", directory=stub_index)
    assert isinstance(index, EntityIndex)
    assert len(index) == 5
