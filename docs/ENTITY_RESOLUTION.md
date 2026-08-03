# Entity resolution (merge into README.md under "Extending this to a multi-source knowledge graph")

> **Merge instruction.** README.md is not owned by this change. The section below is written
> to slot in directly after the existing **"What breaks first: entity resolution"** paragraph,
> which currently ends with *"…verifies a brand→generic resolution with a count probe before it
> is allowed to affect a query."* Everything below is new prose; nothing above it needs editing.

---

### Closing it: hybrid retrieval that proposes, and a count probe that decides

The limitation named above is now implemented rather than only described. `app/entities.py`
resolves aliases with two independent retrievers, and does so without loosening the rule that
every number in a response is an API `totalCount`.

| Stage | Mechanism | May produce |
|---|---|---|
| **Propose** | SQLite FTS5 + bm25, unioned with static-embedding cosine | a ranked list of *names* |
| **Confirm** | one count probe per candidate, under the caller's own filter | the number |

`resolve(query, kind, top_k)` performs no network I/O at all — a test breaks `httpx` and
asserts resolution still works, which makes the guarantee structural: code that cannot reach
the registry cannot obtain a `totalCount`, and therefore cannot present one. It returns
`Candidate` objects that are frozen, carry `verified=False`, and carry `count=None`. The
dataclass rejects the invalid state outright: a candidate holding a count while unverified, or
claiming verification with a zero count, raises rather than constructs.

`verify_candidates(candidates, base_filter, client)` is the only code permitted to write
`count`, and it writes exactly what `CTGovClient.count` returned. **A candidate whose probe
returns 0 is discarded, not kept with a zero.** A probe that *fails* also discards — a
transport error must never resolve in favour of the candidate. Crucially, **which retriever
proposed a candidate makes no difference here**: adding a second proposer must not add a
second trust level, so bm25 hits, semantic hits and registry-alias hits are all confirmed by
the same probe.

Results are ordered by the probe count, not by retrieval rank — when the index and the
registry disagree, the registry wins.

### Why FTS5, and why it is the choice that ports

The index is a few thousand short strings. That is a text-retrieval problem, and FTS5 is a
real, boring answer: an inverted index with Okapi bm25, in the standard library, no server.
The table is declared `content='entities'` — **external content** — so the FTS structure
stores only the inverted index and reads document text back from the source table on demand.
`AFTER INSERT / DELETE / UPDATE` triggers keep the two in sync; with external content that is
not bookkeeping, it is the only thing making the index correct.

It also ports. Postgres `tsvector` / `ts_rank` is the same idea with the same shape, so this
schema and query translate almost line for line — which a bespoke vector similarity would not.

### The second retriever, and an honest report on what it bought

Static embeddings (`model2vec`, `potion-base-8M`, CPU-only, no API) are baked into a BLOB
column on the same table. The request path needs numpy and nothing else: no model, no network,
no server. Verified by import-blocking both `model2vec` and `httpx` and resolving anyway.

The two retrievers are **unioned, never blended**. bm25 (negative, corpus-relative) and cosine
(in [-1, 1]) are on incomparable scales, and any weighted sum would be a made-up constant
dressed as relevance. Candidates proposed by both rank first — agreement between independent
methods is evidence a single score cannot express — and `Candidate.source` records
`fts` / `semantic` / `both`.

**What the semantic arm actually did, measured.** It did not deliver the bridge it was added
for. Cosines from the shipped model:

```
Keytruda      <-> Pembrolizumab            0.052    the pair we wanted bridged
MSD           <-> Merck Sharp & Dohme LLC  0.088    the other pair we wanted bridged
Opdivo        <-> Nivolumab                0.403    a true pair it does get
Pembrolizumab <-> Omalizumab               0.418    a FALSE pair, scoring higher than both
```

It ranks a wrong pair above a right one, and **no threshold separates them**. The cause is
structural, not a tuning problem: nearest-neighbour search over entity names retrieves
entities of the same *category*, not the same *identity*. Anchored on Pembrolizumab, the
model's neighbours in the live index are Trastuzumab (0.549) and Rituximab (0.512) — other
monoclonal antibodies, correctly "similar", useless as aliases. Both are real drugs, so both
**passed** a count probe (7 and 5 melanoma trials). The probe protects against fabricated
numbers; it does not protect against a sibling being mistaken for a synonym.

So `MAX_SEMANTIC_ONLY` defaults to **0**: the semantic arm is retained for what it
demonstrably does well — corroboration, contributing the `both` tag — and is not permitted to
introduce candidates alone. The knob is a module constant, so the tradeoff stays visible
rather than buried in a threshold. Brand→generic bridging is instead carried by the registry's
own `InterventionOtherName` field, which is evidence rather than inference: pembrolizumab's
entry lists `Keytruda`, `MK-3475`, `Lambrolizumab` and a dozen biosimilar codes because real
sponsors filed them.

### The corpus is bounded, and says so

40,000 studies paged; the build reports exactly what it kept:

```
intervention: saw 43,891 distinct normalised forms; 38,805 appeared on <2 studies;
              5,086 eligible; CAP=2500 -> kept 2,500, dropped 2,586
sponsor:      saw 10,327 distinct normalised forms;  6,704 appeared on <2 studies;
              3,623 eligible; CAP=2500 -> kept 2,500, dropped 1,123
```

5,000 entities in **one 4.59 MB SQLite file**, committed. `index_status()` surfaces the cap
report, for the same reason the network builder warns that its shortlist came from a sample.

### Worked examples, run against the live API

**Alias → canonical, scoped.** `"Keytruda"` in melanoma. Both spellings collapse to one
entity and one number, and both retrievers agree:

```
PROPOSED:  'Pembrolizumab'  bm25= 0.000 cos=1.000 [alias/both]  UNVERIFIED
           'MK-3475'        bm25=-8.058 cos=0.781 [bm25/both]   UNVERIFIED
CONFIRMED: 'Pembrolizumab'  count=339
           'MK-3475'        count=339
```

**Retrieval proposed, the probe rejected.** `"Merck & Co."` in melanoma — three of four
proposals killed by the API:

```
CONFIRMED: 'Merck Sharp & Dohme LLC'   count=39
REJECTED:  'Merck KGaA, Darmstadt, Germany'                          probe returned 0
           'Acceleron Pharma, Inc., ... subsidiary of Merck & Co.'   probe returned 0
           'Trius Therapeutics LLC, a subsidiary of Merck & Co.'     probe returned 0
```

**FTS MATCH fails, LIKE fallback resolves.** FTS5's query language rejects a great deal of
ordinary text, so the fallback is a constantly-exercised path rather than defensive decoration:

```
QUERY "Guy's Hospital"
  retrieval arm: FTS5 MATCH REJECTED (fts5: syntax error near "'") -> LIKE fallback
  CONFIRMED: "Guy's and St Thomas' NHS Foundation Trust"  count=289   [like-fallback/both]
```

Measured breakages: `'` → *syntax error*, `&` → *syntax error*, `.` → *syntax error*,
`MK-3475` → *no such column: 3475*, unbalanced `"` → *unterminated string*. (A *balanced*
pair of quotes parses fine as a phrase query — an assumption a test caught me getting wrong.)

**No lexical footing, and no semantic rescue.** `"MSD"` proposes **nothing**. It shares no
token with `Merck Sharp & Dohme LLC` and the embedding does not bridge it (0.088). Returning
nothing is the correct answer: proposing the nearest unrelated string would be worse, because
a probe would happily confirm that unrelated sponsor exists.

### Two bugs found by measurement, root-caused rather than patched

1. **`"Merck & Co., Inc."` normalised to `merck and`.** `&`→`and` expanded *before*
   legal-suffix stripping, leaving a dangling conjunction whose retrieval was dominated by
   "and" — matching every *"X and Y NHS Trust"* in the registry. The top Merck proposal was an
   orthopaedic hospital, which would have probed non-zero and laundered straight through.
2. **The LIKE fallback ranked by popularity, not relevance.** It ordered by `doc_freq`. For
   `"Guy's Hospital"`, the token `hospital` appears in **513 of 2,500** sponsors and `guy` in
   exactly **1** — and the 1 was correct. Popularity ordering buried it under Massachusetts
   General and five other large hospitals. Fixed with summed **inverse document frequency**, a
   computed property of the corpus and the same principle bm25 uses — not a tuned constant.
   That distinction matters here, because a tuned constant provably cannot work: the correct
   seed for `merck` and the garbage seed for `merck and` scored 0.250 and 0.256.

The same reasoning is why an **absolute token-containment gate** guards the lexical arm rather
than a score cutoff. bm25 is corpus-relative — the same value means different things in two
queries — so a candidate must share an identity-bearing token (or a ≥4-character prefix) with
the query. That is a condition, not a threshold.

### What this does *not* claim

A probe verifies **existence under the filter**, not **relevance**. `MedImmune LLC` once
survived a Merck query with a genuine count of 4 — a real sponsor with real melanoma trials,
simply not Merck. The invariant that holds is the one that matters for correctness: no number
in any response was invented, and every candidate that reached a result was counted. Ranking
quality is a separate, softer problem, and the honest next step is a labelled alias set to
calibrate against — not another retriever.
