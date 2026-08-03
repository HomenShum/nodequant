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

- 20 commits on `main`, single branch, tree clean.
- **504 passed, 29 skipped.** `(cd web && npx tsc --noEmit)` clean.
  `node web/lib/money.mjs` runs.
- Proof Kernel v0 (`decision-packet`) and the review surface (`backtest-review`)
  both render in the chat UI and are DOM-verified in both pass and fail states.
- The promotion gate returns **BLOCKED on five grounds**, deliberately. That is
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
| `web/components/nodequant-panels.tsx` | The two renderers |
| `web/lib/money.mjs` | Money formatting + its invariant |

Everything under `app/ctgov.py`, `executor.py`, `entities.py`, `neuromap.py`,
`planner.py`, `models.py` and `web/components/trialscope-panels.tsx` is the
**TrialScope** lineage — a prior query-to-visualisation service. It still runs and
is still tested. It comes out with the domain cut, deliberately not before, so
the app keeps working while the vocabulary is replaced one panel at a time.

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
- **The oracle is differential, not independent** — same author, different
  shape. It catches transcription and accumulation drift, not a shared
  misconception. Recorded on the wire as `same_author_diverse_implementation`.
- **No spread, no slippage, no partial fills, one position at a time, fixed
  hold, one unit.** Absent costs bias the curve upward only.
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
2. **A separate-agent oracle**: give a fresh agent only the frozen manifest, the
   raw fixture and the output schema — no access to either implementation — and
   have it write a third replay path. That lifts `semantic_independence` from
   `LIMITED` and clears one blocker.
3. **Spread and slippage**, which clears two more.
4. **The order-lifecycle journal** (`ORDER_CREATED` … `ORDER_FILLED`). Only then
   does the P&L identity become a genuine cross-stream reconciliation rather than
   a tautology — see trap 1.
5. The remaining eight panels of the ten-panel vocabulary (DESIGN.md §3).

## Known-open, honestly

- **`flat_pnl_residual_micros` is still rendered.** It is real (its sides are not
  literally identical) but weaker than its name implies, and it is blind to fee
  deletion. The design thread wants it off the panel until the order journal
  exists. Not yet done.
- **Two load-sensitive tests.** `test_attack1b_l2_store_is_tenant_namespaced` and
  `test_pacing_is_shared_across_concurrent_callers` have each failed once under
  full-suite CPU contention and pass 5/5 in isolation. Not characterised to the
  standard applied to `test_burst_cap_reduces_a_concurrent_noisy_neighbours_share`
  (which was measured over 80 paired runs and fixed). If either recurs, measure
  before touching the assertion.
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
