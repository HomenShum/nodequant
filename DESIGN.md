# NodeQuant — Converged Design

**Sources converged:** TrialScope — a prior agentic query-to-visualization service by the same
author, which contributes the panel seam and the verification invariants · the NodeQuant two-loop
trading design · [DeepDiagram](https://github.com/LingyiChen-AI/DeepDiagram), which contributes the
renderer set and canvas.

**The convergence in one line, from the thread:**

> TrialScope supplies the law of authority. DeepDiagram supplies an optional visual scratchpad.
> NodeQuant never confuses the scratchpad with the ledger.

---

## 1. The three parents

| Parent | Keep | Reject |
|---|---|---|
| **TrialScope** | SSE `data-<name>` ⇄ `makeAssistantDataUI` seam; closed panel vocabulary (`trialscope-panels.tsx:3-12` — "the backend decides which of these appear… but it may only choose from this set"); every-number-is-a-totalCount; trace panel; memory carries filters never counts | Inline-only rendering (no canvas); hand-rolled div bars for financial series |
| **Stock AI thread** | Two nested loops; "engine detects and executes, agent investigates and challenges, risk layer permits, verifier proves"; four replay checks; autonomy ladder; Proof-Kernel scope discipline | — (domain spec) |
| **DeepDiagram** | Incremental `<design_concept>`/`<code>` parsing; visible generation progress; artifact version history; canvas zoom/pan; resizable research workspace; mermaid + mind-map rendering; editable artifact source; code hash + provenance; execution-trace step cards | Arbitrary React code; model-authored ECharts options; model-authored ReactFlow execution code; diagrams.net iframe (v1); scripts/HTML/network/dynamic imports; model-defined component types |

Note the sharp edge on DeepDiagram: **the renderers are adopted, the authorship model is not.**
ECharts belongs inside `backtest-review`, but its series come from the typed backend. ReactFlow may
render a typed explanatory graph, but the model emits **graph data, not executable React**.

## 2. The authority law (enforceable, testable, one sentence)

> Only closed typed panels may display authoritative facts, numbers, strategy state, decisions,
> orders, outcomes, or verdicts; generated artifacts may reference those objects by ID but may
> neither restate nor produce them.

A generated diagram attempting to display `SPY $631.42` / `BUY 10 CALLS` / `Risk gate: APPROVED`
must fail validation with `RESEARCH_ARTIFACT_REJECTED`.

**It must not render with a warning. It must not render at all.**

This is where the thread overruled the first draft of this document. TrialScope's `freeform` badge
("generated · unvalidated · no figures", `trialscope-panels.tsx:350-367`) is the right pattern for a
paragraph of prose and the wrong pattern for a diagram — because visual prominence itself
communicates authority. Prose gets a badge; diagrams get hard rejection.

## 3. Panel vocabulary — exactly ten SSE data types

```
data-decision-packet      data-evidence-packet      data-option-structure
data-trade-intent         data-risk-gate-verdict    data-execution-receipt
data-position-state       data-adjudication-verdict data-backtest-review
data-research-diagram   ← the only quarantined one
```

The backend selects which appear based on loop stage. The model selects none of them.

Notable payload rules:
- `option-structure` — the model may **select among typed candidates**; it may not author a
  contract or a Greek.
- `trade-intent` — action is a closed enum: `NO_TRADE | WAIT | OPEN_APPROVED_STRUCTURE | REDUCE |
  CLOSE | REVIEW`; carries supporting + counterevidence fact refs.
- `evidence-packet` — every fact carries a `knownAt` timestamp; rejected future sources are shown
  as rejected; quantities come only from deterministic probes.

Type-level quarantine, not just runtime:

```ts
type RiskGateInput = DecisionPacket | TradeIntent | OptionStructure;
// ResearchDiagram is intentionally absent.
```

## 4. Surfaces = the two loops

Add `react-resizable-panels` at the TrialScope insertion point (`web/app/assistant.tsx:101`).

**Market-time operating surface.** Main column carries the decision chain — `decision-packet` →
`trade-intent` → `risk-gate-verdict` → `execution-receipt` → `position-state` →
`adjudication-verdict`. Right review rail carries `evidence-packet`, `option-structure`, traces and
receipts. `research-diagram` lives in a secondary research drawer only. `backtest-review` is not a
market-time panel.

> The generated diagram never occupies the center position used by the risk verdict or position
> state. Visual prominence communicates authority.

**Backtest review surface.** Primary: `backtest-review`, the selected trade's `decision-packet`,
`adjudication-verdict`. Secondary: `evidence-packet`, `option-structure`, `execution-receipt`. This
is where promotion decisions are made — the human gate of the autonomy ladder.

## 5. Streaming

Transport unchanged: FastAPI SSE, AI SDK v7 UI-message-stream frames, one
`makeAssistantDataUI` registration per panel. Typed panels emit atomically — a partial number is a
wrong number. Only `research-diagram` streams progressively, borrowing DeepDiagram's incremental tag
parser and its fault-tolerance gating (mindmap/infographic render partials; mermaid/flow wait for
completion).

## 6. Verification invariants

- The agent graph never produces numbers; prices and counts come only from deterministic probes.
- Four replay checks per displayed fact: independent recomputation, admissibility, physical
  future-data isolation, future-poison differential test.
- Panel payload TS types generated from Pydantic — fixes TrialScope's known hand-mirror drift
  hazard (`trialscope-panels.tsx:17-30` vs `app/models.py`).
- **Rejection corpus** for the generated tier, each of which must return
  `{"status":"blocked","code":"AUTHORITATIVE_CONTENT_IN_RESEARCH_ARTIFACT"}`:
  "SPY was 0.8% above VWAP" · "Sharpe improved from 0.7 to 1.4" · "BUY 10 contracts" ·
  "Risk approved" · "Expected loss: $230" · "Source published tomorrow confirms the thesis".

**Falsification condition — when two-tier collapses back to closed-only:** if a generated artifact
ever displays an authoritative number or decision, influences risk or execution, or introduces a
fact without a valid `factRef`, the `research-diagram` tier is deleted and the product reverts to a
closed vocabulary.

## 7. Proof Kernel v0 — one panel

Ship `data-decision-packet` alone. Not the equity curve, option structure, research diagram,
blotter, narrative, or resizable canvas.

It proves exactly one claim: *this signal was produced from these exact facts, every fact was
knowable at the declared timestamp, and an independent implementation reproduced the packet.*

Payload carries `packetId` + `packetHash`; `strategy{id, version, configHash}`;
`signal{signalId, symbol, direction, decisionAtUtc, earliestExecutionAtUtc}`; `rules[]` with
`factRefs` and boolean results; `facts[]` each with a receipt.

**PASS is displayed only when all eight hold:** every displayed value has a FactReceipt; the
independent implementation reproduces every value under canonical rounding; `maximumKnownAtUtc <
decisionAtUtc` for every input; deleting all post-decision data leaves the packet hash unchanged;
poisoning all post-decision data leaves it unchanged; primary and independent implementations agree
on fired/timestamp/direction/entry-eligibility; `unsupportedFactCount == 0`.

Release gate over the frozen first 25 SPY V0.1-core signals:

```json
{ "status": "PASS", "packetsExpected": 25, "packetsPassed": 25, "factMismatches": 0,
  "postAsOfInputs": 0, "futureDeleteChanges": 0, "futurePoisonChanges": 0,
  "signalMismatches": 0, "unsupportedFacts": 0 }
```

## 8. Sourcing the trading surface UI

The repo has **zero chart libraries** — `web/package.json` carries only cytoscape, for the network
graph. Every trading chart is a new dependency decision, not a reuse.

### 8a. Renderers

**One primary: Apache ECharts 6.1.0 (Apache-2.0).** It is the only permissive library covering all
six panel shapes, and its entire API is a plain JSON `option` object — the same shape the authority
law already demands.

| Panel | Library | Note |
|---|---|---|
| Equity curve + drawdown | ECharts dual `line`, shared `axisPointer` | `uPlot` (MIT, 22 KB) if it needs 100k+ points |
| Candlestick + signal markers | ECharts `candlestick` + `markPoint` | markers are *data*, not code |
| Options payoff (multi-leg) | ECharts `line` on a numeric price axis | **no library exists** — payoff is `max(0, S−K)` summed over legs, ~30 lines |
| IV smile / surface | 2D multi-line per expiry first; `echarts-gl` lazy-loaded for 3D | traders read slices; the surface is a demo toy |
| Trade blotter | `@tanstack/react-table` + `@tanstack/react-virtual` (MIT) | headless, typed column defs |
| Greeks / position tiles | none — Tailwind grid + typed `Tile[]` | a tile is a div with a number |

```bash
npm i echarts@6.1.0 echarts-for-react@3.0.6 @tanstack/react-table@8.21.3 @tanstack/react-virtual@3.14.9
```

**The adapter boundary is the load-bearing part.** ECharts' `option` *is* config, so the authority
law holds only if we own the translation: one hand-written, unit-tested pure function per panel —
`renderEquityCurve(payload: EquityCurvePayload): EChartsOption`. The model emits the payload and
nothing else. A model emitting `EChartsOption`, a `series` array, or JSX is the same failure mode
with extra steps. This is also why Recharts / visx / Nivo score worse here: JSX composition invites
per-chart authoring, whereas payload → adapter is enforceable by the type system.

**Rejected, with reasons.** TradingView Lightweight Charts as *primary* — its x-axis is time-only,
so it structurally cannot draw a payoff diagram or an IV smile, and Apache-2.0 §4(d) propagates a
NOTICE requiring a tradingview.com link even after `attributionLogo: false` hides the in-chart logo.
Highcharts Stock — commercial license. Plotly — 1.39 MB. `react-financial-charts` — peer-capped at
React 18, unmaintained since 2023.

**License landmines in the surrounding ecosystem:** vectorbt carries a **Commons Clause** rider
despite its Apache badge (not open source); OpenBB and `backtesting.py` are **AGPL-3.0**; freqtrade
and frequi are **GPL-3.0**. Read their layouts; copy none of their code.

### 8b. Reference UIs (visual design, not code)

Nothing here is a component to install — these are surfaces to study and convert into design-dna
observations.

- **[OptionStrat builder](https://optionstrat.com/build/)** — public, no login. Solves the option
  structure panel almost entirely: leg strip (long above / short below, calls green / puts red),
  net debit/credit line, solid = payoff at expiry vs dashed = payoff at T+n on shared axes,
  breakeven ticks, strike verticals.
- **[QuantConnect backtest results](https://www.quantconnect.com/docs/v2/cloud-platform/backtesting/results)**
  — public docs, screenshotted. This is the backtest review information architecture. Steal the
  **Orders vs Trades split** (every submission/fill/partial/cancel, expandable per order — that is
  `execution-receipt`; closed round-trips are `position-state`), the per-order jump-to-chart icon,
  and the **Code tab** that freezes the exact source beside the run.
- **[IBKR Option Strategy Lab](https://www.interactivebrokers.com/en/general/education/pdfnotes/WN-TWSOptionStrategyLab.php)**
  — the only shipped product whose flow *is* our flow: forecast → ranked candidate structures →
  compare table → drill → approve.
- **[quantstats tearsheet](https://github.com/ranaroussi/quantstats)** (Apache-2.0) — the canonical
  section order for a strategy review, plus the two highest-signal elements in quant reporting: the
  monthly-returns heatmap and the worst-drawdowns table with recovery duration.
- **[Sierra Agent Traces](https://sierra.ai/blog/agent-traces)** — non-finance, but the right model
  for rendering agent reasoning as an expandable chain, and its **Supervisors** concept renders the
  checker as a separate voice from the proposer. That matches our rule that the proposer must never
  render its own approval.
- **[QuantConnect LEAN `/Report`](https://github.com/QuantConnect/Lean)** (Apache-2.0) — 29 named
  `ReportElements` are a ready-made panel taxonomy.

**Anti-pattern to avoid,** visible in OptionsPlay: a single composite score summarizing trend,
liquidity, IV, and earnings. Readers see the number and stop. Render `risk-gate-verdict` as a named
checklist with per-item pass/fail and reason — falsifiable per criterion instead of inviting
deference to a number. `risk-gate-verdict` is a trust surface: no motion, and never style an
undecided proposal in acceptance colors.

**Mobbin is not the source here.** Its general Web catalog surfaces only crypto exchanges (Kraken,
Gemini, Uniswap, Binance); a Robinhood search returns "Request app." A separate **Finance+** vertical
does exist at `mobbin.com/finance` (Webull, Wealthfront, Betterment, eToro), but it is a paid add-on
tier — the page shows upgrade prompts on the signed-in session. Capture from the public sources
above instead, into `design-dna/observations/` under the existing license discipline: live
inspection only, no cached pixels, atomic facts + URLs + dates.

### 8c. awesome-systematic-trading — a backend source, not a UI source

Its **Visualization** section is D-Tale (Flask + React over pandas), mplfinance (matplotlib), and
btplotting (Bokeh/backtrader). All Python plotting for notebooks and static reports. **Nothing there
is bootstrappable into the Next.js chat surface** — no React components, no web renderers.

Its real value sits in the sections that produce *numbers*, which is precisely what the authority law
needs, because the agent is forbidden from authoring them:

- **Pricing** — [FinancePy](https://github.com/domokane/FinancePy),
  [PyQL/QuantLib](https://github.com/enthought/pyql),
  [tf-quant-finance](https://github.com/google/tf-quant-finance). These become the deterministic
  greeks and option pricer behind `option-structure`. The model selects among typed candidates; a
  pricer produces every Greek.
- **Metrics** — [quantstats](https://github.com/ranaroussi/quantstats),
  [ffn](https://github.com/pmorissette/ffn), empyrical. These are the natural **independent oracle**
  for `backtest-review`: recompute Sharpe, drawdown, and returns in a second implementation, which
  is exactly PASS condition #2 and #6 of the Proof Kernel.
- **Risk / optimization** — pyfolio, PyPortfolioOpt, spectre.

So the answer to "can we bootstrap UI from it" is no — but it supplies the second implementation the
replay checks require, which is the harder half.

## 8d. What shipped, and what the reviews changed

Proof Kernel v0 and the review surface are built. Two adversarial passes and the
design thread each overturned something, and the corrections are more useful than
the original design:

- **`Rule.result` was testimony.** Hardcoded `true`, rendered to the user, checked
  by nothing — at bar 200 the panel showed "uptrend: true" while its own facts
  said otherwise. Rules now carry `lhs/op/rhs` and are recomputed against the
  packet's facts.
- **The lookahead check was vacuous.** It appended a Fact and asserted the
  mutation runs caught it, but a cosmetic relabel fails identically. The mutation
  runs detect *rebuild inequality*; proving they catch lookahead needs an actual
  future read, which is now what the test does.
- **The firing decision was outside the packet.** `build_packet` never called the
  predicate, so lookahead in entry logic was invisible to the mutation runs. Both
  now derive from one definition.
- **"Independent replay" was not independent.** It is a *differential* replay:
  same author, different shape. Renamed, and the panel now carries an assurance
  block — corpus, market-data conformance, semantic independence — with the badge
  reading `VERIFIER KERNEL PASS`, never a bare `PASS`.
- **Floats were financial truth.** Replaced with an integer-microdollar
  append-only ledger. The accounting identity now holds exactly at every point,
  not within a tolerance.
- **Conservation, not the oracle, catches fabrication.** Injecting position with
  no cash movement passed both the identity and the differential oracle, because
  both reducers read the same fabricated event. Event-consistency and
  negative-position checks close it.
- **The panel found a bug the tests could not.** Fees rendered `$0.00` while
  being 3,400 micros. Fixed at the root (a realistic fee), not in the formatter.

**The promotion gate is the honest headline: BLOCKED, on five computed
grounds** — synthetic corpus, market-data conformance never run, a same-author
oracle, no spread model, no slippage model. Every kernel check is green and the
strategy still cannot be promoted. That gap is the product.

A later pass closed three more: a position was marked only at exit, so it was
carried at its entry price for the whole hold and drawdown was understated by
$2.52 (0.46%, and scaling with size); the curve's x-axis was index-spaced while
shipping timestamps it never read; and absent costs biased the curve upward
only, which is the direction that manufactures a promotable-looking strategy.

## 9. Build order

The architecture is two-tier; **the first shipped version behaves like closed-only.**

1. **Proof Kernel v0** — `decision-packet` only. Closed typed data. No generated diagrams. No
   resizable canvas.
2. **Next** — `backtest-review`, `evidence-packet`, `adjudication-verdict` (the review surface).
3. **Then** — the quarantined `research-diagram` + canvas.
4. **Finally** — the market-time chain: `option-structure`, `trade-intent`, `risk-gate-verdict`,
   `execution-receipt`, `position-state`.
