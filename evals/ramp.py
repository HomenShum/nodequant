"""Concurrency ramp: measure where this actually saturates.

Two fixes landed after the scalability audit — LRU eviction and single-flight
de-duplication. This measures them under increasing concurrency instead of
asserting they help, and it measures the OLD behaviour in the same process so
the comparison is not against a remembered number.

No live API. A transport that actually suspends stands in for upstream, because
the audit's first finding was that a synchronous mock serialises the coroutines
and reports the race as already fixed:

    sync MockTransport   8 concurrent calls -> 1 request   (the race is hidden)
    transport with await 8 concurrent calls -> 8 requests  (the race is real)

    uv run --no-sync python -m evals.ramp
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import httpx

from app.ctgov import CTGovClient, Filter

REPORT = Path(__file__).resolve().parent / "ramp_report.json"
UPSTREAM_LATENCY_S = 0.05


class _SlowTransport(httpx.AsyncBaseTransport):
    """Suspends, so concurrent callers genuinely interleave."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        await asyncio.sleep(UPSTREAM_LATENCY_S)
        return httpx.Response(200, json={"totalCount": 4242, "studies": []})


def _client(transport: _SlowTransport) -> CTGovClient:
    return CTGovClient(client=httpx.AsyncClient(transport=transport))


async def _run(concurrency: int, distinct_keys: int, single_flight: bool) -> dict[str, Any]:
    transport = _SlowTransport()
    client = _client(transport)
    if not single_flight:
        # Disable coalescing by making the in-flight table permanently empty.
        class _Blind(dict):
            def get(self, *a, **k): return None
            def __setitem__(self, *a, **k): pass
        client._inflight = _Blind()  # type: ignore[assignment]

    filters = [Filter({"query.term": f"q{i % distinct_keys}"}) for i in range(concurrency)]
    started = time.perf_counter()
    results = await asyncio.gather(*(client.count(f) for f in filters))
    elapsed = time.perf_counter() - started

    assert set(results) == {4242}, "correctness must not depend on the optimisation"
    stats = client.cache_stats
    return {
        "concurrency": concurrency,
        "distinct_keys": distinct_keys,
        "single_flight": single_flight,
        "upstream_requests": len(transport.calls),
        "ideal_requests": distinct_keys,
        "wasted": len(transport.calls) - distinct_keys,
        "coalesced": stats.get("coalesced", 0),
        "seconds": round(elapsed, 3),
    }


async def _lru_vs_fifo(reads: int, cap: int) -> dict[str, Any]:
    """The hot-key survival test, both policies, same process."""
    from app.ctgov import _BoundedCache

    hot = {"query.term": "hot"}
    out: dict[str, Any] = {}

    lru = _BoundedCache(max_entries=cap)
    lru.put(hot, 1)
    for i in range(reads):
        lru.get(hot)
        lru.put({"query.term": f"cold{i}"}, i)
    out["lru"] = {"hot_survived": lru.get(hot) is not None,
                  "misses": lru.misses, "hits": lru.hits}

    # FIFO reconstruction: same cache with recency tracking disabled.
    fifo = _BoundedCache(max_entries=cap)
    fifo._data = OrderedDict()  # type: ignore[attr-defined]
    fifo.put(hot, 1)
    for i in range(reads):
        key = _BoundedCache.key(hot)
        entry = fifo._data.get(key)          # read WITHOUT move_to_end
        if entry is not None:
            fifo.hits += 1
        else:
            fifo.misses += 1
        fifo.put({"query.term": f"cold{i}"}, i)
    out["fifo"] = {"hot_survived": fifo._data.get(_BoundedCache.key(hot)) is not None,
                   "misses": fifo.misses, "hits": fifo.hits}
    return out


async def main() -> None:
    rows: list[dict[str, Any]] = []
    print("=== single-flight ramp (1 distinct key, rising concurrency) ===")
    print(f"{'conc':>5} {'sf':>4} {'upstream':>9} {'ideal':>6} {'wasted':>7} {'sec':>7}")
    for concurrency in (1, 2, 4, 8, 16, 32, 64, 128):
        for sf in (False, True):
            row = await _run(concurrency, distinct_keys=1, single_flight=sf)
            rows.append(row)
            print(f"{concurrency:5d} {str(sf):>4} {row['upstream_requests']:9d} "
                  f"{row['ideal_requests']:6d} {row['wasted']:7d} {row['seconds']:7.3f}")

    print("\n=== mixed workload: 64 concurrent over N distinct keys ===")
    print(f"{'keys':>5} {'sf':>4} {'upstream':>9} {'ideal':>6} {'wasted':>7} {'sec':>7}")
    for keys in (1, 4, 16, 64):
        for sf in (False, True):
            row = await _run(64, distinct_keys=keys, single_flight=sf)
            rows.append(row)
            print(f"{keys:5d} {str(sf):>4} {row['upstream_requests']:9d} "
                  f"{row['ideal_requests']:6d} {row['wasted']:7d} {row['seconds']:7.3f}")

    print("\n=== LRU vs FIFO: a key read on every iteration ===")
    policy = await _lru_vs_fifo(reads=3000, cap=2048)
    for name, r in policy.items():
        total = r["hits"] + r["misses"]
        rate = r["misses"] / total * 100 if total else 0
        print(f"  {name:5s} hot_survived={str(r['hot_survived']):5s} "
              f"misses={r['misses']:5d}  miss_rate={rate:5.1f}%")

    sf_on = [r for r in rows if r["single_flight"]]
    sf_off = [r for r in rows if not r["single_flight"]]
    saved = sum(r["wasted"] for r in sf_off) - sum(r["wasted"] for r in sf_on)
    summary = {
        "upstream_requests_saved": saved,
        "worst_case_waste_without_single_flight": max(r["wasted"] for r in sf_off),
        "worst_case_waste_with_single_flight": max(r["wasted"] for r in sf_on),
        "lru_vs_fifo": policy,
    }
    REPORT.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2),
                      encoding="utf-8")
    print("\n--- summary ---")
    print(json.dumps(summary, indent=2))
    print(f"\nWrote {REPORT}")


if __name__ == "__main__":
    asyncio.run(main())
