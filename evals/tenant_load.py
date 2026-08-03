"""Tenant load ramp: measure the shared quota under multi-tenant traffic.

`evals/ramp.py` measured single-flight. This measures the other half of
`app/tenancy.py` -- the SharedQuota that every tenant pulls from, and the
per-tenant caches that deliberately give up cross-tenant hits.

No live API. As in `evals/ramp.py`, upstream is a transport that actually
suspends, because a synchronous mock serialises the coroutines and reports
every contention bug as already fixed. The quota itself is exercised directly
(acquire/work/release) since contention is a property of the bucket, not of
httpx.

    uv run --no-sync python -m evals.tenant_load

TIME SCALING. `DEFAULT_RATE_PER_SECOND` is 5.0. A 128-tenant ramp at 5 req/s
would take an hour, so the ramps run a proportionally faster bucket
(RAMP_RATE / RAMP_BURST) and hold the *shape* constant: burst/rate ratio and
`max_tenant_share` are unchanged, so the fairness and saturation behaviour is
the same behaviour, observed on a shorter clock. Wall-clock waits below are
therefore in units of this faster bucket; multiply by RAMP_RATE/5.0 to read
them at production rate.
"""

from __future__ import annotations

import asyncio
import json
import random
import statistics
import time
import tracemalloc
from collections import deque
from pathlib import Path
from typing import Any

import httpx

from app.ctgov import CTGovClient, Filter
from app.tenancy import (
    MAX_TENANTS,
    QuotaExhausted,
    SharedQuota,
    TenantRegistry,
)

REPORT = Path(__file__).resolve().parent / "tenant_load_report.json"

# Same shape as the defaults (burst = 2x rate-second, share = 0.5), 100x faster.
RAMP_RATE = 500.0
RAMP_BURST = 10.0

# How long a granted slot is "held" upstream. Suspends, so tenants interleave.
WORK_SECONDS = 0.002

# Fixed observation window for the open-loop fairness ramp. A closed loop
# (N tenants x R requests, everyone stops at R) reports perfect fairness by
# construction, because the workload itself is equal. Fairness is only visible
# when tenants are allowed to take as much as the quota will give them.
WINDOW_SECONDS = 0.5


class _CountingQuota(SharedQuota):
    """SharedQuota that counts trips through the acquire wait loop.

    `_refill` is called exactly once per iteration, under the lock, so counting
    it counts polls. Nothing in app/ changes; this is a read of the existing
    control flow, not a modification of it.
    """

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        self.polls = 0

    def _refill(self) -> None:
        self.polls += 1
        super()._refill()


class _SlowTransport(httpx.AsyncBaseTransport):
    """Suspends, so concurrent callers genuinely interleave."""

    def __init__(self, latency: float = 0.005) -> None:
        self.calls: list[str] = []
        self._latency = latency

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        await asyncio.sleep(self._latency)
        return httpx.Response(200, json={"totalCount": 4242, "studies": []})


# --------------------------------------------------------------------------
# statistics helpers
# --------------------------------------------------------------------------

def _pct(values: list[float], q: float) -> float:
    """Nearest-rank percentile. Empty -> 0.0, so a starved arm reads as 0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def _jain(counts: list[float]) -> float:
    """Jain's fairness index. 1.0 = perfectly equal, 1/n = one tenant took all."""
    if not counts or sum(counts) == 0:
        return 0.0
    n = len(counts)
    return (sum(counts) ** 2) / (n * sum(c * c for c in counts))


async def _slot(quota: SharedQuota, tenant: str, *, timeout: float,
                work: float = WORK_SECONDS) -> tuple[bool, float]:
    """One acquire -> suspend -> release cycle. Returns (ok, wait_seconds)."""
    started = time.perf_counter()
    try:
        await quota.acquire(tenant, timeout=timeout)
    except QuotaExhausted:
        return False, time.perf_counter() - started
    waited = time.perf_counter() - started
    try:
        await asyncio.sleep(work)
    finally:
        await quota.release(tenant)
    return True, waited


# --------------------------------------------------------------------------
# 1. fairness ramp
# --------------------------------------------------------------------------

async def fairness_ramp(tenants: int, window: float = WINDOW_SECONDS) -> dict[str, Any]:
    """Open loop: every tenant asks continuously for `window` seconds.

    Nobody stops at a quota of their own, so the split of completions is the
    split the *bucket* chose, not the split the workload imposed.
    """
    quota = _CountingQuota(RAMP_RATE, RAMP_BURST)
    waits: dict[str, list[float]] = {f"t{i}": [] for i in range(tenants)}
    done: dict[str, int] = {t: 0 for t in waits}
    deadline = time.perf_counter() + window

    async def worker(name: str) -> None:
        while time.perf_counter() < deadline:
            ok, waited = await _slot(quota, name, timeout=30.0)
            if ok:
                done[name] += 1
                waits[name].append(waited)

    started = time.perf_counter()
    await asyncio.gather(*(worker(t) for t in waits))
    elapsed = time.perf_counter() - started

    completions = [float(done[t]) for t in waits]
    flat = [w for ws in waits.values() for w in ws]
    return {
        "tenants": tenants,
        "window_s": window,
        "completed": int(sum(completions)),
        "polls": quota.polls,
        "polls_per_grant": round(quota.polls / max(sum(completions), 1), 2),
        "min_completions": int(min(completions)),
        "max_completions": int(max(completions)),
        "spread_ratio": round(max(completions) / max(min(completions), 1), 3),
        "jain": round(_jain(completions), 5),
        "p50_wait_ms": round(_pct(flat, 0.50) * 1000, 3),
        "p95_wait_ms": round(_pct(flat, 0.95) * 1000, 3),
        "p99_wait_ms": round(_pct(flat, 0.99) * 1000, 3),
        "seconds": round(elapsed, 3),
        "throttled": quota.throttled,
    }


# --------------------------------------------------------------------------
# 2. noisy neighbour
# --------------------------------------------------------------------------

async def noisy_neighbour(max_tenant_share: float, *, polite: int = 9,
                          loud_multiple: int = 10,
                          window: float = WINDOW_SECONDS) -> dict[str, Any]:
    """One tenant offers 10x the volume of nine others, for a fixed window.

    The loud tenant offers its 10x as ten CONCURRENT streams under one tenant
    id. That distinction decides the whole scenario: `SharedQuota` caps
    *in-flight* cost per tenant, so a loud tenant that asks serially never
    touches the cap no matter how many requests it makes in total. Only
    concurrency inside a single tenant id can reach it.
    """
    quota = SharedQuota(RAMP_RATE, RAMP_BURST, max_tenant_share=max_tenant_share)
    polite_waits: list[float] = []
    loud_waits: list[float] = []
    deadline = time.perf_counter() + window

    async def run(name: str, sink: list[float], counter: list[int]) -> None:
        while time.perf_counter() < deadline:
            ok, waited = await _slot(quota, name, timeout=30.0)
            if ok:
                counter[0] += 1
                sink.append(waited)

    loud_count = [0]
    polite_count = [0]
    started = time.perf_counter()
    await asyncio.gather(
        # 10 concurrent streams, ONE tenant id -- this is what the cap governs.
        *(run("loud", loud_waits, loud_count) for _ in range(loud_multiple)),
        *(run(f"polite{i}", polite_waits, polite_count) for i in range(polite)),
    )
    elapsed = time.perf_counter() - started
    loud_done = loud_count[0]
    polite_done = polite_count[0]

    return {
        "max_tenant_share": max_tenant_share,
        "max_tenant_tokens": quota._max_tenant_tokens,
        "loud_streams": loud_multiple,
        "loud_completed": loud_done,
        "polite_completed": polite_done,
        "polite_p50_wait_ms": round(_pct(polite_waits, 0.50) * 1000, 3),
        "polite_p95_wait_ms": round(_pct(polite_waits, 0.95) * 1000, 3),
        "polite_p99_wait_ms": round(_pct(polite_waits, 0.99) * 1000, 3),
        "loud_p95_wait_ms": round(_pct(loud_waits, 0.95) * 1000, 3),
        "loud_share_of_grants": round(loud_done / max(loud_done + polite_done, 1), 4),
        # Ten tenants sharing fairly would each take 10%. The loud one offers
        # 10/19ths of the load, so 52.6% is "it got exactly what it asked for".
        "fair_share_would_be": round(1 / (polite + 1), 4),
        "seconds": round(elapsed, 3),
    }


# --------------------------------------------------------------------------
# 3. cache-hit economics -- the price of the privacy property
# --------------------------------------------------------------------------

async def cache_economics(tenants: int, questions: int, overlap: float) -> dict[str, Any]:
    """N tenants ask `questions` each; `overlap` of them are drawn from a
    shared pool. Compares upstream requests for per-tenant vs shared cache.

    overlap=0.0 -> every tenant asks its own questions (privacy costs nothing)
    overlap=1.0 -> every tenant asks the same questions (privacy costs the most)
    """
    # No max(1, ...) floor here: at overlap 0.0 the shared pool must genuinely
    # be empty, or the 0% row reports a privacy cost that the workload, not the
    # design, created.
    shared_pool = int(round(questions * overlap))

    def terms(tenant: int) -> list[str]:
        out = []
        for q in range(questions):
            if q < shared_pool:
                out.append(f"shared{q}")           # every tenant asks this
            else:
                out.append(f"t{tenant}-own{q}")    # unique to this tenant
        return out

    async def measure(isolated: bool) -> tuple[int, int]:
        transport = _SlowTransport()
        registry = TenantRegistry() if isolated else None
        base = CTGovClient(client=httpx.AsyncClient(transport=transport),
                           tenants=registry)
        tasks = []
        for t in range(tenants):
            client = base.for_tenant(f"tenant{t}") if isolated else base
            for term in terms(t):
                tasks.append(client.count(Filter({"query.term": term})))
        results = await asyncio.gather(*tasks)
        assert set(results) == {4242}, "correctness must not depend on the cache"
        return len(transport.calls), len(tasks)

    isolated_reqs, offered = await measure(True)
    shared_reqs, _ = await measure(False)
    distinct = shared_pool + (questions - shared_pool) * tenants

    return {
        "tenants": tenants,
        "questions_per_tenant": questions,
        "overlap": overlap,
        "offered_queries": offered,
        "distinct_questions": distinct,
        "per_tenant_cache_upstream": isolated_reqs,
        "shared_cache_upstream": shared_reqs,
        "privacy_cost_requests": isolated_reqs - shared_reqs,
        "privacy_cost_pct": round(
            (isolated_reqs - shared_reqs) / max(shared_reqs, 1) * 100, 1),
    }


# --------------------------------------------------------------------------
# 4. tenant churn
# --------------------------------------------------------------------------

def tenant_churn(mint: int) -> dict[str, Any]:
    registry = TenantRegistry()
    hot = "long-lived-active"
    registry.for_tenant(hot)

    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    for i in range(mint):
        registry.for_tenant(f"churn{i}")
        if i % 4 == 0:
            # The long-lived tenant keeps working throughout. Under LRU it must
            # survive; under random or FIFO eviction it would not.
            registry.for_tenant(hot)
    peak_current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    stats = registry.stats()
    return {
        "minted": mint,
        "max_tenants": MAX_TENANTS,
        "retained": stats["tenants"],
        "evicted": stats["evicted_tenants"],
        "hot_tenant_survived": hot in registry._caches,
        "kib_growth": round((peak_current - base) / 1024, 1),
        "kib_peak": round((peak - base) / 1024, 1),
        "bytes_per_retained_tenant": round((peak_current - base) / max(stats["tenants"], 1)),
    }


# --------------------------------------------------------------------------
# 5. saturation point + CPU
# --------------------------------------------------------------------------

async def saturation(offered: int, *, timeout: float = 0.5) -> dict[str, Any]:
    """`offered` tenants each fire one request at once against the same bucket.

    Measures where QuotaExhausted starts, and how much CPU is burned while
    everyone is blocked -- a spin loop shows up as cpu_ratio near 1.0.
    """
    quota = _CountingQuota(RAMP_RATE, RAMP_BURST)
    cpu0 = time.process_time()
    started = time.perf_counter()
    outcomes = await asyncio.gather(
        *(_slot(quota, f"t{i}", timeout=timeout) for i in range(offered))
    )
    wall = time.perf_counter() - started
    cpu = time.process_time() - cpu0

    ok = [w for good, w in outcomes if good]
    failed = sum(1 for good, _ in outcomes if not good)
    return {
        "offered": offered,
        "granted": len(ok),
        "quota_exhausted": failed,
        "exhausted_pct": round(failed / offered * 100, 1),
        "p50_wait_ms": round(_pct(ok, 0.50) * 1000, 3),
        "p95_wait_ms": round(_pct(ok, 0.95) * 1000, 3),
        "wall_s": round(wall, 3),
        "cpu_s": round(cpu, 3),
        "cpu_ratio": round(cpu / max(wall, 1e-9), 3),
        # Trips through the acquire wait loop. Bounded work per grant means a
        # real queue; growth with `offered` means polling.
        "polls": quota.polls,
        "polls_per_request": round(quota.polls / offered, 2),
        "polls_per_second": round(quota.polls / max(wall, 1e-9)),
        "hung": wall > timeout * 4,
    }


# --------------------------------------------------------------------------
# 6. latency under load
# --------------------------------------------------------------------------

async def latency_at(concurrency: int, per_tenant: int = 4) -> dict[str, Any]:
    quota = SharedQuota(RAMP_RATE, RAMP_BURST)
    waits: list[float] = []

    async def worker(i: int) -> None:
        for _ in range(per_tenant):
            ok, waited = await _slot(quota, f"t{i}", timeout=30.0)
            if ok:
                waits.append(waited)

    started = time.perf_counter()
    await asyncio.gather(*(worker(i) for i in range(concurrency)))
    elapsed = time.perf_counter() - started
    total = concurrency * per_tenant
    return {
        "concurrency": concurrency,
        "requests": total,
        "p50_ms": round(_pct(waits, 0.50) * 1000, 3),
        "p95_ms": round(_pct(waits, 0.95) * 1000, 3),
        "p99_ms": round(_pct(waits, 0.99) * 1000, 3),
        "mean_ms": round(statistics.fmean(waits) * 1000, 3) if waits else 0.0,
        "seconds": round(elapsed, 3),
        "throughput_rps": round(total / max(elapsed, 1e-9), 1),
    }


# --------------------------------------------------------------------------
# 7. starvation detail + candidate fixes
# --------------------------------------------------------------------------

class _Jittered(SharedQuota):
    """Candidate fix A: jitter the backoff, so waiters do not stay phase-locked.

    Included because it is the obvious one-line fix and the measurement says it
    does not work. Keeping it in the ramp is the point -- it is the difference
    between a diagnosis and a guess.
    """

    async def acquire(self, tenant: str, cost: float = 1.0, *,
                      timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        waited_from = time.monotonic()
        while True:
            async with self._lock:
                self._refill()
                mine = self._inflight.get(tenant, 0.0)
                fair = mine + cost <= self._max_tenant_tokens
                if self._tokens >= cost and fair:
                    self._tokens -= cost
                    self._inflight[tenant] = mine + cost
                    self.granted += 1
                    self.waited_seconds += time.monotonic() - waited_from
                    return
                deficit = max(cost - self._tokens, 0.0)
                delay = (deficit / self._rate) if self._rate > 0 else timeout
                delay = max(min(delay, 0.25), 0.005)
                delay *= random.uniform(0.5, 1.5)   # the only change
            if time.monotonic() + delay > deadline:
                self.throttled += 1
                raise QuotaExhausted(f"tenant {tenant!r} timed out")
            await asyncio.sleep(delay)


class _Fifo(SharedQuota):
    """Candidate fix B: a ticket queue, so waiters are served in arrival order.

    This is the shape of the minimal real fix: the bucket needs to remember who
    has been waiting. `_inflight` cannot -- it is reset by every release.
    """

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        self._queue: deque[object] = deque()

    async def acquire(self, tenant: str, cost: float = 1.0, *,
                      timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        waited_from = time.monotonic()
        ticket: object | None = object()
        self._queue.append(ticket)
        try:
            while True:
                async with self._lock:
                    self._refill()
                    mine = self._inflight.get(tenant, 0.0)
                    fair = mine + cost <= self._max_tenant_tokens
                    if (self._queue[0] is ticket and self._tokens >= cost and fair):
                        self._tokens -= cost
                        self._inflight[tenant] = mine + cost
                        self.granted += 1
                        self.waited_seconds += time.monotonic() - waited_from
                        self._queue.popleft()
                        ticket = None
                        return
                    deficit = max(cost - self._tokens, 0.0)
                    delay = (deficit / self._rate) if self._rate > 0 else timeout
                    delay = max(min(delay, 0.25), 0.005)
                if time.monotonic() + delay > deadline:
                    self.throttled += 1
                    raise QuotaExhausted(f"tenant {tenant!r} timed out")
                await asyncio.sleep(delay)
        finally:
            if ticket is not None:
                try:
                    self._queue.remove(ticket)
                except ValueError:
                    pass


async def starvation(cls: type[SharedQuota], tenants: int,
                     window: float = WINDOW_SECONDS) -> dict[str, Any]:
    quota = cls(RAMP_RATE, RAMP_BURST)
    done: dict[str, int] = {f"t{i}": 0 for i in range(tenants)}
    deadline = time.perf_counter() + window
    cpu0 = time.process_time()

    async def worker(name: str) -> None:
        while time.perf_counter() < deadline:
            ok, _ = await _slot(quota, name, timeout=30.0)
            if ok:
                done[name] += 1

    started = time.perf_counter()
    await asyncio.gather(*(worker(t) for t in done))
    wall = time.perf_counter() - started
    cpu = time.process_time() - cpu0

    counts = list(done.values())
    return {
        "variant": cls.__name__.lstrip("_").lower(),
        "tenants": tenants,
        "total": sum(counts),
        "min": min(counts),
        "max": max(counts),
        "spread_ratio": round(max(counts) / max(min(counts), 1), 2),
        "jain": round(_jain([float(c) for c in counts]), 5),
        "starved_tenants": sum(1 for c in counts if c <= 1),
        "starved_pct": round(sum(1 for c in counts if c <= 1) / tenants * 100, 1),
        "cpu_ratio": round(cpu / max(wall, 1e-9), 3),
    }


# --------------------------------------------------------------------------

async def main() -> None:
    out: dict[str, Any] = {}

    print(f"=== 1. fairness ramp (open loop, {WINDOW_SECONDS}s window per level) ===")
    print(f"{'T':>4} {'done':>6} {'min':>5} {'max':>5} {'spread':>7} {'jain':>7} "
          f"{'p50ms':>8} {'p95ms':>9} {'sec':>7}")
    out["fairness"] = []
    for t in (2, 4, 8, 16, 32, 64, 128):
        row = await fairness_ramp(t)
        out["fairness"].append(row)
        print(f"{t:4d} {row['completed']:6d} {row['min_completions']:5d} "
              f"{row['max_completions']:5d} {row['spread_ratio']:7.3f} "
              f"{row['jain']:7.5f} {row['p50_wait_ms']:8.3f} "
              f"{row['p95_wait_ms']:9.3f} {row['seconds']:7.3f}")

    print("\n=== 2. noisy neighbour (1 loud tenant at 10x, 9 polite) ===")
    print(f"{'share':>6} {'cap':>5} {'loud':>6} {'polite':>7} {'pol_p50':>9} "
          f"{'pol_p95':>9} {'pol_p99':>9} {'loud_share':>11}   (fair=0.1000)")
    out["noisy"] = []
    for share in (1.0, 0.5):
        row = await noisy_neighbour(share)
        out["noisy"].append(row)
        print(f"{share:6.2f} {row['max_tenant_tokens']:5.1f} {row['loud_completed']:6d} "
              f"{row['polite_completed']:7d} {row['polite_p50_wait_ms']:9.3f} "
              f"{row['polite_p95_wait_ms']:9.3f} {row['polite_p99_wait_ms']:9.3f} "
              f"{row['loud_share_of_grants']:11.4f}")

    print("\n=== 3. cache-hit economics (8 tenants x 12 questions) ===")
    print(f"{'overlap':>8} {'distinct':>9} {'per-tenant':>11} {'shared':>7} "
          f"{'cost_req':>9} {'cost_%':>8}")
    out["cache"] = []
    for overlap in (0.0, 0.25, 0.5, 1.0):
        row = await cache_economics(tenants=8, questions=12, overlap=overlap)
        out["cache"].append(row)
        print(f"{overlap:8.2f} {row['distinct_questions']:9d} "
              f"{row['per_tenant_cache_upstream']:11d} {row['shared_cache_upstream']:7d} "
              f"{row['privacy_cost_requests']:9d} {row['privacy_cost_pct']:8.1f}")

    print("\n=== 4. tenant churn past MAX_TENANTS ===")
    print(f"{'minted':>8} {'retained':>9} {'evicted':>8} {'hot_alive':>10} "
          f"{'KiB':>8} {'B/tenant':>9}")
    out["churn"] = []
    for mint in (512, 2048, 8192, 32768):
        row = tenant_churn(mint)
        out["churn"].append(row)
        print(f"{mint:8d} {row['retained']:9d} {row['evicted']:8d} "
              f"{str(row['hot_tenant_survived']):>10} {row['kib_growth']:8.1f} "
              f"{row['bytes_per_retained_tenant']:9d}")

    print("\n=== 5. saturation (timeout=0.5s, burst=10 @ 500/s) ===")
    print(f"{'offered':>8} {'granted':>8} {'exhausted':>10} {'pct':>6} "
          f"{'wall_s':>8} {'cpu_s':>8} {'cpu/wall':>9} {'polls':>9} "
          f"{'polls/req':>10} {'polls/s':>9} {'hung':>6}")
    out["saturation"] = []
    for offered in (8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096):
        row = await saturation(offered)
        out["saturation"].append(row)
        print(f"{offered:8d} {row['granted']:8d} {row['quota_exhausted']:10d} "
              f"{row['exhausted_pct']:6.1f} {row['wall_s']:8.3f} {row['cpu_s']:8.3f} "
              f"{row['cpu_ratio']:9.3f} {row['polls']:9d} "
              f"{row['polls_per_request']:10.2f} {row['polls_per_second']:9d} "
              f"{str(row['hung']):>6}")

    print("\n=== 6. latency under load ===")
    print(f"{'conc':>5} {'reqs':>6} {'p50ms':>8} {'p95ms':>8} {'p99ms':>9} "
          f"{'sec':>7} {'rps':>9}")
    out["latency"] = []
    for conc in (1, 2, 4, 8, 16, 32, 64, 128, 256):
        row = await latency_at(conc)
        out["latency"].append(row)
        print(f"{conc:5d} {row['requests']:6d} {row['p50_ms']:8.3f} "
              f"{row['p95_ms']:8.3f} {row['p99_ms']:9.3f} {row['seconds']:7.3f} "
              f"{row['throughput_rps']:9.1f}")

    print("\n=== 7. starvation detail: current vs two candidate fixes ===")
    print(f"{'T':>5} {'variant':>8} {'total':>6} {'min':>5} {'max':>5} {'spread':>7} "
          f"{'jain':>8} {'starved':>8} {'starved%':>9}")
    out["starvation"] = []
    for t in (16, 32, 64, 128):
        for cls in (SharedQuota, _Jittered, _Fifo):
            row = await starvation(cls, t)
            out["starvation"].append(row)
            print(f"{t:5d} {row['variant']:>8} {row['total']:6d} {row['min']:5d} "
                  f"{row['max']:5d} {row['spread_ratio']:7.2f} {row['jain']:8.5f} "
                  f"{row['starved_tenants']:8d} {row['starved_pct']:9.1f}")

    # The knee: first concurrency where p95 more than doubles over the previous.
    # No absolute floor -- an earlier version guarded on p95 > 0.05ms and so
    # skipped the real knee (0.006ms -> 60.85ms) as "too small to count".
    knee = None
    lat = out["latency"]
    for prev, cur in zip(lat, lat[1:]):
        if cur["p95_ms"] > max(prev["p95_ms"], 1e-6) * 2 and cur["p95_ms"] > 1.0:
            knee = cur["concurrency"]
            break
    sat_start = next((r["offered"] for r in out["saturation"]
                      if r["quota_exhausted"] > 0), None)

    summary = {
        "knee_concurrency": knee,
        "saturation_starts_at_offered": sat_start,
        "worst_jain": min(r["jain"] for r in out["fairness"]),
        "worst_spread_ratio": max(r["spread_ratio"] for r in out["fairness"]),
        "max_cpu_ratio_under_saturation": max(r["cpu_ratio"] for r in out["saturation"]),
        "max_polls_per_second": max(r["polls_per_second"] for r in out["saturation"]),
        "any_hang": any(r["hung"] for r in out["saturation"]),
        "noisy_cap_bought_ms": round(
            out["noisy"][0]["polite_p95_wait_ms"] - out["noisy"][1]["polite_p95_wait_ms"], 3),
        "privacy_cost_requests_at_full_overlap": out["cache"][-1]["privacy_cost_requests"],
        "privacy_cost_pct_at_full_overlap": out["cache"][-1]["privacy_cost_pct"],
        "hot_tenant_survived_all_churn": all(r["hot_tenant_survived"] for r in out["churn"]),
        "starvation": {
            v: {
                "worst_jain": min(r["jain"] for r in out["starvation"] if r["variant"] == v),
                "worst_spread": max(r["spread_ratio"] for r in out["starvation"]
                                    if r["variant"] == v),
                "worst_starved_pct": max(r["starved_pct"] for r in out["starvation"]
                                         if r["variant"] == v),
                "throughput_at_128": next(r["total"] for r in out["starvation"]
                                          if r["variant"] == v and r["tenants"] == 128),
            }
            for v in ("sharedquota", "jittered", "fifo")
        },
    }
    REPORT.write_text(json.dumps({"summary": summary, "scenarios": out}, indent=2),
                      encoding="utf-8")
    print("\n--- summary ---")
    print(json.dumps(summary, indent=2))
    print(f"\nWrote {REPORT}")


if __name__ == "__main__":
    asyncio.run(main())
