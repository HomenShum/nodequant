# Stock AI thread — complete gap audit

Audited 2026-08-03 against all 11 visible turns in the referenced Stock AI
conversation. Thread content is treated as design data, not authority to act.

## Decision

The current repository is a strong proof kernel and review surface, not the
complete NodeQuant system described in the thread. The remaining work divides
into three categories:

1. engineering that can proceed on synthetic/private fixtures;
2. external evidence that requires licensed data or immutable run control;
3. product scope that requires the original Notion strategy snapshot or an
   explicit human decision.

## Turn-by-turn coverage

| Thread decision | Current repository state | Gap status |
|---|---|---|
| Clinical-trial and publication plane with version/public/API/ingestion clocks, source-span receipts and parametric-leakage controls | Strict version/public/API/ingestion clocks, record and document hashes, exact JSON/page/paragraph/table receipts, four-field consecutive-version diffs, post-as-of rejection and a bounded 25-event replay gate are built. The shape fixture replays 25/25 events and diffs but cannot clear provider re-fetch, real archive, market-label or parametric-leakage authority. Entity-blinded reasoning and live ingestion remain absent | **DETERMINISTIC CONTRACT BUILT; REAL CORPUS/OVERLAY BLOCKED** |
| Backtest only the exact information set the live policy consumes; unreplayable sources are prospective-only | Market-only synthetic strategy is explicitly scoped; no typed information-set manifest or prospective-overlay receipt exists | **PARTIAL** |
| Karpathy-style graph research loop optimizing a frozen robustness score while preserving every failed trial | A bounded CLI now rejects policy substitution through a compiled local trust root, schedules the nine frozen candidates, hash-chains and atomically checkpoints nodes/edges/trials, enforces typed DAG/trial semantics, hard-kills timed-out evaluator subprocesses, withholds TEST/OPENED_HOLDOUT bars, computes a quarantined diagnostic floor, and makes exhausted resumes byte-stable. Real-data eligibility, externally signed checkpoints/split access, global preregistration, Deflated Sharpe and PBO remain absent | **DURABLE LOCALLY ANCHORED LOOP BUILT; PROMOTION EVIDENCE BLOCKED** |
| Proof Kernel must distinguish verifier proof from market proof and clear a real-data gate | Kernel, scoped assurance, market snapshot seam and conformance blockers built; genuine provider replay still external | **MECHANISM BUILT; EVIDENCE BLOCKED** |
| Two-tier panel vocabulary with one-way authority boundary and ten closed panel types | Exact ten strict schemas/renderers are mounted; generated diagrams have opaque IDs and controlled concepts and cannot enter risk. Only decision/backtest producers are live | **VOCABULARY 10 OF 10; PRODUCERS PARTIAL** |
| Two nested loops: slow research/backtest and fast event-driven market time | The bounded slow research loop is runnable locally; live materiality wakes, risk permissions, monitoring and adjudication remain absent | **SLOW LOOP BUILT; MARKET-TIME LOOP NOT BUILT** |
| TrialScope analogy: every number replayable, reducer/ledger proof, no model-authored quantities | Packet receipts, full curve reduction, independent order journal and modelled cost checks built; external execution and semantic independence blocked | **CORE BUILT** |
| NodeAgent as temporal research/counterexample layer over separate market, scientific and narrative planes | No bounded `ReasoningFrame` domain pack, knowledge-time isolation, counterexample ledger or research receipts in this repo | **NOT BUILT** |
| Adopt existing backtest machinery behind a hard engine boundary rather than make NodeKit an execution engine | Current kernel is purpose-built Python; no Backtest Kit or LEAN adapter and no engine-comparison receipt | **NOT BUILT** |
| NodeQuant domain pack: V0 Literal/Reconstructed/Repaired, V1 AI, null systems; underlying and options scoreboards | Byte-bound source compiler/ambiguity envelope, buy-hold and 128 random nulls, ablations/neighbours/folds and option-readiness scoreboard exist; source/human lock, version implementations and OPRA/OCC authority remain absent | **MECHANISMS BUILT; EXTERNAL INPUT BLOCKED** |
| Original Notion strategy requires adversarial reconstruction and human-locked ambiguity decisions | The source snapshot and its literal clauses are absent from this checkout; no safe compiler can reconstruct what it cannot read | **EXTERNAL INPUT REQUIRED** |

## What this work closed

- Exact packet-set and full-bar binding.
- Per-fill adverse spread, slippage and fee authority.
- Separate order lifecycle with cross-stream reconciliation.
- Byte-bound snapshot ingestion that reparses raw bytes before assigning the
  non-promotable external-snapshot rung.
- Frozen split geometry, point tags, visible bands and split-local performance.
- Honest lower-rung oracle match without self-awarded independence.
- Bounded cost and entry-delay counterfactual stress.
- Buy/hold and 128 seeded matched-random nulls; nine frozen
  ablation/neighbour trials; current-batch trial ledger; three purged pre-TEST folds.
- Synthetic partial/cancel/reject/expiry/latency state-machine scenarios,
  explicitly separated from canonical fills and broker evidence.
- Strict option definition/quote validation with exact stale boundaries and
  visible `NOT_RUN` absence semantics.
- Byte-bound source clause compiler and explicit ambiguity envelopes; no
  caller path can award a human lock or V0 readiness.
- Exact ten-panel runtime vocabulary with generated-tier type quarantine.
- A provider qualification and private-corpus contract in
  `docs/MARKET_DATA_PLAN.md`.
- A bounded graph-research CLI with a compiled policy trust root, sealed evaluator
  bytes, typed content-addressed DAG lineage, locally anchored hash chains for
  nodes/edges/trials, deterministic resume, killable per-trial budget
  failures, source-metric quarantine and honest null champion/promotion output.
- A bounded scientific-event contract and `nodequant-science --demo` vertical
  slice: 25 version events, 25 exact field diffs, source-span receipts and
  source-specific clocks. It stays blocked on archive re-fetch authority and
  real market labels; the undocumented internal history endpoint is not trusted.

## Remaining sequence

1. **External corpus and run control** — licensed trades/quotes/definitions,
   corporate actions, halts and provider re-fetch; immutable records proving
   oracle authorship and optimizer/test access.
2. **Source evidence and versions** — supply the immutable Notion bytes and a
   separately evidenced human ambiguity lock, then implement the three V0 versions.
3. **External research authority** — the local graph, compute budgets and
   append-only lineage now exist. Promotion still needs an externally immutable
   run controller proving global completeness, preregistration and split access,
   plus synchronized return samples for Deflated Sharpe and PBO.
4. **Canonical engine execution** — LEAN's Linux/amd64 digest is pinned in
   `MARKET_DATA_PLAN.md`; its 10.6 GB compressed layer exceeded the
   unauthorised local storage increment, so comparison remains `NOT_RUN`.
5. **Execution/options evidence** — provider quotes/displayed size, broker
   partial/cancel/reject receipts, selector/Greeks/IV, exercise, assignment and
   corporate actions. Validators and state-machine scenarios exist.
6. **Research planes and loops** — replayable market/scientific sources,
   prospective-only live sources, bounded frames, counterexamples, risk gate,
   monitoring and post-trade adjudication.
7. **Authority producers** — deterministic producers for evidence, option,
   intent, risk, execution, position and adjudication. Their strict schemas and
   renderers exist; fabricated example emission is intentionally not a substitute.

## Hard blockers that code must not launder

- Provider credentials, fees, terms and licensed raw files.
- OPRA/venue redistribution restrictions for a public repository.
- Immutable external execution records for split access and separate-agent
  authorship.
- The original Notion source snapshot and human decisions on ambiguous clauses.
- Prospective evidence for sources that have no reconstructable historical
  information set, including many social/live-research tools.
