"""`trialscope` — the CLI surface.

Written for an agent first and a human second. That means:

  * stdout is the artifact and nothing else; every log line, timing and
    diagnostic goes to stderr, so `trialscope query ... --json | jq` is safe.
  * no spinners, no colour, no cursor control, ever. Output is byte-stable for
    the same input, so an agent can diff two runs.
  * a non-zero exit status on every failure path, with the reason on stderr.
  * `--help` on every subcommand states cost and latency, because a network
    query here is 12-40s and an agent needs to budget for that.

The CLI runs the same planner and executor as the HTTP service. There is no
second implementation that could disagree with the first about a number.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from .ctgov import CTGovClient, CTGovError, Filter
from .models import QueryRequest, QueryResponse, TrialPhase, TrialStatus
from .planner import Planner, PlannerError, fallback_plan
from .neuromap import render_neuromap
from .render import render_ascii, render_markdown, render_svg

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"

EPILOG = """\
cost model (read this before batching calls)
  query      1 LLM call (skippable with --no-llm) + 12-57 ClinicalTrials.gov
             count-probes. 12s for a distribution, 20-25s for a comparison or
             a country breakdown, 30-40s for a network graph. Rate-limited
             upstream; the client paces itself and retries 429s.
  replay     re-issues every step in meta.trace. Same order of magnitude as the
             original query, no LLM.
  schema     offline, instant.
  examples   offline, instant. Use these to develop a renderer with no network.

the one invariant
  The model plans; it never answers. Every count is a ClinicalTrials.gov
  totalCount probe recorded in meta.trace and replayable with `trialscope
  replay`.

rendering contract (enforced by --svg / --md / default output)
  1. meta.buckets_are_exclusive == false  =>  no pie, no 100% stacked bar.
     A reconciliation strip stating bucket_sum vs total_matching is drawn.
  2. is_unbucketed marks are rendered in reserved grey WITH a hatch, never
     dropped.
  3. "showing N of M" wherever returned < total or citations < citation_total.
  4. meta.denominator_note sits next to the chart.
"""


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _load_response(path: Path) -> QueryResponse:
    """Accept either a bare QueryResponse or an example file (`{request,response}`)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if "response" in raw and "visualization" not in raw:
        raw = raw["response"]
    return QueryResponse.model_validate(raw)


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------


async def _run_query(request: QueryRequest, no_llm: bool) -> tuple[QueryResponse, dict[str, Any]]:
    """The full pipeline, out of process from FastAPI.

    Returns the response plus a stats dict that mirrors what /api/health would
    report for this one call, so the caller can see what it cost.
    """
    from .executor import execute  # imported here to keep `--help` fast

    started = time.monotonic()
    async with CTGovClient() as client:
        use_llm = bool(os.environ.get("OPENAI_API_KEY")) and not no_llm
        used_fallback = True
        plan = None
        if use_llm:
            try:
                plan = await Planner().plan(request)
                used_fallback = False
            except (PlannerError, Exception) as exc:  # noqa: BLE001 - degrade, don't fail
                _err(f"planner: falling back to keywords ({type(exc).__name__}: {exc})")
        if plan is None:
            plan = fallback_plan(request)

        response = await execute(plan, request, client)
        stats = {
            "ms": int((time.monotonic() - started) * 1000),
            "ctgov_requests": client.request_count,
            "ctgov_retries": client.retry_count,
            "planner": "keyword-fallback" if used_fallback else "llm",
        }

    if used_fallback:
        response.meta.assumptions.append(
            "Planned by the deterministic keyword fallback; no LLM was used."
        )
    return response, stats


def _request_from_args(args: argparse.Namespace) -> QueryRequest:
    return QueryRequest(
        query=args.query,
        drug_name=args.drug,
        condition=args.condition,
        sponsor=args.sponsor,
        location=args.location,
        phase=[TrialPhase(p) for p in args.phase] if args.phase else None,
        status=[TrialStatus(s) for s in args.status] if args.status else None,
        start_year=args.start_year,
        end_year=args.end_year,
        max_citations_per_datum=args.max_citations,
    )


def cmd_query(args: argparse.Namespace) -> int:
    try:
        request = _request_from_args(args)
    except ValueError as exc:
        _err(f"invalid request: {exc}")
        return 2

    try:
        response, stats = asyncio.run(_run_query(request, args.no_llm))
    except CTGovError as exc:
        _err(f"clinicaltrials.gov: {exc}")
        return 3
    except ValueError as exc:
        _err(f"could not answer that question: {exc}")
        return 4
    except KeyboardInterrupt:  # pragma: no cover
        _err("interrupted")
        return 130

    _err(
        f"planner={stats['planner']} ctgov_requests={stats['ctgov_requests']} "
        f"retries={stats['ctgov_retries']} elapsed_ms={stats['ms']}"
    )

    if args.svg:
        Path(args.svg).write_text(render_svg(response, theme=args.theme), encoding="utf-8")
        _err(f"wrote {args.svg}")

    if args.json:
        print(json.dumps(response.model_dump(mode="json"), indent=2, sort_keys=False))
    elif args.md:
        print(render_markdown(response))
    else:
        print(render_ascii(response))
    return 0


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


def cmd_schema(args: argparse.Namespace) -> int:
    payload = {
        "request": QueryRequest.model_json_schema(),
        "response": QueryResponse.model_json_schema(),
    }
    if args.which != "both":
        payload = {args.which: payload[args.which]}
    print(json.dumps(payload, indent=2))
    return 0


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


async def _replay(response: QueryResponse) -> list[dict[str, Any]]:
    """Re-issue every recorded trace step and compare.

    This is the honesty check the whole design is built around: if a number in
    the response cannot be reproduced by replaying the literal request that
    produced it, the response is wrong, and this says so out loud.
    """
    results: list[dict[str, Any]] = []
    async with CTGovClient() as client:
        for step in response.meta.trace:
            row: dict[str, Any] = {
                "step": step.step,
                "recorded": step.result_count,
                "params": step.params,
            }
            if step.result_count is None:
                row.update(status="skipped", reason="no count recorded for this step")
                results.append(row)
                continue
            params = {k: v for k, v in step.params.items() if k not in ("countTotal", "pageSize")}
            try:
                live = await client.count(Filter(params))
            except (CTGovError, httpx.HTTPError) as exc:
                row.update(status="error", live=None, reason=str(exc))
            else:
                row.update(
                    status="match" if live == step.result_count else "drift",
                    live=live,
                    delta=live - step.result_count,
                )
            results.append(row)
    return results


def cmd_replay(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        _err(f"no such file: {path}")
        return 2
    try:
        response = _load_response(path)
    except Exception as exc:  # noqa: BLE001
        _err(f"not a valid QueryResponse: {exc}")
        return 2

    rows = asyncio.run(_replay(response))
    drift = [r for r in rows if r["status"] == "drift"]
    errors = [r for r in rows if r["status"] == "error"]

    if args.json:
        print(json.dumps({"steps": rows, "drift": len(drift), "errors": len(errors)}, indent=2))
    else:
        print(f"replay {path.name} — {len(rows)} steps")
        for row in rows:
            mark = {"match": "ok  ", "drift": "DRIFT", "error": "ERR ", "skipped": "skip"}[
                row["status"]
            ]
            live = row.get("live")
            print(
                f"  {mark} {row['step']:<34} recorded={row['recorded']!s:>8} "
                f"live={live!s:>8}"
                + (f"  ({row['delta']:+,})" if row.get("delta") else "")
                + (f"  {row.get('reason', '')}" if row["status"] == "error" else "")
            )
        print(
            f"\n{len(rows) - len(drift) - len(errors)} reproduced · "
            f"{len(drift)} drifted · {len(errors)} errored"
        )
        if drift:
            print(
                "drift is expected over time: ClinicalTrials.gov gains and loses studies daily. "
                "It is a correctness problem only if the shape of the answer changed."
            )
    return 0 if not errors and (not drift or args.allow_drift) else 5


# ---------------------------------------------------------------------------
# examples
# ---------------------------------------------------------------------------


def cmd_examples(args: argparse.Namespace) -> int:
    index_path = EXAMPLES_DIR / "index.json"
    if not index_path.exists():
        _err(f"no examples bundled at {EXAMPLES_DIR}")
        return 2
    index = json.loads(index_path.read_text(encoding="utf-8"))

    if args.slug:
        match = next((e for e in index if e["slug"] == args.slug), None)
        if match is None:
            _err(f"unknown example {args.slug!r}; run `trialscope examples` to list them")
            return 2
        response = _load_response(EXAMPLES_DIR / f"{args.slug}.json")
        if args.svg:
            Path(args.svg).write_text(render_svg(response), encoding="utf-8")
            _err(f"wrote {args.svg}")
        if args.json:
            print(json.dumps(response.model_dump(mode="json"), indent=2))
        elif args.md:
            print(render_markdown(response))
        else:
            print(render_ascii(response))
        return 0

    if args.json:
        print(json.dumps(index, indent=2))
        return 0

    print(f"{len(index)} bundled runs — real output, captured live, no network needed to view")
    print(f"{'slug':<24} {'viz':<18} {'marks':>5} {'edges':>5} {'matched':>9} {'excl':>5} "
          f"{'calls':>5} {'ms':>6}")
    for entry in index:
        print(
            f"{entry['slug']:<24} {entry['viz']:<18} {entry['marks']:>5} {entry['edges']:>5} "
            f"{entry['total_matching']:>9,} {str(entry['exclusive']):>5} "
            f"{entry['api_calls']:>5} {entry['ms']:>6}"
        )
    for entry in index:
        print(f"\n  {entry['slug']}\n    {entry['query']}")
    print("\nrender one:  trialscope examples --slug 01-distribution-phases --svg chart.svg")
    return 0


# ---------------------------------------------------------------------------
# render (bonus: re-render a saved response without re-querying)
# ---------------------------------------------------------------------------


def cmd_render(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        _err(f"no such file: {path}")
        return 2
    try:
        response = _load_response(path)
    except Exception as exc:  # noqa: BLE001
        _err(f"not a valid QueryResponse: {exc}")
        return 2
    text = {
        "svg": lambda: render_svg(response, theme=args.theme),
        "markdown": lambda: render_markdown(response),
        "ascii": lambda: render_ascii(response),
    }[args.format]()
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        _err(f"wrote {args.out}")
    else:
        print(text)
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trialscope",
        description=(
            "ClinicalTrials.gov natural-language question -> visualization spec + chart. "
            "Every count is an API totalCount probe; no number is produced by a model."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version="trialscope 0.1.0")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    q = sub.add_parser(
        "query",
        help="ask a question (NETWORK, 12-40s)",
        description=(
            "Ask a natural-language question. Costs one LLM planning call and "
            "12-57 ClinicalTrials.gov count-probes; 12s typical, 40s for a network "
            "graph. Default output is an ASCII chart; use --json for the raw "
            "QueryResponse, --md for a transcript-ready markdown block with an "
            "inline chart, --svg to write a standalone SVG."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    q.add_argument("query", help="the question, 3-500 characters")
    q.add_argument("--json", action="store_true", help="print the raw QueryResponse JSON")
    q.add_argument("--md", action="store_true", help="print markdown + inline data-URI SVG")
    q.add_argument("--svg", metavar="PATH", help="also write a standalone SVG here")
    q.add_argument(
        "--theme", choices=["light", "dark", "auto"], default="auto",
        help="SVG colour scheme; auto follows the viewer (default)",
    )
    q.add_argument("--no-llm", action="store_true",
                   help="skip the LLM and use the deterministic keyword planner")
    q.add_argument("--drug", help="constrain intervention (query.intr)")
    q.add_argument("--condition", help="constrain condition (query.cond)")
    q.add_argument("--sponsor", help="constrain lead sponsor (query.spons)")
    q.add_argument("--location", help="constrain location (query.locn)")
    q.add_argument("--phase", action="append", choices=[p.value for p in TrialPhase],
                   help="restrict to this phase; repeatable")
    q.add_argument("--status", action="append", choices=[s.value for s in TrialStatus],
                   help="restrict to this status; repeatable")
    q.add_argument("--start-year", type=int, dest="start_year")
    q.add_argument("--end-year", type=int, dest="end_year")
    q.add_argument("--max-citations", type=int, default=3, dest="max_citations",
                   help="citations per datum, 0-10 (0 disables citation fetching, ~40%% faster)")
    q.set_defaults(func=cmd_query)

    s = sub.add_parser(
        "schema", help="print the JSON Schemas (offline, instant)",
        description="Print the request and response JSON Schemas. No network, no LLM.",
    )
    s.add_argument("--which", choices=["request", "response", "both"], default="both")
    s.set_defaults(func=cmd_schema)

    r = sub.add_parser(
        "replay", help="re-issue meta.trace and verify counts (NETWORK)",
        description=(
            "Re-issue every request recorded in meta.trace and compare the live "
            "count to the recorded one. Exits 5 if any step drifted or errored. "
            "Costs one upstream request per trace step, no LLM."
        ),
    )
    r.add_argument("path", help="a QueryResponse JSON file, or an examples/*.json file")
    r.add_argument("--json", action="store_true", help="machine-readable verification report")
    r.add_argument("--allow-drift", action="store_true",
                   help="exit 0 even when counts moved (the registry changes daily)")
    r.set_defaults(func=cmd_replay)

    e = sub.add_parser(
        "examples", help="list or render the bundled example runs (offline)",
        description=(
            "Five real runs captured against the live API, checked into examples/. "
            "Offline. Use these to build and test a renderer with no network and no key."
        ),
    )
    e.add_argument("--slug", help="render one example instead of listing all")
    e.add_argument("--json", action="store_true")
    e.add_argument("--md", action="store_true")
    e.add_argument("--svg", metavar="PATH")
    e.set_defaults(func=cmd_examples)

    d = sub.add_parser(
        "render", help="re-render a saved response (offline)",
        description="Turn a saved QueryResponse JSON into an artifact. No network.",
    )
    d.add_argument("path")
    d.add_argument("--format", choices=["svg", "markdown", "ascii"], default="ascii")
    d.add_argument("--theme", choices=["light", "dark", "auto"], default="auto")
    d.add_argument("--out", metavar="PATH", help="write here instead of stdout")
    d.set_defaults(func=cmd_render)

    return parser


def _force_utf8() -> None:
    """Windows consoles default to cp1252 and would raise on the bar glyphs.

    An artifact that renders on one machine and crashes on another is not an
    artifact, so the encoding is pinned rather than inherited. `errors=replace`
    keeps a legacy code page degrading to '?' instead of aborting mid-chart.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):  # pragma: no cover
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    args = build_parser().parse_args(argv)
    if getattr(args, "theme", None) == "auto":
        args.theme = None
    try:
        return int(args.func(args))
    except BrokenPipeError:  # pragma: no cover - `| head`
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
