# Tenant load: the shared quota under multi-tenant traffic

`evals/ramp.py` measured single-flight. This measures the other half of
`app/tenancy.py`: the `SharedQuota` every tenant pulls from, and the per-tenant
caches that deliberately give up cross-tenant hits.

    uv run --no-sync python -m evals.tenant_load     # writes evals/tenant_load_report.json
    uv run --no-sync python -m pytest tests/test_quota_fairness.py

No live API. Upstream is a transport that actually suspends, for the reason
`evals/ramp.py` gives: a synchronous mock serialises the coroutines and reports
every contention bug as already fixed. The quota is driven directly
(acquire → suspend → release), because contention is a property of the bucket,
not of httpx.

**Headline.** The cache isolation works and costs exactly `(T-1)` upstream
requests per shared question. The quota does **not** work as documented: it is
fair below the burst ceiling and starves 90% of tenants above it.

---

## Reading the numbers

`DEFAULT_RATE_PER_SECOND` is 5.0, so a 128-tenant ramp at production rate would
take an hour. The ramps run a bucket 100x faster (`RAMP_RATE = 500`,
`RAMP_BURST = 10`) holding the *shape* constant — same burst/rate ratio, same
`max_tenant_share`. Waits below are in units of that faster bucket. Multiply by
100 to read them at production rate; the ratios and the fairness index are
dimensionless and carry over directly.

One caveat stated up front: `time.process_time()` is process-wide. The CPU
figures in scenario 5 are single-threaded runs and are reported as evidence,
not asserted on in the tests — poll counts are the load-independent metric, and
those are what `tests/test_quota_fairness.py` gates on.

---

## 1. Fairness ramp

T tenants, each asking continuously for a 0.5s window. **Open loop by design**:
a closed loop (T tenants × R requests, everyone stops at R) reports perfect
fairness by construction, because the workload itself is equal. Fairness is only
visible when tenants may take as much as the quota will give them.

| T | completed | min | max | spread | Jain | p50 wait | p95 wait |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 2 | 66 | 33 | 33 | 1.00 | 1.00000 | 0.005 ms | 0.011 ms |
| 4 | 132 | 33 | 33 | 1.00 | 1.00000 | 0.002 ms | 0.007 ms |
| 8 | 256 | 25 | 33 | 1.32 | 0.99321 | 0.001 ms | 0.014 ms |
| 16 | 266 | **1** | 33 | **33.0** | **0.54075** | 0.001 ms | 15.85 ms |
| 32 | 281 | **1** | 33 | **33.0** | **0.30302** | 0.001 ms | 524.96 ms |
| 64 | 313 | **1** | 33 | **33.0** | **0.19209** | 0.002 ms | 586.79 ms |
| 128 | 376 | **1** | 33 | **33.0** | **0.14006** | 0.003 ms | 696.47 ms |

The band is not narrow. It is 33:1, and it breaks at exactly `T > burst`.

Up to 8 tenants the quota is genuinely fair (Jain ≥ 0.993). From 16 tenants on,
some tenants complete 33 requests and others complete **one**, then wait out the
whole window. Total throughput is fine throughout (~500/s, the configured rate)
— the bucket delivers its capacity, it just hands nearly all of it to a
persistent minority.

The p50 is 0.002 ms and the p95 is 587 ms. That gap is the shape of the defect:
most grants go to tenants who never wait, and the mean is meaningless.

### Who starves

| T | tenants with ≤1 grant | share |
|---:|---:|---:|
| 16 | 5 | 31.2% |
| 32 | 21 | 65.6% |
| 64 | 45 | 70.3% |
| 128 | 115 | **89.8%** |

The winners are a stable set of ~10, and they win for the whole window — this is
not rotation, it is lock-in. At T=128, 115 of 128 tenants get one request each
in half a second.

---

## 2. Noisy neighbour, and what the cap actually buys

One tenant offering 10x the volume of nine others, 0.5s window. The loud tenant
offers its 10x as **ten concurrent streams under one tenant id**. That
distinction decides the scenario: `SharedQuota` caps *in-flight* cost, so a loud
tenant asking serially never touches the cap however many requests it makes.

| `max_tenant_share` | cap (tokens) | loud done | polite done | polite p50 | polite p95 | polite p99 | loud share |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1.0 (uncapped) | 10.0 | 252 | 16 | 510.5 ms | 525.0 ms | 525.1 ms | 94.0% |
| 0.5 (`MAX_TENANT_BURST_SHARE`) | 5.0 | 170 | 100 | 0.003 ms | 203.7 ms | 530.2 ms | 63.0% |

**The cap buys a lot, and it is worth keeping.** Polite tenants go from 16
completions to 100 (6.3x), their p95 wait falls from 525 ms to 204 ms (−321 ms,
−61%), and their p50 collapses from 510 ms to 3 µs — uncapped, the median polite
request waits half a second; capped, the median polite request does not wait at
all.

**But it does not deliver fairness.** Nine polite tenants and one loud one
sharing fairly would give the loud tenant 10%. It takes 63% even capped. And the
polite p99 is *unchanged* (525 ms → 530 ms): the cap raises the floor for most
polite requests without removing the tail, because the tail is the scenario-1
starvation, which the cap cannot address.

Across 24 trials (12 idle, 12 with four cores busy) `capped < uncapped` held
24/24, with the uncapped share ranging 0.690–0.947 and the capped 0.507–0.636.
The relative effect is solid; the absolute numbers move with machine load.

---

## 3. Cache-hit economics: the price of the privacy property

8 tenants, 12 questions each, varying how many questions are drawn from a shared
pool. Per-tenant caching vs a single shared cache, same workload, same process.

| overlap | distinct questions | per-tenant cache | shared cache | privacy cost | as % |
|---:|---:|---:|---:|---:|---:|
| 0% | 96 | 96 | 96 | **0** | 0.0% |
| 25% | 75 | 96 | 75 | **+21** | +28.0% |
| 50% | 54 | 96 | 54 | **+42** | +77.8% |
| 100% | 12 | 96 | 12 | **+84** | **+700%** |

**Stated in requests, as required:**

> Per-tenant caching costs **`(T-1)` extra upstream requests for every question
> that `T` tenants both ask**. Nothing at all when tenants ask disjoint
> questions; `(T-1)x` when they ask the same ones.

At the measured scale: 8 tenants asking 12 identical questions cost **96
upstream requests instead of 12** — 84 extra, an 8x multiplier.

Scaled to this system's actual constants, the number gets serious. `MAX_TENANTS`
is 512, and the module docstring records that one network query costs 40–57
upstream requests. If all 512 tenants ask the same popular question:

| | upstream requests for one shared query |
|---|---:|
| shared cache | 40 – 57 |
| per-tenant cache | **20,480 – 29,184** |

That is the bill for not telling one tenant what another asked. It is a real
cost and it is the correct trade here — the leak it closes is a 31,377x timing
oracle over competitors' research questions — but it should be budgeted, not
assumed away. The mitigation that survives the threat model is a longer TTL and
a shared L2 keyed so that occupancy is not observable, not re-sharing L1.

Two related properties hold and are pinned in the tests:

* 8 tenants asking the same question **concurrently** cost 8 requests, not 1.
  Cross-tenant coalescing would make B's latency depend on A's timing.
* 16 identical concurrent queries from **one** tenant still coalesce to 1. The
  isolation does not cost a tenant its own single-flight.

---

## 4. Tenant churn

Minting tenant ids continuously past `MAX_TENANTS` (512), with one long-lived
tenant staying active throughout. `tracemalloc`, single process.

| minted | retained | evicted | active tenant survived | heap growth | bytes/tenant |
|---:|---:|---:|:--:|---:|---:|
| 512 | 512 | 1 | yes | 226.0 KiB | 452 |
| 2,048 | 512 | 1,537 | yes | 246.9 KiB | 494 |
| 8,192 | 512 | 7,681 | yes | 246.9 KiB | 494 |
| 32,768 | 512 | 32,257 | yes | 247.4 KiB | 495 |

Clean. Retention pins at exactly `MAX_TENANTS`, memory is flat at ~247 KiB from
2,048 ids onward (64x more ids, +0.2%), and the long-lived active tenant
survives 32,768 evictions. That last row is the one that matters: eviction is
LRU, not random, so an attacker who can mint ids cannot choose which rival to
evict — which would be a write-side channel into that rival's latency.

---

## 5. Saturation point

Offered load fired at once against burst=10 @ 500/s, each caller with a 0.5s
timeout.

| offered | granted | `QuotaExhausted` | % | wall | cpu | cpu/wall | polls | polls/req | polls/s | hung |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:--:|
| 8 | 8 | 0 | 0.0 | 0.008s | 0.000 | 0.00 | 8 | 1.00 | 995 | no |
| 16 | 16 | 0 | 0.0 | 0.032s | 0.000 | 0.00 | 22 | 1.38 | 698 | no |
| 32 | 32 | 0 | 0.0 | 0.063s | 0.000 | 0.00 | 75 | 2.34 | 1,195 | no |
| 64 | 64 | 0 | 0.0 | 0.139s | 0.000 | 0.00 | 284 | 4.44 | 2,042 | no |
| 128 | 128 | 0 | 0.0 | 0.261s | 0.016 | 0.06 | 1,100 | 8.59 | 4,221 | no |
| 256 | 256 | 0 | 0.0 | 0.510s | 0.000 | 0.00 | 4,314 | 16.85 | 8,452 | no |
| **512** | 265 | **247** | **48.2** | 0.528s | 0.000 | 0.00 | 12,813 | 25.03 | 24,283 | no |
| 1,024 | 264 | 760 | 74.2 | 0.522s | 0.422 | 0.81 | 50,150 | 48.97 | 96,126 | no |
| 2,048 | 262 | 1,786 | 87.2 | 0.526s | 0.516 | **0.98** | 78,391 | 38.28 | 148,986 | no |
| 4,096 | 273 | 3,823 | 93.3 | 0.542s | 0.531 | **0.98** | 83,197 | 20.31 | 153,538 | no |

**Exhaustion starts between 256 and 512 offered** — i.e. at
`rate x timeout + burst` = `500 x 0.5 + 10` = 260 grants. Grants plateau at
262–275 no matter how much is offered (512 → 265, 4,096 → 273). That is textbook
graceful degradation: the excess is refused, not absorbed.

**It degrades by raising, not by hanging.** Wall time stays at 0.51–0.54s for
every level from 256 to 4,096 — bounded by the caller's own timeout, not by the
size of the herd. No level hung.

**It is not a spin loop, but CPU is O(waiters).** Polls per request stay in the
range 1–49, so each waiter genuinely sleeps. But the backoff has a 5 ms floor
(`max(min(delay, 0.25), 0.005)`) while tokens arrive every 2 ms, so every
blocked waiter wakes ~200 times a second and takes the single global lock. At
2,048 concurrent waiters that is ~150,000 lock acquisitions per second and
**98% of a core spent almost entirely on waiters that get refused anyway**.

Not a P0 — it sleeps and it terminates. But it means a single event loop
saturates a core at roughly 2,000 concurrent waiters, which is a capacity limit
worth knowing, and it is a consequence of the same missing queue as scenario 1:
a waiter that had a queue position would not need to poll at all.

---

## 6. Latency under load, and the knee

| concurrency | requests | p50 | p95 | p99 | seconds | throughput |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 4 | 0.010 ms | 0.037 ms | 0.037 ms | 0.060 | 66.7/s |
| 2 | 8 | 0.007 ms | 0.013 ms | 0.013 ms | 0.061 | 132.1/s |
| 4 | 16 | 0.003 ms | 0.012 ms | 0.018 ms | 0.063 | 252.8/s |
| 8 | 32 | 0.002 ms | 0.008 ms | 0.011 ms | 0.061 | 527.2/s |
| **16** | 64 | 0.003 ms | **62.14 ms** | 62.14 ms | 0.156 | 411.5/s |
| 32 | 128 | 0.003 ms | 172.09 ms | 189.99 ms | 0.266 | 480.3/s |
| 64 | 256 | 0.002 ms | 371.30 ms | 432.86 ms | 0.559 | 458.1/s |
| 128 | 512 | 0.002 ms | 777.69 ms | 947.55 ms | 1.057 | 484.3/s |
| 256 | 1,024 | 0.002 ms | 1,607.79 ms | 1,919.21 ms | 2.077 | 492.9/s |

### The knee is at concurrency 16 — that is, at `burst`

Between concurrency 8 and 16, p95 goes from 0.008 ms to 62.14 ms: a **7,750x
jump** for a 2x increase in load. Throughput does not improve across that
boundary (527/s → 412/s, it gets *worse*), so nothing is bought for the latency.

The knee sits exactly where offered concurrency exceeds `burst = 10`. Below it,
every caller is served from the bucket without entering the wait loop. Above it,
callers enter the poll loop and p95 grows linearly with concurrency
(≈6.1 ms per unit) while throughput stays flat at the configured rate — the
signature of a saturated server, and the point past which adding load only adds
queue.

**Operationally: keep concurrent in-flight requests per instance at or below
`burst`.** With the production default of 10, that is 10. p50 stays flat at
2 µs at every level, so p50 is not a usable health signal here — alert on p95.

---

## 7. The defect, and what fixes it

Same open-loop workload, comparing the shipped quota against two candidate
fixes, both implemented as subclasses in `evals/tenant_load.py` overriding only
`acquire`. **`app/` is unmodified.**

| T | variant | total | min | max | spread | Jain | starved | starved % |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 16 | current | 264 | 1 | 33 | 33.0 | 0.54328 | 5 | 31.2% |
| 16 | jitter | 264 | 2 | 33 | 16.5 | 0.55975 | 0 | 0.0% |
| 16 | **fifo** | 192 | 12 | 12 | **1.00** | **1.00000** | 0 | 0.0% |
| 32 | current | 282 | 1 | 33 | 33.0 | 0.31025 | 21 | 65.6% |
| 32 | jitter | 281 | 1 | 33 | 33.0 | 0.31704 | 13 | 40.6% |
| 32 | **fifo** | 241 | 7 | 8 | **1.14** | **0.99563** | 0 | 0.0% |
| 64 | current | 312 | 1 | 26 | 26.0 | 0.30530 | 45 | 70.3% |
| 64 | jitter | 313 | 1 | 33 | 33.0 | 0.19653 | 43 | 67.2% |
| 64 | **fifo** | 297 | 4 | 5 | **1.25** | **0.98942** | 0 | 0.0% |
| 128 | current | 377 | 1 | 33 | 33.0 | 0.13948 | 115 | 89.8% |
| 128 | jitter | 376 | 1 | 33 | 33.0 | 0.14995 | 101 | 78.9% |
| 128 | **fifo** | 367 | 2 | 3 | **1.50** | **0.98618** | 0 | 0.0% |

**Jitter — the obvious one-line fix — does not work.** Jain at 128 tenants goes
0.139 → 0.150. It is kept in the ramp precisely because it fails: that is the
difference between a diagnosis and a guess. The cause is not randomness in wake
order.

**A FIFO ticket works**, and costs ~3% throughput at 128 tenants (377 → 367).

### Root cause

`SharedQuota.acquire` is a retry-with-backoff poll loop. On failure it sleeps
and re-contends **from scratch**. Nothing anywhere records that a tenant has
been waiting, so when tokens appear the grant goes to whichever coroutine the
event loop happens to run next. A tenant that has waited 500 ms has no advantage
over one that arrived this instant, and a tenant already cycling
acquire → release re-arms its timer phase-locked to the refill while the
starved ones wake to an empty bucket. That the winner set is stable rather than
rotating is what makes it starvation rather than noise.

The deeper reason the existing mechanism cannot help:

> **`max_tenant_share` bounds concurrency, not throughput share.** It limits
> in-flight cost, and `release` resets that. A tenant that acquires and
> releases immediately never exceeds its ceiling no matter how much of the
> bucket it consumes over time. The docstring's claim that `acquire` is "fair
> across tenants" is true for *simultaneous holdings* and false for
> *throughput* — and throughput is what a tenant experiences.

That is also why the fix is not a tuning change. There is no value of
`max_tenant_share` that fixes it: scenario 2 shows the cap working correctly and
the loud tenant still taking 63%.

### Minimal diff (NOT applied — `app/**` is out of scope for this task)

```diff
--- a/app/tenancy.py
+++ b/app/tenancy.py
@@
-from collections import OrderedDict
+from collections import OrderedDict, deque
@@ class SharedQuota.__init__
         self._inflight: dict[str, float] = {}
+        # Arrival order. The bucket must remember who has been waiting;
+        # _inflight cannot, because release() resets it.
+        self._waiting: deque[object] = deque()
@@ class SharedQuota.acquire
         deadline = time.monotonic() + timeout
         waited_from = time.monotonic()
-        while True:
-            async with self._lock:
-                self._refill()
-                mine = self._inflight.get(tenant, 0.0)
-                fair = mine + cost <= self._max_tenant_tokens
-                if self._tokens >= cost and fair:
-                    self._tokens -= cost
-                    self._inflight[tenant] = mine + cost
-                    self.granted += 1
-                    self.waited_seconds += time.monotonic() - waited_from
-                    return
-                deficit = max(cost - self._tokens, 0.0)
-                delay = (deficit / self._rate) if self._rate > 0 else timeout
-                delay = max(min(delay, 0.25), 0.005)
-            if time.monotonic() + delay > deadline:
-                self.throttled += 1
-                raise QuotaExhausted(
-                    f"tenant {tenant!r} waited over {timeout}s for upstream capacity"
-                )
-            await asyncio.sleep(delay)
+        ticket: object | None = object()
+        self._waiting.append(ticket)
+        try:
+            while True:
+                async with self._lock:
+                    self._refill()
+                    mine = self._inflight.get(tenant, 0.0)
+                    fair = mine + cost <= self._max_tenant_tokens
+                    if self._waiting[0] is ticket and self._tokens >= cost and fair:
+                        self._tokens -= cost
+                        self._inflight[tenant] = mine + cost
+                        self.granted += 1
+                        self.waited_seconds += time.monotonic() - waited_from
+                        self._waiting.popleft()
+                        ticket = None
+                        return
+                    deficit = max(cost - self._tokens, 0.0)
+                    delay = (deficit / self._rate) if self._rate > 0 else timeout
+                    delay = max(min(delay, 0.25), 0.005)
+                if time.monotonic() + delay > deadline:
+                    self.throttled += 1
+                    raise QuotaExhausted(
+                        f"tenant {tenant!r} waited over {timeout}s for upstream capacity"
+                    )
+                await asyncio.sleep(delay)
+        finally:
+            if ticket is not None:
+                try:
+                    self._waiting.remove(ticket)
+                except ValueError:
+                    pass
```

Two honest caveats on this diff:

1. **Head-of-line blocking.** Strict FIFO makes everyone wait when the front
   ticket's tenant is over its own in-flight cap. Measured cost: −27% throughput
   at T=16 (264 → 192), −3% at T=128 (377 → 367). A production version should
   skip tickets blocked *only* by `max_tenant_share` rather than blocking the
   queue behind them. That is no longer a minimal diff, which is why it is not
   proposed as one.
2. **It does not remove the polling.** CPU is still O(waiters) with a 5 ms
   floor (scenario 5). The complete fix replaces the poll loop with an
   `asyncio.Condition` the releaser notifies, which addresses fairness and the
   150k-lock/s CPU cost together. FIFO is the smaller, verifiable step.

---

## What the tests pin

`tests/test_quota_fairness.py` — 14 passing, 2 strict xfail.

Holding today, asserted:

* graceful exhaustion: 1,024 offered raises `QuotaExhausted`, bounded by the
  caller's timeout, never hangs
* a refusal is always an exception, never a silent return that an agent could
  read as capacity
* the wait loop sleeps: < 50 polls per request (measured 3.7–4.5, stable idle
  and under load)
* `max_tenant_share` bounds in-flight cost, and a concurrent noisy neighbour's
  share falls when it is applied
* registry bounded at `MAX_TENANTS`; an active tenant survives 8,192 evictions
  (LRU, not random)
* case/whitespace variants cannot mint separate caches (fairness evasion)
* `stats()` never leaks tenant ids
* the privacy price is exactly `(T-1) x questions` upstream requests
* no cross-tenant coalescing; single-flight preserved within a tenant
* fairness **does** hold below the burst ceiling (Jain ≥ 0.90 at T=8)

Documented but broken, as `@pytest.mark.xfail(strict=True)`:

* `test_fairness_index_stays_within_a_narrow_band` — Jain ≥ 0.80 at 128 tenants
* `test_no_tenant_is_starved_under_contention` — no tenant limited to ≤1 grant

Strict xfail is the deliberate choice: the intended contract is written down and
executable, the suite still tells the truth about what the code does today, and
whoever fixes the quota is forced back here to promote them — because a strict
xfail that starts passing *fails*. Alongside them,
`test_starvation_stays_no_worse_than_measured` guards the other direction, so
the defect cannot quietly get worse either.

Thresholds were set from measured variance (12 idle and 12 loaded trials), not
guessed: the noisy-neighbour test asserts the relative comparison
(`capped < uncapped`, 24/24) with only loose absolute bounds, because the
absolute shares move with machine load while the ordering does not.
