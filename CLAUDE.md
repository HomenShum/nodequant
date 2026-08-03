# Working in this repo

Read this before writing code here. It is short because only a few things
matter, and each one was learned by being wrong first.

## The invariant

**The model plans WHICH questions to ask the API. It never answers them.**

Every number that reaches a user is a `totalCount` from ClinicalTrials.gov. No
count is summed, estimated, interpolated, or produced by an LLM. If you find
yourself computing a number rather than probing for it, stop — you are about to
break the only property this system actually promises.

The falsifiable form of that invariant is **trace replay**: every step in
`meta.trace` carries the literal URL and params, and re-issuing it must return
the same number. `evals/run_eval.py` re-probes through `evals/api.py`, which
shares no code path with `app/ctgov.py`, so the system never confirms its own
arithmetic.

## The gate: adversarial review is not optional

Every change here goes through an adversarial pass before it is called done.
This is a standing rule, not a per-task instruction, because the single most
common failure of a coding agent is claiming success regardless of reality.

Run it with the `fable-judge` skill, whose stance is fixed: **a report is a set
of claims, not evidence.** Point it at the work and make it hunt specifically
for:

- **Weakened checks** — assertions loosened, expected values edited to match new
  behaviour, tests skipped or deleted. A changed test is guilty until its
  justification traces to a spec or a measurement.
- **Unwired mechanisms** — a module with passing tests that nothing constructs.
  This repo has shipped that exact bug: `app/tenancy.py` was correct, tested,
  documented in the README as closing a 31,377x timing channel, and completely
  unreachable. Tests passing is not the same as code running.
- **Claims measured before the code changed** — an eval number quoted from a run
  that predates the integration it is being used to justify.
- **Reasoned-about vs. observed** — anything declared verified that was only
  read, not run.

The verdict must be VERIFIED, VERIFIED WITH CAVEATS, or REFUTED, evidence first.
A REFUTED verdict on your own work is the gate doing its job; suppressing it to
look finished is the failure the gate exists to catch.

## Capture the surface, every session

`EVIDENCE.md` indexes every user-facing surface with the screenshot that proves
it works and the script that regenerates it. Append to it as you work; do not
assemble it at the end, because evidence gathered at the end only covers what
you remembered to look at.

**A surface with no capture has not been QA'd, however green its tests are.**
Two user-visible defects here were found only because someone asked for a
screenshot that did not exist: the chat network view had no interactive map
(the renderer existed, reachable only via MCP), and "New Thread" is a no-op
because a thread-list UI sits on a single-thread runtime. Neither had a failing
test. Both are obvious in one screenshot.

Restart the server before capturing. A process older than the change produces
evidence about a tree that no longer exists.

## Measure, then claim

Never assert a number you did not just produce. Falsified beliefs live in
`MEASUREMENTS.md`, each with the belief, the measurement that killed it, and the
change it forced. Add to it rather than quietly correcting a number, because the
list of things that turned out to be wrong is more useful to the next reader
than the current values alone.

Things measured here that contradict intuition, so you do not re-derive them:

- A synchronous mock transport **hides** the single-flight race — 8 concurrent
  calls collapse to 1 request and the bug reports itself as already fixed. Use a
  transport that actually `await`s.
- Jitter does **not** fix quota unfairness (Jain 0.139 → 0.150). Only arrival
  ordering does (→ 0.986).
- `max_tenant_share` bounds **in-flight** cost, which a tenant that acquires and
  releases immediately never exceeds. It cannot fix starvation on its own.
- Bucket overlap and absent values can cancel exactly, so
  `sum(buckets) == total` does **not** prove buckets are exclusive.
  `buckets_are_exclusive` is a property of the DIMENSION, asserted from the
  API's field semantics, not an arithmetic check.
- Citation fields and entity fields are different sets. Reusing the citation
  field list for entity extraction omits `InterventionName` and silently yields
  0-edge network graphs.

## Layout

    app/ctgov.py     count-probe client: LRU cache, single-flight, pacing, retry
    app/tenancy.py   per-tenant caches over a shared FIFO-ordered quota
    app/planner.py   question -> Dimension + viz type (LLM, with a deterministic fallback)
    app/executor.py  Dimension -> probes -> Visualization + trace
    app/models.py    every field cites the design-dna rule that forced it
    evals/           independent re-probe; run_eval.py, wild.py, ramp.py
    docs/            audits and attack reports, each with its measurements

## Running things

    uv run --no-sync pytest -q                      # 438 passed, 29 skipped
    set -a && . ./.env && set +a
    uv run --no-sync python -m evals.run_eval       # curated cases, independent re-probe
    uv run --no-sync python -m evals.wild --n 40    # registry coverage, not curated
    uv run --no-sync python -m evals.ramp           # concurrency ramp, no live API

`docs/AGENT_PLAYBOOK.md` has the prompts that produced this repo, including the
ones that caught real defects. Reuse them; they are the reproduction recipe.
