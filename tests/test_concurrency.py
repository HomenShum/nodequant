"""Concurrency audit: state shared between requests, and what interleaving costs.

Every test here asserts on a RESULT or a COUNT, never on "nothing raised".
Three of them reproduce defects; the rest prove a shared structure is safe with
the measurement that shows it.

A note on method that matters more than any single test here:

    `httpx.MockTransport` with a synchronous handler NEVER SUSPENDS. Under it,
    eight concurrent `count()` calls produced ONE upstream request and seven
    cache hits — the read-then-write race looked fixed. It is not fixed; the
    mock serialised the coroutines. The same probe against a transport with a
    real `await asyncio.sleep()` produced EIGHT upstream requests and zero
    cache hits.

    A concurrency test built on a transport that cannot yield is a test that
    cannot observe a race. `_SlowTransport` below exists for that reason and
    every overlap test uses it.
"""

from __future__ import annotations

import asyncio
import json
import threading

import httpx
import pytest

from app.ctgov import (
    MAX_CONCURRENT_PROBES,
    MIN_REQUEST_INTERVAL_S,
    THROTTLE_COOLDOWN_S,
    CTGovClient,
    CTGovError,
    Filter,
)
from app.memory import MemoryStore, resolve_followup
from app.models import VizType
from app.planner import Dimension, PlanFilters, QueryPlan

# --------------------------------------------------------------------------
# A transport that actually yields
# --------------------------------------------------------------------------


class _SlowTransport(httpx.AsyncBaseTransport):
    """Async transport with a real suspension point.

    Records every request, tracks true concurrency, and lets a responder decide
    each reply so a mid-flight failure can be injected deterministically.
    """

    def __init__(self, latency: float = 0.02, responder=None):
        self.latency = latency
        self.calls: list[str] = []
        self.start_times: list[float] = []
        self.inflight = 0
        self.max_inflight = 0
        self._responder = responder

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        loop = asyncio.get_running_loop()
        self.calls.append(str(request.url))
        self.start_times.append(loop.time())
        n = len(self.calls)
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            await asyncio.sleep(self.latency)
            if self._responder is not None:
                return self._responder(request, n)
            return httpx.Response(200, json={"totalCount": 4242, "studies": []})
        finally:
            self.inflight -= 1


def _client(transport: _SlowTransport) -> CTGovClient:
    return CTGovClient(client=httpx.AsyncClient(transport=transport))


def _plan(drug: str | None = None, condition: str | None = None,
          dimension: Dimension = Dimension.PHASE) -> QueryPlan:
    return QueryPlan(
        title="t", interpretation="i",
        filters=PlanFilters(drug_name=drug, condition=condition),
        dimension=dimension, viz_type=VizType.BAR_CHART,
    )


# ==========================================================================
# 1. _BoundedCache read-then-write race
# ==========================================================================


async def test_concurrent_identical_queries_are_coalesced_into_one_request() -> None:
    """N coroutines miss on the same key, all fetch, all write.

    The RESULT is correct — every caller gets the same measured number and the
    cache ends with one entry holding that number, because the racing writes
    are identical. What is lost is work: eight identical questions cost eight
    upstream probes against a rate limit every request shares.

    If this ever fails with `upstream == 1`, single-flight was added; update
    docs/SCALABILITY_AUDIT.md rather than deleting the assertion.
    """
    n = 8
    transport = _SlowTransport(latency=0.05)
    async with _client(transport) as client:
        filt = Filter({"query.term": "pembrolizumab"})
        results = await asyncio.gather(*(client.count(filt) for _ in range(n)))

    print(f"\n  [L1 race] {n} concurrent identical count() calls")
    print(f"    upstream requests : {len(transport.calls)}  (ideal 1)")
    print(f"    cache             : {client.cache_stats}")
    print(f"    redundant probes  : {len(transport.calls) - 1} "
          f"({(len(transport.calls) - 1) / n * 100:.0f}% wasted)")
    print(f"    rate-budget cost  : {(len(transport.calls) - 1) * MIN_REQUEST_INTERVAL_S:.2f}s "
          f"of shared pacing spent on duplicates")

    # Correctness: every caller got the same measured number.
    assert set(results) == {4242}
    # And the cache converged on that number rather than on a torn value.
    assert client.cache_stats["entries"] == 1
    # The finding: no in-flight deduplication.
    # Inverted from the reproduction. Before single-flight, 8 concurrent
    # identical queries cost 8 upstream probes -- 7 wasted (88%) and 1.26s of a
    # rate budget every request shares. Waiting on an answer already in flight
    # is strictly cheaper than asking the same question again.
    assert len(transport.calls) == 1, (
        f"N identical concurrent queries must coalesce to ONE probe, got "
        f"{len(transport.calls)}"
    )

    # The warm path is correct: a subsequent call costs nothing.
    async with _client(transport):
        pass
    before = len(transport.calls)
    async with client:
        again = await client.count(Filter({"query.term": "pembrolizumab"}))
    assert again == 4242
    assert len(transport.calls) == before, "warm cache serves without a probe"


async def test_sequential_identical_queries_cost_exactly_one_probe() -> None:
    """The same key stream with no overlap: the cache does its job perfectly.

    Stated so the finding above is read as a race, not as a broken cache.
    """
    transport = _SlowTransport(latency=0.0)
    async with _client(transport) as client:
        filt = Filter({"query.term": "melanoma"})
        for _ in range(8):
            assert await client.count(filt) == 4242
    print(f"\n  [L1 sequential] 8 identical count() calls -> "
          f"upstream={len(transport.calls)} cache={client.cache_stats}")
    assert len(transport.calls) == 1
    assert client.cache_stats["hits"] == 7


# ==========================================================================
# 2. Is the shared rate limiter actually shared?
# ==========================================================================


async def test_cooldown_does_not_reach_requests_that_already_reserved_a_slot() -> None:
    """THE HOLE. `_pace()` computes its wait under the lock, then sleeps OUTSIDE it.

        async with self._pace_lock:
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + MIN_REQUEST_INTERVAL_S
        if wait > 0:
            await asyncio.sleep(wait)          # <-- captured BEFORE any cooldown

    `_apply_cooldown` pushes `_next_slot` forward, which correctly delays every
    request that has not yet entered `_pace`. It cannot delay a request that is
    already sleeping on a wait computed from the pre-cooldown schedule.

    Measured directly: six requests reserve slots, a cooldown is then applied,
    and we count how many are released inside the cooldown window anyway.
    """
    transport = _SlowTransport(latency=0.0)
    async with _client(transport) as client:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        released: list[float] = []

        async def paced() -> None:
            await client._pace()
            released.append(loop.time() - t0)

        tasks = [asyncio.create_task(paced()) for _ in range(6)]
        await asyncio.sleep(0.01)          # let all six reserve their slots
        applied_at = loop.time() - t0
        await client._apply_cooldown(THROTTLE_COOLDOWN_S)
        await asyncio.gather(*tasks)

    window_end = applied_at + THROTTLE_COOLDOWN_S
    escaped = [r for r in released if applied_at <= r < window_end]
    print(f"\n  [cooldown hole] cooldown applied at t={applied_at:.3f}s, "
          f"window ends t={window_end:.3f}s")
    print(f"    _pace() releases: {[round(r, 3) for r in sorted(released)]}")
    print(f"    RELEASED INSIDE THE COOLDOWN WINDOW: {len(escaped)} of 6")

    assert escaped, (
        "expected the documented hole: requests that reserved a slot before the "
        "cooldown ignore it"
    )
    # Every reservation made before the cooldown escapes it.
    assert len(escaped) >= 4


async def test_cooldown_escapees_on_the_real_path_are_bounded_by_the_semaphore() -> None:
    """How bad is the hole in practice? Bounded, and the bound is measurable.

    `_pace()` is only reached while holding the concurrency semaphore, so at
    most `MAX_CONCURRENT_PROBES - 1` other requests can have a live reservation
    when a 429 arrives. That is the blast radius: 2, not the whole fan-out.
    """
    seen: dict[str, float] = {}
    loop_holder: dict[str, object] = {}

    def responder(request: httpx.Request, n: int) -> httpx.Response:
        if n == 1:
            seen["429_at"] = asyncio.get_running_loop().time()
            return httpx.Response(429, json={})
        return httpx.Response(200, json={"totalCount": 1, "studies": []})

    transport = _SlowTransport(latency=0.02, responder=responder)
    async with _client(transport) as client:
        loop_holder["loop"] = asyncio.get_running_loop()
        await client.count_many(
            {f"b{i}": Filter({"query.term": f"t{i}"}) for i in range(8)}
        )

    cooldown_at = seen["429_at"]
    starts = transport.start_times
    escaped = [s for s in starts[1:] if cooldown_at <= s < cooldown_at + THROTTLE_COOLDOWN_S]
    offsets = [round(s - starts[0], 3) for s in starts]
    print(f"\n  [cooldown blast radius] 8-probe fan-out, first probe 429s")
    print(f"    request start offsets: {offsets}")
    print(f"    started during the {THROTTLE_COOLDOWN_S}s cooldown: {len(escaped)}")
    print(f"    MAX_CONCURRENT_PROBES = {MAX_CONCURRENT_PROBES} -> "
          f"bound is {MAX_CONCURRENT_PROBES - 1}")

    assert escaped, "the hole is reachable on the real request path"
    assert len(escaped) <= MAX_CONCURRENT_PROBES - 1, (
        "escapees must be bounded by the semaphore, not by the fan-out width"
    )
    # And the rest DID respect it: the cooldown is genuinely shared for them.
    late = [s for s in starts[1:] if s >= cooldown_at + THROTTLE_COOLDOWN_S]
    assert len(late) >= 5, "the cooldown delayed the majority of the fan-out"


async def test_pacing_is_shared_across_concurrent_callers() -> None:
    """The positive result: `_next_slot` really is one schedule, not one per task."""
    transport = _SlowTransport(latency=0.0)
    async with _client(transport) as client:
        await client.count_many(
            {f"b{i}": Filter({"query.term": f"t{i}"}) for i in range(6)}
        )
    starts = transport.start_times
    gaps = [round(b - a, 3) for a, b in zip(starts, starts[1:])]
    print(f"\n  [shared pacing] inter-request gaps: {gaps} "
          f"(MIN_REQUEST_INTERVAL_S={MIN_REQUEST_INTERVAL_S})")
    assert transport.max_inflight <= MAX_CONCURRENT_PROBES
    assert min(gaps) >= MIN_REQUEST_INTERVAL_S * 0.9, (
        "one shared schedule; per-task schedules would let requests bunch"
    )


# ==========================================================================
# 3. MemoryStore — REAL BUG: /api/chat keys every caller on the string "default"
# ==========================================================================


def test_memory_store_isolates_distinct_session_ids() -> None:
    """`MemoryStore` itself is correct. Establish that before blaming it."""
    store = MemoryStore()
    alice = store.for_session("alice")
    bob = store.for_session("bob")
    alice.record("pembrolizumab by phase", _plan(drug="Pembrolizumab"))
    bob.record("melanoma by country", _plan(condition="Melanoma",
                                            dimension=Dimension.COUNTRY))

    resolved = resolve_followup("what about phase 3?", alice)
    assert resolved is not None
    print(f"\n  [session isolation] alice follow-up -> "
          f"drug={resolved.drug_name} condition={resolved.condition}")
    assert resolved.drug_name == "Pembrolizumab"
    assert resolved.condition is None
    assert alice is not bob


def test_shared_default_session_leaks_one_callers_filters_into_anothers_followup() -> None:
    """REAL CORRECTNESS BUG, at the memory layer.

    `app/main.py` calls `app.state.memory.for_session("default")` on every
    `/api/chat` request, so `MemoryStore`'s isolation is bypassed and every
    concurrent caller writes into one history.

    Alice asks about Pembrolizumab. Bob asks about Melanoma. Alice's follow-up
    inherits BOB's subject and dimension. The count that comes back is honestly
    measured — for a question nobody asked. That is precisely the harm
    `app/memory.py`'s own header says the module exists to prevent.
    """
    store = MemoryStore()
    alice = store.for_session("default")
    bob = store.for_session("default")
    assert alice is bob, "every caller shares one ConversationMemory"

    alice.record("Show pembrolizumab trials by phase", _plan(drug="Pembrolizumab"),
                 labels=["PHASE1", "PHASE2"])
    bob.record("Show melanoma trials by country",
               _plan(condition="Melanoma", dimension=Dimension.COUNTRY),
               labels=["United States", "China"])

    leaked = resolve_followup("what about phase 3?", alice)
    assert leaked is not None
    print("\n  [shared-session leak] alice asked about Pembrolizumab; "
          "her follow-up resolved to:")
    print(f"    query     = {leaked.query!r}")
    print(f"    drug_name = {leaked.drug_name!r}   <- Alice's subject, LOST")
    print(f"    condition = {leaked.condition!r}   <- Bob's subject, INHERITED")

    assert leaked.drug_name is None, "Alice's own filter was dropped"
    assert leaked.condition == "Melanoma", "Bob's filter was inherited"


def test_sessions_are_isolated_end_to_end_over_api_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same defect through the real HTTP surface, with a mocked upstream.

    Two interleaved 'users' hit POST /api/chat. The third request is a
    follow-up belonging to the first user; the trace panel shows which filters
    were actually applied.

    Pacing is disabled here: rate limiting is measured in its own tests, and
    leaving it on costs ~18s of real sleeping for ~60 mocked probes.
    """
    from fastapi.testclient import TestClient

    import app.ctgov as ctgov_mod
    monkeypatch.setattr(ctgov_mod, "MIN_REQUEST_INTERVAL_S", 0.0)

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)  # force the no-LLM path
    monkeypatch.setenv("DATABASE_URL", "")               # no L2

    from app.main import app

    # The planner is stubbed rather than run, because this test is about session
    # KEYING, not about extraction. The shipped keyword fallback resolves only a
    # dimension and returns drug_name=None, so asserting on an extracted drug
    # name needs a live LLM -- which is exactly why this test used to pass on a
    # machine with a .env and fail on a fresh clone. A fixed mapping keeps the
    # isolation property under test and takes the network out of it.
    import app.main as main_mod
    from app.models import TrialPhase, VizType
    from app.planner import Dimension, PlanFilters, QueryPlan

    def stub_plan(request):
        text = request.query.lower()
        if "pembrolizumab" in text:
            return QueryPlan(
                dimension=Dimension.PHASE, viz_type=VizType.BAR_CHART,
                title="pembrolizumab trials by phase", interpretation="stubbed plan",
                filters=PlanFilters(drug_name="Pembrolizumab"),
            )
        if "melanoma" in text:
            return QueryPlan(
                dimension=Dimension.COUNTRY, viz_type=VizType.BAR_CHART,
                title="melanoma trials by country", interpretation="stubbed plan",
                filters=PlanFilters(condition="melanoma"),
            )
        # The follow-up names a phase and no drug on purpose: the drug can only
        # come from Alice's own memory, which is the property under test.
        return QueryPlan(
            dimension=Dimension.PHASE, viz_type=VizType.BAR_CHART,
            title="phase 3", interpretation="stubbed plan",
            filters=PlanFilters(phase=[TrialPhase.PHASE3]),
        )

    monkeypatch.setattr(main_mod, "fallback_plan", stub_plan)

    def handler(request: httpx.Request) -> httpx.Response:
        if "countTotal" in str(request.url):
            return httpx.Response(200, json={"totalCount": 123, "studies": []})
        return httpx.Response(200, json={"studies": [{
            "protocolSection": {
                "identificationModule": {"nctId": "NCT00000001",
                                         "briefTitle": "A study"},
                "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Acme"}},
                "designModule": {"phases": ["PHASE2"]},
                "statusModule": {"overallStatus": "RECRUITING"},
            }}]})

    def ask(http, text: str, session: str) -> dict:
        raw = http.post("/api/chat",
                        json={"id": session,
                              "messages": [{"role": "user", "content": text}]}).text
        frames = [json.loads(line[6:]) for line in raw.splitlines()
                  if line.startswith("data: ")]
        trace = next(f for f in frames if f.get("type") == "data-trace")
        return trace["data"]["filters_applied"]

    with TestClient(app) as http:
        app.state.ctgov = CTGovClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        alice_1 = ask(http, "Show pembrolizumab trials by phase", "alice")
        bob_1 = ask(http, "Show melanoma trials by country", "bob")
        alice_2 = ask(http, "what about phase 3?", "alice")

    print("\n  [end-to-end /api/chat] filters_applied per request")
    print(f"    alice #1 : {alice_1}")
    print(f"    bob   #1 : {bob_1}")
    print(f"    alice #2 : {alice_2}   <- should still be Pembrolizumab")

    assert alice_1 == {"query.intr": "Pembrolizumab"}
    assert bob_1 == {"query.cond": "melanoma"}
    # The bug, through the documented transport:
    # Inverted from the reproduction. Every caller used to share the literal
    # session "default", so Alice's follow-up inherited Bob's filters and
    # returned an honestly-measured answer to a question nobody asked -- passing
    # citations, trace and reconciliation unchanged. The transport already sent a
    # conversation id; main.py received and discarded it.
    assert alice_2.get("query.intr") == "Pembrolizumab", (
        "Alice's follow-up must inherit HER drug filter"
    )
    assert "query.cond" not in alice_2 or alice_2.get("query.cond") != "melanoma", (
        "Bob's condition must not leak into Alice's session"
    )


def test_concurrent_writers_to_one_session_lose_no_turns_and_respect_bounds() -> None:
    """Given they DO share a session, is the sharing at least thread-safe?

    Yes: `ConversationMemory` guards append+evict with one lock, so eight
    threads writing simultaneously leave a consistent, in-bounds history. The
    defect above is a keying bug, not a data race — worth separating, because
    the fixes are different.
    """
    store = MemoryStore()
    session = store.for_session("default")
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def writer(w: int) -> None:
        try:
            barrier.wait()
            for i in range(200):
                session.record(f"writer {w} turn {i}", _plan(drug=f"d{w}-{i}"))
        except BaseException as exc:      # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(w,)) for w in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print(f"\n  [concurrent writes] 8 threads x 200 turns -> "
          f"turns={len(session)} bytes={session.size_bytes()} errors={len(errors)}")
    assert not errors
    assert len(session) <= session.max_turns
    assert session.size_bytes() <= session.max_bytes
    assert len(session) >= 1
    # The history is coherent: every retained turn is a real turn someone wrote.
    for turn in session.turns:
        assert turn.query.startswith("writer ")


def test_session_evicted_mid_request_becomes_a_silent_write_to_nowhere() -> None:
    """A held `ConversationMemory` reference survives its own eviction.

    `for_session` returns the object; LRU eviction later drops the map entry.
    A request holding the reference keeps writing to an orphan. Nothing raises
    and nothing is corrupted — the turn is simply not there next time.
    Acceptable at MAX_SESSIONS=256, but it is a lost write, not a no-op.
    """
    store = MemoryStore(max_sessions=2)
    held = store.for_session("alice")
    held.record("alice turn 1", _plan(drug="Pembrolizumab"))

    store.for_session("bob")
    store.for_session("carol")          # evicts alice

    assert "alice" not in store.session_ids
    held.record("alice turn 2", _plan(drug="Pembrolizumab"))   # writes to nowhere
    fresh = store.for_session("alice")

    print(f"\n  [orphaned session] held object turns={len(held)}, "
          f"store's alice turns={len(fresh)}")
    assert len(held) == 2
    assert len(fresh) == 0, "the write landed on an orphan, not on the store"
    assert fresh is not held


# ==========================================================================
# 4. Can one request's failure corrupt another's state?
# ==========================================================================


async def test_upstream_failure_mid_flight_does_not_corrupt_concurrent_requests() -> None:
    """A non-retryable CTGovError raised while other probes overlap.

    Asserts on RESULTS: the survivors return the right number, the cache does
    not gain a poisoned entry for the failed key, the shared pacing schedule is
    still monotonic, and a request issued afterwards succeeds.
    """
    def responder(request: httpx.Request, n: int) -> httpx.Response:
        if "boom" in str(request.url):
            return httpx.Response(400, text="Invalid prefix in parameter name")
        return httpx.Response(200, json={"totalCount": 99, "studies": []})

    transport = _SlowTransport(latency=0.05, responder=responder)
    async with _client(transport) as client:
        good = Filter({"query.term": "ok"})
        other = Filter({"query.term": "also-ok"})
        bad = Filter({"query.term": "boom"})

        results = await asyncio.gather(
            client.count(bad), client.count(good), client.count(other),
            return_exceptions=True,
        )
        failures = [r for r in results if isinstance(r, BaseException)]
        successes = [r for r in results if not isinstance(r, BaseException)]

        bad_probe = {**bad.params, "countTotal": "true",
                     "pageSize": "1", "fields": "NCTId"}
        poisoned = client._cache.get(bad_probe)
        after = await client.count(Filter({"query.term": "afterwards"}))

    print(f"\n  [failure isolation] {len(failures)} failed, "
          f"{len(successes)} succeeded concurrently")
    print(f"    survivor results        : {successes}")
    print(f"    cache entry for failure : {poisoned!r}")
    print(f"    post-failure count      : {after}")

    assert len(failures) == 1 and isinstance(failures[0], CTGovError)
    assert successes == [99, 99], "concurrent requests returned their own measurement"
    assert poisoned is None, "a failed probe must not leave a cache entry"
    assert after == 99, "the client is still usable after a peer request failed"


async def test_a_failed_turn_is_not_recorded_as_inherited_context() -> None:
    """The failure path must not leave memory claiming a subject it never measured."""
    store = MemoryStore()
    session = store.for_session("s1")
    session.record("pembrolizumab by phase", _plan(drug="Pembrolizumab"))

    def responder(request: httpx.Request, n: int) -> httpx.Response:
        return httpx.Response(400, text="nope")

    transport = _SlowTransport(latency=0.0, responder=responder)
    async with _client(transport) as client:
        with pytest.raises(CTGovError):
            await client.count(Filter({"query.term": "melanoma"}))

    # main.py records only AFTER execute() returns, so the failed turn is absent.
    resolved = resolve_followup("what about phase 3?", session)
    assert resolved is not None
    print(f"\n  [failed turn] follow-up subject after a failed request: "
          f"{resolved.drug_name!r}")
    assert resolved.drug_name == "Pembrolizumab"


# ==========================================================================
# 5. entities.load_index — check-then-act across threads
# ==========================================================================


def test_concurrent_cold_start_opens_and_orphans_sqlite_connections() -> None:
    """`load_index` checks `_CACHE`, releases the lock, opens, then re-locks.

    Two threads that miss simultaneously both open a connection; the loser's is
    overwritten in the map and never closed. FastAPI runs sync work in a
    threadpool, so a cold start under load hits this on the first N concurrent
    entity queries.

    Correctness is unaffected (all handles point at the same read-only file);
    the cost is file descriptors and per-connection page cache, and
    `clear_cache()` cannot reclaim them.
    """
    from app import entities

    try:
        entities.load_index("sponsor")
    except Exception as exc:                       # noqa: BLE001
        pytest.skip(f"entity index unavailable: {exc}")
    entities.clear_cache()

    opened: list[object] = []
    real_connect = entities.sqlite3.connect

    def spy(target, *args, **kwargs):
        conn = real_connect(target, *args, **kwargs)
        if "entities.db" in str(target):
            opened.append(conn)
        return conn

    entities.sqlite3.connect = spy
    barrier = threading.Barrier(8)
    try:
        def worker() -> None:
            barrier.wait()
            entities.load_index("sponsor")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        entities.sqlite3.connect = real_connect

    retained = {id(v.connection) for v in entities._CACHE.values()}
    orphans = [c for c in opened if id(c) not in retained]
    still_open = 0
    for conn in orphans:
        try:
            conn.execute("SELECT 1")
            still_open += 1
        except Exception:                          # noqa: BLE001
            pass

    print(f"\n  [load_index race] 8 concurrent cold callers")
    print(f"    file connections opened : {len(opened)}")
    print(f"    retained in _CACHE      : {len(retained)}")
    print(f"    ORPHANED                : {len(orphans)} "
          f"(still open and unreachable: {still_open})")

    for conn in orphans:
        try:
            conn.close()
        except Exception:                          # noqa: BLE001
            pass
    entities.clear_cache()

    # The finding. If a future fix holds the lock across open(), this becomes 0
    # and the assertion below should be inverted along with the audit doc.
    assert len(opened) > 1, "check-then-act: concurrent cold callers each open one"
    assert len(retained) == 1, "exactly one handle survives; the rest are orphans"
    assert len(orphans) == len(opened) - 1


def test_entity_index_handle_is_safe_to_share_across_threads() -> None:
    """The positive result: one handle, many threads, one lock, correct rows."""
    from app import entities

    try:
        index = entities.load_index("sponsor")
    except Exception as exc:                       # noqa: BLE001
        pytest.skip(f"entity index unavailable: {exc}")

    expected = len(index)
    seen: list[int] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        try:
            barrier.wait()
            for _ in range(25):
                seen.append(len(index))
        except BaseException as exc:               # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print(f"\n  [shared index] 8 threads x 25 reads -> "
          f"distinct results={set(seen)} errors={len(errors)}")
    assert not errors
    assert set(seen) == {expected}, "no torn or interleaved reads"
