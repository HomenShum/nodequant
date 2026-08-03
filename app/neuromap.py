"""Interactive neuromap — the graph the agent actually traversed.

Deliberately NOT a new application, and deliberately not hand-rolled SVG.

Everything shown here already exists in a `QueryResponse`: `GraphNode.count` and
`GraphEdge.weight` are exact probe counts, and `meta.trace` already records every
probe issued. This module adds interaction and the trace-to-graph link, nothing
else.

Rendering is delegated to Cytoscape.js rather than emitting SVG and writing
pan/zoom/highlight by hand. Four things that would otherwise be custom code with
their own edge cases are library primitives:

    edge thickness by weight   ->  style mapData(weight, ...)
    hover to isolate a node    ->  .neighborhood()
    pan / zoom / fit           ->  built in
    node placement             ->  a named layout

The bundle is vendored (`app/vendor/cytoscape.min.js`) and inlined, so a
generated map opens offline with no CDN and no network access.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .models import QueryResponse, VizType

VENDOR = Path(__file__).resolve().parent / "vendor" / "cytoscape.min.js"

# One clause is a node probe; two AND-composed clauses is an edge probe.
# Matching the params structurally beats parsing the step's prose label, which
# would break silently the first time a step name is reworded.
_AREA_VALUE = re.compile(r'AREA\[(?:LeadSponsorName|InterventionName)\]"([^"]+)"')


def trace_to_graph(response: QueryResponse) -> list[dict[str, Any]]:
    """Map each trace step to the graph element it measured."""
    viz = response.visualization
    by_label = {n.label: n.id for n in viz.nodes}
    edges = {frozenset((e.source, e.target)): f"{e.source}|{e.target}" for e in viz.edges}

    steps: list[dict[str, Any]] = []
    for order, step in enumerate(response.meta.trace):
        found = _AREA_VALUE.findall(step.params.get("filter.advanced", ""))
        ids = [by_label[v] for v in found if v in by_label]
        kind: str = "other"
        target: str | None = None
        if len(ids) == 1:
            kind, target = "node", ids[0]
        elif len(ids) >= 2 and frozenset(ids[:2]) in edges:
            kind, target = "edge", edges[frozenset(ids[:2])]
        steps.append({
            "order": order,
            "step": step.step,
            "kind": kind,
            "target": target,
            "result": step.result_count,
        })
    return steps


def _elements(response: QueryResponse) -> list[dict[str, Any]]:
    viz = response.visualization
    items: list[dict[str, Any]] = []
    for node in viz.nodes:
        items.append({"data": {
            "id": node.id, "label": node.label, "kind": node.type, "count": node.count,
        }})
    known = {n.id for n in viz.nodes}
    for edge in viz.edges:
        if edge.source in known and edge.target in known:
            items.append({"data": {
                "id": f"{edge.source}|{edge.target}",
                "source": edge.source, "target": edge.target, "weight": edge.weight,
            }})
    return items


def render_neuromap(response: QueryResponse, *, bundle_src: str | None = None) -> str:
    """Self-contained interactive HTML. No CDN, no fetch, no external assets.

    `bundle_src` exists for ONE caller: the chat UI, which embeds this in an
    iframe per network response. Inlining 435 KB of Cytoscape into every message
    would put it on the SSE stream once per query; pointing at a same-origin URL
    lets the browser cache it once for the session.

    It defaults to None so the MCP and CLI paths keep inlining, because their
    promise is a file you can mail to someone and open on a plane. That promise
    is the reason the bundle is vendored at all, and a default that quietly
    broke it would be the wrong trade for a convenience the chat UI wants.
    """
    viz, meta = response.visualization, response.meta
    if viz.type is not VizType.NETWORK_GRAPH or not viz.nodes:
        return (
            "<!doctype html><meta charset=utf-8><title>Neuromap</title>"
            "<p style='font:14px/1.6 system-ui;padding:28px;max-width:60ch'>"
            "This response is not a network graph, so there is no connection map to "
            "draw. Ask something like &ldquo;show a network of sponsors and drugs for "
            "melanoma trials&rdquo;.</p>"
        )

    import json

    if bundle_src:
        # Same-origin script tag. No integrity attribute: the file is served by
        # this process from app/vendor, so there is no third party to pin.
        bundle_tag = f'<script src="{bundle_src}"></script>'
    else:
        inlined = VENDOR.read_text(encoding="utf-8") if VENDOR.exists() else ""
        bundle_tag = f"<script>{inlined}</script>"
    # Only the inlining path can fail this way. When bundle_src is set the file
    # is fetched by the browser from this process, so a missing file surfaces as
    # a 404 in the iframe rather than as a silently empty <script>.
    if not bundle_src and not VENDOR.exists():  # pragma: no cover - ships vendored
        return (
            "<!doctype html><meta charset=utf-8><title>Neuromap</title>"
            "<p style='font:14px system-ui;padding:28px'>Cytoscape bundle missing at "
            f"<code>{VENDOR}</code>. Reinstall the package.</p>"
        )

    steps = trace_to_graph(response)
    traced = sum(1 for s in steps if s["kind"] != "other")
    rows = "".join(
        f'<li data-kind="{s["kind"]}" data-target="{s["target"] or ""}">'
        f'<span>{s["step"]}</span><b>{"" if s["result"] is None else f"{s['result']:,}"}</b></li>'
        for s in steps
    )
    weights = [e.weight for e in viz.edges] or [1]
    counts = [n.count for n in viz.nodes] or [1]

    return f"""<!doctype html><meta charset=utf-8>
<title>Neuromap — {viz.title}</title>
<style>
:root{{--bg:#0e0f10;--pan:#151719;--ink:#e2e6e9;--dim:#7f868c;--line:#25292c;
 --acc:#4fd1c5;--warn:#e8a33d}}
@media(prefers-color-scheme:light){{:root{{--bg:#f7f8f8;--pan:#fff;--ink:#15181a;
 --dim:#697077;--line:#e2e5e7;--acc:#0f766e;--warn:#a86a10}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
 font:13px/1.5 ui-sans-serif,system-ui,sans-serif;font-variant-numeric:tabular-nums}}
header{{padding:14px 20px;border-bottom:1px solid var(--line)}}
h1{{font-size:16px;margin:0 0 3px}}
.sub{{color:var(--dim);font-size:12px}}
.wrap{{display:grid;grid-template-columns:1fr 330px;height:calc(100vh - 72px)}}
#cy{{width:100%;height:100%}}
aside{{border-left:1px solid var(--line);background:var(--pan);overflow:auto;padding:14px}}
h2{{font:600 11px ui-monospace,monospace;letter-spacing:.08em;color:var(--dim);
 margin:0 0 8px;text-transform:uppercase}}
ol{{list-style:none;margin:0;padding:0}}
li{{display:flex;gap:8px;padding:6px 7px;cursor:pointer;font-size:11.5px;
 border-top:1px solid var(--line);border-radius:5px}}
li:hover,li.on{{background:var(--bg)}}
li[data-kind=other]{{opacity:.45;cursor:default}}
li span{{flex:1;font-family:ui-monospace,monospace;color:var(--dim);overflow:hidden;
 text-overflow:ellipsis;white-space:nowrap}}
li b{{font-family:ui-monospace,monospace}}
.foot{{margin-top:12px;padding-top:10px;border-top:1px solid var(--line);
 color:var(--dim);font-size:11px}}
input#find{{width:100%;box-sizing:border-box;border:1px solid var(--line);
  background:var(--bg);color:var(--fg);border-radius:5px;padding:5px 8px;
  font:12px ui-monospace,monospace}}
input#find:focus{{outline:2px solid var(--acc);outline-offset:-1px}}
.hint{{display:block;margin-top:4px;font:10px ui-monospace,monospace;color:var(--dim)}}
button{{border:1px solid var(--line);background:var(--pan);color:var(--dim);
 border-radius:5px;cursor:pointer;padding:3px 9px;font-size:11px;margin-right:4px}}
</style>
<header>
  <h1>{viz.title}</h1>
  <div class=sub>{meta.denominator_note} · edge thickness = co-occurring trials ·
    node size = trials · hover a node to isolate its connections</div>
</header>
<div class=wrap>
  <div id=cy></div>
  <aside>
    <h2>Agent traversal · {traced} of {len(steps)} steps mapped</h2>
    <div style="margin-bottom:8px">
      <input id=find type=search placeholder="Find a node…" autocomplete=off
             aria-label="Find a node" aria-describedby=findhint>
      <span id=findhint class=hint></span>
    </div>
    <div style="margin-bottom:8px"><button id=fit>fit</button><button id=clr>clear</button></div>
    <ol id=tr>{rows}</ol>
    <div class=foot>Click a step to highlight the node or edge it measured. Every
      number here is a <code>totalCount</code> this run measured — none is
      model-generated.</div>
  </aside>
</div>
{bundle_tag}
<script>
// Cytoscape parses colours itself and does not resolve CSS custom properties:
// passing var(--acc) renders everything grey. Resolve to literal hex here.
const dark = matchMedia('(prefers-color-scheme: dark)').matches;
const C = dark
  ? {{ acc:'#4fd1c5', acc2:'#a58ce8', warn:'#e8a33d', ink:'#e2e6e9', dim:'#7f868c', bg:'#0e0f10' }}
  : {{ acc:'#0f766e', acc2:'#6d4fbb', warn:'#a86a10', ink:'#15181a', dim:'#697077', bg:'#f7f8f8' }};

const cy = window.cy = cytoscape({{
  container: document.getElementById('cy'),
  elements: {json.dumps(_elements(response))},
  layout: {{ name: 'cose', animate: false, nodeRepulsion: 42000,
            idealEdgeLength: 240, nodeOverlap: 40, padding: 60, randomize: false }},
  style: [
    {{ selector: 'node', style: {{
        'background-color': ele => ele.data('kind') === 'sponsor' ? C.acc2 : C.acc,
        // Library primitive: no hand-written radius maths, no divide-by-zero.
        'width':  'mapData(count, {min(counts)}, {max(counts)}, 16, 62)',
        'height': 'mapData(count, {min(counts)}, {max(counts)}, 16, 62)',
        'label': 'data(label)', 'font-size': 10, 'color': C.ink,
        'text-valign': 'bottom', 'text-margin-y': 6, 'text-wrap': 'ellipsis',
        'text-max-width': 110, 'text-background-color': C.bg,
        'text-background-opacity': 0.8, 'text-background-padding': 2,
        'min-zoomed-font-size': 7 }} }},
    {{ selector: 'edge', style: {{
        'line-color': C.acc, 'curve-style': 'bezier', 'opacity': 0.4,
        'width': 'mapData(weight, {min(weights)}, {max(weights)}, 1, 11)',
        'label': 'data(weight)', 'font-size': 9, 'color': C.dim,
        'text-background-color': C.bg, 'text-background-opacity': 0.9,
        'text-background-padding': 2 }} }},
    {{ selector: '.hot', style: {{
        'background-color': C.warn, 'line-color': C.warn, 'opacity': 1, 'z-index': 99 }} }},
    {{ selector: '.faded', style: {{ 'opacity': 0.07, 'text-opacity': 0.07 }} }},
  ],
}});

const clear = () => cy.elements().removeClass('hot faded');

// Node finder: incremental / filter-as-you-type over node labels.
//
// Reuses the SAME .hot/.faded classes the hover interaction uses, rather than
// adding a second highlight mechanism. Two mechanisms would eventually disagree
// about what "selected" means, and the reader would see a node dimmed by one
// and lit by the other.
//
// Substring match, not fuzzy: these are sponsor and drug names a reader is
// reading off the screen. Fuzzy matching helps when you are guessing at a name
// and hurts when you are copying one -- "NCI" should not surface "Nivolumab".
const findEl = document.getElementById('find');
const hint = document.getElementById('findhint');

function runFind(raw) {{
  const q = raw.trim().toLowerCase();
  if (!q) {{ clear(); hint.textContent = ''; return; }}
  const hits = cy.nodes().filter(n => (n.data('label') || '').toLowerCase().includes(q));
  if (!hits.length) {{
    // Fade everything: an empty result must LOOK empty. Leaving the graph fully
    // lit reads as "no filter applied", which is the opposite of the truth.
    cy.elements().addClass('faded').removeClass('hot');
    hint.textContent = 'no match';
    return;
  }}
  // Matched nodes plus the edges between them, so a match set of two connected
  // sponsors still shows the relationship that makes the pair interesting.
  const keep = hits.union(hits.edgesWith(hits));
  cy.elements().addClass('faded').removeClass('hot');
  keep.removeClass('faded').addClass('hot');
  hint.textContent = hits.length + (hits.length === 1 ? ' node' : ' nodes');
}}

findEl.addEventListener('input', e => runFind(e.target.value));
findEl.addEventListener('keydown', e => {{
  if (e.key === 'Escape') {{ findEl.value = ''; runFind(''); }}
  // Enter frames the matches. Typing narrows; committing navigates.
  if (e.key === 'Enter') {{
    const lit = cy.elements('.hot');
    if (lit.length) cy.animate({{ fit: {{ eles: lit, padding: 60 }}, duration: 250 }});
  }}
}});

// Type-to-search: start typing anywhere and the box takes over, so the finder
// does not require noticing it first.
document.addEventListener('keydown', e => {{
  if (e.target === findEl || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key.length === 1 && /[a-zA-Z0-9]/.test(e.key)) {{
    findEl.focus(); findEl.value += e.key; runFind(findEl.value); e.preventDefault();
  }}
}});

// .neighborhood() is why this is three lines instead of a set-membership loop.
cy.on('mouseover', 'node', e => {{
  const keep = e.target.closedNeighborhood();
  cy.elements().addClass('faded');
  keep.removeClass('faded').addClass('hot');
}});
cy.on('mouseout', 'node', clear);

document.querySelectorAll('#tr li').forEach(li => {{
  if (li.dataset.kind === 'other') return;
  li.onclick = () => {{
    clear();
    document.querySelectorAll('#tr li').forEach(o => o.classList.remove('on'));
    li.classList.add('on');
    const el = cy.getElementById(li.dataset.target);
    if (!el || el.empty()) return;
    cy.elements().addClass('faded');
    const show = li.dataset.kind === 'edge' ? el.union(el.connectedNodes()) : el;
    show.removeClass('faded').addClass('hot');
  }};
}});
cy.ready(() => cy.fit(undefined, 50));
document.getElementById('fit').onclick = () => cy.fit(undefined, 40);
document.getElementById('clr').onclick = () => {{
  clear();
  document.querySelectorAll('#tr li').forEach(o => o.classList.remove('on'));
}};
</script>"""
