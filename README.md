# NodeQuant

An options-trading research and execution surface where **an AI agent may investigate and
challenge, but may never author a number.**

Every figure the interface displays — a price, a Greek, a fill, a Sharpe ratio — comes from a
deterministic probe or a deterministic computation, carries a receipt, and is reproducible by an
independent implementation. The language model chooses what to look at, what to doubt, and what to
propose. It does not produce the arithmetic, and it cannot invent a way to display it.

Status: **early.** Design settled ([DESIGN.md](DESIGN.md)); Proof Kernel v0 and the
backtest review surface are built. Picking this up? Start with
[HANDOFF.md](HANDOFF.md).

## Why

Most "AI trading" surfaces let a model write the numbers it then reasons over. That fails silently:
a plausible fabricated price propagates into a decision chain, and nothing downstream can tell the
difference. NodeQuant makes that structurally impossible rather than discouraged.

The rule is one enforceable sentence:

> Only closed typed panels may display authoritative facts, numbers, strategy state, decisions,
> orders, outcomes, or verdicts; generated artifacts may reference those objects by ID but may
> neither restate nor produce them.

A generated diagram that tries to display `SPY $631.42` or `Risk gate: APPROVED` fails validation
with `RESEARCH_ARTIFACT_REJECTED`. It does not render with a warning. It does not render at all.

## How it works

Two nested loops. A slow **research loop** decides which strategy version is allowed to run: freeze
the experiment, compile it to deterministic code, replay every signal independently, let the agent
investigate the results and propose a delta, attack that delta adversarially, then promote. A fast
**market-time loop** runs the promoted strategy: deterministic signal detection, materiality
triage, a point-in-time investigation with an evidence packet, a bounded trade intent, a
deterministic risk gate, execution, and post-trade adjudication.

The division of labour:

> The engine detects and executes. The agent investigates and challenges. The risk layer decides
> what is permitted. The verifier proves what actually happened.

The UI is the operating surface for the fast loop and the review surface for the slow one. Panels
are a closed vocabulary streamed over SSE as `data-<panel>` frames; the backend selects which
appear from the current loop stage, and the model selects none of them. See
[DESIGN.md](DESIGN.md) for the ten panels and the quarantined research-artifact tier.

## Quick start

```bash
uv sync --extra dev
```

```bash
uv run uvicorn app.main:app --port 8000
```

```bash
cd web && npm install && npm run dev
```

The preregistered genuine-daily-data diagnostic is reproducible from the frozen
snapshot with `uv run nodequant-alpha --policy research/real_alpha_policy_g2.json
--raw-directory evidence/real-alpha-search/raw --reuse-raw`. Its current verdict
is negative; see [docs/REAL_ALPHA_SEARCH.md](docs/REAL_ALPHA_SEARCH.md).

The breadth-first successor runs 100 discovery candidates across 13 families,
seals the replication set and drift-aware evaluator before reading a new ETF
universe, and preserves all 116 measured trials. Composite momentum improved
replication point estimates but was not a discovery survivor and failed fold and
active-return uncertainty gates; see
[docs/ALPHA_BREADTH_SEARCH.md](docs/ALPHA_BREADTH_SEARCH.md).

The next evidence rung is a content-addressed future-time packet rather than
another historical holdout. See [docs/PROSPECTIVE_ALPHA.md](docs/PROSPECTIVE_ALPHA.md)
and reproduce it with `uv run --no-sync python -m app.prospective_alpha --reuse-raw`.

An independent-mechanism crypto experiment searched 106 BTC UTC/calendar rules
and opened an externally sealed ETH 2023-2025 panel, but was invalidated after
an audit proved Coinbase bucket-start timestamps had been treated as close
instants. Corrected post-hoc sensitivity remains negative; it is not replacement
replication or alpha evidence. See
[docs/CRYPTO_SEASONALITY.md](docs/CRYPTO_SEASONALITY.md).

The first positive confirmatory research result is a cross-geography factor
persistence diagnostic. A 15-subset U.S. discovery selected equal-weight HML,
RMW and momentum, then one externally authorized developed-ex-U.S. holdout
passed its sole frozen gate across 420 months: the one-sided 95% studentized
block-bootstrap lower bound on annualized stressed mean was +2.25%, and all five
registered folds were positive. This is revised published zero-cost factor
evidence, not executable or promotion-eligible alpha. See
[docs/FACTOR_PREMIA.md](docs/FACTOR_PREMIA.md).

Its next investability falsifier froze one liquid long-only proxy before opening
return bytes: equal MTUM/QUAL/VLUE versus SPY, monthly, with measured turnover
charges. The proxy failed all three registered gates over 2014–2025: 12.30%
versus 13.40% CAGR, −0.95% annual active mean, −2.36% one-sided block lower
bound, 1/4 positive folds, and −1.03 Sharpe after removing the best 12 months.
This is a useful contradiction, not a reason to tune the now-open ETF panel.
See [docs/INVESTABLE_FACTOR.md](docs/INVESTABLE_FACTOR.md).

The research trail is also compiled into a deterministic point-in-time multiplex
graph at `/research-graph`: the content-addressed nodes and typed edges cover
the failed daily searches, prospective packet, invalidated crypto branch, factor
policy/authority/result, visuals, and open implementation gap. Search, temporal
cutoff, layer filters, and deterministic attention change what a reviewer sees;
the graph never copies performance values out of their authoritative artifacts.
See [docs/RESEARCH_EVIDENCE_GRAPH.md](docs/RESEARCH_EVIDENCE_GRAPH.md).

The trader-facing successor is specified separately as the **U.S. Large-Cap
Market Activity Graph**. It distinguishes activity, directional pressure, fund
flow, and rotation; freezes a multiscale market→sector→industry→instrument→event
spine; and refuses to render a fake live map before the point-in-time universe,
market, options, event, and source corpora reconcile. See
[docs/MARKET_ACTIVITY_GRAPH.md](docs/MARKET_ACTIVITY_GRAPH.md).

Copy `.env.example` to `.env` and set your model provider key. Postgres is optional — it caches
measured probes and nothing else. See `docker-compose.yml`.

## Verification

Every displayed fact passes four checks: independent recomputation, admissibility, physical
future-data isolation, and a future-poison differential test. A panel may show `PASS` only when all
of them hold and no displayed value lacks a receipt.

```bash
uv run pytest
```

## Lineage

NodeQuant reuses the panel seam, the memory and probe-cache invariants, and the
formula-not-the-number computation layer from **TrialScope**, an earlier agentic
query-to-visualization service by the same author. The renderer set and research canvas follow
[DeepDiagram](https://github.com/LingyiChen-AI/DeepDiagram) — its renderers, deliberately not its
model-authored-code model.

## License

MIT.
