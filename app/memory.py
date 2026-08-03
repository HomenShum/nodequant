"""Conversation memory for multi-step work — filters, never counts.

The whole system rests on one rule:

    The model chooses WHICH questions to ask the API. It never answers them.
    Every number is a ClinicalTrials.gov `totalCount`.

Memory is the single most dangerous place to break that rule, because memory is
the one component whose *job* is to make an earlier answer available later. If a
count from turn 1 survives into turn 3, the system has produced a number that was
not measured for the question being asked — which is indistinguishable, to a
reader, from a hallucination. It is worse than a hallucination, actually: it is a
*real* number attached to the wrong question, so every consistency check the
system has (citations, trace, reconciliation) passes on it.

So this module stores exactly one thing that can be replayed: **filters**.

    STORED                              NOT STORED
    ------------------------------      ------------------------------
    the validated QueryPlan             any Datum.count
    the PlanFilters applied             total_matching / bucket_sum
    the Dimension bucketed by           citation_total
    the VizType rendered                returned / total
    the bucket LABELS (result shape)    anything a count could hide in

A label ("PHASE3", "Germany") is the *shape* of the last answer — enough for the
planner to know what was already looked at. A count is the *answer*. The first is
context; the second is a claim. Only the first is kept.

`summarise()` is the surface where a stored number would leak into a model
prompt, so it carries a second, independent guarantee: the returned string is
built without numbers, and is then scrubbed of every multi-digit run before it is
returned. Belt and braces, because the failure is silent.

Three public pieces:

    ConversationMemory  bounded per-session turn history
    MemoryStore         bounded collection of sessions
    resolve_followup    "now compare that to Opdivo" -> a NEW QueryRequest

`resolve_followup` re-measures from scratch. It inherits the prior turn's
FILTERS and applies a delta; it never reuses a result. A follow-up costs exactly
as many API probes as the same question asked cold.
"""

from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .models import QueryRequest, QueryResponse, TrialPhase, TrialStatus, VizType
from .planner import Dimension, PlanFilters, QueryPlan

# --------------------------------------------------------------------------
# Bounds
#
# An unbounded session store is not a slow leak, it is a fast one: an agent loop
# issuing follow-ups holds one session open and appends forever. Every container
# here has a MAX and an eviction policy, and the caps are enforced on write, not
# on read, so memory cannot grow between reads.
# --------------------------------------------------------------------------

MAX_TURNS = 8
MAX_BYTES = 16_384
MAX_SESSIONS = 256

#: Per-turn caps, applied before storage so one turn can never exceed the
#: session budget on its own (which would make byte eviction unable to converge).
MAX_STORED_LABELS = 24
MAX_LABEL_CHARS = 64
MAX_QUERY_CHARS = 500

#: Two or more digits in a row. Deliberately stricter than the executor's
#: 3+-digit rule: in a recap even a two-digit figure reads as an answer.
_MULTI_DIGIT = re.compile(r"\d[\d,]*\d")


# --------------------------------------------------------------------------
# A turn
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Turn:
    """One question and the SHAPE of its answer. No quantity, by construction.

    There is no field on this dataclass that can hold a count. That is the point
    — the invariant is enforced by the type, not by a rule someone has to
    remember when adding a field later.
    """

    query: str
    plan: QueryPlan
    filters: PlanFilters
    dimension: Dimension
    viz_type: VizType
    labels: tuple[str, ...] = ()
    max_citations_per_datum: int = 3

    @property
    def subject(self) -> str:
        """The entity this turn was about, for the recap. Names only."""
        f = self.filters
        return f.drug_name or f.condition or f.sponsor or f.location or "all trials"

    def size_bytes(self) -> int:
        """Approximate stored size, used for byte-budget eviction.

        Measured against the JSON encoding rather than `sys.getsizeof`, because
        that is what actually scales with content and what a caller can reason
        about when picking `max_bytes`.
        """
        payload = {
            "query": self.query,
            "plan": self.plan.model_dump(mode="json"),
            "dimension": self.dimension.value,
            "viz_type": self.viz_type.value,
            "labels": list(self.labels),
        }
        return len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def _clean_labels(labels: Iterable[str]) -> tuple[str, ...]:
    """Bound the label list. Labels are shape; they are still attacker-shaped text."""
    out: list[str] = []
    for label in labels:
        if not isinstance(label, str):
            continue
        text = label.strip()[:MAX_LABEL_CHARS]
        if text:
            out.append(text)
        if len(out) >= MAX_STORED_LABELS:
            break
    return tuple(out)


# --------------------------------------------------------------------------
# Per-session memory
# --------------------------------------------------------------------------


class ConversationMemory:
    """Bounded turn history for ONE session.

    Bounded twice, because the two bounds fail differently: `max_turns` caps how
    much context the planner sees, `max_bytes` caps what the process holds. A
    turn whose plan carries long free-text fields can blow the byte budget long
    before the turn count is reached.

    Eviction is oldest-first. The newest turn is never evicted — a memory that
    drops the turn a follow-up refers to is worse than no memory, because
    `resolve_followup` would then silently produce a fresh query while the user
    believes their context was carried.
    """

    def __init__(
        self,
        session_id: str,
        max_turns: int = MAX_TURNS,
        max_bytes: int = MAX_BYTES,
    ) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be at least 1")
        if max_bytes < 1:
            raise ValueError("max_bytes must be at least 1")
        self.session_id = session_id
        self.max_turns = max_turns
        self.max_bytes = max_bytes
        self._turns: list[Turn] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------- reading

    @property
    def turns(self) -> tuple[Turn, ...]:
        with self._lock:
            return tuple(self._turns)

    @property
    def last(self) -> Turn | None:
        with self._lock:
            return self._turns[-1] if self._turns else None

    def size_bytes(self) -> int:
        return sum(t.size_bytes() for t in self.turns)

    def __len__(self) -> int:
        return len(self.turns)

    # ------------------------------------------------------------- writing

    def record(
        self,
        query: str,
        plan: QueryPlan,
        labels: Iterable[str] = (),
        viz_type: VizType | None = None,
        max_citations_per_datum: int = 3,
    ) -> Turn:
        """Store one turn. Accepts a plan and the result SHAPE — never counts.

        There is deliberately no `counts` parameter and no `**extra`. A caller
        that wants to remember a number has to change this signature, which is a
        code review, which is the control.
        """
        turn = Turn(
            query=(query or "").strip()[:MAX_QUERY_CHARS],
            plan=plan,
            filters=plan.filters,
            dimension=plan.dimension,
            viz_type=viz_type or plan.viz_type,
            labels=_clean_labels(labels),
            max_citations_per_datum=max_citations_per_datum,
        )
        with self._lock:
            self._turns.append(turn)
            self._evict_locked()
        return turn

    def record_response(
        self, query: str, plan: QueryPlan, response: QueryResponse
    ) -> Turn:
        """Convenience wrapper that extracts ONLY the shape from a response.

        Note what is read off `response`: `label` and `type`. Not `count`, not
        `total`, not `total_matching`. This function is the natural place for a
        count to sneak in, so it is written to make the omission obvious.
        """
        viz = response.visualization
        labels = [d.label for d in viz.data] or [n.label for n in viz.nodes]
        return self.record(
            query=query,
            plan=plan,
            labels=labels,
            viz_type=viz.type,
        )

    def clear(self) -> None:
        with self._lock:
            self._turns.clear()

    def _evict_locked(self) -> None:
        """Drop oldest turns until both budgets are satisfied."""
        while len(self._turns) > self.max_turns:
            self._turns.pop(0)
        # Never evict to empty: a lone oversized turn is kept (its own fields are
        # already capped at write time, so it cannot grow without bound).
        while len(self._turns) > 1 and sum(t.size_bytes() for t in self._turns) > self.max_bytes:
            self._turns.pop(0)


# --------------------------------------------------------------------------
# Session store
# --------------------------------------------------------------------------


class MemoryStore:
    """A bounded, LRU collection of `ConversationMemory` objects.

    Sessions are the outer unbounded dimension. A per-session cap does nothing
    if a new session id can be minted per request — which is exactly what an
    agent loop, or an unauthenticated endpoint, does.
    """

    def __init__(
        self,
        max_sessions: int = MAX_SESSIONS,
        max_turns: int = MAX_TURNS,
        max_bytes: int = MAX_BYTES,
    ) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions must be at least 1")
        self.max_sessions = max_sessions
        self.max_turns = max_turns
        self.max_bytes = max_bytes
        self._sessions: OrderedDict[str, ConversationMemory] = OrderedDict()
        self._lock = threading.Lock()

    def for_session(self, session_id: str) -> ConversationMemory:
        key = (session_id or "").strip() or "default"
        with self._lock:
            memory = self._sessions.get(key)
            if memory is None:
                memory = ConversationMemory(key, self.max_turns, self.max_bytes)
                self._sessions[key] = memory
            self._sessions.move_to_end(key)
            while len(self._sessions) > self.max_sessions:
                self._sessions.popitem(last=False)
            return memory

    def drop(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    @property
    def session_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._sessions)


# --------------------------------------------------------------------------
# Recap for the planner prompt
# --------------------------------------------------------------------------

_DIMENSION_RECAP: dict[Dimension, str] = {
    Dimension.PHASE: "phase",
    Dimension.STATUS: "recruitment status",
    Dimension.YEAR: "start year",
    Dimension.COUNTRY: "country",
    Dimension.SPONSOR: "sponsor",
    Dimension.INTERVENTION_TYPE: "intervention type",
    Dimension.ENROLLMENT: "enrollment size",
    Dimension.ENROLLMENT_BY_PHASE: "enrollment against phase",
    Dimension.SPONSOR_DRUG_NETWORK: "a sponsor-drug network",
    Dimension.DRUG_DRUG_NETWORK: "a drug co-occurrence network",
    Dimension.NONE: "a single total",
}


def scrub_numbers(text: str) -> str:
    """Remove every multi-digit run.

    The last line of defence for `summarise`. A recap containing "45,231" would
    be read by the planner as a fact about the world, and the planner has no way
    to tell a remembered measurement from an invented one — both arrive as
    tokens in a prompt. So no multi-digit run leaves this module at all, even one
    that is genuinely a year: losing "since 2020" from a recap costs nothing
    (the filter itself is carried structurally by `resolve_followup`), while
    keeping it would mean the scrub has exceptions, and an exception is a hole.
    """
    cleaned = _MULTI_DIGIT.sub("", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" +([,.;:])", r"\1", cleaned)
    return cleaned.strip()


def _filter_phrases(filters: PlanFilters) -> list[str]:
    """Name filters WITHOUT their values where the value is a number."""
    parts: list[str] = []
    if filters.drug_name:
        parts.append(f"drug {filters.drug_name}")
    if filters.condition:
        parts.append(f"condition {filters.condition}")
    if filters.sponsor:
        parts.append(f"sponsor {filters.sponsor}")
    if filters.location:
        parts.append(f"location {filters.location}")
    if filters.phase:
        parts.append("phase " + "/".join(p.value for p in filters.phase))
    if filters.status:
        parts.append("status " + "/".join(s.value for s in filters.status))
    # Years are named as constraints, never as values — see `scrub_numbers`.
    if filters.start_year and filters.end_year:
        parts.append("a start-date window")
    elif filters.start_year:
        parts.append("an earliest-start-year constraint")
    elif filters.end_year:
        parts.append("a latest-start-year constraint")
    return parts


def summarise(memory: ConversationMemory | None) -> str:
    """A short, model-readable recap of prior turns, for the planner prompt.

    Names entities and dimensions. Contains no quantities of any kind — see
    `scrub_numbers`. The closing line is not decoration: without it a planner
    reading "asked about melanoma by phase" may try to be helpful and restate a
    figure it thinks it saw. Saying explicitly that no result is available makes
    the absence deliberate rather than an omission to be filled in.
    """
    turns = memory.turns if memory is not None else ()
    if not turns:
        return ""

    lines = ["Earlier in this conversation (context only — no results are carried over):"]
    for turn in turns:
        dimension = _DIMENSION_RECAP.get(turn.dimension, turn.dimension.value)
        line = f"- asked about {turn.subject}, grouped by {dimension}"
        filters = _filter_phrases(turn.filters)
        if filters:
            line += "; filters: " + ", ".join(filters)
        line += f"; rendered as {turn.viz_type.value}"
        lines.append(line)

    latest = _filter_phrases(turns[-1].filters)
    if latest:
        lines.append("Filters still in effect unless changed: " + ", ".join(latest) + ".")
    lines.append(
        "No counts, totals or findings from those turns are available. Every number "
        "must be measured from the API for the question being planned now."
    )
    return scrub_numbers("\n".join(lines))


# --------------------------------------------------------------------------
# Follow-up resolution
# --------------------------------------------------------------------------

# An anaphor is what makes a sentence elliptical: it points at something outside
# itself. Its presence is the strongest available signal that inheriting context
# is what the user meant.
_ANAPHOR = re.compile(
    r"\b(that|those|these|this|them|it|its|same|previous|prior|above|before)\b", re.I
)

# Openers that continue rather than start. Matched at the beginning only: "and"
# in the middle of a sentence is a conjunction, not a continuation marker.
_CONTINUATION = re.compile(
    r"^\s*(?:ok(?:ay)?[,\s]+)?"
    r"(now|then|and|but|also|instead|next|"
    r"drill\s+(?:in(?:to)?|down)|narrow|restrict|limit|filter|"
    r"just|only|split|break\s+(?:it\s+)?(?:down|out)|"
    r"what\s+about|how\s+about|same)\b",
    re.I,
)

# Elliptical by construction — "what about melanoma" cannot stand alone.
_ELLIPTICAL = re.compile(r"^\s*(?:what|how)\s+about\b", re.I)

# A self-contained interrogative. If the user wrote one of these and gave us no
# anaphor, they asked a NEW question.
_INTERROGATIVE = re.compile(
    r"^\s*(?:(?:now|then|and|but|also|ok|okay)[,\s]+)*"
    r"(how|what|which|where|who|when|show|list|give|find|plot|chart|graph|display)\b",
    re.I,
)

_PHASE_WORDS: dict[str, TrialPhase] = {
    "early phase 1": TrialPhase.EARLY_PHASE1,
    "early phase1": TrialPhase.EARLY_PHASE1,
    "early_phase1": TrialPhase.EARLY_PHASE1,
    "phase 1": TrialPhase.PHASE1,
    "phase1": TrialPhase.PHASE1,
    "phase i": TrialPhase.PHASE1,
    "phase 2": TrialPhase.PHASE2,
    "phase2": TrialPhase.PHASE2,
    "phase ii": TrialPhase.PHASE2,
    "phase 3": TrialPhase.PHASE3,
    "phase3": TrialPhase.PHASE3,
    "phase iii": TrialPhase.PHASE3,
    "phase 4": TrialPhase.PHASE4,
    "phase4": TrialPhase.PHASE4,
    "phase iv": TrialPhase.PHASE4,
}

_STATUS_WORDS: dict[str, TrialStatus] = {
    "not yet recruiting": TrialStatus.NOT_YET_RECRUITING,
    "active not recruiting": TrialStatus.ACTIVE_NOT_RECRUITING,
    "active, not recruiting": TrialStatus.ACTIVE_NOT_RECRUITING,
    "recruiting": TrialStatus.RECRUITING,
    "completed": TrialStatus.COMPLETED,
    "terminated": TrialStatus.TERMINATED,
    "withdrawn": TrialStatus.WITHDRAWN,
    "suspended": TrialStatus.SUSPENDED,
}

# Curated, deliberately finite. A regex for "in <Capitalised word>" would read
# "in Phase 3" and "in Total" as countries; a closed list cannot.
_COUNTRIES = {
    "united states", "usa", "u.s.", "us", "america", "canada", "mexico", "brazil",
    "argentina", "chile", "colombia", "united kingdom", "uk", "england", "scotland",
    "ireland", "france", "germany", "spain", "italy", "netherlands", "belgium",
    "switzerland", "austria", "sweden", "norway", "denmark", "finland", "poland",
    "portugal", "greece", "czechia", "czech republic", "hungary", "romania",
    "russia", "ukraine", "turkey", "israel", "saudi arabia",
    "united arab emirates", "egypt", "south africa", "nigeria", "kenya", "india",
    "china", "japan", "south korea", "korea", "taiwan", "hong kong", "singapore",
    "thailand", "vietnam", "malaysia", "indonesia", "philippines", "australia",
    "new zealand", "europe", "asia",
}

# Drug-name morphology. WHO stem suffixes plus a handful of brands that do not
# follow them. This decides which SLOT a name goes in, not whether the name is
# real — the API decides that, as it does everywhere else in this system.
_DRUG_SUFFIXES = (
    "mab", "nib", "tinib", "ciclib", "parib", "lisib", "rafenib", "zomib",
    "limus", "cept", "tide", "sartan", "prazole", "mycin", "cycline", "platin",
    "taxel", "rubicin", "vastatin", "gliptin", "glutide", "ximab", "zumab",
    "stat", "vir", "olol", "azole", "pril", "cillin", "setron", "afil",
)
_DRUG_BRANDS = {
    "keytruda", "opdivo", "yervoy", "tecentriq", "imfinzi", "bavencio",
    "herceptin", "avastin", "rituxan", "revlimid", "humira", "ozempic",
    "wegovy", "mounjaro", "enhertu", "darzalex", "kisqali", "ibrance",
    "verzenio", "tagrisso", "lynparza", "jakafi", "xtandi", "erleada",
    "keytruda®", "trodelvy", "padcev", "aspirin", "metformin", "insulin",
}
_CONDITION_MARKERS = (
    "cancer", "carcinoma", "sarcoma", "lymphoma", "leukemia", "leukaemia",
    "myeloma", "melanoma", "glioma", "glioblastoma", "tumor", "tumour",
    "neoplasm", "diabetes", "asthma", "copd", "alzheimer", "parkinson",
    "dementia", "depression", "anxiety", "schizophrenia", "obesity",
    "hypertension", "stroke", "epilepsy", "arthritis", "psoriasis", "eczema",
    "hiv", "covid", "influenza", "hepatitis", "cirrhosis", "sclerosis",
    "fibrosis", "anemia", "anaemia", "syndrome", "disease", "disorder",
    "failure", "infection", "itis", "osis", "pathy", "emia",
)

_DIMENSION_PATTERNS: tuple[tuple[re.Pattern[str], Dimension], ...] = (
    (re.compile(r"\b(network|co-?occur|combination(?:s)?\s+with)\b", re.I), Dimension.SPONSOR_DRUG_NETWORK),
    (re.compile(r"\b(enrol?ment|participants?|how\s+large|trial\s+size|sizes)\b", re.I), Dimension.ENROLLMENT),
    (re.compile(r"\b(per\s+year|over\s+time|by\s+year|each\s+year|yearly|annually|trend)\b", re.I), Dimension.YEAR),
    (re.compile(r"\b(countr(?:y|ies)|geograph|by\s+region|where)\b", re.I), Dimension.COUNTRY),
    (re.compile(r"\bby\s+sponsor|\bsponsors\b|\bwho\s+sponsors\b", re.I), Dimension.SPONSOR),
    (re.compile(r"\b(by\s+status|recruitment\s+status|by\s+recruitment)\b", re.I), Dimension.STATUS),
    (re.compile(r"\bintervention\s+type\b", re.I), Dimension.INTERVENTION_TYPE),
    (re.compile(r"\b(by\s+phase|across\s+phases|phase\s+(?:distribution|breakdown))\b", re.I), Dimension.PHASE),
)

# Query prose per dimension. Also chosen so the OFFLINE keyword planner
# (`planner.fallback_plan`) routes each one to the same dimension the LLM would.
# A follow-up must behave identically with and without credentials, or the
# feature is only testable with a key.
_DIMENSION_PROSE: dict[Dimension, str] = {
    Dimension.PHASE: "distributed across phases",
    Dimension.STATUS: "broken down by recruitment status",
    Dimension.YEAR: "per year",
    Dimension.COUNTRY: "by country",
    Dimension.SPONSOR: "by sponsor",
    Dimension.INTERVENTION_TYPE: "by intervention type",
    Dimension.ENROLLMENT: "by enrollment size",
    Dimension.ENROLLMENT_BY_PHASE: "by enrollment and phase",
    Dimension.SPONSOR_DRUG_NETWORK: "as a sponsor and drug network",
    Dimension.DRUG_DRUG_NETWORK: "as a drug co-occurrence network",
    Dimension.NONE: "",
}


def classify_entity(name: str) -> str:
    """Which filter slot a bare entity name belongs in: 'drug' or 'condition'.

    Morphology only. Getting this wrong costs a wrong-but-visible filter (the
    response states `filters_applied`, and a wrong slot returns few or no
    trials); getting it wrong is therefore recoverable, whereas asking a model to
    decide would put an unverified assertion on the path to a number.
    """
    low = name.strip().lower()
    if low in _DRUG_BRANDS:
        return "drug"
    if any(marker in low for marker in _CONDITION_MARKERS):
        return "condition"
    last = low.split()[-1] if low.split() else low
    if any(last.endswith(suffix) for suffix in _DRUG_SUFFIXES):
        return "drug"
    return "condition"


@dataclass
class _Delta:
    """What the follow-up changes, relative to the inherited filters."""

    updates: dict[str, Any] = field(default_factory=dict)
    dimension: Dimension | None = None
    compare_with: str | None = None
    matched: bool = False


def _match_phases(text: str) -> list[TrialPhase] | None:
    found: list[TrialPhase] = []
    for word, phase in _PHASE_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", text, re.I) and phase not in found:
            found.append(phase)
    return found or None


def _match_statuses(text: str) -> list[TrialStatus] | None:
    found: list[TrialStatus] = []
    for word, status in _STATUS_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", text, re.I) and status not in found:
            found.append(status)
    # "not yet recruiting" also matches "recruiting"; keep the longest reading.
    if TrialStatus.NOT_YET_RECRUITING in found or TrialStatus.ACTIVE_NOT_RECRUITING in found:
        found = [s for s in found if s is not TrialStatus.RECRUITING]
    return found or None


def _match_years(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    if m := re.search(r"\bfrom\s+(\d{4})\s+(?:to|through|until|-)\s*(\d{4})\b", text, re.I):
        out["start_year"], out["end_year"] = int(m.group(1)), int(m.group(2))
        return out
    if m := re.search(r"\bbetween\s+(\d{4})\s+and\s+(\d{4})\b", text, re.I):
        out["start_year"], out["end_year"] = int(m.group(1)), int(m.group(2))
        return out
    if m := re.search(r"\b(?:since|after|from|starting)\s+(\d{4})\b", text, re.I):
        out["start_year"] = int(m.group(1))
        return out
    if m := re.search(r"\b(?:before|until|up\s+to|prior\s+to)\s+(\d{4})\b", text, re.I):
        out["end_year"] = int(m.group(1))
        return out
    if m := re.search(r"\b(?:in|during|for)\s+(\d{4})\b", text, re.I):
        out["start_year"] = out["end_year"] = int(m.group(1))
        return out
    return out


def _match_location(text: str) -> str | None:
    for country in sorted(_COUNTRIES, key=len, reverse=True):
        if re.search(rf"\b(?:in|from|within)\s+{re.escape(country)}\b", text, re.I):
            return country.title() if country.islower() else country
    return None


_COMPARE_TAIL = re.compile(
    r"\b(?:compare(?:d)?(?:\s+\S+){0,3}?\s+(?:to|with|against)|versus|vs\.?|"
    r"\bagainst)\s+(?P<name>[A-Za-z0-9][\w' \-]{1,60})",
    re.I,
)
_ABOUT_TAIL = re.compile(
    r"^\s*(?:what|how)\s+about\s+(?P<name>[A-Za-z0-9][\w' \-]{1,60})", re.I
)
_SWITCH_TAIL = re.compile(
    r"\b(?:switch\s+to|change\s+to|do|try|now\s+do|instead\s+use)\s+"
    r"(?P<name>[A-Za-z0-9][\w' \-]{1,60})",
    re.I,
)

_STOPWORD_NAMES = {
    "that", "this", "those", "these", "it", "them", "same", "phase", "phases",
    "status", "year", "years", "country", "countries", "sponsor", "sponsors",
    "trials", "trial", "the", "a", "an", "total", "enrollment", "network",
}


def _extract_entity(text: str) -> tuple[str | None, bool]:
    """Return (entity name, is_comparison).

    Trailing words like "trials" are stripped so "what about melanoma trials"
    and "what about melanoma" produce the same filter.
    """
    for pattern, is_comparison in (
        (_COMPARE_TAIL, True),
        (_ABOUT_TAIL, False),
        (_SWITCH_TAIL, False),
    ):
        m = pattern.search(text)
        if not m:
            continue
        name = m.group("name").strip(" .,?!;:'\"-")
        words = [w for w in name.split() if w.lower() not in {"trials", "trial", "studies", "study"}]
        name = " ".join(words[:4]).strip()
        if not name or name.lower() in _STOPWORD_NAMES:
            continue
        if name.lower() in _COUNTRIES:
            continue
        if _match_phases(name) or _match_statuses(name) or re.fullmatch(r"\d{4}", name):
            continue
        return name[:100], is_comparison
    return None, False


def _detect_dimension(text: str) -> Dimension | None:
    """A requested regrouping, if any. `None` means 'keep the prior dimension'."""
    if re.search(r"\b(?:how\s+many|total|count)\b", text, re.I) and not re.search(
        r"\b(?:by|per|across|each)\b", text, re.I
    ):
        return Dimension.NONE
    for pattern, dimension in _DIMENSION_PATTERNS:
        if pattern.search(text):
            if dimension is Dimension.SPONSOR_DRUG_NETWORK and re.search(
                r"\bdrug", text, re.I
            ) and not re.search(r"\bsponsor", text, re.I):
                return Dimension.DRUG_DRUG_NETWORK
            if dimension is Dimension.ENROLLMENT and re.search(r"\bphase", text, re.I):
                return Dimension.ENROLLMENT_BY_PHASE
            return dimension
    return None


def is_followup(text: str, memory: ConversationMemory | None = None) -> bool:
    """Is this an elliptical continuation rather than a new question?

    The asymmetry here is deliberate and is the whole safety argument for this
    function. A missed follow-up costs the user a re-typed filter. A FALSE
    follow-up silently applies a stale constraint to a fresh question and returns
    a correct number for a question nobody asked — the same failure class as a
    replayed count, arriving by a different door. So:

        a self-contained interrogative with no anaphor is NEVER a follow-up,
        even when it opens with "now".

    "Now how are lung cancer trials distributed across phases?" is a new
    question that happens to start with "now". It must not inherit melanoma.
    """
    if memory is not None and not memory.turns:
        return False
    raw = (text or "").strip()
    if not raw:
        return False
    has_anaphor = bool(_ANAPHOR.search(raw))
    if _ELLIPTICAL.match(raw):
        return True
    if _INTERROGATIVE.match(raw) and not has_anaphor:
        return False
    return has_anaphor or bool(_CONTINUATION.match(raw))


def _compose_query(filters: dict[str, Any], dimension: Dimension) -> str:
    """Build a self-contained question from inherited filters + delta.

    Rebuilt from the structured filters rather than by string-patching the
    user's words, so the prose and the explicit `QueryRequest` fields cannot
    disagree. The explicit fields are what actually bind (`merged_with_request`
    lets them override the planner); the prose exists so the planner picks a
    sensible dimension and title.
    """
    subject_bits: list[str] = []
    if filters.get("drug_name"):
        subject_bits.append(f"{filters['drug_name']}")
    if filters.get("condition"):
        subject_bits.append(f"{filters['condition']}")
    subject = " ".join(subject_bits) if subject_bits else "clinical"

    clauses: list[str] = []
    if filters.get("sponsor"):
        clauses.append(f"sponsored by {filters['sponsor']}")
    if filters.get("location"):
        clauses.append(f"in {filters['location']}")
    if filters.get("phase"):
        phases = "/".join(p.value for p in filters["phase"])
        clauses.append(f"in {phases}")
    if filters.get("status"):
        statuses = "/".join(s.value for s in filters["status"])
        clauses.append(f"with status {statuses}")
    if filters.get("start_year") and filters.get("end_year"):
        clauses.append(f"started between {filters['start_year']} and {filters['end_year']}")
    elif filters.get("start_year"):
        clauses.append(f"started since {filters['start_year']}")
    elif filters.get("end_year"):
        clauses.append(f"started before {filters['end_year']}")

    tail = (" " + " ".join(clauses)) if clauses else ""
    prose = _DIMENSION_PROSE.get(dimension, "")

    if dimension is Dimension.NONE:
        text = f"How many {subject} trials are there in total{tail}?"
    else:
        text = f"{subject} trials{tail} {prose}"

    text = re.sub(r"\s{2,}", " ", text).strip()
    return text[:MAX_QUERY_CHARS]


def resolve_followup(
    text: str, memory: ConversationMemory | None
) -> QueryRequest | None:
    """Turn an elliptical follow-up into a fresh, fully-specified QueryRequest.

    Returns `None` when the text is not a follow-up, when there is nothing to
    inherit, or when no delta could be identified — three different ways of
    saying "treat this as a new question", which is always the safe default.

    What is inherited: FILTERS. What is not inherited: everything else. The
    returned request goes through the same planner and the same executor as a
    cold question, issues the same probes, and produces numbers measured for
    THIS question. There is no path by which the previous turn's counts reach
    the response, because the previous turn's counts were never stored.
    """
    if memory is None:
        return None
    last = memory.last
    if last is None:
        return None
    raw = (text or "").strip()
    if not raw or not is_followup(raw, memory):
        return None

    inherited: dict[str, Any] = {
        "drug_name": last.filters.drug_name,
        "condition": last.filters.condition,
        "sponsor": last.filters.sponsor,
        "location": last.filters.location,
        "phase": list(last.filters.phase) if last.filters.phase else None,
        "status": list(last.filters.status) if last.filters.status else None,
        "start_year": last.filters.start_year,
        "end_year": last.filters.end_year,
    }

    delta = _Delta()

    if phases := _match_phases(raw):
        delta.updates["phase"] = phases
        delta.matched = True
    if statuses := _match_statuses(raw):
        delta.updates["status"] = statuses
        delta.matched = True
    if years := _match_years(raw):
        # A year delta replaces the whole window rather than merging into it:
        # "same but since 2020" with an inherited end_year of 2015 would
        # otherwise compose into an empty, and silently wrong, range.
        delta.updates["start_year"] = years.get("start_year")
        delta.updates["end_year"] = years.get("end_year")
        delta.matched = True
    if location := _match_location(raw):
        delta.updates["location"] = location
        delta.matched = True

    entity, is_comparison = _extract_entity(raw)
    if entity:
        slot = "drug_name" if classify_entity(entity) == "drug" else "condition"
        if is_comparison and slot == "drug_name" and inherited.get("drug_name"):
            # Two named drugs: this is a real A-vs-B comparison. Leave BOTH out
            # of the explicit fields — pinning `drug_name` would collapse the
            # comparison to one arm — and let the planner emit `compare_values`,
            # which the executor measures as separate series.
            delta.compare_with = entity
        else:
            delta.updates[slot] = entity
        delta.matched = True

    if (dimension := _detect_dimension(raw)) is not None:
        delta.dimension = dimension
        delta.matched = True

    if not delta.matched:
        # An anaphor with no identifiable change ("what about that?") is not
        # actionable. Re-running the previous query verbatim would spend probes
        # to redraw the same chart; saying nothing lets the caller ask again.
        return None

    merged = {**inherited, **delta.updates}
    dimension = delta.dimension or last.dimension
    compare_with = delta.compare_with

    if compare_with:
        # The comparison arms travel in the prose only.
        query_text = (
            f"Compare {merged['drug_name']} and {compare_with}"
            + (f" for {merged['condition']}" if merged.get("condition") else "")
            + " trials "
            + _DIMENSION_PROSE.get(dimension, "")
        )
        query_text = re.sub(r"\s{2,}", " ", query_text).strip()[:MAX_QUERY_CHARS]
        merged["drug_name"] = None
    else:
        query_text = _compose_query(merged, dimension)

    if len(query_text) < 3:
        return None

    return QueryRequest(
        query=query_text,
        drug_name=merged.get("drug_name"),
        condition=merged.get("condition"),
        sponsor=merged.get("sponsor"),
        location=merged.get("location"),
        phase=merged.get("phase"),
        status=merged.get("status"),
        start_year=merged.get("start_year"),
        end_year=merged.get("end_year"),
        max_citations_per_datum=last.max_citations_per_datum,
    )
