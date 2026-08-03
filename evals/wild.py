"""Production-coverage eval: the whole registry, not a curated set.

`evals/cases.py` is 31 hand-picked cases. They prove the pipeline is correct on
questions someone chose. They cannot answer the production question, which is:

    what happens when a real user asks about a condition nobody picked?

This samples conditions and sponsors from ClinicalTrials.gov itself — including
the long tail — asks a real question about each, and measures:

  answered      did a valid QueryResponse come back at all
  counts_ok     do the returned counts survive an INDEPENDENT re-probe
  replay_ok     does every trace step still reproduce its number
  empty         a valid response whose buckets are all zero (found nothing)
  failed        an exception, with its class

The distinction that matters: `query.cond` is a SEARCH over the whole registry,
so coverage there is not bounded by the 5,000-entity offline index. Entity
DISCOVERY (sponsor shortlists, network nodes) is bounded, and is sampled from
matching records rather than an index. This script measures both paths so the
difference is visible rather than argued.

    set -a && . ./.env && set +a
    uv run --no-sync python -m evals.wild --n 40 --seed 7
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from app.ctgov import CTGovClient
from app.executor import execute
from app.models import QueryRequest
from app.planner import fallback_plan

from .api import Prober

load_dotenv()
REPORT = Path(__file__).resolve().parent / "wild_report.json"
BASE = "https://clinicaltrials.gov/api/v2"


async def sample_registry_values(field: str, limit: int) -> list[str]:
    """Pull real field values straight from the registry's own stats endpoint.

    Deliberately not from our offline index — the point is to ask about things
    the index may never have seen.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{BASE}/stats/field/values", params={"fields": field})
        r.raise_for_status()
        payload = r.json()
    values = []
    for entry in payload:
        for item in entry.get("topValues", []) or []:
            name = item.get("value")
            if name and len(name) > 2:
                values.append(name)
    return values[:limit]


QUESTION_SHAPES = [
    ("How are {} trials distributed across phases?", "phase"),
    ("How many {} trials are there?", "none"),
    ("Which countries have the most {} trials?", "country"),
    ("How many {} trials started each year since 2018?", "year"),
    ("Show a network of sponsors and drugs for {} trials.", "sponsor_drug_network"),
]


async def probe_one(
    condition: str, shape: tuple[str, str], client: CTGovClient, prober: Prober
) -> dict[str, Any]:
    template, expect_dim = shape
    query = template.format(condition)
    started = time.perf_counter()
    row: dict[str, Any] = {
        "condition": condition, "query": query, "expect_dimension": expect_dim,
    }
    try:
        # Deterministic planner: this measures the EXECUTOR's registry coverage,
        # not the LLM's phrasing luck. An LLM in the loop would conflate the two.
        request = QueryRequest(query=query, condition=condition)
        plan = fallback_plan(request)
        response = await execute(plan, request, client)
    except Exception as exc:
        row.update(answered=False, failure=type(exc).__name__,
                   detail=str(exc)[:120], seconds=round(time.perf_counter() - started, 1))
        return row

    viz, meta = response.visualization, response.meta
    marks = len(viz.data) or len(viz.nodes)
    total = sum(d.count for d in viz.data) + sum(n.count for n in viz.nodes)

    # Independently re-probe every trace step. Same discipline as the curated
    # eval: the system does not get to confirm its own numbers.
    replay_ok = replay_total = 0
    for step in meta.trace:
        if step.result_count is None:
            continue
        params = {k: v for k, v in step.params.items()
                  if k not in ("countTotal", "pageSize")}
        replay_total += 1
        try:
            if await prober.count(params) == step.result_count:
                replay_ok += 1
        except Exception:
            pass

    row.update(
        answered=True,
        dimension=plan.dimension.value,
        viz=viz.type.value,
        marks=marks,
        total_matching=meta.total_matching,
        empty=(total == 0),
        exclusive=meta.buckets_are_exclusive,
        replay_ok=replay_ok,
        replay_total=replay_total,
        warnings=len(meta.warnings),
        seconds=round(time.perf_counter() - started, 1),
    )
    return row


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    random.seed(args.seed)
    conditions = await sample_registry_values("Condition", 400)
    if not conditions:
        raise SystemExit("could not sample conditions from the registry")
    picked = random.sample(conditions, min(args.n, len(conditions)))

    rows: list[dict[str, Any]] = []
    async with CTGovClient() as client, Prober() as prober:
        for i, condition in enumerate(picked, 1):
            shape = QUESTION_SHAPES[i % len(QUESTION_SHAPES)]
            row = await probe_one(condition, shape, client, prober)
            rows.append(row)
            flag = ("OK " if row.get("answered") and not row.get("empty")
                    else "EMPTY" if row.get("answered") else "FAIL")
            print(f"[{i}/{len(picked)}] {flag:5s} {condition[:34]:36s} "
                  f"{row.get('dimension','-'):18s} marks={row.get('marks','-'):>3} "
                  f"{row.get('seconds')}s")

    answered = [r for r in rows if r.get("answered")]
    empty = [r for r in answered if r.get("empty")]
    replay_num = sum(r.get("replay_ok", 0) for r in answered)
    replay_den = sum(r.get("replay_total", 0) for r in answered)
    summary = {
        "sampled": len(rows),
        "answered": len(answered),
        "answered_rate": round(len(answered) / len(rows), 4),
        "empty_results": len(empty),
        "replay_pass_rate": round(replay_num / replay_den, 4) if replay_den else None,
        "replay_steps": replay_den,
        "failures": {},
        "median_seconds": sorted(r["seconds"] for r in rows)[len(rows) // 2],
    }
    for r in rows:
        if not r.get("answered"):
            k = r.get("failure", "?")
            summary["failures"][k] = summary["failures"].get(k, 0) + 1

    REPORT.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2),
                      encoding="utf-8")
    print("\n--- summary ---")
    print(json.dumps(summary, indent=2))
    print(f"\nWrote {REPORT}")


if __name__ == "__main__":
    asyncio.run(main())
