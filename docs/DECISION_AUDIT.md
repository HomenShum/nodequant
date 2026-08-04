# Decision audit — every load-bearing rationale, judged twice — 2026-08-04

Every major decision in this repo, re-judged in two independent passes:

- **Pass A (proponent):** does the rationale hold on the evidence that existed
  when it was decided? Was it measured or merely asserted?
- **Pass B (adversary):** steelman the opposite position with everything learned
  since. What would refute it? Did anything already?

Where the passes disagree, the disagreement is stated rather than averaged.
Verdicts: **UPHELD** · **UPHELD WITH CAVEATS** · **PARTIALLY OVERTURNED** ·
**OVERTURNED**. An audit that upholds everything is not an audit; this one
overturns one of its own recommendations, refines two, and reports one process
failure by the auditor.

## Verdict table

| # | Decision | A | B | Verdict |
|---|---|---|---|---|
| 1 | Publish with orphan history; assignment archive stays private | measured (object-level grep clean, 404 on raw) | lineage claim now unverifiable by strangers — accepted cost | **UPHELD** |
| 2 | Two-tier authority law; research tier hard-rejects numbers | thread-adjudicated; falsification clause defined | tier demand asserted, not measured; it is the first cut under scope pressure — by its own clause | **UPHELD** |
| 3 | Proof Kernel v0: one panel, synthetic fixture | invariant-first ordering; shipped end-to-end | sub-claim "real bars change nothing structural" was later judged FALSE by the thread and corrected | **UPHELD** (sub-claim overturned in-session) |
| 4 | NOT_RUN fails; None fails; absence ≠ zero | attack-tested repeatedly (C1; audit) | permanent BLOCKED risks alarm fatigue — mitigated by enumerated, actionable blockers | **UPHELD** |
| 5 | Derive, don't validate (`posting()`) | closed a 3-instance bug class; fabricated postings inert | over-applied: sharing the derivation with the oracle made the cross-check agree by construction; an external review caught it | **UPHELD WITH CAVEAT** — derive in producers, **diversify in verifiers** |
| 6 | The evidence rule (mutation-demonstrated checks) | killed two tautologies; regression tests name which mechanism catches which attack | covers only *named* failure classes; the C3 attack was caught by an unrelated counter — luck, not coverage | **UPHELD** + keep an attack registry |
| 7 | Quota-flake fix: pooled means + −0.02 tolerance | 80 paired runs; 30/30 after; knockout proved coverage kept | bimodality never root-caused; bound fitted to one machine; deleting the assertion was equally defensible | **UPHELD WITH CAVEATS** |
| 8 | Integer microdollars + money invariant | caught a real $0.00-fees display bug; self-check runs | none found — the Windows-dead guard was found and fixed by audit | **UPHELD** |
| 9 | `EXTERNAL_BAR_SNAPSHOT` boundary | thread: "correct and enforced, not decorative" | name overclaims (rename pending); docs and code currently disagree — a documented-vs-implemented gap of the exact class we audit for | **UPHELD, action open** |
| 10 | Temporal-authority kernel before terminal layout | receipts prove provenance, not freshness — real hole | in replay mode nothing is ever live; full freshness-policy machinery pre-live is speculative | **UPHELD WITH SCOPE CAVEAT** — build the minimal core (as-of, epoch, late-frame rejection) now; policy engine when live data exists |
| 11a | Bars belong in columnar storage (Parquet/ArcticDB/DuckDB); not Memgraph | field evidence (no graph DB in the domain's own curated list); RAM economics | Memgraph legitimate later for *stored* lineage once it stops being recomputable | **UPHELD** |
| 11b | "Stop committing raw JSON and PNGs to git" | git at 42MB, no LFS, binaries forever | **conflicts with this project's own evidence discipline** (screenshots-throughout is a standing user rule that caught 3 bugs) and with stranger-verifiability | **PARTIALLY OVERTURNED** — split the policy: raw *provider* exports out (licensing, size); screenshots stay, managed (LFS or element crops) |
| 12 | Local pinned open-weight planner | authority law makes the model swappable by construction; pinned weights ≈ content-addressable input | determinism overstated (inference nondeterminism survives pinning); planner quality shapes research quality even when it cannot author numbers | **UPHELD WITH CAVEATS** — pilot and measure, do not claim full determinism |
| 13 | Adopt Langfuse/Opik for observability | mature, self-hostable, maps to the trace panel | **contradicts the zero-running-services asset the same analysis praised** while rejecting Memgraph for exactly that cost | **OVERTURNED** (withdrawn by this audit) — keep in-repo traces; revisit at live trading |
| 14 | `ExecutionPlan` closes the execution-authority hole | thread-confirmed; attack demonstrated (foresight exit under a valid packet) | five-object chain is the *full* fix; the *minimal* fix is sell-side constraints on existing objects | **UPHELD with sequencing** — constrain sells first, then the chain |
| 15 | Consult one design thread throughout | the thread was measurably corrective (predicted the $2.52 drawdown understatement; killed two tautologies) | single-prior monoculture: every architectural verdict now descends from one conversation | **UPHELD WITH CAVEAT** — council-mode a second, differently-primed thread before the temporal-kernel build |
| 16 | Screenshots of every surface, both states | found 3 bugs tests structurally could not (branding, WCAG 2.04 contrast, $0.00 fees) | none | **UPHELD** |

## The contested ones, in full

### 11b — the storage advice contradicted the project's own discipline

The recommendation "stop committing raw JSON and PNGs" was given in the same
session in which the standing screenshot rule caught three real bugs, and in a
repo whose HANDOFF tells the next agent to screenshot every surface in both
states. Both cannot be policy. Resolution, replacing the blanket advice:

- **Raw provider exports leave the repo** (see finding F2 below — this is now a
  licensing question, not just a size one). The design already supports this:
  receipts carry `sha256` + locator, so the bytes can live outside the repo
  while the hash stays in it. That is the `BYTE_ADDRESSED` design doing its job.
- **Screenshots stay committed** — they are the evidence discipline — but
  managed: element crops preferred over full-page, LFS if size becomes real.
- **Derived JSON keeps following the existing rule**: recompute, don't store.

### 13 — the observability recommendation is withdrawn

Judged against my own Memgraph rejection, it does not survive: the strongest
argument there was that NodeQuant needs zero running services and that a
stranger can verify it with `uv run`. Langfuse/Opik reintroduce a service for
benefit that the in-repo trace panel and committed evidence already mostly
provide. Wrong trade today; right trade possibly at live trading, where trace
volume stops being recomputable.

### 5 — the caveat worth promoting to a principle

Deriving postings from one canonical function fixed the duplicate-authority
class in the *producer* path — and then quietly broke the *verifier* path,
because routing the oracle through the same `posting()` made the cross-check
agree by construction. An external review caught it; the oracle now derives
by hand. The principle the caveat generalises to:

> **Derive in the authority path. Diversify in the verification path.**
> One canonical derivation for what is true; deliberately different derivations
> for checking it.

## New findings produced by this audit

**F1 — Commit `4bba7d1` mass-published unreviewed work.** A commit labelled as
a docs change ("Consult the thread…, record its verdict") used `git add -A`
while another agent's uncommitted work sat in the tree, and swept **846 files,
+194,630 lines** into the public repo — the research/experiment modules, the
market-map UI, and the whole `evidence/` tree. My own process error, caught by
this audit. The work itself appears legitimate (self-checks pass; suite
verification running at time of writing), but it reached `origin/main` without
review, and the commit message describes ~2% of its contents. Remediation is
verification-forward (F4), not history rewrite — the repo is public and the
content is not sensitive, with the possible exception of F2.

**F2 — Raw provider exports are now public, with unverified redistribution
terms.** Committed and pushed: `nasdaq-screener-observed-2026-08-04.json`
(2.1MB), iShares IVV holdings snapshot, GLD/SLV price raws (~0.4MB each).
CFTC data is US-government work and fine. The Nasdaq screener export and
iShares holdings are provider content whose redistribution terms were never
checked. Action: check terms; expected outcome is replace-with-fetch-script +
committed `sha256` + locator (the receipts already carry both, so nothing in
the proof chain breaks). Until checked, treat as the repo's only live external
exposure.

**F3 — The take-home archive is a provenance single point of failure.** The
private lineage lives in one local directory with no remote. If that disk
dies, the "prior service by the same author" claim loses its only evidence.
Action: private backup (private GitHub repo or offline copy). Not urgent;
irreversible if ignored long enough.

**F4 — In-tree claims I did not verify are now verified or marked.** The
swept-in HANDOFF claims "658 passed, 29 skipped". The three module self-checks
pass as of this audit (gate PASS 25/25 with per-packet hashes; ledger
acceptable, now including spread/slippage costs; promotion BLOCKED with the
current blocker set). Full-suite verification was launched alongside this
document; its result belongs to whoever reads this next — `uv run --no-sync
pytest -q` — and the number printed there, not the number in any document,
is the state of the repo.

## What the double-judging did *not* shake

The verification spine survived both passes untouched: NOT_RUN-fails,
absence-is-not-zero, mutation-demonstrated checks, integer money, knockout
testing, screenshots-in-both-states, and the promotion gate's refusal to pass
while anything is unproven. Every one of those was attacked at least twice
this session — by the 13-agent audit, by an external reviewer, or by the
design thread — and held. The repo's central bet, that the gap between green
checks and BLOCKED promotion *is* the product, stands.

## Ranked actions

1. **F2** — check redistribution terms for the committed provider exports;
   expected: bytes out, `sha256` + locator stay. *(exposure, live)*
2. **F4** — read the suite result; reconcile any drift between it and HANDOFF.
3. **D9** — land the `BYTE_ADDRESSED_BAR_SNAPSHOT` rename + evidence-class
   de-laddering, closing the docs-vs-code gap.
4. **D14** — sell-side authority constraints (small), then `ExecutionPlan`.
5. **D10** — minimal temporal core only: `as_of` on every panel, workspace
   epoch, late-frame rejection. Defer the freshness-policy engine.
6. **D15** — council-mode a second thread before building the temporal kernel.
7. **F3** — back up the private archive.
8. **F1 hygiene** — never `git add -A` in a shared tree; add-by-path only.
