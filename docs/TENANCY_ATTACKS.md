# Tenancy attack report

Adversarial audit of the isolation claim in `app/tenancy.py`. Reproduce with:

```
uv run --no-sync pytest tests/test_tenant_sidechannel.py -q -s
```

Raw numbers are written to `artifacts/tenancy_attacks.json` by the suite itself,
so this document cannot drift from what was measured.

---

## The headline

`app/tenancy.py` is correct, and it is not connected to anything.

```
$ grep -rn "TenantRegistry\|SharedQuota\|tenants=" --include=*.py app/ evals/
app/ctgov.py:327:            scoped = self._tenants.for_tenant(self._tenant)
app/ctgov.py:390:    def for_tenant(self, tenant: str | None) -> "CTGovClient":
app/tenancy.py:72:class SharedQuota:
app/tenancy.py:203:class TenantRegistry:
```

Nothing constructs a `TenantRegistry`. Nothing constructs a `SharedQuota`.
No call site passes `tenants=`. The one process that serves traffic builds:

```python
# app/main.py:63
async with store, CTGovClient(store=store) as client:
```

`tenants` defaults to `None`, so `app/ctgov.py:326` never takes its branch and
every request in the process reads and writes the single global `self._cache`
and `self._inflight`. `app/main.py` and `app/mcp_server.py` never read a tenant
identifier from anywhere — no header, no argument, no session field. There is
exactly one tenant, and it is everybody.

**The 31,377x side-channel the module documents as solved is live in the
running service.** Every attack below was therefore run twice: once against
`CTGovClient(store=store)` (SHIPPED — what `app/main.py:63` builds) and once
against `CTGovClient(tenants=TenantRegistry())` (ISOLATED — what the module was
written for). The gap between the two columns is the value of the unwired code.

Method: every overlap measurement uses a transport that `await asyncio.sleep`s.
A synchronous `httpx.MockTransport` serialises coroutines and reports races as
already fixed — that mistake already produced one false "no race" result in this
repo (`tests/test_concurrency.py` docstring), and it is not repeated here.

---

## Severity ranking

| # | Finding | Verdict | Severity |
|---|---|---|---|
| 1 | Tenancy module unreachable; one global cache serves all callers | **LEAKED** | **P0** |
| 2 | `/api/health` publishes a global upstream counter → noise-free query oracle | **LEAKED** | **P0** |
| 3 | Shared L2 store defeats per-tenant L1 even when the registry IS wired | **LEAKED** | **P0** |
| 4 | Cross-tenant single-flight coalescing (shipped config) | **LEAKED** | **P0** |
| 5 | `normalise()` U+212A folds to ASCII `k` → two tenants share one cache | **LEAKED** | **P1** |
| 6 | `MAX_TENANT_BURST_SHARE` bounds concurrency, not throughput → 91.9% capture | **LEAKED** | **P1** |
| 7 | `SharedQuota._inflight` unbounded, attacker-keyed, never reaped | **LEAKED** | **P1** |
| 8 | `normalise()` splits (ZWSP, trailing dot, fullwidth, U+0130); no length cap | **LEAKED** | **P2** |
| 9 | Idle tenant's whole cache evictable for 512 invented ids | **LEAKED** (availability only) | **P2** |
| 10 | `TenantRegistry.stats()` yields exact tenant population by saturation probing | **LEAKED** | **P3** |
| 11 | Timing oracle *with the registry wired* | **did not leak** | — |
| 12 | Cross-tenant entry eviction *with the registry wired* | **did not leak** | — |
| 13 | Active tenant eviction (attacker cannot choose the victim) | **did not leak** | — |
| 14 | `stats()` exposing tenant ids or per-tenant counters | **did not leak** | — |
| 15 | Memory plateau under 10,000 minted ids | **did not leak** | — |

---

## ATTACK 1 — Timing oracle

Tenant A queries X. Tenant B then queries X, and separately queries a control
term B invented and nobody has ever asked. If B can tell the two apart, B has
recovered what A asked. 200 trials per configuration, fresh keys each trial.

The metric that matters is **attacker success rate**: the Mann-Whitney statistic
`P(probe < control)`. It is literally the attacker's win rate — shown one warm
and one cold sample with the labels hidden, how often does "the faster one is
the question my rival asked" win? `0.5` is a coin flip. `1.0` is a perfect
oracle.

The 180 ms inter-request pacing was dialled down to 2 ms for the run. That is
**conservative**: pacing only ever lengthens a cold fetch, so the deployed delta
is larger than reported, never smaller.

| | SHIPPED (`app/main.py:63`) | ISOLATED (`tenants=TenantRegistry()`) |
|---|---|---|
| trials | 200 | 200 |
| probe mean (rival asked this) | **0.0103 ms** | 15.4528 ms |
| probe p50 / p95 | 0.0091 / 0.0154 ms | 15.5132 / 16.1930 ms |
| control mean (never asked) | **15.4852 ms** | 15.5003 ms |
| control p50 / p95 | 15.5291 / 16.1714 ms | 15.5588 / 16.1511 ms |
| ratio (mean) | **1,506.8x** | 1.0x |
| ratio (p50) | **1,706.5x** | 1.0x |
| distributions overlap | **False** | True |
| **attacker success rate** | **1.0000** | **0.5230** |

**VERDICT: LEAKED (P0).** In the configuration that actually runs, the
distributions do not overlap at all — the slowest warm probe (0.0154 ms p95) is
three orders of magnitude below the fastest cold control. The attacker
classifies with a perfect 1.0000 success rate over 200 trials. This is not a
statistical channel requiring averaging; a single sample decides it.

The 1,506x here is smaller than the 31,377x in the docstring only because the
test transport is faster than the real API and pacing was reduced. Both numbers
describe the same total separation.

The ISOLATED column is the proof the mitigation works: 0.5230 success rate is a
coin flip, and the distributions overlap. **The code is right. It is just not
running.**

### Minimal fix

```diff
--- a/app/main.py
+++ b/app/main.py
@@
-from app.ctgov import CTGovClient, CTGovError
+from app.ctgov import CTGovClient, CTGovError
+from app.tenancy import TenantRegistry
@@ lifespan
-    async with store, CTGovClient(store=store) as client:
+    async with store, CTGovClient(store=store, tenants=TenantRegistry()) as client:
         app.state.ctgov = client
```

and, at each request entry point, scope the client to the caller:

```diff
 @app.post("/api/query", response_model=QueryResponse)
-async def query(request: QueryRequest) -> QueryResponse:
+async def query(request: QueryRequest, x_tenant_id: str | None = Header(None)) -> QueryResponse:
     plan, used_fallback = await _plan_for(request)
     try:
-        response = await execute(plan, request, app.state.ctgov)
+        response = await execute(plan, request, app.state.ctgov.for_tenant(x_tenant_id))
```

Note the second half is not optional. Wiring the registry without scoping
per request routes every caller to `normalise(None) == "anonymous"`, i.e. one
shared cache again — the leak with more machinery in front of it.

---

## ATTACK 2 — Coalescing oracle

A and B issue the same query concurrently. A starts; 100 ms into A's 200 ms
upstream call, B asks the same question. If single-flight coalesces across
tenants, B's latency collapses to A's *remaining* time — B learns not only that
A asked, but roughly when A started.

| | latency | upstream requests |
|---|---|---|
| B alone, no concurrent A (baseline) | 212.67 ms | 1 |
| **SHIPPED**, B with A mid-flight | **107.27 ms** | **1** |
| ISOLATED, B with A mid-flight | 215.91 ms | 2 |

**VERDICT: LEAKED (P0) in the shipped configuration.** One upstream request
served two tenants. B's latency fell to 50.4% of baseline, and the residual
(107 ms) is a direct readout of how long A had been running. B can binary-search
A's start time by varying its own probe delay.

ISOLATED did not leak: two requests, and B's 215.91 ms is statistically
indistinguishable from the 212.67 ms baseline. The per-tenant `inflight` table
at `app/tenancy.py:167` does exactly what its comment claims.

**Fix:** same as Attack 1. `app/ctgov.py:328` already routes `inflight` to the
scoped cache; it just never executes.

---

## ATTACK 3 — Eviction oracle

### 3a. Can A evict B's cache entries?

| | flood size | B's warm key evicted |
|---|---|---|
| SHIPPED | 2,112 keys | **True** |
| ISOLATED | 2,048 keys | False |

**VERDICT: LEAKED (P0, same root cause) shipped; did not leak isolated.**
One `_BoundedCache` capped at `CACHE_MAX_ENTRIES` is shared, so A's flood pushes
B's entry out and B's next read pays a cold fetch. Isolated, A's 2,048-key flood
lands entirely in A's own 512-entry cache and B's key is untouched — four times
`PER_TENANT_ENTRIES` with zero effect on the victim.

### 3b. Can A evict B's whole *tenant* by minting ids?

| | value |
|---|---|
| `MAX_TENANTS` | 512 |
| ids to evict an **idle** victim | **512** |
| idle victim entries after attack | 0 (was 1) |
| **active** victim survived 2,048 minted ids | **True** |

**VERDICT: LEAKED (P2, availability only).** 512 invented strings destroy an
idle tenant's entire cache. That is cheap and it is a real denial-of-service —
the victim's next queries all miss and burn shared upstream budget.

It is **not** a confidentiality leak, and the docstring's reasoning at
`app/tenancy.py:244` holds under measurement: eviction is LRU, so the attacker
cannot *choose* the victim. An actively-querying tenant withstood 2,048 minted
ids because its own traffic keeps it off the LRU end. The attacker can only
evict tenants that are already idle, and cannot observe whether it hit anyone.

> Method note worth keeping: the first version of this attack reported the
> victim surviving after 2,049 ids. That was wrong — the survival check itself
> called `for_tenant(victim)`, touching the victim and moving it to MRU every
> iteration. The measurement was interfering with the target. Both variants are
> now measured separately.

**Fix (P2):** reserve capacity so that unauthenticated/newly-minted ids cannot
occupy the whole registry — e.g. partition the registry into an authenticated
region and a smaller anonymous region, or require ids to be attested before they
allocate a cache. Not worth doing before Attacks 1–4.

### 3c. Tenant-population enumeration

The attacker mints ids one at a time and watches `stats()["tenants"]` until it
stops growing. The id count at saturation is `MAX_TENANTS`, so the pre-existing
population falls out by subtraction. No id is ever guessed.

| | value |
|---|---|
| hidden tenants actually present | 7 |
| ids minted before saturation | 506 |
| population inferred by attacker | **7 (exact)** |

**VERDICT: LEAKED (P3).** Exact, not approximate. Mitigated in practice only
because `stats()` is currently not exposed on any route — which is luck, not
design, since `app/tenancy.py:259` was written to be an endpoint.

**Fix:** do not expose `TenantRegistry.stats()` publicly; if it must be served,
quantise `tenants` (e.g. round to the nearest 64) or drop the field.

---

## ATTACK 4 — Quota starvation

`MAX_TENANT_BURST_SHARE = 0.5` claims "a hot tenant degrades its own latency
rather than everyone else's." One greedy tenant (24 concurrent workers,
acquire-and-release-immediately) against 3 polite tenants (hold 20 ms), 2.0 s,
`rate=20/s burst=10`.

| tenant | completed | p95 wait |
|---|---|---|
| **greedy** | **57** | — |
| polite-0 | 1 | 1006.55 ms |
| polite-1 | 3 | 983.20 ms |
| polite-2 | 1 | 1006.60 ms |

Greedy share of grants: **91.9%**. Fair share for 4 tenants: 25.0%.
Polite p95 waits sit on the 1.0 s timeout ceiling — those are timeouts, not waits.

**VERDICT: LEAKED (P1).** The claim is false as written. The root cause is a
type error in what is being bounded:

```python
# app/tenancy.py:119-123
mine = self._inflight.get(tenant, 0.0)
fair = mine + cost <= self._max_tenant_tokens
```

`_inflight` is **concurrency**, decremented on `release()`. `MAX_TENANT_BURST_SHARE`
therefore caps how many requests a tenant may have *outstanding at one instant*.
It says nothing about **throughput**. A tenant that acquires and releases
instantly keeps `mine` at 1.0 forever, never trips the cap, and consumes the
token *rate* without limit. The fairness check is measuring the wrong quantity.

Polite tenants are not fully starved (1–3 completions each), which is what keeps
this P1 rather than P0.

### Minimal fix

Bound the rate, not just the concurrency — add a per-tenant deficit counter that
decays, and require both checks:

```diff
--- a/app/tenancy.py
+++ b/app/tenancy.py
@@ class SharedQuota.__init__
         self._inflight: dict[str, float] = {}
+        # Recent consumption, decayed at the bucket's own rate. `_inflight`
+        # bounds concurrency; this bounds THROUGHPUT. Measured without it: one
+        # tenant took 91.9% of grants against a 25% fair share.
+        self._recent: dict[str, tuple[float, float]] = {}
+        self._max_tenant_rate_share = max_tenant_share
@@ in acquire(), inside the lock
         mine = self._inflight.get(tenant, 0.0)
-        fair = mine + cost <= self._max_tenant_tokens
+        used, seen = self._recent.get(tenant, (0.0, self._updated))
+        used = max(0.0, used - (self._updated - seen) * self._rate)
+        self._recent[tenant] = (used, self._updated)
+        fair = (mine + cost <= self._max_tenant_tokens
+                and used + cost <= self._burst * self._max_tenant_rate_share)
         if self._tokens >= cost and fair:
             self._tokens -= cost
             self._inflight[tenant] = mine + cost
+            self._recent[tenant] = (used + cost, self._updated)
```

`_recent` must be reaped on the same schedule as `_inflight` — see Attack 4b.

### 4b. `_inflight` is unbounded and attacker-keyed

| | value |
|---|---|
| tenant ids acquired without release | 10,000 |
| `len(quota._inflight)` | **10,000** |
| retained memory | **1,010.9 KB** |
| `stats()["tenants_inflight"]` | 10,000 |

**VERDICT: LEAKED (P1).** The comment at `app/tenancy.py:94` says "Bounded by
the tenant registry above it." It is not — `SharedQuota` holds no reference to
the registry and enforces no cap of its own. Keys are attacker-chosen strings,
and every `acquire()` whose `release()` is skipped (any exception between the
two, any crash, any cancelled task) leaves a permanent entry. There is no
eviction path and no reaper. Under an agent loop that retries, this grows
monotonically.

### Minimal fix

```diff
--- a/app/tenancy.py
+++ b/app/tenancy.py
@@
+# Independent of the registry: SharedQuota holds no reference to it, so its
+# own tables must carry their own cap. Attacker-chosen keys with no bound is
+# an OOM an agent retry loop reaches in minutes.
+MAX_QUOTA_TENANTS = MAX_TENANTS
@@ in acquire(), on the grant path
                     self._inflight[tenant] = mine + cost
+                    if len(self._inflight) > MAX_QUOTA_TENANTS:
+                        # Drop the smallest in-flight holder: it is the closest
+                        # thing to a leaked entry, and dropping it only ever
+                        # loosens that tenant's own fairness cap.
+                        victim = min(self._inflight, key=self._inflight.get)
+                        if victim != tenant:
+                            self._inflight.pop(victim, None)
```

Better still, make the leak structurally impossible with a context manager so
`release()` cannot be skipped:

```python
@asynccontextmanager
async def lease(self, tenant: str, cost: float = 1.0, *, timeout: float = 30.0):
    await self.acquire(tenant, cost, timeout=timeout)
    try:
        yield
    finally:
        await self.release(tenant, cost)
```

---

## ATTACK 5 — Enumeration

### 5a. `stats()` surfaces

```
TenantRegistry.stats() = {'tenants': 3, 'entries_total': 3,
                          'evicted_tenants': 0,
                          'quota': {'granted': 0, 'throttled': 0,
                                    'tenants_inflight': 0, 'tokens_available': 10.0}}
```

**VERDICT: did not leak.** No tenant id appears in the serialised output, no
per-tenant counter is present, and the key set is exactly
`{tenants, entries_total, evicted_tenants, quota}`. `entries_total` is aggregate
volume, which the module's threat model explicitly accepts. The comments at
`app/tenancy.py:147` and `:260` are honoured by the code. (See 3c for the one
inference this still permits.)

### 5b. `/api/health` is a noise-free query oracle — **P0**

```
GET /api/health -> {'status': 'ok', 'llm_configured': True,
                    'ctgov_requests': 2, 'ctgov_retries': 0}
```

| probe | `ctgov_requests` delta |
|---|---|
| a term the victim already asked | **0** |
| a term nobody has asked | **1** |

**VERDICT: LEAKED (P0).** `app/main.py:102` publishes the process-global upstream
request counter on an unauthenticated GET. An attacker reads it, issues one
query, reads it again: **delta 0 means somebody else already asked that exact
question.**

This is strictly worse than the timing oracle in Attack 1. Timing is statistical
and needs trials; this is deterministic, needs one probe, is immune to network
jitter, and works even from a client too slow to measure a 15 ms difference. It
also survives naive mitigations — adding jitter to response latency does nothing
to a counter.

Note that fixing Attack 1 alone does **not** fix this. With per-tenant caches,
the delta for a warm-for-someone-else term becomes 1 as well, which closes the
content oracle — but the counter still publishes every tenant's aggregate
upstream volume to anonymous callers, and `ctgov_retries` publishes when the
platform is being throttled.

### Minimal fix

```diff
--- a/app/main.py
+++ b/app/main.py
@@ async def health
-    client: CTGovClient = app.state.ctgov
     return {
         "status": "ok",
         "llm_configured": bool(os.environ.get("OPENAI_API_KEY")),
-        "ctgov_requests": client.request_count,
-        "ctgov_retries": client.retry_count,
     }
```

A liveness probe needs to answer "is this process up". It does not need to
answer "what has everyone else been asking". If the counters are wanted for
operations, serve them on an authenticated route or export them to the metrics
sink rather than the public health check.

### 5c. MCP surface

`grep -c tenant app/mcp_server.py` → **0**.

**VERDICT: LEAKED (P0, same root cause as Attack 1).** The MCP tools take no
tenant parameter and construct their own bare `CTGovClient()` at
`app/mcp_server.py:172` and `:331`. Every MCP caller is the same identity and
shares one cache. Any fix to `app/main.py` must be applied here too or the MCP
route remains a bypass.

---

## ATTACK 6 — Normalisation bypass

`normalise()` is `(tenant or "").strip().lower() or "anonymous"`.

### Collisions — two tenants that should differ share one cache — **P1**

| attacker id | victim id | both normalise to |
|---|---|---|
| `'\u212Aacme-pharma'` (U+212A KELVIN SIGN + `acme-pharma`) | `'kacme-pharma'` | `'kacme-pharma'` |
| `'\u212AENALOG'` | `'kenalog'` | `'kenalog'` |

**VERDICT: LEAKED (P1).** U+212A KELVIN SIGN lowercases to ASCII `k` under
`str.lower()`. The attack direction is what makes this serious: **the attacker
picks its own id.** It never has to present the victim's identifier, pass the
victim's authentication, or guess anything — it registers with a leading U+212A and
`str.lower()` walks it into the victim's cache. Full read/write access to the
victim's entries, and Attack 1's oracle at point-blank range.

This is P1 rather than P0 only because it is downstream of Attack 1: today
there are no separate caches to collide.

### Splits — one tenant mints unlimited caches, evading fairness — **P2**

| id | normalises to | why |
|---|---|---|
| `'acme\u200B'` | `'acme\u200B'` | ZERO WIDTH SPACE is not stripped |
| `'acme.'` | `'acme.'` | trailing dot is not stripped |
| `'\uFF41cme'` | `'\uFF41cme'` | FULLWIDTH `a` does not fold to ASCII |
| `'\u0130STANBUL-BIO'` | `'i\u0307stanbul-bio'` | U+0130 folds to `i` + U+0307, not `i` |

Control: `'acme\u2000'` (EN QUAD) -> `'acme'`. Correctly stripped, as intended.

**VERDICT: LEAKED (P2).** The docstring at `app/tenancy.py:229` states the
requirement precisely — "a tenant could evade its own fairness accounting simply
by changing case" — and `.lower()` is not sufficient to enforce it. There are
unlimited zero-width variants of any id, so a tenant can mint unlimited caches
and unlimited quota identities, which is also the ammunition for Attack 3b.

### Empty, None, and length

- `None`, `""`, `"   "`, `"\t"` all → `"anonymous"`, and a caller may **name
  itself** `"anonymous"` and join that bucket deliberately. Every
  unauthenticated caller shares one cache with a guessable name.
- A 1,000,000-character id is accepted verbatim. No length cap. Combined with
  Attack 4b (`_inflight` unbounded), an attacker controls both the number of
  keys and the size of each.

### Minimal fix

```diff
--- a/app/tenancy.py
+++ b/app/tenancy.py
@@
+import re
+
+MAX_TENANT_ID_LENGTH = 128
+_TENANT_ID = re.compile(r"[a-z0-9._@-]{1,%d}" % MAX_TENANT_ID_LENGTH)
@@
     @staticmethod
     def normalise(tenant: str | None) -> str:
-        return (tenant or "").strip().lower() or "anonymous"
+        raw = (tenant or "").strip()
+        # VALIDATE BEFORE FOLDING, and in this order specifically. Any case
+        # conversion can map a non-ASCII character ONTO an existing ASCII id --
+        # U+212A KELVIN SIGN becomes 'k' under both lower() and NFKC -- so a
+        # check applied after folding sees a clean ASCII string and approves a
+        # collision the attacker chose. Rejecting non-ASCII first makes that
+        # class of attack unreachable rather than merely enumerated.
+        if not raw.isascii() or not _TENANT_ID.fullmatch(raw.casefold()):
+            return "anonymous"
+        return raw.casefold()
```

Note the ordering, because the obvious version of this fix does not work.
Normalising first and validating after -- `NFKC`, then `casefold()`, then an
alphabet check -- **still collides**: `unicodedata.normalize("NFKC", "\u212A")`
is ASCII `"K"`, which casefolds to `"k"` and passes the alphabet check cleanly.
The first draft of this remedy did exactly that, and it was caught only by
running it rather than reasoning about it:

```
NFKC(U+212A) = 'K'   ->  normalise('\u212Aacme-pharma') == 'kacme-pharma'   # still collides
```

Validating the raw input first closes it. Verified against the same cases the
attack used:

| input | result | collides with victim |
|---|---|---|
| `'\u212Aacme-pharma'` | `'anonymous'` | **False** |
| `'acme\u200B'` | `'anonymous'` | False |
| `'\uFF41cme'` | `'anonymous'` | False |
| `'\u0130STANBUL-BIO'` | `'anonymous'` | False |
| `'a' * 1_000_000` | `'anonymous'` | -- |
| `'ACME'` (control) | `'acme'` | True, as required |
| `' acme '` (control) | `'acme'` | True, as required |
| `'acme\u2000'` (control) | `'acme'` | True, as required |

`raw.isascii()` also removes the need for `unicodedata` entirely: on an
ASCII-only string `casefold()` and `lower()` agree, so the U+0130 and U+00DF
folding hazards cannot arise at all.

Caveat to state plainly: routing every rejected id to `"anonymous"` means a
malformed id joins the shared anonymous bucket. That is acceptable only because
`"anonymous"` is not a trusted tenant. If tenant ids ever become
authentication-bearing, this must raise instead of returning a default.

---

## ATTACK 7 — Memory

10,000 minted tenant ids, 4 cache entries each, under `tracemalloc`.

| | value |
|---|---|
| ids presented | 10,000 |
| tenants retained | **512** (`MAX_TENANTS`) |
| entries retained | 2,048 |
| tenants evicted | 9,488 |
| retained memory | **666.8 KB** |

Per-tenant entry cap: 5,120 keys written → **512** held (`PER_TENANT_ENTRIES`).

**VERDICT: did not leak.** Memory plateaus. Both bounds hold exactly, the
registry evicts precisely `10,000 - 512` tenants, and 10,000 attacker-chosen ids
cost under a megabyte. `TenantRegistry` satisfies the BOUND requirement.

`SharedQuota` does not — see Attack 4b, where the same 10,000 ids retain 10,000
entries and 1,010.9 KB with no cap and no reaper. The registry was given bounds;
the quota was given a comment claiming it inherits them.

---

## What holds

Stated plainly, because four of these were attacked hard and did not move:

1. **Per-tenant caching closes the timing oracle completely.** 200 trials,
   attacker success rate 0.5230 — a coin flip — with fully overlapping
   distributions. The design in `app/tenancy.py` is correct.
2. **Per-tenant single-flight closes the coalescing oracle.** 2 upstream
   requests, B's latency within 1.5% of its no-contention baseline.
3. **LRU eviction denies the attacker a choice of victim.** An actively-querying
   tenant survived 2,048 minted ids. The reasoning at `app/tenancy.py:244` is
   sound and measured.
4. **`stats()` leaks no tenant id and no per-tenant counter**, and the tenant
   registry's memory plateaus exactly at its documented bounds.

## What to fix, in order

1. **Wire it in.** `app/main.py:63` + per-request `for_tenant(...)`, and the same
   at `app/mcp_server.py:172` and `:331`. Everything in Attacks 1, 2, 3a and 5c
   is one unpassed keyword argument.
2. **Drop `ctgov_requests` / `ctgov_retries` from `/api/health`.** Two deleted
   lines. This one is not fixed by step 1 and is the cheapest P0 here.
3. **Key the L2 store by tenant**, or do not consult it across tenants. Step 1
   without this leaves the channel open at 546x (Attack 1b).
4. **Harden `normalise()`** — reject non-ASCII and out-of-alphabet ids *before*
   case-folding, and cap the length. Order matters: folding first re-opens the
   collision. See Attack 6 for the verification table and the failed first
   attempt.
5. **Bound `SharedQuota` by rate, not only concurrency**, and cap/reap
   `_inflight`.

Steps 1–3 are the same defect wearing three hats: a cache that is shared by a
tenant-unaware caller leaks queries, and it does not matter whether that cache is
L1, the in-flight table, an L2 row, or an integer on a health endpoint.

---

## Appendix — reproducing

```
uv run --no-sync pytest tests/test_tenant_sidechannel.py -q -s
TENANCY_TRIALS=1000 uv run --no-sync pytest tests/test_tenant_sidechannel.py -q -s -k attack1
```

Every test in `tests/test_tenant_sidechannel.py` that documents a leak **asserts
that the leak is present**, with an assertion message naming this file. When
someone lands a fix, those tests fail loudly and the finding gets closed
deliberately rather than silently. A green suite here means "the measurements
still say what this document says", not "the system is safe".
