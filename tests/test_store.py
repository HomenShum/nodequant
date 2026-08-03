"""Tests for the optional Postgres persistence layer.

Two tiers, and the split is deliberate:

  * The **no-database** tier always runs. It is the tier that matters most,
    because the claim it defends is "this service still works with no database
    at all". A persistence layer that is only tested with a database attached
    has never tested its own optionality.

  * The **integration** tier needs a real Postgres and SKIPS, loudly, when there
    isn't one. It is not mocked. Mocking a database to prove upsert semantics
    and TTL predicates would only prove that the mock does what the mock was
    written to do — the behaviours under test here (ON CONFLICT convergence,
    `now() - interval` evaluated against the database clock, the CHECK
    constraints) live in Postgres, not in Python.

Start a database with::

    docker compose up -d --wait
    export TEST_DATABASE_URL=postgresql://trialscope:trialscope@localhost:5432/trialscope

Then re-run. Without it, `pytest -q` reports the integration tests as skipped
rather than passing — a skipped test says "unverified", which is true; a mocked
pass would say "verified", which would not be.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.ctgov import _BoundedCache
from app.store import (
    DEFAULT_TTL_SECONDS,
    MAX_TTL_SECONDS,
    MIN_TTL_SECONDS,
    CachedCount,
    Store,
    get_store,
    probe_key,
    reset_store,
)

# --------------------------------------------------------------------------
# Integration gate
# --------------------------------------------------------------------------

TEST_DSN = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


def _probe_database(dsn: str) -> str:
    """Return "" if the database is usable, else the reason it is not.

    Probed once at collection time rather than per test, so a missing database
    produces one clear skip reason instead of N connection timeouts.
    """
    if not dsn:
        return "TEST_DATABASE_URL is not set (run: docker compose up -d --wait)"
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        return "asyncpg is not installed (run: uv pip install asyncpg)"

    async def _check() -> str:
        store = Store(dsn)
        if not await store.connect():
            await store.close()
            return f"could not connect to {dsn.rsplit('@', 1)[-1]}"
        ok = await store.apply_migration()
        await store.close()
        return "" if ok else "migration could not be applied"

    try:
        return asyncio.run(_check())
    except Exception as exc:  # pragma: no cover - environment dependent
        return f"database probe failed: {exc}"


_SKIP_REASON = _probe_database(TEST_DSN)
needs_db = pytest.mark.skipif(bool(_SKIP_REASON), reason=f"no Postgres: {_SKIP_REASON}")


@pytest.fixture
async def store():
    """A connected store on a schema-migrated database, cleaned up after."""
    s = Store(TEST_DSN, ttl_seconds=DEFAULT_TTL_SECONDS)
    assert await s.connect()
    yield s
    await s.close()


def unique_params(**extra: str) -> dict[str, str]:
    """Filter params namespaced to one test.

    Every integration test writes under its own uuid so the suite is safe to run
    repeatedly against a database that already has rows, and safe to run in
    parallel against itself. A test that depends on an empty table is a test
    that fails the second time you run it.
    """
    return {
        "query.term": uuid.uuid4().hex,
        "countTotal": "true",
        "pageSize": "1",
        **extra,
    }


# ==========================================================================
# Tier 1 — no database. Always runs.
# ==========================================================================


class TestDisabledWithoutDatabaseURL:
    """The service must behave exactly as it does today with no DATABASE_URL.

    Persona: an engineer who cloned the repo, has no Docker, and ran uvicorn.
    Every one of these calls happens on their request path. None may raise, and
    none may return a value that could be mistaken for a measurement.
    """

    async def test_absent_env_means_not_configured(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        s = Store()
        assert s.configured is False
        assert s.enabled is False

    async def test_connect_returns_false_and_does_not_raise(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        s = Store()
        assert await s.connect() is False
        assert s.enabled is False

    async def test_every_operation_is_a_safe_noop(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        s = Store()
        await s.connect()

        # Reads return None — the miss sentinel — never 0 and never a default.
        assert await s.get_count({"query.term": "pembrolizumab"}) is None
        # Writes report that they did not store, rather than pretending.
        assert await s.put_count({"query.term": "pembrolizumab"}, 1742) is False
        assert await s.sweep_expired() == 0
        assert await s.apply_migration() is False
        assert await s.lookup_alias("intervention", "keytruda") is None
        assert await s.aliases_for("intervention", "Pembrolizumab") == []
        assert await s.upsert_alias(
            kind="intervention", canonical="Pembrolizumab",
            alias="Keytruda", alias_norm="keytruda",
        ) is False
        # Closing something never opened is fine, and idempotent.
        await s.close()
        await s.close()

    async def test_miss_is_none_not_zero(self, monkeypatch):
        """Zero is a real answer from this API ("no trials match").

        So a cache miss must be distinguishable from a measured zero. If a miss
        returned 0, a disabled cache would silently fabricate the measurement
        "no trials match" for every query in the system.
        """
        monkeypatch.delenv("DATABASE_URL", raising=False)
        s = Store()
        result = await s.get_count({"query.term": "x"})
        assert result is None
        assert result is not False
        assert result != 0

    async def test_context_manager_works_disabled(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        async with Store() as s:
            assert s.enabled is False
            assert await s.get_count({"a": "b"}) is None

    async def test_empty_dsn_is_treated_as_absent(self):
        s = Store("")
        assert s.configured is False
        assert await s.connect() is False

    async def test_unreachable_database_degrades_rather_than_raises(self):
        """Configured but down is the same no-op path as not configured.

        This is the failure mode that actually happens in production — Postgres
        restarts, the service does not. It must cost latency, never availability.
        """
        s = Store("postgresql://nobody:nobody@127.0.0.1:59999/nothing")
        assert s.configured is True
        assert await s.connect() is False
        assert s.enabled is False
        assert await s.get_count({"a": "b"}) is None
        assert await s.put_count({"a": "b"}, 1) is False
        await s.close()

    async def test_stats_are_observable_while_disabled(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        s = Store()
        await s.get_count({"a": "b"})
        await s.get_count({"a": "c"})
        info = s.describe()
        assert info["configured"] is False
        assert info["enabled"] is False
        assert info["misses"] == 2
        assert info["hits"] == 0

    async def test_module_default_is_inert(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        reset_store()
        try:
            assert get_store().enabled is False
            assert get_store() is get_store()
        finally:
            reset_store()


class TestKeyNormalisation:
    """Key derivation is a pure function; it needs no database to pin."""

    def test_order_independent(self):
        a = probe_key({"query.term": "melanoma", "filter.overallStatus": "RECRUITING"})
        b = probe_key({"filter.overallStatus": "RECRUITING", "query.term": "melanoma"})
        assert a == b

    def test_different_filters_do_not_collide(self):
        assert probe_key({"query.term": "melanoma"}) != probe_key({"query.term": "nsclc"})

    def test_matches_in_process_cache_key(self):
        """The L1 and L2 caches must agree on what "the same filter" means.

        app/store.py re-implements this key rather than importing
        _BoundedCache.key, so the dependency stays one-directional and the
        service can run with no database. This test is what stops the two
        copies drifting — if either changes, a filter cached in-process would
        miss in Postgres and the shared cache would quietly stop sharing.
        """
        for params in (
            {"query.term": "pembrolizumab", "countTotal": "true", "pageSize": "1"},
            {"filter.advanced": "AREA[Phase]PHASE3", "fields": "NCTId"},
            {"z": "1", "a": "2", "m": "3"},
            {"single": "value"},
        ):
            assert probe_key(params) == _BoundedCache.key(params)

    def test_key_is_stable_across_calls(self):
        params = {"b": "2", "a": "1"}
        assert probe_key(params) == probe_key(dict(reversed(list(params.items()))))


class TestTTLConfiguration:
    """TTL is a correctness bound, so its configuration is clamped, not trusted."""

    def test_default(self, monkeypatch):
        monkeypatch.delenv("PROBE_CACHE_TTL_SECONDS", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert Store().ttl_seconds == DEFAULT_TTL_SECONDS

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("PROBE_CACHE_TTL_SECONDS", "300")
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert Store().ttl_seconds == 300

    def test_absurd_ttl_is_clamped_not_honoured(self, monkeypatch):
        """A one-year TTL is not a cache, it is a stale dataset."""
        monkeypatch.setenv("PROBE_CACHE_TTL_SECONDS", str(365 * 24 * 3600))
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert Store().ttl_seconds == MAX_TTL_SECONDS

    def test_zero_and_negative_are_clamped_to_the_safe_side(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("PROBE_CACHE_TTL_SECONDS", "0")
        assert Store().ttl_seconds == MIN_TTL_SECONDS
        monkeypatch.setenv("PROBE_CACHE_TTL_SECONDS", "-99")
        assert Store().ttl_seconds == MIN_TTL_SECONDS

    def test_garbage_ttl_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("PROBE_CACHE_TTL_SECONDS", "soon")
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert Store().ttl_seconds == DEFAULT_TTL_SECONDS


class TestCachedCountShape:
    """A served count must be able to explain itself without a database."""

    def test_provenance_names_the_measurement(self):
        c = CachedCount(
            total_count=1742,
            params={"query.term": "pembrolizumab", "countTotal": "true"},
            measured_at=datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
            source="clinicaltrials.gov/api/v2",
            age_seconds=90.0,
        )
        text = c.provenance
        assert "1742" in text
        assert "clinicaltrials.gov/api/v2" in text
        # Replayable: the exact request is recoverable from the served value.
        assert "countTotal=true" in text and "query.term=pembrolizumab" in text


# ==========================================================================
# Tier 2 — integration. Requires a real Postgres.
# ==========================================================================


@needs_db
class TestRoundTrip:
    async def test_count_round_trips_with_params_intact(self, store):
        params = unique_params(**{"filter.overallStatus": "RECRUITING"})
        assert await store.put_count(params, 1742) is True

        hit = await store.get_count(params)
        assert hit is not None
        assert hit.total_count == 1742
        # The exact request is preserved, so the value stays replayable.
        assert hit.params == params
        assert hit.source == "clinicaltrials.gov/api/v2"
        assert hit.age_seconds < 60
        assert hit.measured_at.tzinfo is not None
        assert store.stats.hits == 1

    async def test_measured_zero_round_trips_as_zero(self, store):
        """Zero is a measurement, not an absence. It must survive the cache."""
        params = unique_params()
        assert await store.put_count(params, 0) is True
        hit = await store.get_count(params)
        assert hit is not None
        assert hit.total_count == 0

    async def test_miss_returns_none_never_a_default(self, store):
        assert await store.get_count(unique_params()) is None
        assert store.stats.misses >= 1

    async def test_source_is_recorded_per_row(self, store):
        params = unique_params()
        await store.put_count(params, 12, source="other-registry/v1")
        hit = await store.get_count(params)
        assert hit is not None and hit.source == "other-registry/v1"

    async def test_rewrite_updates_the_count_and_the_measurement_time(self, store):
        params = unique_params()
        old = datetime.now(timezone.utc) - timedelta(hours=6)
        await store.put_count(params, 100, measured_at=old)
        await store.put_count(params, 200)

        hit = await store.get_count(params)
        assert hit is not None
        assert hit.total_count == 200
        # A new measurement resets the age. The count and its timestamp move
        # together or the pair is a lie.
        assert hit.age_seconds < 60


@needs_db
class TestTTLIsEnforcedAsAMiss:
    async def test_count_past_ttl_is_a_miss(self, store):
        """The load-bearing test for the whole module.

        A count measured 48 hours ago is not a measurement of today's corpus.
        Past its TTL it must be invisible — not returned with a warning, not
        returned with a flag the caller might ignore. A MISS.
        """
        params = unique_params()
        stale = datetime.now(timezone.utc) - timedelta(hours=48)
        assert await store.put_count(params, 1742, measured_at=stale) is True

        assert await store.get_count(params) is None
        assert store.stats.expired == 1
        assert store.stats.hits == 0

    async def test_count_inside_ttl_is_a_hit(self, store):
        """The other half: the TTL must not reject fresh measurements either."""
        params = unique_params()
        recent = datetime.now(timezone.utc) - timedelta(hours=2)
        await store.put_count(params, 555, measured_at=recent)

        hit = await store.get_count(params)
        assert hit is not None and hit.total_count == 555
        assert 3600 < hit.age_seconds < 3 * 3600

    async def test_boundary_just_inside_and_just_outside(self):
        """Checked at the edge, because an off-by-one in the TTL predicate is
        exactly the bug that serves yesterday's counts as today's."""
        s = Store(TEST_DSN, ttl_seconds=3600)
        assert await s.connect()
        try:
            fresh, stale = unique_params(), unique_params()
            now = datetime.now(timezone.utc)
            await s.put_count(fresh, 1, measured_at=now - timedelta(seconds=3500))
            await s.put_count(stale, 2, measured_at=now - timedelta(seconds=3700))

            assert (await s.get_count(fresh)) is not None
            assert (await s.get_count(stale)) is None
        finally:
            await s.close()

    async def test_a_short_ttl_expires_a_row_the_long_ttl_would_serve(self):
        """Same row, two readers, different TTLs — freshness is the reader's
        bound, not a property baked into the row at write time."""
        params = unique_params()
        writer = Store(TEST_DSN, ttl_seconds=DEFAULT_TTL_SECONDS)
        assert await writer.connect()
        try:
            await writer.put_count(
                params, 42, measured_at=datetime.now(timezone.utc) - timedelta(hours=3)
            )
            assert (await writer.get_count(params)) is not None
        finally:
            await writer.close()

        strict = Store(TEST_DSN, ttl_seconds=60)
        assert await strict.connect()
        try:
            assert (await strict.get_count(params)) is None
            assert strict.stats.expired == 1
        finally:
            await strict.close()

    async def test_expired_rows_are_swept(self, store):
        """BOUND: a table keyed on user-controlled filters needs eviction."""
        params = unique_params()
        await store.put_count(
            params, 7, measured_at=datetime.now(timezone.utc) - timedelta(days=3)
        )
        removed = await store.sweep_expired(limit=10_000)
        assert removed >= 1
        assert await store.get_count(params) is None


@needs_db
class TestKeyNormalisationHitsOneRow:
    async def test_same_filter_built_two_ways_hits_the_same_row(self, store):
        """Order-independence has to hold in the database, not just in the key
        function — two workers building the same filter in different orders
        must not pay two upstream probes for one number."""
        term = uuid.uuid4().hex
        first = {"query.term": term, "filter.overallStatus": "RECRUITING", "pageSize": "1"}
        second = {"pageSize": "1", "query.term": term, "filter.overallStatus": "RECRUITING"}

        assert await store.put_count(first, 314) is True
        hit = await store.get_count(second)
        assert hit is not None
        assert hit.total_count == 314

        # And it is genuinely one row, not two that happen to agree.
        rows = await store._pool.fetchval(
            "SELECT count(*) FROM probe_cache WHERE params->>'query.term' = $1", term
        )
        assert rows == 1

    async def test_a_different_filter_does_not_alias_onto_it(self, store):
        base = unique_params()
        other = {**base, "filter.overallStatus": "COMPLETED"}
        await store.put_count(base, 10)
        assert await store.get_count(other) is None


@needs_db
class TestConcurrentWrites:
    async def test_concurrent_writes_of_one_key_converge_on_one_row(self, store):
        """Burst scenario: N workers probe the same popular filter at once.

        This is not hypothetical — it is what happens when the service is
        scaled out and two users ask about pembrolizumab in the same second.
        Upsert semantics must make that converge, not raise and not duplicate.
        """
        params = unique_params()
        results = await asyncio.gather(
            *(store.put_count(params, 1000 + i) for i in range(32)),
            return_exceptions=True,
        )
        assert all(r is True for r in results), [r for r in results if r is not True]

        rows = await store._pool.fetchval(
            "SELECT count(*) FROM probe_cache WHERE cache_key = $1", probe_key(params)
        )
        assert rows == 1

        hit = await store.get_count(params)
        assert hit is not None
        # Last writer wins, and every writer wrote a number it measured, so any
        # of them is a legitimate answer — but it must be one of them, not a sum
        # or a partial write.
        assert hit.total_count in range(1000, 1032)

    async def test_concurrent_writes_from_independent_pools(self):
        """Same key, separate connection pools — the multi-instance case the
        in-process cache cannot cover at all."""
        params = unique_params()
        stores = [Store(TEST_DSN) for _ in range(4)]
        assert all(await asyncio.gather(*(s.connect() for s in stores)))
        try:
            results = await asyncio.gather(
                *(s.put_count(params, 500 + i) for i, s in enumerate(stores) for _ in range(8)),
                return_exceptions=True,
            )
            assert all(r is True for r in results), [r for r in results if r is not True]

            hit = await stores[0].get_count(params)
            assert hit is not None
            assert hit.total_count in range(500, 504)
            assert hit.params == params  # params survived every concurrent rewrite
        finally:
            await asyncio.gather(*(s.close() for s in stores))

    async def test_concurrent_reads_and_writes_never_yield_a_torn_value(self, store):
        """Sustained mixed load: readers must only ever see a complete row."""
        params = unique_params()
        await store.put_count(params, 1)

        async def reader() -> list[CachedCount | None]:
            return [await store.get_count(params) for _ in range(20)]

        async def writer() -> None:
            for i in range(20):
                await store.put_count(params, 2000 + i)

        reads, *_ = await asyncio.gather(reader(), writer(), reader(), writer())
        for hit in reads:
            if hit is None:
                continue
            assert hit.total_count == 1 or hit.total_count in range(2000, 2020)
            assert hit.params == params


@needs_db
class TestWriteGuards:
    """The write path is where a non-measured number would have to enter."""

    async def test_refuses_a_negative_count(self, store):
        assert await store.put_count(unique_params(), -1) is False

    async def test_refuses_a_non_integer_count(self, store):
        assert await store.put_count(unique_params(), 3.5) is False  # type: ignore[arg-type]
        assert await store.put_count(unique_params(), "1742") is False  # type: ignore[arg-type]
        assert await store.put_count(unique_params(), True) is False  # type: ignore[arg-type]

    async def test_refuses_a_count_with_no_params(self, store):
        """A count whose request is unknown cannot be replayed, so it cannot be
        vouched for, so it does not get stored."""
        assert await store.put_count({}, 100) is False


@needs_db
class TestEntityAliasTable:
    """The shape app/entities.py can build on. Not wired to it here."""

    async def test_alias_round_trips(self, store):
        canonical = f"Pembrolizumab-{uuid.uuid4().hex[:8]}"
        norm = f"keytruda{uuid.uuid4().hex[:8]}"
        assert await store.upsert_alias(
            kind="intervention", canonical=canonical, alias="Keytruda",
            alias_norm=norm, source="probe-verified",
        ) is True

        row = await store.lookup_alias("intervention", norm)
        assert row is not None
        assert row["canonical"] == canonical
        assert row["alias"] == "Keytruda"
        assert row["source"] == "probe-verified"
        assert row["embedding"] is None  # nullable, as specified

    async def test_alias_miss_returns_none(self, store):
        assert await store.lookup_alias("intervention", uuid.uuid4().hex) is None

    async def test_embedding_bytes_round_trip(self, store):
        import struct
        vector = struct.pack("<4f", 0.1, 0.2, 0.3, 0.4)
        norm = f"msd{uuid.uuid4().hex[:8]}"
        assert await store.upsert_alias(
            kind="sponsor", canonical="Merck Sharp & Dohme LLC", alias="MSD",
            alias_norm=norm, embedding=vector, embedding_dim=4,
        ) is True
        row = await store.lookup_alias("sponsor", norm)
        assert row is not None
        assert bytes(row["embedding"]) == vector
        assert row["embedding_dim"] == 4

    async def test_embedding_without_dim_is_refused(self, store):
        assert await store.upsert_alias(
            kind="sponsor", canonical="X", alias="X",
            alias_norm=uuid.uuid4().hex, embedding=b"\x00\x00\x00\x00",
        ) is False

    async def test_mismatched_embedding_length_is_refused(self, store):
        assert await store.upsert_alias(
            kind="sponsor", canonical="X", alias="X",
            alias_norm=uuid.uuid4().hex, embedding=b"\x00" * 4, embedding_dim=99,
        ) is False

    async def test_aliases_for_collects_every_surface_form(self, store):
        canonical = f"Merck-{uuid.uuid4().hex[:8]}"
        for alias in ("Merck & Co.", "MSD", "Merck Sharp & Dohme LLC"):
            assert await store.upsert_alias(
                kind="sponsor", canonical=canonical, alias=alias,
                alias_norm=f"{alias.lower()}-{uuid.uuid4().hex[:6]}",
            ) is True
        rows = await store.aliases_for("sponsor", canonical)
        assert {r["alias"] for r in rows} == {"Merck & Co.", "MSD", "Merck Sharp & Dohme LLC"}

    async def test_one_normalised_alias_resolves_to_one_entity(self, store):
        """Re-registering an alias moves it rather than duplicating it: a
        surface form that resolves to two entities is an unresolvable index."""
        norm = uuid.uuid4().hex
        await store.upsert_alias(
            kind="intervention", canonical="A", alias="a", alias_norm=norm)
        await store.upsert_alias(
            kind="intervention", canonical="B", alias="b", alias_norm=norm)
        rows = await store._pool.fetchval(
            "SELECT count(*) FROM entity_alias WHERE alias_norm = $1", norm)
        assert rows == 1
        row = await store.lookup_alias("intervention", norm)
        assert row is not None and row["canonical"] == "B"

    async def test_alias_table_cannot_hold_a_count(self, store):
        """Structural: there is no column for a trial count in entity_alias.

        The absence is the enforcement. If someone adds one, an unverified
        number gains a place to live and this test fails deliberately.
        """
        columns = await store._pool.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'entity_alias'"
        )
        names = {r["column_name"] for r in columns}
        assert not (names & {"count", "total_count", "trial_count", "n"})


@needs_db
class TestMigrationIsIdempotent:
    async def test_reapplying_the_migration_is_a_noop(self, store):
        params = unique_params()
        await store.put_count(params, 99)
        assert await store.apply_migration() is True
        assert await store.apply_migration() is True
        # Data survives: the migration creates, it does not drop.
        hit = await store.get_count(params)
        assert hit is not None and hit.total_count == 99
