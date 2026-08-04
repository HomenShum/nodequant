"""ClinicalTrials.gov API v2 client.

Design note — why count-probes instead of paging:

The v2 API exposes `/stats/field/values`, but it rejects query filters
("Invalid prefix in parameter name: query.intr"), so it only ever returns
*global* counts across all ~380k studies. It cannot answer "phases for
pembrolizumab".

The alternative most implementations reach for is to page every matching
study and tally client-side. For a common drug that is thousands of records
per question, and it puts a large JSON payload in front of the model.

Instead we use `countTotal=true&pageSize=1`, which returns the exact match
count while transferring essentially no study data. One probe per bucket
gives an exact distribution:

    Pembrolizumab by phase -> 5 probes
    EARLY_PHASE1: 51  PHASE1: 1036  PHASE2: 1742  PHASE3: 360  PHASE4: 16

The counts come from the API, never from the model. Measured at ~1.8s for 8
sequential calls with no rate limiting observed.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any

import httpx

BASE_URL = "https://clinicaltrials.gov/api/v2"

# Study fields we pull when we need real records (for citations).
CITATION_FIELDS = "NCTId,BriefTitle,Phase,OverallStatus,LeadSponsorName,StartDate"

# Fields needed to discover entities for network graphs. Kept separate from
# CITATION_FIELDS deliberately: reusing the citation list here silently omitted
# InterventionName, which left every network graph with sponsor nodes only and
# therefore zero edges, since edges referencing an undiscovered node are dropped.
ENTITY_FIELDS = ("NCTId,LeadSponsorName,InterventionName,InterventionType,"
                 "LocationCountry")

# Bounded concurrency. Measured the hard way: 8 *sequential* calls complete in
# ~1.8s with no complaint, but a burst of 12 concurrent requests (6 count probes
# plus 6 citation fetches) earns HTTP 429. Sequential throughput says nothing
# about tolerated concurrency, so this is set low and paired with backoff rather
# than tuned to the edge of a limit the API does not document.
MAX_CONCURRENT_PROBES = 3

# Minimum spacing between request starts, smoothing bursts a semaphore alone
# would still allow (3 in flight can still mean 3 at the same instant).
#
# Tuned empirically rather than guessed: at 0.06s a network query issued 105
# requests of which 37 were retries, so a third of the work was spent being
# rejected. Pacing more slowly issues fewer total requests and finishes sooner.
MIN_REQUEST_INTERVAL_S = 0.18

# Direct callers of `_pace` are normally bounded by the request semaphore, but
# the queue is independently bounded so a future call site cannot turn waiting
# tasks into an unbounded memory collection.
MAX_PACE_WAITERS = 1024

# Retry policy for 429 and transient 5xx.
#
# Uses *full* jitter (sleep a random duration in [0, cap]) rather than
# cap-plus-small-jitter. With a narrow jitter band, a burst that collectively
# trips a limit backs off in lockstep and re-trips it on retry — observed here
# as six citation fetches failing four times in a row despite exponential
# backoff. Full jitter decorrelates them.
MAX_RETRIES = 5
BACKOFF_BASE_S = 0.4
BACKOFF_CAP_S = 8.0
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# When any request is throttled, every other request pauses too. A 429 is
# information about the *shared* budget, so reacting per-request lets the
# remaining in-flight calls keep pushing while one politely waits.
THROTTLE_COOLDOWN_S = 1.5

# Ceiling on concurrent single-flight entries. Well above any legitimate fan-out
# (a network query peaks near 60 probes) and far below anything that threatens
# the process, so it only ever fires on abuse or on leaked cancelled futures.
MAX_INFLIGHT = 10_000

# Hard cap on probes per request so a pathological plan cannot fan out forever.
MAX_PROBES_PER_REQUEST = 60


# Count-probe cache.
#
# Probes repeat heavily: every phase question re-counts the same six buckets for
# a popular condition, the eval re-probes the same cases, and a network graph
# re-counts a node once per pair it appears in. Counts are stable over a session,
# so caching them removes most of the latency without weakening the guarantee —
# a cache hit still returns a number this process measured from the API.
#
# BOUNDED with explicit eviction: an unbounded dict keyed on user-controlled
# filters is a memory leak an agent loop would find within minutes.
CACHE_MAX_ENTRIES = 2048

# L1 entries expire too.
#
# Until now the process lifetime WAS the staleness bound, which made a missing
# TTL safe by accident. That accident ends the moment this cache is warmed from
# a shared persistent layer: a long-lived worker would then serve counts older
# than the shared TTL straight out of memory, enforcing expiry on the slow path
# and bypassing it on the fast one. Trial counts change daily, and a stale count
# presented as measured is precisely the failure this system exists to prevent.
CACHE_TTL_SECONDS = 24 * 3600


class _BoundedCache:
    """LRU cache with a hard entry cap and a per-entry TTL.

    Was FIFO. Measured cost of that: a key read on every one of 3,000 requests
    was still evicted, producing a 31.8% miss rate and 954 avoidable upstream
    probes -- roughly 171s of a rate budget every instance shares. FIFO evicts
    by age; the working set is defined by recency.
    """

    def __init__(self, max_entries: int = CACHE_MAX_ENTRIES,
                 ttl_seconds: float = CACHE_TTL_SECONDS):
        self._max = max_entries
        self._ttl = ttl_seconds
        # value plus a monotonic deadline: monotonic so a system clock change
        # cannot resurrect an expired entry.
        self._data: OrderedDict[str, tuple[int, float]] = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.expired = 0

    @staticmethod
    def key(params: dict[str, str]) -> str:
        # Sorted so two logically identical filters built in different orders
        # produce the same key.
        return "&".join(f"{k}={params[k]}" for k in sorted(params))

    def get(self, params: dict[str, str]) -> int | None:
        key = self.key(params)
        entry = self._data.get(key)
        if entry is None:
            self.misses += 1
            return None
        value, deadline = entry
        if deadline > time.monotonic():
            # Recency, not age: move the hot key to the end so it survives.
            self._data.move_to_end(key)
        if deadline <= time.monotonic():
            # Expired is a MISS, not a hit. Counted separately so a mis-set TTL
            # is distinguishable from a genuinely diverse query mix.
            del self._data[key]
            self.expired += 1
            self.misses += 1
            return None
        self.hits += 1
        return value

    def put(self, params: dict[str, str], value: int) -> None:
        if len(self._data) >= self._max:
            # Evict least-recently-USED, a quarter at a time so eviction cost is
            # amortised rather than paid on every write.
            for stale in list(self._data)[: self._max // 4]:
                del self._data[stale]
        self._data[self.key(params)] = (value, time.monotonic() + self._ttl)

    def __len__(self) -> int:
        return len(self._data)


class CTGovError(RuntimeError):
    """Raised when ClinicalTrials.gov returns an unusable response."""


@dataclass(frozen=True)
class Filter:
    """A single query constraint, expressed in the API's own vocabulary.

    `params` are merged directly into the querystring. Keeping them as the
    API's native parameter names (rather than inventing an intermediate DSL)
    means there is no translation layer to get wrong, and the exact request we
    issued is reproducible from the response metadata.
    """

    params: dict[str, str] = field(default_factory=dict)

    def merged_with(self, other: "Filter") -> "Filter":
        """Combine two filters, concatenating `filter.advanced` clauses with AND."""
        merged = dict(self.params)
        for key, value in other.params.items():
            if key == "filter.advanced" and key in merged:
                merged[key] = f"{merged[key]} AND {value}"
            else:
                merged[key] = value
        return Filter(merged)


class CTGovClient:
    """Async client for the subset of ClinicalTrials.gov v2 we rely on."""

    def __init__(self, timeout: float = 20.0, client: httpx.AsyncClient | None = None,
                 store: Any = None, tenants: Any = None):
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_PROBES)
        self._pace_condition = asyncio.Condition()
        self._pace_waiters: deque[object] = deque()
        self._next_slot = 0.0
        self.request_count = 0
        self.retry_count = 0
        self.coalesced = 0
        self._cache = _BoundedCache()
        # Optional shared L2. None means exactly today's behaviour: the
        # service must run with no database at all.
        self._store = store
        # Single-flight: N concurrent misses on the SAME key share one upstream
        # request instead of issuing N. Measured before this: 8 concurrent
        # identical queries produced 8 probes, 7 of them wasted (88%) and 1.26s
        # of a shared rate budget spent re-asking a question already in flight.
        self._inflight: dict[str, asyncio.Future[int]] = {}
        # Per-tenant isolation. When a registry is present, the global cache and
        # in-flight table above are bypassed entirely: a tenant's counts and its
        # coalescing both live in its own cache, so neither its hits nor its
        # latency can be observed by another tenant. See app/tenancy.py for why
        # the quota stays shared while the cache does not.
        self._tenants = tenants
        self._tenant: str | None = None

    async def _pace(self) -> None:
        """Space request starts by at least MIN_REQUEST_INTERVAL_S.

        The semaphore bounds how many requests are in flight; it does not stop
        four of them starting in the same millisecond. This does.
        """
        loop = asyncio.get_running_loop()
        token = object()
        async with self._pace_condition:
            if len(self._pace_waiters) >= MAX_PACE_WAITERS:
                raise CTGovError("request pacing queue is full")
            self._pace_waiters.append(token)
            try:
                while True:
                    wait = self._next_slot - loop.time()
                    if self._pace_waiters[0] is token and wait <= 0:
                        self._pace_waiters.popleft()
                        self._next_slot = loop.time() + MIN_REQUEST_INTERVAL_S
                        self._pace_condition.notify_all()
                        return
                    if self._pace_waiters[0] is not token:
                        await self._pace_condition.wait()
                        continue
                    try:
                        # A condition wait, rather than sleep, lets a newly
                        # applied shared cooldown wake the head immediately so
                        # it recalculates against the extended deadline.
                        await asyncio.wait_for(self._pace_condition.wait(), timeout=wait)
                    except TimeoutError:
                        pass
            finally:
                if token in self._pace_waiters:
                    was_head = self._pace_waiters[0] is token
                    self._pace_waiters.remove(token)
                    if was_head:
                        self._pace_condition.notify_all()

    async def _apply_cooldown(self, seconds: float) -> None:
        """Delay the shared schedule so every pending request backs off together."""
        async with self._pace_condition:
            loop = asyncio.get_running_loop()
            self._next_slot = max(self._next_slot, loop.time()) + seconds
            self._pace_condition.notify_all()

    async def __aenter__(self) -> "CTGovClient":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                headers={"Accept": "application/json"},
            )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("CTGovClient must be used as an async context manager")
        return self._client

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        """GET with pacing, bounded concurrency, and backoff on 429/5xx.

        Retries are jittered so that a burst which collectively trips a limit
        does not retry in lockstep and trip it again. `Retry-After` is honoured
        when the API sends it, because a served hint beats a guessed backoff.
        """
        last_detail = ""
        for attempt in range(MAX_RETRIES):
            async with self._semaphore:
                await self._pace()
                self.request_count += 1
                try:
                    response = await self.client.get(f"{BASE_URL}{path}", params=params)
                except httpx.RequestError as exc:
                    last_detail = f"transport error: {exc}"
                    response = None

            if response is not None:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise CTGovError(f"{path} returned non-JSON body") from exc
                if response.status_code not in RETRYABLE_STATUS:
                    # Surface the API's own message; it is specific and worth keeping.
                    raise CTGovError(
                        f"{path} returned HTTP {response.status_code}: {response.text[:200]}"
                    )
                last_detail = f"HTTP {response.status_code}"

            if attempt + 1 >= MAX_RETRIES:
                break

            # Full jitter: uniform in [0, cap]. See the note on RETRYABLE_STATUS.
            cap = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** attempt))
            delay = random.uniform(0, cap)
            if response is not None:
                header = response.headers.get("Retry-After")
                if header:
                    try:
                        # Capped: an upstream Retry-After of 86400 (or a garbage
                        # huge value) would otherwise park the request for a day
                        # while the caller's connection waits on it.
                        delay = max(delay, min(float(header), BACKOFF_CAP_S))
                    except ValueError:
                        pass
            self.retry_count += 1
            if response is not None and response.status_code == 429:
                await self._apply_cooldown(THROTTLE_COOLDOWN_S)
            await asyncio.sleep(delay)

        raise CTGovError(f"{path} failed after {MAX_RETRIES} attempts ({last_detail})")

    async def count(self, filt: Filter) -> int:
        """Return the exact number of studies matching `filt`.

        This is the primitive the whole aggregation layer is built on. It asks
        for one study and reads only `totalCount`, so cost is independent of
        how many studies actually match.
        """
        params = {**filt.params, "countTotal": "true", "pageSize": "1", "fields": "NCTId"}
        cache = self._cache
        inflight = self._inflight
        if self._tenants is not None:
            scoped = self._tenants.for_tenant(self._tenant)
            cache, inflight = scoped, scoped.inflight

        cached = cache.get(params)
        if cached is not None:
            return cached

        key = _BoundedCache.key(params)
        existing = inflight.get(key)
        if existing is not None:
            # Someone is already asking. Wait for their answer rather than
            # asking again -- the upstream quota is shared, so a duplicate probe
            # costs every other request too.
            self.coalesced += 1
            return await existing

        if len(inflight) >= MAX_INFLIGHT:
            # Every other collection in this file is bounded; this one was
            # missed because its entries "always resolve" -- true on the happy
            # path, and false the moment a probe is cancelled mid-flight, which
            # is exactly what a client disconnect does. Keys are query params,
            # so an attacker who varies the query string grows it for free.
            #
            # Refuse rather than evict. Evicting an in-flight entry would let a
            # second caller issue a duplicate upstream probe against the shared
            # quota, so eviction converts a memory problem into a rate-limit
            # problem. Backpressure is the honest answer: the caller retries.
            raise CTGovError(
                f"too many probes in flight ({len(inflight)}); retry shortly"
            )

        loop = asyncio.get_running_loop()
        future: asyncio.Future[int] = loop.create_future()
        inflight[key] = future
        try:
            total = await self._count_uncached(params, cache)
        except BaseException as exc:
            future.set_exception(exc)
            # Waiters must see the failure, but the exception must not be
            # reported as un-retrieved if nobody awaited it.
            future.exception()
            raise
        else:
            future.set_result(total)
            return total
        finally:
            inflight.pop(key, None)

    async def _count_uncached(self, params: dict[str, str], cache: Any = None) -> int:
        """The actual fetch. Only ever entered by one coroutine per key."""
        cache = self._cache if cache is None else cache
        if self._store is not None:
            # A cache is an optimisation and must never be able to FAIL a
            # request. Today's Store catches everything internally, but that is
            # a property of one implementation, not of this seam — so the
            # boundary lives here too.
            try:
                # Namespace the L2 by tenant. A per-tenant L1 miss falling
                # through to a SHARED L2 row reopens the timing channel at a
                # measured 546x -- the same defect one layer down.
                shared = await self._store.get_count(self._tenant_scoped(params))
            except Exception:
                shared = None
            if shared is not None:
                # Warm L1 from L2. Safe only because L1 now expires too --
                # without its TTL this line would let a long-lived worker serve
                # counts past the shared TTL out of memory indefinitely.
                cache.put(params, shared.total_count)
                return shared.total_count
        payload = await self._get("/studies", params)
        total = payload.get("totalCount")
        if not isinstance(total, int):
            raise CTGovError("response did not include an integer totalCount")
        cache.put(params, total)
        if self._store is not None:
            try:
                await self._store.put_count(self._tenant_scoped(params), total)
            except Exception:
                pass  # a write-through failure must not lose a measured count
        return total

    def _tenant_scoped(self, params: dict[str, str]) -> dict[str, str]:
        """Params namespaced by tenant for L2 lookups.

        The prefix key is not a query parameter -- it never reaches the API. It
        exists so two tenants asking the same question occupy different rows.
        """
        if not self._tenant:
            return params
        return {"__tenant": self._tenant, **params}

    def for_tenant(self, tenant: str | None) -> "CTGovClient":
        """Scope this client to a tenant for the duration of one request.

        Returns a shallow view sharing the transport, the pacing state and the
        shared quota -- so the upstream budget stays pooled -- while cache reads
        and writes are routed to that tenant's isolated cache.
        """
        scoped = object.__new__(CTGovClient)
        scoped.__dict__ = dict(self.__dict__)
        scoped._tenant = self._tenants.normalise(tenant) if self._tenants else None
        return scoped

    @property
    def cache_stats(self) -> dict[str, int]:
        return {"entries": len(self._cache), "hits": self._cache.hits,
                "misses": self._cache.misses, "expired": self._cache.expired,
                "coalesced": self.coalesced, "inflight": len(self._inflight)}

    async def count_many(self, filters: dict[str, Filter]) -> dict[str, int]:
        """Run one count-probe per bucket concurrently.

        Returns a bucket-label -> exact-count mapping. Raises if the plan
        exceeds `MAX_PROBES_PER_REQUEST`, rather than silently truncating:
        a partial distribution presented as complete is a correctness bug, not
        a performance tradeoff.
        """
        if len(filters) > MAX_PROBES_PER_REQUEST:
            raise CTGovError(
                f"plan requires {len(filters)} probes, exceeding the "
                f"{MAX_PROBES_PER_REQUEST} cap; narrow the query"
            )
        labels = list(filters)
        counts = await asyncio.gather(*(self.count(filters[k]) for k in labels))
        return dict(zip(labels, counts))

    async def sample_studies(
        self, filt: Filter, limit: int = 3, fields: str = CITATION_FIELDS
    ) -> list[dict[str, Any]]:
        """Fetch a few real study records.

        Deliberately small for citations: they need to point at genuine records,
        but we do not need every record that contributed to a bucket in order to
        prove the bucket is real. Callers doing entity discovery pass
        `ENTITY_FIELDS` and a larger limit.
        """
        params = {
            **filt.params,
            "pageSize": str(max(1, min(limit, 200))),
            "fields": fields,
        }
        payload = await self._get("/studies", params)
        studies = payload.get("studies")
        return studies if isinstance(studies, list) else []
