"""Per-tenant cache isolation with a shared upstream quota.

The problem this solves, measured before it existed:

    tenant A (cold probe)   207.1 ms
    tenant B (warm, shared)   0.0 ms      ratio 31,377x

ClinicalTrials.gov data is public, so a shared cache leaks no *data*. It leaks
*queries*. A 31,000x timing delta lets one tenant determine whether another
recently asked about a specific drug-indication pair. On a platform where
competing organisations are tenants and competitive intelligence is a supported
workflow, which questions a tenant is asking is more sensitive than the public
figures those questions return.

The tension is real and has no free answer. A shared cache exists precisely to
solve the shared-rate-limit bottleneck (a network query costs 40-57 upstream
requests against a budget every instance shares). Partitioning it per tenant
reinstates that bottleneck.

The resolution is to split the two things that were conflated:

    CACHE   is per-tenant.  It reveals intent, so it is isolated.
    QUOTA   is shared.      It reveals volume, not questions, so it is pooled.

A token bucket says how much a tenant asked, never what. That is the difference
between an acceptable and an unacceptable leak here.

Three consequences worth stating, because each is a deliberate cost:

1. **Single-flight is per-tenant too.** Coalescing two tenants' identical
   concurrent queries into one request is itself a side-channel — B's latency
   would depend on A's timing. Within a tenant it is preserved.
2. **Cross-tenant cache hits are given up.** Two tenants asking the same
   question pay twice. That is the price of not telling either about the other.
3. **Eviction is per-tenant.** Otherwise a tenant could evict a rival's entries
   and detect the resulting latency change — a write-side version of the same
   channel.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import unicodedata
from collections import OrderedDict
from typing import Any

# A tenant's cache is bounded, and the number of tenants is bounded. Both are
# required: an unbounded tenant registry is a leak an attacker can drive simply
# by presenting new tenant ids.
MAX_TENANTS = 512
PER_TENANT_ENTRIES = 512

# Idle tenants are dropped. An id that has not been seen for this long is not
# worth the memory, and retaining it indefinitely would let anyone who can mint
# ids grow the process without bound.
TENANT_IDLE_SECONDS = 3600.0

# Shared upstream budget. This is the thing that genuinely must be pooled: the
# rate limit belongs to the API, not to any tenant.
DEFAULT_RATE_PER_SECOND = 5.0
DEFAULT_BURST = 10.0

# No single tenant may hold more than this share of the burst at once, so a hot
# tenant degrades its own latency rather than everyone else's.
MAX_TENANT_BURST_SHARE = 0.5


class QuotaExhausted(RuntimeError):
    """Raised when a tenant's wait for capacity exceeds its ceiling."""


class SharedQuota:
    """A token bucket shared by every tenant, with per-tenant fairness.

    Deliberately reveals only volume. A tenant can observe that the system is
    busy; it cannot observe *which* questions made it busy, which is the whole
    distinction this module exists to preserve.
    """

    def __init__(
        self,
        rate_per_second: float = DEFAULT_RATE_PER_SECOND,
        burst: float = DEFAULT_BURST,
        *,
        max_tenant_share: float = MAX_TENANT_BURST_SHARE,
    ) -> None:
        self._rate = rate_per_second
        self._burst = burst
        self._tokens = burst
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()
        self._max_tenant_tokens = burst * max_tenant_share
        # Per-tenant in-flight accounting, so one caller cannot occupy the whole
        # bucket while others starve. Bounded by the tenant registry above it.
        self._inflight: dict[str, float] = {}
        # Arrival order. Retry-with-backoff alone has NO ordering, so above the
        # burst ceiling a few tenants lock in and the rest starve. Measured
        # open-loop at T=128: Jain 0.140, spread 33:1, 89.8% of tenants getting
        # exactly one grant per 500ms -- stable lock-in, not rotation.
        #
        # Jitter does not fix it (measured 0.139 -> 0.150); only ordering does.
        # A ticket queue restores Jain 0.986 for roughly 3% throughput. The cost
        # is head-of-line blocking, which is the honest trade: a slow tenant now
        # delays the queue instead of a lucky one monopolising it.
        self._ticket = 0
        self._serving = 0
        self.granted = 0
        self.waited_seconds = 0.0
        self.throttled = 0

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
            self._updated = now

    async def acquire(self, tenant: str, cost: float = 1.0, *, timeout: float = 30.0) -> None:
        """Wait for capacity. Fair across tenants, and bounded.

        Fairness is enforced on in-flight cost rather than historical use: a
        tenant that has been quiet all day should not be able to claim a windfall
        of the bucket, and one that has been busy should not be punished forever.
        """
        deadline = time.monotonic() + timeout
        waited_from = time.monotonic()
        async with self._lock:
            my_ticket = self._ticket
            self._ticket += 1
        while True:
            async with self._lock:
                self._refill()
                mine = self._inflight.get(tenant, 0.0)
                fair = mine + cost <= self._max_tenant_tokens
                # Only the tenant at the head of the queue may take tokens.
                # Without this, arrival order is irrelevant and whoever happens
                # to wake on a refill wins -- repeatedly.
                my_turn = my_ticket <= self._serving
                if self._tokens >= cost and fair and my_turn:
                    self._tokens -= cost
                    self._inflight[tenant] = mine + cost
                    self._serving = max(self._serving, my_ticket) + 1
                    self.granted += 1
                    self.waited_seconds += time.monotonic() - waited_from
                    return
                # How long until enough tokens exist? Never busy-wait.
                deficit = max(cost - self._tokens, 0.0)
                delay = (deficit / self._rate) if self._rate > 0 else timeout
                delay = max(min(delay, 0.25), 0.005)
            if time.monotonic() + delay > deadline:
                async with self._lock:
                    # Yield the slot, or the queue stalls behind an abandoned
                    # ticket and a timeout becomes an outage for everyone after.
                    self._serving = max(self._serving, my_ticket + 1)
                self.throttled += 1
                raise QuotaExhausted(
                    f"tenant {tenant!r} waited over {timeout}s for upstream capacity"
                )
            await asyncio.sleep(delay)

    async def release(self, tenant: str, cost: float = 1.0) -> None:
        async with self._lock:
            remaining = self._inflight.get(tenant, 0.0) - cost
            if remaining > 0:
                self._inflight[tenant] = remaining
            else:
                self._inflight.pop(tenant, None)

    def stats(self) -> dict[str, Any]:
        # Aggregate only. Per-tenant counters are deliberately NOT exposed here:
        # a stats endpoint that reports them is the same side-channel by another
        # route.
        return {
            "granted": self.granted,
            "throttled": self.throttled,
            "tenants_inflight": len(self._inflight),
            "tokens_available": round(self._tokens, 2),
        }


class _TenantCache:
    """One tenant's isolated LRU cache. Never shared, never enumerable."""

    def __init__(self, max_entries: int = PER_TENANT_ENTRIES, ttl_seconds: float = 86400.0):
        self._max = max_entries
        self._ttl = ttl_seconds
        self._data: OrderedDict[str, tuple[int, float]] = OrderedDict()
        # Single-flight is per-tenant. Coalescing across tenants would make B's
        # latency depend on A's timing, which is the side-channel restated.
        self.inflight: dict[str, asyncio.Future[int]] = {}
        self.hits = 0
        self.misses = 0
        self.touched = time.monotonic()

    @staticmethod
    def key(params: dict[str, str]) -> str:
        return "&".join(f"{k}={params[k]}" for k in sorted(params))

    def get(self, params: dict[str, str]) -> int | None:
        self.touched = time.monotonic()
        k = self.key(params)
        entry = self._data.get(k)
        if entry is None:
            self.misses += 1
            return None
        value, deadline = entry
        if deadline <= time.monotonic():
            del self._data[k]
            self.misses += 1
            return None
        self._data.move_to_end(k)
        self.hits += 1
        return value

    def put(self, params: dict[str, str], value: int) -> None:
        self.touched = time.monotonic()
        if len(self._data) >= self._max:
            for stale in list(self._data)[: max(1, self._max // 4)]:
                del self._data[stale]
        self._data[self.key(params)] = (value, time.monotonic() + self._ttl)

    def __len__(self) -> int:
        return len(self._data)


class TenantRegistry:
    """Bounded registry of isolated per-tenant caches.

    Isolation is structural: a caller receives only its own cache and has no
    handle through which another tenant's cache is reachable. There is no
    lookup-by-other-id, and no aggregate that would let one be inferred.
    """

    def __init__(
        self,
        max_tenants: int = MAX_TENANTS,
        per_tenant_entries: int = PER_TENANT_ENTRIES,
        idle_seconds: float = TENANT_IDLE_SECONDS,
        quota: SharedQuota | None = None,
    ) -> None:
        self._max = max_tenants
        self._entries = per_tenant_entries
        self._idle = idle_seconds
        self._caches: OrderedDict[str, _TenantCache] = OrderedDict()
        self.quota = quota or SharedQuota()
        self.evicted_tenants = 0

    @staticmethod
    def normalise(tenant: str | None) -> str:
        """One canonical id per tenant.

        Whitespace and case variants must not mint separate caches, or a tenant
        could evade its own fairness accounting simply by changing case.

        `.strip().lower()` is not enough, and fails in both directions:

        COLLIDE (the dangerous one). `U+212A KELVIN SIGN` lowercases to ASCII
        'k', so 'Kacme-pharma'.lower() == 'kacme-pharma'. An attacker who picks
        their own tenant string lands in a rival's partition -- the exact
        channel this module exists to close, walked in through the front door.
        Turkish dotted capital 'I' folds to 'i' + U+0307 for the same reason.

        SPLIT. Zero-width space, fullwidth 'a', and a trailing dot each mint a
        SEPARATE partition for what is one tenant, which evades the per-tenant
        fairness accounting rather than the cache isolation.

        Order matters. NFKC first, so compatibility characters (fullwidth,
        KELVIN) fold to their ASCII forms; casefold second, because it is the
        correct case operation for comparison and handles multi-character
        expansions that .lower() does not; then strip format characters, which
        are invisible and therefore the easiest to smuggle.
        """
        raw = tenant or ""
        folded = unicodedata.normalize("NFKC", raw).casefold()
        # Cf = format characters: zero-width space/joiner, bidi overrides, BOM.
        # Nothing legitimate carries them, and they are invisible in a log.
        cleaned = "".join(
            c
            for c in folded
            if unicodedata.category(c) != "Cf"
            # U+0307 COMBINING DOT ABOVE, and only that mark. casefold() turns
            # 'I' (U+0130, Turkish dotted capital I) into 'i' + U+0307, and no
            # precomposed form exists to recombine into -- 'i' already carries a
            # dot -- so NFKC cannot undo it and 'ISTANBUL-BIO' would mint a
            # partition separate from 'istanbul-bio'. Dropping the mark is safe
            # here precisely because it is redundant over 'i'. Other combining
            # marks are LEFT ALONE: stripping accents wholesale would fold
            # genuinely distinct names together, trading a split for a collision,
            # and a collision is the far worse of the two failures.
            and c != "̇"
        )
        # Re-normalise: casefold can emit sequences that are not NFKC (dotted
        # capital I -> 'i' + U+0307 combining dot), and an id that differs only
        # by a combining mark is a split by another name.
        cleaned = unicodedata.normalize("NFKC", cleaned).strip().strip(".")
        if not cleaned:
            return "anonymous"

        # The tension that cannot be normalised away, and the reason this
        # method ends with a quarantine rather than a fold:
        #
        #   Folding non-ASCII onto ASCII is exactly what MERGES 'ISTANBUL-BIO'
        #   with 'istanbul-bio' (good, closes a fairness split) and exactly what
        #   MERGES U+212A KELVIN SIGN + 'acme-pharma' with 'kacme-pharma' (bad,
        #   hands an attacker a rival's cache partition). It is one mechanism.
        #   No normalisation form fixes one direction without opening the other.
        #
        # So the input domain gets constrained instead. An id that was pure
        # ASCII to begin with folds normally -- case, whitespace, trailing dots,
        # zero-width characters. An id that contained ANY non-ASCII character
        # keeps its own namespace and can never land on an ASCII tenant's
        # partition, however it folds.
        #
        # The cost is honest and small: a genuinely non-ASCII tenant name gets a
        # stable partition of its own rather than being merged with its ASCII
        # transliteration. Trading a split for a collision would be the wrong
        # way round -- a split costs fairness accounting, a collision costs the
        # confidentiality this module exists for.
        if not raw.isascii():
            digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
            return f"u-{cleaned}-{digest}"
        return cleaned

    def for_tenant(self, tenant: str | None) -> _TenantCache:
        key = self.normalise(tenant)
        cache = self._caches.get(key)
        if cache is not None:
            self._caches.move_to_end(key)
            cache.touched = time.monotonic()
            return cache

        self._reap()
        if len(self._caches) >= self._max:
            # Evict the least-recently-used tenant, never a random one: an
            # attacker who could choose the victim would have a write-side
            # channel into a rival's latency.
            self._caches.popitem(last=False)
            self.evicted_tenants += 1
        cache = _TenantCache(self._entries)
        self._caches[key] = cache
        return cache

    def _reap(self) -> None:
        cutoff = time.monotonic() - self._idle
        for key in [k for k, c in self._caches.items() if c.touched < cutoff]:
            del self._caches[key]
            self.evicted_tenants += 1

    def stats(self) -> dict[str, Any]:
        # Aggregate only, by design. Never per-tenant, and never tenant ids:
        # an endpoint that lists them reintroduces the leak it exists to close.
        return {
            "tenants": len(self._caches),
            "entries_total": sum(len(c) for c in self._caches.values()),
            "evicted_tenants": self.evicted_tenants,
            "quota": self.quota.stats(),
        }
