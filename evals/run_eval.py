"""Score the pipeline against evals/golden.json.

    set -a && . ./.env && set +a
    uv run --no-sync python -m evals.run_eval --planner structured
    uv run --no-sync python -m evals.run_eval --planner graph
    uv run --no-sync python -m evals.run_eval --planner fallback

Six metrics, three of which are hard failures.

COUNT ACCURACY (hard)
    Every datum count must equal the independently-computed golden value
    exactly. Not "within tolerance" — the system's entire claim is that its
    numbers are measurements, and a measurement that is close is still wrong.

TRACE REPLAY (hard)
    The falsifiable form of the central claim. Every `meta.trace` step is
    re-issued verbatim and the returned `totalCount` compared to the number
    the response reported. If the system ever printed a figure it did not
    measure, this is where it shows.

NO FABRICATED NUMBERS (hard)
    `meta.interpretation`, `meta.assumptions` and `visualization.title` are
    the free-text surfaces. Any multi-digit run in them that is not a year the
    caller supplied is a number a model wrote.

ROUTING (soft)
    Did the planner pick the dimension and chart type the question class calls
    for? A routing miss is a quality failure, not a correctness one — the
    counts under a wrong dimension are still real counts.

SCHEMA VALIDITY (hard)
    The response revalidates against `QueryResponse`.

EXCLUSIVITY (hard)
    Multi-valued dimensions must report `buckets_are_exclusive: false`. Getting
    this wrong licenses a renderer to draw a pie chart of overlapping data.

Nothing here computes an expected count from app code. Static expectations come
from golden.json; dynamic ones (sponsor shortlists, network nodes and edges,
whose labels cannot be known in advance) are re-probed at scoring time through
`evals.api`, which shares no code path with `app.ctgov`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from app.ctgov import CTGovClient
from app.executor import execute
from app.models import QueryRequest, QueryResponse
from app.planner import Planner, QueryPlan, fallback_plan

from .api import Prober
from .cases import adv, merge
from .golden import load as load_golden

load_dotenv()

REPORT_MD = Path(__file__).resolve().parent / "report.md"
REPORT_JSON = Path(__file__).resolve().parent / "report.json"
# Prose that is not a measurement lives in its own file and is appended, so
# regenerating the report cannot quietly delete the reasoning behind it.
BENCHMARKS_MD = Path(__file__).resolve().parent / "benchmarks.md"

# Same shape the executor uses to sanitise model prose: three or more digit-ish
# characters. Deliberately identical so the eval measures the rule the system
# claims to enforce, not a stricter one of its own.
NUMERIC_RUN = re.compile(r"\d[\d,]{2,}")
YEAR_IN_TEXT = re.compile(r"\b(19|20)\d{2}\b")

# Historical guard, retained deliberately.
#
# Network traces used to summarise their fan-out in ONE synthetic step ("probe
# 12 nodes + 15 edges") whose result_count was a number of requests, not a
# totalCount — unreplayable, and counting it as a pass would have inflated the
# replay rate. This detector is what measured that gap (network trace coverage
# ~0.5). The executor now records one step per node and per edge probe, so this
# pattern should match nothing; if it ever matches again, the summarising
# behaviour has regressed and coverage will drop below 1.0 rather than silently
# passing.
NON_REPLAYABLE_STEP = re.compile(r"^probe \d+ nodes")


# --------------------------------------------------------------- planning


async def make_plan(mode: str, request: QueryRequest, planner: Any) -> QueryPlan:
    if mode == "fallback":
        return fallback_plan(request)
    assert planner is not None
    return await planner.plan(request)


def build_planner(mode: str, ctgov: CTGovClient) -> Any:
    """One planner for the whole run.

    The graph planner shares the run's `CTGovClient` rather than opening its
    own: its verification probes and the executor's measurement probes draw on
    the same undocumented upstream budget, and two clients pacing themselves
    independently is how a 429 storm starts.
    """
    if mode == "fallback":
        return None
    if mode == "graph":
        from app.agent import GraphPlanner

        return GraphPlanner(ctgov=ctgov)
    return Planner()


# ------------------------------------------------------------ probe rebuild
#
# These rebuild an independent probe for a label the system chose. They are
# written here, from the API's vocabulary, rather than imported from the
# executor — the whole point is to check the executor's composition against a
# second opinion.


def sponsor_probe(base: dict[str, str], label: str) -> dict[str, str]:
    return merge(base, adv(f'AREA[LeadSponsorName]"{label}"'))


def entity_probe(base: dict[str, str], node_type: str, label: str) -> dict[str, str]:
    area = "AREA[LeadSponsorName]" if node_type == "sponsor" else "AREA[InterventionName]"
    return merge(base, adv(f'{area}"{label}"'))


def pair_probe(base: dict[str, str], lt: str, ll: str, rt: str, rl: str) -> dict[str, str]:
    return merge(base, entity_probe({}, lt, ll), entity_probe({}, rt, rl))


# ----------------------------------------------------------------- metrics


def _allowed_year_strings(golden_case: dict) -> set[str]:
    """Years the response is permitted to mention in free text.

    A year the caller supplied is an echo, not an invention. Everything else is
    a number a model produced, and there is no legitimate reason for one to
    appear in prose composed from validated enum values.
    """
    allowed: set[str] = set()
    request = golden_case.get("request", {})
    lo, hi = request.get("start_year"), request.get("end_year")
    if lo and hi:
        allowed |= {str(y) for y in range(int(lo), int(hi) + 1)}
    for key in ("start_year", "end_year"):
        if request.get(key):
            allowed.add(str(request[key]))
    allowed |= {str(y) for y in golden_case.get("allowed_years", [])}
    # Years typed into the question itself are equally the caller's.
    allowed |= {m.group() for m in YEAR_IN_TEXT.finditer(golden_case["query"])}
    return allowed


def check_fabricated_numbers(response: QueryResponse, golden_case: dict) -> list[str]:
    allowed = _allowed_year_strings(golden_case)
    hits: list[str] = []
    surfaces = [
        ("meta.interpretation", response.meta.interpretation),
        ("visualization.title", response.visualization.title),
    ]
    surfaces += [(f"meta.assumptions[{i}]", a) for i, a in enumerate(response.meta.assumptions)]
    for where, text in surfaces:
        for match in NUMERIC_RUN.finditer(text or ""):
            token = match.group().replace(",", "")
            if token not in allowed:
                hits.append(f"{where}: {match.group()!r} in {text!r}")
    return hits


async def replay_trace(response: QueryResponse, prober: Prober) -> dict[str, Any]:
    """Re-issue every trace step and compare the API's answer to the reported one."""
    replayable: list[tuple[str, dict[str, str], int]] = []
    skipped: list[str] = []
    for step in response.meta.trace:
        if step.result_count is None or NON_REPLAYABLE_STEP.match(step.step):
            skipped.append(step.step)
            continue
        params = {k: v for k, v in step.params.items() if k not in ("countTotal", "pageSize")}
        replayable.append((step.step, params, step.result_count))

    results = await asyncio.gather(
        *(prober.count(p) for _, p, _ in replayable), return_exceptions=True
    )
    mismatches = []
    errors = []
    passed = 0
    for (name, params, reported), actual in zip(replayable, results):
        if isinstance(actual, BaseException):
            errors.append({"step": name, "error": str(actual)})
            continue
        if actual == reported:
            passed += 1
        else:
            mismatches.append({"step": name, "reported": reported,
                               "replayed": actual, "params": params})
    return {
        "steps_total": len(response.meta.trace),
        "steps_replayable": len(replayable),
        "steps_skipped": skipped,
        "passed": passed,
        "mismatches": mismatches,
        "errors": errors,
    }


async def check_counts(
    response: QueryResponse, golden_case: dict, prober: Prober,
    routing_ok: bool = True,
) -> dict[str, Any]:
    """Compare every reported number to an independently measured one.

    Counts are only comparable when routing agreed. If the planner answered a
    different question, its numbers may each be perfectly correct while matching
    nothing in the golden case, and comparing them measures the routing miss a
    second time rather than measuring accuracy.

    Observed: "How many trials does Novartis sponsor?" routed to a per-sponsor
    breakdown instead of a total. Every bucket was correct, but each was
    compared against the single golden total, turning one routing failure into
    eleven reported wrong numbers — which then buried the question of whether
    any real count was ever wrong.
    """
    problems: list[str] = []
    checked = 0

    if not routing_ok:
        return {
            "checked": 0,
            "problems": [],
            "passed": True,
            "comparable": False,
            "not_compared_reason": (
                "routing mismatch: the planner answered a differently-shaped "
                "question, so its counts are not comparable to this golden case"
            ),
        }

    if response.meta.total_matching != golden_case["total_matching"]:
        problems.append(
            f"total_matching {response.meta.total_matching} != golden "
            f"{golden_case['total_matching']}"
        )
    checked += 1

    verify = golden_case["verify"]
    base = golden_case["base_params"]

    if verify == "static" and golden_case["expect_dimension"] == "none":
        # A single total has one synthetic datum, not a bucket vocabulary. It
        # must restate the base count exactly — a `single_value` card that
        # disagrees with its own denominator is the worst version of this bug.
        for datum in response.visualization.data:
            checked += 1
            if datum.count != golden_case["total_matching"]:
                problems.append(
                    f"single value {datum.label!r}: got {datum.count}, "
                    f"golden {golden_case['total_matching']}"
                )

    elif verify == "static":
        # Comparison cases key on (series, label); everything else on label.
        actual: dict[str, int] = {}
        for datum in response.visualization.data:
            key = f"{datum.series}||{datum.label}" if datum.series else datum.label
            actual[key] = datum.count
        expected = {k: v["count"] for k, v in golden_case["buckets"].items()}
        for label, want in expected.items():
            checked += 1
            if label not in actual:
                problems.append(f"missing bucket {label!r} (expected {want})")
            elif actual[label] != want:
                problems.append(f"bucket {label!r}: got {actual[label]}, golden {want}")
        extra = set(actual) - set(expected)
        if extra:
            problems.append(f"unexpected buckets: {sorted(extra)}")

    elif verify == "sponsor_labels":
        probes = {
            d.label: sponsor_probe(base, d.label) for d in response.visualization.data
        }
        truth = await prober.count_many(probes) if probes else {}
        for datum in response.visualization.data:
            checked += 1
            if truth[datum.label] != datum.count:
                problems.append(
                    f"sponsor {datum.label!r}: got {datum.count}, probed {truth[datum.label]}"
                )

    elif verify == "country_labels":
        # Country buckets are DISCOVERED from the result set, like sponsors, so
        # their labels cannot be known in advance. Pinning a fixed list made the
        # golden set assert the executor's old hardcoded vocabulary rather than
        # the truth: 'Australia' is 4th for melanoma (305 trials) and was 10th in
        # that list. Each discovered label is re-probed independently instead.
        probes = {
            d.label: merge(base, adv(f'AREA[LocationCountry]"{d.label}"'))
            for d in response.visualization.data
        }
        truth = await prober.count_many(probes) if probes else {}
        for datum in response.visualization.data:
            checked += 1
            if truth[datum.label] != datum.count:
                problems.append(
                    f"country {datum.label!r}: got {datum.count}, probed {truth[datum.label]}"
                )

    elif verify == "network":
        if response.visualization.type.value != "network_graph":
            problems.append(
                f"expected a network_graph, got {response.visualization.type.value}"
            )
        nodes = {n.id: n for n in response.visualization.nodes}
        node_probes = {n.id: entity_probe(base, n.type, n.label) for n in nodes.values()}
        edge_probes = {
            f"{e.source}->{e.target}": pair_probe(
                base, nodes[e.source].type, nodes[e.source].label,
                nodes[e.target].type, nodes[e.target].label,
            )
            for e in response.visualization.edges
        }
        truth = await prober.count_many({**node_probes, **edge_probes})
        for node in nodes.values():
            checked += 1
            if truth[node.id] != node.count:
                problems.append(
                    f"node {node.label!r}: got {node.count}, probed {truth[node.id]}"
                )
        for edge in response.visualization.edges:
            checked += 1
            key = f"{edge.source}->{edge.target}"
            if truth[key] != edge.weight:
                problems.append(
                    f"edge {key}: got {edge.weight}, probed {truth[key]}"
                )

    return {"checked": checked, "problems": problems, "passed": not problems,
            "comparable": True}


def check_exclusivity(response: QueryResponse, golden_case: dict) -> dict[str, Any]:
    expected = golden_case["expect_exclusive"]
    actual = response.meta.buckets_are_exclusive
    multi = golden_case["expect_dimension"] in {
        "phase", "country", "intervention_type", "sponsor"
    }
    problems = []
    if multi and actual:
        problems.append(
            f"dimension {golden_case['expect_dimension']} is multi-valued but "
            f"buckets_are_exclusive=True — this licenses a pie chart of overlapping data"
        )
    elif actual != expected:
        problems.append(f"buckets_are_exclusive={actual}, golden expects {expected}")
    return {"expected": expected, "actual": actual, "problems": problems,
            "passed": not problems}


def check_schema(response: QueryResponse) -> dict[str, Any]:
    try:
        QueryResponse.model_validate(response.model_dump(mode="json"))
        return {"passed": True, "error": None}
    except Exception as exc:  # noqa: BLE001 - any validation failure is a failure
        return {"passed": False, "error": str(exc)}


# -------------------------------------------------------------- case runner


async def run_case(
    golden_case: dict, mode: str, planner: Any,
    ctgov: CTGovClient, prober: Prober,
) -> dict[str, Any]:
    started = time.perf_counter()
    result: dict[str, Any] = {"id": golden_case["id"], "query": golden_case["query"],
                              "planner": mode}
    try:
        request = QueryRequest(query=golden_case["query"], **golden_case.get("request", {}))
        plan = await make_plan(mode, request, planner)
        result["plan"] = {
            "dimension": plan.dimension.value,
            "viz_type": plan.viz_type.value,
            "compare_values": plan.compare_values,
            "compare_field": plan.compare_field,
        }
        response = await execute(plan, request, ctgov)
    except Exception as exc:  # noqa: BLE001
        result.update({
            "error": f"{type(exc).__name__}: {exc}",
            "seconds": round(time.perf_counter() - started, 2),
            "hard_pass": False,
        })
        return result

    result["viz_type"] = response.visualization.type.value
    result["routing"] = {
        "expected_dimension": golden_case["expect_dimension"],
        "actual_dimension": plan.dimension.value,
        "dimension_ok": plan.dimension.value == golden_case["expect_dimension"],
        "expected_viz": golden_case["expect_viz"],
        "actual_viz": response.visualization.type.value,
        "viz_ok": response.visualization.type.value in golden_case["expect_viz"],
    }
    # Counts are scored only when routing agreed. Under a different dimension
    # the numbers answer a different question, so comparing them re-measures the
    # routing miss instead of measuring accuracy.
    result["counts"] = await check_counts(
        response, golden_case, prober, routing_ok=result["routing"]["dimension_ok"]
    )
    result["trace_replay"] = await replay_trace(response, prober)
    fabricated = check_fabricated_numbers(response, golden_case)
    result["fabricated_numbers"] = {"hits": fabricated, "passed": not fabricated}
    result["schema"] = check_schema(response)
    result["exclusivity"] = check_exclusivity(response, golden_case)
    result["seconds"] = round(time.perf_counter() - started, 2)

    replay = result["trace_replay"]
    replay_ok = not replay["mismatches"] and not replay["errors"]
    result["hard_pass"] = (
        result["counts"]["passed"] and replay_ok
        and result["fabricated_numbers"]["passed"]
        and result["schema"]["passed"] and result["exclusivity"]["passed"]
    )
    return result


# ----------------------------------------------------------------- reporting


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(results)
    ran = [r for r in results if "error" not in r]
    replay_pass = sum(r["trace_replay"]["passed"] for r in ran)
    replay_total = sum(r["trace_replay"]["steps_replayable"] for r in ran)
    replay_skipped = sum(len(r["trace_replay"]["steps_skipped"]) for r in ran)
    replay_all = sum(r["trace_replay"]["steps_total"] for r in ran)
    counts_checked = sum(r["counts"]["checked"] for r in ran)
    comparable = [r for r in ran if r["counts"].get("comparable", True)]
    counts_bad = sum(len(r["counts"]["problems"]) for r in comparable)

    def rate(num: int, den: int) -> float | None:
        return round(num / den, 4) if den else None

    return {
        "cases": n,
        "cases_errored": n - len(ran),
        "hard_pass": sum(1 for r in results if r.get("hard_pass")),
        "hard_pass_rate": rate(sum(1 for r in results if r.get("hard_pass")), n),
        # Only cases whose routing agreed contribute to count accuracy. Scoring
        # a routing miss as N wrong numbers double-counts one failure and hides
        # whether any real count was ever wrong.
        "count_accuracy": {
            "cases_compared": len(comparable),
            "cases_not_compared": len(ran) - len(comparable),
            "cases_passed": sum(1 for r in comparable if r["counts"]["passed"]),
            "cases_rate": rate(
                sum(1 for r in comparable if r["counts"]["passed"]), len(comparable)
            ),
            "numbers_checked": counts_checked,
            "numbers_wrong": counts_bad,
        },
        "trace_replay": {
            "steps_in_traces": replay_all,
            "steps_replayable": replay_total,
            "steps_non_replayable": replay_skipped,
            "steps_passed": replay_pass,
            "replay_pass_rate": rate(replay_pass, replay_total),
            "trace_coverage": rate(replay_total, replay_all),
        },
        "routing": {
            "dimension_rate": rate(sum(1 for r in ran if r["routing"]["dimension_ok"]), len(ran)),
            "viz_rate": rate(sum(1 for r in ran if r["routing"]["viz_ok"]), len(ran)),
        },
        "no_fabricated_numbers_rate": rate(
            sum(1 for r in ran if r["fabricated_numbers"]["passed"]), len(ran)
        ),
        "schema_valid_rate": rate(sum(1 for r in ran if r["schema"]["passed"]), len(ran)),
        "exclusivity_rate": rate(sum(1 for r in ran if r["exclusivity"]["passed"]), len(ran)),
        "median_seconds": (
            sorted(r["seconds"] for r in ran)[len(ran) // 2] if ran else None
        ),
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def write_reports(payload: dict[str, Any]) -> None:
    REPORT_JSON.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    agg = payload["aggregate"]
    lines: list[str] = []
    add = lines.append
    add("# Evaluation report")
    add("")
    add(f"Generated {payload['run_at']} · planner `{payload['planner']}` · "
        f"model `{payload['model']}`")
    add(f"Golden set generated {payload['golden_generated_at']} "
        f"({payload['golden_age_hours']}h before this run).")
    add("")
    add("## Aggregate")
    add("")
    add("| Metric | Value |")
    add("|---|---|")
    add(f"| Cases | {agg['cases']} ({agg['cases_errored']} errored) |")
    add(f"| **Hard pass** (all hard metrics) | **{agg['hard_pass']}/{agg['cases']} — "
        f"{_pct(agg['hard_pass_rate'])}** |")
    add(f"| Count accuracy (cases) | {_pct(agg['count_accuracy']['cases_rate'])} |")
    add(f"| Numbers checked / wrong | {agg['count_accuracy']['numbers_checked']} / "
        f"{agg['count_accuracy']['numbers_wrong']} |")
    add(f"| **Trace replay pass rate** | **{_pct(agg['trace_replay']['replay_pass_rate'])}** "
        f"({agg['trace_replay']['steps_passed']}/"
        f"{agg['trace_replay']['steps_replayable']} steps) |")
    add(f"| Trace coverage (replayable / all steps) | "
        f"{_pct(agg['trace_replay']['trace_coverage'])} "
        f"({agg['trace_replay']['steps_non_replayable']} non-replayable) |")
    add(f"| Routing — dimension | {_pct(agg['routing']['dimension_rate'])} |")
    add(f"| Routing — viz type | {_pct(agg['routing']['viz_rate'])} |")
    add(f"| No fabricated numbers | {_pct(agg['no_fabricated_numbers_rate'])} |")
    add(f"| Schema valid | {_pct(agg['schema_valid_rate'])} |")
    add(f"| Exclusivity correct | {_pct(agg['exclusivity_rate'])} |")
    add(f"| Median case latency | {agg['median_seconds']}s |")
    add("")
    add("## Per case")
    add("")
    add("| Case | Dim (want→got) | Viz | Counts | Replay | Fab | Excl | Hard | s |")
    add("|---|---|---|---|---|---|---|---|--:|")
    for r in payload["results"]:
        if "error" in r:
            add(f"| `{r['id']}` | — | — | — | — | — | — | ERROR | {r.get('seconds', '')} |")
            continue
        rt = r["routing"]
        tr = r["trace_replay"]
        add(
            f"| `{r['id']}` "
            f"| {rt['expected_dimension']}→{rt['actual_dimension']} "
            f"{'ok' if rt['dimension_ok'] else 'MISS'} "
            f"| {rt['actual_viz']} {'ok' if rt['viz_ok'] else 'MISS'} "
            f"| {'ok' if r['counts']['passed'] else 'FAIL(' + str(len(r['counts']['problems'])) + ')'} "
            f"| {tr['passed']}/{tr['steps_replayable']} "
            f"| {'ok' if r['fabricated_numbers']['passed'] else 'FAIL'} "
            f"| {'ok' if r['exclusivity']['passed'] else 'FAIL'} "
            f"| {'PASS' if r['hard_pass'] else 'FAIL'} | {r['seconds']} |"
        )
    add("")

    failures = [r for r in payload["results"] if not r.get("hard_pass")]
    if failures:
        add("## Failures in detail")
        add("")
        for r in failures:
            add(f"### `{r['id']}`")
            add("")
            if "error" in r:
                add(f"- errored: {r['error']}")
                add("")
                continue
            for problem in r["counts"]["problems"]:
                add(f"- count: {problem}")
            for mismatch in r["trace_replay"]["mismatches"]:
                add(f"- replay: {mismatch['step']} reported "
                    f"{mismatch['reported']}, replayed {mismatch['replayed']}")
            for err in r["trace_replay"]["errors"]:
                add(f"- replay error: {err['step']}: {err['error']}")
            for hit in r["fabricated_numbers"]["hits"]:
                add(f"- fabricated number: {hit}")
            for problem in r["exclusivity"]["problems"]:
                add(f"- exclusivity: {problem}")
            if not r["schema"]["passed"]:
                add(f"- schema: {r['schema']['error']}")
            add("")

    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------- main


async def main() -> None:
    parser = argparse.ArgumentParser(description="Score the pipeline against golden.json")
    parser.add_argument("--planner", default="structured",
                        choices=["structured", "graph", "fallback"])
    parser.add_argument("--cases", help="comma-separated case ids; default all")
    parser.add_argument("--limit", type=int, default=0, help="run only the first N cases")
    parser.add_argument("--no-report", action="store_true")
    args = parser.parse_args()

    golden = load_golden()
    cases = golden["cases"]
    if args.cases:
        wanted = {c.strip() for c in args.cases.split(",")}
        cases = [c for c in cases if c["id"] in wanted]
    if args.limit:
        cases = cases[: args.limit]

    # Age of the OLDEST case, not of the file: `--merge` tops up a subset, so a
    # file-level timestamp would hide a stale case behind a fresh sibling.
    stamps = [
        datetime.fromisoformat(c.get("measured_at", golden["generated_at"]))
        for c in cases
    ] or [datetime.fromisoformat(golden["generated_at"])]
    age_hours = round(
        (datetime.now(timezone.utc) - min(stamps)).total_seconds() / 3600, 1
    )
    if age_hours > 24:
        print(f"WARNING: golden.json is {age_hours}h old. ClinicalTrials.gov gains "
              f"studies daily; regenerate before trusting count mismatches.")

    if args.planner in ("structured", "graph") and not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit(f"--planner {args.planner} needs OPENAI_API_KEY in the environment")

    results: list[dict[str, Any]] = []
    async with CTGovClient() as ctgov, Prober() as prober:
        planner = build_planner(args.planner, ctgov)
        for i, case in enumerate(cases, 1):
            print(f"[{i}/{len(cases)}] {case['id']} ...", end="", flush=True)
            result = await run_case(case, args.planner, planner, ctgov, prober)
            results.append(result)
            if "error" in result:
                print(f" ERROR {result['error']}")
            else:
                print(f" {'PASS' if result['hard_pass'] else 'FAIL'} ({result['seconds']}s)")

    payload = {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "planner": args.planner,
        "model": os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")
        if args.planner != "fallback" else "none (keyword planner)",
        "golden_generated_at": golden["generated_at"],
        "golden_age_hours": age_hours,
        "aggregate": aggregate(results),
        "results": results,
    }
    agg = payload["aggregate"]
    print("\n--- aggregate ---")
    print(json.dumps(agg, indent=2))
    if not args.no_report:
        write_reports(payload)
        print(f"\nWrote {REPORT_MD} and {REPORT_JSON}")


if __name__ == "__main__":
    asyncio.run(main())
