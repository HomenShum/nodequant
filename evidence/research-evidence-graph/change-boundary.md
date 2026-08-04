# Research evidence graph UI boundary

- Route: `/research-graph`
- Viewport: 1440 × 1000
- Theme: browser default / light
- Session: anonymous local Next.js development server
- Fixture: repository research artifacts as of 2026-08-04

## CHANGE A — research graph page

- Current state: the route returns HTTP 404 and renders the framework's empty
  not-found page.
- Expected after state: a deterministic, read-only, interactive temporal
  multiplex evidence graph compiled from content-addressed repository artifacts.
- Trigger: navigate directly to `/research-graph`.
- Data source: a checked-in projection generated from a bounded checked-in
  catalog; values remain owned by linked source artifacts.
- Empty state: explicit `NO MATCHING EVIDENCE` after filters remove all nodes.
- Loading state: server-rendered shell plus a visible `LOADING GRAPH` canvas
  state until Cytoscape is ready.
- Error state: explicit projection-load failure; never a blank canvas.
- Populated state: node and edge counts, temporal cutoff, multiplex layer
  filters, attention mode, search, graph canvas, selected-node evidence receipt,
  authority/limitation disclosure, and a direct next action.
- Overflow: graph canvas pans/zooms; receipts wrap; controls wrap on narrow
  screens rather than widening the page.
- Responsive: two-column workspace above 960px; stacked canvas and inspector
  below it.
- Interaction: search focuses matching nodes; layer toggles filter; time slider
  removes nodes not yet knowable; attention mode sizes nodes by deterministic
  review priority; clicking a node opens its receipt.

## Out of scope

- The existing `/` Proof Kernel chat surface.
- Authoritative metric calculation or promotion decisions.
- Model-authored graph nodes, edge weights, facts, or trading actions.
- Neural prediction; this is the deterministic visualization/replay rung that
  precedes model fitting.
