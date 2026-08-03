"""MCP server tests, driven the way a coding agent would drive the server.

The scenario each test follows is the one a reviewing engineer will actually
run: connect, list tools, read the descriptions to decide what to call, work
offline against the examples, then spend one network call and verify it.

Two things are tested that are easy to skip and expensive to get wrong:

  * the tool *descriptions* — an agent budgets from them, so latency, cost and
    failure modes must be stated. A description that omits "12-40s" causes real
    timeouts downstream.
  * the failure contract — these tools return {"ok": false, ...} instead of
    raising, because a raised exception inside a tool call gives an agent no
    structured way to decide whether to retry, narrow, or give up.

Offline. The one network tool is exercised against an injected fake client.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from app import mcp_server
from app.models import QueryResponse

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
NETWORK_TOOLS = {"ctgov_query", "ctgov_replay"}
OFFLINE_TOOLS = {"ctgov_render", "ctgov_schema", "ctgov_examples"}


async def call(name: str, **arguments: Any) -> dict[str, Any]:
    result = await mcp_server.server.call_tool(name, arguments)
    assert result.is_error is False, result.content
    assert result.structured_content is not None
    return result.structured_content


def example_response(slug: str = "01-distribution-phases") -> QueryResponse:
    raw = json.loads((EXAMPLES / f"{slug}.json").read_text(encoding="utf-8"))
    return QueryResponse.model_validate(raw["response"])


# ---------------------------------------------------------------------------
# discovery: what an agent sees on connect
# ---------------------------------------------------------------------------


async def test_all_five_tools_are_advertised() -> None:
    tools = await mcp_server.server.list_tools()
    assert {t.name for t in tools} == NETWORK_TOOLS | OFFLINE_TOOLS
    for tool in tools:
        assert tool.title, f"{tool.name} has no human-readable title"
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True, "no tool here mutates anything"


async def test_server_instructions_state_the_invariant_and_the_budget() -> None:
    text = mcp_server.INSTRUCTIONS
    assert "never answers" in text
    assert "buckets_are_exclusive" in text
    assert "12-40 seconds" in text
    assert "ctgov_examples" in text, "an agent should be pointed at the offline path first"


async def test_descriptions_state_cost_and_failure_modes() -> None:
    """Written FOR AN AGENT: what it returns, what it costs, how it fails."""
    tools = {t.name: (t.description or "") for t in await mcp_server.server.list_tools()}

    for name, description in tools.items():
        assert len(description) > 400, f"{name} description is too thin to plan against"
        assert "RETURNS" in description or "Return" in description, name

    assert "12-57 requests" in tools["ctgov_query"]
    assert "~30-40s" in tools["ctgov_query"]
    assert "Do NOT issue these in parallel" in tools["ctgov_query"]
    for mode in ("invalid_request", "upstream", "unanswerable"):
        assert mode in tools["ctgov_query"], f"ctgov_query does not document {mode}"

    assert "one ClinicalTrials.gov request per trace step" in tools["ctgov_replay"]
    assert "drift" in tools["ctgov_replay"].lower()

    for name in OFFLINE_TOOLS:
        lowered = tools[name].lower()
        assert "offline" in lowered and "no network" in lowered, name

    # Network tools must be marked open-world; offline ones must not be.
    annotations = {t.name: t.annotations for t in await mcp_server.server.list_tools()}
    for name in NETWORK_TOOLS:
        assert annotations[name].open_world_hint is True, name
    for name in OFFLINE_TOOLS:
        assert annotations[name].open_world_hint is not True, name


async def test_query_schema_exposes_the_structured_constraints() -> None:
    tool = next(t for t in await mcp_server.server.list_tools() if t.name == "ctgov_query")
    properties = tool.input_schema["properties"]
    assert tool.input_schema["required"] == ["query"]
    for field in ("drug_name", "condition", "sponsor", "location", "phase", "status",
                  "start_year", "end_year", "max_citations_per_datum"):
        assert field in properties, field


# ---------------------------------------------------------------------------
# offline path: schema and examples
# ---------------------------------------------------------------------------


async def test_schema_tool_carries_the_rules_a_json_schema_cannot_express() -> None:
    payload = await call("ctgov_schema")
    assert payload["ok"] is True
    assert "query" in payload["request"]["required"]
    assert "visualization" in payload["response"]["properties"]

    contract = " ".join(payload["contract"])
    assert len(payload["contract"]) == 4
    assert "NEVER render a pie" in contract
    assert "is_unbucketed" in contract
    assert "citation_total" in contract
    assert "denominator_note" in contract
    assert "never answers" in payload["invariant"]


async def test_examples_index_then_one_full_run() -> None:
    index = (await call("ctgov_examples"))["index"]
    assert len(index) == 5
    assert {e["viz"] for e in index} == {
        "bar_chart", "time_series", "grouped_bar_chart", "network_graph"
    }
    # The claim in the tool description — every bundled run overlaps — must hold.
    assert all(e["exclusive"] is False for e in index)

    payload = await call("ctgov_examples", slug="01-distribution-phases")
    assert payload["slug"] == "01-distribution-phases"
    QueryResponse.model_validate(payload["response"])
    assert "data:image/svg+xml;base64," in payload["markdown"]
    assert "BUCKETS OVERLAP" in payload["ascii"]

    lean = await call("ctgov_examples", slug="01-distribution-phases", include_render=False)
    assert "markdown" not in lean and "ascii" not in lean


async def test_unknown_slug_returns_a_recoverable_error() -> None:
    payload = await call("ctgov_examples", slug="99-does-not-exist")
    assert payload["ok"] is False
    assert "unknown_slug" in payload["error"]
    assert "01-distribution-phases" in payload["remedy"], "the remedy must be actionable"


# ---------------------------------------------------------------------------
# render: the contract is enforced server-side, not left to the agent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["svg", "markdown", "ascii"])
async def test_render_every_format(fmt: str) -> None:
    body = json.dumps(example_response().model_dump(mode="json"))
    payload = await call("ctgov_render", response_json=body, format=fmt)
    assert payload["ok"] is True and payload["format"] == fmt
    artifact = payload["artifact"]
    assert "of 3,738 trials matching query.cond=melanoma" in artifact
    assert payload["part_of_whole_marks_allowed"] is False
    if fmt == "svg":
        ET.fromstring(artifact)


async def test_render_accepts_the_example_wrapper_shape() -> None:
    raw = (EXAMPLES / "04-network-graph.json").read_text(encoding="utf-8")
    payload = await call("ctgov_render", response_json=raw, format="ascii")
    assert payload["ok"] is True
    assert "NODES" in payload["artifact"] and "EDGES" in payload["artifact"]


async def test_render_rejects_a_malformed_payload_without_raising() -> None:
    payload = await call("ctgov_render", response_json='{"visualization": {}}')
    assert payload["ok"] is False
    assert "invalid_response" in payload["error"]
    assert payload["remedy"]

    payload = await call("ctgov_render", response_json="not json at all")
    assert payload["ok"] is False


async def test_render_theme_is_honoured() -> None:
    body = json.dumps(example_response().model_dump(mode="json"))
    dark = await call("ctgov_render", response_json=body, format="svg", theme="dark")
    assert 'data-theme="dark"' in dark["artifact"]


# ---------------------------------------------------------------------------
# query: plumbing real, network faked
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, counts: dict[str, int] | None = None, raises: Exception | None = None):
        self._counts = counts or {}
        self._raises = raises
        self.request_count = 0
        self.retry_count = 0

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def for_tenant(self, tenant: str | None):  # noqa: ANN201
        """The real client returns a tenant-scoped view; a double must too."""
        self.scoped_for = tenant
        return self

    async def count(self, filt) -> int:  # noqa: ANN001
        if self._raises:
            raise self._raises
        self.request_count += 1
        key = filt.params.get("filter.advanced") or filt.params.get("query.cond") or ""
        return self._counts.get(key, self._counts.get("*", 0))


async def test_query_returns_chart_json_and_a_machine_readable_contract(monkeypatch) -> None:
    response = example_response()

    async def _execute(plan, request, client):  # noqa: ANN001
        return response

    monkeypatch.setattr("app.executor.execute", _execute)
    monkeypatch.setattr(mcp_server, "CTGovClient", lambda *a, **k: _FakeClient())
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    payload = await call("ctgov_query", query="How are melanoma trials distributed across phases?")
    assert payload["ok"] is True
    assert set(payload) >= {"markdown", "ascii", "response", "stats", "render_contract"}

    QueryResponse.model_validate(payload["response"])
    assert "data:image/svg+xml;base64," in payload["markdown"]
    assert "BUCKETS OVERLAP" in payload["ascii"]

    contract = payload["render_contract"]
    assert contract["buckets_are_exclusive"] is False
    assert contract["part_of_whole_marks_allowed"] is False, (
        "an agent drawing its own chart must be able to read this without parsing prose"
    )
    assert contract["denominator_note"].startswith("of 3,738")
    assert any("showing 3 of 1,529" in note for note in contract["showing"])
    assert contract["warnings"]

    assert payload["stats"]["planner"] == "keyword-fallback"
    assert isinstance(payload["stats"]["ms"], int)
    assert "no LLM was used" in " ".join(payload["response"]["meta"]["assumptions"])


async def test_query_rejects_a_bad_enum_before_touching_the_network(monkeypatch) -> None:
    def _explode(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("must not open a client for an invalid request")

    monkeypatch.setattr(mcp_server, "CTGovClient", _explode)
    payload = await call("ctgov_query", query="melanoma phases", phase=["PHASE9"])
    assert payload["ok"] is False
    assert "invalid_request" in payload["error"]
    assert "PHASE1" in payload["remedy"], "the remedy must list the valid values"


async def test_query_rejects_a_too_short_question(monkeypatch) -> None:
    monkeypatch.setattr(mcp_server, "CTGovClient", lambda *a, **k: _FakeClient())
    payload = await call("ctgov_query", query="hi")
    assert payload["ok"] is False and "invalid_request" in payload["error"]


async def test_upstream_failure_is_reported_as_retryable(monkeypatch) -> None:
    from app.ctgov import CTGovError

    monkeypatch.setattr(
        mcp_server, "CTGovClient", lambda *a, **k: _FakeClient(raises=CTGovError("429 exhausted"))
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    payload = await call("ctgov_query", query="How are melanoma trials distributed across phases?")
    assert payload["ok"] is False
    assert payload["error"].startswith("upstream:")
    assert "Retry" in payload["remedy"]


async def test_probe_cap_is_reported_as_unanswerable(monkeypatch) -> None:
    async def _execute(plan, request, client):  # noqa: ANN001
        raise ValueError("plan would issue 214 probes, above the cap of 60")

    monkeypatch.setattr("app.executor.execute", _execute)
    monkeypatch.setattr(mcp_server, "CTGovClient", lambda *a, **k: _FakeClient())
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    payload = await call("ctgov_query", query="everything about everything")
    assert payload["ok"] is False
    assert payload["error"].startswith("unanswerable:")
    assert "Narrow" in payload["remedy"]


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


async def test_replay_reproduces_and_reports_a_verdict(monkeypatch) -> None:
    response = example_response()
    recorded = {
        (s.params.get("filter.advanced") or s.params.get("query.cond") or ""): s.result_count
        for s in response.meta.trace
    }
    monkeypatch.setattr(mcp_server, "CTGovClient", lambda *a, **k: _FakeClient(counts=recorded))

    payload = await call(
        "ctgov_replay", response_json=json.dumps(response.model_dump(mode="json"))
    )
    assert payload["ok"] is True
    assert payload["summary"]["verdict"] == "reproduced"
    assert payload["summary"]["total"] == len(response.meta.trace)
    assert payload["summary"]["drifted"] == 0
    assert all(step["status"] == "match" for step in payload["steps"])
    assert all(step["delta"] == 0 for step in payload["steps"])


async def test_replay_reports_drift_step_by_step(monkeypatch) -> None:
    response = example_response()
    monkeypatch.setattr(mcp_server, "CTGovClient", lambda *a, **k: _FakeClient(counts={"*": 1}))
    payload = await call(
        "ctgov_replay", response_json=json.dumps(response.model_dump(mode="json"))
    )
    assert payload["summary"]["verdict"] == "not fully reproduced"
    assert payload["summary"]["drifted"] == len(response.meta.trace)
    drifted = payload["steps"][0]
    assert drifted["live"] == 1
    assert drifted["delta"] == 1 - drifted["recorded"]
    assert drifted["params"], "the literal request must travel with the verdict"


async def test_replay_one_bad_step_does_not_lose_the_whole_report(monkeypatch) -> None:
    monkeypatch.setattr(
        mcp_server, "CTGovClient", lambda *a, **k: _FakeClient(raises=RuntimeError("boom"))
    )
    payload = await call(
        "ctgov_replay",
        response_json=json.dumps(example_response().model_dump(mode="json")),
    )
    assert payload["ok"] is True, "a failed upstream call is a finding, not a crash"
    assert payload["summary"]["errored"] == payload["summary"]["total"]
    assert payload["steps"][0]["reason"] == "boom"


async def test_replay_rejects_a_response_with_no_trace() -> None:
    response = example_response()
    payload = response.model_dump(mode="json")
    payload["meta"]["trace"] = []
    result = await call("ctgov_replay", response_json=json.dumps(payload))
    assert result["ok"] is False and "no_trace" in result["error"]


async def test_replay_rejects_garbage() -> None:
    result = await call("ctgov_replay", response_json="{}")
    assert result["ok"] is False and "invalid_response" in result["error"]
