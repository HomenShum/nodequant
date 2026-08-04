# Storage — the decision — 2026-08-04

One sentence: **NodeQuant serves files, not a database server.**

This settles a question asked three times. The prior answers were surveys.
This one is the decision, with the escalation points named so nobody has to
re-litigate it.

## By data type

| Data | Store | Why |
|---|---|---|
| Bars — real market data, append-only | **Parquet files** (pyarrow) | Columnar, ~10x smaller than JSON, serverless, replayable by hash. The domain's own curated tool list has a Databases section with no graph store in it — bars are a scan problem, not a traversal problem |
| Service state — workspace epochs, freshness, chat threads | **SQLite** (stdlib `sqlite3`) | One instance, single writer, zero setup, zero services. The temporal-authority kernel's state lands here |
| Evidence, receipts, thread records | **Git**, committed | Content-hashed, public, already the audit trail. Screenshots stay (the evidence discipline); raw provider bytes leave (licensing + size), with `sha256` + locator staying in the receipt |
| Ledger, curves, gate verdicts | **Not stored** | Pure functions of bars + packets. Stored derived data is a second authority that can drift from the computation — this repo's most-repeated bug class |
| Probe cache + entity aliases (TrialScope legacy) | **Postgres — optional, off by default** | `docker-compose.yml` exists for local dev of this cache only. The service runs with no `DATABASE_URL`, which its own comment calls the point |

## Query engine

**DuckDB, added the day a SQL query over bars is needed — not before.** It is
a library, not a service: zero cost while absent, one dependency when needed.
`pyarrow` reads Parquet sequentially, which is all the conformance runner needs
at v0.

## Escalation triggers — defined so the decision has edges

1. **Multi-instance deployment** → turn Postgres on. `app/store.py`, asyncpg,
   and `migrations/001_init.sql` already exist; this is configuration, not a
   build.
2. **Lineage stops being recomputable** — live fills, months of real history
   that no fixture can replay → revisit **ArcticDB** (serverless, versioned,
   built by a systematic fund) for series, and only then consider a graph
   store for stored lineage. Memgraph before that point is RAM-priced
   infrastructure for a scan-shaped workload, and a stored lineage graph today
   would duplicate an authority the code derives.

## Rejected, with reasons on record

- **Memgraph now** — in-memory pricing for append-only columnar data; no graph
  database appears anywhere in the domain's curated list; a stored graph would
  be a second authority (HANDOFF trap 3, promoted to infrastructure).
- **ArcticDB now** — good tool, right shape, heavier dependency than v0 needs.
  Named as the escalation target instead of the default.
- **Any managed database** — the deploy story ("a stranger verifies it with
  `uv run`") and the demo story ("zero services") are the same property. Paid
  for repeatedly this session; not spent without a trigger firing.

NOT VERIFIED: the design thread has not reviewed this split. Council it before
the bar store lands with the conformance runner.
