"""Invariant tests, written against the failures this system actually had.

Every test here is a regression for a bug that shipped into a running build
during development, or for an invariant whose violation would silently produce
wrong numbers rather than an error. They use a fake client so they are fast and
deterministic; live behaviour is covered separately in test_live.py.

The three real bugs, and where they are pinned:
  1. network graphs returned zero edges          -> test_entity_fields_*
  2. drug-drug edge weights equalled node totals -> test_pair_filter_*
  3. retry storm on concurrent 429s              -> test_throttle_*
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from app.ctgov import (
    CITATION_FIELDS, ENTITY_FIELDS, CTGovClient, CTGovError, Filter,
    MAX_PROBES_PER_REQUEST, _BoundedCache,
)
from app.executor import base_filter, bucket_filters, execute
from app.models import Citation, Datum, Encoding, QueryRequest, Visualization, VizType
from app.planner import Dimension, PlanFilters, QueryPlan, fallback_plan


# ---------------------------------------------------------------- fake client


class FakeCTGov(CTGovClient):
    """Replays scripted counts. Records every request for assertion."""

    def __init__(self, counts: dict[str, int], studies: list[dict[str, Any]] | None = None):
        super().__init__()
        self._counts = counts
        self._studies = studies or []
        self.count_calls: list[dict[str, str]] = []
        self.sample_calls: list[tuple[dict[str, str], str]] = []

    async def count(self, filt: Filter) -> int:
        self.count_calls.append(dict(filt.params))
        if "filter.advanced" not in filt.params:
            return self._counts.get("__base__", 0)
        # An unscripted bucket is an empty bucket. Falling back to the base
        # count here would hand every bucket the same non-zero value and make
        # the zero-retention and exclusivity tests pass for the wrong reason.
        return self._counts.get(filt.params["filter.advanced"], 0)

    async def sample_studies(self, filt: Filter, limit: int = 3, fields: str = CITATION_FIELDS):
        self.sample_calls.append((dict(filt.params), fields))
        return self._studies[:limit]


def _study(nct: str, title: str, sponsor: str = "", interventions: tuple[str, ...] = ()):
    protocol: dict[str, Any] = {
        "identificationModule": {"nctId": nct, "briefTitle": title},
    }
    if sponsor:
        protocol["sponsorCollaboratorsModule"] = {"leadSponsor": {"name": sponsor}}
    if interventions:
        protocol["armsInterventionsModule"] = {
            "interventions": [{"name": n} for n in interventions]
        }
    return {"protocolSection": protocol}


def _plan(dimension: Dimension, viz: VizType, **filters) -> QueryPlan:
    return QueryPlan(
        dimension=dimension, viz_type=viz, title="t", interpretation="i",
        filters=PlanFilters(**filters),
    )


# ------------------------------------------------------- bug 1: entity fields


async def test_entity_discovery_requests_intervention_fields():
    """Network graphs were empty because entity discovery reused the citation
    field list, which omits InterventionName. Discovery must ask for its own
    fields or one whole side of every graph silently disappears."""
    client = FakeCTGov({"__base__": 10}, [_study("NCT00000001", "T", "Merck", ("Pembrolizumab",))])
    plan = _plan(Dimension.SPONSOR_DRUG_NETWORK, VizType.NETWORK_GRAPH, condition="melanoma")

    await execute(plan, QueryRequest(query="network of sponsors and drugs"), client)

    discovery_fields = {fields for _, fields in client.sample_calls}
    assert ENTITY_FIELDS in discovery_fields, (
        "entity discovery must request ENTITY_FIELDS; using CITATION_FIELDS "
        "yields no interventions and therefore no edges"
    )
    assert "InterventionName" in ENTITY_FIELDS


async def test_network_graph_has_edges_when_both_sides_present():
    client = FakeCTGov(
        {
            "__base__": 50,
            # Node probes: one AREA clause each.
            'AREA[LeadSponsorName]"Merck"': 40,
            'AREA[InterventionName]"Pembrolizumab"': 35,
            'AREA[InterventionName]"Ipilimumab"': 20,
            # Edge probes: both clauses AND-composed.
            'AREA[LeadSponsorName]"Merck" AND AREA[InterventionName]"Pembrolizumab"': 31,
            'AREA[LeadSponsorName]"Merck" AND AREA[InterventionName]"Ipilimumab"': 12,
        },
        [_study("NCT00000001", "T", "Merck", ("Pembrolizumab", "Ipilimumab"))],
    )
    plan = _plan(Dimension.SPONSOR_DRUG_NETWORK, VizType.NETWORK_GRAPH, condition="melanoma")
    response = await execute(plan, QueryRequest(query="network of sponsors"), client)

    node_types = {n.type for n in response.visualization.nodes}
    assert node_types == {"sponsor", "intervention"}, (
        f"graph must have both sides, got {node_types}"
    )
    edges = response.visualization.edges
    assert edges, "a two-sided graph with non-zero pair counts must have edges"

    # The weight must be the pair probe, never the target node's own total.
    # Equality with the node count is exactly the bug that shipped.
    weights = {e.weight for e in edges}
    assert weights <= {31, 12}, f"edge weights must come from pair probes, got {weights}"
    assert 50 not in weights, "edge weight equals the node total -- the pair filter collapsed"


# ------------------------------------------- trace completeness (networks)


async def test_every_network_number_is_replayable_from_the_trace():
    """The README promises every number can be reproduced from meta.trace.

    Networks used to emit ONE synthetic step whose `result_count` was the number
    of REQUESTS issued, not a totalCount — so replaying it compared a request
    tally against a study count, and the probes producing every node count and
    edge weight were never recorded at all. Roughly half of a network response
    was unreplayable while the docs claimed otherwise.
    """
    client = FakeCTGov(
        {
            "__base__": 50,
            'AREA[LeadSponsorName]"Merck"': 40,
            'AREA[InterventionName]"Pembrolizumab"': 35,
            'AREA[InterventionName]"Ipilimumab"': 20,
            'AREA[LeadSponsorName]"Merck" AND AREA[InterventionName]"Pembrolizumab"': 31,
            'AREA[LeadSponsorName]"Merck" AND AREA[InterventionName]"Ipilimumab"': 12,
        },
        [_study("NCT00000001", "T", "Merck", ("Pembrolizumab", "Ipilimumab"))],
    )
    plan = _plan(Dimension.SPONSOR_DRUG_NETWORK, VizType.NETWORK_GRAPH, condition="melanoma")
    response = await execute(plan, QueryRequest(query="network of sponsors"), client)
    viz, meta = response.visualization, response.meta

    traced = {s.result_count for s in meta.trace if s.result_count is not None}
    for node in viz.nodes:
        assert node.count in traced, f"node {node.label} count {node.count} absent from trace"
    for edge in viz.edges:
        assert edge.weight in traced, f"edge {edge.source}->{edge.target} weight absent from trace"

    # No step may report more than the matching population. That is the
    # signature of a request tally masquerading as a study count.
    for step in meta.trace:
        if step.result_count is not None:
            assert step.result_count <= meta.total_matching, (
                f"step {step.step!r} reports {step.result_count} > total_matching "
                f"{meta.total_matching}: looks like a request count, not a totalCount"
            )

    # Each step must carry the literal params it issued, or it cannot be replayed.
    for step in meta.trace:
        assert step.params.get("countTotal") == "true"
        assert step.params.get("pageSize") == "1"

    # 1 base probe + 3 node probes + 2 edge probes, one step each.
    assert len(meta.trace) == 6, f"expected one trace step per probe, got {len(meta.trace)}"


# --------------------------------------------------------- bug 2: pair filter


def test_filter_merge_overwrites_non_advanced_keys():
    """Pins the surprising behaviour that caused the edge-weight bug, so a
    future change to merged_with cannot quietly alter it."""
    merged = Filter({"query.intr": "A"}).merged_with(Filter({"query.intr": "B"}))
    assert merged.params["query.intr"] == "B", "same-key merge overwrites, it does not intersect"

    anded = (Filter({"filter.advanced": "X"}).merged_with(Filter({"filter.advanced": "Y"})))
    assert anded.params["filter.advanced"] == "X AND Y", "advanced clauses AND-compose"


async def test_drug_drug_edges_intersect_rather_than_overwrite():
    """Every lung-cancer edge came back as 1697 -- Carboplatin's own total --
    because both sides wrote query.intr and the second overwrote the first.
    The pair filter must produce an intersection."""
    client = FakeCTGov(
        {"__base__": 100},
        [_study("NCT00000001", "T", "S", ("Cisplatin", "Carboplatin", "Pemetrexed"))],
    )
    plan = _plan(Dimension.DRUG_DRUG_NETWORK, VizType.NETWORK_GRAPH, condition="lung cancer")
    await execute(plan, QueryRequest(query="which drugs co-occur"), client)

    # Both entities must appear, AND-composed, in one advanced clause. A probe
    # naming only one drug is the collapsed filter that produced edge weights
    # equal to a node's own total.
    pair_calls = [
        params["filter.advanced"] for params in client.count_calls
        if params.get("filter.advanced", "").count("AREA[InterventionName]") == 2
    ]
    assert pair_calls, "edge probes must name BOTH interventions"
    for clause in pair_calls:
        assert " AND " in clause, f"pair probe must intersect, got {clause}"
        assert clause.count('"') == 4, f"expected two quoted entities, got {clause}"

    # And the base constraint must survive alongside them.
    for params in client.count_calls:
        if params.get("filter.advanced", "").count("AREA[InterventionName]") == 2:
            assert params.get("query.cond") == "lung cancer", (
                "base condition filter was dropped by the pair merge"
            )


# ------------------------------------------------- bug 3: throttle behaviour


async def test_throttle_cooldown_delays_all_pending_requests():
    """A 429 is information about a shared budget. Reacting per-request lets
    the other in-flight calls keep pushing, which is how the retry storm
    formed. The cooldown must move the shared schedule."""
    client = CTGovClient()
    loop = asyncio.get_running_loop()
    before = client._next_slot
    await client._apply_cooldown(2.0)
    assert client._next_slot >= loop.time() + 1.9
    assert client._next_slot > before


async def test_pacing_serialises_request_starts():
    client = CTGovClient()
    start = asyncio.get_running_loop().time()
    await asyncio.gather(*(client._pace() for _ in range(5)))
    elapsed = asyncio.get_running_loop().time() - start
    assert elapsed > 0, "five paced starts must not all fire in the same instant"


# ------------------------------------------------- the core anti-hallucination


async def test_counts_come_from_probes_not_samples():
    """The sample is allowed to supply labels, never quantities. Here the
    sample has one study but the probe says 742; the datum must say 742."""
    client = FakeCTGov(
        {"AREA[Phase]PHASE2": 742, "__base__": 900},
        [_study("NCT00000001", "Only one sampled study")],
    )
    plan = _plan(Dimension.PHASE, VizType.BAR_CHART, condition="melanoma")
    response = await execute(plan, QueryRequest(query="phases"), client)

    phase2 = next(d for d in response.visualization.data if d.label == "PHASE2")
    assert phase2.count == 742, "count must be the probe result, not len(sample)"
    assert phase2.citation_total == 742
    assert len(phase2.citations) <= 3, "citations are a bounded sample of that total"


def test_citation_total_cannot_understate_returned_citations():
    with pytest.raises(ValueError):
        Datum(
            label="x", count=5, citation_total=1,
            citations=[
                Citation(nct_id="NCT00000001", excerpt="a", field="briefTitle"),
                Citation(nct_id="NCT00000002", excerpt="b", field="briefTitle"),
            ],
        )


# --------------------------------------------------- exclusivity, both signs


async def test_overlapping_buckets_are_reported_not_exclusive():
    """Pembrolizumab shape: buckets sum ABOVE the total (double counting)."""
    counts = {"__base__": 2915, "AREA[Phase]PHASE1": 1036, "AREA[Phase]PHASE2": 1742,
              "AREA[Phase]PHASE3": 360, "AREA[Phase]PHASE4": 16,
              "AREA[Phase]EARLY_PHASE1": 51, "AREA[Phase]NA": 0}
    client = FakeCTGov(counts)
    plan = _plan(Dimension.PHASE, VizType.BAR_CHART, drug_name="Pembrolizumab")
    meta = (await execute(plan, QueryRequest(query="phases", max_citations_per_datum=0), client)).meta

    assert meta.buckets_are_exclusive is False
    assert meta.bucket_sum > meta.total_matching
    assert any("double-counted" in w or "overlap" in w.lower() for w in meta.warnings)
    assert any("pie" in w for w in meta.warnings), "renderer must be told not to draw a pie"


async def test_undercounting_buckets_are_also_reported_not_exclusive():
    """Melanoma shape: buckets sum BELOW the total (absent field). The
    direction of the discrepancy is not predictable, so a fixed caveat would
    be wrong half the time."""
    counts = {"__base__": 3738, "AREA[Phase]PHASE1": 1340, "AREA[Phase]PHASE2": 1529,
              "AREA[Phase]PHASE3": 219, "AREA[Phase]PHASE4": 37,
              "AREA[Phase]EARLY_PHASE1": 65, "AREA[Phase]NA": 412}
    client = FakeCTGov(counts)
    plan = _plan(Dimension.PHASE, VizType.BAR_CHART, condition="melanoma")
    meta = (await execute(plan, QueryRequest(query="phases", max_citations_per_datum=0), client)).meta

    assert meta.buckets_are_exclusive is False
    assert meta.bucket_sum < meta.total_matching
    assert meta.unbucketed_count == 3738 - meta.bucket_sum
    assert any("no bucket" in w for w in meta.warnings)


async def test_exact_partition_is_reported_exclusive():
    """Only a single-valued dimension may ever be called exclusive. Status is
    one value per trial, so an exact sum really is a partition."""
    counts = {"__base__": 100, "AREA[OverallStatus]RECRUITING": 60,
              "AREA[OverallStatus]COMPLETED": 40}
    client = FakeCTGov(counts)
    plan = _plan(Dimension.STATUS, VizType.BAR_CHART, condition="x")
    meta = (await execute(plan, QueryRequest(query="by status", max_citations_per_datum=0), client)).meta

    assert meta.buckets_are_exclusive is True
    assert meta.warnings == []


async def test_multi_valued_dimension_never_claims_exclusivity():
    """Overlap inflates the sum and absent values deflate it; the two can cancel
    exactly. Deciding exclusivity by arithmetic equality would then license a
    pie chart of overlapping data, so a multi-valued dimension is never
    exclusive regardless of what the counts happen to be."""
    counts = {"__base__": 100, "AREA[Phase]PHASE1": 60, "AREA[Phase]PHASE2": 40,
              "AREA[Phase]PHASE3": 0, "AREA[Phase]PHASE4": 0,
              "AREA[Phase]EARLY_PHASE1": 0, "AREA[Phase]NA": 0}
    client = FakeCTGov(counts)
    plan = _plan(Dimension.PHASE, VizType.BAR_CHART, condition="x")
    meta = (await execute(plan, QueryRequest(query="phases", max_citations_per_datum=0), client)).meta

    assert meta.bucket_sum == meta.total_matching, "sums match exactly here"
    assert meta.buckets_are_exclusive is False, "but phase can never be a partition"
    assert any("coincidental" in w for w in meta.warnings)


async def test_zero_count_buckets_are_retained():
    """Dropping empty buckets makes a distribution look denser than it is."""
    counts = {"__base__": 100, "AREA[Phase]PHASE1": 100}
    client = FakeCTGov(counts)
    plan = _plan(Dimension.PHASE, VizType.BAR_CHART, condition="x")
    data = (await execute(plan, QueryRequest(query="phases", max_citations_per_datum=0), client)).visualization.data
    assert len(data) == 6, "all six phase buckets must appear, including zeros"
    assert any(d.count == 0 for d in data)


async def test_unbucketed_datum_is_flagged():
    counts = {"__base__": 100, "AREA[Phase]NA": 30, "AREA[Phase]PHASE1": 70}
    client = FakeCTGov(counts)
    plan = _plan(Dimension.PHASE, VizType.BAR_CHART, condition="x")
    data = (await execute(plan, QueryRequest(query="phases", max_citations_per_datum=0), client)).visualization.data
    na = next(d for d in data if d.label == "NA")
    assert na.is_unbucketed is True
    assert all(d.is_unbucketed is False for d in data if d.label != "NA")
    assert data[-1].label == "NA", "the unbucketed remainder sorts last"


# --------------------------------------------------- cache, bounded & correct


async def test_repeated_probe_is_served_from_cache_without_a_second_request():
    """Stubs the HTTP seam so the real count() and its cache are exercised."""
    client = CTGovClient()
    calls: list[dict[str, str]] = []

    async def fake_get(path: str, params: dict[str, str]):
        calls.append(params)
        return {"totalCount": 3738}

    client._get = fake_get  # type: ignore[method-assign]
    filt = Filter({"query.cond": "melanoma"})

    first = await client.count(filt)
    second = await client.count(filt)

    assert first == second == 3738
    assert len(calls) == 1, f"second probe must hit the cache, made {len(calls)} requests"
    assert client.cache_stats["hits"] == 1


def test_cache_key_is_order_independent():
    """Two logically identical filters built in different orders must share a
    key, or the cache silently never hits."""
    a = _BoundedCache.key({"query.cond": "melanoma", "countTotal": "true"})
    b = _BoundedCache.key({"countTotal": "true", "query.cond": "melanoma"})
    assert a == b


def test_cached_count_expires_and_an_expired_entry_is_a_miss():
    """L1 must expire on its own, not rely on the process dying.

    Process lifetime used to BE the staleness bound, which made a missing TTL
    safe by accident. Warming this cache from a shared persistent layer ends
    that accident: a long-lived worker would serve counts older than the shared
    TTL out of memory, enforcing expiry on the slow path and bypassing it on the
    fast one. A stale count presented as measured is the failure this whole
    system exists to prevent.
    """
    cache = _BoundedCache(ttl_seconds=0.05)
    cache.put({"q": "melanoma"}, 3738)
    assert cache.get({"q": "melanoma"}) == 3738

    time.sleep(0.06)
    assert cache.get({"q": "melanoma"}) is None, "an expired entry must be a MISS"
    assert cache.expired == 1, "expiry is counted separately from an ordinary miss"
    # and it must be gone, not merely reported missing
    assert len(cache) == 0


def test_expiry_uses_a_monotonic_clock():
    """A system clock change must not resurrect an expired entry."""
    import inspect
    source = inspect.getsource(_BoundedCache)
    assert "time.monotonic()" in source
    assert "time.time()" not in source, "wall-clock expiry is settable by the host"


def test_cache_is_bounded_and_evicts():
    """An unbounded dict keyed on user-controlled filters is a memory leak an
    agent loop would find within minutes."""
    cache = _BoundedCache(max_entries=100)
    for i in range(500):
        cache.put({"q": str(i)}, i)
    assert len(cache) <= 100, f"cache grew to {len(cache)} past its cap"
    assert cache.get({"q": "499"}) == 499, "the most recent entry must survive"


# ---------------------------------------------- histogram / scatter routing


def test_enrollment_questions_route_to_histogram():
    plan = fallback_plan(QueryRequest(query="How large are melanoma trials by enrollment?"))
    assert plan.dimension is Dimension.ENROLLMENT
    assert plan.viz_type is VizType.HISTOGRAM


def test_enrollment_versus_phase_routes_to_scatter():
    plan = fallback_plan(
        QueryRequest(query="Does enrollment size vary by phase for glioblastoma?")
    )
    assert plan.dimension is Dimension.ENROLLMENT_BY_PHASE
    assert plan.viz_type is VizType.SCATTER_PLOT


def test_enrollment_dimensions_reject_mismatched_viz():
    with pytest.raises(ValueError, match="requires histogram"):
        QueryPlan(dimension=Dimension.ENROLLMENT, viz_type=VizType.BAR_CHART,
                  title="t", interpretation="i")
    with pytest.raises(ValueError, match="requires scatter_plot"):
        QueryPlan(dimension=Dimension.ENROLLMENT_BY_PHASE, viz_type=VizType.HISTOGRAM,
                  title="t", interpretation="i")


def test_enrollment_bins_are_contiguous_and_probe_ranges():
    """Overlapping or gapped bins would double-count or lose trials."""
    from app.executor import ENROLLMENT_BINS
    for (_, _, hi), (_, lo_next, _) in zip(ENROLLMENT_BINS, ENROLLMENT_BINS[1:]):
        assert lo_next == hi + 1, f"bin gap/overlap at {hi} -> {lo_next}"


def test_country_buckets_are_discovered_not_hardcoded():
    """A fixed list of twelve answered 'which countries have the most trials'
    with whichever of those twelve appeared, silently excluding the rest."""
    plan = _plan(Dimension.COUNTRY, VizType.BAR_CHART, condition="melanoma")
    assert bucket_filters(plan, Filter({})) == {}, (
        "COUNTRY must resolve from the result set at execution time, like SPONSOR"
    )


# ------------------------------------------------------------ bounds & safety


async def test_probe_count_is_capped_rather_than_silently_truncated():
    client = FakeCTGov({"__base__": 1})
    too_many = {str(i): Filter({"filter.advanced": str(i)}) for i in range(MAX_PROBES_PER_REQUEST + 1)}
    real = CTGovClient()
    with pytest.raises(CTGovError, match="exceeding"):
        await real.count_many(too_many)


def test_year_buckets_are_bounded():
    plan = _plan(Dimension.YEAR, VizType.TIME_SERIES, condition="x", start_year=1900, end_year=2026)
    buckets = bucket_filters(plan, Filter({}))
    assert len(buckets) <= 15, "an unbounded year range must degrade to a window, not 126 probes"


def test_advanced_filter_clauses_all_survive_composition():
    """Phase, status and date each add an advanced clause; none may clobber
    another, or a query silently ignores part of what the user asked for."""
    from app.models import TrialPhase, TrialStatus
    plan = _plan(
        Dimension.PHASE, VizType.BAR_CHART, drug_name="X",
        phase=[TrialPhase.PHASE3], status=[TrialStatus.RECRUITING],
        start_year=2020, end_year=2024,
    )
    advanced = base_filter(plan).params["filter.advanced"]
    assert "AREA[Phase]PHASE3" in advanced
    assert "AREA[OverallStatus]RECRUITING" in advanced
    assert "AREA[StartDate]RANGE[2020-01-01,2024-12-31]" in advanced
    assert advanced.count(" AND ") == 2


# ------------------------------------------------------------------- planner


def test_plan_rejects_network_dimension_with_wrong_viz():
    with pytest.raises(ValueError, match="network_graph"):
        QueryPlan(dimension=Dimension.SPONSOR_DRUG_NETWORK, viz_type=VizType.BAR_CHART,
                  title="t", interpretation="i")


def test_plan_rejects_network_viz_without_network_dimension():
    with pytest.raises(ValueError, match="network"):
        QueryPlan(dimension=Dimension.PHASE, viz_type=VizType.NETWORK_GRAPH,
                  title="t", interpretation="i")


def test_plan_rejects_single_value_with_a_bucketed_dimension():
    """`single_value` renders ONE number; every other dimension emits buckets.

    This pair was unguarded, so a plan with dimension=sponsor and
    viz_type=single_value validated cleanly and answered "how many trials does
    Novartis sponsor?" with an eleven-row per-sponsor breakdown labelled as a
    single total. Second validator gap of this shape found here: an invariant
    enforced on one combination but not the parallel one.
    """
    with pytest.raises(ValueError, match="single_value requires dimension none"):
        QueryPlan(dimension=Dimension.SPONSOR, viz_type=VizType.SINGLE_VALUE,
                  title="t", interpretation="i")


def test_plan_rejects_dimension_none_with_a_bucketed_viz():
    with pytest.raises(ValueError, match="requires viz_type single_value"):
        QueryPlan(dimension=Dimension.NONE, viz_type=VizType.BAR_CHART,
                  title="t", interpretation="i")


def test_counting_questions_route_to_a_single_total():
    """A counting question wants one number, even when it happens to contain a
    grouping word.

    "How many trials does Novartis sponsor?" contains "sponsor" and was routed
    to a per-sponsor breakdown — answering a question the user did not ask, then
    labelling it a total. Counting lead-ins must be checked before grouping
    keywords.
    """
    for text in ("How many trials does Novartis sponsor?",
                 "Count the glioblastoma trials that started in 2020 or later.",
                 "What is the total number of melanoma trials?"):
        plan = fallback_plan(QueryRequest(query=text))
        assert plan.dimension is Dimension.NONE, f"{text!r} -> {plan.dimension.value}"
        assert plan.viz_type is VizType.SINGLE_VALUE


def test_an_explicit_grouping_still_beats_the_counting_lead_in():
    """"How many per year" is a counting question WITH a grouping, and must
    still produce a series. The fix must not swallow these."""
    for text, expected in [
        ("How many trials started each year for glioblastoma?", Dimension.YEAR),
        ("How many melanoma trials by phase?", Dimension.PHASE),
        ("Count the trials per year since 2015.", Dimension.YEAR),
    ]:
        plan = fallback_plan(QueryRequest(query=text))
        assert plan.dimension is expected, f"{text!r} -> {plan.dimension.value}"
        assert plan.viz_type is not VizType.SINGLE_VALUE


def test_plan_rejects_single_value_comparison():
    with pytest.raises(ValueError, match="at least two"):
        QueryPlan(dimension=Dimension.PHASE, viz_type=VizType.BAR_CHART, title="t",
                  interpretation="i", compare_values=["OnlyOne"])


def test_explicit_request_fields_override_inferred_and_are_recorded():
    plan = QueryPlan(dimension=Dimension.PHASE, viz_type=VizType.BAR_CHART, title="t",
                     interpretation="i", filters=PlanFilters(drug_name="GuessedDrug"))
    merged = plan.merged_with_request(QueryRequest(query="phases for it", drug_name="RealDrug"))
    assert merged.filters.drug_name == "RealDrug"
    assert any("overrode" in a for a in merged.assumptions), "an override must be disclosed"


def test_fallback_planner_covers_every_appendix_query_class():
    cases = [
        ("How has the number of trials changed per year since 2015?", Dimension.YEAR),
        ("How are trials distributed across phases?", Dimension.PHASE),
        ("Show a network of sponsors and drugs.", Dimension.SPONSOR_DRUG_NETWORK),
        ("Which countries have the most recruiting trials?", Dimension.COUNTRY),
        ("How many trials are there?", Dimension.NONE),
    ]
    for text, expected in cases:
        assert fallback_plan(QueryRequest(query=text)).dimension is expected, text


def test_request_rejects_incoherent_year_range():
    with pytest.raises(ValueError, match="start_year"):
        QueryRequest(query="trials by year", start_year=2024, end_year=2020)


def test_request_rejects_empty_list_filters():
    with pytest.raises(ValueError, match="non-empty"):
        QueryRequest(query="trials by phase", phase=[])


def test_request_rejects_too_short_query():
    with pytest.raises(ValueError, match="at least 3 characters"):
        QueryRequest(query="q")


# -------------------------------------------------------------- schema shape


def test_network_visualization_rejects_dangling_edges():
    from app.models import GraphEdge, GraphNode
    with pytest.raises(ValueError, match="unknown node"):
        Visualization(
            type=VizType.NETWORK_GRAPH, title="t", encoding=Encoding(),
            nodes=[GraphNode(id="a", label="A", type="sponsor", count=1)],
            edges=[GraphEdge(source="a", target="ghost", weight=1)],
        )


def test_non_network_visualization_requires_data():
    with pytest.raises(ValueError, match="requires data"):
        Visualization(type=VizType.BAR_CHART, title="t", encoding=Encoding(), data=[])


def test_citation_rejects_malformed_nct_id():
    with pytest.raises(ValueError):
        Citation(nct_id="12345678", excerpt="x", field="briefTitle")
