"use client";

/**
 * Data-part renderers — the validated component vocabulary the backend
 * composes from.
 *
 * The backend decides which of these appear for a given question (that is the
 * generative composition), but it may only choose from this set, and each one
 * renders a payload the response schema guarantees. A model cannot invent a
 * panel type, and the one panel that IS model-authored (`freeform`) is visibly
 * marked and forbidden from containing figures.
 */

import { makeAssistantDataUI } from "@assistant-ui/react";
import { useState } from "react";

type Citation = { nct_id: string; excerpt: string; field: string };
type Datum = {
  label: string;
  count: number;
  series: string | null;
  is_unbucketed: boolean;
  citation_total: number;
  citations: Citation[];
};

type CitationGroup = { label: string; returned: number; total: number; citations: Citation[] };
type GNode = { id: string; label: string; type: string; count: number };
type GEdge = { source: string; target: string; weight: number };
type Step = { step: string; url: string; params: Record<string, string>; result_count: number | null };

const nf = new Intl.NumberFormat("en-US");

/* ------------------------------------------------------------------ chart */

export const DistributionUI = makeAssistantDataUI<{
  title: string;
  viz_type: string;
  data: Datum[];
  denominator_note: string;
  sort: string | null;
  units: string | null;
}>({
  name: "distribution",
  render: ({ data }) => {
    const max = Math.max(...data.data.map((d: Datum) => d.count), 1);
    const series = [...new Set(data.data.map((d: Datum) => d.series).filter(Boolean))] as string[];
    return (
      <section className="my-3 rounded-lg border border-border bg-card p-4">
        <header className="mb-3">
          <h3 className="text-sm font-semibold">{data.title}</h3>
          {/* rule-2: the denominator sits beside the mark, always */}
          <p className="text-muted-foreground text-xs">{data.denominator_note}</p>
        </header>

        {series.length > 0 && (
          <div className="mb-2 flex gap-3 text-[11px] text-muted-foreground">
            {series.map((s, i) => (
              <span key={s} className="flex items-center gap-1.5">
                <i
                  className="inline-block size-2 rounded-[2px]"
                  style={{ background: i === 0 ? "var(--ts-accent)" : "var(--ts-accent-2)" }}
                />
                {s}
              </span>
            ))}
          </div>
        )}

        <div className="space-y-1.5">
          {data.data.map((d: Datum, i: number) => {
            const idx = d.series ? series.indexOf(d.series) : -1;
            return (
              <div key={i} className="grid grid-cols-[7rem_1fr_3.5rem] items-center gap-2.5">
                <span
                  className="truncate text-right font-mono text-[11px] text-muted-foreground"
                  title={d.series ? `${d.series} — ${d.label}` : d.label}
                >
                  {d.label}
                </span>
                <div className="h-4 rounded-sm bg-muted">
                  <div
                    className="h-4 rounded-sm"
                    style={{
                      width: `${Math.max((d.count / max) * 100, 0.6)}%`,
                      // rule-1: reserved grey + hatch for "no value recorded",
                      // so it is distinguishable without relying on colour
                      background: d.is_unbucketed
                        ? "repeating-linear-gradient(45deg,var(--ts-null),var(--ts-null) 3px,transparent 3px,transparent 6px), var(--ts-null)"
                        : idx === 1
                          ? "var(--ts-accent-2)"
                          : "var(--ts-accent)",
                    }}
                  />
                </div>
                <span className="text-right font-mono text-[11px] font-semibold tabular-nums">
                  {nf.format(d.count)}
                </span>
              </div>
            );
          })}
        </div>

        <p className="mt-3 border-t border-border pt-2 text-[11px] text-muted-foreground">
          sorted {data.sort ?? "—"} · units: {data.units ?? "trials"}
          {data.data.some((d: Datum) => d.is_unbucketed) && " · hatched grey = no value recorded"}
        </p>
      </section>
    );
  },
});

/* --------------------------------------------------- reconciliation panel */

export const ReconciliationUI = makeAssistantDataUI<{
  total_matching: number;
  bucket_sum: number;
  unbucketed_count: number | null;
  buckets_are_exclusive: boolean;
  warnings: string[];
}>({
  name: "reconciliation",
  render: ({ data }) => (
    <section className="my-3 rounded-lg border-l-[3px] border border-[var(--ts-warn)] bg-[var(--ts-warn-bg)] p-4">
      <header className="mb-2 flex items-baseline gap-2">
        <h3 className="text-sm font-semibold">Reconciliation</h3>
        <span className="ml-auto rounded-full border border-[var(--ts-warn)] px-2 py-0.5 font-mono text-[10px] text-[var(--ts-warn)]">
          auto-added
        </span>
      </header>
      <dl className="mb-2 space-y-0.5">
        {[
          ["matched", data.total_matching],
          ["bucket sum", data.bucket_sum],
          ...(data.unbucketed_count != null
            ? ([["in no bucket", data.unbucketed_count]] as [string, number][])
            : []),
        ].map(([k, v]) => (
          <div key={k} className="flex justify-between border-t border-border/60 py-1 text-xs first:border-0">
            <dt className="text-muted-foreground">{k}</dt>
            <dd className="font-mono font-semibold tabular-nums">{nf.format(v as number)}</dd>
          </div>
        ))}
      </dl>
      {data.warnings.map((w: string, i: number) => (
        <p key={i} className="mt-1.5 text-[11px] leading-relaxed text-[var(--ts-warn)]">
          {w}
        </p>
      ))}
      <p className="mt-2 border-t border-border/60 pt-2 font-mono text-[10px] text-[var(--ts-warn)]">
        buckets_are_exclusive = {String(data.buckets_are_exclusive)} → part-of-whole marks blocked
      </p>
    </section>
  ),
});

/* ------------------------------------------------------------- citations */

export const CitationsUI = makeAssistantDataUI<{
  groups: { label: string; returned: number; total: number; citations: Citation[] }[];
}>({
  name: "citations",
  render: ({ data }) => (
    <section className="my-3 rounded-lg border border-border bg-card p-4">
      <h3 className="mb-2 text-sm font-semibold">Sources</h3>
      {data.groups.map((g: CitationGroup) => (
        <div key={g.label} className="mb-3 last:mb-0">
          {/* rule-6: truncation announces itself */}
          <p className="mb-1 font-mono text-[11px] text-muted-foreground">
            {g.label} — showing {g.returned} of {nf.format(g.total)}
          </p>
          {g.citations.map((c: Citation) => (
            <div key={c.nct_id} className="border-t border-border py-1.5 first:border-0">
              <div className="flex items-center gap-2">
                <a
                  className="font-mono text-[11px] font-semibold text-[var(--ts-accent)] underline"
                  href={`https://clinicaltrials.gov/study/${c.nct_id}`}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {c.nct_id}
                </a>
                {/* rule-8: name the field, so a reader sees WHY it supports the datum */}
                <span className="rounded border border-border px-1.5 font-mono text-[10px] text-muted-foreground">
                  {c.field}
                </span>
              </div>
              <p className="mt-0.5 text-[11px] italic text-muted-foreground">“{c.excerpt}”</p>
            </div>
          ))}
        </div>
      ))}
      <p className="mt-2 border-t border-border pt-2 text-[10px] text-muted-foreground">
        Excerpts are copied verbatim from the API response. Never generated.
      </p>
    </section>
  ),
});

/* ----------------------------------------------------------- network graph */

export const NetworkUI = makeAssistantDataUI<{
  title: string;
  nodes: { id: string; label: string; type: string; count: number }[];
  edges: { source: string; target: string; weight: number }[];
  returned: number;
  total: number;
  denominator_note: string;
  neuromap_html?: string;
}>({
  name: "network",
  render: ({ data }) => {
    const [showMap, setShowMap] = useState(false);
    const byId = Object.fromEntries(data.nodes.map((n: GNode) => [n.id, n.label]));
    const maxW = Math.max(...data.edges.map((e: GEdge) => e.weight), 1);
    return (
      <section className="my-3 rounded-lg border border-border bg-card p-4">
        <header className="mb-3">
          <h3 className="text-sm font-semibold">{data.title}</h3>
          <p className="text-muted-foreground text-xs">{data.denominator_note}</p>
        </header>

        {/* The ranked list below is the default because it is scannable and
            answers "which pairs are biggest" without any interaction. The map
            answers a different question -- how the entities connect -- so it is
            offered rather than substituted. Mounted only when opened: an iframe
            per network response would run a layout the reader never looked at. */}
        {data.neuromap_html && (
          <div className="mb-3">
            <button
              type="button"
              onClick={() => setShowMap((v) => !v)}
              className="rounded-md border border-border px-2 py-1 text-[11px] hover:bg-accent"
              aria-expanded={showMap}
            >
              {showMap ? "Hide" : "Show"} interactive map
            </button>
            {showMap && (
              <iframe
                title="Interactive sponsor-drug map"
                data-testid="neuromap-frame"
                srcDoc={data.neuromap_html}
                // Scripts yes -- Cytoscape must run. same-origin NO: the frame
                // needs nothing from this document, and withholding it means a
                // bug in the vendored bundle cannot reach the chat session.
                sandbox="allow-scripts"
                className="mt-2 h-[460px] w-full rounded-md border border-border bg-white"
              />
            )}
          </div>
        )}

        <div className="mb-3 flex flex-wrap gap-1.5">
          {data.nodes.map((n: GNode) => (
            <span
              key={n.id}
              className="flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-[11px]"
            >
              <i
                className="inline-block size-2 rounded-full"
                style={{
                  background: n.type === "sponsor" ? "var(--ts-sponsor)" : "var(--ts-accent)",
                }}
              />
              {n.label}
              {/* rule-5: magnitude in the label, never encoded as area alone */}
              <b className="font-mono tabular-nums text-muted-foreground">{nf.format(n.count)}</b>
            </span>
          ))}
        </div>

        <p className="mb-1 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
          co-occurring trials
        </p>
        <div className="space-y-1">
          {data.edges.slice(0, 12).map((e: GEdge, i: number) => (
            <div key={i} className="grid grid-cols-[1fr_5rem_2.5rem] items-center gap-2 text-[11px]">
              <span className="truncate text-muted-foreground">
                {byId[e.source]} <span className="opacity-50">→</span> {byId[e.target]}
              </span>
              <div className="h-2 rounded-sm bg-muted">
                <div
                  className="h-2 rounded-sm"
                  style={{ width: `${(e.weight / maxW) * 100}%`, background: "var(--ts-accent)" }}
                />
              </div>
              <span className="text-right font-mono font-semibold tabular-nums">{e.weight}</span>
            </div>
          ))}
        </div>
        <p className="mt-2 border-t border-border pt-2 text-[10px] text-muted-foreground">
          showing {data.returned} nodes, {data.edges.length} edges · entity shortlist is sampled
        </p>
      </section>
    );
  },
});

/* ---------------------------------------------------------------- trace */

export const TraceUI = makeAssistantDataUI<{
  interpretation: string;
  steps: { step: string; url: string; params: Record<string, string>; result_count: number | null }[];
  assumptions: string[];
  filters_applied: Record<string, string>;
}>({
  name: "trace",
  render: ({ data }) => (
    <details className="my-3 rounded-lg border border-border bg-card">
      {/* rule-9: collapsed by default — the trace is for the sceptic */}
      <summary className="cursor-pointer px-4 py-2.5 text-xs font-semibold">
        {data.steps.length} API calls · every number replayable
      </summary>
      <div className="px-4 pb-3">
        {data.steps.map((s: Step, i: number) => (
          <div key={i} className="grid grid-cols-[8rem_1fr_3.5rem] gap-2 border-t border-border py-1.5">
            <span className="truncate text-[11px] text-muted-foreground">{s.step}</span>
            {/* rule-7: the LITERAL request, so it can be replayed */}
            <code className="truncate rounded border border-border bg-muted px-1.5 font-mono text-[10px]">
              {new URLSearchParams(s.params).toString()}
            </code>
            <span className="text-right font-mono text-[11px] font-semibold tabular-nums">
              {s.result_count ?? "—"}
            </span>
          </div>
        ))}
        {data.assumptions.length > 0 && (
          <div className="mt-2 border-t border-border pt-2">
            <p className="mb-1 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
              assumptions
            </p>
            <ul className="list-disc pl-4 text-[11px] text-muted-foreground">
              {data.assumptions.map((a: string, i: number) => (
                <li key={i}>{a}</li>
              ))}
            </ul>
          </div>
        )}
        <p className="mt-2 border-t border-border pt-2 text-[10px] text-muted-foreground">
          Every count above came from <code>totalCount</code>. Paste any query into a browser to
          reproduce it.
        </p>
      </div>
    </details>
  ),
});

/* ------------------------------------------- freeform (escape hatch) */

export const FreeformUI = makeAssistantDataUI<{ title: string; body: string }>({
  name: "freeform",
  render: ({ data }) => (
    <section className="my-3 rounded-lg border border-dashed border-[var(--ts-gen)] bg-[var(--ts-gen-bg)] p-4">
      <header className="mb-2 flex items-baseline gap-2">
        <h3 className="text-sm font-semibold">{data.title}</h3>
        <span className="ml-auto rounded-full border border-[var(--ts-gen)] px-2 py-0.5 font-mono text-[10px] text-[var(--ts-gen)]">
          generated · unvalidated · no figures
        </span>
      </header>
      <p className="text-xs leading-relaxed">{data.body}</p>
      <p className="mt-2 border-t border-dashed border-[var(--ts-gen)] pt-2 text-[10px] text-[var(--ts-gen)]">
        No primitive covered this. Written by the model, not measured from the API — and barred
        from containing figures, so it cannot misstate a count.
      </p>
    </section>
  ),
});

export const TrialScopePanels = () => (
  <>
    <DistributionUI />
    <ReconciliationUI />
    <CitationsUI />
    <NetworkUI />
    <TraceUI />
    <FreeformUI />
  </>
);
