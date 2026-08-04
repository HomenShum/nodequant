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
import type { ReactNode } from "react";

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

const clock = (iso: string) => new Date(iso).toISOString().replace("T", " ").replace(".000Z", "Z");

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
                <span className="text-muted-foreground font-mono text-[10px]">{c.detail}</span>
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
            {unresolved} of {v.checks.length} checks did not pass. This packet is not evidence of
            anything until they do.
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
  promotion: {
    target: string;
    policy_hash: string;
    verdict: string;
    blockers: Blocker[];
    independent_oracle?: {
      independence: string;
      matched: boolean;
      provenance_verified: boolean;
    };
    split_discipline?: {
      manifest_hash: string;
      untagged_points: number;
      provenance_verified: boolean;
    };
  };
  receipt: Record<string, string | number | boolean>;
  summary: Record<string, number>;
  curve: {
    at_utc: string;
    equity_micros: number;
    drawdown_micros: number;
    through_event_seq: number;
    split_id: string | null;
  }[];
  splits: {
    split_id: string;
    start_utc: string;
    end_utc: string;
    promotion_eligible: boolean;
  }[];
  split_metrics: {
    split_id: string;
    point_count: number;
    net_pnl_micros: number;
    max_drawdown_micros: number;
    peak_at_utc: string;
    trough_at_utc: string;
    recovered_at_utc: string | null;
    decline_duration_seconds: number;
    recovery_duration_seconds: number | null;
    worst_drawdown_underwater_seconds: number;
    total_time_underwater_seconds: number;
    episodes_in_drawdown_interval: number;
    episode_ids_in_drawdown_interval: string[];
    had_drawdown: boolean;
    observation_censored: boolean;
    sampled_at_event_times: boolean;
  }[];
  execution_stress: {
    scenario_id: string;
    entry_delay_bars: number;
    cost_multiplier: number;
    final_equity_micros: number;
    max_drawdown_micros: number;
    fill_count: number;
    completed_episode_count: number;
    attempted_packet_count: number;
    executed_packet_count: number;
    tail_dropped_packet_count: number;
    collision_skipped_packet_count: number;
    assumption: string;
  }[];
  null_benchmarks: {
    split_id: string;
    strategy_net_pnl_micros: number;
    buy_hold_net_pnl_micros: number;
    matched_random_trial_count: number;
    matched_random_seed: number;
    matched_episode_count: number;
    random_median_pnl_micros: number;
    random_p95_pnl_micros: number;
    strategy_percentile_bps: number;
    evidence_scope: string;
  };
  statistical_adequacy: {
    split_id: string;
    episode_count: number;
    excluded_cross_boundary_episodes: number;
    evidence_scope: string;
    checks: {
      check_id: string;
      status: CheckStatus;
      observed: number | null;
      required: number | null;
      detail: string;
    }[];
  };
  robustness: {
    baseline_matches_bound_ledger: boolean;
    opened_holdout_observed_variants: number;
    access_provenance_verified: boolean;
    evidence_scope: string;
    suite_hash: string;
    result_root_hash: string;
    rows: {
      variant_id: string;
      fast_window: number;
      slow_window: number;
      hold_bars: number;
      use_uptrend: boolean;
      use_pullback: boolean;
      development_net_pnl_micros: number | null;
      validation_net_pnl_micros: number | null;
      test_net_pnl_micros: number | null;
      opened_holdout_net_pnl_micros: number | null;
      development_episode_count: number;
      validation_episode_count: number;
      test_episode_count: number;
      opened_holdout_episode_count: number;
      attempted_signals: number;
      collision_skips: number;
      tail_drops: number;
      cross_boundary_exclusions: number;
    }[];
  };
  trial_ledger: {
    trial_count: number;
    batch_complete: boolean;
    global_completeness_verified: boolean;
    pre_test_registration_verified: boolean;
    opened_holdout_observed_trial_count: number;
    robustness_suite_hash: string;
    robustness_receipt_hash: string;
    trial_ledger_hash: string;
    evidence_scope: string;
    records: {
      trial_id: string;
      definition_hash: string;
      result_hash: string;
      observed_split_count: number;
      diagnostic_only: boolean;
    }[];
  };
  walk_forward: {
    fold_plan_hash: string;
    matrix_root_hash: string;
    folds_end_before_test: boolean;
    purge_verified: boolean;
    access_provenance_verified: boolean;
    evidence_scope: string;
    rows: {
      variant_id: string;
      folds: { fold_id: string; net_pnl_micros: number | null; episode_count: number }[];
    }[];
  };
  execution_lifecycle: {
    suite_hash: string;
    runner_hash: string;
    scenario_count: number;
    expectation_mismatches: number;
    covers_partial_fill: boolean;
    covers_cancel: boolean;
    covers_reject: boolean;
    covers_expiry: boolean;
    covers_latency_breach: boolean;
    evidence_scope: string;
    observations: {
      scenario_id: string;
      outcome: string;
      terminal_event: string;
      filled_qty: number;
      remaining_qty: number;
      terminal_latency_ms: number;
      expectation_matched: boolean;
    }[];
  };
  option_readiness: {
    status: CheckStatus;
    snapshot_hash: string | null;
    definition_count: number;
    quote_count: number;
    eligible_quote_count: number | null;
    duplicate_definition_rows: number | null;
    duplicate_quote_rows: number | null;
    missing_definition_quotes: number | null;
    future_definition_mismatches: number | null;
    expired_definition_mismatches: number | null;
    future_quote_mismatches: number | null;
    receive_order_mismatches: number | null;
    stale_quote_mismatches: number | null;
    crossed_quote_mismatches: number | null;
    zero_size_mismatches: number | null;
    missing_greeks_mismatches: number | null;
    provenance_mismatches: number | null;
    provider_replay_verified: boolean;
    point_in_time_definitions_verified: boolean;
    selector_ready: boolean;
    evidence_scope: string;
  };
  source_compiler: {
    source_status: CheckStatus;
    source_hash: string | null;
    clause_root_hash: string | null;
    snapshot_root_hash: string | null;
    clause_count: number | null;
    unresolved_ambiguity_count: number | null;
    contradictory_clause_count: number | null;
    byte_spans_verified: boolean;
    human_lock_verified: boolean;
    literal_version_ready: boolean;
    reconstructed_version_ready: boolean;
    repaired_version_ready: boolean;
    evidence_scope: string;
  };
};

// Single source of truth for money rendering; see lib/money.mjs for the
// invariant that a non-zero value never renders as zero, and its self-check.
const usd = formatUsd;

/** Equity and drawdown as two SVG polylines.
 *
 *  ponytail: no chart library. This is a static curve from a typed array, and a
 *  polyline is ~15 lines against ~370KB for ECharts. Reach for a real chart the
 *  moment this needs zoom, crosshair, or linked panning — not before. */
function Curve({
  points,
  splits,
}: {
  points: BacktestReviewData["curve"];
  splits: BacktestReviewData["splits"];
}) {
  if (points.length < 2) return null;
  const W = 640;
  const H = 120;
  const firstBySplit = new Map<string, number>();
  const eq = points.map((p) => {
    const split = p.split_id ?? "UNTAGGED";
    if (!firstBySplit.has(split)) firstBySplit.set(split, p.equity_micros);
    return p.equity_micros - (firstBySplit.get(split) ?? p.equity_micros);
  });
  const lo = Math.min(...eq);
  const hi = Math.max(...eq);
  const span = hi - lo || 1;

  // X is TIME, not index. Index-spacing rendered a 30-bar hold and a
  // zero-second gap between a mark and its fill at the same width, so the
  // curve's shape was not a function of time — a chart that misstates when
  // something happened is as wrong as one that misstates how much.
  const t = points.map((p) => Date.parse(p.at_utc));
  const t0 = splits.length ? Date.parse(splits[0].start_utc) : t[0];
  const tEnd = splits.length ? Date.parse(splits[splits.length - 1].end_utc) : t[t.length - 1];
  const tSpan = tEnd - t0 || 1;
  const x = (i: number) => ((t[i] - t0) / tSpan) * W;
  const xTime = (value: string) => ((Date.parse(value) - t0) / tSpan) * W;
  const y = (v: number) => H - ((v - lo) / span) * H;

  const splitIds = [...new Set(points.map((point) => point.split_id ?? "UNTAGGED"))];
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
        {splits.map((split, index) => {
          const left = xTime(split.start_utc);
          const right = xTime(split.end_utc);
          return (
            <g key={split.split_id} data-split-band={split.split_id}>
              <rect
                x={left}
                y={0}
                width={Math.max(1, right - left)}
                height={H + 48}
                fill={split.promotion_eligible ? "var(--ts-accent)" : "currentColor"}
                opacity={split.promotion_eligible ? 0.1 : index % 2 ? 0.035 : 0.06}
              />
              <text x={left + 4} y={11} className="fill-muted-foreground text-[8px]">
                {split.split_id.replace("OPENED_", "")}
              </text>
            </g>
          );
        })}
        {splitIds.map((splitId) => {
          const segment = points
            .map((point, index) => ({ point, index, value: eq[index] }))
            .filter(({ point }) => (point.split_id ?? "UNTAGGED") === splitId);
          return (
            <polyline
              key={splitId}
              data-split-curve={splitId}
              points={segment.map(({ index, value }) => `${x(index)},${y(value)}`).join(" ")}
              fill="none"
              stroke="var(--ts-accent)"
              strokeWidth={splitId === "DEVELOPMENT" ? 1 : 1.5}
              opacity={splitId === "DEVELOPMENT" ? 0.55 : 1}
            />
          );
        })}
        <g transform={`translate(0 ${H + 8})`}>
          <polyline points={ddPath} fill="none" stroke="var(--ts-warn)" strokeWidth="1" />
        </g>
      </svg>
      <p className="text-muted-foreground mt-1 font-mono text-[9px]">
        equity rebased within each split · absolute equity retained in payload
      </p>
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

        <Curve points={data.curve} splits={data.splits ?? []} />

        <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[11px]">
          <dt className="text-muted-foreground">final equity</dt>
          <dd className="font-mono tabular-nums">{usd(data.summary.final_equity_micros)}</dd>
          <dt className="text-muted-foreground">realised P&amp;L</dt>
          <dd className="font-mono tabular-nums">{usd(data.summary.realised_pnl_micros)}</dd>
          <dt className="text-muted-foreground">max drawdown</dt>
          <dd className="font-mono tabular-nums">{usd(data.summary.max_drawdown_micros)}</dd>
          <dt className="text-muted-foreground">fees</dt>
          <dd className="font-mono tabular-nums">{usd(data.summary.cumulative_fees_micros)}</dd>
          <dt className="text-muted-foreground">modelled spread</dt>
          <dd className="font-mono tabular-nums" data-testid="modelled-spread">
            {usd(data.summary.cumulative_spread_cost_micros)}
          </dd>
          <dt className="text-muted-foreground">modelled slippage</dt>
          <dd className="font-mono tabular-nums" data-testid="modelled-slippage">
            {usd(data.summary.cumulative_slippage_cost_micros)}
          </dd>
        </dl>

        <div className="mt-4 overflow-x-auto" data-testid="split-performance">
          <table className="w-full border-collapse text-left text-[10px]">
            <caption className="text-muted-foreground mb-1 text-left font-mono">
              split-local performance · sampled at event times
            </caption>
            <thead className="text-muted-foreground border-b border-border font-mono">
              <tr>
                <th className="py-1 pr-2 font-normal">split</th>
                <th className="px-2 py-1 text-right font-normal">net P&amp;L</th>
                <th className="px-2 py-1 text-right font-normal">max DD</th>
                <th className="px-2 py-1 text-right font-normal">total underwater</th>
                <th className="px-2 py-1 text-right font-normal">episodes in interval</th>
                <th className="py-1 pl-2 font-normal">recovery</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {data.splits.map((split: BacktestReviewData["splits"][number]) => {
                const metric = data.split_metrics?.find(
                  (candidate: BacktestReviewData["split_metrics"][number]) =>
                    candidate.split_id === split.split_id,
                );
                return (
                  <tr key={split.split_id} data-split-metric={split.split_id}>
                    <td className="border-border border-b py-1 pr-2">{split.split_id}</td>
                    {metric ? (
                      <>
                        <td className="border-border border-b px-2 py-1 text-right">
                          {usd(metric.net_pnl_micros)}
                        </td>
                        <td className="border-border border-b px-2 py-1 text-right">
                          {usd(metric.max_drawdown_micros)}
                        </td>
                        <td className="border-border border-b px-2 py-1 text-right">
                          {metric.total_time_underwater_seconds}s
                        </td>
                        <td className="border-border border-b px-2 py-1 text-right">
                          {metric.episodes_in_drawdown_interval}
                        </td>
                        <td className="border-border border-b py-1 pl-2">
                          {!metric.had_drawdown
                            ? "no drawdown"
                            : metric.recovered_at_utc
                              ? "recovered"
                              : metric.observation_censored
                                ? "unrecovered (censored)"
                                : "unrecovered"}
                        </td>
                      </>
                    ) : (
                      <td className="border-border text-muted-foreground border-b py-1" colSpan={5}>
                        NOT_RUN — no observations
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        <div className="mt-4" data-testid="worst-drawdowns">
          <p className="text-muted-foreground mb-1 font-mono text-[10px]">
            worst drawdown by split · event-time sampled · recovery may be censored
          </p>
          <div className="space-y-2">
            {data.split_metrics.map((metric: BacktestReviewData["split_metrics"][number]) => (
              <section
                key={metric.split_id}
                className="border-border border-b pb-2 text-[10px]"
                data-drawdown-split={metric.split_id}
              >
                <div className="mb-1 flex items-baseline justify-between font-mono">
                  <span>{metric.split_id}</span>
                  <span>depth {usd(metric.max_drawdown_micros)}</span>
                </div>
                <dl className="grid grid-cols-2 gap-x-4 gap-y-1 md:grid-cols-4">
                  <div>
                    <dt className="text-muted-foreground">peak</dt>
                    <dd className="font-mono">{clock(metric.peak_at_utc)}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">trough</dt>
                    <dd className="font-mono">{clock(metric.trough_at_utc)}</dd>
                  </div>
                  <div className="col-span-2">
                    <dt className="text-muted-foreground">recovery</dt>
                    <dd className="font-mono">
                      {metric.recovered_at_utc
                        ? clock(metric.recovered_at_utc)
                        : metric.observation_censored
                          ? "NOT_OBSERVED (censored)"
                          : "not applicable"}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">decline</dt>
                    <dd className="font-mono">{metric.decline_duration_seconds}s</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">recovery time</dt>
                    <dd className="font-mono">
                      {metric.recovery_duration_seconds === null
                        ? metric.observation_censored
                          ? "NOT_OBSERVED"
                          : "n/a"
                        : `${metric.recovery_duration_seconds}s`}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">interval underwater</dt>
                    <dd className="font-mono">{metric.worst_drawdown_underwater_seconds}s</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">overlapping episodes</dt>
                    <dd className="font-mono">{metric.episodes_in_drawdown_interval}</dd>
                  </div>
                </dl>
                <div
                  className="text-muted-foreground mt-1 break-all font-mono text-[9px]"
                  data-drawdown-episode-ids={metric.split_id}
                >
                  spans: {metric.episode_ids_in_drawdown_interval.length
                    ? metric.episode_ids_in_drawdown_interval.join(", ")
                    : "none"}
                </div>
              </section>
            ))}
          </div>
          <div className="text-muted-foreground mt-1 font-mono text-[9px]">
            span overlap is measured; causal loss attribution is not yet built
          </div>
        </div>

        <div className="mt-4 overflow-x-auto" data-testid="execution-stress">
          <table className="w-full border-collapse text-left text-[10px]">
            <caption className="text-muted-foreground mb-1 text-left font-mono">
              execution stress · modelled counterfactual · not observed execution
            </caption>
            <thead className="text-muted-foreground border-b border-border font-mono">
              <tr>
                <th className="py-1 pr-2 font-normal">scenario</th>
                <th className="px-2 py-1 text-right font-normal">delay</th>
                <th className="px-2 py-1 text-right font-normal">cost</th>
                <th className="px-2 py-1 text-right font-normal">final equity</th>
                <th className="px-2 py-1 text-right font-normal">max DD</th>
                <th className="px-2 py-1 text-right font-normal">episodes</th>
                <th className="py-1 pl-2 text-right font-normal">packet outcome</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {data.execution_stress.map((row: BacktestReviewData["execution_stress"][number]) => (
                <tr key={row.scenario_id} data-stress-scenario={row.scenario_id}>
                  <td className="border-border border-b py-1 pr-2">{row.scenario_id}</td>
                  <td className="border-border border-b px-2 py-1 text-right">
                    {row.entry_delay_bars} bars
                  </td>
                  <td className="border-border border-b px-2 py-1 text-right">
                    {row.cost_multiplier}×
                  </td>
                  <td className="border-border border-b px-2 py-1 text-right">
                    {usd(row.final_equity_micros)}
                  </td>
                  <td className="border-border border-b px-2 py-1 text-right">
                    {usd(row.max_drawdown_micros)}
                  </td>
                  <td className="border-border border-b px-2 py-1 text-right">
                    {row.completed_episode_count}
                  </td>
                  <td className="border-border border-b py-1 pl-2 text-right">
                    {row.executed_packet_count} exec · {row.collision_skipped_packet_count}{" "}
                    collision · {row.tail_dropped_packet_count} tail
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="mt-4" data-testid="null-benchmarks">
          <p className="text-muted-foreground mb-1 font-mono text-[10px]">
            TEST nulls · synthetic timing diagnostic · not promotion evidence
          </p>
          <dl className="grid grid-cols-[1fr_auto] gap-x-4 gap-y-1 text-[10px]">
            <dt className="text-muted-foreground">strategy TEST net P&amp;L</dt>
            <dd className="font-mono text-right">
              {usd(data.null_benchmarks.strategy_net_pnl_micros)}
            </dd>
            <dt className="text-muted-foreground">buy and hold TEST net P&amp;L</dt>
            <dd className="font-mono text-right">
              {usd(data.null_benchmarks.buy_hold_net_pnl_micros)}
            </dd>
            <dt className="text-muted-foreground">
              matched random median ({data.null_benchmarks.matched_random_trial_count} trials)
            </dt>
            <dd className="font-mono text-right">
              {usd(data.null_benchmarks.random_median_pnl_micros)}
            </dd>
            <dt className="text-muted-foreground">matched random P95</dt>
            <dd className="font-mono text-right">
              {usd(data.null_benchmarks.random_p95_pnl_micros)}
            </dd>
            <dt className="text-muted-foreground">strategy empirical percentile</dt>
            <dd className="font-mono text-right">
              {(data.null_benchmarks.strategy_percentile_bps / 100).toFixed(2)}%
            </dd>
            <dt className="text-muted-foreground">matched episodes · seed</dt>
            <dd className="font-mono text-right">
              {data.null_benchmarks.matched_episode_count} ·{" "}
              {data.null_benchmarks.matched_random_seed}
            </dd>
          </dl>
        </div>

        <div className="mt-4" data-testid="statistical-adequacy">
          <p className="text-muted-foreground mb-1 font-mono text-[10px]">
            TEST statistical controls · diagnostic status · not promotion evidence
          </p>
          <div className="space-y-1">
            {data.statistical_adequacy.checks.map(
              (check: BacktestReviewData["statistical_adequacy"]["checks"][number]) => (
              <div
                key={check.check_id}
                className="border-border grid grid-cols-[auto_1fr_auto] items-start gap-x-2 border-b py-1 text-[10px]"
                data-statistical-check={check.check_id}
                data-status={check.status}
              >
                <span className={`rounded border px-1 font-mono ${STATUS_STYLE[check.status]}`}>
                  {check.status}
                </span>
                <div>
                  <div className="font-mono">{check.check_id}</div>
                  <div className="text-muted-foreground">{check.detail}</div>
                </div>
                <div className="font-mono text-right">
                  {check.observed === null ? "observed NOT_RUN" : `observed ${check.observed}`}
                  {check.required === null ? "" : ` · required ${check.required}`}
                </div>
              </div>
              ),
            )}
          </div>
          <p className="text-muted-foreground mt-1 font-mono text-[9px]">
            completed TEST episodes {data.statistical_adequacy.episode_count} · cross-boundary
            excluded {data.statistical_adequacy.excluded_cross_boundary_episodes}
          </p>
        </div>

        <div className="mt-4 overflow-x-auto" data-testid="robustness-neighborhood">
          <table className="w-full border-collapse text-left text-[10px]">
            <caption className="text-muted-foreground mb-1 text-left font-mono">
              post-hoc synthetic ablations + neighbors · self-attested access · not promotion evidence
            </caption>
            <thead className="text-muted-foreground border-b border-border font-mono">
              <tr>
                <th className="py-1 pr-2 font-normal">variant</th>
                <th className="px-1 py-1 font-normal">F/S/H</th>
                <th className="px-1 py-1 text-right font-normal">DEV</th>
                <th className="px-1 py-1 text-right font-normal">VAL</th>
                <th className="px-1 py-1 text-right font-normal">TEST</th>
                <th className="px-1 py-1 text-right font-normal">H/O</th>
                <th className="px-1 py-1 text-right font-normal">ep D/V/T/H</th>
                <th className="py-1 pl-1 text-right font-normal">A/C/T/X</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {data.robustness.rows.map(
                (row: BacktestReviewData["robustness"]["rows"][number]) => (
                  <tr key={row.variant_id} data-robustness-variant={row.variant_id}>
                    <td className="border-border border-b py-1 pr-2">{row.variant_id}</td>
                    <td className="border-border border-b px-1 py-1">
                      {row.fast_window}/{row.slow_window}/{row.hold_bars}
                    </td>
                    <td className="border-border border-b px-1 py-1 text-right">
                      {row.development_net_pnl_micros === null
                        ? "NOT_RUN"
                        : usd(row.development_net_pnl_micros)}
                    </td>
                    <td className="border-border border-b px-1 py-1 text-right">
                      {row.validation_net_pnl_micros === null
                        ? "NOT_RUN"
                        : usd(row.validation_net_pnl_micros)}
                    </td>
                    <td className="border-border border-b px-1 py-1 text-right">
                      {row.test_net_pnl_micros === null ? "NOT_RUN" : usd(row.test_net_pnl_micros)}
                    </td>
                    <td className="border-border border-b px-1 py-1 text-right">
                      {row.opened_holdout_net_pnl_micros === null
                        ? "NOT_RUN"
                        : usd(row.opened_holdout_net_pnl_micros)}
                    </td>
                    <td className="border-border border-b px-1 py-1 text-right">
                      {row.development_episode_count}/{row.validation_episode_count}/
                      {row.test_episode_count}/{row.opened_holdout_episode_count}
                    </td>
                    <td className="border-border border-b py-1 pl-1 text-right">
                      {row.attempted_signals}/{row.collision_skips}/
                      {row.tail_drops}/
                      {row.cross_boundary_exclusions}
                    </td>
                  </tr>
                ),
              )}
            </tbody>
          </table>
          <p className="text-muted-foreground mt-1 font-mono text-[9px]">
            F/S/H fast/slow/hold · ep episodes · A/C/T/X attempted/collision/tail/cross-boundary
          </p>
          <p className="text-muted-foreground mt-1 font-mono text-[9px]">
            OPENED_HOLDOUT observed for {data.robustness.opened_holdout_observed_variants}/
            {data.robustness.rows.length} variants · suite is post-hoc and unusable for selection
          </p>
          <p className="text-muted-foreground mt-1 font-mono text-[9px]">
            baseline matches bound ledger {String(data.robustness.baseline_matches_bound_ledger)} ·
            suite {data.robustness.suite_hash}
          </p>
        </div>

        <div className="mt-4" data-testid="trial-ledger">
          <p className="text-muted-foreground mb-1 font-mono text-[10px]">
            current candidate-trial batch · lineage only · not promotion evidence
          </p>
          <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-[10px] md:grid-cols-4">
            <div>
              <div className="text-muted-foreground">records preserved</div>
              <div className="font-mono">{data.trial_ledger.trial_count}/9</div>
            </div>
            <div>
              <div className="text-muted-foreground">batch complete</div>
              <div className="font-mono">{String(data.trial_ledger.batch_complete)}</div>
            </div>
            <div>
              <div className="text-muted-foreground">global completeness</div>
              <div className="font-mono">UNVERIFIED</div>
            </div>
            <div>
              <div className="text-muted-foreground">pre-test registration</div>
              <div className="font-mono">UNVERIFIED</div>
            </div>
          </div>
          <div className="mt-2 grid grid-cols-3 gap-x-2 font-mono text-[9px]">
            <div className="text-muted-foreground">trial</div>
            <div className="text-muted-foreground">definition hash</div>
            <div className="text-muted-foreground">result hash</div>
            {data.trial_ledger.records.map(
              (record: BacktestReviewData["trial_ledger"]["records"][number]) => (
                <div key={record.trial_id} className="contents">
                  <div className="border-border border-b py-0.5">
                    {record.trial_id} · {record.observed_split_count}/4 splits
                  </div>
                  <div className="border-border border-b py-0.5">
                    {record.definition_hash.slice(7, 19)}…
                  </div>
                  <div className="border-border border-b py-0.5">
                    {record.result_hash.slice(7, 19)}…
                  </div>
                </div>
              ),
            )}
          </div>
          <p className="text-muted-foreground mt-1 font-mono text-[9px]">
            OPENED_HOLDOUT observed {data.trial_ledger.opened_holdout_observed_trial_count}/
            {data.trial_ledger.trial_count} · global completeness and pre-test registration unverified
          </p>
          <p className="text-muted-foreground break-all font-mono text-[9px]">
            ledger {data.trial_ledger.trial_ledger_hash}
          </p>
        </div>

        <div className="mt-4" data-testid="walk-forward-matrix">
          <p className="text-muted-foreground mb-1 font-mono text-[10px]">
            purged walk-forward · 30 bars · all evaluation folds end before TEST · not promotion evidence
          </p>
          <div className="grid grid-cols-4 gap-x-2 font-mono text-[9px]">
            <div className="text-muted-foreground">variant</div>
            <div className="text-muted-foreground text-right">WF1</div>
            <div className="text-muted-foreground text-right">WF2</div>
            <div className="text-muted-foreground text-right">WF3</div>
            {data.walk_forward.rows.map(
              (row: BacktestReviewData["walk_forward"]["rows"][number]) => (
                <div key={row.variant_id} className="contents" data-walk-forward-variant={row.variant_id}>
                  <div className="border-border border-b py-0.5">{row.variant_id}</div>
                  {row.folds.map((fold) => (
                    <div key={fold.fold_id} className="border-border border-b py-0.5 text-right">
                      {fold.net_pnl_micros === null ? "NOT_RUN" : usd(fold.net_pnl_micros)} ·{" "}
                      {fold.episode_count}ep
                    </div>
                  ))}
                </div>
              ),
            )}
          </div>
          <p className="text-muted-foreground mt-1 font-mono text-[9px]">
            purge verified {String(data.walk_forward.purge_verified)} · access provenance self-attested ·
            PBO remains NOT_RUN
          </p>
          <p className="text-muted-foreground break-all font-mono text-[9px]">
            matrix {data.walk_forward.matrix_root_hash}
          </p>
        </div>

        <div className="mt-4" data-testid="execution-lifecycle-suite">
          <p className="text-muted-foreground mb-1 font-mono text-[10px]">
            execution lifecycle scenarios · synthetic diagnostic · not broker evidence
          </p>
          <div className="grid grid-cols-[1.4fr_1fr_.6fr_.6fr_.7fr] gap-x-2 font-mono text-[9px]">
            <div className="text-muted-foreground">scenario</div>
            <div className="text-muted-foreground">outcome</div>
            <div className="text-muted-foreground text-right">fill</div>
            <div className="text-muted-foreground text-right">remain</div>
            <div className="text-muted-foreground text-right">latency</div>
            {data.execution_lifecycle.observations.map(
              (row: BacktestReviewData["execution_lifecycle"]["observations"][number]) => (
                <div key={row.scenario_id} className="contents" data-lifecycle-scenario={row.scenario_id}>
                  <div className="border-border border-b py-0.5">{row.scenario_id}</div>
                  <div className="border-border border-b py-0.5">{row.outcome}</div>
                  <div className="border-border border-b py-0.5 text-right">{row.filled_qty}</div>
                  <div className="border-border border-b py-0.5 text-right">{row.remaining_qty}</div>
                  <div className="border-border border-b py-0.5 text-right">{row.terminal_latency_ms}ms</div>
                </div>
              ),
            )}
          </div>
          <p className="text-muted-foreground mt-1 font-mono text-[9px]">
            partial/cancel/reject/expiry/latency covered · expectation mismatches {data.execution_lifecycle.expectation_mismatches}
          </p>
          <p className="text-muted-foreground break-all font-mono text-[9px]">
            suite {data.execution_lifecycle.suite_hash}
          </p>
        </div>

        <div className="mt-4" data-testid="option-readiness">
          <p className="text-muted-foreground mb-1 font-mono text-[10px]">
            options scoreboard · point-in-time authority required · selector not run
          </p>
          <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 font-mono text-[9px] md:grid-cols-3">
            {([
              ["definitions", data.option_readiness.definition_count > 0 ? data.option_readiness.definition_count : null],
              ["quotes", data.option_readiness.quote_count > 0 ? data.option_readiness.quote_count : null],
              ["eligible", data.option_readiness.eligible_quote_count],
              ["duplicate definitions", data.option_readiness.duplicate_definition_rows],
              ["duplicate quotes", data.option_readiness.duplicate_quote_rows],
              ["missing definitions", data.option_readiness.missing_definition_quotes],
              ["future knowledge", data.option_readiness.future_definition_mismatches],
              ["expired definitions", data.option_readiness.expired_definition_mismatches],
              ["future quotes", data.option_readiness.future_quote_mismatches],
              ["receive order", data.option_readiness.receive_order_mismatches],
              ["stale quotes", data.option_readiness.stale_quote_mismatches],
              ["crossed quotes", data.option_readiness.crossed_quote_mismatches],
              ["zero size", data.option_readiness.zero_size_mismatches],
              ["missing IV/Delta", data.option_readiness.missing_greeks_mismatches],
              ["provenance", data.option_readiness.provenance_mismatches],
            ] as [string, number | null][]).map(([label, value]) => (
              <div key={label} className="flex justify-between border-border border-b py-0.5">
                <span className="text-muted-foreground">{label}</span>
                <span>{value === null ? "NOT_RUN" : value}</span>
              </div>
            ))}
          </div>
          <p className="text-muted-foreground mt-1 font-mono text-[9px]">
            provider replay {String(data.option_readiness.provider_replay_verified)} · point-in-time definitions {String(data.option_readiness.point_in_time_definitions_verified)} · selector ready {String(data.option_readiness.selector_ready)}
          </p>
          <p className="text-muted-foreground font-mono text-[9px]">
            absence is NOT_RUN, never a clean zero · OPRA/OCC corpus remains external
          </p>
        </div>

        <div className="mt-4" data-testid="source-compiler-readiness">
          <p className="text-muted-foreground mb-1 font-mono text-[10px]">
            strategy source compiler · immutable snapshot and human ambiguity lock required
          </p>
          <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 font-mono text-[9px] md:grid-cols-3">
            {([
              ["source snapshot", data.source_compiler.source_hash === null ? "NOT_RUN" : "BOUND"],
              ["literal clauses", data.source_compiler.source_hash === null ? "NOT_RUN" : String(data.source_compiler.clause_count)],
              ["byte spans", data.source_compiler.byte_spans_verified ? "VERIFIED" : "NOT_RUN"],
              ["unresolved ambiguity", data.source_compiler.unresolved_ambiguity_count === null ? "NOT_RUN" : String(data.source_compiler.unresolved_ambiguity_count)],
              ["contradictions", data.source_compiler.contradictory_clause_count === null ? "NOT_RUN" : String(data.source_compiler.contradictory_clause_count)],
              ["human lock", data.source_compiler.human_lock_verified ? "VERIFIED" : "NOT_RUN"],
              ["V0 literal", data.source_compiler.literal_version_ready ? "READY" : "NOT_RUN"],
              ["V0 reconstructed", data.source_compiler.reconstructed_version_ready ? "READY" : "NOT_RUN"],
              ["V0 repaired", data.source_compiler.repaired_version_ready ? "READY" : "NOT_RUN"],
            ] as [string, string][]).map(([label, value]) => (
              <div key={label} className="flex justify-between border-border border-b py-0.5">
                <span className="text-muted-foreground">{label}</span>
                <span>{value}</span>
              </div>
            ))}
          </div>
          <p className="text-muted-foreground mt-1 font-mono text-[9px]">
            original Notion strategy snapshot absent · compiler will preserve underspecified and contradictory clauses as variants
          </p>
        </div>

        <footer className="text-muted-foreground mt-3 border-t border-border pt-2 font-mono text-[10px]">
          <div>
            curve matched {String(data.receipt.full_curve_matched)} · unauthorized fills{" "}
            {String(data.receipt.unauthorized_fills)} · early entries{" "}
            {String(data.receipt.early_entries)} · price mismatches{" "}
            {String(data.receipt.price_mismatches)} · {data.receipt.event_count} events
          </div>
          <div data-testid="journal-reconciliation">
            journal {String(data.receipt.cross_stream_reconciliation)} · mismatches{" "}
            {String(
              (data.receipt.order_journal_receipt as Record<string, number> | undefined)
                ?.total_mismatches ?? 0,
            )}
          </div>
          {promo.independent_oracle && (
            <div data-testid="independent-oracle">
              oracle {promo.independent_oracle.independence} · matched{" "}
              {String(promo.independent_oracle.matched)} · provenance{" "}
              {promo.independent_oracle.provenance_verified ? "verified" : "self-attested"}
            </div>
          )}
          {promo.split_discipline && (
            <div data-testid="split-discipline">
              splits tagged {promo.split_discipline.untagged_points === 0 ? "true" : "false"} ·
              access provenance{" "}
              {promo.split_discipline.provenance_verified ? "verified" : "self-attested"}
            </div>
          )}
          <div className="break-all">policy {promo.policy_hash}</div>
        </footer>
      </section>
    );
  },
});

const ClosedPanel = ({ name, children, quarantined = false }: {
  name: string; children: ReactNode; quarantined?: boolean;
}) => (
  <section
    className={`my-3 rounded-lg border p-4 ${quarantined ? "border-dashed border-[var(--ts-null)] bg-muted/20" : "border-border bg-card"}`}
    data-testid={name}
    data-authority={quarantined ? "quarantined" : "typed"}
  >
    <p className="text-muted-foreground mb-2 font-mono text-[10px]">{name}</p>
    {children}
  </section>
);

export const EvidencePacketUI = makeAssistantDataUI<{
  packet_id: string; decision_at_utc: string;
  accepted_facts: { fact_id: string; label: string; value: number | string; unit: string | null; known_at_utc: string; receipt: { probe_id: string; source: string; retrieved_at_utc: string; source_hash: string; fact_hash: string; probe_inputs: [string, string][] } }[];
  rejected_future_fact_ids: string[];
}>({
  name: "evidence-packet",
  render: ({ data }) => <ClosedPanel name="evidence-packet">
    <div className="font-mono text-xs">{data.packet_id} · {data.accepted_facts.length} measured facts · {data.rejected_future_fact_ids.length} future rejected</div>
  </ClosedPanel>,
});

export const OptionStructureUI = makeAssistantDataUI<{
  candidate_id: string; selector_receipt_hash: string; legs: { contract_id: string; side: "LONG" | "SHORT"; quantity: number; quote_fact_id: string; definition_fact_id: string }[];
}>({
  name: "option-structure",
  render: ({ data }) => <ClosedPanel name="option-structure">
    <div className="font-mono text-xs">candidate {data.candidate_id}</div>
    {data.legs.map((leg: { contract_id: string; side: "LONG" | "SHORT"; quantity: number; quote_fact_id: string; definition_fact_id: string }) => <div key={`${leg.contract_id}-${leg.side}`} className="font-mono text-[10px]">{leg.side} {leg.quantity} · {leg.contract_id}</div>)}
    <div className="text-muted-foreground break-all font-mono text-[9px]">selector {data.selector_receipt_hash}</div>
  </ClosedPanel>,
});

export const TradeIntentUI = makeAssistantDataUI<{
  intent_id: string; decision_packet_hash: string;
  action: "NO_TRADE" | "WAIT" | "OPEN_APPROVED_STRUCTURE" | "REDUCE" | "CLOSE" | "REVIEW";
  option_candidate_id: string | null; supporting_fact_ids: string[]; counterevidence_fact_ids: string[];
}>({
  name: "trade-intent",
  render: ({ data }) => <ClosedPanel name="trade-intent">
    <div className="font-mono text-xs">{data.action} · {data.intent_id}</div>
    <div className="text-muted-foreground font-mono text-[9px]">support {data.supporting_fact_ids.length} · counterevidence {data.counterevidence_fact_ids.length}</div>
  </ClosedPanel>,
});

export const RiskGateVerdictUI = makeAssistantDataUI<{
  status: CheckStatus; input_hash: string; checks: Check[];
}>({
  name: "risk-gate-verdict",
  render: ({ data }) => {
    const status = data.status as CheckStatus;
    return <ClosedPanel name="risk-gate-verdict">
      <div className={`inline-block rounded border px-2 font-mono text-[10px] ${STATUS_STYLE[status]}`}>{STATUS_MARK[status]}</div>
      <div className="mt-1 font-mono text-[9px]">{data.checks.length} deterministic checks</div>
    </ClosedPanel>;
  },
});

export const ExecutionReceiptUI = makeAssistantDataUI<{
  intent_id: string; order_journal_root_hash: string; ledger_root_hash: string; broker_receipt_verified: false;
}>({
  name: "execution-receipt",
  render: ({ data }) => <ClosedPanel name="execution-receipt">
    <div className="font-mono text-xs">intent {data.intent_id} · broker receipt NOT_RUN</div>
    <div className="text-muted-foreground break-all font-mono text-[9px]">journal {data.order_journal_root_hash}</div>
  </ClosedPanel>,
});

export const PositionStateUI = makeAssistantDataUI<{
  through_event_seq: number; ledger_prefix_hash: string; reduction_receipt_hash: string; quantity: number; cash_micros: number; marked_value_micros: number;
}>({
  name: "position-state",
  render: ({ data }) => <ClosedPanel name="position-state">
    <div className="font-mono text-xs">qty {data.quantity} · cash {usd(data.cash_micros)} · marked {usd(data.marked_value_micros)}</div>
    <div className="text-muted-foreground font-mono text-[9px]">through event {data.through_event_seq}</div>
  </ClosedPanel>,
});

export const AdjudicationVerdictUI = makeAssistantDataUI<{
  position_ledger_hash: string;
  outcome: "EXPECTED" | "SIGNAL_FAILURE" | "SELECTION_FAILURE" | "EXECUTION_FAILURE" | "UNRESOLVED";
  supporting_fact_ids: string[]; counterexample_fact_ids: string[];
}>({
  name: "adjudication-verdict",
  render: ({ data }) => <ClosedPanel name="adjudication-verdict">
    <div className="font-mono text-xs">{data.outcome}</div>
    <div className="text-muted-foreground font-mono text-[9px]">support {data.supporting_fact_ids.length} · counterexamples {data.counterexample_fact_ids.length}</div>
  </ClosedPanel>,
});

export const ResearchDiagramUI = makeAssistantDataUI<{
  nodes: { node_id: string; concept: "HYPOTHESIS" | "SUPPORT" | "COUNTEREXAMPLE" | "UNKNOWN" | "QUESTION" }[];
  edges: { source_id: string; target_id: string; relation: "SUPPORTS" | "CONTRADICTS" | "TESTS" | "LEADS_TO" }[];
}>({
  name: "research-diagram",
  render: ({ data }) => <ClosedPanel name="research-diagram" quarantined>
    <div className="font-mono text-xs">generated · quarantined · no facts, figures or verdicts</div>
    <div className="text-muted-foreground font-mono text-[9px]">{data.nodes.length} controlled-concept nodes · {data.edges.length} edges</div>
  </ClosedPanel>,
});

export const NodeQuantPanels = () => (
  <>
    <DecisionPacketUI />
    <EvidencePacketUI />
    <OptionStructureUI />
    <TradeIntentUI />
    <RiskGateVerdictUI />
    <ExecutionReceiptUI />
    <PositionStateUI />
    <AdjudicationVerdictUI />
    <BacktestReviewUI />
    <ResearchDiagramUI />
  </>
);
