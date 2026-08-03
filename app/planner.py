"""Turn a natural-language question into a validated, executable QueryPlan.

The core constraint, and the reason this module is small:

    The model chooses WHICH questions to ask the API. It never answers them.

Every number in the final response comes from a ClinicalTrials.gov count. The
model's entire output surface is this plan — an enum-constrained, schema-validated
object. That is what makes the "hallucination-prone steps" concern tractable:
there is no step where a model is asked to count, sum, or estimate, so there is
no step where it can be wrong about a quantity.

Two layers of defence, because structured outputs alone are not enough:

1. `response_format: json_schema, strict: true` guarantees the SHAPE.
2. `QueryPlan` validation guarantees the SEMANTICS.

Layer 2 is not redundant. Observed during development: asked for a plan with an
`intervention` string field, the model returned a syntactically perfect object
whose `intervention` value was a 300-character essay about pembrolizumab's
development history. The schema was satisfied; the value was useless. Free-text
fields get length caps and are treated as untrusted; anything that can be an
enum is an enum.
"""

from __future__ import annotations

import json
import os
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from .models import QueryRequest, TrialPhase, TrialStatus, VizType

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")


class Dimension(str, Enum):
    """What we bucket the result set by.

    Closed set by design. A model that could invent a dimension could invent one
    the executor has no way to query, and the failure would surface as an empty
    chart rather than an error.
    """

    PHASE = "phase"
    STATUS = "status"
    YEAR = "year"
    COUNTRY = "country"
    SPONSOR = "sponsor"
    INTERVENTION_TYPE = "intervention_type"
    ENROLLMENT = "enrollment"          # binned trial size -> histogram
    ENROLLMENT_BY_PHASE = "enrollment_by_phase"  # size vs phase -> scatter
    SPONSOR_DRUG_NETWORK = "sponsor_drug_network"
    DRUG_DRUG_NETWORK = "drug_drug_network"
    NONE = "none"


class PlanFilters(BaseModel):
    """Filters the model extracted from the prose.

    All optional, all length-capped. These are merged with — and overridden by —
    any explicit structured fields the caller supplied.
    """

    drug_name: str | None = Field(default=None, max_length=100)
    condition: str | None = Field(default=None, max_length=100)
    sponsor: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=100)
    phase: list[TrialPhase] | None = None
    status: list[TrialStatus] | None = None
    start_year: int | None = Field(default=None, ge=1900, le=2100)
    end_year: int | None = Field(default=None, ge=1900, le=2100)

    @model_validator(mode="after")
    def _same_invariants_as_request(self) -> "PlanFilters":
        """Enforce here what QueryRequest enforces.

        These filters reach the executor by a different route than the caller's,
        so validating only on QueryRequest left a gap: an empty `phase=[]` from
        the model silently dropped the constraint, and a reversed year range
        produced zero buckets and then an uncaught ValueError at execution.
        """
        if self.start_year and self.end_year and self.start_year > self.end_year:
            raise ValueError("start_year must not be after end_year")
        for name in ("phase", "status"):
            value = getattr(self, name)
            if value is not None and len(value) == 0:
                raise ValueError(f"{name} must be omitted or non-empty, not an empty list")
        return self


class QueryPlan(BaseModel):
    """The model's complete output surface. Nothing else it says is used."""

    dimension: Dimension
    viz_type: VizType
    title: str = Field(max_length=120)
    interpretation: str = Field(max_length=300)
    filters: PlanFilters = Field(default_factory=PlanFilters)
    compare_values: list[str] | None = Field(
        default=None,
        max_length=4,
        description="For comparisons: the drugs/conditions to place side by side.",
    )
    compare_field: Literal["intervention", "condition", "sponsor"] | None = Field(
        default=None,
        description="Which field compare_values refer to. Required when comparing.",
    )
    assumptions: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def _viz_matches_dimension(self) -> "QueryPlan":
        """Reject plans whose chart type cannot express their dimension.

        Caught in practice: 'show a network of sponsors and drugs' planned as
        `bar_chart`. Shape-valid, semantically wrong, and it would have rendered
        an empty chart because the executor emits nodes/edges for network
        dimensions and `data` for the rest.
        """
        network_dims = {Dimension.SPONSOR_DRUG_NETWORK, Dimension.DRUG_DRUG_NETWORK}
        if self.dimension in network_dims and self.viz_type is not VizType.NETWORK_GRAPH:
            raise ValueError(f"dimension {self.dimension.value} requires viz_type network_graph")
        if self.viz_type is VizType.NETWORK_GRAPH and self.dimension not in network_dims:
            raise ValueError("network_graph requires a network dimension")
        if self.dimension is Dimension.ENROLLMENT and self.viz_type is not VizType.HISTOGRAM:
            raise ValueError("enrollment is a binned distribution and requires histogram")
        if self.dimension is Dimension.ENROLLMENT_BY_PHASE and self.viz_type is not VizType.SCATTER_PLOT:
            raise ValueError("enrollment_by_phase relates two measures and requires scatter_plot")
        if self.dimension is Dimension.YEAR and self.viz_type not in {
            VizType.TIME_SERIES,
            VizType.BAR_CHART,
            VizType.GROUPED_BAR_CHART,
        }:
            raise ValueError("year dimension requires time_series or a bar chart")
        if self.compare_values is not None and len(self.compare_values) < 2:
            raise ValueError("compare_values needs at least two values to compare")
        if self.compare_values and not self.compare_field:
            raise ValueError("compare_values requires compare_field")
        if self.compare_field and not self.compare_values:
            raise ValueError("compare_field requires compare_values")
        if self.compare_values and self.viz_type is not VizType.GROUPED_BAR_CHART:
            raise ValueError("comparisons must use viz_type grouped_bar_chart")
        # single_value renders ONE number; every other dimension emits buckets.
        # The pair was previously unguarded, so `dimension=sponsor` with
        # `viz_type=single_value` validated cleanly and produced a per-sponsor
        # breakdown labelled as a single total — an internally contradictory
        # response to "how many trials does Novartis sponsor?".
        if self.viz_type is VizType.SINGLE_VALUE and self.dimension is not Dimension.NONE:
            raise ValueError(
                f"viz_type single_value requires dimension none, got {self.dimension.value}"
            )
        if self.dimension is Dimension.NONE and self.viz_type is not VizType.SINGLE_VALUE:
            raise ValueError(
                f"dimension none produces one number and requires viz_type single_value, "
                f"got {self.viz_type.value}"
            )
        return self

    def merged_with_request(self, request: QueryRequest) -> "QueryPlan":
        """Explicit caller fields override anything inferred from prose.

        A caller who typed `drug_name` meant it more than the model's reading of
        their sentence did. Overrides are recorded as assumptions so the response
        can say what happened.
        """
        filters = self.filters.model_copy()
        notes = list(self.assumptions)

        for field_name in ("drug_name", "condition", "sponsor", "location", "phase", "status",
                           "start_year", "end_year"):
            explicit = getattr(request, field_name)
            if explicit is None:
                continue
            inferred = getattr(filters, field_name)
            if inferred is not None and inferred != explicit:
                notes.append(
                    f"Caller-supplied {field_name}={explicit!r} overrode {inferred!r} "
                    f"inferred from the question text."
                )
            setattr(filters, field_name, explicit)

        return self.model_copy(update={"filters": filters, "assumptions": notes[:8]})


PLAN_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["dimension", "viz_type", "title", "interpretation", "filters",
                 "compare_values", "compare_field", "assumptions"],
    "properties": {
        "dimension": {"type": "string", "enum": [d.value for d in Dimension]},
        "viz_type": {"type": "string", "enum": [v.value for v in VizType]},
        "title": {"type": "string", "description": "Chart title, under 120 chars."},
        "interpretation": {
            "type": "string",
            "description": "ONE short sentence describing how you read the question. "
                           "Not an answer, not an explanation of the subject matter.",
        },
        "filters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["drug_name", "condition", "sponsor", "location", "phase",
                         "status", "start_year", "end_year"],
            "properties": {
                "drug_name": {"type": ["string", "null"],
                              "description": "Drug/intervention NAME ONLY, e.g. 'Pembrolizumab'."},
                "condition": {"type": ["string", "null"],
                              "description": "Condition NAME ONLY, e.g. 'melanoma'."},
                "sponsor": {"type": ["string", "null"], "description": "Sponsor NAME ONLY."},
                "location": {"type": ["string", "null"], "description": "Country or city NAME ONLY."},
                "phase": {"type": ["array", "null"],
                          "items": {"type": "string", "enum": [p.value for p in TrialPhase]}},
                "status": {"type": ["array", "null"],
                           "items": {"type": "string", "enum": [s.value for s in TrialStatus]}},
                "start_year": {"type": ["integer", "null"]},
                "end_year": {"type": ["integer", "null"]},
            },
        },
        "compare_values": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": "Two to four drug or condition names to compare side by side.",
        },
        "compare_field": {
            "type": ["string", "null"],
            "enum": ["intervention", "condition", "sponsor", None],
            "description": "Which field compare_values name. Required when comparing.",
        },
        "assumptions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Anything you inferred that the user did not state.",
        },
    },
}

SYSTEM_PROMPT = """You plan queries against the ClinicalTrials.gov API. You do not answer questions.

You will receive a question about clinical trials. Produce a plan describing WHICH data to \
retrieve and HOW to visualise it. You will never be asked for counts, totals, or findings — \
those are measured from the API after your plan runs.

Rules:
- Name entities only. `drug_name` is "Pembrolizumab", never a description of pembrolizumab.
- `interpretation` is ONE sentence about how you read the question. It is not an answer and \
not background on the subject.
- Pick `dimension` from the enum. Use `none` when the question wants a single total.
- A question that opens with "how many", "count", "total number of", or "how many \
trials are there" wants ONE number: use dimension `none` and viz_type `single_value`. \
Do NOT break it into a breakdown because a breakdown seems more informative. Only \
choose a bucketed dimension if the question also names a grouping, e.g. "how many per \
year" (year) or "how many by phase" (phase).
- dimension `none` requires viz_type `single_value`, and `single_value` requires \
dimension `none`. Never pair `single_value` with a bucketed dimension.
- Network dimensions (sponsor_drug_network, drug_drug_network) require viz_type network_graph, \
and network_graph requires a network dimension.
- Use `year` + time_series for "over time", "per year", "since YEAR", "changed".
- Use `enrollment` + histogram for questions about trial SIZE distribution: "how \nlarge are", "enrollment distribution", "how many participants", "size of trials".
- Use `enrollment_by_phase` + scatter_plot when a question relates trial size TO \nphase: "does enrollment vary by phase", "size versus phase".
- Use `compare_values` only for explicit A-vs-B comparisons, always with
`compare_field` naming what they are and viz_type grouped_bar_chart.
- Record anything you inferred in `assumptions`.
- If the question names no drug, condition, or sponsor, leave those null rather than guessing."""


class PlannerError(RuntimeError):
    """Raised when no valid plan could be produced."""


class Planner:
    """Wraps the model call. Deliberately thin and provider-swappable."""

    def __init__(self, client: Any = None, model: str = DEFAULT_MODEL):
        self._client = client
        self._model = model

    def _get_client(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI  # imported lazily so tests need no key

            self._client = AsyncOpenAI()
        return self._client

    async def plan(self, request: QueryRequest, max_attempts: int = 2) -> QueryPlan:
        """Produce a validated plan, retrying once with the validation error fed back.

        The retry matters: a rejected plan carries a precise reason, and handing
        that reason back fixes most first-attempt semantic failures without a
        second round of prompt engineering.
        """
        client = self._get_client()
        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": request.query},
        ]

        last_error: str | None = None
        for attempt in range(max_attempts):
            completion = await client.chat.completions.create(
                model=self._model,
                messages=messages,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "query_plan", "strict": True,
                                    "schema": PLAN_JSON_SCHEMA},
                },
            )
            raw = completion.choices[0].message.content or "{}"
            try:
                plan = QueryPlan.model_validate_json(raw)
            except ValidationError as exc:
                last_error = str(exc)
                if attempt + 1 >= max_attempts:
                    break
                messages.extend([
                    {"role": "assistant", "content": raw},
                    {"role": "user",
                     "content": f"That plan was rejected:\n{last_error}\nReturn a corrected plan."},
                ])
                continue
            return plan.merged_with_request(request)

        raise PlannerError(f"no valid plan after {max_attempts} attempts: {last_error}")


def fallback_plan(request: QueryRequest) -> QueryPlan:
    """Deterministic keyword planner, used when no LLM is reachable.

    Not a toy: it keeps the service testable with no credentials and degrades
    the product to "still correct, less flexible" rather than "down". Every
    number it produces is measured the same way as the LLM path's.
    """
    text = request.query.lower()

    # Counting lead-ins are checked FIRST, before any grouping keyword.
    # Ordering matters: "How many trials does Novartis sponsor?" contains the
    # word "sponsor" and was being routed to a per-sponsor breakdown, answering
    # a question the user did not ask. A grouping only wins if it is explicitly
    # requested ("how many per year"), which the second test below covers.
    counting = any(text.startswith(w) or f" {w}" in text
                   for w in ("how many", "count the", "count all",
                             "total number", "how many trials are there"))
    grouping = any(w in text for w in (" per ", " by ", "each year", "across",
                                       "distributed", "breakdown", "grouped"))
    if counting and not grouping:
        subject = request.drug_name or request.condition or "matching trials"
        return QueryPlan(
            dimension=Dimension.NONE, viz_type=VizType.SINGLE_VALUE,
            title=f"Total trials for {subject}",
            interpretation="Keyword planner: single total (no LLM available).",
            filters=PlanFilters(),
            assumptions=["Plan produced by the deterministic fallback planner, not an LLM."],
        ).merged_with_request(request)

    if any(w in text for w in ("network", "co-occur", "combination", "relationship")):
        dimension, viz = Dimension.SPONSOR_DRUG_NETWORK, VizType.NETWORK_GRAPH
        if "drug" in text and "sponsor" not in text:
            dimension = Dimension.DRUG_DRUG_NETWORK
    elif any(w in text for w in ("over time", "per year", "each year", "since", "trend", "changed")):
        dimension, viz = Dimension.YEAR, VizType.TIME_SERIES
    elif any(w in text for w in ("country", "countries", "where", "geographic")):
        dimension, viz = Dimension.COUNTRY, VizType.BAR_CHART
    elif "sponsor" in text:
        dimension, viz = Dimension.SPONSOR, VizType.BAR_CHART
    elif "status" in text or "recruiting" in text:
        dimension, viz = Dimension.STATUS, VizType.BAR_CHART
    elif any(w in text for w in ("intervention", "treatment type")):
        dimension, viz = Dimension.INTERVENTION_TYPE, VizType.BAR_CHART
    elif any(w in text for w in ("enrollment", "participants", "how large", "size of", "sizes")):
        if "phase" in text:
            dimension, viz = Dimension.ENROLLMENT_BY_PHASE, VizType.SCATTER_PLOT
        else:
            dimension, viz = Dimension.ENROLLMENT, VizType.HISTOGRAM
    elif "phase" in text:
        dimension, viz = Dimension.PHASE, VizType.BAR_CHART
    elif any(w in text for w in ("how many", "count", "total", "number of")):
        dimension, viz = Dimension.NONE, VizType.SINGLE_VALUE
    else:
        dimension, viz = Dimension.PHASE, VizType.BAR_CHART

    subject = request.drug_name or request.condition or "matching trials"
    return QueryPlan(
        dimension=dimension,
        viz_type=viz,
        title=f"{subject} by {dimension.value.replace('_', ' ')}"
        if dimension is not Dimension.NONE
        else f"Total trials for {subject}",
        interpretation=f"Keyword planner: grouping by {dimension.value} (no LLM available).",
        filters=PlanFilters(),
        assumptions=["Plan produced by the deterministic fallback planner, not an LLM."],
    ).merged_with_request(request)
