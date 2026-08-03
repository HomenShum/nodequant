"""Generate golden.json by asking ClinicalTrials.gov directly.

The strongest available ground truth for a count question is the API itself:
for any (filter, bucket) pair the correct answer is *definitionally* the
`totalCount` the API returns for the equivalent request. So nothing here is
hand-labelled. What is hand-written is the mapping from a question to the
filters a correct answer must be measured with (`evals/cases.py`); the numbers
are then computed.

Crucially this does not go through `app.planner` or `app.executor`. It uses
`evals.api.Prober` with the literal param dicts from `evals/cases.py`. If the
executor composes filters wrongly, golden.json is unaffected and the eval
catches it.

    uv run --no-sync python -m evals.golden              # regenerate
    uv run --no-sync python -m evals.golden --cases phase_melanoma,year_covid

Counts move: ClinicalTrials.gov gains studies daily, so golden.json is a
snapshot with a timestamp, not a constant. `run_eval` reports its age and
treats staleness as a caveat on any count mismatch it finds.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from .api import Prober
from .cases import CASES, Case

GOLDEN_PATH = Path(__file__).resolve().parent / "golden.json"


def _expected_exclusivity(case: Case, total: int, bucket_sum: int) -> bool:
    """What `meta.buckets_are_exclusive` must be for this case.

    Restates the rule the executor is supposed to follow, from the case's own
    declared dimension rather than from the executor's code:

      * a multi-valued dimension can never partition, whatever the sums say;
      * a single total is trivially exclusive;
      * otherwise the buckets partition iff they actually sum to the total.

    The middle clause is the one that caught a real bug: deciding exclusivity
    by arithmetic alone lets overlap and absent values cancel and report a
    false partition.
    """
    if case.expect_dimension == "none":
        return True
    if case.expect_dimension.endswith("_network"):
        # A co-occurrence graph has no buckets at all: a trial contributes to
        # every node and edge it touches. There is nothing here to partition.
        return False
    if case.multi_valued:
        return False
    return bucket_sum == total


async def generate(cases: list[Case]) -> dict:
    async with Prober() as prober:
        records = []
        failed: list[str] = []
        for case in cases:
            try:
                total = await prober.count(case.base_params)
                bucket_counts: dict[str, int] = {}
                if case.buckets:
                    bucket_counts = await prober.count_many(case.buckets)
            except Exception as exc:  # noqa: BLE001
                # One rate-limited case must not discard the other thirty.
                # Missing cases are reported and can be topped up with --cases.
                print(f"  {case.id:38s} FAILED: {type(exc).__name__}: {exc}")
                failed.append(case.id)
                continue

            bucket_sum = sum(bucket_counts.values()) if bucket_counts else None
            records.append({
                "id": case.id,
                # Per case, because --merge tops up a subset: a file-level
                # timestamp would claim the untouched cases were re-measured.
                "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "query": case.query,
                "request": case.request,
                "expect_dimension": case.expect_dimension,
                "expect_viz": list(case.expect_viz),
                "verify": case.verify,
                "allowed_years": list(case.allowed_years),
                "notes": case.notes,
                "base_params": case.base_params,
                "total_matching": total,
                "buckets": {
                    label: {"params": case.buckets[label], "count": bucket_counts[label]}
                    for label in bucket_counts
                },
                "bucket_sum": bucket_sum,
                "expect_exclusive": _expected_exclusivity(
                    case, total, bucket_sum if bucket_sum is not None else total
                ),
            })
            print(
                f"  {case.id:38s} total={total:>8,}"
                + (f"  buckets={len(bucket_counts):>2d} sum={bucket_sum:>8,}"
                   if bucket_counts else f"  ({case.verify})")
            )

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "generator": "evals/golden.py",
            "source": "https://clinicaltrials.gov/api/v2/studies",
            "method": (
                "Every count is a live countTotal probe issued by evals/api.py with the "
                "literal param dicts hand-written in evals/cases.py. No app code was used "
                "to produce any number in this file."
            ),
            "probe_requests": prober.request_count,
            "probe_retries": prober.retry_count,
            "failed_case_ids": failed,
            "cases": records,
        }


def load() -> dict:
    if not GOLDEN_PATH.exists():
        raise FileNotFoundError(
            f"{GOLDEN_PATH} not found — run `python -m evals.golden` first"
        )
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", help="comma-separated case ids; default all")
    parser.add_argument("--out", default=str(GOLDEN_PATH))
    parser.add_argument(
        "--merge", action="store_true",
        help="keep existing entries for cases not regenerated in this run",
    )
    args = parser.parse_args()

    selected = CASES
    if args.cases:
        wanted = {c.strip() for c in args.cases.split(",") if c.strip()}
        selected = [c for c in CASES if c.id in wanted]
        missing = wanted - {c.id for c in selected}
        if missing:
            raise SystemExit(f"unknown case ids: {sorted(missing)}")

    print(f"Generating golden counts for {len(selected)} cases against the live API...")
    payload = await generate(selected)

    out = Path(args.out)
    if args.merge and out.exists():
        previous = json.loads(out.read_text(encoding="utf-8"))
        fresh = {c["id"] for c in payload["cases"]}
        # Order by the canonical case list so the file stays diff-stable.
        kept = {c["id"]: c for c in previous.get("cases", []) if c["id"] not in fresh}
        kept.update({c["id"]: c for c in payload["cases"]})
        order = [c.id for c in CASES]
        payload["cases"] = [kept[i] for i in order if i in kept]
        payload["merged_from"] = previous.get("generated_at")

    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {out} — {len(payload['cases'])} cases, "
          f"{payload['probe_requests']} probe requests, "
          f"{payload['probe_retries']} retries.")
    if payload["failed_case_ids"]:
        print("Cases that could not be measured: "
              + ", ".join(payload["failed_case_ids"])
              + "\nRetry with: python -m evals.golden --merge --cases "
              + ",".join(payload["failed_case_ids"]))


if __name__ == "__main__":
    asyncio.run(main())
