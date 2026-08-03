"""Hand-written eval cases.

Each case pairs a natural-language question with the *literal* ClinicalTrials.gov
parameter dicts a correct answer must be built from. Those dicts are written out
by hand here — they are not derived from `app.executor.base_filter` or
`app.executor.bucket_filters`. That duplication is the point: it is what makes
`evals/golden.json` an independent measurement rather than a recording of the
system's own output.

Two deliberate scoping decisions:

1. **Base filters are pinned via the structured request fields.** A case sends
   `condition="melanoma"` rather than relying on the planner to extract it from
   prose. Otherwise a count mismatch would be ambiguous — did the executor
   mis-bucket, or did the planner extract `"melanoma skin cancer"`? Entity
   extraction is therefore *not* what these cases measure. Routing is: the
   dimension and chart type come only from the question text.

2. **Dimensions with a discovered vocabulary get verified, not predicted.**
   Sponsor buckets and network nodes come from a bounded sample of records, so
   their labels cannot be known in advance. For those cases golden.json stores
   the base total only, and `run_eval` independently re-probes whatever labels
   came back. Still independent ground truth — just measured at scoring time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# Bucket vocabularies, restated by hand. Kept in sync with app/executor.py by
# `tests/test_eval_golden.py`, which fails if the two ever diverge — a silent
# divergence would make every bucket in a dimension look like a count miss.
PHASES = ["EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4", "NA"]
STATUSES = [
    "RECRUITING", "NOT_YET_RECRUITING", "ACTIVE_NOT_RECRUITING", "COMPLETED",
    "TERMINATED", "WITHDRAWN", "SUSPENDED", "UNKNOWN",
]
INTERVENTION_TYPES = [
    "DRUG", "BIOLOGICAL", "DEVICE", "PROCEDURE", "RADIATION", "BEHAVIORAL",
    "DIETARY_SUPPLEMENT", "GENETIC", "OTHER",
]
COUNTRIES = [
    "United States", "China", "France", "Germany", "Canada", "Spain",
    "United Kingdom", "Italy", "Japan", "Australia", "Netherlands", "Korea",
]

# Dimensions where one trial can hold several values, so buckets can never
# partition the result set regardless of what the arithmetic says.
MULTI_VALUED = {"phase", "country", "intervention_type", "sponsor"}

VerifyMode = Literal["static", "sponsor_labels", "country_labels", "network"]


def merge(*parts: dict[str, str]) -> dict[str, str]:
    """Combine param dicts, AND-ing `filter.advanced` clauses.

    Written independently of `app.ctgov.Filter.merged_with`. The important
    property is the one the production bug log is about: two advanced clauses
    compose with AND rather than one overwriting the other.
    """
    out: dict[str, str] = {}
    advanced: list[str] = []
    for part in parts:
        for key, value in part.items():
            if key == "filter.advanced":
                advanced.append(value)
            else:
                out[key] = value
    if advanced:
        out["filter.advanced"] = " AND ".join(advanced)
    return out


def adv(expr: str) -> dict[str, str]:
    return {"filter.advanced": expr}


def year_range(lo: int | None, hi: int | None) -> dict[str, str]:
    a = f"{lo}-01-01" if lo else "MIN"
    b = f"{hi}-12-31" if hi else "MAX"
    return adv(f"AREA[StartDate]RANGE[{a},{b}]")


@dataclass(frozen=True)
class Case:
    id: str
    query: str
    #: Structured fields sent alongside the prose, pinning the base filter.
    request: dict[str, Any] = field(default_factory=dict)
    #: Literal base params a correct answer must be measured against.
    base_params: dict[str, str] = field(default_factory=dict)
    #: label -> literal params, for fixed-vocabulary dimensions.
    buckets: dict[str, dict[str, str]] = field(default_factory=dict)
    expect_dimension: str = "phase"
    expect_viz: tuple[str, ...] = ("bar_chart",)
    verify: VerifyMode = "static"
    #: Years the response prose is allowed to mention, beyond the request's own.
    allowed_years: tuple[int, ...] = ()
    notes: str = ""

    @property
    def multi_valued(self) -> bool:
        return self.expect_dimension in MULTI_VALUED


def _phase_case(cid, query, req, base, dim_notes="") -> Case:
    return Case(
        id=cid, query=query, request=req, base_params=base,
        buckets={p: merge(base, adv(f"AREA[Phase]{p}")) for p in PHASES},
        expect_dimension="phase", expect_viz=("bar_chart",), notes=dim_notes,
    )


def _status_case(cid, query, req, base) -> Case:
    return Case(
        id=cid, query=query, request=req, base_params=base,
        buckets={s: merge(base, adv(f"AREA[OverallStatus]{s}")) for s in STATUSES},
        expect_dimension="status", expect_viz=("bar_chart",),
    )


def _year_case(cid, query, req, base, lo, hi) -> Case:
    return Case(
        id=cid, query=query, request=req, base_params=base,
        buckets={
            str(y): merge(base, year_range(y, y)) for y in range(lo, hi + 1)
        },
        expect_dimension="year", expect_viz=("time_series", "bar_chart"),
        allowed_years=tuple(range(lo, hi + 1)),
    )


def _country_case(cid, query, req, base) -> Case:
    """Country buckets are resolved from the result set at execution time.

    This case used to pin the executor's old hardcoded list of twelve, which
    meant the golden set asserted an implementation detail rather than the
    truth. Verification is dynamic: every label the system returns is re-probed
    through evals.api, which shares no code with app.ctgov.
    """
    return Case(
        id=cid, query=query, request=req, base_params=base,
        buckets={},
        expect_dimension="country", expect_viz=("bar_chart",),
        verify="country_labels",
    )


def _itype_case(cid, query, req, base) -> Case:
    return Case(
        id=cid, query=query, request=req, base_params=base,
        buckets={
            t: merge(base, adv(f"AREA[InterventionType]{t}")) for t in INTERVENTION_TYPES
        },
        expect_dimension="intervention_type", expect_viz=("bar_chart",),
    )


def _total_case(cid, query, req, base) -> Case:
    return Case(
        id=cid, query=query, request=req, base_params=base, buckets={},
        expect_dimension="none", expect_viz=("single_value",),
    )


def _compare_case(cid, query, req, base, field_area, values, bucket_vocab, kind) -> Case:
    """Cross the bucket vocabulary with each compared entity.

    Comparison values are deliberately NOT pinned in `request` — extracting
    "Pembrolizumab and Nivolumab" from the prose is part of what a comparison
    case is testing. If the planner misses the comparison, the expected keys
    simply will not appear and the case fails on count accuracy, which is the
    correct outcome.
    """
    area_expr = {
        "phase": lambda b: f"AREA[Phase]{b}",
        "status": lambda b: f"AREA[OverallStatus]{b}",
    }[kind]
    buckets: dict[str, dict[str, str]] = {}
    for value in values:
        for bucket in bucket_vocab:
            buckets[f"{value}||{bucket}"] = merge(
                base, adv(area_expr(bucket)), adv(f'{field_area}"{value}"')
            )
    return Case(
        id=cid, query=query, request=req, base_params=base, buckets=buckets,
        expect_dimension=kind, expect_viz=("grouped_bar_chart",),
    )


CASES: list[Case] = [
    # ---------------------------------------------------------------- phase
    _phase_case("phase_melanoma", "How are melanoma trials distributed across phases?",
                {"condition": "melanoma"}, {"query.cond": "melanoma"},
                "README's headline example: 136 unbucketed, non-exclusive."),
    _phase_case("phase_pembrolizumab", "Show the phase breakdown for pembrolizumab trials.",
                {"drug_name": "Pembrolizumab"}, {"query.intr": "Pembrolizumab"},
                "README's overlap example: bucket sum exceeds matched total."),
    _phase_case("phase_glioblastoma", "What phases are glioblastoma trials in?",
                {"condition": "glioblastoma"}, {"query.cond": "glioblastoma"}),
    _phase_case("phase_nsclc_recruiting",
                "Break down currently recruiting non-small cell lung cancer trials by phase.",
                {"condition": "non-small cell lung cancer", "status": ["RECRUITING"]},
                merge({"query.cond": "non-small cell lung cancer"},
                      adv("(AREA[OverallStatus]RECRUITING)"))),
    _phase_case("phase_pfizer", "Distribution of Pfizer trials by phase.",
                {"sponsor": "Pfizer"}, {"query.spons": "Pfizer"}),
    _phase_case("phase_covid_windowed",
                "Phase distribution of COVID-19 trials started between 2020 and 2022.",
                {"condition": "COVID-19", "start_year": 2020, "end_year": 2022},
                merge({"query.cond": "COVID-19"}, year_range(2020, 2022))),

    # --------------------------------------------------------------- status
    _status_case("status_melanoma", "What is the recruitment status of melanoma trials?",
                 {"condition": "melanoma"}, {"query.cond": "melanoma"}),
    _status_case("status_pembrolizumab", "Show pembrolizumab trials by status.",
                 {"drug_name": "Pembrolizumab"}, {"query.intr": "Pembrolizumab"}),
    _status_case("status_breast_cancer", "Breast cancer trials grouped by overall status.",
                 {"condition": "breast cancer"}, {"query.cond": "breast cancer"}),
    _status_case("status_alzheimers", "How many Alzheimer's disease trials are in each status?",
                 {"condition": "Alzheimer Disease"}, {"query.cond": "Alzheimer Disease"}),

    # ----------------------------------------------------------------- year
    _year_case("year_glioblastoma", "How many glioblastoma trials started each year since 2018?",
               {"condition": "glioblastoma", "start_year": 2018, "end_year": 2024},
               merge({"query.cond": "glioblastoma"}, year_range(2018, 2024)), 2018, 2024),
    _year_case("year_pembrolizumab", "Pembrolizumab trials per year from 2016 to 2023.",
               {"drug_name": "Pembrolizumab", "start_year": 2016, "end_year": 2023},
               merge({"query.intr": "Pembrolizumab"}, year_range(2016, 2023)), 2016, 2023),
    _year_case("year_covid", "Trend of COVID-19 trial starts per year 2020 through 2024.",
               {"condition": "COVID-19", "start_year": 2020, "end_year": 2024},
               merge({"query.cond": "COVID-19"}, year_range(2020, 2024)), 2020, 2024),
    _year_case("year_melanoma", "How has the number of melanoma trials changed each year "
                                "between 2015 and 2022?",
               {"condition": "melanoma", "start_year": 2015, "end_year": 2022},
               merge({"query.cond": "melanoma"}, year_range(2015, 2022)), 2015, 2022),

    # -------------------------------------------------------------- country
    _country_case("country_pancreatic_recruiting",
                  "Which countries are running recruiting pancreatic cancer trials?",
                  {"condition": "pancreatic cancer", "status": ["RECRUITING"]},
                  merge({"query.cond": "pancreatic cancer"},
                        adv("(AREA[OverallStatus]RECRUITING)"))),
    _country_case("country_melanoma", "Where in the world are melanoma trials run?",
                  {"condition": "melanoma"}, {"query.cond": "melanoma"}),
    _country_case("country_diabetes", "Geographic distribution of type 2 diabetes trials by country.",
                  {"condition": "type 2 diabetes"}, {"query.cond": "type 2 diabetes"}),

    # ---------------------------------------------------- intervention type
    _itype_case("itype_melanoma", "What types of intervention do melanoma trials use?",
                {"condition": "melanoma"}, {"query.cond": "melanoma"}),
    _itype_case("itype_alzheimers",
                "Break Alzheimer's trials down by intervention type.",
                {"condition": "Alzheimer Disease"}, {"query.cond": "Alzheimer Disease"}),

    # ------------------------------------------------------- single totals
    _total_case("total_pembrolizumab", "How many pembrolizumab trials are there in total?",
                {"drug_name": "Pembrolizumab"}, {"query.intr": "Pembrolizumab"}),
    _total_case("total_melanoma_recruiting",
                "What is the total number of recruiting melanoma trials?",
                {"condition": "melanoma", "status": ["RECRUITING"]},
                merge({"query.cond": "melanoma"}, adv("(AREA[OverallStatus]RECRUITING)"))),
    _total_case("total_novartis", "How many trials does Novartis sponsor?",
                {"sponsor": "Novartis"}, {"query.spons": "Novartis"}),
    _total_case("total_glioblastoma_since_2020",
                "Count the glioblastoma trials that started in 2020 or later.",
                {"condition": "glioblastoma", "start_year": 2020},
                merge({"query.cond": "glioblastoma"}, year_range(2020, None))),

    # --------------------------------------------------------- comparisons
    _compare_case("compare_pembro_nivo",
                  "Compare Pembrolizumab and Nivolumab across trial phases.",
                  {}, {}, "AREA[InterventionName]",
                  ["Pembrolizumab", "Nivolumab"], PHASES, "phase"),
    _compare_case("compare_melanoma_nsclc_status",
                  "Compare melanoma and non-small cell lung cancer trials by recruitment status.",
                  {}, {}, "AREA[ConditionSearch]",
                  ["melanoma", "non-small cell lung cancer"], STATUSES, "status"),
    _compare_case("compare_pfizer_novartis_phase",
                  "Compare Pfizer versus Novartis trials across phases.",
                  {}, {}, "AREA[LeadSponsorName]",
                  ["Pfizer", "Novartis"], PHASES, "phase"),

    # ------------------------------------------------------------- sponsor
    Case(
        id="sponsor_melanoma",
        query="Which sponsors run the most melanoma trials?",
        request={"condition": "melanoma"}, base_params={"query.cond": "melanoma"},
        expect_dimension="sponsor", expect_viz=("bar_chart",), verify="sponsor_labels",
        notes="Sponsor labels are discovered from a sample, so they are verified, not predicted.",
    ),
    Case(
        id="sponsor_lung_cancer",
        query="Show the leading sponsors of lung cancer trials.",
        request={"condition": "lung cancer"}, base_params={"query.cond": "lung cancer"},
        expect_dimension="sponsor", expect_viz=("bar_chart",), verify="sponsor_labels",
    ),

    # ------------------------------------------------------------ networks
    Case(
        id="network_sponsor_drug_melanoma",
        query="Show the network of relationships between sponsors and drugs in melanoma trials.",
        request={"condition": "melanoma"}, base_params={"query.cond": "melanoma"},
        expect_dimension="sponsor_drug_network", expect_viz=("network_graph",),
        verify="network",
    ),
    Case(
        id="network_drug_drug_nsclc",
        query="Which drugs are studied in combination with each other in non-small cell "
              "lung cancer trials? Show the drug co-occurrence network.",
        request={"condition": "non-small cell lung cancer"},
        base_params={"query.cond": "non-small cell lung cancer"},
        expect_dimension="drug_drug_network", expect_viz=("network_graph",),
        verify="network",
    ),
    Case(
        id="network_sponsor_drug_pembrolizumab",
        query="Show sponsor-drug relationships across pembrolizumab trials as a network.",
        request={"drug_name": "Pembrolizumab"}, base_params={"query.intr": "Pembrolizumab"},
        expect_dimension="sponsor_drug_network", expect_viz=("network_graph",),
        verify="network",
        notes="Regression for the shipped bug where the base drug filter was overwritten "
              "by each node's own filter, returning an unrelated portfolio.",
    ),
]


def by_id(case_id: str) -> Case:
    for case in CASES:
        if case.id == case_id:
            return case
    raise KeyError(case_id)
