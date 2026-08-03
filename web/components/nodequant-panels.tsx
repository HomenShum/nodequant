"use client";

/**
 * NodeQuant panel renderers — the closed vocabulary the backend composes from.
 *
 * The backend decides which of these appear for a given loop stage; it may only
 * choose from this set, and each renders a payload the response schema
 * guarantees. A model cannot invent a panel type, and no panel here accepts a
 * number a model authored.
 *
 * Proof Kernel v0 ships exactly one: `decision-packet`.
 *
 * This is a trust surface. The reviewer decides here whether to believe the
 * system, so three rules hold and are worth stating because each is easy to
 * violate by accident:
 *
 *   1. No motion. Nothing animates toward a verdict.
 *   2. NOT_RUN is never styled like PASS. An unrun check is rendered as its own
 *      state, because the failure mode that matters is a reader skimming a wall
 *      of neutral rows and reading absence as approval.
 *   3. The verdict is a named checklist, not a score. A single composite number
 *      invites deference; eight named checks stay falsifiable one at a time.
 */

import { makeAssistantDataUI } from "@assistant-ui/react";

import { formatUsd } from "@/lib/money.mjs";

type CheckStatus = "PASS" | "FAIL" | "NOT_RUN";

type Check = {
  check_id: string;
  label: string;
  status: CheckStatus;
  detail: string;
};

type Receipt = {
  probe_id: string;
  source: string;
  retrieved_at_utc: string;
};

type Fact = {
  fact_id: string;
  label: string;
  value: number | string;
  unit: string | null;
  known_at_utc: string;
  receipt: Receipt | null;
};

type Rule = {
  rule_id: string;
  label: string;
  lhs: string;
  op: string;
  rhs: string | number;
  result: boolean;
};

type DecisionPacketData = {
  packet_id: string;
  packet_hash: string;
  strategy: { id: string; version: string; config_hash: string };
  signal: {
    signal_id: string;
    symbol: string;
    direction: "long" | "short";
    decision_at_utc: string;
    earliest_execution_at_utc: string;
  };
  rules: Rule[];
  facts: Fact[];
  verification: {
    status: CheckStatus;
    checks: Check[];
    assurance_headline: string;
    assurance: {
      verifier_integrity: CheckStatus;
      market_data_conformance: CheckStatus;
      semantic_independence: "STRONG" | "LIMITED" | "NOT_RUN";
      evidence_class: string;
    };
  };
};

const clock = (iso: string) =>
  new Date(iso).toISOString().replace("T", " ").replace(".000Z", "Z");

/** Three states, three treatments. NOT_RUN deliberately reads as unresolved
 *  rather than benign — dashed and muted, never the resolved-pass colour. */
const STATUS_STYLE: Record<CheckStatus, string> = {
  PASS: "border-[var(--ts-accent)] text-[var(--ts-accent)]",
  FAIL: "border-[var(--ts-warn)] text-[var(--ts-warn)]",
  NOT_RUN: "border-dashed border-[var(--ts-null)] text-[var(--ts-null)]",
};

const STATUS_MARK: Record<CheckStatus, string> = {
  PASS: "PASS",
  FAIL: "FAIL",
  NOT_RUN: "NOT RUN",
};

export const DecisionPacketUI = makeAssistantDataUI<DecisionPacketData>({
  name: "decision-packet",
  render: ({ data }) => {
    // `data` arrives loosely typed from the data-part runtime, so callback
    // params are annotated explicitly here — same convention as the other panels.
    const v: DecisionPacketData["verification"] = data.verification;
    const verdict = v.status as CheckStatus;
    const unresolved = v.checks.filter((c: Check) => c.status !== "PASS").length;

    return (
      <section
        className="my-3 rounded-lg border border-border bg-card p-4"
        data-testid="decision-packet"
        data-verdict={v.status}
        data-packet-hash={data.packet_hash}
      >
        <header className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h3 className="text-sm font-semibold">
            {data.signal.symbol} {data.signal.direction} · {data.signal.signal_id}
          </h3>
          {/* Scoped, never a bare PASS. "PASS" alone reads as "this is true of
              the market"; what was actually established is that the verifier is
              sound over a synthetic corpus, with a differential — not
              independent — replay. The headline is computed server-side so the
              renderer cannot invent a friendlier one. */}
          <span
            className={`rounded-full border px-2 py-0.5 font-mono text-[10px] ${STATUS_STYLE[verdict]}`}
            data-testid="decision-packet-verdict"
          >
            {v.assurance_headline ?? STATUS_MARK[verdict]}
          </span>
          <span className="text-muted-foreground ml-auto font-mono text-[10px]">
            {data.strategy.id}@{data.strategy.version}
          </span>
        </header>

        {/* The verdict, as eight named checks. Deliberately above the data:
            whether to believe the numbers is the prior question. */}
        <ol className="mb-4 space-y-1" data-testid="decision-packet-checks">
          {v.checks.map((c: Check) => (
            <li
              key={c.check_id}
              className="flex items-baseline gap-2 text-xs"
              data-check={c.check_id}
              data-status={c.status}
            >
              <span
                className={`w-16 shrink-0 rounded border px-1 text-center font-mono text-[9px] ${STATUS_STYLE[c.status]}`}
              >
                {STATUS_MARK[c.status]}
              </span>
              <span className="flex-1">{c.label}</span>
              {c.detail && (
                <span className="text-muted-foreground font-mono text-[10px]">
                  {c.detail}
                </span>
              )}
            </li>
          ))}
        </ol>

        {/* What class of claim the verdict supports. Kept adjacent to the
            checklist because a reader who sees seven green rows will otherwise
            generalise from them, and two of these three are not green. */}
        {v.assurance && (
          <dl
            className="text-muted-foreground mb-4 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[10px]"
            data-testid="decision-packet-assurance"
            data-evidence-class={v.assurance.evidence_class}
            data-semantic-independence={v.assurance.semantic_independence}
          >
            <dt>corpus</dt>
            <dd className="font-mono">{v.assurance.evidence_class}</dd>
            <dt>market-data conformance</dt>
            <dd className="font-mono">{v.assurance.market_data_conformance}</dd>
            <dt>semantic independence</dt>
            <dd className="font-mono">{v.assurance.semantic_independence}</dd>
          </dl>
        )}

        {unresolved > 0 && (
          <p
            className="mb-4 border-l-2 border-[var(--ts-warn)] pl-2 text-xs"
            data-testid="decision-packet-unresolved"
          >
            {unresolved} of {v.checks.length} checks did not pass. This packet is
            not evidence of anything until they do.
          </p>
        )}

        <div className="mb-4">
          <h4 className="text-muted-foreground mb-1 text-[11px] font-semibold uppercase tracking-wide">
            Rules
          </h4>
          <ul className="space-y-1">
            {data.rules.map((r: Rule) => (
              <li
                key={r.rule_id}
                className="flex items-baseline gap-2 text-xs"
                data-rule={r.rule_id}
                data-result={String(r.result)}
              >
                <span className="font-mono text-[10px]">{r.result ? "true" : "false"}</span>
                <span className="flex-1">{r.label}</span>
                {/* The comparison, not a list of citations. A reader can check
                    this against the facts table below without trusting the
                    boolean -- which is the whole reason `result` stopped being
                    a free-standing claim. */}
                <span className="text-muted-foreground font-mono text-[10px]">
                  {r.lhs} {r.op} {r.rhs}
                </span>
              </li>
            ))}
          </ul>
        </div>

        <div>
          <h4 className="text-muted-foreground mb-1 text-[11px] font-semibold uppercase tracking-wide">
            Facts
          </h4>
          {/* Wide content scrolls inside its own container rather than the page. */}
          <div className="overflow-x-auto">
            <table className="w-full text-xs" data-testid="decision-packet-facts">
              <thead className="text-muted-foreground text-[10px] uppercase">
                <tr>
                  <th className="py-1 pr-3 text-left font-medium">Fact</th>
                  <th className="py-1 pr-3 text-right font-medium">Value</th>
                  <th className="py-1 pr-3 text-left font-medium">Knowable at</th>
                  <th className="py-1 text-left font-medium">Probe</th>
                </tr>
              </thead>
              <tbody>
                {data.facts.map((f: Fact) => (
                  <tr key={f.fact_id} className="border-border/50 border-t" data-fact={f.fact_id}>
                    <td className="py-1 pr-3">{f.label}</td>
                    <td className="py-1 pr-3 text-right font-mono tabular-nums">
                      {f.value}
                      {f.unit ? ` ${f.unit}` : ""}
                    </td>
                    <td className="text-muted-foreground py-1 pr-3 font-mono text-[10px]">
                      {clock(f.known_at_utc)}
                    </td>
                    <td className="py-1 font-mono text-[10px]">
                      {/* A value with no receipt is called out in place. It is
                          the one thing a reader must not have to infer. */}
                      {f.receipt ? (
                        f.receipt.probe_id
                      ) : (
                        <span className="text-[var(--ts-warn)]">no receipt</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <footer className="text-muted-foreground mt-3 border-t border-border pt-2 font-mono text-[10px]">
          <div>
            decided {clock(data.signal.decision_at_utc)} · executable{" "}
            {clock(data.signal.earliest_execution_at_utc)}
          </div>
          <div className="break-all">{data.packet_hash}</div>
        </footer>
      </section>
    );
  },
});

/* ----------------------------------------------------- backtest review */

type Blocker = { code: string; detail: string };

type BacktestReviewData = {
  strategy: { id: string; version: string; config_hash: string };
  promotion: { target: string; policy_hash: string; verdict: string; blockers: Blocker[] };
  receipt: Record<string, string | number | boolean>;
  summary: Record<string, number>;
  curve: {
    at_utc: string;
    equity_micros: number;
    drawdown_micros: number;
    through_event_seq: number;
  }[];
};

// Single source of truth for money rendering; see lib/money.mjs for the
// invariant that a non-zero value never renders as zero, and its self-check.
const usd = formatUsd;

/** Equity and drawdown as two SVG polylines.
 *
 *  ponytail: no chart library. This is a static curve from a typed array, and a
 *  polyline is ~15 lines against ~370KB for ECharts. Reach for a real chart the
 *  moment this needs zoom, crosshair, or linked panning — not before. */
function Curve({ points }: { points: BacktestReviewData["curve"] }) {
  if (points.length < 2) return null;
  const W = 640;
  const H = 120;
  const eq = points.map((p) => p.equity_micros);
  const lo = Math.min(...eq);
  const hi = Math.max(...eq);
  const span = hi - lo || 1;

  // X is TIME, not index. Index-spacing rendered a 30-bar hold and a
  // zero-second gap between a mark and its fill at the same width, so the
  // curve's shape was not a function of time — a chart that misstates when
  // something happened is as wrong as one that misstates how much.
  const t = points.map((p) => Date.parse(p.at_utc));
  const t0 = t[0];
  const tSpan = t[t.length - 1] - t0 || 1;
  const x = (i: number) => ((t[i] - t0) / tSpan) * W;
  const y = (v: number) => H - ((v - lo) / span) * H;

  const equityPath = points.map((p, i) => `${x(i)},${y(p.equity_micros)}`).join(" ");
  const ddLo = Math.min(...points.map((p) => p.drawdown_micros));
  const ddPath = points
    .map((p, i) => `${x(i)},${40 - (p.drawdown_micros / (ddLo || -1)) * 40}`)
    .join(" ");

  return (
    <div className="overflow-x-auto">
      <svg
        viewBox={`0 0 ${W} ${H + 56}`}
        className="w-full"
        role="img"
        aria-label="Equity curve with drawdown"
        data-testid="backtest-curve"
      >
        <polyline
          points={equityPath}
          fill="none"
          stroke="var(--ts-accent)"
          strokeWidth="1.5"
        />
        <g transform={`translate(0 ${H + 8})`}>
          <polyline
            points={ddPath}
            fill="none"
            stroke="var(--ts-warn)"
            strokeWidth="1"
          />
        </g>
      </svg>
    </div>
  );
}

export const BacktestReviewUI = makeAssistantDataUI<BacktestReviewData>({
  name: "backtest-review",
  render: ({ data }) => {
    const promo = data.promotion;
    const blocked = promo.verdict !== "PROMOTABLE";
    return (
      <section
        className="my-3 rounded-lg border border-border bg-card p-4"
        data-testid="backtest-review"
        data-verdict={promo.verdict}
        data-blockers={String(promo.blockers?.length ?? 0)}
      >
        <header className="mb-3 flex flex-wrap items-baseline gap-x-3">
          <h3 className="text-sm font-semibold">Backtest review</h3>
          {/* The gate first. A curve shown first reads as the result; the gate
              is the result. */}
          <span
            className={`rounded-full border px-2 py-0.5 font-mono text-[10px] ${
              blocked ? STATUS_STYLE.FAIL : STATUS_STYLE.PASS
            }`}
            data-testid="promotion-verdict"
          >
            {promo.verdict} → {promo.target}
          </span>
          <span className="text-muted-foreground ml-auto font-mono text-[10px]">
            {data.strategy.id}@{data.strategy.version}
          </span>
        </header>

        {blocked && (
          <ul className="mb-4 space-y-1" data-testid="promotion-blockers">
            {promo.blockers.map((b: Blocker) => (
              <li
                key={b.code}
                className="border-l-2 border-[var(--ts-warn)] pl-2 text-xs"
                data-blocker={b.code}
              >
                <span className="font-mono text-[10px]">{b.code}</span>
                <p className="text-muted-foreground">{b.detail}</p>
              </li>
            ))}
          </ul>
        )}

        <Curve points={data.curve} />

        <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[11px]">
          <dt className="text-muted-foreground">final equity</dt>
          <dd className="font-mono tabular-nums">
            {usd(data.summary.final_equity_micros)}
          </dd>
          <dt className="text-muted-foreground">realised P&amp;L</dt>
          <dd className="font-mono tabular-nums">
            {usd(data.summary.realised_pnl_micros)}
          </dd>
          <dt className="text-muted-foreground">max drawdown</dt>
          <dd className="font-mono tabular-nums">
            {usd(data.summary.max_drawdown_micros)}
          </dd>
          <dt className="text-muted-foreground">fees</dt>
          <dd className="font-mono tabular-nums">
            {usd(data.summary.cumulative_fees_micros)}
          </dd>
        </dl>

        <footer className="text-muted-foreground mt-3 border-t border-border pt-2 font-mono text-[10px]">
          <div>
            curve matched {String(data.receipt.full_curve_matched)} ·
            unauthorized fills {String(data.receipt.unauthorized_fills)} · early
            entries {String(data.receipt.early_entries)} · price mismatches{" "}
            {String(data.receipt.price_mismatches)} · {data.receipt.event_count} events
          </div>
          <div className="break-all">policy {promo.policy_hash}</div>
        </footer>
      </section>
    );
  },
});

export const NodeQuantPanels = () => (
  <>
    <DecisionPacketUI />
    <BacktestReviewUI />
  </>
);
