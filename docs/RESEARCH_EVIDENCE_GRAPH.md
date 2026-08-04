# Temporal multiplex research evidence graph

## Decision

Research evidence is no longer allowed to end as disconnected JSON, prose and
screenshots. `nodequant-research-graph` compiles the current alpha program into
one bounded, content-addressed, point-in-time projection and `/research-graph`
renders it as a read-only interactive review surface.

The graph is an index and attention surface, not a new authority. It carries
artifact paths, hashes, status, knowability time, time authority, typed edges and
deterministic review priority. It deliberately does **not** copy Sharpe, return,
price, position, risk or promotion values out of their owning artifacts.

## Current measured surface

- 146 nodes
- 150 edges
- six multiplex layers: evidence, experiment, falsification, governance,
  research and temporal
- point-in-time slider from the frozen program through the latest conservative
  2026-08-04 day-end evidence bound
- three deterministic attention modes: evidence authority, contradictions first
  and recency
- searchable graph, pan/zoom, node selection and content-hash inspector
- explicit loading, render-error and no-matching-evidence states
- current graph hash:
  `sha256:0b8241a505b2a02ad61667df581840877b3725591eabbbb9133ed4aed5717726`

The current catalog covers the negative daily ETF search, breadth-first search,
future-time packet, invalidated crypto branch, factor policy, all 15 factor
discovery trials, external replication authority, positive factor result,
before/after visual, the open execution-cost gap, the frozen Market Activity
Graph contract, and the liquid factor-proxy policy/authority/falsification/visual
chain. Trial collections remain inside their hashed parent result and
expand as graph nodes without copying their metrics. The market contract is a
governed blocker node; it does not masquerade as a live snapshot.

## Temporal and authority rules

1. A node appears only at or after `known_at_utc`.
2. Every timestamp names its authority: an artifact field, an external anchor, a
   document date, or a conservative upper bound.
3. An edge appears no earlier than both endpoint nodes.
4. Every artifact path is repository-contained, byte-bounded and SHA-256 hashed.
5. Catalog node/edge/byte limits fail closed.
6. Attention scores control node size only. They are deterministic review
   priority, not learned causal weights, confidence, or alpha.
7. The graph cannot authorize promotion, execution or capital.

The Stock AI design order remains: deterministic temporal graph, traversal,
replayable visual activity, outcome labels, simple baselines, graph-feature
baseline, then—and only if incremental value survives—a temporal GNN.

## Reproduce

```powershell
uv run --no-sync nodequant-research-graph
uv run --no-sync pytest tests/test_research_evidence_graph.py -q
cd web
npm run build
node capture-research-evidence-graph.mjs
```

Visual proof:

- `evidence/research-evidence-graph/before.png` — real HTTP 404 before the route
- `evidence/research-evidence-graph/after.png` — populated interactive surface,
  SHA-256 `ca543ad6b8ec31ce9e2660afb73cf6b1af7ed78f86aa7bcf79e44eec003960aa`
- `evidence/research-evidence-graph/comparison.png` — side-by-side before/after,
  SHA-256 `b1401831a210d91f7d4a5302bde3afdd2d1a9e0b03634f88811286f1348de617`

The interaction replay observed HTTP 200, 146/150 at full scope, one exact node
for the `Developed ex-U.S.` search, an explicit empty state for an impossible
query, one node at the earliest cutoff, and zero browser console errors.

## Open next rung

The graph currently stores research-process evidence, not a point-in-time market
entity graph. The next graph increment should add stable instrument/topic/event
identities and snapshot-versioned economic, co-activity and information edges.
Only after replay and leakage tests pass should graph features be compared with
the same target and costs against own-history, tabular-neighbor and boosted-tree
baselines.
