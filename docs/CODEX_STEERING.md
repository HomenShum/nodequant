# Steering context for Codex — 2026-08-05

Read this before the next task. It carries decisions made outside your session:
owner direction, design-thread verdicts, and a production-readiness review.
Each item says what to do and why. Where a claim is unproven it says NOT
VERIFIED.

Style: short sentences, one idea each, exact numbers.

---

## 0. Working boundary

Two agents work this tree. Rules that keep us out of each other's way:

- Commit by path. Never `git add -A`. It cost 846 unreviewed files once.
- You own `web/app/market-map/*`, `web/prepare-market-map-notion-image.mjs`,
  and `evidence/market-activity/*`. I have not touched them.
- I own `docs/*`, `research/policy.json`, `app/research_trust.py`, and the
  seal chain.
- `app/main.py` is shared. It holds your uncommitted context gate and my
  uncommitted review cache. Commit yours; mine follows.

**CI is now live** (`.github/workflows/ci.yml`). Backend is green. The web job
is red on `market-map-view.tsx`: 4 unclosed JSX tags at lines 163, 167, 202,
382. That file is yours and mid-edit. Fix or revert before the next push.

---

## 1. HIGHEST PRIORITY — delete the `/market-map` route

Owner direction, verbatim: *"no one would ever use this route. It should all
just be one single chat interface with interactive UI components, like
TradingView or OpenBB."*

The surface itself is good. Treemap, 504 equities, 11 sectors, NVDA weight
`7.6433%`, an authority receipt with the IVV holdings hash, a replay hash in
the footer. Keep every part of that.

The container is wrong. A separate page makes the user leave the agent to look
at the market, then come back to ask about it. Both reference demos we studied
use one surface with panes inside it. Never a page change.

**Do this:**

1. Add `data-market-map` as panel eleven in the closed vocabulary.
   `web/components/nodequant-panels.tsx` registers it. `app/authority_panels.py`
   gets its strict schema. Same rules as the other ten: closed payload, no
   model-authored numbers.
2. Move the render logic out of `market-map-view.tsx` into that panel.
3. Delete the route. Not a redirect. One surface, one vocabulary, no second way
   to see the same data.
4. Clicking a constituent SETS THE WORKSPACE SYMBOL. It does not navigate.

**Update `docs/DEMO_REFERENCE.md` when done.** It currently says dual-pane is
"specified, not built". That was wrong: a third pattern shipped as a separate
route, which is worse than unbuilt because it looks finished.

## 2. Symbol linking must be transactional

From the design thread, and it constrains item 1.

Every required pane commits to one `(symbol, epoch)` or the workspace becomes
explicitly non-actionable. Best-effort linking makes a coherent-looking false
state, which is worse than an obviously broken one.

- Any SSE frame from a superseded epoch must be REJECTED. A late frame must
  never repaint a pane with old data.
- A failed OPTIONAL pane permits `PARTIAL`. Mask its old content and label it
  as belonging to the previous epoch.
- Your `/api/kernel/context` gate is the right foundation. Extend it; do not
  replace it.

## 3. Invert the parameter form

A production-readiness review named a pattern worth adopting. It also decides
how the market map should feel.

```
conventional   human picks agent -> human fills N fields -> agent runs
inverted       agent reads context -> agent proposes the FILLED form,
               with provenance per field -> human CORRECTS -> agent runs
```

The human moves from composition to correction. People are poor at recalling N
fields and excellent at spotting the one that is wrong.

Applied here: the agent proposes the workspace context — symbol, epoch,
as-of, evidence class — each field showing where it came from. The human
corrects one line. This is the real human-in-the-loop point. Not the write
gate; the PARAMETER gate. A wrong inferred symbol is caught by no test and
changes every answer.

## 4. Storage — decided, with one refinement

Full decision: `docs/STORAGE.md`. Summary: files, not a database server.
Parquet for bars, SQLite for service state, git for evidence, nothing for
derived data.

**Two arguments are stronger than what that file records.**

First, against a graph database. The decisive question is: does the graph
ANSWER, or PROPOSE? Ours proposes — every candidate is confirmed against the
source of truth. So graph quality moves recall and cost, never correctness. A
bounded 2-hop traversal over thousands of nodes is a self-join, not a graph
workload. Add the host fact: Neo4j speaks Bolt on raw TCP `7687`, and managed
hosts serve HTTP/1, HTTP/2, gRPC and WebSockets only. It is unreachable
whatever the code does. "The host will not run it" is a fact a reviewer can
check. "We chose simple" is an opinion.

Second, a genuine EXCEPTION to "never store derived data". The rule is: state
is justified when it is expensive to re-derive AND safe to replay. An
immutable receipt behind a stable link is neither. It earns its row precisely
because it must NOT be replayed — recomputing behind a permalink silently
changes what the link showed, while every component stays correct. **If we
ever expose a shareable link to a decision packet or a review, that packet
must be STORED, not recomputed.** Add it to `docs/STORAGE.md` when you build
sharing.

Do not build the store yet. Bars are still fixture-only. No data, no store.

## 5. Production-readiness gaps, ranked

Full map: `docs/PRODUCTION_PATH.md`. The three that matter next:

1. **Three-stage fault chain.** We have stages 1 and 2: errors intercepted,
   backoff present in `app/ctgov.py`. Stage 3 exists for the planner
   (`fallback_plan`) and nowhere else. Every future provider adapter needs a
   deterministic local-rule downgrade so an outage degrades the product to
   "less flexible" instead of "down".
2. **Full jitter — already correct, do not regress it.** Verified at
   `app/ctgov.py:70-74`: it sleeps uniformly in `[0, cap]`, not
   cap-plus-narrow-band, and the comment states the reason — a narrow band lets
   a burst that failed together retry together, which is the storm backoff
   exists to prevent. Any new provider adapter must copy this form.
3. **Spend cap that DOWNGRADES, not fails.** `MAX_RETRIES` and deadlines bound
   one request. Nothing bounds a stream of individually-legal requests. When
   the budget trips, fall back to the deterministic planner. A cost incident
   becomes a quality dip. This is mandatory the day the planner joins the
   kernel path — not before, because the kernel path calls no LLM today.

## 6. If you build an LLM judge

Do not quote one run. The same file measured 34/44 then 22/44 — 27% of the
scale. Median of 3, per dimension.

Run a NEGATIVE CONTROL first: corrupt a response per dimension and confirm the
judge catches each. If the control fails, the real run must not execute. A
healthy product and a sleeping judge look identical from a pass rate.

Gate below the ceiling. 6 of 8, not 8 of 8. A gate pinned at the observed
maximum fails on variance alone, and one that cries wolf gets turned off.

Tell the judge what it must NOT judge. Numbers are verified by the kernel; a
judge re-checking arithmetic it cannot see invents disagreements.

Watch double negatives in field names. `buckets_are_exclusive: false` was
inverted by a judge on every case. A field name that traps a careful reader
traps a model too. Pass the fact, not the negation.

## 7. Verification habits

- Take the BEFORE before. It cannot be reconstructed afterwards.
- Verify by fetching the live URL. Never a green build.
- Print status codes. A `steps=0` was once read as "200 with an empty trace"
  when it was a 502 behaving correctly.
- Assert on CONTENT, not on HTTP 200. A shell that renders with no rows looks
  fine and says nothing.
- Screenshot every surface you change, in BOTH states. That habit caught three
  bugs the tests could not: stale branding, a 2.04 contrast failure below WCAG
  AA, and fees rendering `$0.00` while non-zero.

## 8. Do not "fix" these

Deliberate. Each blocks promotion by design.

- Synthetic fixture bars. Proves the verifier, not SPY.
- Differential replay, not independent. Same author, different shape.
- Four promotion blockers. The gap between green checks and BLOCKED is the
  product.
- `research_trust.py` outside the sealed set. That is what breaks the
  circularity. Sealed files are pinned `text eol=lf` in `.gitattributes`
  because the trust root hashes raw bytes and CRLF broke CI twice.
