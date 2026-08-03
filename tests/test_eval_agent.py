"""Offline tests for the LangGraph planning agent and the eval harness.

No network, no API key. The model is a scripted stub and ClinicalTrials.gov is
a fake client with a fixed count table, so every assertion here is about the
graph's control flow rather than about what a real model happens to say today.

The load-bearing test is `test_graph_cannot_emit_a_number`. Adding a graph
means adding model calls, and every model call is a new surface a fabricated
figure could arrive on. That test drives the graph with a stub that tries to
put a count into every free-text field it owns and asserts none of them reach
the plan — and therefore none reach the rendered response.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any

import pytest

from app.agent import (
    CRITIQUE_SCHEMA,
    DECOMPOSE_SCHEMA,
    MAX_REVISIONS,
    RESOLVE_SCHEMA,
    GraphPlanner,
    make_planner,
)
from app.ctgov import CTGovClient, Filter
from app.executor import execute
from app.models import QueryRequest
from app.planner import Dimension, Planner

NUMERIC_RUN = re.compile(r"\d[\d,]{2,}")


# ------------------------------------------------------------------- stubs


class StubModel:
    """An OpenAI-shaped client that replies from a script keyed by schema name.

    `Planner` and every node in `GraphPlanner` go through the same
    `chat.completions.create` call, so one stub covers all of them and the
    tests exercise the real code path rather than a mocked-out planner.
    """

    def __init__(self, script: dict[str, Any]):
        #: schema name -> payload, or a list of payloads consumed in order.
        self.script = {k: (list(v) if isinstance(v, list) else v) for k, v in script.items()}
        self.calls: list[str] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, *, model: str, messages: list[dict[str, str]],
                      response_format: dict[str, Any]) -> Any:
        name = response_format["json_schema"]["name"]
        self.calls.append(name)
        payload = self.script.get(name, {})
        if isinstance(payload, list):
            payload = payload.pop(0) if len(payload) > 1 else payload[0]
        content = json.dumps(payload)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


class FakeCTGov(CTGovClient):
    """Counts from a table keyed by a param signature. Records every probe."""

    def __init__(self, counts: dict[str, int], default: int = 0):
        super().__init__()
        self._counts = counts
        self._default = default
        self.probes: list[dict[str, str]] = []

    @staticmethod
    def key(params: dict[str, str]) -> str:
        return "&".join(f"{k}={v}" for k, v in sorted(params.items()))

    async def count(self, filt: Filter) -> int:
        self.probes.append(dict(filt.params))
        return self._counts.get(self.key(filt.params), self._default)

    async def sample_studies(self, filt: Filter, limit: int = 3, fields: str = ""):
        return []


def _plan_payload(**overrides: Any) -> dict[str, Any]:
    base = {
        "dimension": "phase", "viz_type": "bar_chart",
        "title": "Melanoma trials by phase",
        "interpretation": "Grouping melanoma trials by phase.",
        "filters": {"drug_name": None, "condition": "melanoma", "sponsor": None,
                    "location": None, "phase": None, "status": None,
                    "start_year": None, "end_year": None},
        "compare_values": None, "compare_field": None, "assumptions": [],
    }
    filters = overrides.pop("filters", None)
    if filters:
        base["filters"] = {**base["filters"], **filters}
    base.update(overrides)
    return base


def _approve() -> dict[str, Any]:
    return {"approved": True, "problems": [], "suggested_dimension": None,
            "suggested_viz_type": None}


def _no_split(question: str) -> dict[str, Any]:
    return {"is_compound": False, "subquestions": [question]}


# ------------------------------------------------------- interface parity


async def test_graph_planner_is_a_drop_in_for_planner():
    """`plan(request) -> QueryPlan`, same as `Planner`, so main.py can swap it."""
    question = "How are melanoma trials distributed across phases?"
    model = StubModel({
        "decomposition": _no_split(question),
        "query_plan": _plan_payload(),
        "entity_resolution": {"candidates": []},
        "plan_critique": _approve(),
    })
    graph = GraphPlanner(client=model, ctgov=FakeCTGov({}, default=3738))

    plan = await graph.plan(QueryRequest(query=question))

    assert plan.dimension is Dimension.PHASE
    assert plan.viz_type.value == "bar_chart"
    assert plan.filters.condition == "melanoma"
    # Same public surface as the single-call planner.
    assert hasattr(Planner, "plan") and callable(graph.plan)


async def test_make_planner_switches_on_env(monkeypatch):
    monkeypatch.delenv("PLANNER", raising=False)
    assert isinstance(make_planner(client=StubModel({})), Planner)
    monkeypatch.setenv("PLANNER", "graph")
    assert isinstance(make_planner(client=StubModel({})), GraphPlanner)
    # The single-call planner stays the default for anything unrecognised.
    monkeypatch.setenv("PLANNER", "something-else")
    assert isinstance(make_planner(client=StubModel({})), Planner)


# ------------------------------------------------------------ decomposition


async def test_compound_question_yields_two_subplans():
    """"Phases AND countries" is two distributions; one QueryPlan cannot hold both."""
    question = "Show phases and countries for melanoma trials"
    model = StubModel({
        "decomposition": {
            "is_compound": True,
            "subquestions": ["Show phases for melanoma trials",
                             "Show countries for melanoma trials"],
        },
        "query_plan": [
            _plan_payload(dimension="phase"),
            _plan_payload(dimension="country", title="Melanoma trials by country"),
        ],
        "entity_resolution": {"candidates": []},
        "plan_critique": _approve(),
    })
    graph = GraphPlanner(client=model, ctgov=FakeCTGov({}, default=3738))

    plans = await graph.plan_multi(QueryRequest(query=question))

    assert len(plans) == 2, "a compound question must produce one plan per breakdown"
    assert [p.dimension for p in plans] == [Dimension.PHASE, Dimension.COUNTRY]
    # The single-plan interface must say the second breakdown exists rather than
    # silently discarding half the question.
    assert any("breakdown" in a for a in plans[0].assumptions)


async def test_non_compound_question_is_not_split():
    question = "Recruiting melanoma trials in Germany by phase"
    model = StubModel({
        "decomposition": {"is_compound": True, "subquestions": [question]},
        "query_plan": _plan_payload(),
        "entity_resolution": {"candidates": []},
        "plan_critique": _approve(),
    })
    graph = GraphPlanner(client=model, ctgov=FakeCTGov({}, default=10))

    assert len(await graph.plan_multi(QueryRequest(query=question))) == 1


async def test_decomposition_is_bounded_to_two_subquestions():
    model = StubModel({
        "decomposition": {"is_compound": True,
                          "subquestions": [f"q{i}" for i in range(9)]},
        "query_plan": _plan_payload(),
        "entity_resolution": {"candidates": []},
        "plan_critique": _approve(),
    })
    graph = GraphPlanner(client=model, ctgov=FakeCTGov({}, default=5))

    plans = await graph.plan_multi(QueryRequest(query="lots of things at once"))
    assert len(plans) <= 2, "an unbounded decomposition is an unbounded fan-out"


# -------------------------------------------------------- entity resolution


async def test_entity_resolution_is_decided_by_probe_not_by_the_model():
    """Brand -> generic is only accepted when the API says the generic matches more.

    The model may assert "Keytruda is Pembrolizumab"; that assertion is not
    what changes the plan. Both spellings are counted and the registry decides.
    """
    counts = {
        "query.intr=Keytruda": 41,
        "query.intr=Pembrolizumab": 2915,
    }
    ctgov = FakeCTGov(counts, default=2915)
    model = StubModel({
        "decomposition": _no_split("Keytruda trials by phase"),
        "query_plan": _plan_payload(filters={"drug_name": "Keytruda", "condition": None}),
        "entity_resolution": {"candidates": [{
            "field": "drug_name", "original": "Keytruda",
            "alternatives": ["Pembrolizumab"],
        }]},
        "plan_critique": _approve(),
    })
    graph = GraphPlanner(client=model, ctgov=ctgov)

    plan = await graph.plan(QueryRequest(query="Keytruda trials by phase"))

    assert plan.filters.drug_name == "Pembrolizumab"
    probed = {FakeCTGov.key(p) for p in ctgov.probes}
    assert "query.intr=Keytruda" in probed and "query.intr=Pembrolizumab" in probed, (
        "both spellings must be counted; a resolution taken from model knowledge "
        "alone is exactly the unverified assertion this system forbids"
    )


async def test_unverified_alternative_is_rejected():
    """An alternative the API does not support must not replace the original."""
    ctgov = FakeCTGov(
        {"query.intr=Pembrolizumab": 2915, "query.intr=Pembrolizumabb": 0},
        default=2915,
    )
    model = StubModel({
        "decomposition": _no_split("Pembrolizumab trials by phase"),
        "query_plan": _plan_payload(
            filters={"drug_name": "Pembrolizumab", "condition": None}),
        "entity_resolution": {"candidates": [{
            "field": "drug_name", "original": "Pembrolizumab",
            "alternatives": ["Pembrolizumabb"],
        }]},
        "plan_critique": _approve(),
    })
    graph = GraphPlanner(client=model, ctgov=ctgov)

    plan = await graph.plan(QueryRequest(query="Pembrolizumab trials by phase"))
    assert plan.filters.drug_name == "Pembrolizumab"


async def test_resolution_survives_probe_failure():
    """If the probe cannot run, keep the original rather than act on belief."""

    class Broken(FakeCTGov):
        async def count(self, filt: Filter) -> int:
            if "query.intr" in filt.params:
                raise RuntimeError("upstream down")
            return 100

    model = StubModel({
        "decomposition": _no_split("Keytruda trials by phase"),
        "query_plan": _plan_payload(filters={"drug_name": "Keytruda", "condition": None}),
        "entity_resolution": {"candidates": [{
            "field": "drug_name", "original": "Keytruda",
            "alternatives": ["Pembrolizumab"],
        }]},
        "plan_critique": _approve(),
    })
    graph = GraphPlanner(client=model, ctgov=Broken({}, default=100))

    plan = await graph.plan(QueryRequest(query="Keytruda trials by phase"))
    assert plan.filters.drug_name == "Keytruda"


# ---------------------------------------------------------- self-correction


async def test_zero_result_plan_is_revised():
    """A plan matching nothing is retried before an empty chart is returned."""
    ctgov = FakeCTGov({"query.cond=melanoma": 3738}, default=0)
    model = StubModel({
        "decomposition": _no_split("Trials for a mistyped condition, by phase"),
        "query_plan": [
            _plan_payload(filters={"condition": "melanomaa"}),
            _plan_payload(filters={"condition": "melanoma"}),
        ],
        "entity_resolution": {"candidates": []},
        "plan_critique": _approve(),
    })
    graph = GraphPlanner(client=model, ctgov=ctgov)

    plan = await graph.plan(QueryRequest(query="Trials for a mistyped condition, by phase"))

    assert plan.filters.condition == "melanoma"
    assert model.calls.count("query_plan") == 2, "the zero result must trigger one re-plan"


async def test_revision_loop_is_bounded():
    """Every filter matches nothing: the graph must give up, not spin."""
    model = StubModel({
        "decomposition": _no_split("Trials for something that does not exist"),
        "query_plan": _plan_payload(filters={"condition": "not-a-condition"}),
        "entity_resolution": {"candidates": []},
        "plan_critique": _approve(),
    })
    graph = GraphPlanner(client=model, ctgov=FakeCTGov({}, default=0))

    plan = await graph.plan(QueryRequest(query="Trials for something that does not exist"))

    assert plan is not None, "an unfixable plan is still returned; the executor reports zero"
    assert model.calls.count("query_plan") <= 1 + MAX_REVISIONS


async def test_critique_rejection_triggers_a_revision():
    """A plan that answers the wrong question is caught before execution."""
    model = StubModel({
        "decomposition": _no_split("How many melanoma trials started each year since 2018?"),
        "query_plan": [
            _plan_payload(dimension="phase", viz_type="bar_chart"),
            _plan_payload(dimension="year", viz_type="time_series",
                          title="Melanoma trials per year",
                          filters={"start_year": 2018, "end_year": 2024}),
        ],
        "entity_resolution": {"candidates": []},
        "plan_critique": [
            {"approved": False,
             "problems": ["the question asks for a per-year trend, not a phase breakdown"],
             "suggested_dimension": "year", "suggested_viz_type": "time_series"},
            _approve(),
        ],
    })
    graph = GraphPlanner(client=model, ctgov=FakeCTGov({}, default=500))

    plan = await graph.plan(
        QueryRequest(query="How many melanoma trials started each year since 2018?")
    )

    assert plan.dimension is Dimension.YEAR
    assert plan.viz_type.value == "time_series"
    assert any("revised" in a for a in plan.assumptions)


async def test_critique_suggestion_cannot_produce_an_illegal_plan():
    """A critic suggesting `year` must not yield `year` + `network_graph`."""
    model = StubModel({
        "decomposition": _no_split("melanoma trials over time"),
        "query_plan": _plan_payload(dimension="phase", viz_type="bar_chart"),
        "entity_resolution": {"candidates": []},
        "plan_critique": [
            {"approved": False, "problems": ["wrong dimension"],
             "suggested_dimension": "year", "suggested_viz_type": "network_graph"},
            _approve(),
        ],
    })
    graph = GraphPlanner(client=model, ctgov=FakeCTGov({}, default=500))

    plan = await graph.plan(QueryRequest(query="melanoma trials over time"))
    # The schema would reject year+network_graph; the repair must pick a legal chart.
    assert plan.dimension is Dimension.YEAR
    assert plan.viz_type.value in {"time_series", "bar_chart", "grouped_bar_chart"}


async def test_graph_degrades_when_the_model_is_unavailable():
    """Every extra node is an enhancement; losing them must not lose the request."""

    class Dead:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._boom))

        async def _boom(self, **_: Any) -> Any:
            raise RuntimeError("model unavailable")

    graph = GraphPlanner(client=Dead(), ctgov=FakeCTGov({}, default=99))
    plan = await graph.plan(QueryRequest(query="melanoma trials by phase", condition="melanoma"))

    assert plan.dimension is Dimension.PHASE, "should fall back to the keyword planner"
    assert plan.filters.condition == "melanoma"


# ------------------------------------------------- the central invariant


def _schema_types(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        value = node.get("type")
        if isinstance(value, str):
            found.add(value)
        elif isinstance(value, list):
            found |= {v for v in value if isinstance(v, str)}
        for child in node.values():
            found |= _schema_types(child)
    elif isinstance(node, list):
        for child in node:
            found |= _schema_types(child)
    return found


def test_no_agent_schema_has_a_numeric_field():
    """The graph must have no slot a model could put a count in.

    This is the structural half of the invariant: not "the model did not emit a
    number this time" but "there is nowhere for a number to go". Adding an
    integer field to any of these schemas should fail here immediately.
    """
    for name, schema in (
        ("DECOMPOSE_SCHEMA", DECOMPOSE_SCHEMA),
        ("RESOLVE_SCHEMA", RESOLVE_SCHEMA),
        ("CRITIQUE_SCHEMA", CRITIQUE_SCHEMA),
    ):
        types = _schema_types(schema)
        assert not types & {"integer", "number"}, (
            f"{name} exposes a numeric field; the graph plans, it does not measure"
        )


async def test_graph_cannot_emit_a_number():
    """A stub that stuffs a count into every field it owns gets none into the plan.

    The graph makes more model calls than the single-call planner, so it has
    more chances to write a figure. `title` is the dangerous one — the executor
    composes `interpretation` itself and drops numeric `assumptions`, but the
    plan's title reaches `visualization.title` verbatim.
    """
    poisoned_title = "Melanoma trials by phase — 3,738 total, 1529 in PHASE2"
    model = StubModel({
        "decomposition": {
            "is_compound": False,
            "subquestions": ["There are 3738 melanoma trials; break them down by phase"],
        },
        "query_plan": _plan_payload(
            title=poisoned_title,
            interpretation="There are 3,738 matching trials, 1529 of them in PHASE2.",
            assumptions=["Assumed the 3,738 melanoma trials are the population.",
                         "Assumed phases are exclusive."],
        ),
        "entity_resolution": {"candidates": [{
            "field": "condition", "original": "melanoma",
            "alternatives": ["melanoma (3738 trials)"],
        }]},
        "plan_critique": {
            "approved": False,
            "problems": ["the plan omits the 3,738 unbucketed trials"],
            "suggested_dimension": None, "suggested_viz_type": None,
        },
    })
    ctgov = FakeCTGov({}, default=3738)
    graph = GraphPlanner(client=model, ctgov=ctgov)
    request = QueryRequest(query="How are melanoma trials distributed across phases?")

    plan = await graph.plan(request)

    assert not NUMERIC_RUN.search(plan.title), (
        f"a model-authored figure reached the chart title: {plan.title!r}"
    )
    for assumption in plan.assumptions:
        assert not NUMERIC_RUN.search(assumption), (
            f"a model-authored figure reached the assumptions: {assumption!r}"
        )

    # And end to end: the rendered response carries no number the API did not give.
    response = await execute(plan, request, ctgov)
    assert not NUMERIC_RUN.search(response.visualization.title)
    assert not NUMERIC_RUN.search(response.meta.interpretation)
    for assumption in response.meta.assumptions:
        assert not NUMERIC_RUN.search(assumption)
    # Every datum is the fake client's count, not the stub model's 1529.
    assert {d.count for d in response.visualization.data} == {3738}


async def test_years_the_caller_supplied_are_not_treated_as_fabrications():
    """The scrub must not be so blunt it strips a year the caller asked for."""
    model = StubModel({
        "decomposition": _no_split("melanoma trials per year 2018-2024"),
        "query_plan": _plan_payload(
            dimension="year", viz_type="time_series",
            title="Melanoma trials per year, 2018-2024",
            filters={"start_year": 2018, "end_year": 2024},
        ),
        "entity_resolution": {"candidates": []},
        "plan_critique": _approve(),
    })
    graph = GraphPlanner(client=model, ctgov=FakeCTGov({}, default=400))

    plan = await graph.plan(
        QueryRequest(query="melanoma trials per year 2018-2024",
                     start_year=2018, end_year=2024)
    )
    assert "2018" in plan.title and "2024" in plan.title


async def test_graph_plan_executes_to_measured_counts_only():
    """The end-to-end property: the graph plans, the executor measures."""
    counts = {
        "filter.advanced=AREA[Phase]PHASE2&query.cond=melanoma": 1529,
        "filter.advanced=AREA[Phase]PHASE3&query.cond=melanoma": 402,
        "query.cond=melanoma": 3738,
    }
    ctgov = FakeCTGov(counts, default=0)
    model = StubModel({
        "decomposition": _no_split("melanoma trials by phase"),
        "query_plan": _plan_payload(),
        "entity_resolution": {"candidates": []},
        "plan_critique": _approve(),
    })
    graph = GraphPlanner(client=model, ctgov=ctgov)
    request = QueryRequest(query="melanoma trials by phase", condition="melanoma")

    plan = await graph.plan(request)
    response = await execute(plan, request, ctgov)

    by_label = {d.label: d.count for d in response.visualization.data}
    assert by_label["PHASE2"] == 1529 and by_label["PHASE3"] == 402
    assert response.meta.total_matching == 3738
    assert response.meta.buckets_are_exclusive is False, "phase is multi-valued"


@pytest.mark.parametrize("query", [
    "How are melanoma trials distributed across phases?",
    "Show me a network of sponsors and drugs",
    "How many pembrolizumab trials are there?",
])
async def test_graph_always_returns_a_valid_plan(query: str):
    """Whatever the model says, the graph returns something the executor accepts."""
    model = StubModel({
        "decomposition": _no_split(query),
        "query_plan": _plan_payload(),
        "entity_resolution": {"candidates": []},
        "plan_critique": _approve(),
    })
    graph = GraphPlanner(client=model, ctgov=FakeCTGov({}, default=7))
    plan = await graph.plan(QueryRequest(query=query))
    assert plan.dimension in set(Dimension)
    assert len(plan.title) <= 120
