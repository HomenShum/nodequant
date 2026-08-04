# Production path — mapped to the 2026 senior-agent checklist — 2026-08-04

Source checklist: [HomenShum/2026-senior-agent-engineering-checklist](https://github.com/HomenShum/2026-senior-agent-engineering-checklist).
Its rule: every item filled before shipping. Absence declared with a rationale
is a pass. Silent absence is a fail.

That rule is this repo's own promotion-gate idea, applied to the service. So
this file is written as a gate. Each item: HAVE, PARTIAL, MISSING, or
DECLARED-ABSENT. Every HAVE names its file. Every number was measured, not
recalled.

Two paths ship separately:

- **Demo path**: deterministic kernel, frozen fixture, no LLM, no services.
  Deployable now. See the caveats in the session log.
- **Production path**: the agent runs, real bars flow, actions have risk.
  This file. Nothing here ships until its area is green or declared.

---

## Area 1 — Goal translation and human-in-the-loop

| Item | Status | Evidence / gap |
|---|---|---|
| Business goal in stakeholder's words | HAVE | README line 1: agent may investigate, never author a number |
| Quantified target clause | PARTIAL | Promotion gate has exact criteria. No `metric/comparator/value` clause for the *service* itself |
| Actions risk-tiered, high = suspend-approve | HAVE (structure) | The promotion gate IS suspend-approve: BLOCKED until a human promotes. Live trade actions do not exist yet, so the runtime suspension point is unbuilt |
| No high-risk actions declared | HAVE | True today and stated: kernel routes are read-only replay |
| Long-horizon decomposition | HAVE | `research_loop.py` bounded DAG, `--max-experiments` |

**To build:** one `metric/comparator/value` clause per surface. Example:
`review-p99-ms / <= / 500`. Put them in the gate file, not in prose.

## Area 2 — Fault-tolerant tooling

| Item | Status | Evidence / gap |
|---|---|---|
| Raw errors intercepted | PARTIAL | Review route has a boundary (uncommitted). `kernel_chat` NOT VERIFIED. TrialScope routes return typed 502/422 |
| Backoff with base and cap | HAVE | `app/ctgov.py`: 12 backoff/retry sites, paced client |
| NAMED fallback | HAVE | `fallback_plan()` — keyword planner when the LLM is absent or fails. The response says which planner ran |
| Tool contracts with examples | PARTIAL | MCP server has schemas + examples. Market-data provider adapter does not exist yet |

**To build:** error boundary on every SSE generator, one pattern, one test
each. Provider adapter gets backoff + a named fallback (cached snapshot,
labelled stale) on day one.

## Area 3 — Context control

| Item | Status | Evidence / gap |
|---|---|---|
| Named compression strategy | HAVE | `app/memory.py`: bounded, evicting, carries filters never counts |
| Token budget per run | MISSING | No budget anywhere. Grep confirms |
| `none` with rationale | AVAILABLE | Kernel path uses zero tokens. That is a valid declaration TODAY and must be written down before it silently stops being true |

**To build:** a declared budget the day the planner joins the kernel path.
Until then: the written `none` rationale.

## Area 4 — Runtime guards and the three golden metrics

| Item | Status | Evidence / gap |
|---|---|---|
| `task-completion-rate` ≥ 0.95 | MISSING | Not instrumented by name |
| `tool-call-error-rate` ≤ 0.001 | MISSING | Not instrumented by name |
| `p99-latency-ms` ≤ 500 | PARTIAL | Measured by hand 2026-08-04: review route 11,270→3ms after caching one pure computation. Not instrumented, not continuous |
| Loop breaker | HAVE | `app/agent.py`: `MAX_SUBQUESTIONS = 2`, `MAX_REVISIONS = 2`. Research DAG bounded |
| Circuit breaker | MISSING | No error-rate trip anywhere |
| Cost fuse | DECLARED-ABSENT (valid today) | Kernel path calls no LLM. Becomes MANDATORY with the planner |

**To build, in order:** (1) authenticated `/metrics` with the three names —
the health endpoint deliberately refuses counters because unauthenticated
counters are a cross-tenant oracle; that decision stands, so metrics need
auth. (2) circuit breaker on the provider adapter. (3) cost fuse with the
planner.

## Area 5 — Release engineering

| Item | Status | Evidence / gap |
|---|---|---|
| CI | **MISSING** | No `.github/workflows`. 763 tests exist and run nowhere automatically. The largest single gap in this file |
| Judge-backed regression | MISSING | The eval harness exists (`evals/`) and runs by hand |
| Canary + automatic rollback | MISSING | Nothing is deployed |
| Pinned model id + eval before swap | PARTIAL | Kernel: no model, trivially pinned. Planner model comes from env, unpinned. The audit already flagged unpinned hosted models as the one unhashed dependency |
| One trace id end to end | PARTIAL | Every artifact carries content hashes; no *request* trace id joins them |

**To build, in order:** (1) CI on push: suite + tsc + the three self-check
modules. One workflow file. Everything else in this area depends on it.
(2) request trace id threaded through SSE frames and receipts. (3) the rest
follows deployment.

---

## The domain ladder (runs in parallel with the checklist)

The checklist covers the *service*. The *product* has its own gate — the
promotion blockers, today, exactly four:

```
SYNTHETIC_CORPUS                 -> real bars via the conformance runner
MARKET_DATA_CONFORMANCE_NOT_RUN  -> gap/halt/split/revision suite on a real export
ORACLE_NOT_INDEPENDENT_ENOUGH    -> separate-agent oracle from the frozen spec
SPLIT_DISCIPLINE_NOT_PROVEN      -> immutable split-access provenance
```

Spread, slippage, and the order journal already landed. The temporal-authority
kernel is in flight in the tree. Storage (Parquet/ArcticDB) lands with real
bars — no store before there is data worth storing.

## Order of work, combined

1. **CI.** One file. Everything else assumes it.
2. Error boundaries on all SSE routes + the golden-metric clauses in a gate file.
3. Authenticated metrics endpoint, three names exact.
4. Real bars + conformance runner (clears two blockers; brings storage).
5. Separate-agent oracle (clears one blocker).
6. Provider adapter with backoff, fallback, circuit breaker.
7. Deploy demo path behind a proxy rate limit; measure the golden metrics live.
8. Planner joins the kernel path WITH cost fuse + pinned model + eval-before-swap, not before.
9. Canary + rollback once traffic exists to canary.

The declared-absent items are honest today and expire the day the planner or
live orders arrive. This file is the record of which is which.
