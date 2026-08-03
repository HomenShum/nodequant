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
