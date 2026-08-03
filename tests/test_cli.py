"""CLI tests, written as the sessions an evaluating agent would actually run.

Each test is one persona working end to end, not a flag in isolation:

  * a coding agent that has just cloned the repo and has no API key,
  * a renderer author who wants the schema and the offline fixtures,
  * a reviewer checking that a claimed number can still be reproduced,
  * an operator hitting the sad paths — bad file, bad enum, upstream down.

Everything here is offline. The two commands that touch the network
(`query`, `replay`) are exercised against injected fakes so the failure modes
are covered without waiting 40s or depending on ClinicalTrials.gov being up.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from app import cli
from app.models import QueryResponse

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def example_response(slug: str = "01-distribution-phases") -> QueryResponse:
    raw = json.loads((EXAMPLES / f"{slug}.json").read_text(encoding="utf-8"))
    return QueryResponse.model_validate(raw["response"])


# ---------------------------------------------------------------------------
# persona: agent that just cloned the repo, no key, no network
# ---------------------------------------------------------------------------


def test_cold_start_discovery_path(capsys) -> None:
    """`--help` -> `examples` -> `schema` is the loop an agent runs before it
    spends a 40s network call. All three must work with nothing configured."""
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    # The help must state cost and the invariant, or an agent cannot budget.
    for phrase in ("count-probes", "12-40s", "The model plans", "no pie", "replay"):
        assert phrase in help_text, f"help does not mention {phrase!r}"
    for command in ("query", "schema", "replay", "examples", "render"):
        assert command in help_text

    code, out, _ = run(capsys, "examples")
    assert code == 0
    index = json.loads((EXAMPLES / "index.json").read_text(encoding="utf-8"))
    assert f"{len(index)} bundled runs" in out
    for entry in index:
        assert entry["slug"] in out
        assert entry["query"] in out

    code, out, _ = run(capsys, "schema")
    assert code == 0
    schema = json.loads(out)
    assert set(schema) == {"request", "response"}
    assert "query" in schema["request"]["required"]
    # The fields a renderer must branch on are all discoverable from the schema.
    definitions = schema["response"].get("$defs", {})
    meta = definitions["ResponseMeta"]["properties"]
    for field in ("buckets_are_exclusive", "denominator_note", "bucket_sum", "warnings", "trace"):
        assert field in meta
    assert "MUST NOT draw a pie" in meta["buckets_are_exclusive"]["description"]


def test_schema_can_be_narrowed(capsys) -> None:
    code, out, _ = run(capsys, "schema", "--which", "request")
    assert code == 0
    assert set(json.loads(out)) == {"request"}


# ---------------------------------------------------------------------------
# persona: renderer author working entirely from the bundled fixtures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "slug",
    [
        "01-distribution-phases",
        "02-time-series",
        "03-comparison",
        "04-network-graph",
        "05-geographic",
    ],
)
def test_every_example_renders_in_every_format(capsys, tmp_path: Path, slug: str) -> None:
    svg_path = tmp_path / f"{slug}.svg"
    code, out, err = run(capsys, "examples", "--slug", slug, "--svg", str(svg_path))
    assert code == 0
    assert svg_path.exists()
    ET.parse(svg_path)  # well-formed
    assert str(svg_path) in err, "the artifact path must be announced on stderr"
    assert "of " in out  # the denominator line

    code, md, _ = run(capsys, "examples", "--slug", slug, "--md")
    assert code == 0 and "data:image/svg+xml;base64," in md

    code, raw, _ = run(capsys, "examples", "--slug", slug, "--json")
    assert code == 0
    QueryResponse.model_validate(json.loads(raw))


def test_stdout_is_only_the_artifact(capsys, tmp_path: Path) -> None:
    """`trialscope ... --json | jq` must never choke on a diagnostic. Every
    log line goes to stderr."""
    code, out, err = run(
        capsys, "examples", "--slug", "01-distribution-phases",
        "--json", "--svg", str(tmp_path / "c.svg"),
    )
    assert code == 0
    json.loads(out)  # stdout parses as JSON with nothing prepended
    assert err.strip(), "the write notice belongs on stderr"


def test_render_subcommand_round_trips_a_saved_response(capsys, tmp_path: Path) -> None:
    saved = tmp_path / "response.json"
    saved.write_text(
        json.dumps(example_response().model_dump(mode="json")), encoding="utf-8"
    )
    out_path = tmp_path / "chart.svg"
    code, _, _ = run(capsys, "render", str(saved), "--format", "svg", "--out", str(out_path))
    assert code == 0
    ET.parse(out_path)

    # And it accepts a bundled example file, wrapper and all.
    code, out, _ = run(
        capsys, "render", str(EXAMPLES / "01-distribution-phases.json"), "--format", "ascii"
    )
    assert code == 0
    assert "BUCKETS OVERLAP" in out


def test_theme_flag_pins_the_colour_scheme(capsys, tmp_path: Path) -> None:
    for theme, expected in (("dark", 'data-theme="dark"'), ("light", 'data-theme="light"')):
        path = tmp_path / f"{theme}.svg"
        run(capsys, "render", str(EXAMPLES / "01-distribution-phases.json"),
            "--format", "svg", "--theme", theme, "--out", str(path))
        assert expected in path.read_text(encoding="utf-8")
    path = tmp_path / "auto.svg"
    run(capsys, "render", str(EXAMPLES / "01-distribution-phases.json"),
        "--format", "svg", "--out", str(path))
    body = path.read_text(encoding="utf-8")
    assert 'data-theme="dark"' not in body.split("<defs>")[0]
    assert "prefers-color-scheme:dark" in body


# ---------------------------------------------------------------------------
# persona: reviewer verifying a claimed number
# ---------------------------------------------------------------------------


class _FakeClient:
    """Stands in for CTGovClient. `counts` maps a marker param to a live count."""

    def __init__(self, counts: dict[str, int] | None = None, raises: Exception | None = None):
        self._counts = counts or {}
        self._raises = raises
        self.request_count = 0
        self.retry_count = 0
        self.seen: list[dict[str, str]] = []

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def for_tenant(self, tenant: str | None):  # noqa: ANN201
        """Real CTGovClient returns a tenant-scoped view; a double must too.

        Added when the MCP server stopped opening a client per call. Not a
        loosened check -- the opposite: a double that lacks a method the
        production path calls is a hole, and this test suite only caught the
        MCP wiring change because the hole finally showed.
        """
        self.scoped_for = tenant
        return self

    async def count(self, filt) -> int:  # noqa: ANN001
        if self._raises:
            raise self._raises
        self.request_count += 1
        self.seen.append(dict(filt.params))
        key = filt.params.get("filter.advanced") or filt.params.get("query.cond") or ""
        return self._counts.get(key, self._counts.get("*", 0))


def test_replay_reproduces_a_response_and_never_re_sends_paging_params(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    response = example_response()
    recorded = {
        (s.params.get("filter.advanced") or s.params.get("query.cond") or ""): s.result_count
        for s in response.meta.trace
    }
    fake = _FakeClient(counts=recorded)
    monkeypatch.setattr(cli, "CTGovClient", lambda *a, **k: fake)

    path = tmp_path / "r.json"
    path.write_text(json.dumps(response.model_dump(mode="json")), encoding="utf-8")

    code, out, _ = run(capsys, "replay", str(path))
    assert code == 0
    assert f"{len(response.meta.trace)} steps" in out
    assert "drifted" in out and "0 drifted" in out
    assert "DRIFT" not in out
    # countTotal/pageSize are response metadata, not query constraints; sending
    # them back would still work but would not be the same request shape.
    assert all("countTotal" not in seen and "pageSize" not in seen for seen in fake.seen)
    assert fake.request_count == len(response.meta.trace)


def test_replay_reports_drift_and_exits_nonzero(capsys, monkeypatch, tmp_path: Path) -> None:
    response = example_response()
    fake = _FakeClient(counts={"*": 0})  # every live count comes back wrong
    monkeypatch.setattr(cli, "CTGovClient", lambda *a, **k: fake)
    path = tmp_path / "r.json"
    path.write_text(json.dumps(response.model_dump(mode="json")), encoding="utf-8")

    code, out, _ = run(capsys, "replay", str(path))
    assert code == 5, "a drifted verification must not exit 0"
    assert "DRIFT" in out
    assert "recorded=" in out and "live=" in out

    # An operator who knows the registry moved can opt out of the failure.
    code, _, _ = run(capsys, "replay", str(path), "--allow-drift")
    assert code == 0

    code, out, _ = run(capsys, "replay", str(path), "--json")
    assert code == 5
    report = json.loads(out)
    assert report["drift"] == len(response.meta.trace)
    assert {s["status"] for s in report["steps"]} == {"drift"}
    assert all(s["delta"] == -s["recorded"] for s in report["steps"])


def test_replay_accepts_a_bundled_example_file(capsys, monkeypatch) -> None:
    response = example_response("02-time-series")
    recorded = {
        (s.params.get("filter.advanced") or s.params.get("query.cond") or ""): s.result_count
        for s in response.meta.trace
    }
    monkeypatch.setattr(cli, "CTGovClient", lambda *a, **k: _FakeClient(counts=recorded))
    code, _, _ = run(capsys, "replay", str(EXAMPLES / "02-time-series.json"))
    assert code == 0


def test_replay_survives_one_broken_step(capsys, monkeypatch, tmp_path: Path) -> None:
    from app.ctgov import CTGovError

    monkeypatch.setattr(
        cli, "CTGovClient", lambda *a, **k: _FakeClient(raises=CTGovError("upstream 503"))
    )
    path = tmp_path / "r.json"
    path.write_text(json.dumps(example_response().model_dump(mode="json")), encoding="utf-8")
    code, out, _ = run(capsys, "replay", str(path))
    assert code == 5
    assert "ERR" in out and "upstream 503" in out


# ---------------------------------------------------------------------------
# persona: operator on the sad paths
# ---------------------------------------------------------------------------


def test_missing_file_exits_nonzero_and_explains(capsys, tmp_path: Path) -> None:
    code, out, err = run(capsys, "replay", str(tmp_path / "nope.json"))
    assert code == 2 and "no such file" in err and out == ""

    code, _, err = run(capsys, "render", str(tmp_path / "nope.json"))
    assert code == 2 and "no such file" in err


def test_garbage_payload_is_rejected_not_rendered(capsys, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"visualization": {"type": "bar_chart"}}', encoding="utf-8")
    code, out, err = run(capsys, "render", str(bad))
    assert code == 2
    assert "not a valid QueryResponse" in err
    assert out == "", "nothing may be printed for an invalid response"


def test_unknown_example_slug_lists_the_valid_ones(capsys) -> None:
    code, _, err = run(capsys, "examples", "--slug", "99-nope")
    assert code == 2 and "unknown example" in err


def test_invalid_enum_is_rejected_by_the_parser(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["query", "melanoma phases", "--phase", "PHASE9"])
    assert exit_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_incoherent_years_exit_nonzero_before_any_network_call(capsys, monkeypatch) -> None:
    called = False

    async def _never(*args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal called
        called = True
        raise AssertionError("must not reach the network")

    monkeypatch.setattr(cli, "_run_query", _never)
    code, out, err = run(
        capsys, "query", "melanoma phases", "--start-year", "2024", "--end-year", "2020"
    )
    assert code == 2
    assert "invalid request" in err
    assert out == "" and called is False


def test_missing_subcommand_exits_nonzero(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main([])
    assert exit_info.value.code == 2


# ---------------------------------------------------------------------------
# persona: agent running a real query (pipeline faked, plumbing real)
# ---------------------------------------------------------------------------


@pytest.fixture
def faked_query(monkeypatch):
    """Replace only the pipeline, so argument parsing, output routing, exit
    codes and artifact writing are all exercised for real."""
    seen: dict = {}

    async def _fake(request, no_llm):  # noqa: ANN001
        seen["request"] = request
        seen["no_llm"] = no_llm
        return example_response(), {
            "ms": 12230, "ctgov_requests": 17, "ctgov_retries": 4, "planner": "llm",
        }

    monkeypatch.setattr(cli, "_run_query", _fake)
    return seen


def test_query_default_output_is_an_ascii_chart_with_the_contract(capsys, faked_query) -> None:
    code, out, err = run(capsys, "query", "How are melanoma trials distributed across phases?")
    assert code == 0
    assert "Melanoma trials by phase" in out
    assert "of 3,738 trials matching query.cond=melanoma" in out
    assert "BUCKETS OVERLAP" in out and "no pie" in out
    assert "no value recorded" in out
    assert "showing 3 of 1,529 citations" in out
    # cost accounting goes to stderr where it cannot corrupt the artifact
    assert "ctgov_requests=17" in err and "elapsed_ms=12230" in err
    assert "ctgov_requests" not in out


def test_query_flags_reach_the_request_and_artifacts_are_written(
    capsys, faked_query, tmp_path: Path
) -> None:
    svg_path = tmp_path / "out.svg"
    code, out, _ = run(
        capsys, "query", "phases for pembrolizumab",
        "--drug", "Pembrolizumab", "--condition", "melanoma",
        "--phase", "PHASE1", "--phase", "PHASE2",
        "--status", "RECRUITING", "--start-year", "2018", "--end-year", "2024",
        "--max-citations", "0", "--no-llm",
        "--json", "--svg", str(svg_path),
    )
    assert code == 0
    request = faked_query["request"]
    assert request.drug_name == "Pembrolizumab"
    assert request.condition == "melanoma"
    assert [p.value for p in request.phase] == ["PHASE1", "PHASE2"]
    assert [s.value for s in request.status] == ["RECRUITING"]
    assert (request.start_year, request.end_year) == (2018, 2024)
    assert request.max_citations_per_datum == 0
    assert faked_query["no_llm"] is True

    QueryResponse.model_validate(json.loads(out))
    ET.parse(svg_path)


def test_query_markdown_is_transcript_ready(capsys, faked_query) -> None:
    code, out, _ = run(capsys, "query", "melanoma phases", "--md")
    assert code == 0
    assert out.startswith("### Melanoma trials by phase")
    assert "| label |" in out
    assert "data:image/svg+xml;base64," in out
    assert "Buckets overlap" in out
    assert "<details><summary>Trace" in out


def test_query_output_is_byte_stable_across_runs(capsys, faked_query) -> None:
    _, first, _ = run(capsys, "query", "melanoma phases", "--md")
    _, second, _ = run(capsys, "query", "melanoma phases", "--md")
    assert first == second


def test_upstream_failure_exits_3_with_nothing_on_stdout(capsys, monkeypatch) -> None:
    from app.ctgov import CTGovError

    async def _boom(request, no_llm):  # noqa: ANN001
        raise CTGovError("503 after 5 retries")

    monkeypatch.setattr(cli, "_run_query", _boom)
    code, out, err = run(capsys, "query", "melanoma phases")
    assert code == 3
    assert out == ""
    assert "clinicaltrials.gov" in err and "503" in err


def test_unanswerable_query_exits_4(capsys, monkeypatch) -> None:
    async def _boom(request, no_llm):  # noqa: ANN001
        raise ValueError("plan would issue 214 probes, above the cap of 60")

    monkeypatch.setattr(cli, "_run_query", _boom)
    code, out, err = run(capsys, "query", "everything about everything")
    assert code == 4 and out == "" and "above the cap" in err
