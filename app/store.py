"""Optional Postgres persistence for the count-probe cache and the entity index.

WHY THIS EXISTS
---------------

`app/ctgov.py`'s `_BoundedCache` is per-process. It dies with the worker, so N
service instances share nothing — while sharing the one thing that actually
binds: the ClinicalTrials.gov rate limit. A network query costs 40-57 upstream
requests, which means horizontal scaling makes the bottleneck *worse*, not
better. Two workers asking the same phase question pay twice for the same six
numbers and spend the shared budget doing it. That is the scaling problem the
README names, and it is the only reason this module exists.

WHAT IT IS ALLOWED TO DO
------------------------

Every number this service reports is an API `totalCount`. A cached value is
legitimate only if it is a number THIS SYSTEM measured from the API and can
still replay. Persistence must not turn a stale value into an apparently-fresh
measurement. Three consequences, each enforced in code below rather than
documented and hoped for:

1. **TTL is a correctness bound, not a performance knob.** Trial counts change
   daily. A count measured 40 hours ago is not a measurement of today's corpus,
   so past its TTL this module reports a MISS — it does not return the row with
   a caveat, because a caveat downstream of a `dict.get()` is a caveat nobody
   reads. The row is invisible. The caller re-probes.

2. **A cached count keeps its provenance.** `params` (the exact querystring) and
   `measured_at` travel with the value, so any served count is replayable
   against the live API and attributable to a moment in time.

3. **Reads never invent a row.** A miss returns ``None``. Never ``0``, never a
   default, never a partial. Zero is a real and meaningful answer from this API
   ("no trials match"), so returning it on a cache miss would fabricate a
   measurement — the exact failure this system exists to prevent.

OPTIONALITY
-----------

Absent ``DATABASE_URL`` this module is inert: every call is a safe no-op, the
driver is never imported, and the service behaves exactly as it does today. The
same is true if the database is configured but unreachable, or fails mid-flight:
reads degrade to misses and writes drop silently. That degradation direction is
chosen deliberately. A cache miss costs latency; a cache error propagated into
the request path costs availability; a stale hit costs correctness. This module
will spend latency, and never the other two.

INTEGRATION
-----------

Designed as an adapter the existing in-process cache can *delegate to*, not a
replacement for it. `_BoundedCache` stays the L1 (per-process, microseconds);
this is the L2 (shared, one network round-trip). Nothing here imports
`app.ctgov`, so wiring it up is a change in one direction only, and the service
still runs with no database at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# 24 hours. ClinicalTrials.gov ingests new and updated registrations
# continuously; a count is a measurement of a corpus that moves daily. This is
# the outer bound on how wrong a served count is allowed to be, so it belongs
# with the correctness constants, not the tuning ones. Configurable so an
# operator can tighten it; there is a hard ceiling below so nobody can widen it
# into meaninglessness.
DEFAULT_TTL_SECONDS = 24 * 60 * 60

# A TTL of a week is not a cache, it is a stale dataset with a friendly name.
# Clamped rather than trusted: this value arrives from the environment.
MAX_TTL_SECONDS = 7 * 24 * 60 * 60
MIN_TTL_SECONDS = 1

# Pool bounds. Small on purpose: this cache sits in front of a rate-limited
# upstream, so it is never the hot path, and an oversized pool on N workers is
# just a different exhaustion problem (Postgres has a connection limit too).
DEFAULT_POOL_MIN = 1
DEFAULT_POOL_MAX = 5

# Every database call is wrapped in this. An unbounded await on a degraded
# database would hold the request open longer than simply re-probing the API,
# which defeats the purpose of the cache — so the timeout is deliberately
# shorter than a typical upstream probe (~300ms) plus its retries.
DEFAULT_TIMEOUT_S = 2.0
CONNECT_TIMEOUT_S = 5.0

# Bound on rows removed per sweep. A table keyed on user-controlled filters
# grows without limit if nothing prunes it — the same leak `_BoundedCache`'s
# eviction guards against, just with a slower fuse because disk is bigger than
# RAM. Bounded per call so a sweep cannot lock the table for an unbounded time.
DEFAULT_SWEEP_LIMIT = 5_000

# Guard against a caller writing something that is not an API totalCount.
MAX_PLAUSIBLE_COUNT = 100_000_000

DEFAULT_SOURCE = "clinicaltrials.gov/api/v2"

MIGRATION_PATH = Path(__file__).resolve().parent.parent / "migrations" / "001_init.sql"


def probe_key(params: Mapping[str, Any]) -> str:
    """Normalise a filter into a cache key.

    Sorted by parameter name so two logically identical filters built in
    different orders produce the same key and collide on one row, rather than
    paying two upstream probes for one number.

    This intentionally reproduces `app.ctgov._BoundedCache.key` rather than
    importing it: importing `app.ctgov` here would make the dependency
    bidirectional, and the integration direction has to stay one-way for this
    layer to remain optional. `tests/test_store.py` asserts the two agree, so
    the duplication is pinned by a test rather than by a comment.
    """
    return "&".join(f"{k}={params[k]}" for k in sorted(params))


def _resolve_ttl(ttl_seconds: float | None) -> float:
    if ttl_seconds is None:
        raw = os.environ.get("PROBE_CACHE_TTL_SECONDS", "")
        try:
            ttl_seconds = float(raw) if raw.strip() else DEFAULT_TTL_SECONDS
        except ValueError:
            log.warning("PROBE_CACHE_TTL_SECONDS=%r is not a number; using default", raw)
            ttl_seconds = DEFAULT_TTL_SECONDS
    # Clamped, not rejected: a misconfigured TTL should degrade to a safe one,
    # not take the service down. Clamping toward *fresher* is the safe
    # direction — the failure mode of a too-short TTL is cost, not a wrong number.
    return max(MIN_TTL_SECONDS, min(float(ttl_seconds), MAX_TTL_SECONDS))


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Read result
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CachedCount:
    """A count this system measured, replayed from shared storage.

    Frozen, and carrying its own provenance, because a bare ``int`` handed back
    from a cache is indistinguishable from a fresh measurement at the call site.
    Anything that reports this number can say when it was measured, from what
    request, and by which source — which is what makes it legitimate to report
    at all.
    """

    total_count: int
    """The API's `totalCount`, verbatim. Never computed, never defaulted."""

    params: dict[str, str]
    """The exact querystring that produced it. Keeps the value replayable."""

    measured_at: datetime
    """When the API was actually asked. Not when this row was written."""

    source: str
    """Which upstream measured it."""

    age_seconds: float
    """Age at read time, computed against the database clock rather than the
    worker's — N workers with skewed clocks must not disagree about whether the
    same row is expired."""

    @property
    def provenance(self) -> str:
        """One line a trace or a `meta.assumptions` entry can carry verbatim."""
        return (
            f"count={self.total_count} measured {self.age_seconds:.0f}s ago "
            f"({self.measured_at.isoformat()}) from {self.source}; "
            f"replayable as {probe_key(self.params)}"
        )


@dataclass
class StoreStats:
    """Counters, so the cache can be observed rather than assumed."""

    hits: int = 0
    misses: int = 0
    expired: int = 0
    """Rows found but past TTL. Counted separately from `misses` because a high
    expiry rate means the TTL is mis-set, while a high miss rate means the query
    mix is diverse — different problems with different fixes."""
    writes: int = 0
    errors: int = 0
    """Database failures that degraded to a miss. Non-zero here means the
    service is silently paying full upstream cost; it must be visible."""

    def as_dict(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "expired": self.expired,
            "writes": self.writes,
            "errors": self.errors,
        }


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


class Store:
    """Optional async Postgres adapter. Inert without ``DATABASE_URL``.

    Usage::

        store = Store()                 # reads DATABASE_URL from the environment
        await store.connect()           # False if disabled or unreachable
        hit = await store.get_count(params)   # CachedCount | None
        await store.put_count(params, total)
        await store.close()

    Or as an async context manager::

        async with Store() as store:
            ...

    Every method is safe to call when disabled; none of them raise.
    """

    def __init__(
        self,
        dsn: str | None = None,
        *,
        ttl_seconds: float | None = None,
        pool_min: int = DEFAULT_POOL_MIN,
        pool_max: int = DEFAULT_POOL_MAX,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        source: str = DEFAULT_SOURCE,
    ):
        # An explicit `dsn=""` is as much a request to stay disabled as an
        # absent DATABASE_URL, so both fall through to the same inert path.
        self._dsn = (dsn if dsn is not None else os.environ.get("DATABASE_URL", "")) or ""
        self.ttl_seconds = _resolve_ttl(ttl_seconds)
        self._pool_min = max(1, pool_min)
        self._pool_max = max(self._pool_min, pool_max)
        self._timeout_s = timeout_s
        self._source = source
        self._pool: Any = None
        self._connect_lock = asyncio.Lock()
        # Distinct from `enabled`: configured-but-broken. Once a connection
        # attempt has failed we stop retrying it per-request — a database that
        # is down should cost one failed connect, not one per query.
        self._degraded = False
        self.stats = StoreStats()

    # -- lifecycle ---------------------------------------------------------

    @property
    def configured(self) -> bool:
        """True if a DSN was supplied. Says nothing about reachability."""
        return bool(self._dsn)

    @property
    def enabled(self) -> bool:
        """True only if there is a live pool. This is what the read/write paths
        check, so 'no DATABASE_URL' and 'database is down' take the same
        no-op branch and there is only one degradation path to reason about."""
        return self._pool is not None and not self._degraded

    async def connect(self) -> bool:
        """Open the pool. Returns True if the store is usable.

        Never raises. An absent DSN, an unreachable host, bad credentials, or a
        missing `asyncpg` all return False and leave the service running exactly
        as it does with no database at all.
        """
        if not self._dsn:
            return False
        if self._pool is not None:
            return not self._degraded
        async with self._connect_lock:
            if self._pool is not None:
                return not self._degraded
            if self._degraded:
                return False
            try:
                import asyncpg  # imported lazily: never required to run the service
            except ImportError:
                log.warning(
                    "DATABASE_URL is set but asyncpg is not installed; "
                    "running without shared probe cache"
                )
                self._degraded = True
                return False
            try:
                self._pool = await asyncio.wait_for(
                    asyncpg.create_pool(
                        dsn=self._dsn,
                        min_size=self._pool_min,
                        max_size=self._pool_max,
                        command_timeout=self._timeout_s,
                    ),
                    timeout=CONNECT_TIMEOUT_S,
                )
            except Exception as exc:  # asyncpg raises a wide family here
                log.warning("probe cache unavailable (%s); continuing without it", exc)
                self._pool = None
                self._degraded = True
                return False
            self._degraded = False
            return True

    async def close(self) -> None:
        """Close the pool. Safe to call when never connected."""
        pool, self._pool = self._pool, None
        if pool is not None:
            try:
                await pool.close()
            except Exception as exc:
                log.warning("error closing probe cache pool: %s", exc)

    async def __aenter__(self) -> "Store":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    def _fail(self, what: str, exc: BaseException) -> None:
        """Record a database failure without letting it reach the caller.

        Deliberately does NOT mark the store degraded: a single statement
        failing (a deadlock, a timeout) is not evidence the database is gone,
        and permanently disabling the cache on one bad query would be an
        overreaction. Connection-level failure is handled in `connect`.
        """
        self.stats.errors += 1
        log.warning("probe cache %s failed (%s); treating as miss", what, exc)

    # -- probe cache -------------------------------------------------------

    async def get_count(self, params: Mapping[str, Any]) -> CachedCount | None:
        """Return a still-valid measured count, or None.

        None means "not measured recently enough" — including the case where a
        row exists but is past its TTL. Callers must treat None as a MISS and
        re-probe. There is no code path here that returns a number this system
        did not measure, and none that returns a number it measured too long ago.
        """
        if not self.enabled:
            self.stats.misses += 1
            return None
        key = probe_key(params)
        try:
            row = await asyncio.wait_for(
                self._pool.fetchrow(
                    # The TTL predicate lives in SQL against the DATABASE clock,
                    # not in Python against the worker's. N workers with skewed
                    # clocks must not disagree about whether a row is expired.
                    #
                    # `expired` is selected rather than inferred so the caller
                    # can distinguish "never measured" from "measured, too old"
                    # — the second means the TTL is doing its job and is worth
                    # counting separately.
                    """
                    SELECT total_count,
                           params,
                           measured_at,
                           source,
                           EXTRACT(EPOCH FROM (now() - measured_at))::float8 AS age_seconds,
                           (measured_at <= now() - ($2::float8 * interval '1 second'))
                               AS expired
                      FROM probe_cache
                     WHERE cache_key = $1
                    """,
                    key,
                    float(self.ttl_seconds),
                ),
                timeout=self._timeout_s,
            )
        except Exception as exc:
            self._fail("read", exc)
            self.stats.misses += 1
            return None

        if row is None:
            self.stats.misses += 1
            return None
        if row["expired"]:
            # A stale count is not a hit with a warning. It is a miss. The row
            # is left in place for the bounded sweep to remove rather than
            # deleted on the read path, so a read stays a read.
            self.stats.expired += 1
            self.stats.misses += 1
            return None

        try:
            raw = row["params"]
            # asyncpg returns jsonb as str unless a codec is registered.
            stored = json.loads(raw) if isinstance(raw, (str, bytes)) else dict(raw)
            total = int(row["total_count"])
        except (TypeError, ValueError) as exc:
            # A row we cannot decode is a row we cannot vouch for. Miss.
            self._fail("decode", exc)
            self.stats.misses += 1
            return None

        self.stats.hits += 1
        return CachedCount(
            total_count=total,
            params={str(k): str(v) for k, v in stored.items()},
            measured_at=row["measured_at"],
            source=row["source"],
            age_seconds=float(row["age_seconds"]),
        )

    async def put_count(
        self,
        params: Mapping[str, Any],
        total_count: int,
        *,
        source: str | None = None,
        measured_at: datetime | None = None,
    ) -> bool:
        """Persist a count this system just measured. Returns True if stored.

        `measured_at` defaults to now and should be passed only when the caller
        knows the real measurement time (a replayed trace, a backfill). It is
        the field TTL is judged against, so passing a time you did not measure
        at is the one way to defeat the freshness guarantee from the outside.
        """
        if not self.enabled:
            return False

        # Reject anything that is not plausibly an API totalCount. A cache that
        # will store whatever it is handed becomes a laundering path for
        # non-measured numbers; the write path is where that gets stopped.
        if isinstance(total_count, bool) or not isinstance(total_count, int):
            log.warning("refusing to cache non-integer count %r", total_count)
            return False
        if total_count < 0 or total_count > MAX_PLAUSIBLE_COUNT:
            log.warning("refusing to cache implausible count %r", total_count)
            return False
        if not params:
            log.warning("refusing to cache a count with no parameters (not replayable)")
            return False

        key = probe_key(params)
        payload = json.dumps({str(k): str(v) for k, v in params.items()}, sort_keys=True)
        stamp = measured_at or _now()
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)

        try:
            await asyncio.wait_for(
                self._pool.execute(
                    # Upsert, so concurrent writers of the same key converge on
                    # one row instead of raising or duplicating. Last writer
                    # wins, which is correct here: both wrote a number they
                    # measured, and the later measurement is the fresher one.
                    #
                    # measured_at is overwritten with EXCLUDED, never left at
                    # the old value — the whole point is that this row now
                    # represents a NEW measurement. written_at moves with it so
                    # the two timestamps stay independently auditable.
                    """
                    INSERT INTO probe_cache
                        (cache_key, total_count, params, measured_at, source, written_at)
                    VALUES ($1, $2, $3::jsonb, $4, $5, now())
                    ON CONFLICT (cache_key) DO UPDATE
                       SET total_count = EXCLUDED.total_count,
                           params      = EXCLUDED.params,
                           measured_at = EXCLUDED.measured_at,
                           source      = EXCLUDED.source,
                           written_at  = now()
                    """,
                    key,
                    total_count,
                    payload,
                    stamp,
                    source or self._source,
                ),
                timeout=self._timeout_s,
            )
        except Exception as exc:
            self._fail("write", exc)
            return False
        self.stats.writes += 1
        return True

    async def sweep_expired(self, limit: int = DEFAULT_SWEEP_LIMIT) -> int:
        """Delete rows past their TTL. Returns how many were removed.

        BOUND: `probe_cache` is keyed on user-controlled filters, so without a
        sweep it grows without limit — the same leak `_BoundedCache`'s eviction
        exists to prevent, on a slower fuse. Bounded per call so a sweep can
        never take an unbounded lock on the read path's table.
        """
        if not self.enabled:
            return 0
        try:
            status = await asyncio.wait_for(
                self._pool.execute(
                    """
                    DELETE FROM probe_cache
                     WHERE cache_key IN (
                           SELECT cache_key
                             FROM probe_cache
                            WHERE measured_at <= now() - ($1::float8 * interval '1 second')
                            ORDER BY measured_at
                            LIMIT $2
                     )
                    """,
                    float(self.ttl_seconds),
                    int(max(1, limit)),
                ),
                timeout=max(self._timeout_s, 10.0),
            )
        except Exception as exc:
            self._fail("sweep", exc)
            return 0
        # asyncpg returns e.g. "DELETE 42".
        try:
            return int(str(status).rsplit(" ", 1)[-1])
        except ValueError:
            return 0

    # -- entity index ------------------------------------------------------
    #
    # Provided for app/entities.py (owned by another agent). Nothing else in
    # this module depends on these; they exist so the table has a documented,
    # tested access shape rather than only a DDL file.

    async def upsert_alias(
        self,
        *,
        kind: str,
        canonical: str,
        alias: str,
        alias_norm: str,
        embedding: bytes | None = None,
        embedding_dim: int | None = None,
        source: str = "offline-index",
    ) -> bool:
        """Record that `alias` may denote `canonical`. Returns True if stored.

        `alias_norm` is supplied by the caller — it is `entities.normalize`'s
        output, and re-implementing that normalisation in SQL would create two
        copies that drift. There is deliberately no count parameter: this table
        stores proposals, and a proposal that could carry a number would be a
        way to smuggle an unverified count into a response.
        """
        if not self.enabled:
            return False
        if (embedding is None) != (embedding_dim is None):
            log.warning("refusing alias with mismatched embedding/embedding_dim")
            return False
        if embedding is not None and len(embedding) != (embedding_dim or 0) * 4:
            log.warning("refusing alias whose embedding length disagrees with its dim")
            return False
        try:
            await asyncio.wait_for(
                self._pool.execute(
                    """
                    INSERT INTO entity_alias
                        (kind, canonical, alias, alias_norm,
                         embedding, embedding_dim, source, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, now())
                    ON CONFLICT (kind, alias_norm) DO UPDATE
                       SET canonical     = EXCLUDED.canonical,
                           alias         = EXCLUDED.alias,
                           embedding     = EXCLUDED.embedding,
                           embedding_dim = EXCLUDED.embedding_dim,
                           source        = EXCLUDED.source,
                           updated_at    = now()
                    """,
                    kind, canonical, alias, alias_norm,
                    embedding, embedding_dim, source,
                ),
                timeout=self._timeout_s,
            )
        except Exception as exc:
            self._fail("alias write", exc)
            return False
        return True

    async def lookup_alias(self, kind: str, alias_norm: str) -> dict[str, Any] | None:
        """Resolve a normalised alias to its canonical entity, or None.

        None on miss, never a guess. A resolution this table proposes is still
        only a proposal — the caller must probe-verify it before any count
        derived from it is reported.
        """
        if not self.enabled:
            return None
        try:
            row = await asyncio.wait_for(
                self._pool.fetchrow(
                    """
                    SELECT kind, canonical, alias, alias_norm,
                           embedding, embedding_dim, source, updated_at
                      FROM entity_alias
                     WHERE kind = $1 AND alias_norm = $2
                    """,
                    kind, alias_norm,
                ),
                timeout=self._timeout_s,
            )
        except Exception as exc:
            self._fail("alias read", exc)
            return None
        return dict(row) if row is not None else None

    async def aliases_for(self, kind: str, canonical: str) -> list[dict[str, Any]]:
        """Every recorded surface form of one entity. Empty list on miss."""
        if not self.enabled:
            return []
        try:
            rows = await asyncio.wait_for(
                self._pool.fetch(
                    """
                    SELECT alias, alias_norm, source, updated_at
                      FROM entity_alias
                     WHERE kind = $1 AND canonical = $2
                     ORDER BY alias
                    """,
                    kind, canonical,
                ),
                timeout=self._timeout_s,
            )
        except Exception as exc:
            self._fail("alias scan", exc)
            return []
        return [dict(r) for r in rows]

    # -- schema ------------------------------------------------------------

    async def apply_migration(self, path: Path | None = None) -> bool:
        """Apply `migrations/001_init.sql`. Idempotent; returns True on success.

        Convenience for local development and tests. In production the
        migration is applied by docker-compose's init mount or by an operator —
        a service that migrates its own schema on boot races itself across N
        replicas.
        """
        if not self.enabled:
            return False
        sql_path = path or MIGRATION_PATH
        try:
            sql = sql_path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("could not read migration %s: %s", sql_path, exc)
            return False
        try:
            await asyncio.wait_for(self._pool.execute(sql), timeout=30.0)
        except Exception as exc:
            self._fail("migration", exc)
            return False
        return True

    # -- introspection -----------------------------------------------------

    def describe(self) -> dict[str, Any]:
        """Operational snapshot. Safe to expose on a health endpoint."""
        return {
            "configured": self.configured,
            "enabled": self.enabled,
            "degraded": self._degraded,
            "ttl_seconds": self.ttl_seconds,
            **self.stats.as_dict(),
        }


# --------------------------------------------------------------------------
# Module-level default
# --------------------------------------------------------------------------

_default: Store | None = None


def get_store() -> Store:
    """Process-wide default store. Inert unless ``DATABASE_URL`` is set."""
    global _default
    if _default is None:
        _default = Store()
    return _default


def reset_store() -> None:
    """Drop the module-level default. For tests."""
    global _default
    _default = None
