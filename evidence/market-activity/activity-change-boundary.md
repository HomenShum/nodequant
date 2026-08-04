# UI change boundary — first qualified activity observation

Route: `/market-map`
Viewport: `1728x1117`
Theme: dark fixed market-map theme
Session: anonymous local browser, all sectors, NVDA selected
Trigger: open the market map, then select Activity Snapshot
Fixture/input: `web/app/market-map/activity.json` compiled from the write-once Nasdaq screener observation

## CHANGE A · Activity mode and receipt

Current: `ACTIVITY FEED REQUIRED` is disabled and the header only describes structural coverage.

Expected: `ACTIVITY SNAPSHOT` is enabled, switches deterministically between Structure and Activity, names 503/503 eligible coverage plus one inactive structural residual, and states that the observation is provider-reported share volume—not flow, relative volume, or provider-replayable history.

## CHANGE B · Activity treemap encoding

Current: tile fill encodes sector identity while area encodes official IVV weight.

Expected: area remains structural weight in both modes. Activity mode adds a measured, legend-bound intensity encoding from `reported_share_volume`; it does not move tiles, infer direction, or turn absence into zero. HOLX remains a visibly structural zero-weight residual but is marked inactive and excluded from activity aggregation.

## CHANGE C · Instrument activity proof

Current: inspector exposes only structural weight and fund market value.

Expected: Activity mode exposes the selected ticker’s provider symbol, reported share volume, last sale, percentage change, observation time, exact raw receipt hash, and the non-promotion claim boundary. Structure mode remains unchanged.

## CHANGE D · Sector activity distribution

Current: the river repeats structural portfolio weights and declares no temporal claim.

Expected: Activity mode shows each sector’s exact share of included reported share volume, with the market total and zero aggregation residual. Structure mode preserves the existing weight river.

| State | Expected visible result |
| --- | --- |
| Empty | Not applicable; the write-once snapshot has exact eligible coverage or the compiler emits no output. |
| Loading | No fake spinner; the static content-addressed snapshot is present at page load. |
| Error | A missing/malformed symbol, false receipt, future-dated source, duplicate, or oversized body prevents compilation and preserves the previous artifact. |
| Populated | 503 eligible holdings observed across 11 sectors; one inactive HOLX structural residual disclosed. |
| Overflow | Long source/hash/boundary text wraps within the inspector; treemap and controls stay inside the viewport. |
| Responsive | Mode action, honesty boundary, selected-ticker facts, and source receipt remain reachable at mobile width. |

Out of scope: structural area geometry, search semantics, sector filtering, research graph, chat behavior, options/rotation/attention modes, any fund-flow or directional-pressure claim.

Unchanged assertion: the same 504 provider structural rows, 11 sector regions, portfolio-weight geometry, ticker search, sector filter, selected-ticker behavior, and structural receipt remain available. No tile is repositioned by the activity observation.
