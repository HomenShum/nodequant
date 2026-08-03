"""A second, independent ClinicalTrials.gov count client.

This exists so the ground truth is not produced by the code being scored.
`app/ctgov.py` is the system under test; if its filter composition is wrong,
golden counts generated with it would be wrong in exactly the same way and
the eval would pass on broken data. So this module re-implements the one
primitive the eval needs — "how many studies match this literal param dict" —
in about forty lines, with no shared code path.

It is intentionally dumber than the production client: no Filter object, no
merge semantics, no bucket vocabulary. Callers hand it a fully-formed param
dict and get back a `totalCount`.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx

STUDIES_URL = "https://clinicaltrials.gov/api/v2/studies"

# The eval issues thousands of probes across a full run, and during scoring it
# runs *alongside* the system under test, which is issuing its own. Two clients
# share one undocumented upstream budget, so this one is paced more
# conservatively than production and backs the whole schedule off on a 429
# rather than only the request that got one. First attempt at production's
# settings exhausted five retries on a 429 twenty probes into generation.
MAX_CONCURRENT = 2
MIN_INTERVAL_S = 0.35
MAX_RETRIES = 8
BACKOFF_CAP_S = 15.0
THROTTLE_COOLDOWN_S = 2.5
RETRYABLE = {429, 500, 502, 503, 504}


class ProbeError(RuntimeError):
    """The API did not return a usable totalCount."""


class Prober:
    """Counts studies matching a literal ClinicalTrials.gov param dict."""

    def __init__(self, client: httpx.AsyncClient | None = None, timeout: float = 30.0):
        self._client = client
        self._owns = client is None
        self._timeout = timeout
        self._sem = asyncio.Semaphore(MAX_CONCURRENT)
        self._lock = asyncio.Lock()
        self._next_slot = 0.0
        self.request_count = 0
        self.retry_count = 0

    async def __aenter__(self) -> "Prober":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout, headers={"Accept": "application/json"}
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _pace(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + MIN_INTERVAL_S
        if wait > 0:
            await asyncio.sleep(wait)

    async def _cooldown(self, seconds: float) -> None:
        """Push the shared schedule out so every pending probe waits too.

        A 429 is information about a budget shared by all in-flight requests,
        not about the one that happened to receive it.
        """
        async with self._lock:
            loop = asyncio.get_running_loop()
            self._next_slot = max(self._next_slot, loop.time()) + seconds

    async def _get(self, params: dict[str, str]) -> dict[str, Any]:
        assert self._client is not None, "Prober must be used as an async context manager"
        detail = ""
        for attempt in range(MAX_RETRIES):
            async with self._sem:
                await self._pace()
                self.request_count += 1
                try:
                    response = await self._client.get(STUDIES_URL, params=params)
                except httpx.RequestError as exc:
                    detail = f"transport error: {exc}"
                    response = None
            if response is not None:
                if response.status_code == 200:
                    return response.json()
                if response.status_code not in RETRYABLE:
                    raise ProbeError(
                        f"HTTP {response.status_code}: {response.text[:200]} for {params}"
                    )
                detail = f"HTTP {response.status_code}"
            if attempt + 1 >= MAX_RETRIES:
                break
            self.retry_count += 1
            # Full jitter, so a burst that collectively tripped the limit does
            # not retry in lockstep and trip it again.
            delay = random.uniform(0, min(BACKOFF_CAP_S, 0.5 * (2**attempt)))
            if response is not None:
                header = response.headers.get("Retry-After")
                if header:
                    try:
                        delay = max(delay, min(float(header), BACKOFF_CAP_S))
                    except ValueError:
                        pass
                if response.status_code == 429:
                    await self._cooldown(THROTTLE_COOLDOWN_S)
            await asyncio.sleep(delay)
        raise ProbeError(f"probe failed after {MAX_RETRIES} attempts ({detail}) for {params}")

    async def count(self, params: dict[str, str]) -> int:
        """Exact number of studies matching `params`."""
        payload = await self._get(
            {**params, "countTotal": "true", "pageSize": "1", "fields": "NCTId"}
        )
        total = payload.get("totalCount")
        if not isinstance(total, int):
            raise ProbeError(f"no integer totalCount in response for {params}")
        return total

    async def count_many(self, named: dict[str, dict[str, str]]) -> dict[str, int]:
        keys = list(named)
        values = await asyncio.gather(*(self.count(named[k]) for k in keys))
        return dict(zip(keys, values))
