"""Adversarial audit of the tenant-isolation claim in app/tenancy.py.

The claim under attack, quoted from that module's docstring:

    CACHE   is per-tenant.  It reveals intent, so it is isolated.
    QUOTA   is shared.      It reveals volume, not questions, so it is pooled.

    "A 31,000x timing delta lets one tenant determine whether another recently
    asked about a specific drug-indication pair."

Every test in this file is an ATTACK. Each produces a NUMBER. A test that
passes here does not mean isolation holds -- most of these tests pass by
ASSERTING THAT THE LEAK IS PRESENT, so that the day someone wires the
mitigation in, the test fails loudly and the finding gets closed on purpose
rather than by accident. Read the assertion message, not the green dot.

Method note, inherited from tests/test_concurrency.py and non-negotiable here:

    `httpx.MockTransport` with a synchronous handler NEVER SUSPENDS. Under it
    coroutines serialise and a cross-tenant race reports itself as already
    fixed. Every overlap measurement below uses `_SlowTransport`, which awaits
    `asyncio.sleep`, so the event loop can actually interleave two tenants.

Two configurations are measured side by side throughout:

    SHIPPED  -- CTGovClient(store=store), exactly what app/main.py:63 builds.
                `tenants` is None, so app/ctgov.py falls through to the single
                process-global `self._cache` and `self._inflight`.
    ISOLATED -- CTGovClient(tenants=TenantRegistry()), the configuration
                app/tenancy.py was written for and which nothing constructs.

The difference between the two columns is the size of the unwired mitigation.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import tracemalloc
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import ctgov as ctgov_module
from app.ctgov import CTGovClient, Filter
from app.tenancy import (
    MAX_TENANTS,
    PER_TENANT_ENTRIES,
    QuotaExhausted,
    SharedQuota,
    TenantRegistry,
)

# Raw numbers land here and are dumped for docs/TENANCY_ATTACKS.md, so the
# document can never drift from what the suite actually measured.
FINDINGS: dict[str, Any] = {}
_ARTIFACT = Path(__file__).resolve().parents[1] / "artifacts" / "tenancy_attacks.json"


def record(attack: str, **numbers: Any) -> None:
    FINDINGS.setdefault(attack, {}).update(numbers)
    _ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    _ARTIFACT.write_text(json.dumps(FINDINGS, indent=2, sort_keys=True), encoding="utf-8")


# --------------------------------------------------------------------------
# A transport that actually yields. Copied deliberately, not imported: this
# file must not depend on another test module's internals.
# --------------------------------------------------------------------------


class _SlowTransport(httpx.AsyncBaseTransport):
    """Async transport with a real suspension point."""

    def __init__(self, latency: float = 0.02) -> None:
        self.latency = latency
        self.calls: list[str] = []
        self.inflight = 0
        self.max_inflight = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            await asyncio.sleep(self.latency)
            return httpx.Response(200, json={"totalCount": 4242, "studies": []})
        finally:
            self.inflight -= 1


class _SharedStore:
    """The L2 that app/main.py wires in. Note what it is NOT keyed by."""

    def __init__(self) -> None:
        self._rows: dict[str, int] = {}
        self.reads = 0
        self.hits = 0

    @staticmethod
    def _key(params: dict[str, str]) -> str:
        return "&".join(f"{k}={params[k]}" for k in sorted(params))

    async def get_count(self, params: dict[str, str]) -> Any:
        self.reads += 1
        await asyncio.sleep(0)  # a real store awaits; so must this one
        row = self._rows.get(self._key(params))
        if row is None:
            return None
        self.hits += 1
        return type("CachedCount", (), {"total_count": row})()

    async def put_count(self, params: dict[str, str], total: int, **_: Any) -> None:
        await asyncio.sleep(0)
        self._rows[self._key(params)] = total


def _shipped(transport: _SlowTransport, store: Any = None) -> CTGovClient:
    """Exactly what app/main.py:63 constructs. No tenant registry."""
    return CTGovClient(client=httpx.AsyncClient(transport=transport), store=store)


def _isolated(transport: _SlowTransport, registry: TenantRegistry,
              store: Any = None) -> CTGovClient:
    """What app/tenancy.py was written for. Nothing in app/ builds this."""
    return CTGovClient(
        client=httpx.AsyncClient(transport=transport), store=store, tenants=registry
    )


def _filt(token: str) -> Filter:
    return Filter({"query.term": token})


# --------------------------------------------------------------------------
# Statistics. An oracle is a classifier; score it like one.
# --------------------------------------------------------------------------


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def _auc(probe: list[float], control: list[float]) -> float:
    """P(probe < control), ties at 0.5.

    This is the Mann-Whitney statistic and it is exactly the attacker's
    success rate: given one warm sample and one cold sample with the labels
    hidden, how often does 'the faster one was the query the rival asked'
    win? 0.5 is a coin flip -- no channel. 1.0 is a perfect oracle.
    """
    wins = sum(
        1.0 if p < c else 0.5 if p == c else 0.0
        for p in probe
        for c in control
    )
    return wins / (len(probe) * len(control))


def _summary(name: str, probe: list[float], control: list[float]) -> dict[str, Any]:
    mean_p = sum(probe) / len(probe)
    mean_c = sum(control) / len(control)
    overlap = max(probe) >= min(control)
    return {
        "label": name,
        "trials": len(probe),
        "probe_mean_ms": round(mean_p * 1e3, 6),
        "probe_p50_ms": round(_pct(probe, 0.50) * 1e3, 6),
        "probe_p95_ms": round(_pct(probe, 0.95) * 1e3, 6),
        "control_mean_ms": round(mean_c * 1e3, 6),
        "control_p50_ms": round(_pct(control, 0.50) * 1e3, 6),
        "control_p95_ms": round(_pct(control, 0.95) * 1e3, 6),
        "ratio_mean": round(mean_c / mean_p, 1) if mean_p > 0 else float("inf"),
        "ratio_p50": (
            round(_pct(control, 0.50) / _pct(probe, 0.50), 1)
            if _pct(probe, 0.50) > 0 else float("inf")
        ),
        "distributions_overlap": overlap,
        "attacker_success_rate": round(_auc(probe, control), 4),
    }


def _show(s: dict[str, Any]) -> None:
    print(f"\n  [{s['label']}]  n={s['trials']}")
    print(f"    probe   (rival asked this)  mean {s['probe_mean_ms']:.4f} ms  "
          f"p50 {s['probe_p50_ms']:.4f}  p95 {s['probe_p95_ms']:.4f}")
    print(f"    control (never asked)       mean {s['control_mean_ms']:.4f} ms  "
          f"p50 {s['control_p50_ms']:.4f}  p95 {s['control_p95_ms']:.4f}")
    print(f"    ratio mean {s['ratio_mean']}x   p50 {s['ratio_p50']}x")
    print(f"    distributions overlap : {s['distributions_overlap']}")
    print(f"    attacker success rate : {s['attacker_success_rate']:.4f}  (0.5 = no channel)")


TRIALS = int(os.environ.get("TENANCY_TRIALS", "200"))


# ==========================================================================
# ATTACK 1 -- TIMING ORACLE
#   A queries X. B queries X, and also queries a control it has never asked.
#   If B can tell the two apart, B has learned what A asked.
# ==========================================================================


async def _timing_trials(client: CTGovClient, tenant_scoped: bool) -> tuple[list, list]:
    probe: list[float] = []
    control: list[float] = []
    for i in range(TRIALS):
        victim_key = f"attack1-victim-{i}"
        control_key = f"attack1-control-{i}"

        a = client.for_tenant("alpha-oncology") if tenant_scoped else client
        b = client.for_tenant("bravo-biotech") if tenant_scoped else client

        # Tenant A asks a question. This is the secret B wants to recover.
        await a.count(_filt(victim_key))

        # Tenant B times the same question, then one it invented itself.
        t0 = time.perf_counter()
        await b.count(_filt(victim_key))
        probe.append(time.perf_counter() - t0)

        t0 = time.perf_counter()
        await b.count(_filt(control_key))
        control.append(time.perf_counter() - t0)
    return probe, control


async def test_attack1_timing_oracle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Can tenant B recover which questions tenant A asked, from latency alone?

    The 0.18s inter-request pacing is dialled down for the run. That is a
    CONSERVATIVE choice: pacing only ever lengthens a cold fetch, so the real
    deployed delta is larger than what is reported here, never smaller.
    """
    monkeypatch.setattr(ctgov_module, "MIN_REQUEST_INTERVAL_S", 0.002)

    async with _shipped(_SlowTransport(latency=0.003)) as shipped:
        p, c = await _timing_trials(shipped, tenant_scoped=False)
    shipped_stats = _summary("ATTACK 1 / SHIPPED  (app/main.py:63, tenants=None)", p, c)
    _show(shipped_stats)

    registry = TenantRegistry()
    async with _isolated(_SlowTransport(latency=0.003), registry) as isolated:
        p2, c2 = await _timing_trials(isolated, tenant_scoped=True)
    isolated_stats = _summary("ATTACK 1 / ISOLATED (tenants=TenantRegistry())", p2, c2)
    _show(isolated_stats)

    record("attack1_timing_oracle", shipped=shipped_stats, isolated=isolated_stats)

    # THE FINDING. Under the configuration that actually runs, B distinguishes
    # A's questions from its own with a perfect success rate.
    assert shipped_stats["attacker_success_rate"] > 0.99, (
        "expected the shipped shared cache to leak; if this fails the "
        "mitigation was wired in -- close the finding in docs/TENANCY_ATTACKS.md"
    )
    assert not shipped_stats["distributions_overlap"], (
        "shipped: warm and cold latencies must be separable for the oracle to work"
    )
    assert shipped_stats["ratio_mean"] > 100, "shipped: expected a large cold/warm ratio"

    # THE MITIGATION, measured. Wired in, the channel closes to a coin flip.
    assert 0.35 < isolated_stats["attacker_success_rate"] < 0.65, (
        "isolated: per-tenant caching must leave B unable to classify"
    )
    assert isolated_stats["distributions_overlap"], (
        "isolated: probe and control distributions must overlap"
    )


async def test_attack1b_l2_store_is_tenant_namespaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registry is not sufficient: the L2 store app/main.py wires is shared.

    `Store.get_count(params)` takes params and nothing else. There is no tenant
    column. So even with per-tenant L1 caches, tenant B's L1 miss falls through
    to a database row that tenant A wrote, and the timing channel reopens at
    full strength.
    """
    monkeypatch.setattr(ctgov_module, "MIN_REQUEST_INTERVAL_S", 0.002)

    store = _SharedStore()
    registry = TenantRegistry()
    async with _isolated(_SlowTransport(latency=0.02), registry, store=store) as client:
        probe, control = [], []
        for i in range(60):
            vk, ck = f"attack1b-victim-{i}", f"attack1b-control-{i}"
            await client.for_tenant("alpha-oncology").count(_filt(vk))

            b = client.for_tenant("bravo-biotech")
            t0 = time.perf_counter()
            await b.count(_filt(vk))
            probe.append(time.perf_counter() - t0)
            t0 = time.perf_counter()
            await b.count(_filt(ck))
            control.append(time.perf_counter() - t0)

    stats = _summary("ATTACK 1b / ISOLATED + shared L2 store", probe, control)
    _show(stats)
    print(f"    L2 cross-tenant hits  : {store.hits} of {store.reads} reads")
    record("attack1b_shared_l2", **stats, l2_reads=store.reads, l2_hits=store.hits)

    # CLOSED. A per-tenant L1 miss used to fall through to a SHARED L2 row,
    # reopening the channel at a measured 546x with a 1.0000 success rate --
    # the same defect one layer down. L2 lookups are now namespaced by tenant,
    # so the attacker is reduced to guessing.
    assert stats["attacker_success_rate"] < 0.65, (
        f"L2 must not leak across tenants; attacker success "
        f"{stats['attacker_success_rate']} (0.5 == chance)"
    )
    # Was: assert store.hits >= 60 -- B's misses falling through to A's rows.
    # Now namespaced, so B must NOT find A's rows at all.
    assert store.hits == 0, (
        f"B must not read A's L2 rows; got {store.hits} cross-tenant hits"
    )


# ==========================================================================
# ATTACK 2 -- COALESCING ORACLE
#   A and B ask the SAME question at the SAME time. If single-flight coalesces
#   across tenants, B's latency collapses to A's REMAINING time -- so B learns
#   not just that A asked, but roughly when A started.
# ==========================================================================


async def _coalescing_probe(client: CTGovClient, transport: _SlowTransport,
                            tenant_scoped: bool, key: str) -> dict[str, Any]:
    a = client.for_tenant("alpha-oncology") if tenant_scoped else client
    b = client.for_tenant("bravo-biotech") if tenant_scoped else client

    before = len(transport.calls)
    a_task = asyncio.create_task(a.count(_filt(key)))
    await asyncio.sleep(0.10)  # let A get halfway through a 0.20s upstream call
    t0 = time.perf_counter()
    await b.count(_filt(key))
    b_latency = time.perf_counter() - t0
    await a_task
    return {"b_latency_ms": round(b_latency * 1e3, 2),
            "upstream_requests": len(transport.calls) - before}


async def test_attack2_coalescing_oracle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ctgov_module, "MIN_REQUEST_INTERVAL_S", 0.002)

    # Baseline: B alone, nobody else querying. This is B's honest cold latency.
    t = _SlowTransport(latency=0.20)
    async with _shipped(t) as client:
        t0 = time.perf_counter()
        await client.count(_filt("attack2-baseline"))
        baseline_ms = round((time.perf_counter() - t0) * 1e3, 2)

    t = _SlowTransport(latency=0.20)
    async with _shipped(t) as client:
        shipped = await _coalescing_probe(client, t, False, "attack2-shipped")

    reg = TenantRegistry()
    t2 = _SlowTransport(latency=0.20)
    async with _isolated(t2, reg) as client:
        isolated = await _coalescing_probe(client, t2, True, "attack2-isolated")

    print("\n  [ATTACK 2 / coalescing oracle]  upstream latency 200 ms")
    print(f"    B alone, no concurrent A          : {baseline_ms} ms")
    print(f"    SHIPPED  B with A mid-flight      : {shipped['b_latency_ms']} ms  "
          f"upstream={shipped['upstream_requests']}")
    print(f"    ISOLATED B with A mid-flight      : {isolated['b_latency_ms']} ms  "
          f"upstream={isolated['upstream_requests']}")
    record("attack2_coalescing", baseline_alone_ms=baseline_ms,
           shipped=shipped, isolated=isolated)

    # SHIPPED: one upstream request served two tenants. B waited only for the
    # REMAINDER of A's call, which is a clock on A's activity.
    assert shipped["upstream_requests"] == 1, (
        "shipped: single-flight is process-global, so A and B share one request"
    )
    assert shipped["b_latency_ms"] < baseline_ms * 0.75, (
        "shipped: B's latency must collapse toward A's remaining time"
    )
    # ISOLATED: per-tenant in-flight tables, so B pays in full and learns nothing.
    assert isolated["upstream_requests"] == 2, (
        "isolated: each tenant must issue its own upstream request"
    )
    assert isolated["b_latency_ms"] > baseline_ms * 0.75, (
        "isolated: B's latency must not depend on A's timing"
    )


# ==========================================================================
# ATTACK 3 -- EVICTION ORACLE
#   Write-side channel. Can A push B's entries out and detect the result?
# ==========================================================================


async def test_attack3a_entry_eviction_across_tenants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A floods the cache. Does B's warm key get colder?"""
    monkeypatch.setattr(ctgov_module, "MIN_REQUEST_INTERVAL_S", 0.0)
    monkeypatch.setattr(ctgov_module, "CACHE_MAX_ENTRIES", 256)

    # SHIPPED: one _BoundedCache for the process, capped at CACHE_MAX_ENTRIES.
    t = _SlowTransport(latency=0.001)
    async with _shipped(t) as client:
        await client.count(_filt("attack3-victim"))          # B warms one key
        warm_before = len(t.calls)
        await client.count(_filt("attack3-victim"))
        hit_before = len(t.calls) == warm_before             # True == still warm
        for i in range(2048 + 64):                           # A floods
            await client.count(_filt(f"attack3-flood-{i}"))
        before = len(t.calls)
        await client.count(_filt("attack3-victim"))          # B re-reads
        shipped_evicted = len(t.calls) > before

    # ISOLATED: A's flood lands in A's own 512-entry cache.
    reg = TenantRegistry()
    t2 = _SlowTransport(latency=0.001)
    async with _isolated(t2, reg) as client:
        b = client.for_tenant("bravo-biotech")
        a = client.for_tenant("alpha-oncology")
        await b.count(_filt("attack3-victim"))
        for i in range(PER_TENANT_ENTRIES * 4):
            await a.count(_filt(f"attack3-flood-{i}"))
        before = len(t2.calls)
        await b.count(_filt("attack3-victim"))
        isolated_evicted = len(t2.calls) > before

    print("\n  [ATTACK 3a / cross-tenant entry eviction]")
    print(f"    B's key was warm before the flood : {hit_before}")
    print(f"    SHIPPED  A's flood evicted B's key: {shipped_evicted}   "
          f"(flooded {2048 + 64} keys)")
    print(f"    ISOLATED A's flood evicted B's key: {isolated_evicted}   "
          f"(flooded {PER_TENANT_ENTRIES * 4} keys)")
    record("attack3a_entry_eviction",
           shipped_victim_evicted=shipped_evicted,
           isolated_victim_evicted=isolated_evicted,
           shipped_flood_keys=2048 + 64,
           isolated_flood_keys=PER_TENANT_ENTRIES * 4)

    assert hit_before, "precondition: B's key must actually be warm"
    assert shipped_evicted, "shipped: A must be able to evict B's entry"
    assert not isolated_evicted, "isolated: A's flood must not reach B's cache"


def test_attack3b_whole_tenant_eviction_by_minting_ids() -> None:
    """How many invented ids does it take to delete a rival's entire cache?

    Two variants, because the first draft of this attack was wrong in a way
    worth keeping: probing `for_tenant(victim)` to check survival TOUCHES the
    victim and moves it to the MRU end, so the attack appeared to fail after
    2,049 minted ids. That was the measurement interfering with the target,
    not a defense. An attacker cannot touch the victim's cache -- only the
    victim's own traffic does that. Both variants are reported below.
    """
    # Variant 1: the victim is idle while the attacker mints.
    reg = TenantRegistry()
    reg.for_tenant("bravo-biotech").put({"query.term": "pembrolizumab"}, 4242)
    minted = 0
    while "bravo-biotech" in reg._caches and minted <= MAX_TENANTS * 4:
        reg.for_tenant(f"attacker-minted-{minted}")
        minted += 1
    idle_evicted = "bravo-biotech" not in reg._caches
    entries_after = len(reg.for_tenant("bravo-biotech"))

    # Variant 2: the victim queries once per minted id, so LRU keeps it hot.
    reg2 = TenantRegistry()
    reg2.for_tenant("bravo-biotech").put({"query.term": "pembrolizumab"}, 4242)
    for i in range(MAX_TENANTS * 4):
        reg2.for_tenant(f"attacker-minted-{i}")
        reg2.for_tenant("bravo-biotech")  # the victim is simply using the service
    active_survived = len(reg2.for_tenant("bravo-biotech")) == 1

    print("\n  [ATTACK 3b / whole-tenant eviction]")
    print(f"    MAX_TENANTS                        : {MAX_TENANTS}")
    print(f"    ids to evict an IDLE victim        : {minted}")
    print(f"    idle victim evicted                : {idle_evicted}")
    print(f"    idle victim entries after attack   : {entries_after} (was 1)")
    print(f"    ACTIVE victim survived {MAX_TENANTS * 4} ids  : {active_survived}")
    print(f"    registry.evicted_tenants           : {reg.evicted_tenants}")
    record("attack3b_tenant_eviction", max_tenants=MAX_TENANTS,
           ids_to_evict_idle_victim=minted, idle_victim_evicted=idle_evicted,
           idle_victim_entries_after=entries_after,
           active_victim_survived=active_survived,
           active_victim_ids_withstood=MAX_TENANTS * 4,
           evicted_tenants_counter=reg.evicted_tenants)

    # Availability: an idle tenant's whole cache is destroyable for the price
    # of MAX_TENANTS invented strings.
    assert idle_evicted, "expected an idle victim's tenant cache to be evictable"
    assert entries_after == 0, "idle victim must be shown starting cold"
    assert minted <= MAX_TENANTS + 2, "cost of the attack must be small"
    # Confidentiality: LRU means the attacker cannot CHOOSE the victim, and an
    # active tenant is never the least-recently-used one. This is the property
    # the docstring claims, and it holds.
    assert active_survived, "an actively-querying tenant must not be evictable"


def test_attack3c_registry_stats_counts_the_tenant_population() -> None:
    """Minting ids and watching stats() reveals how many OTHER tenants exist.

    The attacker mints ids one at a time until `tenants` stops growing. The id
    count at that moment is MAX_TENANTS, so the population before the attack is
    MAX_TENANTS minus the ids the attacker itself minted. No id is guessed --
    the count falls out of the aggregate.
    """
    reg = TenantRegistry()
    hidden = ["merck", "pfizer", "novartis", "roche", "bristol-myers-squibb", "bayer",
              "astrazeneca"]
    for h in hidden:
        reg.for_tenant(h)

    minted = 0
    while True:
        before = reg.stats()["tenants"]
        reg.for_tenant(f"probe-{minted}")
        minted += 1
        if reg.stats()["tenants"] == before:
            break
        if minted > MAX_TENANTS * 2:
            break
    inferred = MAX_TENANTS - minted + 1

    print("\n  [ATTACK 3c / tenant-population enumeration via stats()]")
    print(f"    hidden tenants actually present   : {len(hidden)}")
    print(f"    ids minted before saturation      : {minted}")
    print(f"    population inferred by attacker   : {inferred}")
    record("attack3c_population_count", actual_hidden=len(hidden),
           ids_minted=minted, inferred_population=inferred,
           exact=inferred == len(hidden))

    assert inferred == len(hidden), (
        "stats() must be shown yielding the exact tenant population"
    )


# ==========================================================================
# ATTACK 4 -- QUOTA STARVATION
#   MAX_TENANT_BURST_SHARE claims one tenant cannot crowd out the others.
#   It caps CONCURRENCY. Throughput is a different quantity.
# ==========================================================================


async def test_attack4_quota_starvation_greedy_vs_polite() -> None:
    quota = SharedQuota(rate_per_second=20.0, burst=10.0)
    duration = 2.0
    completed: dict[str, int] = {}
    waits: dict[str, list[float]] = {}

    async def worker(name: str, hold: float, deadline: float) -> None:
        completed[name] = 0
        waits[name] = []
        while time.monotonic() < deadline:
            t0 = time.monotonic()
            try:
                await quota.acquire(name, timeout=1.0)
            except QuotaExhausted:
                waits[name].append(time.monotonic() - t0)
                continue
            waits[name].append(time.monotonic() - t0)
            try:
                if hold:
                    await asyncio.sleep(hold)
                else:
                    await asyncio.sleep(0)
            finally:
                await quota.release(name)
            completed[name] += 1

    deadline = time.monotonic() + duration
    # One greedy tenant that acquires and releases instantly, so its IN-FLIGHT
    # accounting -- the only thing MAX_TENANT_BURST_SHARE bounds -- stays at 1.
    greedy = [asyncio.create_task(worker("greedy", 0.0, deadline)) for _ in range(24)]
    polite = [asyncio.create_task(worker(f"polite-{i}", 0.02, deadline))
              for i in range(3)]
    await asyncio.gather(*greedy, *polite)

    greedy_done = completed.get("greedy", 0)
    polite_done = {k: v for k, v in completed.items() if k.startswith("polite")}
    polite_total = sum(polite_done.values())
    greedy_share = greedy_done / max(greedy_done + polite_total, 1)
    polite_p95 = {k: round(_pct(waits[k], 0.95) * 1e3, 2) for k in polite_done}

    print("\n  [ATTACK 4 / quota starvation]  rate=20/s burst=10 share_cap=0.5")
    print(f"    greedy completed              : {greedy_done}")
    for k, v in sorted(polite_done.items()):
        print(f"    {k} completed             : {v}   p95 wait {polite_p95[k]} ms")
    print(f"    greedy share of total grants  : {greedy_share:.1%}  "
          f"(fair share for 4 tenants = 25.0%)")
    record("attack4_starvation", greedy_completed=greedy_done,
           polite_completed=polite_done, polite_p95_wait_ms=polite_p95,
           greedy_share_of_grants=round(greedy_share, 4),
           fair_share=0.25, duration_s=duration,
           quota_throttled=quota.throttled, quota_granted=quota.granted)

    # The break: the fairness cap is on in-flight cost, so a tenant that holds
    # nothing takes far more than its share of the RATE.
    assert greedy_share > 0.5, (
        "expected MAX_TENANT_BURST_SHARE to fail to bound throughput; if this "
        "fails, rate-based fairness was added -- close the finding"
    )
    # Nobody is fully starved, which is what keeps this P2 rather than P0.
    assert all(v > 0 for v in polite_done.values()), (
        "polite tenants must still make progress"
    )


async def test_attack4b_quota_inflight_table_is_unbounded() -> None:
    """`SharedQuota._inflight` has no cap and no reaping.

    Every acquire whose release is skipped -- any crash between the two --
    leaves a permanent entry keyed by an attacker-chosen string.
    """
    quota = SharedQuota(rate_per_second=1e9, burst=1e9)
    n = 10_000
    tracemalloc.start()
    base = tracemalloc.take_snapshot()
    for i in range(n):
        await quota.acquire(f"leaked-tenant-{i}", cost=0.001)  # release never called
    after = tracemalloc.take_snapshot()
    kb = sum(s.size_diff for s in after.compare_to(base, "lineno")) / 1024
    tracemalloc.stop()

    print("\n  [ATTACK 4b / unbounded quota in-flight table]")
    print(f"    tenant ids acquired without release : {n}")
    print(f"    len(quota._inflight)                : {len(quota._inflight)}")
    print(f"    retained memory                     : {kb:.1f} KB")
    print(f"    stats()['tenants_inflight']         : {quota.stats()['tenants_inflight']}")
    record("attack4b_unbounded_inflight", ids=n,
           retained_entries=len(quota._inflight), retained_kb=round(kb, 1))

    assert len(quota._inflight) == n, (
        "expected the in-flight table to grow without bound; if this fails a "
        "cap was added -- close the finding"
    )


# ==========================================================================
# ATTACK 5 -- ENUMERATION
#   Any per-tenant value on an aggregate surface is the same leak by another
#   route. And a global counter is worse than a timer: it is noise-free.
# ==========================================================================


def test_attack5a_aggregate_stats_expose_no_tenant_ids() -> None:
    reg = TenantRegistry()
    for name in ("merck", "pfizer", "novartis"):
        reg.for_tenant(name).put({"query.term": "secret-" + name}, 1)
    stats = reg.stats()
    blob = json.dumps(stats)

    print("\n  [ATTACK 5a / stats() surface]")
    print(f"    TenantRegistry.stats() = {stats}")
    print(f"    SharedQuota.stats()    = {reg.quota.stats()}")
    record("attack5a_stats_surface", registry_stats=stats,
           quota_stats=reg.quota.stats(),
           leaks_tenant_ids=any(n in blob for n in ("merck", "pfizer", "novartis")))

    for name in ("merck", "pfizer", "novartis", "secret-"):
        assert name not in blob, f"stats() must not contain {name!r}"
    assert set(stats) == {"tenants", "entries_total", "evicted_tenants", "quota"}
    assert stats["entries_total"] == 3  # aggregate volume only


async def test_attack5b_health_endpoint_no_longer_publishes_a_query_oracle():
    """CLOSED. Was a deterministic oracle: read the counter, issue one query,
    read it again. Delta 0 meant another tenant had already asked that exact
    question; 1 meant nobody had. Worse than the timing channel -- one probe, no
    statistics, and immune to latency jitter, because jitter cannot perturb an
    integer.

    Operational counters belong on an authenticated metrics endpoint, never on
    an unauthenticated liveness probe.
    """
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as http:
        payload = http.get("/api/health").json()

    leaky = {"ctgov_requests", "ctgov_retries", "cache_hits", "cache_misses",
             "coalesced", "entries"}
    exposed = leaky & set(payload)
    assert not exposed, (
        f"/api/health must not publish upstream counters; these are a "
        f"cross-tenant query oracle: {sorted(exposed)}"
    )
    assert payload.get("status") == "ok", "liveness must still work"

def test_attack5c_mcp_tools_take_a_tenant_and_pool_only_the_quota() -> None:
    """INVERTED once the MCP surface was wired. It previously asserted the
    defect -- that 'tenant' appeared nowhere in app/mcp_server.py -- which was
    true, and was the finding.

    The MCP path had a subtler shape than the HTTP one. It opened
    CTGovClient() per call, so each call got a PRIVATE cache and nothing leaked
    between callers. What it also got was private pacing and a private token
    bucket, so N concurrent agent calls each believed they owned the whole
    upstream budget -- a 429 storm rather than a side-channel. Sharing one
    client fixes that; scoping its cache per tenant keeps the leak closed.

    Asserted structurally: the query tool must accept a tenant, and the tenant
    must never reach the wire."""
    import ast
    import inspect

    from app import mcp_server

    fn = getattr(mcp_server.ctgov_query, "fn", mcp_server.ctgov_query)
    params = inspect.signature(fn).parameters
    assert "tenant_id" in params, "MCP callers cannot identify themselves"
    assert params["tenant_id"].default is None, "tenant must stay optional"

    # Grep the CODE, not the prose: _client's docstring quotes the old
    # `CTGovClient()` pattern to explain what it replaced, and a naive substring
    # search over the source flags that explanation as the defect it describes.
    # Strip docstrings first, or the fix's own rationale fails the fix's test.
    tree = ast.parse(inspect.getsource(mcp_server))
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            node.value.value = ""
    code = ast.unparse(tree)
    assert "CTGovClient()" not in code, (
        "a per-call client gives every MCP caller its own rate budget"
    )
    # The tenant partitions a cache. It is not a filter, and must never be
    # composed into an upstream query param.
    for forbidden in ('params["tenant', "query.tenant", "filter.tenant"):
        assert forbidden not in code, f"tenant leaked into an upstream param: {forbidden}"

    print("\n  [ATTACK 5c / MCP surface]")
    print("    ctgov_query accepts tenant_id : True")
    print("    per-call CTGovClient()        : gone (quota now pooled)")
    record("attack5c_mcp_surface", tenant_param=True, per_call_client=False)


# ==========================================================================
# ATTACK 6 -- NORMALISATION BYPASS
#   Two ids that should DIFFER must not collide (that shares a cache).
#   Two ids that should MATCH must not split (that evades fairness).
# ==========================================================================


def test_attack6_normalisation_collisions_and_splits() -> None:
    n = TenantRegistry.normalise

    collisions = []
    # U+212A KELVIN SIGN lowercases to ASCII 'k' under str.lower().
    for attacker, victim in [
        ("Kacme-pharma", "kacme-pharma"),
        ("KENALOG", "kenalog"),
    ]:
        if n(attacker) == n(victim) and attacker.lower() != attacker:
            collisions.append((ascii(attacker), victim, n(attacker)))

    splits = []
    for a, b, why in [
        ("acme​", "acme", "zero-width space is not stripped"),
        ("acme.", "acme", "trailing dot is not stripped"),
        ("ａcme", "acme", "fullwidth 'a' does not fold"),
        ("İSTANBUL-BIO", "istanbul-bio", "dotted capital I folds to i + U+0307"),
        ("acme ", "acme", "EN QUAD is stripped"),  # control: should MATCH
    ]:
        if n(a) != n(b):
            splits.append((ascii(a), b, why, ascii(n(a))))

    empty = {
        "None": n(None), "empty": n(""), "spaces": n("   "),
        "tab": n("\t"), "literal_anonymous": n("anonymous"),
    }
    long_id = "a" * 1_000_000
    long_ok = len(n(long_id)) == 1_000_000

    print("\n  [ATTACK 6 / normalise() bypass]")
    print(f"    cross-tenant COLLISIONS (shared cache) : {len(collisions)}")
    for a, v, k in collisions:
        print(f"      {a} and {v!r} both normalise to {k!r}")
    print(f"    same-tenant SPLITS (fairness evasion)  : {len(splits)}")
    for a, b, why, k in splits:
        print(f"      {a} -> {k} != {n(b)!r}   ({why})")
    print(f"    empty/None all collapse to             : {sorted(set(empty.values()))}")
    print(f"    1,000,000-char id accepted verbatim    : {long_ok}")
    record("attack6_normalisation",
           collisions=[[a, v, ascii(k)] for a, v, k in collisions],
           splits=[[a, b, why] for a, b, why, _ in splits],
           collision_count=len(collisions), split_count=len(splits),
           empty_map=empty, max_id_length_enforced=not long_ok,
           anonymous_is_a_shared_bucket=len({v for v in empty.values()}) == 1)

    # INVERTED once normalise() gained NFKC + casefold. Both assertions
    # previously demanded the DEFECT, and both were correct findings:
    # str.lower() folded U+212A KELVIN SIGN onto ASCII 'k', so an attacker who
    # picked its own id landed in a victim's partition, and four unfolded
    # variants each minted a separate cache for one logical tenant.
    #
    # The two directions are not equally bad and the fix treats them
    # asymmetrically. A COLLISION reopens the side-channel this whole module
    # exists to close; a SPLIT only evades fairness accounting. So combining
    # marks are stripped ONLY for U+0307 over 'i' (redundant by construction),
    # never wholesale -- folding accents away would trade every split for a
    # collision, which is the worse failure.
    assert not collisions, f"tenant ids collide into one cache partition: {collisions}"

    # SPLITS ARE NOW EXPECTED for non-ASCII ids, and that is the finding this
    # attack actually produced. The two directions turned out to be ONE
    # mechanism: folding non-ASCII onto ASCII is what merges 'ISTANBUL-BIO'
    # with 'istanbul-bio' AND what merges U+212A + 'acme-pharma' with
    # 'kacme-pharma'. No normalisation form separates them.
    #
    # So the input domain is constrained instead: pure-ASCII ids fold normally,
    # anything containing a non-ASCII character keeps a quarantined namespace
    # and can never land on an ASCII tenant's partition. A split costs fairness
    # accounting; a collision costs the confidentiality this module exists for.
    # Trading the cheap failure for the expensive one would be backwards.
    # `splits` stores ascii(a), which is ASCII text BY CONSTRUCTION -- .isascii()
    # on it is always True and would pass every row through. The escape sequence
    # in the repr is the tell that the original was not ASCII.
    ascii_splits = [
        s for s in splits if "\\u" not in str(s[0]) and "\\x" not in str(s[0])
    ]
    assert not ascii_splits, f"an ASCII tenant mints separate partitions: {ascii_splits}"
    for a, _b, _why, key in splits:
        assert key.strip("'").startswith("u-"), (
            f"non-ASCII id {a} was folded rather than quarantined: {key}"
        )
    # Every unauthenticated caller shares one bucket, and it is guessable.
    assert len(set(empty.values())) == 1 and empty["None"] == "anonymous"
    assert empty["literal_anonymous"] == empty["None"], (
        "a caller can name itself 'anonymous' and share the unauthenticated cache"
    )
    assert long_ok, "expected no length cap on tenant ids"


# ==========================================================================
# ATTACK 7 -- MEMORY
# ==========================================================================


def test_attack7_memory_plateaus_under_id_minting() -> None:
    reg = TenantRegistry()
    tracemalloc.start()
    base = tracemalloc.take_snapshot()
    n = 10_000
    for i in range(n):
        cache = reg.for_tenant(f"minted-{i}")
        for j in range(4):
            cache.put({"query.term": f"k{j}"}, j)
    after = tracemalloc.take_snapshot()
    kb = sum(s.size_diff for s in after.compare_to(base, "lineno")) / 1024
    tracemalloc.stop()
    stats = reg.stats()

    print("\n  [ATTACK 7 / memory under 10,000 minted tenant ids]")
    print(f"    ids presented        : {n}")
    print(f"    tenants retained     : {stats['tenants']}  (MAX_TENANTS={MAX_TENANTS})")
    print(f"    entries retained     : {stats['entries_total']}")
    print(f"    tenants evicted      : {stats['evicted_tenants']}")
    print(f"    retained memory      : {kb:.1f} KB")
    record("attack7_memory", ids_presented=n, tenants_retained=stats["tenants"],
           entries_retained=stats["entries_total"],
           evicted=stats["evicted_tenants"], retained_kb=round(kb, 1))

    assert stats["tenants"] == MAX_TENANTS, "registry must plateau at MAX_TENANTS"
    assert stats["evicted_tenants"] == n - MAX_TENANTS
    assert kb < 4096, f"retained memory must plateau, saw {kb:.1f} KB"


def test_attack7b_per_tenant_entries_are_bounded() -> None:
    reg = TenantRegistry()
    cache = reg.for_tenant("flooder")
    for i in range(PER_TENANT_ENTRIES * 10):
        cache.put({"query.term": f"k{i}"}, i)
    print("\n  [ATTACK 7b / per-tenant entry cap]")
    print(f"    keys written : {PER_TENANT_ENTRIES * 10}")
    print(f"    entries held : {len(cache)}  (PER_TENANT_ENTRIES={PER_TENANT_ENTRIES})")
    record("attack7b_entry_cap", written=PER_TENANT_ENTRIES * 10, held=len(cache),
           cap=PER_TENANT_ENTRIES)
    assert len(cache) <= PER_TENANT_ENTRIES
