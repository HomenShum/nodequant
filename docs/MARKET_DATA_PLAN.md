# Market-data qualification plan

## Decision

Use `awesome-systematic-trading` as a library index, not as evidence or a data
license. The promotion corpus requires a byte-retained provider export with both
event and receive timestamps, a point-in-time corporate-action stream, and an
independent halt source. Until those inputs exist locally, market-data
conformance remains `NOT_RUN`.

No licensed raw data belongs in this public repository. Local provider files go
under ignored `market-data/raw/`; derived caches go under ignored
`market-data/cache/`. Only bounded manifests, hashes, schemas, aggregate
mismatch counts, and reproducible commands may be committed.

## Qualified source stack

| Need | Primary source | Independent check | Why it is required |
|---|---|---|---|
| Trades and executable top of book | Databento US equities `trades` plus `MBP-1` or `TBBO` | Venue sample/replay where licensed | Databento documents `ts_event`, `ts_recv`, venue/publisher identity and book actions. OHLCV alone cannot prove late prints, quote crossing, or executable spread. |
| Revisions and corrections | Raw DBN event stream retained by retrieval batch | Re-fetch the same batch locator and compare exact bytes | The conformance gate must distinguish a historical record from a later revised view. |
| Splits and other adjustments | Databento point-in-time corporate actions / adjustment factors | Issuer or exchange notice for sampled events | Corporate-action records carry update timestamps and action states; a final adjusted price series erases what was knowable at the time. |
| Halts and resumptions | Nasdaq/Cboe official halt files | Cross-check the other venue feed | A missing bar during a halt is not a feed gap; the classifier needs official halt/resume times and reason codes. |
| Session calendar | Exchange calendar applicable to the listing | Second calendar implementation | Expected intervals must exclude closed sessions, early closes and declared halts before counting gaps. |
| Options NBBO and definitions | OPRA top-of-book plus point-in-time definitions | OCC reference data | Required before option selection, quote-age, IV, Greeks or liquidity can become authoritative. |

Primary documentation:

- [Databento schema fields and market-data formats](https://databento.com/docs/schemas-and-data-formats)
- [Databento timestamp semantics](https://databento.com/docs/standards-and-conventions/common-fields-enums-types)
- [Databento point-in-time corporate actions](https://databento.com/docs/schemas-and-data-formats/corporate-actions)
- [Cboe current and historical halt notices](https://www.cboe.com/us/equities/market_statistics/halts/)
- [awesome-systematic-trading index](https://github.com/paperswithbacktest/awesome-systematic-trading)

## Catalog disposition

The linked awesome list is useful discovery input, but its entries occupy
different trust roles.  Installing a wrapper does not acquire the underlying
data rights or make a historical view point-in-time.

| Catalog entry | NodeQuant role | Admissibility decision |
|---|---|---|
| QuantConnect LEAN | Canonical event-engine comparison | **QUALIFY** behind a container/version/data/output hash boundary; engine agreement is differential evidence, not market provenance. |
| QuantStats | Return-series analytics cross-check | **QUALIFY** only after NodeQuant freezes an independently authored return series; never let it reconstruct trades from the same ledger accumulator. |
| vectorbt | Fast parameter/ablation exploration | **QUALIFY FOR RESEARCH**, never promotion authority; retain every tried configuration and recompute selected candidates in the canonical engine. |
| ArcticDB | Immutable/versioned local time-series store | **QUALIFY FOR STORAGE**; a database preserves supplied bytes but cannot certify their provider semantics. |
| yfinance / pandas-datareader / OpenBB | Low-cost exploratory bars and source discovery | **PROSPECTIVE OR DIAGNOSTIC ONLY** unless the specific upstream dataset supplies stable locators, as-known revisions, terms and provider replay. Adjusted OHLCV cannot establish late prints or executable spread. |
| Wallstreet and similar option wrappers | Interactive option exploration | **DO NOT USE AS OPTION AUTHORITY** without OPRA/OCC provenance, point-in-time definitions, quote timestamps and redistribution permission. |
| Marketstore / TectonicDB | Local event storage candidates | **STORAGE ONLY**; same provenance limitation as ArcticDB. |

This classification is intentionally asymmetric: open-source code licenses can
qualify an engine or library, while market-data admissibility depends on the
upstream data license, timestamps, revision model and replay locator. The
awesome list grants none of those merely by linking a package.

## Canonical engine run control

LEAN is the selected differential engine from the catalog, but the comparison
has not run. On 2026-08-03 Docker resolved `quantconnect/lean:latest` for
Linux/amd64 to manifest digest
`sha256:728627ca9ec4b92ad47d4647d332609a068bd9de00466007f85b4ae31e4675c4`.
The mutable tag is not admissible; a run must use the digest and preserve the
image config, algorithm source, normalised input data, engine config, stdout,
orders, trades and result JSON hashes.

The published image currently includes a compressed layer of 10,620,090,672
bytes. The local pull was deliberately stopped because the machine already had
15.89 GB of Docker images and no storage budget was authorised for another
roughly 10.6 GB layer. This is a measured execution blocker, not a failed engine
comparison. A slimmer source build or explicit Docker storage allocation is
required before the adapter can emit anything other than `NOT_RUN`.

## Required private corpus manifest

The provider adapter must issue a manifest containing:

```json
{
  "provider": "databento",
  "dataset": "XNAS.ITCH",
  "schema": ["trades", "mbp-1", "definition"],
  "symbols": ["SPY", "QQQ"],
  "start_utc": "...",
  "end_utc": "...",
  "retrieved_at_utc": "...",
  "provider_batch_locator": "...",
  "raw_file_sha256": {"...dbn.zst": "sha256:..."},
  "calendar_sha256": "sha256:...",
  "halts_sha256": "sha256:...",
  "corporate_actions_sha256": "sha256:...",
  "license_allows_local_research": true,
  "license_allows_redistribution": false
}
```

The provider name and hashes are metadata, not self-proving evidence. Promotion
must recompute file hashes, re-fetch the batch locator, and compare the replayed
bytes before it can report `provider_refetch_mismatches = 0`.

## Conformance acceptance matrix

Every row is a separate mutation-tested check. Absence remains `null/NOT_RUN`,
never zero.

| Failure class | Observation | Required mutation |
|---|---|---|
| Gap | Expected calendar interval has no event/bar outside a declared halt | Delete one normal-session interval; the gap counter increases |
| Halt | Events appear inside a halt, or the gap classifier calls a halt a feed loss | Remove/shift one halt boundary; the halt counter increases |
| Split | Pre/post prices and quantities disagree with the point-in-time action | Change one split factor/effective time; the split counter increases |
| Revision | Latest-view data replaces the as-known record without lineage | Substitute a revised record; revision counter increases |
| Late print | Event time precedes receive/order time beyond the frozen policy | Move one `ts_recv`; late-print counter increases |
| Provider replay | Re-fetched bytes differ from the frozen raw-file hash | Flip one byte; refetch counter increases |

Only all-zero observed counters over a genuine, re-fetchable corpus may produce
`REAL_BAR_REPLAY`. `EXTERNAL_BAR_SNAPSHOT` remains a useful ingestion rung and
is intentionally absent from `PROMOTABLE_EVIDENCE`.

## Procurement boundary

The remaining external step needs credentials, provider terms and licensed
files. Code can prepare and verify the seam, but it cannot accept terms, spend
money, or manufacture OPRA/venue provenance. That is an explicit external
blocker, not an engineering PASS.
