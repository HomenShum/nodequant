# NodeQuant U.S. Large-Cap Market Activity Graph

## Decision

The trader-facing showcase is a multiscale, temporal **market activity graph**,
not a force-directed ticker cloud and not a claim that secondary-market volume
is literal capital flow.

The central promise is:

> See where activity is concentrated, how that concentration is changing, which
> instruments and events are connected to it, and what evidence produced every
> visible activation.

V0 declares a bounded universe: S&P 500 constituents, 11 GICS sectors, selected
sector/industry ETFs, broad benchmarks, a bounded cross-asset context ring, and
point-in-time events/sources. “Entire U.S. market” is prohibited.

## Four quantities that must never be collapsed

1. **Trading activity** — shares, contracts, or dollar turnover.
2. **Directional pressure** — an explicitly named buyer/seller-initiation proxy.
3. **Fund flow** — creations, redemptions, subscriptions, or withdrawals from a
   qualified source.
4. **Rotation pressure** — a calculated relative-performance, breadth, activity,
   and exposure change.

A relative-volume spike may not be labeled money flowing into a sector. Options
volume may not be labeled directional demand without opening/closing, spread,
hedge, and trade-direction evidence.

## Stable multiscale spine

```text
U.S. large-cap market
→ sector
→ industry group / tradable proxy
→ company / security
→ option or ETF exposure
→ event / topic
→ original source record
```

The structural layout is stable. Events and attention activate around entities;
they do not move the permanent taxonomy. A bounded outer ring carries rates,
dollar, gold, oil, volatility, small-cap, growth, value, and crypto-risk proxies.

## Multiplex layers

- **Structural:** membership, representation, ETF holding, declared exposure.
- **Market activity:** co-activity, rolling correlation, lead/lag, relative state.
- **Attention/event:** source publication, mention, topic activation, sector link.
- **Options:** expiry activity, IV state, skew state, term structure.
- **Research:** visibly non-authoritative “may affect” hypotheses and falsifiers.

Solid edges are observed relationships. Dashed edges are calculated inference.
Hypothesis edges are visually quarantined and can never authorize risk or trade.

## Five views and one synchronized river

1. Market Activity — structure, relative turnover, freshness, relative return,
   breadth, and newly crossed materiality thresholds.
2. Rotation Pressure — relative return, breadth, activity, acceleration, pressure
   proxies, and qualified fund-flow data as separate components.
3. Options Activity — volume, premium, put/call, IV, skew, term structure, and
   temporally valid open-interest change.
4. Attention and Events — deduplicated mention bursts, source diversity,
   novelty, centrality, and provenance; never treated as market activity.
5. Combined Investigation — transparent component ranking, never a mysterious
   composite score.

The Sector Rotation River carries 11 time bands below the graph. Width represents
share of included dollar turnover, position represents rotation-pressure rank,
tone represents sector-relative return, and markers show event activation or
community migration. Scrubbing reconstructs the graph at the same timestamp.

## First authoritative invariant

> At every timestamp, each displayed market, sector, industry, and instrument
> metric must reduce from the same canonical source events, reconcile exactly
> across hierarchy levels, and replay to the same graph hash.

The snapshot must fail closed unless it proves:

- declared-universe manifest and exact coverage counts;
- zero unmapped constituents and duplicate sector memberships;
- zero market→sector and sector→constituent turnover residual;
- zero breadth/return aggregation mismatches;
- zero post-as-of events and snapshot/community/migration replay mismatches;
- zero activations without metric receipts, events without sources, or agent
  paths with invalid references;
- browser graph hash match and zero console errors.

Counts must be populated from the real universe and corpus, never hardcoded for
appearance.

## Seven-scene demo acceptance

1. Stable large-cap opening map.
2. Agent focuses where 15-minute activity is concentrating.
3. Search MU and traverse market→technology→semiconductors→MU plus two hops.
4. Focus deterministic contradictions across price, breadth, ETF activity,
   options confirmation, events, and rates-sensitive peers.
5. Scrub community migrations with ghosted prior position.
6. Open a deterministic historical intraday analog with subsequent outcome.
7. Disconnect/reconnect and replay the same timestamp to identical events,
   nodes, edges, activity values, communities, graph hash, and agent references.

## Honest current status

The structural v0 surface is now real and replayable. It is compiled from the
official IVV holdings response dated 2026-08-03 and renders 504 equity rows in 11
sector regions with zero unmapped rows and zero duplicate tickers. The official
equity weights reconcile to 99.76673%; cash, collateral, money-market, and futures
rows are excluded. The deterministic manifest is
`sha256:f128a79c538f59a71ac01c8f67049276795ada35acabcb19103ec07ebc9b13ea`.

This clears **structural coverage only**. The map does not yet have authoritative
underlying trades/quotes, ETF issuance/redemption, signed-order-flow proxies,
options trades/quotes/open interest, or a point-in-time event/source corpus.
Accordingly, Activity, Rotation, Options, and Attention remain visibly locked as
`FEED REQUIRED`. The UI makes no live volume, money-flow, return, causal, or alpha
claim.

Browser acceptance on a fresh production build requires HTTP 200, 504 mounted
and unique ticker tiles, exact `MU` search to one focused Micron tile, Health Care
isolation to 60 holdings, restored all-sector state, and zero console errors.

The research order remains deterministic snapshot → traversal → replayable
visual → historical outcomes → simple baselines → graph-feature baseline →
temporal GNN only after incremental point-in-time value survives costs and
latency.

## Structural source

- [Official iShares Core S&P 500 ETF holdings](https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf)
- Raw provider response receipt: `evidence/market-activity/raw/ivv-holdings-2026-08-03.receipt.json`
- Deterministic projection: `web/app/market-map/data.json`
- Browser proof: `evidence/market-activity/browser-proof.json`

## Primary external definitions

- [Investment Company Institute ETF issuance/redemption statistics](https://www.ici.org/research/stats/etf/etfs_04_25)
- [Cboe U.S. options market statistics](https://www.cboe.com/markets/us/options/market-statistics/daily/)
- [S&P Global GICS](https://www.spglobal.com/spdji/en/landing/topic/gics/)
