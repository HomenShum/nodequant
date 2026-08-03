"""Request and response schemas.

Every non-obvious field here exists because a reference renderer demanded it.
The rule that forced each one is cited inline as `design-dna rule-N`; the
evidence behind that rule is in design-dna/rules.yaml and
design-dna/observations/. If a field has no rule, it should not be here.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

# --------------------------------------------------------------------------
# Request
# --------------------------------------------------------------------------


class TrialPhase(str, Enum):
    """Phases exactly as ClinicalTrials.gov spells them.

    Mirroring the API's vocabulary rather than inventing prettier names means
    no lossy translation step, and an invalid phase is rejected by the schema
    instead of producing an empty result set at query time.
    """

    EARLY_PHASE1 = "EARLY_PHASE1"
    PHASE1 = "PHASE1"
    PHASE2 = "PHASE2"
    PHASE3 = "PHASE3"
    PHASE4 = "PHASE4"
    NA = "NA"


class TrialStatus(str, Enum):
    RECRUITING = "RECRUITING"
    NOT_YET_RECRUITING = "NOT_YET_RECRUITING"
    ACTIVE_NOT_RECRUITING = "ACTIVE_NOT_RECRUITING"
    COMPLETED = "COMPLETED"
    TERMINATED = "TERMINATED"
    WITHDRAWN = "WITHDRAWN"
    SUSPENDED = "SUSPENDED"
    UNKNOWN = "UNKNOWN"


class QueryRequest(BaseModel):
    """What a caller sends us.

    Only `query` is required. The optional fields are not a parallel query
    language — they are *constraints applied on top of* whatever the natural
    language asks for. When both disagree, the explicit field wins, because a
    caller who typed a field meant it more than the model's reading of prose.
    """

    query: Annotated[str, Field(min_length=3, max_length=500)]

    drug_name: str | None = Field(
        default=None,
        max_length=200,
        description="Intervention name, e.g. 'Pembrolizumab'. Applied as query.intr.",
    )
    condition: str | None = Field(
        default=None,
        max_length=200,
        description="Condition or disease, e.g. 'melanoma'. Applied as query.cond.",
    )
    sponsor: str | None = Field(
        default=None, max_length=200, description="Lead sponsor name. Applied as query.spons."
    )
    location: str | None = Field(
        default=None,
        max_length=200,
        description="Country or place name. Applied as query.locn.",
    )
    phase: list[TrialPhase] | None = Field(
        default=None, description="Restrict to these phases. Empty list is rejected."
    )
    status: list[TrialStatus] | None = Field(default=None, description="Restrict to these statuses.")
    start_year: int | None = Field(default=None, ge=1900, le=2100)
    end_year: int | None = Field(default=None, ge=1900, le=2100)

    tenant_id: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Caller identity used ONLY to partition the probe cache. Never sent "
            "upstream and never part of a query. Absent means the shared "
            "'anonymous' partition, which is correct for single-tenant use but "
            "means callers share cache timing with each other."
        ),
    )
    max_citations_per_datum: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Bound on citation fan-out. 0 disables citation fetching entirely.",
    )

    @model_validator(mode="after")
    def _check_coherent(self) -> "QueryRequest":
        if self.start_year and self.end_year and self.start_year > self.end_year:
            raise ValueError("start_year must not be after end_year")
        for name in ("phase", "status"):
            value = getattr(self, name)
            if value is not None and len(value) == 0:
                raise ValueError(f"{name} must be omitted or non-empty, not an empty list")
        return self


# --------------------------------------------------------------------------
# Response — visualization specification
# --------------------------------------------------------------------------


class VizType(str, Enum):
    BAR_CHART = "bar_chart"
    GROUPED_BAR_CHART = "grouped_bar_chart"
    TIME_SERIES = "time_series"
    HISTOGRAM = "histogram"
    SCATTER_PLOT = "scatter_plot"
    NETWORK_GRAPH = "network_graph"
    SINGLE_VALUE = "single_value"


class Citation(BaseModel):
    """A pointer from one visualized datum back to a real trial record.

    `excerpt` is copied verbatim out of the API response — never generated.
    `field` names where it came from, so a reader can see *why* this record
    supports this datum (design-dna rule-8).
    """

    nct_id: str = Field(pattern=r"^NCT\d{8}$")
    excerpt: str = Field(max_length=500)
    field: str = Field(description="API field the excerpt was taken from, e.g. 'briefTitle'.")


class Datum(BaseModel):
    """One mark: a bar, a time bucket, a point."""

    label: str
    count: int = Field(ge=0, description="Exact count from the API. Never model-computed.")

    # Present only for comparisons, where each label appears once per compared
    # entity. A renderer groups or colours by this.
    series: str | None = Field(
        default=None, description="Comparison group this datum belongs to, e.g. a drug name."
    )

    # design-dna rule-4: a bounded sample plus the true total, so a renderer can
    # honestly say "showing 3 of 41" instead of implying 3 is all there is.
    citations: list[Citation] = Field(default_factory=list)
    citation_total: int = Field(
        default=0, ge=0, description="Total records supporting this datum, not just those returned."
    )

    # design-dna rule-1: the unbucketed remainder is a first-class category,
    # rendered in neutral styling, never dropped.
    is_unbucketed: bool = Field(
        default=False,
        description="True for the residual 'no value recorded' category.",
    )

    @model_validator(mode="after")
    def _citation_total_at_least_returned(self) -> "Datum":
        if self.citation_total < len(self.citations):
            raise ValueError("citation_total cannot be smaller than the citations returned")
        return self


class GraphNode(BaseModel):
    """design-dna rule-5: magnitude lives on the node, not in render geometry."""

    id: str
    label: str
    type: str = Field(description="Entity kind, e.g. 'sponsor', 'drug', 'condition'.")
    count: int = Field(ge=0)


class GraphEdge(BaseModel):
    source: str
    target: str
    weight: int = Field(ge=0, description="Co-occurrence count. Explicit, never inferred.")
    citations: list[Citation] = Field(default_factory=list)
    citation_total: int = Field(default=0, ge=0)


class Encoding(BaseModel):
    """Field-to-channel mapping.

    Deliberately permissive in shape because a network graph and a bar chart
    do not share channels; deliberately strict in that every key must name a
    field that exists in `data`, which the executor asserts before returning.
    """

    x: dict[str, str] | None = None
    y: dict[str, str] | None = None
    series: dict[str, str] | None = None
    nodes: dict[str, str] | None = None
    edges: dict[str, str] | None = None


class Visualization(BaseModel):
    type: VizType
    title: str
    encoding: Encoding
    data: list[Datum] = Field(default_factory=list)
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)

    # design-dna rule-6: truncation must announce itself.
    returned: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _shape_matches_type(self) -> "Visualization":
        if self.type is VizType.NETWORK_GRAPH:
            if not self.nodes:
                raise ValueError("network_graph requires nodes")
            ids = {n.id for n in self.nodes}
            for edge in self.edges:
                if edge.source not in ids or edge.target not in ids:
                    raise ValueError(f"edge {edge.source}->{edge.target} references unknown node")
        elif not self.data:
            raise ValueError(f"{self.type.value} requires data")
        return self


class TraceStep(BaseModel):
    """design-dna rule-7: the literal request, so every number is replayable."""

    step: str
    url: str
    params: dict[str, str]
    result_count: int | None = None


class ResponseMeta(BaseModel):
    """Everything a renderer needs that is not a mark."""

    # design-dna rule-9: short-and-always-shown, kept separate from verbose-and-collapsed.
    interpretation: str = Field(description="One line: how we read the question.")
    trace: list[TraceStep] = Field(default_factory=list)

    filters_applied: dict[str, str] = Field(default_factory=dict)
    source: Literal["clinicaltrials.gov"] = "clinicaltrials.gov"

    # design-dna rule-2: a proportion without its denominator is unreadable.
    total_matching: int = Field(ge=0, description="Trials matching the base filters.")
    denominator_note: str = Field(description="Plain-language statement of what the total covers.")

    buckets_are_exclusive: bool = Field(
        description=(
            "False when a trial may appear in more than one bucket. Phase buckets are "
            "NOT exclusive: designModule.phases is an array, so a PHASE1/PHASE2 trial is "
            "counted in both. A renderer MUST NOT draw a pie or 100% stacked bar when this "
            "is False."
        )
    )
    bucket_sum: int | None = Field(
        default=None,
        description="Sum of bucket counts. Differs from total_matching when buckets overlap.",
    )
    unbucketed_count: int | None = Field(
        default=None, description="Records matching the filters but in no bucket."
    )

    sort: str | None = Field(default=None, description="e.g. 'count_desc', 'label_asc'.")
    units: str | None = Field(default=None, description="e.g. 'trials'.")
    time_granularity: str | None = Field(default=None, description="e.g. 'year'.")
    assumptions: list[str] = Field(
        default_factory=list, description="Anything inferred rather than stated by the caller."
    )
    warnings: list[str] = Field(
        default_factory=list, description="Conditions that should temper the reading."
    )


class QueryResponse(BaseModel):
    visualization: Visualization
    meta: ResponseMeta
