"""FastAPI service.

Two transports over one core, deliberately:

  POST /api/query   the documented JSON contract. Request in, QueryResponse out.
                    This is the deliverable a frontend engineer implements
                    against, and what the example runs exercise.

  POST /api/chat    the same pipeline streamed in assistant-ui's protocol, so
                    the demo UI can render panels as they resolve.

The streaming endpoint adds no logic of its own. It calls the same planner and
executor and re-shapes the result into stream events, so there is no second
implementation that could disagree with the first about a number.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .ctgov import CTGovClient, CTGovError
from .neuromap import VENDOR as CYTOSCAPE_BUNDLE, render_neuromap
from .executor import execute
from .memory import MemoryStore, resolve_followup
from .store import get_store
from .tenancy import TenantRegistry
from .models import QueryRequest, QueryResponse
from .agent import make_planner
from .planner import PlannerError, QueryPlan, fallback_plan

load_dotenv()

ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
    if o.strip()
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """One HTTP client and one planner for the process.

    The client owns the rate-limiting state, so sharing it means concurrent
    requests share a throttle budget instead of each opening its own and
    collectively tripping the API's limit.
    """
    # Optional shared probe cache. Inert without DATABASE_URL, so the service
    # runs unchanged with no database at all.
    store = get_store()
    await store.connect()
    # `async with store` so the asyncpg pool is closed on shutdown. Dropping
    # this leaks DEFAULT_POOL_MAX connections per deploy cycle; connect() never
    # raises, so the no-database path is unaffected either way.
    # Per-tenant cache isolation. Without this the probe cache is global and a
    # measured 31,377x cold/warm timing delta lets one tenant detect another's
    # queries. The QUOTA stays shared (it reveals volume, not questions); only
    # the CACHE is partitioned. See app/tenancy.py.
    tenants = TenantRegistry()
    async with store, CTGovClient(store=store, tenants=tenants) as client:
        app.state.ctgov = client
        # PLANNER=graph opts into the multi-step LangGraph planner; anything
        # else keeps the single structured-output call, which stays the default.
        # Both expose `plan(request) -> QueryPlan`, so nothing downstream cares.
        app.state.store = store
        app.state.tenants = tenants
        app.state.memory = MemoryStore()
        app.state.planner = make_planner(ctgov=client)
        yield


app = FastAPI(
    title="ClinicalTrials.gov Query-to-Visualization Agent",
    version="0.1.0",
    lifespan=lifespan,
)
@app.get("/vendor/cytoscape.min.js", include_in_schema=False)
async def cytoscape_bundle() -> FileResponse:
    """Serve the vendored graph library to the chat UI's neuromap iframe.

    Same-origin and vendored: no CDN, so the chat UI has no third-party runtime
    dependency and works on a machine that cannot reach one. Cached for a day --
    the file only changes when the vendored bundle is deliberately replaced.
    """
    if not CYTOSCAPE_BUNDLE.exists():  # pragma: no cover - ships vendored
        raise HTTPException(status_code=404, detail="cytoscape bundle not vendored")
    return FileResponse(
        CYTOSCAPE_BUNDLE,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=86400"},
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type"],
)


async def _plan_for(request: QueryRequest) -> tuple[QueryPlan, bool]:
    """Plan with the LLM, falling back to the keyword planner.

    Returns (plan, used_fallback). A missing key or a model outage degrades the
    service to "less flexible" rather than "down"; the response says which
    planner ran so a reader never has to guess.
    """
    if not os.environ.get("OPENAI_API_KEY"):
        return fallback_plan(request), True
    try:
        return await app.state.planner.plan(request), False
    except (PlannerError, Exception):
        return fallback_plan(request), True


@app.get("/api/health")
async def health() -> dict[str, Any]:
    client: CTGovClient = app.state.ctgov
    return {
        "status": "ok",
        "llm_configured": bool(os.environ.get("OPENAI_API_KEY")),
        # Upstream counters are deliberately NOT published.
        #
        # They are a deterministic cross-tenant oracle: read the counter, issue
        # one query, read it again. A delta of 0 means someone else had already
        # asked that exact question; 1 means nobody had. Strictly worse than the
        # cache timing channel -- one probe, no statistics, and immune to any
        # latency-jitter defence, because jitter does not perturb an integer.
        # Operational visibility belongs on an authenticated metrics endpoint,
        # not on an unauthenticated liveness probe.
    }


@app.post("/api/query", response_model=QueryResponse)
async def query(request: QueryRequest) -> QueryResponse:
    """The documented contract. Natural language in, visualization spec out."""
    plan, used_fallback = await _plan_for(request)
    try:
        scoped = app.state.ctgov.for_tenant(getattr(request, "tenant_id", None))
        response = await execute(plan, request, scoped)
    except CTGovError as exc:
        # 502, not 500: the failure is upstream and the caller may retry.
        raise HTTPException(status_code=502, detail=f"ClinicalTrials.gov: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if used_fallback:
        response.meta.assumptions.append(
            "Planned by the deterministic keyword fallback; no LLM was used."
        )
    return response


# --------------------------------------------------------------------------
# assistant-ui streaming transport
# --------------------------------------------------------------------------


class ChatTextPart(BaseModel):
    type: Literal["text"]
    text: str


class ChatMessage(BaseModel):
    role: str
    content: list[ChatTextPart] | str


class ChatRequest(BaseModel):
    """assistant-ui's request shape. `system` and `tools` are accepted and
    ignored: the planner's contract is fixed here, not caller-supplied."""

    messages: list[ChatMessage] = Field(default_factory=list)
    # assistant-ui's AssistantChatTransport already puts the conversation id in
    # the body; it was being received and discarded. Without it every caller
    # shared one memory session, so one user's follow-up inherited another
    # user's filters — an honestly-measured answer to a question nobody asked,
    # which passes citations, trace and reconciliation unchanged.
    id: str | None = None
    system: str | None = None
    tools: list[Any] | None = None


def _latest_user_text(messages: list[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.role != "user":
            continue
        if isinstance(message.content, str):
            return message.content.strip()
        return " ".join(p.text for p in message.content if p.type == "text").strip()
    return ""


def _panels(response: QueryResponse) -> list[dict[str, Any]]:
    """Choose which panels this answer needs.

    This is the composition step. Panels are not a fixed template: the
    reconciliation panel exists only when the exclusivity check actually
    failed, and the citations panel only when citations were fetched. The
    surface therefore differs per question while every panel is drawn from a
    validated vocabulary the frontend already knows how to render.
    """
    viz, meta = response.visualization, response.meta
    panels: list[dict[str, Any]] = []

    if viz.type.value == "network_graph":
        panels.append({
            "name": "network",
            "data": {
                "title": viz.title,
                "nodes": [n.model_dump() for n in viz.nodes],
                "edges": [e.model_dump() for e in viz.edges],
                "returned": viz.returned, "total": viz.total,
                "denominator_note": meta.denominator_note,
                # The interactive map, rendered by the same code the MCP tool
                # uses. Sent as HTML rather than as a second copy of the data so
                # there is one graph renderer, not two that can disagree --
                # neuromap already matches AREA clauses structurally to tie each
                # node and edge back to the probe that produced it, and
                # reimplementing that in TSX would be the drift.
                #
                # bundle_src, not inlined: 440 KB per message on the SSE stream
                # versus 5 KB and one cached fetch.
                "neuromap_html": render_neuromap(
                    response, bundle_src="/vendor/cytoscape.min.js"
                ),
            },
        })
    else:
        panels.append({
            "name": "distribution",
            "data": {
                "title": viz.title, "viz_type": viz.type.value,
                "encoding": viz.encoding.model_dump(exclude_none=True),
                "data": [d.model_dump() for d in viz.data],
                "denominator_note": meta.denominator_note,
                "sort": meta.sort, "units": meta.units,
                "time_granularity": meta.time_granularity,
            },
        })

    # Composed in only when the measurement says it is needed.
    if not meta.buckets_are_exclusive and meta.bucket_sum is not None:
        panels.append({
            "name": "reconciliation",
            "data": {
                "total_matching": meta.total_matching,
                "bucket_sum": meta.bucket_sum,
                "unbucketed_count": meta.unbucketed_count,
                "buckets_are_exclusive": meta.buckets_are_exclusive,
                "warnings": meta.warnings,
            },
        })

    cited = [d for d in viz.data if d.citations]
    if cited:
        panels.append({
            "name": "citations",
            "data": {
                "groups": [
                    {
                        "label": d.label,
                        "returned": len(d.citations),
                        "total": d.citation_total,
                        "citations": [c.model_dump() for c in d.citations],
                    }
                    for d in cited[:6]
                ]
            },
        })

    panels.append({
        "name": "trace",
        "data": {
            "interpretation": meta.interpretation,
            "steps": [s.model_dump() for s in meta.trace],
            "assumptions": meta.assumptions,
            "filters_applied": meta.filters_applied,
            "source": meta.source,
        },
    })
    return panels


def _sse(frame: dict[str, Any]) -> str:
    return f"data: {json.dumps(frame, separators=(',', ':'))}\n\n"


@app.post("/api/chat")
async def chat(request: dict[str, Any]) -> StreamingResponse:
    """Stream the same answer as panels, for the assistant-ui demo.

    Emits the AI SDK v7 UI-message-stream format directly rather than using
    assistant-stream's DataStreamResponse. That encoder produces the older
    prefixed data-stream (`0:` text, `2:` data), which the installed
    `AssistantChatTransport` on AI SDK v7 does not parse — the panels never
    arrived. assistant-stream ships no v7 encoder, and the format is small
    enough to emit exactly.

    Panels arrive as `data-<name>` parts, which is what assistant-ui's
    `makeAssistantDataUI` renderers bind to.
    """
    user_text = ""
    messages = request.get("messages", [])
    if isinstance(messages, list):
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content", message.get("parts", ""))
            if isinstance(content, str):
                user_text = content.strip()
            elif isinstance(content, list):
                user_text = " ".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ).strip()
            break

    async def frames() -> AsyncIterator[str]:
        # v7 associates every subsequent part with this messageId; without it
        # the client has no message to attach text and data parts to.
        message_id = f"msg-{abs(hash(user_text)) % 10**10}"
        yield _sse({"type": "start", "messageId": message_id})
        yield _sse({"type": "start-step"})
        text_id = "t0"

        async def say(message: str) -> AsyncIterator[str]:
            yield _sse({"type": "text-start", "id": text_id})
            yield _sse({"type": "text-delta", "id": text_id, "delta": message})
            yield _sse({"type": "text-end", "id": text_id})

        if not user_text:
            async for f in say("Ask a question about clinical trials to begin."):
                yield f
            yield _sse({"type": "finish-step"})
            yield _sse({"type": "finish", "finishReason": "stop"})
            return

        try:
            query_request = QueryRequest(query=user_text)
        except ValueError as exc:
            async for f in say(f"That question could not be parsed: {exc}"):
                yield f
            yield _sse({"type": "finish-step"})
            yield _sse({"type": "finish", "finishReason": "stop"})
            return

        # Follow-up resolution. Memory carries FILTERS, never counts, so a
        # follow-up is re-measured from scratch under the inherited filters.
        # resolve_followup returns None whenever it is unsure, so this degrades
        # to stateless behaviour rather than to a wrong answer — a false
        # follow-up would return an honestly-measured count for a question
        # nobody asked, which is the same harm as replaying a stale one.
        # The endpoint takes the raw body, so read the id defensively: it may
        # be absent, non-string, or the body may not be a dict at all.
        raw_id = request.get("id") if isinstance(request, dict) else getattr(request, "id", None)
        session_id = (str(raw_id).strip() if raw_id else "") or "anonymous"
        raw_tenant = request.get("tenant_id") if isinstance(request, dict) else None
        session = app.state.memory.for_session(session_id)
        followup = resolve_followup(user_text, session)
        if followup is not None:
            query_request = followup

        plan, used_fallback = await _plan_for(query_request)
        reason = (
            f"Planned: dimension={plan.dimension.value}, viz={plan.viz_type.value}"
            + (" (keyword fallback, no LLM)" if used_fallback else "")
        )
        yield _sse({"type": "reasoning-start", "id": "r0"})
        yield _sse({"type": "reasoning-delta", "id": "r0", "delta": reason})
        yield _sse({"type": "reasoning-end", "id": "r0"})

        try:
            tenant = raw_tenant or session_id
            scoped = app.state.ctgov.for_tenant(tenant)
            response = await execute(plan, query_request, scoped)
        except (CTGovError, ValueError) as exc:
            yield _sse({"type": "error", "errorText": str(exc)})
            async for f in say(f"Could not complete that query: {exc}"):
                yield f
            yield _sse({"type": "finish-step"})
            yield _sse({"type": "finish", "finishReason": "error"})
            return

        session.record_response(query_request.query, plan, response)

        async for f in say(response.meta.interpretation):
            yield f

        for panel in _panels(response):
            name = panel.pop("name")
            yield _sse({"type": f"data-{name}", "data": panel["data"]})

        seen: set[str] = set()
        for datum in response.visualization.data:
            for citation in datum.citations:
                if citation.nct_id in seen:
                    continue
                seen.add(citation.nct_id)
                yield _sse({
                    "type": "source-url",
                    "sourceId": citation.nct_id,
                    "url": f"https://clinicaltrials.gov/study/{citation.nct_id}",
                    "title": citation.excerpt[:120],
                })

        yield _sse({"type": "finish-step"})
        yield _sse({"type": "finish", "finishReason": "stop"})

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"x-vercel-ai-ui-message-stream": "v1", "cache-control": "no-cache"},
    )
