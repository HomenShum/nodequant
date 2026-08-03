"""A LangGraph planning agent — an alternative to the single structured-output call.

`Planner.plan()` is one model call. That is the right default: it is fast,
cheap, and the plan schema already makes most failure modes unrepresentable.
But three things a single call cannot do come up in real questions:

  * **Compound questions.** "Phases and countries for melanoma" is two
    distributions. One `QueryPlan` has one `dimension`, so a single call must
    silently drop half the question.
  * **Entity resolution.** "Keytruda" and "Pembrolizumab" are the same drug
    and do not return the same count. Deciding between them from model
    knowledge is exactly the kind of unverified assertion this system exists
    to avoid — so the graph *probes* both and lets the API decide.
  * **Zero-result recovery.** A plan that matches nothing is usually a wrong
    filter, not an empty world. A single call has no chance to notice.

The graph adds those, and nothing else. In particular:

    THE GRAPH DOES NOT GAIN THE ABILITY TO PRODUCE NUMBERS.

Every model call in this module returns a schema whose fields are booleans,
enums, and short entity names. There is no integer field anywhere in
`DECOMPOSE_SCHEMA`, `RESOLVE_SCHEMA`, or `CRITIQUE_SCHEMA`, so there is no
slot a count could occupy. The graph *does* issue count probes, but only as
control flow — "is this filter non-empty", "which spelling matches more
records" — and the integers never enter the plan. The plan it emits is the
same `QueryPlan` the single-call planner emits, and the executor measures it
the same way. `tests/test_eval_agent.py` pins this: a stub model that tries to
smuggle a figure into every free-text field it owns must not get one into the
returned plan.

Usage — same interface as `Planner`, so `app/main.py` can swap on an env var:

    planner = GraphPlanner() if os.environ.get("PLANNER") == "graph" else Planner()
    plan = await planner.plan(request)

`plan()` returns the primary plan. `plan_multi()` returns every sub-plan a
compound question produced, for a caller that wants to render both panels.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .ctgov import CTGovClient, Filter
from .models import QueryRequest, VizType
from .planner import (
    DEFAULT_MODEL,
    Dimension,
    Planner,
    PlannerError,
    QueryPlan,
    fallback_plan,
)

# Bounds. Every loop in this graph is finite by construction: a planning agent
# that can retry indefinitely is a latency and cost incident waiting to happen,
# and an unbounded revise loop hides the failure it should be reporting.
MAX_SUBQUESTIONS = 2
MAX_REVISIONS = 2

_NUMERIC_RUN = re.compile(r"\d[\d,]{2,}")

# The chart type each dimension defaults to, used when a critique asks for a
# dimension change without naming a compatible chart. Derived from the same
# constraints `QueryPlan._viz_matches_dimension` enforces, so a repair can
# never produce a plan the schema would reject.
_DEFAULT_VIZ: dict[Dimension, VizType] = {
    Dimension.PHASE: VizType.BAR_CHART,
    Dimension.STATUS: VizType.BAR_CHART,
    Dimension.COUNTRY: VizType.BAR_CHART,
    Dimension.SPONSOR: VizType.BAR_CHART,
    Dimension.INTERVENTION_TYPE: VizType.BAR_CHART,
    Dimension.YEAR: VizType.TIME_SERIES,
    Dimension.SPONSOR_DRUG_NETWORK: VizType.NETWORK_GRAPH,
    Dimension.DRUG_DRUG_NETWORK: VizType.NETWORK_GRAPH,
    Dimension.NONE: VizType.SINGLE_VALUE,
}

# Which API parameter each resolvable filter field maps to. Resolution probes
# use these directly; there is deliberately no path here that guesses.
_RESOLVABLE = {
    "drug_name": "query.intr",
    "condition": "query.cond",
    "sponsor": "query.spons",
}


# --------------------------------------------------------------------------
# Model-facing schemas. Note the absence of any integer field.
# --------------------------------------------------------------------------

DECOMPOSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["is_compound", "subquestions"],
    "properties": {
        "is_compound": {
            "type": "boolean",
            "description": "True only when the question asks for two genuinely "
                           "different breakdowns, e.g. 'phases AND countries'.",
        },
        "subquestions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "One self-contained question per breakdown requested. "
                           "Return the original question unchanged when it is not "
                           "compound. Never more than two.",
        },
    },
}

RESOLVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["candidates"],
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field", "original", "alternatives"],
                "properties": {
                    "field": {"type": "string",
                              "enum": ["drug_name", "condition", "sponsor"]},
                    "original": {"type": "string"},
                    "alternatives": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Other NAMES for the same entity — generic name "
                                       "for a brand, official spelling for an abbreviation. "
                                       "Names only. No descriptions, no counts.",
                    },
                },
            },
        }
    },
}

CRITIQUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["approved", "problems", "suggested_dimension", "suggested_viz_type"],
    "properties": {
        "approved": {"type": "boolean"},
        "problems": {
            "type": "array",
            "items": {"type": "string"},
            "description": "What the plan gets wrong about the question. Empty when approved.",
        },
        "suggested_dimension": {
            "type": ["string", "null"],
            "enum": [d.value for d in Dimension] + [None],
        },
        "suggested_viz_type": {
            "type": ["string", "null"],
            "enum": [v.value for v in VizType] + [None],
        },
    },
}

DECOMPOSE_PROMPT = """You split questions about clinical trials into the separate \
breakdowns they ask for. You never answer them.

A question is compound only when it asks for two DIFFERENT groupings of the data — \
"phases and countries for melanoma trials" is two. "Recruiting melanoma trials in \
Germany" is one question with two filters, not two questions. A comparison \
("Pembrolizumab vs Nivolumab") is ONE question; the plan schema handles comparisons \
directly, so do not split it.

Return at most two subquestions. Each must stand alone: carry every filter from the \
original into each one."""

RESOLVE_PROMPT = """You list alternative NAMES for entities in a clinical-trials query. \
You do not decide which is correct — each candidate is checked against the \
ClinicalTrials.gov API afterwards, and the API decides.

For each drug, condition or sponsor named, give other names for the SAME entity: the \
generic name for a brand ("Keytruda" -> "Pembrolizumab"), the expansion of an \
abbreviation ("NSCLC" -> "Non-Small Cell Lung Cancer"), a common alternative spelling. \
Names only — never a description, never a number, never a different entity that is \
merely similar. Return an empty alternatives list when the name is already the \
canonical one."""

CRITIQUE_PROMPT = """You review a query plan against the question it came from. You \
never see data and never state findings.

Check only this:
- Does `dimension` match what the question asks to group by?
- Does `viz_type` suit that dimension? (year -> time_series; networks -> network_graph; \
a single total -> single_value; a comparison -> grouped_bar_chart.)
- Do the filters capture every constraint the question states, and none it does not?

Approve unless something is actually wrong. When you reject, name the problems and \
suggest the dimension and viz_type you would use instead."""


class AgentState(TypedDict, total=False):
    request: QueryRequest
    subquestions: list[str]
    index: int
    revisions: int
    current: QueryPlan | None
    plans: list[QueryPlan]
    feedback: list[str]
    notes: list[str]
    log: list[dict[str, Any]]
    #: The critic's proposed dimension/viz, consumed by the revise node.
    suggestion: dict[str, Any]


class GraphPlanner:
    """Multi-step planner. Same `plan(request) -> QueryPlan` contract as `Planner`.

    `client` is an OpenAI-shaped async client; `ctgov` is a `CTGovClient`.
    Both are injectable so the graph runs offline in tests with no network and
    no key.
    """

    def __init__(
        self,
        client: Any = None,
        model: str = DEFAULT_MODEL,
        ctgov: CTGovClient | None = None,
        max_revisions: int = MAX_REVISIONS,
    ):
        self._client = client
        self._model = model
        self._ctgov = ctgov
        self._owns_ctgov = ctgov is None
        self._max_revisions = max_revisions
        self._planner = Planner(client=client, model=model)
        self._graph = self._build()

    # ---------------------------------------------------------------- infra

    def _get_client(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI()
            self._planner = Planner(client=self._client, model=self._model)
        return self._client

    async def _structured(
        self, name: str, schema: dict[str, Any], system: str, user: str
    ) -> dict[str, Any]:
        """One strict-schema model call. Returns {} on any failure.

        Failing soft is deliberate: every node that calls this is an
        *enhancement* over the single-call planner. If decomposition or
        critique is unavailable, the graph should degrade to the behaviour of
        the planner it wraps, not fail the request.
        """
        client = self._get_client()
        try:
            completion = await client.chat.completions.create(
                model=self._model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": name, "strict": True, "schema": schema},
                },
            )
            payload = json.loads(completion.choices[0].message.content or "{}")
            return payload if isinstance(payload, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    async def _count(self, params: dict[str, str]) -> int:
        """A count probe used purely as control flow.

        The integer returned here decides which branch runs and which spelling
        of an entity survives. It is never written into the plan, never
        rendered, and never shown to a model.
        """
        if self._ctgov is None:
            self._ctgov = CTGovClient()
            await self._ctgov.__aenter__()
        return await self._ctgov.count(Filter(params))

    # ---------------------------------------------------------------- nodes

    async def _decompose(self, state: AgentState) -> dict[str, Any]:
        question = state["request"].query
        payload = await self._structured(
            "decomposition", DECOMPOSE_SCHEMA, DECOMPOSE_PROMPT, question
        )
        subs = [s.strip() for s in payload.get("subquestions") or [] if isinstance(s, str)]
        subs = [s for s in subs if len(s) >= 3][:MAX_SUBQUESTIONS]
        if not payload.get("is_compound") or len(subs) < 2:
            subs = [question]
        notes = list(state.get("notes", []))
        if len(subs) > 1:
            notes.append(
                "Compound question: split into separate breakdowns; the primary one "
                "is planned and executed."
            )
        return {
            "subquestions": subs, "index": 0, "revisions": 0, "plans": [],
            "feedback": [], "notes": notes,
            "log": [{"node": "decompose", "subquestions": subs}],
        }

    async def _plan_sub(self, state: AgentState) -> dict[str, Any]:
        """Plan the current sub-question with the existing structured planner.

        Reusing `Planner` rather than reimplementing planning is the point: the
        graph is orchestration around the same enum-constrained schema, so it
        inherits every validation guarantee instead of opening a second path
        that could drift from it.
        """
        request = state["request"]
        question = state["subquestions"][state["index"]]
        sub_request = request.model_copy(update={"query": question})
        try:
            plan = await self._planner.plan(sub_request)
        except (PlannerError, Exception):  # noqa: BLE001
            plan = fallback_plan(sub_request)
        return {
            "current": plan, "revisions": 0, "feedback": [],
            "log": state.get("log", []) + [
                {"node": "plan", "question": question,
                 "dimension": plan.dimension.value, "viz": plan.viz_type.value}
            ],
        }

    async def _resolve(self, state: AgentState) -> dict[str, Any]:
        """Disambiguate entity names, deciding by probe rather than by belief.

        The model proposes alternative spellings; it does not choose. Each
        candidate is counted against the API under the same parameter the
        executor would use, and the name that actually matches more records
        wins. A model that "knows" Keytruda is Pembrolizumab still cannot put
        that claim into the plan unless the registry agrees.
        """
        plan = state["current"]
        assert plan is not None
        named = {
            field: value
            for field, value in (
                (f, getattr(plan.filters, f)) for f in _RESOLVABLE
            )
            if value
        }
        if not named:
            return {}

        payload = await self._structured(
            "entity_resolution", RESOLVE_SCHEMA, RESOLVE_PROMPT,
            json.dumps({"question": state["subquestions"][state["index"]],
                        "entities": named}),
        )
        candidates = payload.get("candidates") or []

        updates: dict[str, str] = {}
        notes = list(state.get("notes", []))
        log_entries: list[dict[str, Any]] = []
        for entry in candidates:
            if not isinstance(entry, dict):
                continue
            field = entry.get("field")
            original = named.get(field)
            if not original or field not in _RESOLVABLE:
                continue
            alternatives = [
                a.strip() for a in (entry.get("alternatives") or [])
                if isinstance(a, str) and a.strip() and a.strip().lower() != original.lower()
            ][:3]
            if not alternatives:
                continue

            param = _RESOLVABLE[field]
            try:
                scores = {original: await self._count({param: original})}
                for alternative in alternatives:
                    scores[alternative] = await self._count({param: alternative})
            except Exception:  # noqa: BLE001
                # Probing failed; keep the model's original reading rather than
                # acting on an unverified resolution.
                log_entries.append({"node": "resolve", "field": field, "verified": False})
                continue

            best = max(scores, key=lambda name: scores[name])
            log_entries.append({
                "node": "resolve", "field": field, "original": original,
                "candidates": alternatives, "chose": best, "verified": True,
            })
            if best != original and scores[best] > scores[original]:
                updates[field] = best
                notes.append(
                    f"Resolved {field} {original!r} to {best!r}; both were counted "
                    f"against ClinicalTrials.gov and the latter matched more records."
                )

        if not updates:
            return {"log": state.get("log", []) + log_entries}

        filters = plan.filters.model_copy(update=updates)
        return {
            "current": plan.model_copy(update={"filters": filters}),
            "notes": notes,
            "log": state.get("log", []) + log_entries,
        }

    async def _critique(self, state: AgentState) -> dict[str, Any]:
        plan = state["current"]
        assert plan is not None
        payload = await self._structured(
            "plan_critique", CRITIQUE_SCHEMA, CRITIQUE_PROMPT,
            json.dumps({
                "question": state["subquestions"][state["index"]],
                "plan": {
                    "dimension": plan.dimension.value,
                    "viz_type": plan.viz_type.value,
                    "filters": plan.filters.model_dump(mode="json", exclude_none=True),
                    "compare_values": plan.compare_values,
                    "compare_field": plan.compare_field,
                },
            }),
        )
        # An unavailable critic approves: see `_structured`.
        approved = payload.get("approved", True)
        problems = [p for p in (payload.get("problems") or []) if isinstance(p, str)][:5]
        return {
            "feedback": [] if approved else problems or ["the critic rejected the plan"],
            "log": state.get("log", []) + [{
                "node": "critique", "approved": bool(approved), "problems": problems,
                "suggested_dimension": payload.get("suggested_dimension"),
                "suggested_viz_type": payload.get("suggested_viz_type"),
            }],
            "suggestion": {
                "dimension": payload.get("suggested_dimension"),
                "viz_type": payload.get("suggested_viz_type"),
            },
        }

    async def _probe(self, state: AgentState) -> dict[str, Any]:
        """Does this plan match anything at all?

        A zero-result plan is almost always a wrong filter — a misspelled drug,
        a condition the registry indexes under another name, a year window
        before the trials existed. Worth one bounded retry before returning an
        empty chart. The count itself goes nowhere near the plan.
        """
        plan = state["current"]
        assert plan is not None
        from .executor import base_filter  # local import: avoids an import cycle

        try:
            total = await self._count(base_filter(plan).params)
        except Exception:  # noqa: BLE001
            return {"log": state.get("log", []) + [{"node": "probe", "reached": False}]}

        entry = {"node": "probe", "reached": True, "empty": total == 0}
        if total > 0:
            return {"log": state.get("log", []) + [entry]}
        return {
            "feedback": [
                "This plan's filters match no trials at all. Broaden or correct them — "
                "drop the least certain filter, or use a more standard name for the "
                "condition or drug."
            ],
            "log": state.get("log", []) + [entry],
        }

    async def _revise(self, state: AgentState) -> dict[str, Any]:
        """Re-plan with the objection fed back, then repair deterministically.

        Two layers, for the same reason the base planner has two: the model is
        asked for a better plan, and whatever it returns is then forced into a
        schema-legal shape. A critique that says "use year" must not be able to
        produce `dimension=year, viz_type=network_graph`.
        """
        plan = state["current"]
        assert plan is not None
        question = state["subquestions"][state["index"]]
        objections = "; ".join(state.get("feedback") or [])
        request = state["request"].model_copy(
            update={"query": f"{question}\n\nA previous plan was rejected: {objections}"}
        )
        try:
            revised = await self._planner.plan(request)
        except Exception:  # noqa: BLE001
            revised = plan

        suggestion = state.get("suggestion") or {}
        revised = self._repair(revised, suggestion)
        return {
            "current": revised,
            "revisions": state.get("revisions", 0) + 1,
            "feedback": [],
            "notes": list(state.get("notes", [])) + [
                "Plan revised after review; the first attempt was rejected."
            ],
            "log": state.get("log", []) + [{
                "node": "revise", "attempt": state.get("revisions", 0) + 1,
                "dimension": revised.dimension.value, "viz": revised.viz_type.value,
            }],
        }

    def _repair(self, plan: QueryPlan, suggestion: dict[str, Any]) -> QueryPlan:
        """Apply a critique's suggested dimension with a chart type that is legal for it."""
        wanted = suggestion.get("dimension")
        if not wanted or wanted == plan.dimension.value:
            return plan
        try:
            dimension = Dimension(wanted)
        except ValueError:
            return plan
        viz = _DEFAULT_VIZ[dimension]
        try:
            return QueryPlan(
                dimension=dimension, viz_type=viz, title=plan.title,
                interpretation=plan.interpretation, filters=plan.filters,
                compare_values=plan.compare_values if viz is VizType.GROUPED_BAR_CHART else None,
                compare_field=plan.compare_field if viz is VizType.GROUPED_BAR_CHART else None,
                assumptions=plan.assumptions,
            )
        except ValueError:
            return plan

    async def _advance(self, state: AgentState) -> dict[str, Any]:
        plan = state["current"]
        assert plan is not None
        return {
            "plans": list(state.get("plans", [])) + [plan],
            "index": state["index"] + 1,
            "revisions": 0,
            "feedback": [],
        }

    # --------------------------------------------------------------- routing

    def _after_critique(self, state: AgentState) -> str:
        if state.get("feedback") and state.get("revisions", 0) < self._max_revisions:
            return "revise"
        return "probe"

    def _after_probe(self, state: AgentState) -> str:
        if state.get("feedback") and state.get("revisions", 0) < self._max_revisions:
            return "revise"
        return "advance"

    def _after_advance(self, state: AgentState) -> str:
        return "plan" if state["index"] < len(state["subquestions"]) else END

    def _build(self):
        builder = StateGraph(AgentState)
        builder.add_node("decompose", self._decompose)
        builder.add_node("plan", self._plan_sub)
        builder.add_node("resolve", self._resolve)
        builder.add_node("critique", self._critique)
        builder.add_node("revise", self._revise)
        builder.add_node("probe", self._probe)
        builder.add_node("advance", self._advance)

        builder.add_edge(START, "decompose")
        builder.add_edge("decompose", "plan")
        builder.add_edge("plan", "resolve")
        builder.add_edge("resolve", "critique")
        builder.add_conditional_edges(
            "critique", self._after_critique, {"revise": "revise", "probe": "probe"}
        )
        # A revision goes back through resolution: a re-planned filter is a new
        # entity that has not been verified against the API yet.
        builder.add_edge("revise", "resolve")
        builder.add_conditional_edges(
            "probe", self._after_probe, {"revise": "revise", "advance": "advance"}
        )
        builder.add_conditional_edges(
            "advance", self._after_advance, {"plan": "plan", END: END}
        )
        return builder.compile()

    # ------------------------------------------------------------ scrubbing

    @staticmethod
    def _allowed_numbers(plan: QueryPlan) -> set[str]:
        return {str(y) for y in (plan.filters.start_year, plan.filters.end_year) if y}

    @classmethod
    def _scrub(cls, plan: QueryPlan, notes: list[str]) -> QueryPlan:
        """Strip any figure the graph's extra model calls could have introduced.

        `title` matters most: it is the one free-text model output that reaches
        the rendered response verbatim (`executor` composes `interpretation`
        itself and drops numeric `assumptions`). A multi-step graph makes more
        model calls than a single one, so it has more chances to write a number
        somewhere — this closes that off at the boundary rather than trusting
        each node.
        """
        allowed = cls._allowed_numbers(plan)

        def clean(text: str) -> bool:
            return not {m.group().replace(",", "") for m in _NUMERIC_RUN.finditer(text)} - allowed

        title = plan.title
        if not clean(title):
            subject = (plan.filters.drug_name or plan.filters.condition
                       or plan.filters.sponsor or "matching trials")
            title = f"{subject} by {plan.dimension.value.replace('_', ' ')}"[:120]

        assumptions = [a for a in list(plan.assumptions) + notes if clean(a)][:8]
        return plan.model_copy(update={"title": title, "assumptions": assumptions})

    # ------------------------------------------------------------ interface

    async def plan_multi(self, request: QueryRequest) -> list[QueryPlan]:
        """Every plan the question decomposed into, primary first."""
        state: AgentState = {"request": request}
        final = await self._graph.ainvoke(state)
        plans = final.get("plans") or []
        if not plans:
            current = final.get("current")
            plans = [current] if current is not None else [fallback_plan(request)]
        notes = list(final.get("notes") or [])
        if len(plans) > 1:
            notes.append(
                "This question asked for more than one breakdown; the additional "
                f"breakdown ({plans[1].dimension.value}) was planned but not executed."
            )
        merged = [p.merged_with_request(request) for p in plans]
        return [self._scrub(p, notes if i == 0 else []) for i, p in enumerate(merged)]

    async def plan(self, request: QueryRequest) -> QueryPlan:
        """Drop-in replacement for `Planner.plan`."""
        return (await self.plan_multi(request))[0]

    async def aclose(self) -> None:
        if self._owns_ctgov and self._ctgov is not None:
            await self._ctgov.__aexit__(None, None, None)
            self._ctgov = None


def make_planner(client: Any = None, ctgov: CTGovClient | None = None) -> Any:
    """Select the planner from the environment.

    `PLANNER=graph` opts into the agent graph. Anything else keeps the
    single-call planner, which stays the default: it is faster, and the graph's
    extra abilities only pay for themselves on questions that need them.
    """
    if os.environ.get("PLANNER", "").strip().lower() == "graph":
        return GraphPlanner(client=client, ctgov=ctgov)
    return Planner(client=client)
