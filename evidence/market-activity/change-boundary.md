# UI change boundary

Route: `/`
Viewport: `1728x1117`
Theme: light
Session: anonymous local browser, empty thread
Trigger: open the canonical Next.js chat route
Fixture/input: compiled `web/app/market-map/data.json`; no chat message submitted

## CHANGE A · Global market map entry

Current: The header exposes the sidebar trigger, `NodeQuant > Proof Kernel v0`, and the theme toggle. There is no route to the market map.

Expected: A keyboard-reachable `Market Map` link is visible before the theme toggle and navigates to `/market-map`.

Data source: Static application route; no runtime quantity.

| State | Expected visible result |
| --- | --- |
| Empty | Link remains visible when the chat has no messages. |
| Loading | Link is independent of chat loading and remains usable. |
| Error | Link remains usable when the chat API fails. |
| Populated | Link remains visible after messages render. |
| Overflow | Header controls stay on one line at the proof viewport. |
| Responsive | Text may compact, but the market-map action remains named and reachable. |

## CHANGE B · Market map launch preview

Current: The empty-thread welcome contains only “How can I help you today?” above the composer, so the compiled market map is absent from the primary surface.

Expected: The empty-thread welcome contains an inspectable market-map preview derived from the compiled snapshot, an honest `STRUCTURE ONLY` boundary, source date, coverage counts, and a primary link to the full interactive map. It must not imply live flow, return, or causality.

Data source: Server-derived summary of `web/app/market-map/data.json`; the client receives sector labels, weights, counts, source date, claim boundary, and manifest hash rather than a second authored dataset.

| State | Expected visible result |
| --- | --- |
| Empty | Preview, provenance boundary, and `Explore full market map` action appear above the composer. |
| Loading | Startup’s placeholder empty thread preserves the same preview without a fabricated run status. |
| Error | Chat API errors render in the message surface; the preview does not claim the chat succeeded. |
| Populated | Preview leaves the viewport when messages populate; existing message, receipt, and recovery behavior is unchanged. |
| Overflow | Eleven sector bands fit the declared card and long labels truncate without covering actions. |
| Responsive | Preview stacks its metadata and keeps the action visible; no horizontal page overflow. |

Out of scope: thread list behavior, composer behavior, message rendering, proof-panel vocabulary, market-map internals, research-graph internals, theme behavior.

Unchanged assertion: the same empty thread, composer, sidebar contents, breadcrumb, theme, and chat send path remain available; only the two boxed regions change.
