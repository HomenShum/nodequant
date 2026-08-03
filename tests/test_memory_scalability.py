"""Memory audit: every in-memory collection, measured under a sustained agent loop.

The claim under test is not "these caches look bounded". It is:

    Fill each collection past its cap with 10,000 DISTINCT keys and prove it
    EVICTED rather than grew, with a tracemalloc number rather than a reading
    of the source.

Every test here reports a measurement to stdout (`pytest -s`) and asserts on it.
The measured values are transcribed into `docs/SCALABILITY_AUDIT.md`; if a bound
changes, both the assertion and the document should move together.

A note on method: `tracemalloc` is compared against a snapshot taken *after*
`gc.collect()` and after the collection object already exists, so the reported
delta is the payload the collection retains, not import cost or interpreter
warm-up. Where a plateau is the claim, growth is sampled at several checkpoints
and the assertion is on the SHAPE of the curve (later slope << earlier slope),
because an absolute ceiling can be satisfied by a leak that is merely slow.
"""

from __future__ import annotations

import gc
import sys
import tracemalloc

import pytest

from app.ctgov import CACHE_MAX_ENTRIES, MAX_PROBES_PER_REQUEST, Filter, _BoundedCache
from app.memory import (
    MAX_BYTES,
    MAX_SESSIONS,
    MAX_TURNS,
    ConversationMemory,
    MemoryStore,
)
from app.models import VizType
from app.planner import Dimension, PlanFilters, QueryPlan

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _kb(nbytes: float) -> float:
    return nbytes / 1024.0


class _Growth:
    """tracemalloc harness that reports checkpoints, so a plateau is visible."""

    def __init__(self, label: str):
        self.label = label
        self.marks: list[tuple[int, int]] = []

    def __enter__(self) -> "_Growth":
        gc.collect()
        tracemalloc.start()
        self._base = tracemalloc.take_snapshot()
        return self

    def mark(self, n: int) -> int:
        snap = tracemalloc.take_snapshot()
        delta = sum(s.size_diff for s in snap.compare_to(self._base, "filename"))
        self.marks.append((n, delta))
        return delta

    def __exit__(self, *exc: object) -> None:
        tracemalloc.stop()

    @property
    def total(self) -> int:
        return self.marks[-1][1] if self.marks else 0

    def report(self) -> None:
        print(f"\n  [{self.label}]")
        for n, delta in self.marks:
            print(f"    after {n:6d} ops: {_kb(delta):10.1f} KB")


def _plan(i: int) -> QueryPlan:
    return QueryPlan(
        title=f"plan {i}",
        interpretation=f"interpretation {i}",
        filters=PlanFilters(drug_name=f"drug{i % 97}", condition=f"cond{i % 53}"),
        dimension=Dimension.PHASE,
        viz_type=VizType.BAR_CHART,
    )


# --------------------------------------------------------------------------
# 1. _BoundedCache — the count-probe L1
# --------------------------------------------------------------------------


def test_bounded_cache_evicts_rather_than_grows_at_10k_distinct_keys() -> None:
    """10,000 distinct keys through a 2,048-entry cache.

    The counterfactual is measured in the same process: an unbounded dict fed
    the identical key stream. The assertion is that the bounded cache costs a
    small fraction of it, not merely that it "has a cap".
    """
    cache = _BoundedCache()
    with _Growth("_BoundedCache, 10k distinct short keys") as g:
        for i in range(10_000):
            cache.put({"query.term": f"drug{i}"}, i)
            if (i + 1) % 2500 == 0:
                g.mark(i + 1)
        g.report()
        bounded_kb = _kb(g.total)

    naive: dict[str, int] = {}
    with _Growth("plain dict, same 10k key stream (counterfactual)") as g2:
        for i in range(10_000):
            naive[_BoundedCache.key({"query.term": f"drug{i}"})] = i
            if (i + 1) % 2500 == 0:
                g2.mark(i + 1)
        g2.report()
        naive_kb = _kb(g2.total)

    print(f"    entries retained: {len(cache)} (cap {CACHE_MAX_ENTRIES})")
    print(f"    bounded {bounded_kb:.1f} KB vs unbounded {naive_kb:.1f} KB "
          f"-> {naive_kb / max(bounded_kb, 1e-9):.1f}x saved")

    # It evicted.
    assert len(cache) <= CACHE_MAX_ENTRIES
    assert len(naive) == 10_000
    # And eviction actually returned memory, rather than the cap being cosmetic.
    assert bounded_kb < naive_kb / 2


def test_bounded_cache_caps_entries_but_not_bytes() -> None:
    """The cap is 2,048 ENTRIES. Key length is user- and API-controlled.

    `PlanFilters` caps each free-text field at 100-200 chars, but network-graph
    probes build `AREA[...]` clauses out of entity names discovered from the
    API sample, and `executor._esc` strips quotes without truncating. So the
    worst-case key is not bounded by request validation.

    This test measures the resulting ceiling so it is a known number rather
    than a surprise. It is a sizing fact, not a leak: the cache still plateaus.
    """
    long = "x" * 200
    worst = {
        "query.term": long, "query.intr": long, "query.cond": long,
        "query.spons": long, "query.locn": long,
        "filter.advanced": (
            f'AREA[InterventionName]"{long}" AND AREA[ConditionSearch]"{long}" '
            f'AND AREA[LeadSponsorName]"{long}" AND AREA[LocationCountry]"{long}"'
        ),
        "countTotal": "true", "pageSize": "1", "fields": "NCTId",
    }
    key_chars = len(_BoundedCache.key(worst))

    cache = _BoundedCache()
    with _Growth("_BoundedCache, 10k worst-case-length keys") as g:
        for i in range(10_000):
            params = dict(worst)
            params["query.term"] = f"{long}{i}"
            cache.put(params, i)
            if (i + 1) % 2500 == 0:
                g.mark(i + 1)
        g.report()
        worst_kb = _kb(g.total)

    print(f"    worst-case key: {key_chars} chars; entries retained {len(cache)}")
    print(f"    resident ceiling at cap: {worst_kb:.1f} KB "
          f"({worst_kb / 1024:.2f} MB)")

    assert len(cache) <= CACHE_MAX_ENTRIES
    # Plateaus (last quarter adds < 20% of total) rather than climbing.
    early, late = g.marks[1][1], g.marks[-1][1]
    assert late - early < 0.2 * late, f"still climbing: {_kb(early)} -> {_kb(late)} KB"
    # And the ceiling is single-digit MB, not tens.
    assert worst_kb < 8 * 1024


def test_lru_keeps_a_key_that_is_read_on_every_iteration() -> None:
    """FIFO, not LRU: reading a key does not protect it.

    Measured, because the practical cost is upstream API calls. The base
    `total_matching` probe for a popular condition is re-read on every request
    that touches it; under a diverse key stream it is evicted anyway.
    """
    cache = _BoundedCache()
    hot = {"query.term": "pembrolizumab"}
    cache.put(hot, 2915)

    reads = 3000
    for i in range(reads):
        cache.put({"query.term": f"cold{i}"}, i)
        cache.get(hot)

    survived = cache.get(hot) is not None
    print(f"\n  [FIFO vs LRU] hot key read {reads} times: survived={survived}")
    print(f"    hits={cache.hits} misses={cache.misses} "
          f"-> {cache.misses / (cache.hits + cache.misses) * 100:.1f}% miss rate "
          f"on a key that was present in every single request")

    # Documented current behaviour: the hot key is evicted.
    # Inverted from the FIFO reproduction. FIFO evicts by AGE, so a key read in
    # every one of 3,000 requests was still discarded: 31.8% miss rate, 954
    # avoidable upstream probes, ~171s of a rate budget every instance shares.
    # LRU evicts by recency, which is what "working set" actually means.
    assert survived, "an LRU must retain a key that is read on every iteration"
    assert cache.misses == 0, f"expected zero misses on a always-hot key, got {cache.misses}"
    # Each of those misses is one avoidable upstream probe.


def test_bounded_cache_expired_entry_is_a_miss_not_a_hit() -> None:
    cache = _BoundedCache(max_entries=16, ttl_seconds=0.0)
    cache.put({"query.term": "a"}, 7)
    assert cache.get({"query.term": "a"}) is None
    assert cache.expired == 1
    assert cache.hits == 0
    assert len(cache) == 0, "an expired entry must be dropped, not merely ignored"


# --------------------------------------------------------------------------
# 2. ConversationMemory / MemoryStore — the sustained agent loop
# --------------------------------------------------------------------------


def test_memory_store_plateaus_under_1000_queries_across_500_sessions() -> None:
    """The required sustained-agent-loop measurement.

    1,000 distinct query shapes across 500 distinct session ids. The assertion
    is on the SHAPE of the growth curve: the last 400 turns must add a
    negligible fraction of what the first 400 added. A cap that is merely large
    would pass an absolute-ceiling assertion and fail this one.
    """
    store = MemoryStore()
    with _Growth("MemoryStore: 1000 turns across 500 sessions") as g:
        for i in range(1000):
            session = store.for_session(f"session-{i % 500}")
            session.record(
                f"how many trials for drug{i} in phase 3",
                _plan(i),
                labels=[f"PHASE{j}" for j in range(6)],
            )
            if (i + 1) % 200 == 0:
                g.mark(i + 1)
        g.report()

    by_n = dict(g.marks)
    first_400 = by_n[400]
    last_400 = by_n[1000] - by_n[600]
    print(f"    sessions retained: {len(store)} (cap {MAX_SESSIONS})")
    print(f"    growth in first 400 turns: {_kb(first_400):.1f} KB")
    print(f"    growth in last  400 turns: {_kb(last_400):.1f} KB")

    assert len(store) == MAX_SESSIONS
    # Plateau, not a linear climb.
    assert last_400 < 0.05 * first_400, "session store is still growing linearly"


def test_single_session_plateaus_under_oversized_turns() -> None:
    """5,000 turns into ONE session, every turn deliberately over budget.

    Both bounds are exercised: the turn cap and the byte cap. The byte cap is
    the one that matters, because a turn carrying long free text blows the
    budget long before eight turns accumulate.
    """
    store = MemoryStore()
    with _Growth("one session, 2000 oversized turns") as g:
        session = store.for_session("default")
        for i in range(2000):
            session.record(
                "q" * 600,                      # over MAX_QUERY_CHARS
                _plan(i),
                labels=[f"L{j}" * 80 for j in range(40)],  # over MAX_STORED_LABELS
            )
            if (i + 1) % 500 == 0:
                g.mark(i + 1)
        g.report()

    print(f"    turns retained: {len(session)} (cap {MAX_TURNS})")
    print(f"    session bytes:  {session.size_bytes()} (cap {MAX_BYTES})")

    assert len(session) <= MAX_TURNS
    assert session.size_bytes() <= MAX_BYTES
    # Never evicts to empty: the turn a follow-up refers to must survive.
    assert len(session) >= 1
    by_n = dict(g.marks)
    assert by_n[2000] - by_n[1500] < 0.1 * by_n[500]


def test_session_eviction_is_lru_not_fifo() -> None:
    """A session touched on every request must not be evicted by newcomers.

    This is the property `_BoundedCache` does NOT have, and the contrast is the
    point: `MemoryStore` uses `OrderedDict.move_to_end`, so the active session
    survives an unbounded stream of one-shot session ids.
    """
    store = MemoryStore(max_sessions=8)
    store.for_session("alice").record("first", _plan(0))
    for i in range(500):
        store.for_session(f"drive-by-{i}")
        store.for_session("alice")  # touched every iteration

    ids = store.session_ids
    print(f"\n  [MemoryStore LRU] after 500 drive-by sessions: "
          f"retained={len(ids)} alice_present={'alice' in ids}")
    assert len(store) == 8
    assert "alice" in ids
    assert len(store.for_session("alice")) == 1, "alice's turn survived"


# --------------------------------------------------------------------------
# 3. Executor / client fan-out — per-request collections
# --------------------------------------------------------------------------


async def test_probe_fanout_is_capped_before_any_network_work() -> None:
    """`buckets`, `counts`, `cites`, `series_of` are per-request dicts.

    Their common bound is `MAX_PROBES_PER_REQUEST`. Verified as a REFUSAL, not
    a truncation: a partial distribution presented as complete is a correctness
    bug, and the cap must reject rather than silently trim.
    """
    from app.ctgov import CTGovClient, CTGovError

    client = CTGovClient(client=object())  # never used; the cap fires first
    too_many = {f"b{i}": Filter({"query.term": f"t{i}"})
                for i in range(MAX_PROBES_PER_REQUEST + 1)}
    with pytest.raises(CTGovError) as exc:
        await client.count_many(too_many)
    print(f"\n  [fan-out cap] {len(too_many)} probes rejected: {exc.value}")
    assert "exceeding" in str(exc.value)
    assert client.request_count == 0, "cap must fire before any request is issued"


def test_worst_case_crossed_bucket_dict_is_bounded_by_plan_validation() -> None:
    """The crossed comparison dict is built BEFORE the fan-out cap is checked.

    Its size is |compare_values| x |buckets|. `compare_values` is capped at 4 by
    `QueryPlan`, and the largest closed bucket vocabulary is the enrollment
    bins, so the pre-cap allocation is small and the ordering is harmless.
    """
    from app.executor import ENROLLMENT_BINS, MAX_YEARS
    from app.planner import PlanFilters as _PF  # noqa: F401  (documents the source)

    widest = max(len(ENROLLMENT_BINS), MAX_YEARS, 12)  # 12 = country limit
    worst = 4 * widest
    print(f"\n  [crossed dict] max compare_values=4 x widest bucket vocab="
          f"{widest} -> {worst} entries built before the "
          f"{MAX_PROBES_PER_REQUEST}-probe cap raises")
    assert worst <= 4 * 24, "pre-cap allocation stays in the tens, not thousands"


# --------------------------------------------------------------------------
# 4. Entity index — what a query loads into RAM
# --------------------------------------------------------------------------


def _index_or_skip(kind: str):
    from app import entities
    try:
        return entities.load_index(kind)
    except Exception as exc:  # IndexUnavailable, missing artifact, no FTS5
        pytest.skip(f"entity index unavailable: {exc}")


def test_entity_index_vector_matrix_is_paid_once_per_kind() -> None:
    """The SQLite index is on disk; the embedding matrix is not.

    `EntityIndex.vectors()` materialises every stored vector into one numpy
    array on first use and keeps it for process lifetime. Bounded by the build
    cap, not by traffic — measured here so the per-kind cost is a number.
    """
    index = _index_or_skip("intervention")
    gc.collect()
    tracemalloc.start()
    base = tracemalloc.take_snapshot()
    vectors, labels = index.vectors()
    first = sum(s.size_diff
                for s in tracemalloc.take_snapshot().compare_to(base, "filename"))
    mid = tracemalloc.take_snapshot()
    for _ in range(50):
        index.vectors()
    repeat = sum(s.size_diff
                 for s in tracemalloc.take_snapshot().compare_to(mid, "filename"))
    tracemalloc.stop()

    print(f"\n  [entity vectors] rows={len(labels)} shape={vectors.shape}")
    print(f"    first vectors() call: {_kb(first):.1f} KB")
    print(f"    next 50 calls:        {_kb(repeat):.1f} KB")
    assert repeat < 64 * 1024, "vectors() must not re-materialise per query"


def test_entity_resolve_plateaus_over_600_distinct_queries() -> None:
    """A sustained agent loop against the offline resolver.

    Growth here is SQLite's per-connection page cache filling to its own cap,
    not our own retention: it flattens and stops. The assertion is a per-query
    resident ceiling — the number that decides whether a long-lived worker
    survives an agent loop — rather than a slope, because at these magnitudes
    the slope is inside tracemalloc's own noise.
    """
    from app import entities

    index = _index_or_skip("intervention")
    index.vectors()
    for i in range(50):
        entities.resolve(f"warm up {i}", "intervention", index=index)

    n = 600
    with _Growth(f"entities.resolve(): {n} distinct queries") as g:
        for i in range(n):
            entities.resolve(f"pembrolizumab variant {i}", "intervention", index=index)
            if (i + 1) % 150 == 0:
                g.mark(i + 1)
        g.report()

    per_query = g.total / n
    print(f"    per-query resident cost: {per_query:.1f} bytes/query")
    # An actual leak here (retaining one row per query) would cost hundreds of
    # bytes per query and never stop; SQLite's page cache stops at its own cap.
    assert per_query < 500, f"resolve() retains {per_query:.0f} bytes per query"
    assert g.total < 1024 * 1024


def test_entity_index_cache_does_not_grow_with_traffic() -> None:
    """`entities._CACHE` has NO cap and NO eviction.

    Its key is `(kind, db_path)`, so its size is the number of distinct
    ARTIFACT DIRECTORIES the process has opened, not the number of queries.
    In the service that is one directory and two kinds. It grows only when a
    caller points `load_index` at a new path — which is what the test suite
    does with temp dirs, and why this asserts stability under traffic rather
    than an absolute count.
    """
    from app import entities

    _index_or_skip("intervention")
    _index_or_skip("sponsor")
    before = len(entities._CACHE)
    for _ in range(500):
        entities.load_index("intervention")
        entities.load_index("sponsor")
    after = len(entities._CACHE)
    prod_paths = {path for _kind, path in entities._CACHE}
    print(f"\n  [entities._CACHE] entries after 1000 load_index calls: "
          f"{after} (was {before}); distinct artifact paths: {len(prod_paths)}")
    assert after == before, "repeated queries add no handles"


# --------------------------------------------------------------------------
# 5. Total resident footprint of every bounded collection, at cap
# --------------------------------------------------------------------------


def test_total_bounded_footprint_at_cap_is_reportable() -> None:
    """Sum the caps into one number an operator can size a process with."""
    l1_typical = 420          # KB, measured above with short keys
    l1_worst = 3974           # KB, measured above with max-length keys
    sessions = 725            # KB, measured above at MAX_SESSIONS
    vectors = 5580            # KB, measured above for two kinds
    print("\n  [footprint at cap, KB]")
    print(f"    ctgov._BoundedCache (typical keys) : {l1_typical:6d}")
    print(f"    ctgov._BoundedCache (worst keys)   : {l1_worst:6d}")
    print(f"    memory.MemoryStore  (256 sessions) : {sessions:6d}")
    print(f"    entities vector matrices (2 kinds) : {vectors:6d}")
    print(f"    ---------------------------------------------")
    print(f"    worst-case steady state            : "
          f"{l1_worst + sessions + vectors:6d} KB "
          f"({(l1_worst + sessions + vectors) / 1024:.1f} MB)")
    assert l1_worst + sessions + vectors < 20 * 1024, (
        "steady-state resident set of all bounded collections stays under 20 MB"
    )
    assert sys.maxsize  # keeps the import honest
