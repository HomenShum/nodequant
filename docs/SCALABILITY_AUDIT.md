# Scalability audit — memory and concurrency

Two audits of the request path, run against the code at `385 passed, 29 skipped`.
Both produce measurements. Nothing in this document is an opinion about whether
something "looks fine"; every row carries the number that says so, and every
number is reproducible from a test in this repository.

```
uv run --no-sync pytest tests/test_memory_scalability.py tests/test_concurrency.py -q -s
27 passed
```

All output pasted below is real, copied from those runs on Windows 11 / CPython
3.11. Measurements are `tracemalloc` deltas against a snapshot taken after
`gc.collect()` and after the collection under test already exists, so the number
is what the collection retains, not import cost.

**Scope note.** This audit did not modify `app/**`. Where a fix is warranted the
exact minimal diff is given at the end, unapplied.

---

## Method note — the finding that invalidated the first attempt

The first version of the concurrency harness used `httpx.MockTransport` with a
synchronous handler. Under it, eight concurrent `count()` calls on the same key
produced:

```
upstream requests : 1   cache={'entries': 1, 'hits': 7, 'misses': 1}
```

That reads as "the L1 cache deduplicates concurrent identical queries". It does
not. A synchronous mock handler never suspends, so each coroutine ran to
completion before the next one started — the mock serialised the test and the
race could not occur. The same probe against a transport with a real
`await asyncio.sleep()` produced:

```
upstream requests : 8   cache={'entries': 1, 'hits': 0, 'misses': 8}
```

**A concurrency test built on a transport that cannot yield is a test that
cannot observe a race.** Both cooldown findings below were invisible under the
sync mock for the same reason. `tests/test_concurrency.py::_SlowTransport` exists
because of this, and every overlap test in the file uses it.

---

# Audit 1 — memory and unbounded growth

## Every in-memory collection

| # | Collection | Where | What bounds it | Eviction | Measured under 10k distinct keys / 1k-turn loop | Verdict |
|---|---|---|---|---|---|---|
| 1 | `_BoundedCache._data` (count-probe L1) | `app/ctgov.py:106` | 2,048 entries + 24 h monotonic TTL | FIFO, oldest quarter dropped per overflow | 10,000 distinct keys → **1,808 retained, 422.6 KB**; unbounded counterfactual **1,096.8 KB** and climbing | **Bounded.** Evicts. |
| 1b | same, worst-case key length | — | entries only — **not bytes** | as above | 2,025-char keys → **3,976.5 KB (3.88 MB)**, plateaus | **Bounded, ceiling worth knowing** |
| 2 | `MemoryStore._sessions` | `app/memory.py:281` | 256 sessions | `OrderedDict` **LRU** (`move_to_end`) | 1,000 turns / 500 sessions → **256 retained, 733.5 KB**; last 400 turns added **0.5 KB** | **Bounded. Plateaus.** |
| 3 | `ConversationMemory._turns` | `app/memory.py:174` | 8 turns **and** 16,384 bytes, enforced on write | oldest-first, never to empty | 2,000 deliberately oversized turns → **6 turns, 14,946 bytes, 43.6 KB**; last 500 turns added **0.1 KB** | **Bounded twice. Plateaus.** |
| 4 | `Turn.labels` / `.query` | `app/memory.py:128,197` | 24 labels × 64 chars; 500-char query | truncation at write | oversized inputs truncated before storage | **Bounded by type** |
| 5 | `EntityIndex._vectors` | `app/entities.py:328` | build-time corpus cap (2,500 rows × 256 dims) | none — process-lifetime | first `vectors()` **2,772.1 KB**; next 50 calls **0.3 KB**; two kinds **5,580 KB** | **Bounded, paid once** |
| 6 | `entities._CACHE` | `app/entities.py:388` | **no cap, no eviction** — keyed `(kind, db_path)` | none | 1,000 `load_index` calls added **0 entries** | **Bounded in practice** (one artifact dir), see finding M3 |
| 7 | SQLite page cache (per connection) | implicit | SQLite `cache_size` default (~2 MB/conn) | SQLite's own | 600 distinct `resolve()` calls → **177.6 KB, 303 bytes/query**, flattening | **Bounded by SQLite** |
| 8 | `executor` per-request `buckets` / `counts` / `cites` / `series_of` | `app/executor.py:402-458` | `MAX_PROBES_PER_REQUEST = 60` | request-scoped; freed on return | 61 probes → **`CTGovError` raised before any request is issued** (`request_count == 0`) | **Bounded. Refuses, does not truncate.** |
| 9 | `executor` `crossed` (comparison cross-product) | `app/executor.py:443` | built **before** the 60-probe cap fires | request-scoped | `compare_values` ≤ 4 (plan validator) × widest bucket vocab 15 → **60 entries** max pre-cap | **Bounded by plan validation** |
| 10 | `Store` (asyncpg L2) | `app/store.py` | pool `min/max`, counters only | n/a | no per-key in-memory map — every key lives in Postgres | **Nothing to bound** |
| 11 | `main.py` `seen` (citation dedupe) | `app/main.py:348` | response size, itself capped by #8 | request-scoped | — | **Bounded** |
| 12 | `client.request_count` / `retry_count` | `app/ctgov.py:193` | Python int | never reset | monotonic counters, O(1) | **Not a collection** |

### Total resident footprint at cap

```
  [footprint at cap, KB]
    ctgov._BoundedCache (typical keys) :    420
    ctgov._BoundedCache (worst keys)   :   3974
    memory.MemoryStore  (256 sessions) :    725
    entities vector matrices (2 kinds) :   5580
    ---------------------------------------------
    worst-case steady state            :  10279 KB (10.0 MB)
```

**Every bounded collection in the process sums to ~10 MB of steady state at
every cap simultaneously.** That is the number to size a worker with. There is
no collection whose growth is a function of traffic volume.

### The required measurements

**(1) Fill each bounded collection past its cap with 10,000 distinct keys.**

```
  [_BoundedCache, 10k distinct short keys]
    after   2500 ops:      388.7 KB
    after   5000 ops:      434.4 KB
    after   7500 ops:      429.0 KB
    after  10000 ops:      422.6 KB

  [plain dict, same 10k key stream (counterfactual)]
    after   2500 ops:      267.3 KB
    after   5000 ops:      544.1 KB
    after   7500 ops:      872.1 KB
    after  10000 ops:     1096.8 KB
    entries retained: 1808 (cap 2048)
    bounded 422.6 KB vs unbounded 1096.8 KB -> 2.6x saved
```

The bounded curve is flat from op 2,500 onward. The unbounded curve is a
straight line. It **evicted**; it did not grow. (The counterfactual is measured
in the same process rather than reasoned about, because "an unbounded dict would
be worse" is an assertion until someone runs it.)

**(2) Sustained agent loop: 1,000 distinct query shapes across 500 sessions.**

```
  [MemoryStore: 1000 turns across 500 sessions]
    after    200 ops:      560.5 KB
    after    400 ops:      731.0 KB
    after    600 ops:      732.9 KB
    after    800 ops:      733.3 KB
    after   1000 ops:      733.5 KB
    sessions retained: 256 (cap 256)
    growth in first 400 turns: 731.0 KB
    growth in last  400 turns: 0.5 KB
```

**It plateaus.** The last 400 turns cost **0.07 %** of what the first 400 cost.
The assertion in the test is on the *shape* of the curve (`last_400 < 0.05 ×
first_400`), not on an absolute ceiling, because a ceiling of "under 50 MB" is
satisfied by a leak that is merely slow.

`MemoryStore` is also the one cache in the system with the right eviction
policy: 500 drive-by session ids against `max_sessions=8`, with `alice` touched
every iteration, retains 8 sessions **including `alice` and her turn** — LRU
protects the active session. Contrast finding M1.

**(3) The worst unbounded thing that remains.**

There is no collection left whose size grows with traffic. The worst *remaining*
memory defect is a **bounded-but-unpredictable ceiling**, and a **leak of
non-memory resources**, not a growing dict:

---

### M1 — `_BoundedCache` is FIFO, so a key read on every request is still evicted

```
  [FIFO vs LRU] hot key read 3000 times: survived=False
    hits=2047 misses=954 -> 31.8% miss rate on a key that was
    present in every single request
```

`get()` does not reorder. The base `total_matching` probe for a popular
condition — re-read by every request that touches it — is evicted by a stream of
cold keys anyway. **954 avoidable upstream probes** in a 3,000-request workload
where the key was never absent from the working set.

This is a *cost* defect, not a correctness one: a miss re-measures from the API
and returns the same guarantee. But at `MIN_REQUEST_INTERVAL_S = 0.18` each
avoidable probe costs 180 ms of a rate budget every request shares — **171 s of
shared pacing** across that workload. `MemoryStore` two modules away already uses
`OrderedDict` + `move_to_end`; the L1 does not.

**Severity: P2.** Real, measured, cheap to fix, no correctness impact.

### M2 — the cap is 2,048 *entries*; the byte ceiling is set by data we do not control

```
    worst-case key: 2025 chars; entries retained 1808
    resident ceiling at cap: 3976.5 KB (3.88 MB)
```

versus **422.6 KB** for the same entry count with short keys — a **9.4× swing**
that `CACHE_MAX_ENTRIES = 2048` gives an operator no way to predict.

Root cause, traced upstream: `PlanFilters` caps each free-text field at 100-200
chars, so the *request-validated* path is safe. But network-graph and
top-N-sponsor probes build `AREA[...]` clauses out of entity names discovered
from the API sample (`executor.resolve_top_values` → `_esc`), and
`executor._esc:110` strips quotes **without truncating**. So the key length on
the hottest cache path is bounded by ClinicalTrials.gov's data, not by our
validation.

It still plateaus — this is a sizing surprise, not a leak. **Severity: P2 /
acceptable-by-design once documented**, which is what this row is for.

### M3 — `entities._CACHE` has no cap and no eviction

Keyed `(kind, db_path)`, so its size is the number of distinct *artifact
directories* opened, not the number of queries. Measured: **1,000 `load_index`
calls added 0 entries**. In the service this is 2 entries forever. It grows only
when a caller points `load_index` at a new path — which the test suite does with
temp dirs (10 entries at end of suite). **Severity: P3, acceptable-by-design.**
Flagged because "unbounded map" and "bounded in practice" are different claims
and only one of them is written in the code.

---

# Audit 2 — concurrency and shared state

Shared state inventory (things that outlive one request):

| State | Owner | Mutated by | Lock | Finding |
|---|---|---|---|---|
| `_BoundedCache._data` | `CTGovClient` (one per process) | every `count()` | **none** | C1 — read-then-write race |
| `_next_slot` | `CTGovClient` | `_pace`, `_apply_cooldown` | `_pace_lock` | C2 — cooldown does not reach reserved slots |
| `_semaphore` | `CTGovClient` | every `_get` | asyncio | correct; bounds C2's blast radius |
| `request_count` / `retry_count` | `CTGovClient` | every `_get` | none | safe — no `await` between read and write |
| `MemoryStore._sessions` | `app.state` | every `/api/chat` | `threading.Lock` | store is correct; **C3 — the key is not** |
| `ConversationMemory._turns` | per session | `record()` | `threading.Lock` | correct under 8 concurrent writers |
| `entities._CACHE` | module | `load_index` | `_CACHE_LOCK`, **released between check and act** | C5 — orphaned connections |
| `EntityIndex.connection` | module | every `resolve()` | `EntityIndex._lock` | correct |
| `Store._pool` | `app.state` | every L2 read/write | `_connect_lock` | correct (double-checked under lock) |

---

## C1 — `_BoundedCache` read-then-write race: N identical queries cost N probes

**Reproduction:** `test_concurrent_identical_queries_duplicate_upstream_work`

```
  [L1 race] 8 concurrent identical count() calls
    upstream requests : 8  (ideal 1)
    cache             : {'entries': 1, 'hits': 0, 'misses': 8, 'expired': 0}
    redundant probes  : 7 (88% wasted)
    rate-budget cost  : 1.26s of shared pacing spent on duplicates
```

`count()` does `self._cache.get(params)` → miss → `await self._get(...)` →
`self._cache.put(...)`. There is an `await` between the miss and the write and no
in-flight registry, so every concurrent caller that misses issues its own probe.

**Is the result correct? Yes.** All eight callers returned `4242`, and the cache
ends with exactly **one** entry holding that value — the racing writes are
byte-identical because they are the same measurement of the same filter. There is
no torn read and no wrong number. The cost is purely duplicated upstream work:
**7 of 8 probes wasted, 1.26 s of a shared rate budget**, against an API that
returns 429 on a burst of 12.

**Control:** the same key stream with no overlap costs exactly what it should —

```
  [L1 sequential] 8 identical count() calls -> upstream=1 cache={'hits': 7, 'misses': 1}
```

So the cache is not broken; the race is unguarded. **Severity: P2** — cost and
rate-limit pressure, not correctness. It compounds with M1: FIFO eviction creates
misses, and concurrent misses multiply into duplicate probes.

## C2 — the shared cooldown does not reach requests that already reserved a slot

**This was flagged as a possible hole. It is real.** `_pace()` computes its wait
*inside* the lock and sleeps *outside* it:

```python
async with self._pace_lock:
    now = loop.time()
    wait = self._next_slot - now                     # captured here
    self._next_slot = max(now, self._next_slot) + MIN_REQUEST_INTERVAL_S
if wait > 0:
    await asyncio.sleep(wait)                        # a cooldown applied now cannot change this
```

`_apply_cooldown` pushes `_next_slot` forward. That correctly delays every request
that has not yet *entered* `_pace`. It cannot delay a request already sleeping on
a wait computed from the pre-cooldown schedule.

**Reproduction 1 — the mechanism** (`test_cooldown_does_not_reach_requests_that_already_reserved_a_slot`):
six requests reserve slots, then a 1.5 s cooldown is applied.

```
  [cooldown hole] cooldown applied at t=0.020s, window ends t=1.520s
    _pace() releases: [0.0, 0.192, 0.362, 0.547, 0.732, 0.905]
    RELEASED INSIDE THE COOLDOWN WINDOW: 5 of 6
```

**Every reservation made before the cooldown escapes it.** The cooldown is
advisory for anyone already in the queue.

**Reproduction 2 — the blast radius on the real path**
(`test_cooldown_escapees_on_the_real_path_are_bounded_by_the_semaphore`): an
8-probe fan-out where the first probe returns 429.

```
  [cooldown blast radius] 8-probe fan-out, first probe 429s
    request start offsets: [0.0, 0.196, 0.366, 2.048, 2.232, 2.402, 2.586, 2.775, 2.945]
    started during the 1.5s cooldown: 2
    MAX_CONCURRENT_PROBES = 3 -> bound is 2
```

**2 escapees, not 8.** `_pace()` is only reached while holding the concurrency
semaphore, so at most `MAX_CONCURRENT_PROBES - 1 = 2` requests can hold a live
reservation when the 429 lands. The other 5 waited the full cooldown (first
post-cooldown start at 2.048 s vs cooldown end at ~1.52 s). The semaphore is what
keeps this from being a retry storm.

**Verdict: a real hole with a bounded blast radius.** MEASUREMENTS.md entry #4
describes fixing a retry storm with a *shared* cooldown; that fix is 2/3 effective
rather than complete, and the residual 2 requests are exactly the ones most likely
to re-trip the limit they just tripped. **Severity: P2.** Not P1 only because the
semaphore caps it at 2 and full-jitter retry decorrelates the follow-up.

**Positive control** (`test_pacing_is_shared_across_concurrent_callers`) — the
schedule genuinely is shared, not one per task:

```
  [shared pacing] inter-request gaps: [0.195, 0.17, 0.185, 0.173, 0.185]
  (MIN_REQUEST_INTERVAL_S=0.18)
```

## C3 — REAL CORRECTNESS BUG: every `/api/chat` caller shares one session

`app/main.py:315`

```python
session = app.state.memory.for_session("default")
```

`MemoryStore` supports session isolation and implements it correctly. Nothing
plumbs a session id to it, so **the isolation is bypassed for every request** and
all concurrent callers read and write one `ConversationMemory`.

**Control — the store itself is correct** (`test_memory_store_isolates_distinct_session_ids`):

```
  [session isolation] alice follow-up -> drug=Pembrolizumab condition=None
```

**Reproduction at the memory layer** (`test_shared_default_session_leaks_one_callers_filters_into_anothers_followup`).
Alice asks about Pembrolizumab; Bob asks about Melanoma; Alice asks a follow-up:

```
  [shared-session leak] alice asked about Pembrolizumab; her follow-up resolved to:
    query     = 'Melanoma trials in PHASE3 by country'
    drug_name = None        <- Alice's subject, LOST
    condition = 'Melanoma'  <- Bob's subject, INHERITED
```

**Reproduction end-to-end over the documented HTTP surface**
(`test_shared_default_session_leak_reproduces_end_to_end_over_api_chat`), three
`POST /api/chat` requests against a mocked upstream, reading `filters_applied`
straight off the trace panel:

```
  [end-to-end /api/chat] filters_applied per request
    alice #1 : {'query.intr': 'Pembrolizumab'}
    bob   #1 : {'query.cond': 'melanoma'}
    alice #2 : {'query.cond': 'melanoma', 'filter.advanced': '(AREA[Phase]PHASE3)'}
```

Alice's drug filter is gone. Her follow-up was answered about Bob's condition,
grouped by Bob's dimension.

**Why this is the most serious finding in either audit.** The returned count is
honestly measured, cited, traced, and reconcilable — every consistency check this
system has passes on it. `app/memory.py`'s own header names this exact failure:

> a *real* number attached to the wrong question, so every consistency check the
> system has (citations, trace, reconciliation) passes on it.

The module was built to prevent it and does prevent it. The composition
reintroduces it at the call site — the same shape as MEASUREMENTS.md #12
(`connect()` with no `close()`): a module with green unit tests, and a bug living
entirely in the wiring.

**Severity: P0 for any deployment with more than one concurrent user.** With a
single user it is invisible, which is why it survived.

**Related, lower severity** (`test_session_evicted_mid_request_becomes_a_silent_write_to_nowhere`):
`for_session` hands out the object, and LRU eviction later drops the map entry. A
request holding the reference keeps writing to an orphan.

```
  [orphaned session] held object turns=2, store's alice turns=0
```

Nothing raises and nothing is corrupted; the turn is simply not there next time.
At `MAX_SESSIONS = 256` this needs 256 distinct sessions inside one request's
lifetime. **Severity: P3, acceptable-by-design** — but it is a lost write, not a
no-op, and worth knowing before anyone lowers the cap.

**Not a data race.** Given that they *do* share a session, the sharing is at least
thread-safe (`test_concurrent_writers_to_one_session_lose_no_turns_and_respect_bounds`):

```
  [concurrent writes] 8 threads x 200 turns -> turns=8 bytes=2968 errors=0
```

1,600 concurrent writes, both bounds respected, every retained turn a real turn
someone wrote. C3 is a **keying** bug, not a locking bug — worth separating,
because the fixes are different and only one of them is in `main.py`.

## C4 — one request's failure cannot corrupt another's state

**Reproduction** (`test_upstream_failure_mid_flight_does_not_corrupt_concurrent_requests`):
a non-retryable `CTGovError` (HTTP 400) raised while two other probes overlap on
the same client.

```
  [failure isolation] 1 failed, 2 succeeded concurrently
    survivor results        : [99, 99]
    cache entry for failure : None
    post-failure count      : 99
```

Asserted on results, not on absence of exceptions:

- the survivors returned **their own** measurement (`99`), not a default or a leak;
- the cache holds **no** entry for the failed key — `put()` is after the raise, so
  a failure cannot poison a peer's read;
- the client is still usable afterwards (`post-failure count: 99`), so the shared
  pacing schedule was not corrupted;
- exactly one exception, of the right type.

**Reproduction 2** (`test_a_failed_turn_is_not_recorded_as_inherited_context`): a
failed request must not leave memory claiming a subject it never measured.

```
  [failed turn] follow-up subject after a failed request: 'Pembrolizumab'
```

`main.py` records the turn only *after* `execute()` returns, so a failed turn
never becomes inherited context. **Verdict: correct by design, verified.**

## C5 — `load_index` check-then-act orphans SQLite connections on cold start

**Reproduction** (`test_concurrent_cold_start_opens_and_orphans_sqlite_connections`):

```
  [load_index race] 8 concurrent cold callers
    file connections opened : 8
    retained in _CACHE      : 1
    ORPHANED                : 7 (still open and unreachable: 7)
```

`load_index` checks `_CACHE` under `_CACHE_LOCK`, **releases the lock**, opens the
connection, then re-acquires to store it. Concurrent missers each open one; the
last write wins and the other 7 are unreachable — `clear_cache()` iterates
`_CACHE.values()` and so cannot close them.

FastAPI runs sync work in an anyio threadpool (default 40 workers), so a cold
start under load reaches this on the first N concurrent entity queries.

**Correctness is unaffected** — every handle points at the same read-only file
opened `mode=ro`, and the shared-handle path is itself thread-safe
(`test_entity_index_handle_is_safe_to_share_across_threads`: 8 threads × 25 reads
→ `distinct results={2500}, errors=0`). The cost is file descriptors plus ~2 MB
of SQLite page cache each, reclaimed only when CPython's refcount happens to drop
them. **Severity: P2** — a bounded, one-time cold-start spike (≤ threadpool size),
not a leak that grows with traffic.

---

# What breaks FIRST under load — ranked, with the number

| Rank | What | Trigger | The number | Real bug or by design |
|---|---|---|---|---|
| **1** | **C3 — shared `"default"` session** | The **second concurrent user**. Not load-dependent; concurrency = 2 is enough. | Alice's `query.intr: Pembrolizumab` → `query.cond: melanoma` on her own follow-up. **1 wrong-question answer per interleaved follow-up**, and it passes every consistency check the system has. | **Real bug. P0.** |
| **2** | **C2 — cooldown escapees** | The first 429, which arrives at ~12 concurrent requests per MEASUREMENTS.md #4. | **2 of 8** probes fire inside the 1.5 s cooldown; bounded at `MAX_CONCURRENT_PROBES - 1 = 2`. | **Real hole, bounded blast radius. P2.** |
| **3** | **C1 — duplicate in-flight probes** | Any two concurrent requests asking the same question — the eval, a refresh, a fan-out sharing a base filter. | **8 concurrent identical queries → 8 probes (7 wasted, 88 %), 1.26 s of shared rate budget.** Feeds directly into rank 2. | **Real, cost only. P2.** |
| **4** | **M1 — FIFO evicts the hot key** | A working set wider than 2,048 distinct probes — one busy hour. | **31.8 % miss rate on a key present in every request**; 954 avoidable probes per 3,000, ≈ 171 s of pacing. | **Real, cost only. P2.** |
| **5** | **C5 — cold-start connection orphans** | First N concurrent entity queries after a deploy. | **8 concurrent callers → 7 orphaned open connections**, ~2 MB page cache each. Bounded by threadpool size, once. | **Real, bounded. P2.** |
| **6** | **M2 — byte ceiling of the L1** | Long API-derived entity names in `AREA[...]` probes. | **3.88 MB vs 0.41 MB** for the same 2,048 entries — a 9.4× sizing swing. | **By design, now documented. P2/P3.** |
| **7** | **C3b — orphaned session write** | 256 distinct sessions within one request's lifetime. | Held object 2 turns, store 0 turns. | **By design at MAX_SESSIONS=256. P3.** |
| **8** | **M3 — `entities._CACHE` uncapped** | A caller pointing `load_index` at new paths. | 1,000 calls → **0 new entries** in service configuration. | **By design. P3.** |

**Memory is not what breaks first.** Every collection plateaus; the whole process
sits under **10 MB** with every cap saturated simultaneously, and 1,000 turns
across 500 sessions add **0.5 KB** over the last 400. The caps in `app/memory.py`
and `app/ctgov.py` do what their comments claim.

**What breaks first is correctness under concurrency, at N = 2 users**, followed
by rate-limit efficiency. Ranks 2, 3 and 4 compound: FIFO eviction manufactures
misses, concurrent misses become duplicate probes, duplicate probes trip the 429,
and 2 requests then ignore the cooldown meant to recover from it.

---

# Minimal diffs (NOT applied — `app/**` was out of scope for this audit)

### Fix 1 — C3, the P0. `app/main.py`

**Verified, not assumed.** The id is already on the wire end to end:

- `web/node_modules/@assistant-ui/react-ai-sdk/dist/ui/use-chat/AssistantChatTransport.js:21-45`
  overrides `prepareSendMessagesRequest` and emits `body: { ...optionsEx.body, id, messages, trigger, messageId, metadata }`,
  where `id` is the thread `remoteId` falling back to the chat id.
- `web/app/api/trialscope-chat/route.ts:22` forwards it verbatim
  (`JSON.stringify({ ...body, messages, tools: [] })`).
- `app/main.py:250` takes `request: dict[str, Any]`, so `request["id"]` is
  already in hand and is currently discarded.

Three lines:

```diff
--- a/app/main.py
+++ b/app/main.py
@@
-        session = app.state.memory.for_session("default")
+        # One conversation per chat id. Keying every caller on a constant makes
+        # MemoryStore's isolation unreachable, so two concurrent users inherit
+        # each other's filters — an honestly-measured count for a question
+        # nobody asked. Falls back to "default" only when no id is supplied.
+        session_id = request.get("id") or request.get("chatId") or "default"
+        session = app.state.memory.for_session(str(session_id)[:128])
```

Verification: `tests/test_concurrency.py::test_shared_default_session_leak_reproduces_end_to_end_over_api_chat`
should then show `alice #2 : {'query.intr': 'Pembrolizumab', 'filter.advanced': ...}`,
and its two `assert`s must be inverted along with the C3 row above.

*Caveat worth measuring before shipping:* an unauthenticated endpoint keyed on a
client-supplied id mints one session per distinct id. That is exactly the case
`MAX_SESSIONS = 256` LRU exists for, and `test_memory_store_plateaus_...` already
measures it at 500 distinct ids → 256 retained, 733.5 KB. The truncation to 128
chars bounds the key itself.

### Fix 2 — C2, the cooldown hole. `app/ctgov.py`

Do not try to fix this by reshuffling `_next_slot` reservations — that is where
the fragility lives. Track the cooldown as its own **absolute deadline** and
re-check it after the pacing sleep, so "am I allowed to start?" is answered at
the moment of starting rather than at the moment of queueing:

```diff
--- a/app/ctgov.py
+++ b/app/ctgov.py
@@ class CTGovClient.__init__
         self._next_slot = 0.0
+        # A cooldown is a deadline, not a schedule offset. _next_slot only
+        # binds requests that have not yet reserved a slot; this binds the
+        # ones already asleep on a wait computed before the 429 arrived.
+        self._cooldown_until = 0.0
@@ async def _pace(self) -> None:
         async with self._pace_lock:
             loop = asyncio.get_running_loop()
             now = loop.time()
             wait = self._next_slot - now
             self._next_slot = max(now, self._next_slot) + MIN_REQUEST_INTERVAL_S
         if wait > 0:
             await asyncio.sleep(wait)
+        # Re-read the shared cooldown AFTER sleeping. Measured without this:
+        # 2 of 8 probes started inside a 1.5s cooldown, and 5 of 6 in the
+        # direct test — every reservation made before the 429 escaped it.
+        extra = self._cooldown_until - asyncio.get_running_loop().time()
+        if extra > 0:
+            await asyncio.sleep(extra)
@@ async def _apply_cooldown(self, seconds: float) -> None:
         async with self._pace_lock:
             loop = asyncio.get_running_loop()
-            self._next_slot = max(self._next_slot, loop.time()) + seconds
+            now = loop.time()
+            self._next_slot = max(self._next_slot, now) + seconds
+            self._cooldown_until = max(self._cooldown_until, now + seconds)
```

Terminating by construction: one extra sleep, no loop, and `_cooldown_until` is
read once after waking rather than spun on.

Verification: `test_cooldown_escapees_on_the_real_path_...` should report
`started during the 1.5s cooldown: 0`, and
`test_cooldown_does_not_reach_requests_that_already_reserved_a_slot` should
report `0 of 6` — both assertions inverted. The second test calls `_pace()`
directly with no `_get`, so it exercises the new deadline in isolation.

### Fix 3 — C1 + M1, cache quality. `app/ctgov.py`

Two independent one-liners, both already proven correct elsewhere in this repo:

```diff
@@ class _BoundedCache
     def get(self, params: dict[str, str]) -> int | None:
@@
         self.hits += 1
+        # LRU, not FIFO: a key read on every request must not be evicted by a
+        # stream of cold ones. Measured: 31.8% miss rate on such a key.
+        self._data[key] = entry          # or OrderedDict.move_to_end(key)
         return value
```

and an in-flight registry in `count()` so N concurrent identical probes cost 1:

```diff
@@ class CTGovClient.__init__
+        self._inflight: dict[str, asyncio.Future[int]] = {}
```

`_inflight` needs its own bound — cap it at `MAX_PROBES_PER_REQUEST` × expected
concurrency, or assert it empties, or it becomes finding M4.

### Fix 4 — C5. `app/entities.py`

Hold `_CACHE_LOCK` across the open, or close the loser:

```diff
     with _CACHE_LOCK:
+        existing = _CACHE.get(cache_key)
+        if existing is not None:
+            connection.close()      # lost the race; do not orphan our handle
+            return existing
         _CACHE[cache_key] = index
```

---

## Reproducing everything in this document

```bash
uv run --no-sync pytest tests/test_memory_scalability.py tests/test_concurrency.py -q -s
# 27 passed

uv run --no-sync pytest -q
# 412 passed, 29 skipped
```

Each test prints the measurement it asserts on. Where a test locks in *current*
behaviour that a future fix would change (C1, C2, C3, C5, M1), the docstring says
so and names this file, so the assertion and the finding move together.
