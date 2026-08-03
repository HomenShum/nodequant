"""stdio MCP server for TrialScope.

Every tool description here is written for an agent, not for a human skimming
a README. That means each one states, in order:

  * what it returns, concretely, with field names;
  * what it costs — network calls and wall-clock latency — because a
    ClinicalTrials.gov query is 12-40s and an agent that does not budget for
    that will time out or fan out badly;
  * how it fails, and what the agent should do about it.

Tools return markdown text by default rather than raw JSON, because the point
of this server is that an agent can run a query and immediately *show a human
the chart*. The JSON is always available in the same payload, so nothing is
lost.

Run it:  uv run python -m app.mcp_server
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from .ctgov import CTGovClient, CTGovError, Filter
from .models import QueryRequest, QueryResponse, TrialPhase, TrialStatus
from .planner import Planner, PlannerError, fallback_plan
from .neuromap import render_neuromap
from .render import render_ascii, render_markdown, render_svg, showing_notes

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"

INSTRUCTIONS = """\
TrialScope turns a natural-language question about clinical trials into a
validated visualization specification backed by exact ClinicalTrials.gov counts.

The invariant that makes this trustworthy: the language model plans which
questions to ask the API; it never answers them. Every count in every response
is a ClinicalTrials.gov `totalCount` probe, recorded in `meta.trace` with the
literal URL and params, and re-verifiable with `ctgov_replay`.

Before rendering anything yourself, read `meta.buckets_are_exclusive`. When it
is false the buckets overlap (a trial can hold several phases) and a pie chart
or 100% stacked bar is WRONG. `ctgov_render` already enforces this; if you draw
your own chart, you must too.

Budget: `ctgov_query` takes 12-40 seconds and issues 12-57 upstream requests.
Do not fan out more than one or two at a time. `ctgov_schema`,
`ctgov_examples` and `ctgov_render` are offline and instant — develop against
`ctgov_examples` before spending a network query.
"""

server = MCPServer(
    name="trialscope",
    title="TrialScope — ClinicalTrials.gov query to visualization",
    version="0.1.0",
    instructions=INSTRUCTIONS,
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
READ_ONLY_NET = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)


_SHARED: dict[str, Any] = {}
_SHARED_LOCK = asyncio.Lock()


async def _client(tenant: str | None = None) -> CTGovClient:
    """One client for the whole process, scoped per tenant on read.

    Every tool here used to open `CTGovClient()` per call. That is not a cache
    leak -- a per-call client has a private cache, so nothing crosses between
    callers -- but it is worse in a way that is easier to miss: each call also
    got its own PACING and its own token bucket, so N concurrent agent calls
    each believed they had the full upstream budget. Against a rate-limited API
    that is exactly how you earn a 429 storm, and the MCP surface is a first
    class production path here, not a side door.

    Sharing the client restores the property the HTTP path already has: quota
    and pacing are pooled because they reveal volume, caches are partitioned
    because they reveal intent. See app/tenancy.py for why that split is the
    whole design.
    """
    async with _SHARED_LOCK:
        client = _SHARED.get("client")
        if client is None:
            from .tenancy import TenantRegistry

            client = CTGovClient(tenants=TenantRegistry())
            await client.__aenter__()
            _SHARED["client"] = client
    return client.for_tenant(tenant)


def _load_response(payload: str | dict[str, Any]) -> QueryResponse:
    raw = json.loads(payload) if isinstance(payload, str) else payload
    if isinstance(raw, dict) and "response" in raw and "visualization" not in raw:
        raw = raw["response"]
    return QueryResponse.model_validate(raw)


def _fail(message: str, remedy: str) -> dict[str, Any]:
    """Errors are data, not exceptions. An agent can branch on `ok`."""
    return {"ok": False, "error": message, "remedy": remedy}


# ---------------------------------------------------------------------------


@server.tool(
    name="ctgov_query",
    title="Query ClinicalTrials.gov and get a chart",
    annotations=READ_ONLY_NET,
    description="""\
Ask a natural-language question about clinical trials. Returns a validated
visualization spec plus a ready-to-show chart.

RETURNS a JSON object:
  markdown   a complete markdown block: title, denominator line, overlap
             warning when applicable, a data table, and the chart itself
             inline as a base64 data-URI SVG. Show this to the human verbatim.
  ascii      the same chart as terminal text, for logs and transcripts.
  response   the full QueryResponse. `visualization.data[]` is
             {label,count,series,is_unbucketed,citations[],citation_total};
             `meta` carries interpretation, total_matching, denominator_note,
             buckets_are_exclusive, bucket_sum, unbucketed_count, warnings and
             a replayable `trace`.
  stats      {ms, ctgov_requests, ctgov_retries, planner} — what it actually cost.

COST: 12-57 requests to ClinicalTrials.gov plus one LLM planning call.
  ~12s   distribution by phase/status/sponsor
  ~20-26s comparisons and country breakdowns
  ~30-40s network graphs (`network`, `co-occurrence`, `combination` in the query)
Do NOT issue these in parallel: the upstream API rate-limits and the client
shares one pacing budget. Set max_citations_per_datum=0 to skip citation
fetching and cut roughly 40% of the time when you only need counts.

CORRECTNESS: no number here is produced by a model. Each count is an API
totalCount probe. If meta.buckets_are_exclusive is false, the buckets overlap
and you MUST NOT render a pie, donut, treemap or 100% stacked bar — use the
returned markdown, or grouped/independent bars of your own.

FAILS BY RETURNING {"ok": false, ...}, never by raising:
  invalid_request  query shorter than 3 chars, start_year after end_year, or an
                   unknown phase/status enum value. Fix the argument and retry.
  upstream         ClinicalTrials.gov was unreachable or returned an error after
                   5 retries with backoff. Transient — retry once after ~10s.
  unanswerable     the plan fanned out past the 60-probe cap. Narrow the
                   question with drug_name/condition/start_year.
TENANCY: pass tenant_id when several organisations share this server. It
partitions the probe cache only -- it is never sent upstream and never part of
a query. Omitting it puts you in the shared 'anonymous' partition, which is
correct for single-tenant use but means callers can observe each other's cache
timing. The upstream rate budget is pooled across tenants by design: it reveals
volume, not which questions anyone asked.

No API key configured just degrades planning to a deterministic keyword
planner; the counts are identical and meta.assumptions says so.""",
)
async def ctgov_query(
    query: str,
    drug_name: str | None = None,
    condition: str | None = None,
    sponsor: str | None = None,
    location: str | None = None,
    phase: list[str] | None = None,
    status: list[str] | None = None,
    start_year: int | None = None,
    end_year: int | None = None,
    max_citations_per_datum: int = 3,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    from .executor import execute

    try:
        request = QueryRequest(
            query=query,
            drug_name=drug_name,
            condition=condition,
            sponsor=sponsor,
            location=location,
            phase=[TrialPhase(p) for p in phase] if phase else None,
            status=[TrialStatus(s) for s in status] if status else None,
            start_year=start_year,
            end_year=end_year,
            max_citations_per_datum=max_citations_per_datum,
        )
    except (ValidationError, ValueError) as exc:
        return _fail(
            f"invalid_request: {exc}",
            "Correct the argument and call again. Valid phases: "
            + ", ".join(p.value for p in TrialPhase),
        )

    started = time.monotonic()
    try:
        client = await _client(tenant_id)
        if True:
            used_fallback = True
            plan = None
            if os.environ.get("OPENAI_API_KEY"):
                try:
                    plan = await Planner().plan(request)
                    used_fallback = False
                except (PlannerError, Exception):  # noqa: BLE001 - degrade, don't fail
                    plan = None
            if plan is None:
                plan = fallback_plan(request)

            response = await execute(plan, request, client)
            stats = {
                "ms": int((time.monotonic() - started) * 1000),
                "ctgov_requests": client.request_count,
                "ctgov_retries": client.retry_count,
                "planner": "keyword-fallback" if used_fallback else "llm",
            }
    except CTGovError as exc:
        return _fail(f"upstream: {exc}", "Transient upstream failure. Retry once after ~10s.")
    except ValueError as exc:
        return _fail(
            f"unanswerable: {exc}",
            "Narrow the question with drug_name, condition or a year range.",
        )

    if used_fallback:
        response.meta.assumptions.append(
            "Planned by the deterministic keyword fallback; no LLM was used."
        )

    return {
        "ok": True,
        "markdown": render_markdown(response),
        "ascii": render_ascii(response),
        "response": response.model_dump(mode="json"),
        "stats": stats,
        "render_contract": {
            "buckets_are_exclusive": response.meta.buckets_are_exclusive,
            "part_of_whole_marks_allowed": response.meta.buckets_are_exclusive,
            "denominator_note": response.meta.denominator_note,
            "showing": showing_notes(response),
            "warnings": response.meta.warnings,
        },
    }


@server.tool(
    name="ctgov_render",
    title="Render a saved QueryResponse as SVG, markdown or ASCII",
    annotations=READ_ONLY,
    description="""\
Turn a QueryResponse you already have into a visual artifact. Offline, instant,
no network, no LLM — call this freely.

ARGS
  response_json  a QueryResponse as a JSON string, or an examples/*.json body
                 (the {"request":..,"response":..} wrapper is unwrapped for you).
  format         "svg" | "markdown" | "ascii" | "neuromap".
                   svg      standalone, self-contained, light+dark aware, safe
                            to write to a file or embed.
                   neuromap standalone interactive HTML for a network_graph:
                            hover a node to isolate its neighbourhood, click an
                            agent trace step to highlight the node or edge it
                            measured. Cytoscape is inlined, so it opens offline.
                            ~450 KB; write it to a file rather than inlining it
                            in a chat response.
                   markdown table + inline data-URI SVG; paste into a transcript.
                   ascii    terminal bars; safe for logs.
  theme          "light" | "dark" | null (follow the viewer). SVG only.

RETURNS {ok, format, artifact, part_of_whole_marks_allowed}. `artifact` is the
rendered string; `part_of_whole_marks_allowed` mirrors
meta.buckets_are_exclusive so you can branch on it without parsing prose.

The rendering contract is enforced here, so you cannot accidentally
misrepresent the data:
  * buckets_are_exclusive false => never a pie/donut/treemap/100% stack, and a
    reconciliation strip stating bucket_sum vs total_matching plus the warnings
    is drawn into the artifact.
  * is_unbucketed marks are drawn in reserved grey WITH a hatch pattern, so they
    are distinguishable without colour, and are never dropped.
  * "showing N of M" appears wherever returned < total or citations <
    citation_total.
  * meta.denominator_note sits directly under the title.
  * every bar carries its numeric label. Nothing is encoded by colour alone.

FAILS BY RETURNING {"ok": false, ...}:
  invalid_response  the payload did not validate against QueryResponse. The
                    pydantic error is included; fix the payload, do not retry
                    unchanged.""",
)
def ctgov_render(
    response_json: str,
    format: Literal["svg", "markdown", "ascii", "neuromap"] = "markdown",
    theme: Literal["light", "dark"] | None = None,
) -> dict[str, Any]:
    try:
        response = _load_response(response_json)
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        return _fail(
            f"invalid_response: {exc}",
            "Pass the full QueryResponse object (with `visualization` and `meta`).",
        )
    artifact = {
        "svg": lambda: render_svg(response, theme=theme),
        "markdown": lambda: render_markdown(response),
        "ascii": lambda: render_ascii(response),
        # Interactive graph view. Only meaningful for a network_graph response;
        # render_neuromap returns an explanatory page for anything else rather
        # than an empty canvas.
        "neuromap": lambda: render_neuromap(response),
    }[format]()
    return {
        "ok": True,
        "format": format,
        "artifact": artifact,
        "part_of_whole_marks_allowed": response.meta.buckets_are_exclusive,
    }


@server.tool(
    name="ctgov_replay",
    title="Verify a response by re-issuing its trace",
    annotations=READ_ONLY_NET,
    description="""\
Re-issue every request recorded in meta.trace and compare the live count to the
recorded one, step by step. This is how you check that a response is honest
rather than taking its word for it.

RETURNS
  steps[]   {step, params, recorded, live, delta, status}
            status is "match" | "drift" | "error" | "skipped".
  summary   {total, matched, drifted, errored, verdict}
            verdict is "reproduced" when every step matched.

COST: one ClinicalTrials.gov request per trace step — 7 for a distribution, up
to 57 for a network graph. Roughly 3-30s. No LLM call.

INTERPRETING DRIFT: ClinicalTrials.gov gains and loses studies daily, so an old
response drifting by a few counts is normal and is NOT a bug. A step that
drifts by orders of magnitude, or where the ordering of buckets changed, means
the answer should be re-run. `status: "error"` means the upstream call failed,
which says nothing about the recorded number.

FAILS BY RETURNING {"ok": false, ...}:
  invalid_response  payload did not validate against QueryResponse.
  no_trace          meta.trace was empty — nothing to verify.""",
)
async def ctgov_replay(response_json: str) -> dict[str, Any]:
    try:
        response = _load_response(response_json)
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        return _fail(f"invalid_response: {exc}", "Pass the full QueryResponse object.")
    if not response.meta.trace:
        return _fail("no_trace: meta.trace is empty", "Nothing to verify for this response.")

    steps: list[dict[str, Any]] = []
    client = await _client(None)
    if True:
        for step in response.meta.trace:
            row: dict[str, Any] = {
                "step": step.step,
                "params": step.params,
                "recorded": step.result_count,
                "live": None,
                "delta": None,
            }
            if step.result_count is None:
                row["status"] = "skipped"
                steps.append(row)
                continue
            params = {k: v for k, v in step.params.items() if k not in ("countTotal", "pageSize")}
            try:
                live = await client.count(Filter(params))
            except Exception as exc:  # noqa: BLE001 - one bad step must not kill the report
                row["status"] = "error"
                row["reason"] = str(exc)
            else:
                row["live"] = live
                row["delta"] = live - step.result_count
                row["status"] = "match" if live == step.result_count else "drift"
            steps.append(row)

    matched = sum(1 for s in steps if s["status"] == "match")
    drifted = sum(1 for s in steps if s["status"] == "drift")
    errored = sum(1 for s in steps if s["status"] == "error")
    return {
        "ok": True,
        "steps": steps,
        "summary": {
            "total": len(steps),
            "matched": matched,
            "drifted": drifted,
            "errored": errored,
            "verdict": "reproduced" if drifted == 0 and errored == 0 else "not fully reproduced",
        },
    }


@server.tool(
    name="ctgov_schema",
    title="JSON Schemas for the request and response",
    annotations=READ_ONLY,
    description="""\
Return the JSON Schemas for QueryRequest and QueryResponse. Offline, instant,
no network. Call this first if you are going to consume the response
programmatically or generate a renderer.

RETURNS {request: <JSON Schema>, response: <JSON Schema>, contract: [...]},
where `contract` is the four rendering rules stated in prose, because they are
semantic constraints that a JSON Schema cannot express:
  1. buckets_are_exclusive false => no pie, no 100% stacked bar.
  2. render is_unbucketed data neutrally; never drop it.
  3. citations is a bounded sample; citation_total is the truth.
  4. show denominator_note beside any proportion.

Never fails.""",
)
def ctgov_schema() -> dict[str, Any]:
    return {
        "ok": True,
        "request": QueryRequest.model_json_schema(),
        "response": QueryResponse.model_json_schema(),
        "contract": [
            "meta.buckets_are_exclusive false => NEVER render a pie, donut, treemap or "
            "100% stacked bar. Buckets overlap because designModule.phases is an array. "
            "Use independent or grouped bars and surface meta.warnings.",
            "Render data[].is_unbucketed marks. They are 'no value recorded', not a "
            "category: style them neutrally and distinguish them without relying on "
            "colour. Never drop them.",
            "data[].citations is a bounded sample; data[].citation_total is the real "
            "number. Show 'showing 3 of 219'. Same for visualization.returned vs total.",
            "Show meta.denominator_note next to any proportion. A percentage without its "
            "base is unreadable.",
        ],
        "invariant": (
            "The model plans; it never answers. Every count is a ClinicalTrials.gov "
            "totalCount probe recorded in meta.trace and verifiable with ctgov_replay."
        ),
    }


@server.tool(
    name="ctgov_examples",
    title="Five bundled example runs (offline)",
    annotations=READ_ONLY,
    description="""\
Return the bundled example runs — real responses captured against the live API
and checked into the repo. Offline, instant, no network, no API key.

Use these to build and test a renderer, or to see the response shape, WITHOUT
spending a 12-40s network query. They cover every visualization type the system
produces: bar_chart, time_series, grouped_bar_chart, network_graph, and a
geographic bar_chart.

ARGS
  slug     omit to get the index of all five with their headline numbers; pass
           e.g. "01-distribution-phases" to get one full run.
  include_render  when a slug is given, also return the markdown + ascii
           artifacts for it (default true).

RETURNS
  without slug: {index: [{slug, query, viz, marks, edges, total_matching,
                 exclusive, api_calls, ms}]}
  with slug:    {slug, request, response, markdown, ascii}

Note every bundled run has exclusive=false — phase, year and country buckets all
overlap or leak, which is exactly why the schema carries the exclusivity flag.

FAILS BY RETURNING {"ok": false, ...}:
  unknown_slug   the slug is not one of the five; the valid list is included.""",
)
def ctgov_examples(slug: str | None = None, include_render: bool = True) -> dict[str, Any]:
    index_path = EXAMPLES_DIR / "index.json"
    if not index_path.exists():
        return _fail(
            f"missing_examples: {index_path} not found",
            "The examples/ directory is not present in this checkout.",
        )
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if slug is None:
        return {"ok": True, "index": index}

    entry = next((e for e in index if e["slug"] == slug), None)
    if entry is None:
        return _fail(
            f"unknown_slug: {slug!r}",
            "Valid slugs: " + ", ".join(e["slug"] for e in index),
        )
    raw = json.loads((EXAMPLES_DIR / f"{slug}.json").read_text(encoding="utf-8"))
    response = QueryResponse.model_validate(raw["response"])
    payload: dict[str, Any] = {
        "ok": True,
        "slug": slug,
        "request": raw["request"],
        "response": raw["response"],
    }
    if include_render:
        payload["markdown"] = render_markdown(response)
        payload["ascii"] = render_ascii(response)
    return payload


def main() -> None:  # pragma: no cover - process entry point
    server.run("stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
