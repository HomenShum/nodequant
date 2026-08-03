"""Entity resolution: retrieval PROPOSES, the API CONFIRMS.

This module closes the limitation named in the README: sponsor and drug
shortlists are drawn from a bounded *sample* of matching records, so a rare
entity can be missing, and aliases fragment one entity into several
("Keytruda" vs "Pembrolizumab"; "Merck & Co." vs "MSD" vs "Merck Sharp &
Dohme LLC").

--------------------------------------------------------------------------
The invariant, stated once and enforced structurally
--------------------------------------------------------------------------

The system's central rule is that the model plans WHICH questions to ask and
never answers them: every number in a response is an API ``totalCount``.
A retrieval index is a second way to smuggle a derived number into a result,
so it is fenced the same way — and adding a SECOND proposer (below) changes
nothing about this, which is the point of putting the gate here rather than
inside a retriever:

    * ``Candidate.score`` (bm25) and ``Candidate.semantic_score`` (cosine)
      are *ranking* signals. Neither is a count, neither is ever rendered as
      one, and neither can become one: the only field that may hold a count
      is ``Candidate.count``, and the only code that writes it is
      :func:`verify_candidates`, which writes exactly what
      ``CTGovClient.count`` returned.

    * ``Candidate.verified`` starts ``False`` and only :func:`verify_candidates`
      may set it ``True``. A candidate whose count probe returns 0 is
      *discarded*, not kept with a zero — the index proposed an entity that
      does not exist under this filter, and the API said so.

    * :func:`resolve` performs no network I/O at all. It cannot produce a
      count even by accident, because it never talks to the registry.

Retrieval may PROPOSE. Only the API may CONFIRM.

--------------------------------------------------------------------------
Two retrievers, because they fail differently
--------------------------------------------------------------------------

**FTS5 + bm25** (SQLite, stdlib). An inverted index over canonical names and
the registry's own other-names. Exact, fast, explainable, and it is the
technique that *ports*: Postgres ``tsvector`` / ``ts_rank`` is the same idea
with the same shape, so this schema translates almost line for line. What it
cannot do is bridge two spellings that share no characters — ``MSD`` and
``Merck Sharp & Dohme LLC`` have no common token and no registry other-name
linking them.

**Static embeddings** (model2vec, distilled, CPU-only, no API). Intended to
cover exactly that gap. Vectors are built offline and stored as BLOBs in the
same SQLite file, so the request path needs numpy and nothing else — no
model, no network, no server.

They are unioned, never blended. bm25 (negative, corpus-relative) and cosine
(in [-1, 1]) are on incomparable scales; any weighted sum of them would be a
made-up constant dressed as a relevance score. Instead a candidate proposed
by BOTH retrievers ranks first — agreement between two independent methods
is real evidence — and the remainder interleaves each retriever's own
ordering. ``Candidate.source`` records which retriever(s) proposed it.

*Measured caveat, recorded because it changes how much to trust the semantic
arm.* On this corpus the static model does not do what one hopes. Cosines
from ``potion-base-8M``:

    Keytruda      <-> Pembrolizumab            0.052
    MSD           <-> Merck Sharp & Dohme LLC  0.088
    Pembrolizumab <-> Omalizumab               0.418   <- WRONG pair
    Opdivo        <-> Nivolumab                0.403   <- right pair

It ranks a wrong pair eight times above a right one. On drug names a static
embedding is doing subword morphology (``-lizumab``), not pharmacology, and
brand/generic bridging is carried far better by the registry alias table
(tier 2 below), which is evidence rather than inference. The semantic arm is
therefore admitted in strictly bounded numbers and ranked beneath the
lexical arm — and the count probe is what stops its confusions from
reaching a chart.

--------------------------------------------------------------------------
How a query is resolved, offline, in tiers
--------------------------------------------------------------------------

    1. **Exact.** The query normalises to a canonical name in the index.
    2. **Alias.** The query normalises to one of the ``otherNames`` that
       ClinicalTrials.gov itself records for an entity. Registry data, not
       model belief.
    3. **bm25.** ``entities_fts MATCH`` ranked by ``bm25(entities_fts)``.
    4. **LIKE fallback.** FTS5's query language rejects a great deal of
       ordinary user text — ``&``, ``'``, ``"``, ``.`` and ``-`` are all
       syntax errors, so "Merck & Co.", "Guy's" and even the real drug code
       "MK-3475" cannot be passed to MATCH. That is not an edge case, it is
       Tuesday, and it is why the fallback exists rather than a sanitiser
       that would quietly change what the user asked for.
    5. **Semantic.** Cosine over the stored vectors, seeded per
       :func:`_semantic_query_vector`.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Literal, Sequence

import numpy as np

from .ctgov import CTGovError, Filter

Kind = Literal["intervention", "sponsor"]
KINDS: tuple[Kind, ...] = ("intervention", "sponsor")
Source = Literal["fts", "semantic", "both"]

INDEX_DIR = Path(__file__).resolve().parent / "vendor" / "entity_index"
DB_PATH = INDEX_DIR / "entities.db"

# Hard ceiling on probes issued by one verify_candidates call, including the
# alias fallbacks. Bounded because this runs inside an agent loop.
MAX_VERIFY_PROBES = 24

# Aliases probed when the canonical label itself returns 0. Small: this is a
# fallback for entities the registry only ever spells by brand name, not a
# licence to enumerate a 22-alias list like Pembrolizumab's.
MAX_ALIAS_PROBES = 2

# Rows pulled from retrieval before the token gate and top_k trim. Wider than
# top_k because the gate discards, and a gate that starves the result set is
# as bad as no gate.
RETRIEVAL_LIMIT = 60

# How many semantic-only proposals may enter the union.
#
# DEFAULT 0, and that default is a measured finding rather than timidity.
# Nearest-neighbour search over entity-name embeddings retrieves entities of
# the same CATEGORY, not the same IDENTITY, and entity resolution needs
# identity. Anchored on Pembrolizumab, the model's nearest neighbours in the
# shipped index are Trastuzumab (0.549) and Rituximab (0.512) — other
# monoclonal antibodies, correctly "similar", and completely wrong as
# aliases. Both are real drugs, so both PASS a count probe; the probe
# protects against fabricated numbers, not against a sibling being mistaken
# for a synonym.
#
# Nor can a threshold separate the two cases. Measured on potion-base-8M:
#   Opdivo        <-> Nivolumab     0.403  (a true alias pair)
#   Pembrolizumab <-> Omalizumab    0.418  (a false pair, scoring HIGHER)
# There is no cut that keeps the first and drops the second.
#
# So the semantic arm is retained for what it demonstrably does well —
# CORROBORATION, contributing the "both" tag when it independently agrees
# with the lexical arm — and is not permitted to introduce candidates on its
# own. Raise this to let it propose; the knob is here precisely so the
# tradeoff is visible rather than hidden in a threshold.
MAX_SEMANTIC_ONLY = 0

# Floor on cosine for a semantic proposal. Unlike bm25, cosine is at least
# comparable across queries (fixed range, fixed geometry) — but per the
# measurement above, comparable is not the same as separable.
MIN_SEMANTIC_SCORE = 0.35


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

# Legal-form suffixes only. Deliberately NOT stripping industry words like
# "Pharmaceuticals" or "Oncology": those distinguish real distinct sponsors,
# and collapsing them would merge entities the registry keeps apart — an
# entity-resolution bug that produces confidently wrong counts.
_LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "co",
    "corp", "corporation", "company", "gmbh", "ag", "sa", "sas", "srl",
    "spa", "bv", "nv", "ab", "as", "asa", "oy", "plc", "pty", "kk", "kg",
    "aps", "sarl", "holdings", "holding", "group",
}

# Trailing dose/strength decoration on intervention names ("Lenvatinib 10 mg").
# Stripped so the same drug at four strengths is one entity, not four.
_DOSE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*"
    r"(?:mg/kg|mg/m2|mcg/kg|mg|mcg|ug|µg|g|kg|ml|l|iu|units?|%)\b",
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[^a-z0-9]+")
_WS = re.compile(r"\s+")

# Tokens that carry no identity. Kept tiny and English-only on purpose: a
# longer stop list starts deleting real name fragments.
_STOPWORDS = frozenset({"and", "the", "of", "for", "a", "an", "in", "at", "on", "de", "la"})

# A query token this long or longer may match a label token by prefix, so
# "pembro" reaches "pembrolizumab". Shorter than this and the prefix is not
# evidence of anything — "ca" prefixes several hundred unrelated names.
MIN_PREFIX_LEN = 4


def normalize(text: str, kind: Kind) -> str:
    """Fold surface variation that is never semantically meaningful.

    Conservative on purpose. This handles case, accents, punctuation, the
    ampersand/"and" split and legal-form suffixes — variation that no
    registry uses to distinguish two entities. It does NOT try to handle
    aliases; "Keytruda" and "Pembrolizumab" normalise to different strings,
    which is correct. Bridging those is the index's job, not a regex's.
    """
    lowered = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    lowered = lowered.replace("&", " and ")
    if kind == "intervention":
        lowered = _DOSE.sub(" ", lowered)
    cleaned = _WS.sub(" ", _PUNCT.sub(" ", lowered)).strip()
    if kind == "sponsor":
        tokens = [t for t in cleaned.split(" ") if t]
        # Strip legal-form suffixes AND any conjunction they leave dangling.
        # Both, in one loop, because they interleave: "Merck & Co., Inc."
        # expands to "merck and co inc", and popping only legal forms leaves
        # "merck and" — whose retrieval is then dominated by "and", matching
        # every "X and Y NHS Trust" in the registry. That was a real observed
        # failure, not a hypothetical one, and it is why the "&" expansion has
        # to be reconciled AFTER suffix stripping rather than before.
        while len(tokens) > 1 and (tokens[-1] in _LEGAL_SUFFIXES or tokens[-1] in _STOPWORDS):
            tokens.pop()
        cleaned = " ".join(tokens)
    return cleaned


def content_tokens(normalised: str) -> set[str]:
    """Identity-bearing tokens of an already-normalised string."""
    return {t for t in normalised.split(" ") if t and t not in _STOPWORDS}


# --------------------------------------------------------------------------
# Candidate
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """A proposed entity. Not a fact until the API says so.

    Frozen so nothing downstream can flip ``verified`` or write ``count``
    in place; :func:`verify_candidates` produces new instances via
    ``dataclasses.replace``. The immutability is the enforcement mechanism
    for "only a probe may mark a candidate verified".
    """

    label: str
    kind: Kind
    score: float
    """SQLite FTS5 bm25 rank: NEGATIVE, and more negative means a better
    match. Corpus-relative — comparable only within a single query, never
    across queries and never against a fixed threshold. Its sign is a free
    structural guard: a trial count is a non-negative integer, so a bm25
    score cannot be mistaken for one. 0.0 means "not ranked by bm25" (a LIKE
    or semantic-only proposal)."""

    aliases: tuple[str, ...] = ()
    via: str = "exact"
    """Retrieval tier: exact | alias | bm25 | like-fallback | semantic."""

    source: Source = "fts"
    """Which retriever(s) proposed this: fts | semantic | both."""

    semantic_score: float | None = None
    """Cosine in [-1, 1] against the stored static embeddings, or None when
    the semantic retriever did not propose this candidate. Never blended with
    ``score`` — the two are on incomparable scales."""

    verified: bool = False
    """True only after a count probe returned a non-zero totalCount."""

    count: int | None = None
    """The probe's ``totalCount``, verbatim. None until verified. This is the
    only field in this module that may ever be presented as a number."""

    matched_term: str | None = None
    """The string that actually produced the non-zero probe — the canonical
    label, or the alias that the registry indexes this entity under."""

    def __post_init__(self) -> None:
        # Structural guard, not decoration: an unverified candidate carrying a
        # count is precisely the smuggling path this module exists to close.
        if self.count is not None and not self.verified:
            raise ValueError("count may not be set on an unverified candidate")
        if self.verified and (self.count is None or self.count <= 0):
            raise ValueError("a verified candidate must carry a positive probe count")


# --------------------------------------------------------------------------
# Index loading
# --------------------------------------------------------------------------


class IndexUnavailable(RuntimeError):
    """The persisted index is missing, unreadable, or lacks FTS5.

    Raised rather than silently degrading: a caller that thinks it resolved an
    entity and actually did not would produce a differently-scoped count with
    no warning, which is worse than an error.
    """


def fts5_available(connection: sqlite3.Connection | None = None) -> bool:
    """Whether this interpreter's SQLite was compiled with FTS5."""
    own = connection is None
    conn = connection or sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE __fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE __fts5_probe")
        return True
    except sqlite3.Error:
        return False
    finally:
        if own:
            conn.close()


@dataclass
class EntityIndex:
    """A read-only handle on the committed SQLite artifact."""

    kind: Kind
    connection: sqlite3.Connection
    manifest: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _vectors: np.ndarray | None = None
    _vector_labels: list[str] = field(default_factory=list)

    def query(self, sql: str, params: Sequence = ()) -> list[sqlite3.Row]:
        # sqlite3 connections are opened with check_same_thread=False so a
        # threadpool-backed async server can share one; the lock is what makes
        # that actually safe rather than merely permitted.
        with self._lock:
            return self.connection.execute(sql, params).fetchall()

    def __len__(self) -> int:
        return int(self.query(
            "SELECT COUNT(*) AS n FROM entities WHERE kind = ?", (self.kind,)
        )[0]["n"])

    @property
    def has_semantic(self) -> bool:
        return self.manifest.get("backend") == "fts+semantic"

    def vectors(self) -> tuple[np.ndarray, list[str]]:
        """Stored static embeddings for this kind, as a normalised matrix.

        Materialised once and kept: 2,500 x 256 float32 is 2.5 MB, and
        decoding BLOBs per query would dominate the cost of the dot product
        they feed.
        """
        if self._vectors is None:
            rows = self.query(
                "SELECT canonical, vector FROM entities "
                "WHERE kind = ? AND vector IS NOT NULL ORDER BY rowid",
                (self.kind,),
            )
            if not rows:
                self._vectors = np.zeros((0, 0), dtype=np.float32)
                self._vector_labels = []
            else:
                self._vectors = np.vstack([
                    np.frombuffer(r["vector"], dtype=np.float16).astype(np.float32)
                    for r in rows
                ])
                self._vector_labels = [r["canonical"] for r in rows]
        return self._vectors, self._vector_labels

    @property
    def cap(self) -> int:
        """The corpus bound this artifact was built under."""
        return int(self.manifest.get("caps", {}).get(self.kind, len(self)))

    @property
    def capped(self) -> bool:
        """True when candidates were dropped to honour the cap."""
        return bool(self.manifest.get("cap_report", {}).get(self.kind, {}).get("capped", False))

    def cap_report(self) -> dict:
        """What was seen, what was kept, what was dropped — reported, not hidden."""
        report = dict(self.manifest.get("cap_report", {}).get(self.kind, {}))
        report.setdefault("kept", len(self))
        report.setdefault("cap", self.cap)
        return report


_CACHE: dict[tuple[Kind, str], EntityIndex] = {}
_CACHE_LOCK = threading.Lock()


def load_index(kind: Kind, directory: Path | None = None) -> EntityIndex:
    """Open the persisted index for one kind. Read-only, and no network."""
    base = directory or INDEX_DIR
    db_path = base / "entities.db"
    manifest_path = base / "manifest.json"
    cache_key = (kind, str(db_path))
    with _CACHE_LOCK:
        if cache_key in _CACHE:
            return _CACHE[cache_key]

    for path in (db_path, manifest_path):
        if not path.exists():
            raise IndexUnavailable(
                f"entity index artifact missing: {path}. "
                "Run: uv run --no-sync python scripts/build_entity_index.py"
            )
    if not fts5_available():
        raise IndexUnavailable(
            "this Python's SQLite was compiled without FTS5, so the entity "
            "index cannot be queried. Check `sqlite3.connect(':memory:')"
            ".execute(\"CREATE VIRTUAL TABLE t USING fts5(a)\")`."
        )

    # Opened read-only via URI so a request path physically cannot mutate a
    # committed artifact.
    connection = sqlite3.connect(
        f"file:{db_path}?mode=ro", uri=True, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("SELECT 1 FROM entities LIMIT 1")
        connection.execute("SELECT 1 FROM entities_fts LIMIT 1")
    except sqlite3.Error as exc:
        raise IndexUnavailable(f"{db_path} is not a usable entity index: {exc}") from exc

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    index = EntityIndex(kind=kind, connection=connection, manifest=manifest)
    if len(index) == 0:
        raise IndexUnavailable(f"{db_path} contains no {kind} entities")
    with _CACHE_LOCK:
        _CACHE[cache_key] = index
    return index


def clear_cache() -> None:
    """Drop cached handles. For tests that swap artifact directories."""
    with _CACHE_LOCK:
        for index in _CACHE.values():
            try:
                index.connection.close()
            except sqlite3.Error:
                pass
        _CACHE.clear()


# --------------------------------------------------------------------------
# Retriever 1 — FTS5 / bm25, with the LIKE fallback
# --------------------------------------------------------------------------


def _fts_match_query(query: str) -> str:
    """Build an FTS5 MATCH expression from the user's text.

    Deliberately NOT sanitised into something that always parses. Escaping
    every token into a quoted phrase would make MATCH total, but it would
    also silently reinterpret what the user typed, and it would leave the
    fallback below as dead code that rots. Passing the text through means the
    fallback is exercised constantly and is therefore known to work.

    Prefix wildcards are appended to long tokens so "pembro" reaches
    "pembrolizumab".
    """
    tokens = [t for t in query.split() if t]
    terms = [f"{t}*" if len(t) >= MIN_PREFIX_LEN else t for t in tokens]
    return " OR ".join(terms)


_SELECT = """
    SELECT entities.canonical, entities.other_names, entities.norm_all,
           bm25(entities_fts) AS fts_rank
    FROM entities_fts
    JOIN entities ON entities_fts.rowid = entities.rowid
    WHERE entities_fts MATCH ? AND entities.kind = ?
    ORDER BY fts_rank, entities.canonical
    LIMIT ?
"""

_LIKE_SELECT = """
    SELECT canonical, other_names, norm_all, 0.0 AS fts_rank
    FROM entities
    WHERE kind = ? AND norm_all LIKE ?
    ORDER BY doc_freq DESC, canonical
    LIMIT ?
"""

_LIKE_SELECT_DF = """
    SELECT canonical, other_names, norm_all, doc_freq, 0.0 AS fts_rank
    FROM entities
    WHERE kind = ? AND norm_all LIKE ?
    ORDER BY doc_freq DESC, canonical
    LIMIT ?
"""

_TOKEN_DF = """
    SELECT COUNT(*) AS df FROM entities WHERE kind = ? AND norm_all LIKE ?
"""


def _idf_rank(index: EntityIndex, tokens: set[str], rows: dict[str, sqlite3.Row]) -> list[sqlite3.Row]:
    """Order LIKE hits by the rarity of the query tokens they contain.

    The fallback originally ordered by ``doc_freq`` — entity popularity — and
    that is not a relevance signal at all. Measured on the shipped sponsor
    index: for the query "Guy's Hospital", the token ``hospital`` appears in
    513 of 2,500 entities and ``guy`` in exactly 1, and the 1 was the correct
    answer. Popularity ordering buried it under Massachusetts General
    Hospital, Chang Gung Memorial Hospital and four other large hospitals
    that matched nothing but the word "hospital".

    Summed inverse document frequency fixes it on the same principle bm25
    itself uses — a term that matches half the corpus carries almost no
    information, one that matches a single row carries almost all of it. This
    is a computed property of the corpus, not a tuned constant.
    """
    total = max(1, len(index))
    weights: dict[str, float] = {}
    for token in tokens:
        safe = token.replace("%", "").replace("_", "")
        if not safe:
            continue
        df = int(index.query(_TOKEN_DF, (index.kind, f"%{safe}%"))[0]["df"])
        weights[safe] = math.log(total / max(1, df))

    def score(row: sqlite3.Row) -> tuple[float, int]:
        norm_all = row["norm_all"] or ""
        earned = sum(w for token, w in weights.items() if token in norm_all)
        return (-earned, -int(row["doc_freq"] if "doc_freq" in row.keys() else 0))

    return sorted(rows.values(), key=score)


def _retrieve_fts(index: EntityIndex, query: str, norm: str) -> tuple[list[sqlite3.Row], str]:
    """Ranked rows plus the tier that produced them.

    bm25 first; on an FTS5 syntax error, LIKE over the normalised text. The
    LIKE arm matches on normalised CONTENT TOKENS rather than the raw string,
    because the raw string is exactly what FTS5 just rejected — a literal
    ``LIKE '%merck & co.%'`` would match nothing and turn a recoverable parse
    failure into an empty result.
    """
    try:
        match = _fts_match_query(query)
        if not match:
            return [], "bm25"
        return index.query(_SELECT, (match, index.kind, RETRIEVAL_LIMIT)), "bm25"
    except sqlite3.OperationalError:
        tokens = content_tokens(norm)
        rows: dict[str, sqlite3.Row] = {}
        for token in sorted(tokens, key=len, reverse=True):
            safe = token.replace("%", "").replace("_", "")
            if not safe:
                continue
            for row in index.query(
                _LIKE_SELECT_DF, (index.kind, f"%{safe}%", RETRIEVAL_LIMIT)
            ):
                rows.setdefault(row["canonical"], row)
        return _idf_rank(index, tokens, rows), "like-fallback"


def _shares_identity_token(norm_query: str, norm_row: str) -> bool:
    """Whether a retrieved row shares an identity-bearing token with the query.

    This absolute gate exists because bm25 has no usable absolute threshold:
    the score is corpus-relative — it depends on term frequency, document
    length and collection size — so the same value means different things in
    two queries and no constant separates a good match from a bad one. The
    same was true of the trigram cosine this replaced, where the CORRECT seed
    for "merck" scored 0.250 and the GARBAGE seed for "merck and" scored
    0.256.

    Token containment is a *condition*, not a tuned number: "Merck Sharp &
    Dohme LLC" shares the token "merck"; "Robert Jones and Agnes Hunt ... NHS
    Trust" shares only the stopword "and", which does not count. Prefix
    matching (>= MIN_PREFIX_LEN) covers truncations like "pembro" ->
    "pembrolizumab" without letting two letters match half the registry.

    Applied to the lexical arm ONLY. The semantic arm exists precisely to
    propose entities that share no token, so gating it this way would delete
    its entire reason for being; what protects that arm is the count probe.
    """
    q_tokens = content_tokens(norm_query)
    r_tokens = content_tokens(norm_row)
    if not q_tokens or not r_tokens:
        return False
    if q_tokens & r_tokens:
        return True
    for q in q_tokens:
        if len(q) < MIN_PREFIX_LEN:
            continue
        for r in r_tokens:
            if len(r) < MIN_PREFIX_LEN:
                continue
            if q.startswith(r) or r.startswith(q):
                return True
    return False


def _exact_row(index: EntityIndex, norm: str) -> tuple[sqlite3.Row, str] | None:
    """Canonical-name hit, then registry-alias hit."""
    rows = index.query(
        "SELECT canonical, other_names, norm_all FROM entities "
        "WHERE kind = ? AND norm_canonical = ? LIMIT 1",
        (index.kind, norm),
    )
    if rows:
        return rows[0], "exact"
    rows = index.query(
        "SELECT e.canonical, e.other_names, e.norm_all FROM entity_aliases a "
        "JOIN entities e ON e.rowid = a.entity_rowid "
        "WHERE a.kind = ? AND a.norm_alias = ? LIMIT 1",
        (index.kind, norm),
    )
    if rows:
        return rows[0], "alias"
    return None


def _aliases_of(row: sqlite3.Row) -> tuple[str, ...]:
    raw = row["other_names"] if "other_names" in row.keys() else ""
    return tuple(a.strip() for a in (raw or "").split("|") if a.strip())


# --------------------------------------------------------------------------
# Retriever 2 — static embeddings
# --------------------------------------------------------------------------


def _semantic_query_vector(
    index: EntityIndex, norm: str, anchor: str | None
) -> np.ndarray | None:
    """A query vector obtained WITHOUT loading a model or touching the network.

    The build-time model is 30 MB and the request path must work without it,
    so the query vector is taken from a row already in the index: the exact /
    alias hit if there is one, otherwise the top lexical hit used as an
    anchor. Semantic retrieval then answers "what else is near the thing we
    already matched", which is the useful question — it expands a shortlist
    past the lexical boundary.

    Returns None when there is no anchor, which is the honest outcome for a
    query the lexical arm could not place either.
    """
    matrix, labels = index.vectors()
    if matrix.size == 0 or not labels:
        return None
    lookup = {label: row for row, label in enumerate(labels)}
    for key in (anchor,):
        if key is not None and key in lookup:
            return matrix[lookup[key]]
    rows = index.query(
        "SELECT canonical FROM entities WHERE kind = ? AND norm_canonical = ? LIMIT 1",
        (index.kind, norm),
    )
    if rows and rows[0]["canonical"] in lookup:
        return matrix[lookup[rows[0]["canonical"]]]
    return None


def _retrieve_semantic(
    index: EntityIndex, norm: str, anchor: str | None, limit: int
) -> list[tuple[str, float]]:
    """Nearest stored entities by cosine. Pure numpy, no server.

    A few thousand rows of 256 floats is one matrix-vector product — exact,
    sub-millisecond, and with no approximate-nearest-neighbour recall loss.
    A vector database would add a process to operate, a schema to migrate and
    a request-path failure mode in exchange for solving sub-linear search over
    millions of vectors, which is not this workload.
    """
    if not index.has_semantic:
        return []
    vector = _semantic_query_vector(index, norm, anchor)
    if vector is None:
        return []
    matrix, labels = index.vectors()
    scores = matrix @ vector
    order = np.argsort(-scores)[: limit + 1]
    out: list[tuple[str, float]] = []
    for row in order:
        score = float(min(1.0, max(-1.0, scores[row])))
        if score < MIN_SEMANTIC_SCORE:
            continue
        out.append((labels[row], score))
    return out


def _row_for(index: EntityIndex, canonical: str) -> sqlite3.Row | None:
    rows = index.query(
        "SELECT canonical, other_names, norm_all FROM entities "
        "WHERE kind = ? AND canonical = ? LIMIT 1",
        (index.kind, canonical),
    )
    return rows[0] if rows else None


# --------------------------------------------------------------------------
# Resolution — proposals only
# --------------------------------------------------------------------------


def resolve(
    query: str,
    kind: Kind,
    top_k: int = 5,
    *,
    index: EntityIndex | None = None,
) -> list[Candidate]:
    """Propose entities matching ``query``. Offline, and never a number.

    Returns UNVERIFIED candidates, always — every returned ``Candidate`` has
    ``verified=False`` and ``count=None``. Nothing here may influence a
    response until :func:`verify_candidates` has confirmed it against the API.

    Union of the two retrievers. Ranking is by *agreement*, not by a blended
    score: candidates both retrievers proposed come first (two independent
    methods concurring is evidence a single score cannot express), then the
    lexical remainder in bm25 order, then at most ``MAX_SEMANTIC_ONLY``
    semantic-only proposals. bm25 and cosine are never added, averaged or
    weighted together — they have different signs, different ranges and
    different meanings, so any combination constant would be invented.
    """
    if top_k <= 0:
        return []
    idx = index if index is not None else load_index(kind)
    norm = normalize(query, kind)
    if not norm:
        return []

    # --- lexical arm -------------------------------------------------------
    lexical: list[tuple[sqlite3.Row, str, float]] = []
    seen: set[str] = set()

    exact = _exact_row(idx, norm)
    if exact is not None:
        row, via = exact
        lexical.append((row, via, 0.0))
        seen.add(row["canonical"])

    rows, tier = _retrieve_fts(idx, query, norm)
    for row in rows:
        if row["canonical"] in seen:
            continue
        if not _shares_identity_token(norm, row["norm_all"]):
            continue
        seen.add(row["canonical"])
        lexical.append((row, tier, float(row["fts_rank"])))

    # --- semantic arm ------------------------------------------------------
    anchor = lexical[0][0]["canonical"] if lexical else None
    semantic = dict(_retrieve_semantic(idx, norm, anchor, RETRIEVAL_LIMIT))

    # --- union -------------------------------------------------------------
    both = [entry for entry in lexical if entry[0]["canonical"] in semantic]
    only_lexical = [entry for entry in lexical if entry[0]["canonical"] not in semantic]

    candidates: list[Candidate] = []
    for row, via, score in both:
        candidates.append(Candidate(
            label=row["canonical"], kind=kind, score=score, aliases=_aliases_of(row),
            via=via, source="both", semantic_score=semantic[row["canonical"]],
            verified=False, count=None,
        ))
    for row, via, score in only_lexical:
        candidates.append(Candidate(
            label=row["canonical"], kind=kind, score=score, aliases=_aliases_of(row),
            via=via, source="fts", semantic_score=None, verified=False, count=None,
        ))

    admitted = 0
    for canonical, score in sorted(semantic.items(), key=lambda kv: -kv[1]):
        if admitted >= MAX_SEMANTIC_ONLY or canonical in seen:
            continue
        row = _row_for(idx, canonical)
        if row is None:
            continue
        admitted += 1
        candidates.append(Candidate(
            label=canonical, kind=kind, score=0.0, aliases=_aliases_of(row),
            via="semantic", source="semantic", semantic_score=score,
            verified=False, count=None,
        ))

    return candidates[:top_k]


# --------------------------------------------------------------------------
# Verification — the only place a number enters
# --------------------------------------------------------------------------


def _esc(value: str) -> str:
    """Strip quotes so a value cannot break out of an AREA[...] clause.

    Mirrors app/executor.py's helper deliberately rather than importing a
    private name across modules.
    """
    return value.replace('"', "").replace("\\", "")


_AREA_FIELD: dict[Kind, str] = {
    "intervention": "AREA[InterventionName]",
    "sponsor": "AREA[LeadSponsorName]",
}


def candidate_filter(candidate: Candidate, base_filter: Filter, term: str | None = None) -> Filter:
    """The exact filter a candidate will be counted under.

    An AREA clause, not ``query.intr`` / ``query.spons``, for the same reason
    the network builder uses one: plain keys are OVERWRITTEN by
    ``Filter.merged_with``, so a base filter that already carries
    ``query.intr`` would be silently replaced instead of intersected, and the
    probe would answer a wider question than the one asked.
    """
    value = _esc(term if term is not None else candidate.label)
    clause = f'{_AREA_FIELD[candidate.kind]}"{value}"'
    return base_filter.merged_with(Filter({"filter.advanced": clause}))


async def verify_candidates(
    candidates: Sequence[Candidate],
    base_filter: Filter,
    client,
    *,
    max_probes: int = MAX_VERIFY_PROBES,
) -> list[Candidate]:
    """Confirm proposals with count probes. THE point of this module.

    For each candidate, ask ClinicalTrials.gov how many studies match the
    caller's base filter intersected with that entity. Then:

        * ``totalCount == 0``  -> the candidate is DISCARDED. The index
          proposed an entity that does not exist under this filter. A
          plausible-looking name with no records behind it is exactly the
          failure a retrieval score invites, and dropping it is the
          correction.
        * ``totalCount > 0``   -> a new Candidate is returned with
          ``verified=True`` and ``count`` set to the API's integer, verbatim.

    Which retriever proposed a candidate makes NO difference here. That is
    deliberate: adding a second proposer must not add a second trust level,
    so bm25 hits, semantic hits and registry-alias hits are all confirmed the
    same way, by the same probe.

    When the canonical label itself returns 0 we retry with at most
    ``MAX_ALIAS_PROBES`` of the registry-supplied other-names, because some
    entities are only ever indexed under a brand string. That fallback is
    still a probe: nothing is assumed, and ``matched_term`` records which
    string the registry actually matched.

    Retrieval scores are carried through untouched and are never combined
    with, scaled by, or substituted for the count. Retrieval ordered the
    proposals; the API decided which survive.

    Probe budget is bounded (``max_probes``) so a long candidate list inside
    an agent loop cannot fan out without limit. Candidates past the budget are
    dropped rather than returned unverified — an unverified candidate must
    never reach a result, so "ran out of budget" degrades to "fewer entities",
    never to "unchecked entities".
    """
    verified: list[Candidate] = []
    probes = 0
    for candidate in candidates:
        if probes >= max_probes:
            break
        terms = [candidate.label, *candidate.aliases[:MAX_ALIAS_PROBES]]
        for term in terms:
            if probes >= max_probes:
                break
            probes += 1
            try:
                total = await client.count(candidate_filter(candidate, base_filter, term))
            except CTGovError:
                # The probe did not complete, so this candidate is UNVERIFIED
                # and must not survive. Dropping it degrades the shortlist;
                # keeping it would mean an entity influenced a result without
                # ever having been counted, which is the one thing this
                # module forbids. A transport failure must never be allowed
                # to resolve in favour of the candidate.
                break
            if not isinstance(total, int):
                raise TypeError(
                    f"count probe for {term!r} returned {type(total).__name__}, not an int"
                )
            if total > 0:
                verified.append(
                    replace(candidate, verified=True, count=total, matched_term=term)
                )
                break
            # total == 0: fall through to the next alias, or drop the candidate.
    return verified


async def resolve_verified(
    query: str,
    kind: Kind,
    base_filter: Filter,
    client,
    top_k: int = 5,
    *,
    index: EntityIndex | None = None,
) -> list[Candidate]:
    """Propose then confirm, in one call. The only shape callers should use.

    Every element of the result satisfies ``verified is True`` and carries an
    integer ``count`` that this process read out of an API ``totalCount``.
    Results are ordered by that count, descending — by the measurement, not by
    the rank that merely nominated them.
    """
    proposals = resolve(query, kind, top_k=top_k, index=index)
    confirmed = await verify_candidates(proposals, base_filter, client)
    return sorted(confirmed, key=lambda c: -(c.count or 0))


def index_status() -> dict:
    """Cap and provenance for every kind, for surfacing in meta/warnings.

    The bound is reported rather than buried: a shortlist drawn from a capped
    corpus can still miss a rare entity, and a reader is entitled to know the
    size of the net that was cast.
    """
    status: dict = {}
    for kind in KINDS:
        try:
            idx = load_index(kind)
        except IndexUnavailable as exc:
            status[kind] = {"available": False, "error": str(exc)}
            continue
        status[kind] = {
            "available": True,
            "entities": len(idx),
            "cap": idx.cap,
            "capped": idx.capped,
            "cap_report": idx.cap_report(),
            "backend": idx.manifest.get("backend"),
            "retrieval": idx.manifest.get("retrieval"),
            "semantic_model": idx.manifest.get("semantic_model"),
            "built_at": idx.manifest.get("built_at"),
        }
    return status


def format_candidates(candidates: Iterable[Candidate]) -> str:
    """One line per candidate. Ranks and counts kept visibly apart."""
    lines = []
    for c in candidates:
        state = f"count={c.count}" if c.verified else "UNVERIFIED (no count)"
        sem = f" cos={c.semantic_score:.3f}" if c.semantic_score is not None else ""
        term = f" via {c.matched_term!r}" if c.matched_term and c.matched_term != c.label else ""
        lines.append(
            f"  {c.label!r:42} bm25={c.score:7.3f}{sem:11} "
            f"[{c.via}/{c.source}]  {state}{term}"
        )
    return "\n".join(lines)
