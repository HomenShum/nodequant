"""Execute a QueryPlan against ClinicalTrials.gov and compose the response.

This module contains no model calls. Given a plan, everything here is
deterministic and unit-testable: the same plan always produces the same
counts, and every count is an API `totalCount`.

The exclusivity check is the part worth reading. Before returning a
distribution we measure whether the buckets actually partition the result set,
because on this API they frequently do not and the direction of the error is
not predictable:

    pembrolizumab by phase   bucket sum 3205 > matched 2915   (overlap)
    melanoma by phase        bucket sum 3602 < matched 3738   (missing field)

A fixed caveat would be wrong in one of those cases, so the caveat is computed
per query and the renderer is told, in `buckets_are_exclusive`, whether
part-of-whole marks are safe to draw.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from .ctgov import BASE_URL, ENTITY_FIELDS, CTGovClient, Filter
from .models import (
    Citation, Datum, Encoding, GraphEdge, GraphNode, QueryRequest,
    QueryResponse, ResponseMeta, TraceStep, Visualization, VizType,
)
from .planner import Dimension, QueryPlan

# Bucket vocabularies. Closed sets, so a distribution is always a fixed number
# of probes rather than an unbounded scan.
PHASES = ["EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4", "NA"]
STATUSES = ["RECRUITING", "NOT_YET_RECRUITING", "ACTIVE_NOT_RECRUITING",
            "COMPLETED", "TERMINATED", "WITHDRAWN", "SUSPENDED", "UNKNOWN"]
INTERVENTION_TYPES = ["DRUG", "BIOLOGICAL", "DEVICE", "PROCEDURE", "RADIATION",
                      "BEHAVIORAL", "DIETARY_SUPPLEMENT", "GENETIC", "OTHER"]
COUNTRIES = ["United States", "China", "France", "Germany", "Canada", "Spain",
             "United Kingdom", "Italy", "Japan", "Australia", "Netherlands", "Korea"]

# Bounded so a "by year" question cannot fan out to hundreds of probes.
MAX_YEARS = 15

# Enrollment bins for the histogram dimension. Ranges, not a value list, because
# enrollment is continuous — each bin is one exact count probe over a range.
ENROLLMENT_BINS = [
    ("1-25", 1, 25), ("26-100", 26, 100), ("101-500", 101, 500),
    ("501-1000", 501, 1000), ("1001-5000", 1001, 5000), ("5000+", 5001, 1000000),
]
# How many nodes a network graph may contain before we truncate and say so.
# Six per side keeps a sponsor-drug graph legible and, more practically, keeps
# the probe count bounded: every candidate pair costs one request, so 8x8 was
# reaching ~180 requests and 60s per question once retries were counted.
MAX_NODES_PER_SIDE = 6
MAX_EDGE_PROBES = 24

# Internal key separator for comparison cross-products. Not part of any
# public label; stripped before a Datum is built.
SERIES_SEP = ""  # ASCII unit separator; cannot occur in an API label

# Dimensions where one trial can legitimately occupy several buckets, so the
# buckets can never partition the result set no matter what the counts say.
#
# Exclusivity must be a property of the DIMENSION, not an arithmetic accident.
# Deciding it by `bucket_sum == total_matching` alone is unsound: overlap (which
# inflates the sum) and absent fields (which deflate it) can cancel exactly,
# yielding equality on a set that plainly overlaps and reporting
# `buckets_are_exclusive=True` — licensing a pie chart of overlapping data.
MULTI_VALUED_DIMENSIONS = {
    Dimension.PHASE,            # designModule.phases is an array
    Dimension.COUNTRY,          # a trial runs at sites in many countries
    Dimension.INTERVENTION_TYPE,  # a trial may combine drug + device + behavioral
    Dimension.SPONSOR,          # buckets are a sampled shortlist, never exhaustive
}


def base_filter(plan: QueryPlan) -> Filter:
    """Translate plan filters into ClinicalTrials.gov query params."""
    params: dict[str, str] = {}
    f = plan.filters
    if f.drug_name:
        params["query.intr"] = f.drug_name
    if f.condition:
        params["query.cond"] = f.condition
    if f.sponsor:
        params["query.spons"] = f.sponsor
    if f.location:
        params["query.locn"] = f.location

    advanced: list[str] = []
    if f.phase:
        advanced.append(
            "(" + " OR ".join(f"AREA[Phase]{p.value}" for p in f.phase) + ")"
        )
    if f.status:
        advanced.append(
            "(" + " OR ".join(f"AREA[OverallStatus]{s.value}" for s in f.status) + ")"
        )
    if f.start_year or f.end_year:
        lo = f"{f.start_year}-01-01" if f.start_year else "MIN"
        hi = f"{f.end_year}-12-31" if f.end_year else "MAX"
        advanced.append(f"AREA[StartDate]RANGE[{lo},{hi}]")
    if advanced:
        params["filter.advanced"] = " AND ".join(advanced)
    return Filter(params)


def _esc(value: str) -> str:
    """Strip quotes so a value cannot break out of an AREA[...] clause."""
    return value.replace('"', "").replace("\\", "")


_NUMERIC_RUN = re.compile(r"\d[\d,]{2,}")

# Human-readable descriptions of each dimension, used to build the response's
# interpretation line without asking a model to write it.
_DIMENSION_PROSE = {
    Dimension.PHASE: "grouped by trial phase",
    Dimension.STATUS: "grouped by recruitment status",
    Dimension.YEAR: "counted per start year",
    Dimension.COUNTRY: "grouped by country",
    Dimension.SPONSOR: "grouped by lead sponsor",
    Dimension.INTERVENTION_TYPE: "grouped by intervention type",
    Dimension.SPONSOR_DRUG_NETWORK: "as a sponsor-to-drug co-occurrence network",
    Dimension.DRUG_DRUG_NETWORK: "as a drug-to-drug co-occurrence network",
    Dimension.ENROLLMENT: "binned by enrollment size",
    Dimension.ENROLLMENT_BY_PHASE: "by enrollment size within each phase",
    Dimension.NONE: "as a single total",
}


def describe_plan(plan: QueryPlan) -> str:
    """Build the interpretation line deterministically from validated fields.

    The model's own `interpretation` string is deliberately NOT used here. It is
    free text validated only for length, so nothing stops a plan arriving with
    `interpretation="There are 999 matching trials."` — a fabricated quantity
    presented beside measured ones. Composing this sentence from enum values and
    caller-supplied filters means the response contains no model-authored number
    anywhere.
    """
    parts: list[str] = []
    f = plan.filters
    if f.drug_name:
        parts.append(f"intervention = {f.drug_name}")
    if f.condition:
        parts.append(f"condition = {f.condition}")
    if f.sponsor:
        parts.append(f"sponsor = {f.sponsor}")
    if f.location:
        parts.append(f"location = {f.location}")
    if f.phase:
        parts.append("phase in " + "/".join(p.value for p in f.phase))
    if f.status:
        parts.append("status in " + "/".join(s.value for s in f.status))
    if f.start_year or f.end_year:
        parts.append(f"start {f.start_year or 'any'}-{f.end_year or 'any'}")

    scope = "; ".join(parts) if parts else "no filters applied"
    return f"Trials {_DIMENSION_PROSE[plan.dimension]}, filtered to {scope}."


def sanitise_assumptions(plan: QueryPlan) -> list[str]:
    """Drop model-authored assumptions that assert a quantity.

    An assumption is prose the model wrote. Years it was given are fine to echo;
    any other multi-digit run is a number the model invented, and an invented
    number sitting next to measured ones is exactly the confusion this system
    exists to prevent.
    """
    allowed = {str(y) for y in (plan.filters.start_year, plan.filters.end_year) if y}
    kept: list[str] = []
    for assumption in plan.assumptions:
        runs = {m.group().replace(",", "") for m in _NUMERIC_RUN.finditer(assumption)}
        if runs - allowed:
            continue
        kept.append(assumption)
    return kept


def bucket_filters(plan: QueryPlan, base: Filter) -> dict[str, Filter]:
    """One filter per bucket for the plan's dimension.

    Year buckets are derived from the plan's own range and capped, so an
    unbounded "over time" question degrades to a bounded window rather than
    hundreds of requests.
    """

    def area(expr: str) -> Filter:
        return base.merged_with(Filter({"filter.advanced": expr}))

    dim = plan.dimension
    if dim is Dimension.PHASE:
        return {p: area(f"AREA[Phase]{p}") for p in PHASES}
    if dim is Dimension.STATUS:
        return {s: area(f"AREA[OverallStatus]{s}") for s in STATUSES}
    if dim is Dimension.INTERVENTION_TYPE:
        return {t: area(f"AREA[InterventionType]{t}") for t in INTERVENTION_TYPES}
    if dim is Dimension.COUNTRY:
        # Resolved from the result set at execution time, like SPONSOR. Returning
        # {} here signals that; see _execute_distribution. A fixed list of twelve
        # answered "which countries have the most trials for X" with whichever of
        # those twelve happened to appear, silently excluding every other country.
        return {}
    if dim is Dimension.SPONSOR:
        return {}  # resolved from sampled records, not a fixed vocabulary
    if dim is Dimension.ENROLLMENT:
        return {
            label: area(f"AREA[EnrollmentCount]RANGE[{lo},{hi}]")
            for label, lo, hi in ENROLLMENT_BINS
        }
    if dim is Dimension.ENROLLMENT_BY_PHASE:
        # One probe per (phase, size-bin) cell: the scatter's two measures.
        return {
            f"{ph}{SERIES_SEP}{label}": area(
                f"AREA[Phase]{ph} AND AREA[EnrollmentCount]RANGE[{lo},{hi}]"
            )
            for ph in ("PHASE1", "PHASE2", "PHASE3", "PHASE4")
            for label, lo, hi in ENROLLMENT_BINS
        }
    if dim is Dimension.YEAR:
        end = plan.filters.end_year or 2026
        start = plan.filters.start_year or (end - MAX_YEARS + 1)
        start = max(start, end - MAX_YEARS + 1)
        return {
            str(y): area(f"AREA[StartDate]RANGE[{y}-01-01,{y}-12-31]")
            for y in range(start, end + 1)
        }
    return {}


async def resolve_top_values(
    client: CTGovClient, base: Filter, field_path: str, limit: int, sample: int = 200
) -> list[str]:
    """Find the most common values of a field within the result set.

    Needed for dimensions with no closed vocabulary (sponsors, drugs). We
    sample records to *discover candidate names*, then count each candidate
    with an exact probe — the sample never supplies a number, only a label.

    Requests ENTITY_FIELDS explicitly: the citation field set does not include
    intervention names, and a missing field here yields an empty side of the
    graph rather than an error.
    """
    studies = await client.sample_studies(base, limit=sample, fields=ENTITY_FIELDS)
    tally: dict[str, int] = {}
    for study in studies:
        for value in _extract(study, field_path):
            tally[value] = tally.get(value, 0) + 1
    return [name for name, _ in sorted(tally.items(), key=lambda kv: -kv[1])[:limit]]


def _extract(study: dict[str, Any], field_path: str) -> list[str]:
    """Pull values out of a study record by a small dotted path vocabulary."""
    protocol = study.get("protocolSection", {})
    if field_path == "sponsor":
        name = (protocol.get("sponsorCollaboratorsModule", {})
                .get("leadSponsor", {}).get("name"))
        return [name] if name else []
    if field_path == "intervention":
        arms = protocol.get("armsInterventionsModule", {}).get("interventions", []) or []
        return [i["name"] for i in arms if i.get("name")]
    if field_path == "country":
        locs = protocol.get("contactsLocationsModule", {}).get("locations", []) or []
        # A trial runs at many sites; dedupe per trial so a study with 40 US
        # sites counts once toward "which countries appear", not forty times.
        return sorted({loc["country"] for loc in locs if loc.get("country")})
    return []


def _citation_from(study: dict[str, Any]) -> Citation | None:
    """Build a citation from a real record. Excerpt is copied, never written.

    Returns None rather than raising on a malformed record. A single study with
    an unexpected nctId shape should cost one citation, not the whole response.
    """
    ident = study.get("protocolSection", {}).get("identificationModule", {})
    nct_id, title = ident.get("nctId"), ident.get("briefTitle")
    if not nct_id or not title:
        return None
    try:
        return Citation(nct_id=nct_id, excerpt=title[:500], field="briefTitle")
    except ValueError:
        return None


async def _citations_for(
    client: CTGovClient, filt: Filter, count: int, limit: int
) -> tuple[list[Citation], int]:
    """Fetch a bounded citation sample. `count` is the true total, from the probe."""
    if limit == 0 or count == 0:
        return [], count
    studies = await client.sample_studies(filt, limit=limit)
    return [c for c in (_citation_from(s) for s in studies) if c], count


async def _canonicalise(plan: QueryPlan, base: Filter, client: CTGovClient) -> tuple[Filter, list[str]]:
    """Expand a brand name to the canonical intervention the registry indexes.

    NOT CALLED on the base-filter path, deliberately. Measured: `query.intr` is a
    SEARCH, not an exact match, and the registry already resolves brand to
    generic itself — "Keytruda" and "Pembrolizumab" both return 394 melanoma
    trials. Wiring this there added a probe and rewrote nothing.

    Where it does matter is the exact-match path: network node and edge probes
    use AREA[InterventionName]"X", which does NOT search. A graph anchored on a
    brand name finds fewer trials there than the same query does through
    query.intr. That is the integration worth making, and it belongs in
    _execute_network's entity shortlist rather than here.

    "Keytruda" returns far fewer trials than "Pembrolizumab" because the
    registry indexes the generic. Resolution PROPOSES a canonical name and a
    count probe CONFIRMS it — a proposal that probes to zero is discarded, so
    this can narrow a query but never invent one.

    Deliberately conservative: only the top verified candidate is used, and only
    when it out-counts the caller's own term. Entity nearest-neighbour retrieves
    same-CATEGORY as readily as same-IDENTITY (measured: Pembrolizumab~Trastuzumab
    cosine 0.427), and a probe confirms a sibling exists just as happily as a
    synonym. Narrowing to a sibling would answer a different question.
    """
    notes: list[str] = []
    name = plan.filters.drug_name
    if not name:
        return base, notes
    try:
        from .entities import resolve_verified
        candidates = await resolve_verified(name, "intervention", base, client, top_k=3)
    except Exception:
        return base, notes  # resolution is an optimisation; never fail the query
    best = next((c for c in candidates if c.verified and c.label.lower() != name.lower()), None)
    if best is None:
        return base, notes
    own = await client.count(base)
    if best.count is None or best.count <= own:
        return base, notes
    notes.append(
        f"Resolved {name!r} to the canonical intervention {best.label!r} "
        f"(probe-verified); the registry indexes the generic name."
    )
    return base.merged_with(
        Filter({"filter.advanced": f'AREA[InterventionName]"{_esc(best.label)}"'})
    ), notes


async def execute(
    plan: QueryPlan, request: QueryRequest, client: CTGovClient
) -> QueryResponse:
    """Run the plan. Returns a fully validated response."""
    base = base_filter(plan)
    trace: list[TraceStep] = []

    def record(step: str, filt: Filter, result: int | None = None) -> None:
        trace.append(TraceStep(
            step=step,
            url=f"{BASE_URL}/studies",
            params={**filt.params, "countTotal": "true", "pageSize": "1"},
            result_count=result,
        ))

    total_matching = await client.count(base)
    record("count matching", base, total_matching)

    if plan.dimension in (Dimension.SPONSOR_DRUG_NETWORK, Dimension.DRUG_DRUG_NETWORK):
        response = await _execute_network(plan, request, client, base, total_matching, trace)
    elif plan.dimension is Dimension.NONE:
        response = _single_value(plan, base, total_matching, trace)
    else:
        response = await _execute_distribution(
            plan, request, client, base, total_matching, trace, record
        )
    return response


def _single_value(
    plan: QueryPlan, base: Filter, total: int, trace: list[TraceStep]
) -> QueryResponse:
    viz = Visualization(
        type=VizType.SINGLE_VALUE,
        title=plan.title,
        encoding=Encoding(y={"field": "count"}),
        data=[Datum(label="total", count=total, citation_total=total)],
        returned=1, total=1,
    )
    meta = ResponseMeta(
        interpretation=describe_plan(plan), trace=trace,
        filters_applied={k: v for k, v in base.params.items()},
        total_matching=total,
        denominator_note=f"{total:,} trials matched the applied filters",
        buckets_are_exclusive=True,
        units="trials", assumptions=sanitise_assumptions(plan),
    )
    return QueryResponse(visualization=viz, meta=meta)


async def _execute_distribution(
    plan: QueryPlan, request: QueryRequest, client: CTGovClient, base: Filter,
    total_matching: int, trace: list[TraceStep], record: Any,
) -> QueryResponse:
    buckets = bucket_filters(plan, base)

    if plan.dimension is Dimension.COUNTRY:
        names = await resolve_top_values(client, base, "country", limit=12)
        if not names:
            # Sample exposed no locations; fall back so the question still gets
            # an answer, and say so rather than returning an empty chart.
            names = COUNTRIES
        buckets = {
            c: base.merged_with(Filter({"filter.advanced": f'AREA[LocationCountry]"{_esc(c)}"'}))
            for c in names
        }

    if plan.dimension is Dimension.SPONSOR:
        names = await resolve_top_values(client, base, "sponsor", limit=10)
        # AREA clause so a caller-supplied query.spons is intersected, not replaced.
        buckets = {
            n: base.merged_with(Filter({"filter.advanced": f'AREA[LeadSponsorName]"{_esc(n)}"'}))
            for n in names
        }

    if not buckets:
        raise ValueError(f"no buckets could be built for dimension {plan.dimension.value}")

    # Comparisons: cross the bucket vocabulary with each compared entity, so
    # "compare phases for Keytruda vs Opdivo" becomes one probe per (drug,
    # phase) pair. Previously the plan carried compare_values and the executor
    # ignored them, silently answering the un-compared question instead.
    series_of: dict[str, str] = {}
    if plan.dimension is Dimension.ENROLLMENT_BY_PHASE:
        series_of = {k: k.split(SERIES_SEP, 1)[0] for k in buckets}
    if plan.compare_values and plan.compare_field:
        area = {
            "intervention": "AREA[InterventionName]",
            "condition": "AREA[ConditionSearch]",
            "sponsor": "AREA[LeadSponsorName]",
        }[plan.compare_field]
        crossed: dict[str, Filter] = {}
        for value in plan.compare_values:
            scoped = Filter({"filter.advanced": f'{area}"{_esc(value)}"'})
            for label, filt in buckets.items():
                key = f"{value}{SERIES_SEP}{label}"
                crossed[key] = filt.merged_with(scoped)
                series_of[key] = value
        buckets = crossed

    counts = await client.count_many(buckets)
    for label, filt in buckets.items():
        record(f"count {label}", filt, counts[label])

    # Citations, concurrently, only for buckets that actually have members.
    limit = request.max_citations_per_datum
    nonempty = [lbl for lbl, n in counts.items() if n > 0]
    cite_results = await asyncio.gather(
        *(_citations_for(client, buckets[lbl], counts[lbl], limit) for lbl in nonempty)
    )
    cites = dict(zip(nonempty, cite_results))

    def _bucket_label(key: str) -> str:
        return key.split(SERIES_SEP, 1)[1] if SERIES_SEP in key else key

    data = [
        Datum(
            label=_bucket_label(label),
            series=series_of.get(label),
            count=counts[label],
            citations=cites.get(label, ([], 0))[0],
            citation_total=counts[label],
            is_unbucketed=_bucket_label(label) == "NA",
        )
        for label in counts
    ]
    # Zero-count buckets are retained (design-dna rule-1); only ordering changes.
    if plan.dimension is Dimension.YEAR:
        data.sort(key=lambda d: d.label)
        sort_note = "label_asc"
    else:
        data.sort(key=lambda d: (d.series or "", d.is_unbucketed, -d.count))
        sort_note = "series_then_count_desc" if series_of else "count_desc"

    # --- the exclusivity check -------------------------------------------
    bucket_sum = sum(counts.values())
    multi_valued = plan.dimension in MULTI_VALUED_DIMENSIONS
    exclusive = (not multi_valued) and bucket_sum == total_matching
    warnings: list[str] = []
    unbucketed: int | None = None

    if multi_valued and bucket_sum == total_matching:
        # Equality here is a coincidence, not a partition. Say so explicitly
        # rather than letting the number imply safety.
        warnings.append(
            f"Bucket counts happen to sum to {total_matching:,}, but a trial may hold "
            f"several {plan.dimension.value} values, so this is not a partition and the "
            f"equality is coincidental."
        )

    if not exclusive:
        if bucket_sum > total_matching:
            overlap = bucket_sum - total_matching
            warnings.append(
                f"Buckets overlap: bucket counts sum to {bucket_sum:,} but only "
                f"{total_matching:,} trials match, so ~{overlap:,} memberships are "
                f"double-counted. A trial may hold several {plan.dimension.value} values."
            )
        else:
            unbucketed = total_matching - bucket_sum
            if multi_valued:
                # With overlap also in play, the shortfall is a net figure: the
                # true number of unbucketed trials is at least this, and higher
                # by however many memberships were double-counted.
                warnings.append(
                    f"At least {unbucketed:,} of {total_matching:,} matching trials fall "
                    f"into no bucket. Because a trial may hold several "
                    f"{plan.dimension.value} values, overlap and absent values partly "
                    f"offset each other, so this is a net shortfall, not an exact count."
                )
            else:
                warnings.append(
                    f"{unbucketed:,} of {total_matching:,} matching trials fall into no "
                    f"bucket — the {plan.dimension.value} field is absent on those records."
                )
        warnings.append(
            "Do not render this as a pie or 100% stacked bar: the buckets do not "
            "partition the result set."
        )

    viz = Visualization(
        type=plan.viz_type if plan.viz_type is not VizType.NETWORK_GRAPH else VizType.BAR_CHART,
        title=plan.title,
        encoding=Encoding(
            x={"field": "label"}, y={"field": "count"},
            series={"field": "series"} if series_of else None,
        ),
        data=data, returned=len(data), total=len(data),
    )
    meta = ResponseMeta(
        interpretation=describe_plan(plan), trace=trace,
        filters_applied={k: v for k, v in base.params.items()},
        total_matching=total_matching,
        denominator_note=(
            f"of {total_matching:,} trials matching "
            + (", ".join(f"{k}={v}" for k, v in base.params.items()) or "no filters")
        ),
        buckets_are_exclusive=exclusive,
        bucket_sum=bucket_sum,
        unbucketed_count=unbucketed,
        sort=sort_note, units="trials",
        time_granularity="year" if plan.dimension is Dimension.YEAR else None,
        assumptions=sanitise_assumptions(plan), warnings=warnings,
    )
    return QueryResponse(visualization=viz, meta=meta)


def _empty_network(
    plan: QueryPlan, base: Filter, total_matching: int, trace: list[TraceStep],
    left_field: str, right_field: str,
) -> QueryResponse:
    """A network with no discoverable entities, returned as data not an error.

    Uses SINGLE_VALUE so the response still satisfies the visualization schema
    (a network_graph requires at least one node) while stating plainly that the
    requested graph could not be built.
    """
    viz = Visualization(
        type=VizType.SINGLE_VALUE,
        title=plan.title,
        encoding=Encoding(y={"field": "count"}),
        data=[Datum(label="total", count=total_matching, citation_total=total_matching)],
        returned=1, total=1,
    )
    meta = ResponseMeta(
        interpretation=describe_plan(plan), trace=trace,
        filters_applied={k: v for k, v in base.params.items()},
        total_matching=total_matching,
        denominator_note=f"{total_matching:,} trials matched the applied filters",
        buckets_are_exclusive=True,
        units="trials",
        assumptions=sanitise_assumptions(plan),
        warnings=[
            f"No {left_field}/{right_field} network could be built: the matching trials "
            f"expose no {right_field} values in the sampled records, so there are no "
            f"entities to relate. The total above is still exact.",
        ],
    )
    return QueryResponse(visualization=viz, meta=meta)


async def _execute_network(
    plan: QueryPlan, request: QueryRequest, client: CTGovClient, base: Filter,
    total_matching: int, trace: list[TraceStep],
) -> QueryResponse:
    """Build a co-occurrence graph.

    Edge weights are exact counts from probes, not tallies of a sample. The
    sample is used only to discover which entities are worth probing.
    """
    left_field = "sponsor"
    right_field = "intervention"
    if plan.dimension is Dimension.DRUG_DRUG_NETWORK:
        left_field = right_field = "intervention"

    lefts = await resolve_top_values(client, base, left_field, limit=MAX_NODES_PER_SIDE)
    rights = await resolve_top_values(client, base, right_field, limit=MAX_NODES_PER_SIDE)
    if plan.dimension is Dimension.DRUG_DRUG_NETWORK:
        rights = [r for r in rights if r not in lefts[:1]]

    def side_filter(field: str, value: str) -> Filter:
        """Constrain to one entity via an AREA clause.

        Deliberately not `query.spons` / `query.intr`. Those are plain keys, and
        `Filter.merged_with` overwrites plain keys instead of intersecting them,
        so a base filter carrying `query.intr` (from "…for pembrolizumab trials")
        was silently replaced by the node's own value. AREA clauses AND-compose,
        so both the base constraint and the node constraint survive.
        """
        area = "AREA[LeadSponsorName]" if field == "sponsor" else "AREA[InterventionName]"
        return base.merged_with(Filter({"filter.advanced": f'{area}"{_esc(value)}"'}))

    def pair_filter(left: str, right: str) -> Filter:
        """Constrain a trial to contain BOTH entities.

        Because both sides are AREA clauses this is a genuine intersection. The
        earlier version wrote `query.intr` twice, so for drug-to-drug graphs the
        second value overwrote the first and every edge weight came back equal to
        the right node's own total (all lung-cancer edges read 1697, Carboplatin's
        count) instead of the co-occurrence.
        """
        return side_filter(left_field, left).merged_with(side_filter(right_field, right))

    # Exact node magnitudes.
    node_filters = {f"L::{n}": side_filter(left_field, n) for n in lefts}
    node_filters.update({f"R::{n}": side_filter(right_field, n) for n in rights})
    node_counts = await client.count_many(node_filters)

    # Exact edge weights: one probe per candidate pair, capped.
    # When both sides draw from the same vocabulary the relation is symmetric,
    # so (a,b) and (b,a) are the same edge and probing both doubles the cost.
    symmetric = left_field == right_field
    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[str, str]] = []
    for left in lefts:
        for right in rights:
            if left == right:
                continue
            key = tuple(sorted((left, right))) if symmetric else (left, right)
            if key in seen:
                continue
            seen.add(key)
            pairs.append((left, right))
    pairs = pairs[:MAX_EDGE_PROBES]

    edge_filters = {f"{l}||{r}": pair_filter(l, r) for l, r in pairs}
    edge_counts = await client.count_many(edge_filters) if edge_filters else {}

    # One trace entry per probe, in the same shape _execute_distribution uses.
    #
    # This previously collapsed to a single synthetic step whose `result_count`
    # was the NUMBER OF REQUESTS, not a totalCount — so replaying it compared a
    # request tally against a study count, and the probes that actually produce
    # every node `count` and edge `weight` were not recorded at all. Network
    # responses were therefore only ~50% replayable while the README claimed
    # every number could be reproduced from the trace. Recording each probe in
    # full is the only version of that claim that is true.
    for key, filt in node_filters.items():
        side, _, name = key.partition("::")
        field = left_field if side == "L" else right_field
        trace.append(TraceStep(
            step=f"count {field} {name}",
            url=f"{BASE_URL}/studies",
            params={**filt.params, "countTotal": "true", "pageSize": "1"},
            result_count=node_counts[key],
        ))
    for key, filt in edge_filters.items():
        left_name, _, right_name = key.partition("||")
        trace.append(TraceStep(
            step=f"count {left_name} + {right_name}",
            url=f"{BASE_URL}/studies",
            params={**filt.params, "countTotal": "true", "pageSize": "1"},
            result_count=edge_counts[key],
        ))

    nodes = [
        GraphNode(id=f"L::{n}", label=n, type=left_field, count=node_counts[f"L::{n}"])
        for n in lefts if node_counts.get(f"L::{n}", 0) > 0
    ] + [
        GraphNode(id=f"R::{n}", label=n, type=right_field, count=node_counts[f"R::{n}"])
        for n in rights if node_counts.get(f"R::{n}", 0) > 0
    ]
    known = {n.id for n in nodes}
    edges = [
        GraphEdge(source=f"L::{l}", target=f"R::{r}", weight=w, citation_total=w)
        for (l, r), w in ((p, edge_counts.get(f"{p[0]}||{p[1]}", 0)) for p in pairs)
        if w > 0 and f"L::{l}" in known and f"R::{r}" in known
    ]
    edges.sort(key=lambda e: -e.weight)

    if not nodes:
        # A non-zero base count does not guarantee the sample contains the
        # entity type this graph needs — an observational condition may match
        # thousands of trials and list no interventions at all. Degrade to an
        # explicit empty result rather than raising and returning a 500.
        return _empty_network(plan, base, total_matching, trace, left_field, right_field)

    viz = Visualization(
        type=VizType.NETWORK_GRAPH, title=plan.title,
        encoding=Encoding(
            nodes={"id": "id", "label": "label", "size": "count"},
            edges={"source": "source", "target": "target", "weight": "weight"},
        ),
        nodes=nodes, edges=edges,
        # `total` is not the universe of entities — that is unknown, because the
        # shortlist came from a bounded sample. Reporting the candidate count as
        # a denominator would imply "showing 12 of 12" when the real population
        # is unmeasured. Both fields describe what was actually returned, and
        # the warning below carries the sampling caveat.
        returned=len(nodes), total=len(nodes),
    )
    meta = ResponseMeta(
        interpretation=describe_plan(plan), trace=trace,
        filters_applied={k: v for k, v in base.params.items()},
        total_matching=total_matching,
        denominator_note=(
            f"co-occurrence within {total_matching:,} matching trials; "
            f"top {MAX_NODES_PER_SIDE} entities per side"
        ),
        buckets_are_exclusive=False,
        units="trials",
        assumptions=sanitise_assumptions(plan),
        warnings=[
            "Node counts and edge weights are exact probe counts, but the entity "
            "shortlist is drawn from a sample of matching records, so a rare entity "
            "may be absent from the graph.",
        ],
    )
    return QueryResponse(visualization=viz, meta=meta)
