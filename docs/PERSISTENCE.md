# Persistence

> Merge this into `README.md` — it belongs under **"Where scale actually binds"**
> in *Extending this to a multi-source knowledge graph*, which currently ends by
> naming the shared probe cache as future work. This is that work.

The service has a Postgres layer. It is **optional**: with no `DATABASE_URL` the
driver is never imported, every call is a no-op, and behaviour is byte-identical
to running with no database at all. That is tested, not asserted —
`tests/test_store.py::TestDisabledWithoutDatabaseURL` is the first tier of the
suite and always runs.

| File | Role |
| --- | --- |
| `migrations/001_init.sql` | `probe_cache` and `entity_alias`. Idempotent. |
| `app/store.py` | Async adapter. Inert without `DATABASE_URL`. |
| `docker-compose.yml` | Postgres 16 for local dev; applies the migration on first boot. |
| `tests/test_store.py` | 19 no-database tests + 27 integration tests. |

```bash
docker compose up -d --wait
export DATABASE_URL=postgresql://trialscope:trialscope@localhost:5432/trialscope
```

---

## Why exactly two things deserve state

The question is not "should this service have a database" — the role names
Postgres, and the temptation is to add one and find work for it. The question is
which state is *load-bearing*. The rule applied: **persist what is expensive to
re-derive and safe to replay.** Both halves must hold.

**1. The count-probe cache — expensive to re-derive.** A network query costs
40–57 upstream requests. The in-process `_BoundedCache` in `app/ctgov.py` already
turns a repeat probe from 309ms into 0.0ms, but it dies with the worker. N
instances share nothing while sharing the one resource that actually binds. That
is the whole bottleneck, restated below.

**2. The entity index — expensive to re-derive, and rebuilt offline anyway.**
`Merck Sharp & Dohme LLC`, `Merck & Co.` and `MSD` are one entity. Resolving that
is a batch job over full registry dumps, not something to redo per request.
`entity_alias` gives it a home with a documented shape. Note what the table
*cannot* hold: there is no count column, and a test asserts its absence. An alias
row is a **proposal**; it becomes a fact only when a count probe confirms it,
which is the same rule `app/agent.py`'s graph planner already applies to a
brand→generic resolution.

**What deliberately gets no state.** Plans, rendered visualization specs, traces,
LLM outputs, sessions. Every one of them is cheap to recompute and *derives from
counts that must be re-measured anyway*. Persisting a rendered spec would create
a second artifact that can disagree with a fresh measurement — a stored chart
saying 1,742 next to a live probe saying 1,749, with nothing to arbitrate. The
system's entire premise is that there is exactly one authority for a number.
Caching downstream of that authority manufactures a second one.

Same reason there is no request log, no query history, no "recent searches"
table. Each is a plausible product feature and none of them is in scope here.

---

## Why TTL is a correctness requirement, not a performance knob

This is the part that would be easy to get wrong, and getting it wrong would
quietly break the invariant the whole service is built on.

**Every number in a response is an API `totalCount`.** The in-process cache
already respects that: a hit returns a number *this process measured from the
API*, and the process is short-lived, so staleness is bounded by uptime without
anyone having to think about it. Persistence removes that accidental bound. A row
survives deploys, restarts, and weekends. Without a TTL, the first request on
Monday would serve Friday's count — and it would look exactly like a
measurement, because structurally it *is* one; it is just a measurement of a
corpus that no longer exists. ClinicalTrials.gov ingests new and updated
registrations daily.

So the freshness bound is not a tuning parameter that trades latency for
accuracy. It is the mechanism that keeps a cache hit honest. Three design
consequences:

**A count past its TTL is a MISS, not a hit with a caveat.** `get_count` returns
`None` and the caller re-probes. It does not return the row flagged
`stale=True`, because a flag downstream of a `dict.get()` is a flag nobody
reads, and "the number was there, we just annotated it" is how a stale value
reaches a chart. The row is invisible.

**A miss is `None`, never `0`.** Zero is a real and common answer from this API —
"no trials match". If a miss returned a default, a cold or disabled cache would
fabricate the measurement "no trials match" for every query in the system. There
is no code path in `app/store.py` that returns a number the system did not
measure.

**The TTL is evaluated in SQL, against the database clock.** `WHERE measured_at >
now() - ttl`, not a Python comparison against the worker's clock. N workers with
skewed clocks must not disagree about whether the same row is expired.

Supporting decisions, all in the same direction:

- `measured_at` is the *measurement* time and is distinct from `written_at`. An
  upsert cannot quietly refresh the age of an old count.
- `params` — the exact querystring — is stored with every row, so a served count
  is replayable against the live API. A count whose request you cannot
  reconstruct is unverifiable, so `put_count` refuses to store one with no params.
- TTL is clamped to `[1s, 7d]`, defaulting to 24h. A one-year TTL is not a cache,
  it is a stale dataset with a friendly name. The clamp is toward *fresher*,
  because the failure mode of a too-short TTL is cost and the failure mode of a
  too-long one is a wrong number.
- The write path rejects negatives, non-integers, booleans and implausible
  magnitudes. The cache must not become a laundering path for numbers that never
  came from a probe.

---

## How it addresses the shared-rate-limit bottleneck

From the README: *"Not the code — the upstream rate limit. A network query is
40–57 requests, so horizontal scaling makes it worse, not better."*

That is the precise shape of the problem. Adding a worker adds throughput against
*local* CPU and adds contention against the *shared* upstream budget. Two workers
answering "phases for pembrolizumab" independently issue the same five probes,
get the same five numbers, and spend twice the budget doing it. The API's
undocumented concurrency limit answers with `429`, `app/ctgov.py` backs off with
full jitter, and both requests get slower. Scaling out makes every instance
slower — the pathology the README names.

A shared cache changes what horizontal scaling costs. Probes are heavily
repetitive by construction: every phase question re-counts the same buckets for a
popular condition, the eval re-probes the same cases, and a network graph
re-counts a node once per pair it appears in. Once any instance has measured a
bucket, every instance can replay it for the rest of the TTL. The first worker
pays the upstream cost; the rest pay one local round-trip. Worker count stops
multiplying upstream load.

Two layers, not one:

| | `_BoundedCache` (L1) | `Store` (L2) |
| --- | --- | --- |
| Scope | one process | every instance |
| Cost | ~0ms | one round-trip |
| Lifetime | dies with the worker | survives, bounded by TTL |
| Bound | FIFO eviction at 2048 | TTL + bounded sweep |

L1 is not replaced. It stays the fast path; L2 catches what L1 misses. The
adapter is designed for `_BoundedCache` to *delegate to*, and `app/store.py`
deliberately does not import `app/ctgov.py` so the dependency runs one way only
and the service still boots with no database.

**Bounding.** `probe_cache` is keyed on user-controlled filters, which is the
same leak `_BoundedCache`'s eviction exists to prevent, on a slower fuse — disk
is bigger than RAM, so the growth is less dramatic and the surprise is worse.
`sweep_expired()` deletes past-TTL rows with a per-call limit, so a sweep can
never take an unbounded lock on the read path's table.

**Failure direction.** A database that is unreachable, misconfigured, missing its
driver, or failing mid-flight degrades to a miss. Reads return `None`, writes
return `False`, nothing raises into the request path. The reasoning: a cache miss
costs latency, a propagated cache error costs availability, and a stale hit costs
correctness. This layer will spend latency and never the other two. `errors` is
counted in `describe()` so silent full-cost operation is visible rather than
invisible.

**What this does not fix.** A shared cache reduces *duplicate* upstream load. It
does not coordinate *concurrent* load — N instances can still burst past the
limit on a cold cache, because each is pacing against its own semaphore. The
complete answer is the distributed token bucket the README also names, sharing
the throttle state the way this shares the counts. This table is the substrate
that would sit under it; it is not a substitute for it.

---

## Verification status — read this

**The 19 no-database tests pass. The 27 integration tests have NOT been run.**

Docker Desktop is installed on this machine but its engine could not be started
in a non-interactive session (the process exits immediately and the CLI context
reverts to `desktop-windows`), and the WSL distro has no Postgres and no
passwordless sudo to install one. So there was no real Postgres to run against.

The integration tests are therefore **skipped, with the reason printed**, rather
than mocked into passing. A mock would only prove the mock does what it was
written to do — the behaviours under test (ON CONFLICT convergence, the TTL
predicate on the database clock, the CHECK constraints) live in Postgres, not in
Python. A skip says "unverified", which is true. A mocked pass would say
"verified", which would not be.

To verify:

```bash
docker compose up -d --wait
TEST_DATABASE_URL=postgresql://trialscope:trialscope@localhost:5432/trialscope \
  uv run --no-sync pytest tests/test_store.py -v
```

The 27 skips should become passes. They cover: round-trip with params intact;
measured zero surviving as zero; a 48h-old count reported as a miss; the TTL
boundary just inside and just outside; the same row served by a long TTL and
expired by a short one; order-independent keys hitting exactly one row; 32
concurrent writes of one key converging on one row; the same across four
independent connection pools; mixed concurrent read/write never yielding a torn
value; the write guards; the entity-alias shape; and migration idempotency.

---

## Not yet wired in

`app/store.py` is complete and standalone. It is **not** connected to
`app/ctgov.py` or `app/main.py` — those files were out of scope for this change,
and the integration needs a decision (below) rather than a mechanical edit. The
required diff is in the handover notes accompanying this change.

The decision worth surfacing: **L1 has no TTL today, and that is currently safe
only by accident.** `_BoundedCache` entries never expire, which is fine while the
process is the staleness bound. The moment L1 is warmed from a persistent L2, a
long-lived worker can serve a count older than the TTL out of memory — the TTL
would be enforced on the shared layer and bypassed on the fast one. So wiring
this up correctly means giving `_BoundedCache` an expiry too, not just adding a
delegation call. That is a change to the invariant's enforcement, which is why it
is being raised rather than made quietly.
