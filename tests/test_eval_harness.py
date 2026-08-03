"""Tests for the eval harness itself.

An eval that silently stops measuring is worse than no eval: it reports a
green rate for a system nobody is checking. These are the cases where that
could happen.

The first group is the important one. `evals/cases.py` deliberately restates
the bucket vocabularies rather than importing them from `app.executor` — that
duplication is what makes golden.json an independent measurement. But
duplication drifts, and a drifted vocabulary would show up as *every bucket in
a dimension failing*, which reads like a system bug rather than a harness bug.
So the divergence is asserted here instead of discovered there.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.ctgov import Filter
from app.executor import (
    COUNTRIES as EXEC_COUNTRIES,
    INTERVENTION_TYPES as EXEC_ITYPES,
    MULTI_VALUED_DIMENSIONS,
    PHASES as EXEC_PHASES,
    STATUSES as EXEC_STATUSES,
)
from app.models import (
    Datum, Encoding, QueryResponse, ResponseMeta, TraceStep, Visualization, VizType,
)
from evals import cases as case_mod
from evals.golden import GOLDEN_PATH, _expected_exclusivity
from evals.run_eval import (
    check_counts, check_exclusivity, check_fabricated_numbers, check_schema, replay_trace,
)


# ------------------------------------------------- harness / system parity


@pytest.mark.parametrize("name,mine,theirs", [
    ("PHASES", case_mod.PHASES, EXEC_PHASES),
    ("STATUSES", case_mod.STATUSES, EXEC_STATUSES),
    ("INTERVENTION_TYPES", case_mod.INTERVENTION_TYPES, EXEC_ITYPES),
    ("COUNTRIES", case_mod.COUNTRIES, EXEC_COUNTRIES),
])
def test_bucket_vocabularies_have_not_drifted(name, mine, theirs):
    assert mine == theirs, (
        f"evals/cases.py {name} no longer matches app/executor.py. The duplication is "
        f"intentional, the drift is not: golden.json would expect buckets the executor "
        f"never probes and every case in that dimension would fail for the wrong reason."
    )


def test_multi_valued_dimension_sets_agree():
    assert case_mod.MULTI_VALUED == {d.value for d in MULTI_VALUED_DIMENSIONS}


def test_eval_merge_and_composes_advanced_clauses():
    """The harness must reproduce AND-composition, not key overwriting.

    This is the shipped bug the eval exists to catch. If `evals.cases.merge`
    overwrote instead of AND-ing, golden.json would carry the same wrong
    intersection the executor once did and the eval would confirm the bug.
    """
    merged = case_mod.merge(
        {"query.cond": "melanoma", "filter.advanced": 'AREA[InterventionName]"A"'},
        {"filter.advanced": 'AREA[InterventionName]"B"'},
    )
    assert merged["filter.advanced"] == (
        'AREA[InterventionName]"A" AND AREA[InterventionName]"B"'
    )
    assert merged["query.cond"] == "melanoma"

    # Same result as the production Filter, reached by different code.
    production = Filter({"query.cond": "melanoma",
                         "filter.advanced": 'AREA[InterventionName]"A"'}).merged_with(
        Filter({"filter.advanced": 'AREA[InterventionName]"B"'})
    )
    assert production.params == merged


def test_plain_keys_still_overwrite_in_the_harness():
    """Matching production exactly matters in both directions."""
    assert case_mod.merge({"query.intr": "A"}, {"query.intr": "B"})["query.intr"] == "B"


# -------------------------------------------------------- golden data shape


def _golden() -> dict[str, Any]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def test_golden_file_is_committed_and_complete():
    golden = _golden()
    ids = {c["id"] for c in golden["cases"]}
    assert len(golden["cases"]) >= 20, "the golden set is meant to be 20-40 cases"
    assert ids == {c.id for c in case_mod.CASES}, (
        "golden.json and evals/cases.py disagree about which cases exist; "
        "regenerate with `python -m evals.golden`"
    )
    assert not golden.get("failed_case_ids"), (
        f"golden.json has unmeasured cases: {golden.get('failed_case_ids')}"
    )


def test_golden_covers_every_query_class():
    """A harness that quietly stops covering a class reports a misleading rate."""
    dimensions = {c["expect_dimension"] for c in _golden()["cases"]}
    required = {
        "phase", "status", "year", "country", "intervention_type", "sponsor",
        "none", "sponsor_drug_network", "drug_drug_network",
    }
    assert required <= dimensions, f"missing coverage for {sorted(required - dimensions)}"


def test_golden_counts_are_non_negative_integers():
    for case in _golden()["cases"]:
        assert isinstance(case["total_matching"], int) and case["total_matching"] >= 0
        for label, bucket in case["buckets"].items():
            assert isinstance(bucket["count"], int) and bucket["count"] >= 0, label
            assert bucket["params"], f"{case['id']}/{label} has no probe params"


def test_expected_exclusivity_rules():
    phase = case_mod.by_id("phase_melanoma")
    # Overlap and absent values can cancel exactly; a multi-valued dimension is
    # non-exclusive even when the arithmetic says otherwise.
    assert _expected_exclusivity(phase, 3738, 3738) is False
    year = case_mod.by_id("year_glioblastoma")
    assert _expected_exclusivity(year, 758, 758) is True
    assert _expected_exclusivity(year, 758, 700) is False
    assert _expected_exclusivity(case_mod.by_id("total_novartis"), 5062, 5062) is True
    network = case_mod.by_id("network_sponsor_drug_melanoma")
    assert _expected_exclusivity(network, 3738, 3738) is False


# ------------------------------------------------------------ metric logic


def _response(title="Melanoma trials by phase", interpretation="Trials grouped by phase.",
              assumptions=None, data=None, trace=None, exclusive=False,
              total=3738) -> QueryResponse:
    return QueryResponse(
        visualization=Visualization(
            type=VizType.BAR_CHART, title=title,
            encoding=Encoding(x={"field": "label"}, y={"field": "count"}),
            data=data or [Datum(label="PHASE2", count=1529, citation_total=1529)],
            returned=1, total=1,
        ),
        meta=ResponseMeta(
            interpretation=interpretation, trace=trace or [],
            total_matching=total, denominator_note="n",
            buckets_are_exclusive=exclusive,
            assumptions=assumptions or [],
        ),
    )


def test_fabricated_number_scan_catches_a_model_written_count():
    golden = {"query": "How are melanoma trials distributed across phases?", "request": {}}
    hits = check_fabricated_numbers(
        _response(title="Melanoma trials by phase (3,738 total)"), golden
    )
    assert hits and "3,738" in hits[0]


def test_fabricated_number_scan_allows_caller_supplied_years():
    golden = {
        "query": "Melanoma trials per year 2018-2024",
        "request": {"start_year": 2018, "end_year": 2024},
        "allowed_years": list(range(2018, 2025)),
    }
    assert not check_fabricated_numbers(
        _response(title="Melanoma trials per year, 2018-2024",
                  assumptions=["Assumed the window 2018 to 2024."]),
        golden,
    )


def test_fabricated_number_scan_reads_assumptions_too():
    golden = {"query": "melanoma by phase", "request": {}}
    hits = check_fabricated_numbers(
        _response(assumptions=["Assumed the 3738 melanoma trials are the population."]),
        golden,
    )
    assert any("assumptions" in h for h in hits)


class _StubProber:
    """Answers replays from a table; anything unlisted answers 0."""

    def __init__(self, table: dict[str, int]):
        self.table = table
        self.seen: list[dict[str, str]] = []

    async def count(self, params: dict[str, str]) -> int:
        self.seen.append(params)
        return self.table.get(json.dumps(params, sort_keys=True), 0)


def _step(name: str, params: dict[str, str], result: int) -> TraceStep:
    return TraceStep(step=name, url="https://clinicaltrials.gov/api/v2/studies",
                     params={**params, "countTotal": "true", "pageSize": "1"},
                     result_count=result)


async def test_trace_replay_passes_when_the_api_agrees():
    params = {"query.cond": "melanoma"}
    prober = _StubProber({json.dumps(params, sort_keys=True): 3738})
    result = await replay_trace(
        _response(trace=[_step("count matching", params, 3738)]), prober
    )
    assert result["passed"] == 1 and not result["mismatches"]
    # countTotal/pageSize are the probe's own knobs and must be stripped before
    # replay, or the comparison is against a different request.
    assert prober.seen == [params]


async def test_trace_replay_catches_a_number_the_api_does_not_support():
    params = {"query.cond": "melanoma"}
    prober = _StubProber({json.dumps(params, sort_keys=True): 3738})
    result = await replay_trace(
        _response(trace=[_step("count matching", params, 9999)]), prober
    )
    assert result["passed"] == 0
    assert result["mismatches"][0]["reported"] == 9999
    assert result["mismatches"][0]["replayed"] == 3738


async def test_trace_replay_does_not_count_the_network_summary_step_as_a_pass():
    """The network fan-out step is a request tally, not a totalCount.

    Replaying it would compare a count of probes against a count of studies.
    Treating it as a pass would inflate the headline metric; treating it as a
    failure would be wrong too. It is excluded and reported as uncovered.
    """
    prober = _StubProber({})
    result = await replay_trace(
        _response(trace=[
            _step("count matching", {"query.cond": "melanoma"}, 0),
            _step("probe 12 nodes + 15 edges", {}, 27),
        ]),
        prober,
    )
    assert result["steps_replayable"] == 1
    assert result["steps_skipped"] == ["probe 12 nodes + 15 edges"]


async def test_count_check_flags_a_wrong_bucket():
    golden = {
        "total_matching": 3738, "verify": "static", "expect_dimension": "phase",
        "base_params": {"query.cond": "melanoma"},
        "buckets": {"PHASE2": {"params": {}, "count": 1600}},
    }
    result = await check_counts(_response(), golden, _StubProber({}))
    assert not result["passed"]
    assert "PHASE2" in result["problems"][0]


async def test_count_check_flags_a_missing_bucket():
    golden = {
        "total_matching": 3738, "verify": "static", "expect_dimension": "phase",
        "base_params": {}, "buckets": {"PHASE3": {"params": {}, "count": 402}},
    }
    result = await check_counts(_response(), golden, _StubProber({}))
    assert any("missing bucket" in p for p in result["problems"])


async def test_count_check_passes_a_correct_distribution():
    golden = {
        "total_matching": 3738, "verify": "static", "expect_dimension": "phase",
        "base_params": {}, "buckets": {"PHASE2": {"params": {}, "count": 1529}},
    }
    assert (await check_counts(_response(), golden, _StubProber({})))["passed"]


def test_exclusivity_check_rejects_a_false_partition():
    golden = {"expect_exclusive": False, "expect_dimension": "phase"}
    result = check_exclusivity(_response(exclusive=True), golden)
    assert not result["passed"] and "pie chart" in result["problems"][0]


def test_exclusivity_check_accepts_a_real_partition():
    golden = {"expect_exclusive": True, "expect_dimension": "year"}
    assert check_exclusivity(_response(exclusive=True), golden)["passed"]


def test_schema_check_round_trips():
    assert check_schema(_response())["passed"]
