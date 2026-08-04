# Handoff — 2026-08-03

For the next coding agent. Read this before touching anything; several of the
traps below cost hours to find and are easy to reintroduce.

## What this is

NodeQuant: an options/equity research and execution surface where **an AI agent
may investigate and challenge, but may never author a number.** Every displayed
figure traces to a deterministic probe or computation, carries a receipt, and is
re-derivable. The design is in [DESIGN.md](DESIGN.md); the authority law is §2.

**Read [docs/THREAD_DECISIONS.md](docs/THREAD_DECISIONS.md) before self-directing.**
The reasoning behind this design lives in an external conversation you cannot
see. That file is the part which constrains the code — including which
unbuilt things are deliberate, and the one identity that looks rigorous and is
tautological. Without it you will re-derive decisions or "fix" a scope choice.

## State as of 2026-08-03

- 20 commits on `main`, single branch. The cross-stream honesty audit described
  below is a local working-tree patch; it is deliberately not committed or pushed
  without explicit publication authority.
- **658 passed, 29 skipped** on the final full run (8m03s).
  `(cd web && npm run build)` clean.
  `node web/lib/money.mjs` runs.
- Proof Kernel v0 (`decision-packet`) and the review surface (`backtest-review`)
  both render in the chat UI and are DOM-verified in both pass and fail states.
- The promotion gate returns **BLOCKED on four grounds**, deliberately. That is
  the product, not a defect: every kernel check is green and the strategy still
  cannot be promoted.

```bash
uv sync --extra dev && uv run --no-sync pytest -q
```

Run the three self-checks; each prints its own gate:

```bash
uv run --no-sync python -m app.proof_kernel && uv run --no-sync python -m app.ledger && uv run --no-sync python -m app.promotion
```

## Architecture in one pass

```
bars -> packets -> ledger events -> equity curve -> summary -> promotion gate
```

| File | Owns |
|---|---|
| `app/decision_packet.py` | Packet schema, the **eight** verification checks, `Assurance` (evidence scoping) |
| `app/proof_kernel.py` | Fixture bars, signal detection, packet construction, differential oracle, mutation runs, `run_gate` |
| `app/ledger.py` | Integer-microdollar append-only event ledger, equity reduction, `CurveReductionReceipt` |
| `app/promotion.py` | The promotion gate and its frozen policy |
| `app/main.py` | `/api/kernel/chat`, `/api/kernel/review` (SSE, AI SDK v7 frames) |
| `app/experiment.py`, `robustness.py`, `trial_ledger.py`, `walk_forward.py` | Frozen diagnostic experiment batch and multiple-testing inputs |
| `app/research_graph.py`, `research_evaluator.py`, `research_loop.py` | Bounded content-addressed research DAG, independent diagnostic score and resumable CLI |
| `app/scientific_events.py` | Versioned scientific clocks, exact source-span receipts and deterministic registry-version replay |
| `app/execution_lifecycle.py`, `option_market.py`, `source_compiler.py` | Honest synthetic/external readiness boundaries |
| `app/authority_panels.py` | Exact ten strict panel schemas and research-tier quarantine |
| `web/components/nodequant-panels.tsx` | All ten closed renderers plus the review scoreboards |
| `web/lib/money.mjs` | Money formatting + its invariant |

Everything under `app/ctgov.py`, `executor.py`, `entities.py`, `neuromap.py`,
`planner.py`, `models.py` and `web/components/trialscope-panels.tsx` is the
**TrialScope** lineage — a prior query-to-visualisation service. It still runs,
is still tested and remains available at its explicit API route, but its legacy
panels (including freeform) are no longer mounted in the NodeQuant runtime. The
default UI proxies `/api/nodequant-chat` to `/api/kernel/review` and mounts
exactly the ten NodeQuant panel types.

## The traps — read these before writing a check

Five bugs of the same shape were found here. They will recur.

**1. A check whose two sides come from one accumulator is decoration.**
`max_accounting_residual_micros` was `equity − (cash + position_value)` computed
from the same three locals: zero by construction, across 200 randomised ledgers,
and rendered in the panel footer as evidence. Deleted. The P&L identity
`realised + unrealised − fees == equity − capital` is tautological the same way
and was **refused** before shipping.

The rule, from the design thread: *a check is evidence for a named failure class
only if that failure can change one side without mechanically forcing the same
change onto the other, and mutation tests demonstrate it detects it.* Write the
attack first; if the check does not fail, do not ship the check.

**2. Fixing the producer does not fix the verifier.** `build_ledger` enforces
that entry executes at `earliest_execution_at_utc` — its docstring even names
the failure — but that guard lived only in the code that *writes* the ledger.
The receipt is what a stranger checks, and it contained zero occurrences of
`packet`. A perfect-foresight trade with `packet_id=None` certified clean.

**3. Duplicate authority drifts.** Three instances: the strategy predicate vs the
packet's rules; `Rule.result` vs its own operands; `position_delta` vs `qty`. Each
time, two representations of one fact drove two authoritative calculations.
**Derive, don't validate** — a check can be forgotten, a derivation cannot be
bypassed.

**4. An unrun check is not a clean one.** `None` counters read as zero and
promoted a fabricated ledger. Unrun checks report `None`, and `None` fails.

**5. Absence of a counter is not zero.** `kernel_gate.get(field, 0)` let a bare
`{"status": "PASS"}` clear the gate by omitting everything.

## What is deliberately not built

Not oversights — scope decisions, each of which blocks promotion by design:

- **Synthetic fixture, not market data.** `fixture_bars()` is a closed sine
  formula. It proves the verifier, not SPY. No gaps, halts, splits, late prints
  or revisions.
- **The frozen third replay matches but its task provenance is self-attested.**
  The 25 signals match and the implementation is diverse; without an immutable
  orchestrator record it remains `same_author_diverse_implementation` and does
  not clear `ORACLE_NOT_INDEPENDENT_ENOUGH`.
- **Partial fills are not modelled; one position at a time, fixed hold, one
  unit.** Spread, slippage and commission are now frozen adverse per-fill models,
  visibly labelled modelled rather than observed.
- **Options are not modelled yet.** Underlying only. `EventType` has no
  `ASSIGNMENT` / `EXPIRY` / `CORPORATE_ACTION`.

## Next, in the order the design thread recommends

1. **Real bars.** The smallest increment that falsifies "synthetic changes
   nothing structural". Everything else is downstream of this. The bounded
   `app.market_data.load_csv_snapshot` seam now preserves a provider locator and
   exact source hash through every fact receipt; it deliberately does not call a
   provider or self-certify market-data conformance. Feed it an actual export,
   retain the raw snapshot, then build the gap/halt/split/revision conformance
   suite before changing that gate from `NOT_RUN`.
2. **Provider-independent market conformance** using the exact contract in
   `docs/MARKET_DATA_PLAN.md`; credentials/licensed files are the external
   boundary, not a missing label in code.
3. **Immutable run-controller provenance** for both the frozen oracle task and
   optimizer split access. Repository-authored JSON cannot certify either.
4. **External research authority.** Run `nodequant-research --max-experiments 4`
   to execute/resume the bounded local DAG. A compiled local trust root rejects
   substituted policies; atomic heads bind hash-chained nodes, edges and trials;
   the optimizer subprocess receives no TEST/OPENED_HOLDOUT bars and has a hard
   wall-clock kill budget. It refuses an authoritative champion on the synthetic
   corpus. Next evidence is an externally immutable controller for
   global completeness/preregistration/split access, then synchronized returns
   for Deflated Sharpe/PBO.
5. **Real execution/options evidence.** Synthetic partial/cancel/reject/expiry/
   latency scenarios and strict option snapshot counters exist. Next requires
   broker lifecycle receipts and OPRA/OCC point-in-time data; do not turn the
   diagnostic suite or caller snapshot into authority.
6. **Scientific archive authority.** `nodequant-science --demo` proves the
   deterministic 25-event/diff contract but remains BLOCKED. Supply frozen real
   ClinicalTrials.gov history bytes plus a supported re-fetch/external checkpoint
   and point-in-time market labels; do not treat the undocumented internal
   history endpoint or caller provenance labels as authority.

## Thread-to-repo gap map

This is the larger boundary behind the four immediate blockers. The Stock AI
thread specifies a complete research and market-time system; this repository is
still the proof kernel plus its first review surface. None of the rows below is
silently implied by a green kernel receipt.

| Thread contract | Repository state | Evidence required to close it |
|---|---|---|
| Source snapshot, legacy-rule compiler and ambiguity ledger | **COMPILER/ENVELOPE BUILT; INPUT BLOCKED** | Byte spans and source/clause/combined roots built; original bytes and human lock absent, all V0 readiness false |
| Frozen experiment manifest and chronological development/validation/test/opened-holdout splits | **GEOMETRY + METRICS BUILT; ACCESS PROOF BLOCKED** | Four persistent bands, every point tagged, split-local metrics and empty holdout `NOT_RUN`; immutable run-controller proof still required |
| Null benchmarks, indicator ablations, parameter-neighbour stability, delay/cost stress and multiple-testing controls | **DIAGNOSTICS BUILT; GLOBAL CONTROL BLOCKED** | Buy/hold + 128 random nulls, nine variants, three purged folds and trial batch built; global history/preregistration/Deflated Sharpe/PBO absent |
| Point-in-time option chain and relative-value selection | **STRICT VALIDATOR BUILT; DATA/SELECTOR BLOCKED** | Exact stale/future/duplicate/definition/size/IV/provenance counters built; OPRA/OCC replay and selector absent |
| Realistic order and option lifecycle | **JOURNAL + COSTS + SYNTHETIC FAILURE SUITE BUILT** | Partial/cancel/reject/expiry/latency transitions tested; canonical fill policy, broker receipts, assignment and corporate actions remain |
| Two nested loops: slow research and fast event-driven market time | **BOUNDED SLOW LOOP BUILT; MARKET-TIME LOOP NOT BUILT** | Locally anchored graph loop preserves trials, hard-kills timed-out evaluators and makes exhausted resumes byte-stable; external checkpoint signing, materiality wakes, knowledge-time isolation, deterministic risk, monitoring and adjudication remain |
| Ten typed panels | **10 OF 10 SCHEMAS/RENDERERS; PRODUCERS PARTIAL** | Runtime is exact NodeQuant vocabulary; generated diagrams cannot carry arbitrary content or enter risk. Seven market-time producers remain |
| Three-target promotion ladder | **HEADING ONLY** | Recover the thread's actual Signal-shadow → Execution-shadow → Live-capital criteria; do not infer gates from the titles |

The current work also closes packet-set binding, per-fill cost authority,
cross-stream order reconciliation, and split geometry/metrics. It does not turn
self-attested provenance or external-data absence into PASS.

## Known-open, honestly

- **The first genuine daily-data alpha search is negative.** Eighteen
  preregistered absolute-trend, moving-average, relative-momentum, dual-momentum,
  and falsifier trials used a frozen 4,677-row SPY/QQQ/IWM/TLT/GLD/BIL panel.
  `composite_top2` passed every 2020–2022 test gate but failed 2023–2025:
  0.818 excess Sharpe versus 1.224 always-long, with a −13.8% paired-block-
  bootstrap lower bound on annual active return. Only 6 of 18 full years had
  positive active return. See `docs/REAL_ALPHA_SEARCH.md` and
  `evidence/real-alpha-search/after.png`. Do not tune another daily rule on the
  now-open panel and call it out of sample; the next honest rung is licensed
  intraday executable data or prospective post-2026-08-03 packets.

- **Cross-stream reconciliation is `PASS` for the synthetic simulator, not a
  broker receipt.** The separate 136-event order journal reconciles lifecycle,
  quantities, timestamps, prices, costs, cash, fees, marks and IDs. External
  execution authenticity still requires a broker/provider stream.
- **One load-sensitive test.** `test_attack1b_l2_store_is_tenant_namespaced` has
  failed once under full-suite CPU contention and passes 5/5 in isolation. It is
  not characterised to the standard applied to
  `test_burst_cap_reduces_a_concurrent_noisy_neighbours_share` (80 paired runs).
  The former pacing risk has been measured and fixed with a bounded FIFO
  condition queue; a 50-caller burst plus cancellation now releases in arrival
  order and leaves zero waiters. If the remaining test recurs, measure before
  touching the assertion.
- **The curve samples at event times**, not every bar. With marks now emitted per
  held bar the gap is small, but drawdown is strictly "sampled at event times".
- **Provider replay is not built.** `FactReceipt` now carries bounded replay
  inputs and a source hash for external snapshots, but NodeQuant does not yet
  re-fetch a provider locator or prove that its bytes match the venue's
  point-in-time record. Byte reproducibility is established; market-data
  conformance is not.

## Working agreements from this session

- **Screenshot every surface you change, in both states.** Rendering found three
  bugs the tests structurally could not: stale branding, a WCAG-failing contrast
  regression on a trust surface, and fees displaying `$0.00` while non-zero. Use
  `web/capture-panels.mjs` and `web/capture-review.mjs`; the in-app browser pane
  returns empty `innerText` when it is not compositing, so Playwright is the
  reliable path.
- **Knockout before believing.** Every guard here has a test that breaks the
  guard and confirms the failure. A gate that never fails is not a gate.
- **The panel is a trust surface.** No motion, `NOT_RUN` never styled like
  `PASS`, verdicts as named checklists rather than composite scores, and the
  headline scoped (`VERIFIER KERNEL PASS`, never a bare `PASS`).

## Provenance

The panel seam, the memory and probe-cache invariants, and the
formula-not-the-number computation layer come from **TrialScope**, an earlier
service by the same author. Its full history lives in its own repository and is
deliberately **not** in this one: `main` starts at an orphan commit and an audit
across every reachable object confirms no third-party identifiers anywhere.
Do not re-add that history.
