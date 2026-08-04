"use client";

import cytoscape, { type Core } from "cytoscape";
import { useEffect, useMemo, useRef, useState } from "react";
import graph from "./data.json";

type AttentionMode = "evidence" | "contradiction" | "recency";
type Node = (typeof graph.nodes)[number];

const COLORS: Record<string, string> = {
  governance: "#7d66c7",
  experiment: "#4f7cac",
  temporal: "#2a9d8f",
  evidence: "#27835f",
  falsification: "#d45d59",
  research: "#b7791f",
};

export function ResearchGraphView() {
  const [container, setContainer] = useState<HTMLDivElement | null>(null);
  const cy = useRef<Core | null>(null);
  const [mode, setMode] = useState<AttentionMode>("evidence");
  const [query, setQuery] = useState("");
  const [layers, setLayers] = useState<string[]>(graph.layers);
  const [cutoffIndex, setCutoffIndex] = useState(graph.time_cutoffs.length - 1);
  const [selected, setSelected] = useState<Node | null>(
    graph.nodes.find((node) => node.key === "factor-result") ?? graph.nodes[0],
  );
  const [ready, setReady] = useState(false);
  const [renderError, setRenderError] = useState<string | null>(null);
  const cutoff = graph.time_cutoffs[cutoffIndex];

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const nodes = graph.nodes.filter(
      (node) =>
        node.known_at_utc <= cutoff &&
        layers.includes(node.layer) &&
        (!needle ||
          `${node.label} ${node.kind} ${node.status} ${node.summary}`
            .toLowerCase()
            .includes(needle)),
    );
    const keys = new Set(nodes.map((node) => node.key));
    const edges = graph.edges.filter(
      (edge) =>
        edge.known_at_utc <= cutoff &&
        layers.includes(edge.layer) &&
        keys.has(edge.source) &&
        keys.has(edge.target),
    );
    return { nodes, edges };
  }, [cutoff, layers, query]);

  useEffect(() => {
    if (!selected || !visible.nodes.some((node) => node.key === selected.key)) {
      setSelected(visible.nodes[0] ?? null);
    }
  }, [selected, visible.nodes]);

  useEffect(() => {
    if (!container) return;
    setReady(false);
    setRenderError(null);
    cy.current?.destroy();
    const target = container;
    let instance: Core | null = null;
    const frame = requestAnimationFrame(() => {
      try {
        instance = cytoscape({
          container: target,
          elements: [
            ...visible.nodes.map((node) => ({
              data: {
                id: node.key,
                label: node.label,
                color: COLORS[node.layer] ?? "#64748b",
                score: node.attention[mode],
                status: node.status,
              },
            })),
            ...visible.edges.map((edge) => ({
              data: {
                id: edge.id,
                source: edge.source,
                target: edge.target,
                label: edge.relation,
                color: COLORS[edge.layer] ?? "#94a3b8",
              },
            })),
          ],
          style: [
            {
              selector: "node",
              style: {
                "background-color": "data(color)",
                label: "data(label)",
                width: "mapData(score, 1, 5, 24, 58)",
                height: "mapData(score, 1, 5, 24, 58)",
                color: "#172126",
                "font-size": "9px",
                "text-wrap": "wrap",
                "text-max-width": "92px",
                "text-valign": "bottom",
                "text-margin-y": 7,
                "border-width": 2,
                "border-color": "#ffffff",
              },
            },
            {
              selector: "edge",
              style: {
                width: 1.5,
                "line-color": "data(color)",
                "target-arrow-color": "data(color)",
                "target-arrow-shape": "triangle",
                "curve-style": "bezier",
                label: "data(label)",
                "font-size": "7px",
                color: "#647177",
                "text-background-color": "#ffffff",
                "text-background-opacity": 0.85,
                "text-background-padding": "2px",
              },
            },
            { selector: ":selected", style: { "border-color": "#111827", "border-width": 5 } },
          ],
          layout: {
            name: "cose",
            animate: false,
            randomize: false,
            nodeRepulsion: () => 7000,
            idealEdgeLength: () => 110,
            numIter: 600,
          },
          wheelSensitivity: 0.25,
        });
        instance.on("tap", "node", (event) => {
          const node = graph.nodes.find((candidate) => candidate.key === event.target.id());
          if (node) setSelected(node);
        });
        if (selected && visible.nodes.some((node) => node.key === selected.key)) {
          instance.$id(selected.key).select();
        }
        cy.current = instance;
        setReady(true);
      } catch (error) {
        setRenderError(error instanceof Error ? error.message : "graph renderer failed");
      }
    });
    return () => {
      cancelAnimationFrame(frame);
      instance?.destroy();
    };
  }, [container, visible, mode, selected]);

  const toggleLayer = (layer: string) =>
    setLayers((current) =>
      current.includes(layer) ? current.filter((value) => value !== layer) : [...current, layer],
    );

  return (
    <main
      className="min-h-screen bg-[#e9e7df] p-4 text-[#172126] md:p-7"
      data-testid="research-graph-page"
    >
      <section className="mx-auto max-w-[1540px] border border-[#c8c5bb] bg-white p-5 shadow-xl md:p-8">
        <header className="mb-5 flex flex-wrap items-end gap-4 border-b border-[#cbd1ce] pb-5">
          <div className="max-w-4xl">
            <p className="font-mono text-[10px] font-bold tracking-[0.18em] text-[#607078]">
              NODEQUANT / TEMPORAL MULTIPLEX EVIDENCE
            </p>
            <h1 className="mt-2 text-3xl font-bold">
              The research loop is now traversable, not buried.
            </h1>
            <p className="mt-2 text-sm text-[#56636a]">
              Point-in-time nodes, typed evidence edges, deterministic attention, and
              content-addressed receipts. Measurements stay in their owning artifacts.
            </p>
          </div>
          <div className="ml-auto flex gap-2">
            <a
              href="/market-map"
              className="border border-[#263238] bg-[#263238] px-3 py-2 font-mono text-[10px] font-bold text-white"
            >
              U.S. MARKET MAP
            </a>
            <a
              href="/"
              className="border border-[#263238] px-3 py-2 font-mono text-[10px] font-bold"
            >
              PROOF KERNEL
            </a>
          </div>
        </header>

        <div className="mb-4 grid gap-3 md:grid-cols-4">
          <label className="text-xs font-semibold">
            SEARCH
            <input
              aria-label="Search evidence"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="factor, invalidated, prospective…"
              className="mt-1 w-full border border-[#aeb7b4] px-3 py-2 font-normal"
            />
          </label>
          <label className="text-xs font-semibold">
            ATTENTION
            <select
              aria-label="Attention mode"
              value={mode}
              onChange={(e) => setMode(e.target.value as AttentionMode)}
              className="mt-1 w-full border border-[#aeb7b4] px-3 py-2 font-normal"
            >
              <option value="evidence">Evidence authority</option>
              <option value="contradiction">Contradictions first</option>
              <option value="recency">Recent decisions</option>
            </select>
          </label>
          <label className="text-xs font-semibold md:col-span-2">
            KNOWABLE BY <span className="font-mono font-normal">{cutoff}</span>
            <input
              aria-label="Temporal cutoff"
              type="range"
              min={0}
              max={graph.time_cutoffs.length - 1}
              value={cutoffIndex}
              onChange={(e) => setCutoffIndex(Number(e.target.value))}
              className="mt-3 w-full accent-[#27835f]"
            />
          </label>
        </div>
        <div className="mb-4 flex flex-wrap gap-2" aria-label="Multiplex layers">
          {graph.layers.map((layer) => (
            <button
              key={layer}
              onClick={() => toggleLayer(layer)}
              aria-pressed={layers.includes(layer)}
              className={`border px-2 py-1 font-mono text-[10px] font-bold ${layers.includes(layer) ? "text-white" : "bg-white text-[#647177]"}`}
              style={
                layers.includes(layer)
                  ? { backgroundColor: COLORS[layer], borderColor: COLORS[layer] }
                  : { borderColor: COLORS[layer] }
              }
            >
              {layer.toUpperCase()}
            </button>
          ))}
        </div>

        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_360px]">
          <section
            className="relative min-h-[650px] overflow-hidden border border-[#bfc7c4] bg-[#f8faf9]"
            data-testid="research-graph-canvas"
          >
            {!ready && !renderError && visible.nodes.length > 0 && (
              <div className="absolute inset-0 z-10 grid place-items-center font-mono text-xs">
                LOADING GRAPH
              </div>
            )}
            {renderError && (
              <div className="absolute inset-0 z-10 grid place-items-center bg-[#fff3ee] p-8 text-center">
                <div>
                  <b>GRAPH RENDER FAILED</b>
                  <p className="mt-2 font-mono text-xs">{renderError}</p>
                </div>
              </div>
            )}
            {visible.nodes.length === 0 ? (
              <div key="empty" className="grid min-h-[650px] place-items-center text-center">
                <div>
                  <b>NO MATCHING EVIDENCE</b>
                  <p className="mt-2 text-xs text-[#647177]">
                    Restore a layer, move the cutoff, or clear search.
                  </p>
                </div>
              </div>
            ) : (
              <div key="graph" ref={setContainer} className="h-[650px] w-full" />
            )}
            <div
              className="absolute bottom-3 left-3 border border-[#c9cfcd] bg-white/95 px-3 py-2 font-mono text-[10px]"
              data-testid="graph-counts"
            >
              {visible.nodes.length} NODES / {visible.edges.length} EDGES
            </div>
          </section>

          <aside
            className="border border-[#bfc7c4] bg-[#f8faf9] p-4"
            data-testid="research-graph-inspector"
          >
            {selected ? (
              <>
                <div className="mb-3 flex items-center gap-2">
                  <span
                    className="h-3 w-3 rounded-full"
                    style={{ backgroundColor: COLORS[selected.layer] }}
                  />
                  <span className="font-mono text-[10px] font-bold">
                    {selected.kind.toUpperCase()} / {selected.status}
                  </span>
                </div>
                <h2 className="text-xl font-bold">{selected.label}</h2>
                <p className="mt-3 text-sm leading-6 text-[#4f5d63]">{selected.summary}</p>
                <dl className="mt-5 grid gap-3 text-xs">
                  <div>
                    <dt className="font-bold">KNOWABLE AT</dt>
                    <dd className="break-all font-mono">{selected.known_at_utc}</dd>
                    <dd className="text-[#6b767b]">{selected.time_authority}</dd>
                  </div>
                  <div>
                    <dt className="font-bold">AUTHORITY</dt>
                    <dd className="font-mono">{selected.authority}</dd>
                  </div>
                  <div>
                    <dt className="font-bold">ARTIFACT</dt>
                    <dd className="break-all font-mono">{selected.artifact.path}</dd>
                    <dd className="mt-1 break-all font-mono text-[9px]">
                      {selected.artifact.sha256}
                    </dd>
                  </div>
                  <div>
                    <dt className="font-bold">CONTENT NODE</dt>
                    <dd className="break-all font-mono text-[9px]">{selected.id}</dd>
                  </div>
                </dl>
                <button
                  onClick={() => {
                    setQuery("");
                    setLayers(graph.layers);
                    setCutoffIndex(graph.time_cutoffs.length - 1);
                  }}
                  className="mt-6 w-full bg-[#263238] px-3 py-2 font-mono text-[10px] font-bold text-white"
                >
                  RESET / CONTINUE REVIEW
                </button>
              </>
            ) : (
              <p>Select a node to inspect its receipt.</p>
            )}
          </aside>
        </div>
        <footer className="mt-4 grid gap-3 border-t border-[#cbd1ce] pt-4 text-[11px] text-[#56636a] md:grid-cols-2">
          <p>
            <b>Authority boundary:</b> {graph.claim_boundary.replaceAll("_", " ")}.
          </p>
          <p>
            <b>Graph receipt:</b> <span className="break-all font-mono">{graph.graph_hash}</span>
          </p>
        </footer>
      </section>
    </main>
  );
}
