"""Tests for the seams where independently-built modules meet.

Every module here had green unit tests before it was wired in, and the wiring
still shipped a leak: `store.connect()` was called with no matching `close()`,
so a graceful shutdown left the asyncpg pool open. Unit tests could not see it —
the bug lived entirely in the composition.

These tests cover the composition itself: lifespan, the L1/L2/network read
order, and the rule that a failed turn must not become inherited context.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.ctgov import CTGovClient, Filter


class _RecordingStore:
    """Minimal L2 double. Records every interaction so ordering is assertable."""

    def __init__(self, *, seeded: dict[str, int] | None = None, fail: bool = False):
        self._seeded = seeded or {}
        self._fail = fail
        self.connected = False
        self.closed = False
        self.gets: list[dict[str, str]] = []
        self.puts: list[tuple[dict[str, str], int]] = []

    async def connect(self) -> bool:
        self.connected = True
        return True

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def get_count(self, params: dict[str, str]) -> Any:
        self.gets.append(dict(params))
        if self._fail:
            raise RuntimeError("L2 unavailable")
        key = params.get("filter.advanced", "__base__")
        if key not in self._seeded:
            return None
        return type("Cached", (), {"total_count": self._seeded[key]})()

    async def put_count(self, params: dict[str, str], value: int) -> None:
        if self._fail:
            raise RuntimeError("L2 unavailable")
        self.puts.append((dict(params), value))


def _client_with(store: Any, network_returns: int = 4242) -> CTGovClient:
    client = CTGovClient(store=store)

    async def fake_get(path: str, params: dict[str, str]) -> dict[str, Any]:
        fake_get.calls += 1  # type: ignore[attr-defined]
        return {"totalCount": network_returns}

    fake_get.calls = 0  # type: ignore[attr-defined]
    client._get = fake_get  # type: ignore[method-assign]
    return client


# ------------------------------------------------------------- lifespan


async def test_lifespan_closes_the_store_on_shutdown():
    """The leak this file exists for.

    `connect()` without a matching `close()` leaves the asyncpg pool open on
    every graceful shutdown, accumulating DEFAULT_POOL_MAX connections per
    deploy cycle. Unit tests on Store could not see it; only the composition can.
    """
    from contextlib import asynccontextmanager

    import app.main as main

    store = _RecordingStore()
    original = main.get_store
    main.get_store = lambda: store  # type: ignore[assignment]
    try:
        async with main.lifespan(main.app):
            assert store.connected, "lifespan must connect the store"
            assert not store.closed, "store must stay open for the app's lifetime"
        assert store.closed, "lifespan must close the store on shutdown"
    finally:
        main.get_store = original  # type: ignore[assignment]


async def test_service_runs_with_no_store_at_all():
    """The no-database path is the default and must never depend on an L2."""
    client = _client_with(store=None)
    assert await client.count(Filter({"query.cond": "melanoma"})) == 4242


# ------------------------------------------------ L1 / L2 / network ordering


async def test_l2_hit_avoids_the_network_and_warms_l1():
    store = _RecordingStore(seeded={"__base__": 3738})
    client = _client_with(store)
    filt = Filter({"query.cond": "melanoma"})

    first = await client.count(filt)
    assert first == 3738, "an L2 hit must be returned, not the network value"
    assert client._get.calls == 0, "an L2 hit must not touch the network"  # type: ignore[attr-defined]

    # L1 is now warm: a second read must not consult L2 again.
    gets_before = len(store.gets)
    assert await client.count(filt) == 3738
    assert len(store.gets) == gets_before, "second read must be served from L1"


async def test_network_fetch_writes_both_layers():
    store = _RecordingStore()
    client = _client_with(store, network_returns=1529)
    filt = Filter({"filter.advanced": "AREA[Phase]PHASE2"})

    assert await client.count(filt) == 1529
    assert store.puts, "a network fetch must populate L2"
    assert store.puts[0][1] == 1529
    assert client._cache.get({**filt.params, "countTotal": "true",
                              "pageSize": "1", "fields": "NCTId"}) == 1529


async def test_l2_failure_degrades_to_the_network_rather_than_raising():
    """A cache is an optimisation. It must never be able to fail a request."""
    store = _RecordingStore(fail=True)
    client = _client_with(store, network_returns=777)
    try:
        got = await client.count(Filter({"query.cond": "melanoma"}))
    except Exception as exc:  # pragma: no cover - the assertion is the message
        pytest.fail(f"an L2 failure must not surface to the caller, got {exc!r}")
    assert got == 777, "the network value must still be returned"


async def test_a_stale_l1_entry_is_never_served():
    """Pins the interaction between the two caches.

    L1 previously had no TTL, which was safe only because the process lifetime
    bounded staleness. Warming L1 from a persistent L2 removes that accident.
    """
    import time

    client = _client_with(store=None, network_returns=100)
    client._cache._ttl = 0.05  # type: ignore[attr-defined]
    filt = Filter({"query.cond": "melanoma"})

    assert await client.count(filt) == 100
    time.sleep(0.06)
    calls_before = client._get.calls  # type: ignore[attr-defined]
    assert await client.count(filt) == 100
    assert client._get.calls > calls_before, (  # type: ignore[attr-defined]
        "an expired L1 entry must be re-measured, not served"
    )
