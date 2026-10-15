"use client";

import { useEffect, useMemo, useState } from "react";
import market from "./data.json";
import activity from "./activity.json";

type Sector = (typeof market.sectors)[number];
type Holding = Sector["holdings"][number];
type Rect<T> = { datum: T; x: number; y: number; width: number; height: number };
type MarketMode = "structure" | "activity";

const WIDTH = 1600;
const HEIGHT = 820;
const GAP = 3;
const SECTOR_HEADER = 30;

const COLORS: Record<string, { fill: string; strong: string }> = {
  "Information Technology": { fill: "#183c64", strong: "#77b8f7" },
  Financials: { fill: "#5a345f", strong: "#cf99d7" },
  "Consumer Discretionary": { fill: "#6b3c2f", strong: "#eea07e" },
  "Communication Services": { fill: "#384777", strong: "#9eacef" },
  "Health Care": { fill: "#245b53", strong: "#83d4c3" },
  Industrials: { fill: "#5d512b", strong: "#d9c47b" },
  "Consumer Staples": { fill: "#315b35", strong: "#91ce91" },
  Energy: { fill: "#623f20", strong: "#e1a861" },
  Utilities: { fill: "#344d55", strong: "#9abec8" },
  "Real Estate": { fill: "#4d3e58", strong: "#bba4ca" },
  Materials: { fill: "#4f5037", strong: "#bfc28c" },
};

function splitLayout<T>(
  items: readonly T[],
  value: (item: T) => number,
  x: number,
  y: number,
  width: number,
  height: number,
): Rect<T>[] {
  if (items.length === 0) return [];
  if (items.length === 1) return [{ datum: items[0], x, y, width, height }];
  const total = items.reduce((sum, item) => sum + value(item), 0);
  let cumulative = 0;
  let split = 1;
  let closest = Number.POSITIVE_INFINITY;
  for (let index = 1; index < items.length; index += 1) {
    cumulative += value(items[index - 1]);
    const distance = Math.abs(total / 2 - cumulative);
    if (distance < closest) {
      closest = distance;
      split = index;
    }
  }
  const first = items.slice(0, split);
  const second = items.slice(split);
  const firstTotal = first.reduce((sum, item) => sum + value(item), 0);
  const ratio = Math.max(0.02, Math.min(0.98, firstTotal / total));
  if (width >= height) {
    const firstWidth = width * ratio;
    return [
      ...splitLayout(first, value, x, y, firstWidth, height),
      ...splitLayout(second, value, x + firstWidth, y, width - firstWidth, height),
    ];
  }
  const firstHeight = height * ratio;
  return [
    ...splitLayout(first, value, x, y, width, firstHeight),
    ...splitLayout(second, value, x, y + firstHeight, width, height - firstHeight),
  ];
}

function pct(micropct: number, digits = 2) {
  return (micropct / 1_000_000).toFixed(digits) + "%";
}

function truncate(value: string, limit: number) {
  return value.length <= limit ? value : value.slice(0, Math.max(1, limit - 1)) + "…";
}

const ACTIVITY_HOLDINGS = activity.sectors.flatMap((sector) => sector.holdings);
const ACTIVITY_BY_TICKER = new Map(ACTIVITY_HOLDINGS.map((holding) => [holding.ticker, holding]));
const ACTIVITY_SECTOR_BY_LABEL = new Map(activity.sectors.map((sector) => [sector.label, sector]));
const INACTIVE_TICKERS = new Set(activity.inactive_structural_rows.map((row) => row.ticker));
const ACTIVITY_VOLUMES = ACTIVITY_HOLDINGS.map((holding) => holding.reported_share_volume).filter(
  (volume) => volume > 0,
);
const MIN_LOG_VOLUME = Math.log10(Math.min(...ACTIVITY_VOLUMES));
const MAX_LOG_VOLUME = Math.log10(Math.max(...ACTIVITY_VOLUMES));

function activityFill(volume: number, strong: string) {
  if (volume <= 0) return "#151922";
  const raw = (Math.log10(volume) - MIN_LOG_VOLUME) / (MAX_LOG_VOLUME - MIN_LOG_VOLUME);
  const ratio = Math.max(0, Math.min(1, raw));
  const alpha = Math.round(28 + ratio * 227).toString(16).padStart(2, "0");
  return `${strong}${alpha}`;
}

function compactShares(value: number) {
  return new Intl.NumberFormat("en-US", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

function dollars(micros: number) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(micros / 1_000_000);
}

export function MarketMapView() {
  const [mode, setMode] = useState<MarketMode>("structure");
  const [query, setQuery] = useState("");
  const [sectorFilter, setSectorFilter] = useState<string | null>(null);
  const [selectedTicker, setSelectedTicker] = useState("NVDA");
  const normalized = query.trim().toLowerCase();

  const sectorRects = useMemo(
    () => splitLayout(market.sectors, (sector) => sector.weight_micropct, 0, 0, WIDTH, HEIGHT),
    [],
  );
  const holdingRects = useMemo(
    () =>
      sectorRects.flatMap((sectorRect) =>
        splitLayout(
          sectorRect.datum.holdings,
          (holding) => holding.visual_weight_micropct,
          sectorRect.x + GAP,
          sectorRect.y + SECTOR_HEADER,
          Math.max(1, sectorRect.width - GAP * 2),
          Math.max(1, sectorRect.height - SECTOR_HEADER - GAP),
        ).map((rect) => ({ ...rect, sector: sectorRect.datum })),
      ),
    [sectorRects],
  );
  const selected =
    holdingRects.find((rect) => rect.datum.ticker === selectedTicker) ?? holdingRects[0];
  const selectedActivity = ACTIVITY_BY_TICKER.get(selected.datum.ticker);
  const selectedInactive = activity.inactive_structural_rows.find(
    (row) => row.ticker === selected.datum.ticker,
  );
  const exactTicker = normalized
    ? holdingRects.find((rect) => rect.datum.ticker.toLowerCase() === normalized)?.datum.ticker
    : undefined;
  const matches = (holding: Holding) =>
    (!sectorFilter || holding.sector === sectorFilter) &&
    (!normalized ||
      (exactTicker
        ? holding.ticker === exactTicker
        : `${holding.ticker} ${holding.name} ${holding.sector}`
            .toLowerCase()
            .includes(normalized)));
  const matchCount = holdingRects.filter((rect) => matches(rect.datum)).length;
  const firstFocusedTicker = holdingRects.find((rect) => matches(rect.datum))?.datum.ticker;

  useEffect(() => {
    if (firstFocusedTicker) setSelectedTicker(firstFocusedTicker);
  }, [firstFocusedTicker]);

  return (
    <main
      className="min-h-screen bg-[#080b12] p-3 text-[#e9edf2] md:p-5"
      data-testid="market-map-page"
    >
      <section className="mx-auto max-w-[1800px] overflow-hidden border border-[#2a3240] bg-[#0d121b] shadow-2xl">
        <header className="grid gap-5 border-b border-[#2a3240] p-5 lg:grid-cols-[1fr_auto] lg:items-end">
          <div>
            <p className="font-mono text-[10px] font-bold tracking-[0.22em] text-[#77b8f7]">
              NODEQUANT / U.S. LARGE-CAP MARKET ACTIVITY GRAPH
            </p>
            <h1 className="mt-2 text-3xl font-semibold tracking-[-0.03em] md:text-5xl">
              The whole market. One inspectable surface.
            </h1>
            <p className="mt-3 max-w-4xl text-sm leading-6 text-[#9ea9b8]">
              {mode === "structure" ? (
                <>
                  504 equity holdings grouped into 11 stable sector regions. Area is official IVV
                  portfolio weight. This snapshot proves structural coverage—not live volume,
                  money flow, return, or causality.
                </>
              ) : (
                <>
                  One write-once provider observation covers 503/503 eligible holdings. Geometry
                  remains official IVV weight; color intensity is reported share volume. This is
                  trading activity—not flow, direction, relative volume, or a live feed.
                </>
              )}
            </p>
          </div>
          <div className="flex flex-wrap gap-2 font-mono text-[10px] font-bold">
            <a href="/research-graph" className="border border-[#3c4656] px-3 py-2 text-[#bac4d1]">
              EVIDENCE GRAPH
            </a>
            <a href="/" className="border border-[#3c4656] px-3 py-2 text-[#bac4d1]">
              PROOF KERNEL
            </a>
          </div>
        </header>

        <div className="grid border-b border-[#2a3240] xl:grid-cols-[1fr_430px]">
          <section className="border-b border-[#2a3240] p-4 xl:border-b-0 xl:border-r">
            <div className="mb-3 flex flex-wrap items-center gap-2">
              {[
                ["STRUCTURE", true, "structure"],
                ["ACTIVITY", true, "activity"],
                ["ROTATION", false, null],
                ["OPTIONS", false, null],
                ["ATTENTION", false, null],
              ].map(([label, available, nextMode]) => (
                <button
                  key={String(label)}
                  disabled={!available}
                  data-testid={label === "ACTIVITY" ? "market-mode-activity" : undefined}
                  aria-pressed={nextMode ? mode === nextMode : undefined}
                  onClick={() => nextMode && setMode(nextMode as MarketMode)}
                  title={
                    available
                      ? label === "STRUCTURE"
                        ? "Official holdings structure"
                        : "One provider-reported share-volume observation"
                      : "Qualified time-series feed required"
                  }
                  className={`border px-3 py-2 font-mono text-[10px] font-bold ${available && mode === nextMode ? "border-[#77b8f7] bg-[#17324d] text-white" : available ? "border-[#3c4656] text-[#bac4d1]" : "cursor-not-allowed border-[#2b333f] text-[#596574]"}`}
                >
                  {label} {available ? (label === "ACTIVITY" ? "SNAPSHOT" : "LIVE") : "FEED REQUIRED"}
                </button>
              ))}
              <span className="ml-auto font-mono text-[10px] text-[#7f8b9a]">
                {mode === "structure"
                  ? `AS OF ${market.source.as_of} · OFFICIAL HOLDINGS`
                  : `OBSERVED ${activity.source.provider_date_utc} · ${activity.coverage.observed_rows}/${activity.coverage.eligible_rows} ELIGIBLE`}
              </span>
            </div>
            <div className="relative overflow-hidden border border-[#354151] bg-[#070b11]">
              <svg
                viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
                role="img"
                aria-label={`Full structural map of ${market.universe.equities} U.S. large-cap equity holdings in ${market.universe.sectors} sectors`}
                className="block h-auto w-full"
                data-testid="market-treemap"
              >
                {sectorRects.map((rect) => {
                  const color = COLORS[rect.datum.label];
                  const sectorActivity = ACTIVITY_SECTOR_BY_LABEL.get(rect.datum.label);
                  const sectorActivityShare = sectorActivity
                    ? Math.round(
                        (sectorActivity.reported_share_volume /
                          activity.market.reported_share_volume) *
                          100_000_000,
                      )
                    : 0;
                  return (
                    <g key={rect.datum.key}>
                      <rect
                        x={rect.x + 1}
                        y={rect.y + 1}
                        width={Math.max(0, rect.width - 2)}
                        height={Math.max(0, rect.height - 2)}
                        fill={color.fill}
                        stroke={sectorFilter === rect.datum.label ? "#ffffff" : color.strong}
                        strokeWidth={sectorFilter === rect.datum.label ? 5 : 2}
                      />
                      <text
                        x={rect.x + 9}
                        y={rect.y + 19}
                        fill="#f3f6fa"
                        fontSize="14"
                        fontWeight="700"
                        letterSpacing="0.4"
                      >
                        {truncate(rect.datum.label.toUpperCase(), Math.max(8, rect.width / 9))}
                      </text>
                      <text
                        x={rect.x + rect.width - 8}
                        y={rect.y + 19}
                        fill={color.strong}
                        fontSize="12"
                        textAnchor="end"
                      >
                        {mode === "structure"
                          ? `${pct(rect.datum.weight_micropct)} · ${rect.datum.constituent_count}`
                          : `${pct(sectorActivityShare)} activity · ${sectorActivity?.eligible_constituents ?? 0}`}
                      </text>
                    </g>
                  );
                })}
                {holdingRects.map((rect) => {
                  const holding = rect.datum;
                  const active = matches(holding);
                  const chosen = holding.ticker === selectedTicker;
                  const width = Math.max(0, rect.width - GAP);
                  const height = Math.max(0, rect.height - GAP);
                  const labelFits = width > 28 && height > 16;
                  const detailFits = width > 62 && height > 31;
                  const color = COLORS[holding.sector];
                  const holdingActivity = ACTIVITY_BY_TICKER.get(holding.ticker);
                  const inactive = INACTIVE_TICKERS.has(holding.ticker);
                  return (
                    <g
                      key={holding.ticker}
                      onClick={() => setSelectedTicker(holding.ticker)}
                      className="cursor-pointer"
                      data-ticker={holding.ticker}
                      data-activity-observed={holdingActivity ? "true" : "false"}
                      data-activity-inactive={inactive ? "true" : "false"}
                      data-reported-share-volume={holdingActivity?.reported_share_volume}
                    >
                      <rect
                        x={rect.x + GAP / 2}
                        y={rect.y + GAP / 2}
                        width={width}
                        height={height}
                        fill={
                          chosen
                            ? color.strong
                            : mode === "activity"
                              ? activityFill(holdingActivity?.reported_share_volume ?? 0, color.strong)
                              : "#111a25"
                        }
                        fillOpacity={active ? (chosen ? 0.95 : 0.92) : 0.13}
                        stroke={chosen ? "#ffffff" : color.strong}
                        strokeOpacity={active ? 0.75 : 0.08}
                        strokeWidth={chosen ? 3 : 1}
                      />
                      {labelFits && (
                        <text
                          x={rect.x + rect.width / 2}
                          y={rect.y + rect.height / 2 - (detailFits ? 3 : -4)}
                          fill={chosen ? "#071019" : "#eef3f8"}
                          fillOpacity={active ? 1 : 0.12}
                          fontSize={Math.max(8, Math.min(17, width / 4.3, height / 2.4))}
                          fontWeight="700"
                          textAnchor="middle"
                          pointerEvents="none"
                        >
                          {holding.ticker}
                        </text>
                      )}
                      {detailFits && (
                        <text
                          x={rect.x + rect.width / 2}
                          y={rect.y + rect.height / 2 + 13}
                          fill={chosen ? "#132333" : "#8fa1b4"}
                          fillOpacity={active ? 1 : 0.1}
                          fontSize="9"
                          textAnchor="middle"
                          pointerEvents="none"
                        >
                          {mode === "structure"
                            ? pct(holding.weight_micropct)
                            : holdingActivity
                              ? compactShares(holdingActivity.reported_share_volume)
                              : "INACTIVE"}
                        </text>
                      )}
                    </g>
                  );
                })}
              </svg>
              <div
                className="absolute bottom-3 left-3 border border-[#445163] bg-[#0b1119]/95 px-3 py-2 font-mono text-[10px]"
                data-testid="market-map-counts"
              >
                {mode === "structure" ? (
                  <>
                    {market.universe.equities} EQUITIES / {market.universe.sectors} SECTORS /{" "}
                    {matchCount} IN FOCUS
                  </>
                ) : (
                  <>
                    {activity.coverage.observed_rows}/{activity.coverage.eligible_rows} ELIGIBLE
                    OBSERVED / {activity.coverage.inactive_structural_rows} INACTIVE / {matchCount}{" "}
                    IN FOCUS
                  </>
                )}
              </div>
            </div>
          </section>

          <aside className="p-5" data-testid="market-map-inspector">
            <label className="block font-mono text-[10px] font-bold tracking-[0.15em] text-[#8e9bab]">
              FIND ANY CONSTITUENT
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="MU, semiconductors, health care…"
                className="mt-2 w-full border border-[#3a4657] bg-[#090e15] px-3 py-3 font-sans text-sm text-white outline-none focus:border-[#77b8f7]"
                aria-label="Search market map"
              />
            </label>
            <div className="mt-4 flex flex-wrap gap-1.5">
              <button
                onClick={() => setSectorFilter(null)}
                className={`border px-2 py-1 font-mono text-[9px] ${sectorFilter === null ? "border-white text-white" : "border-[#354151] text-[#778494]"}`}
              >
                ALL 11
              </button>
              {market.sectors.map((sector) => (
                <button
                  key={sector.key}
                  onClick={() =>
                    setSectorFilter(sectorFilter === sector.label ? null : sector.label)
                  }
                  className={`border px-2 py-1 font-mono text-[9px] ${sectorFilter === sector.label ? "text-white" : "text-[#778494]"}`}
                  style={{ borderColor: COLORS[sector.label].strong }}
                >
                  {sector.label.toUpperCase()}
                </button>
              ))}
            </div>

            <div
              className="mt-7 border-l-2 pl-4"
              style={{ borderColor: COLORS[selected.datum.sector].strong }}
            >
              <p className="font-mono text-[10px] text-[#8090a1]">
                MARKET → {selected.datum.sector.toUpperCase()}
              </p>
              <h2 className="mt-2 text-4xl font-semibold">{selected.datum.ticker}</h2>
              <p className="mt-1 text-sm text-[#b4bfcb]">{selected.datum.name}</p>
              <dl className="mt-5 grid grid-cols-2 gap-4 text-xs">
                {mode === "structure" ? (
                  <>
                    <div>
                      <dt className="text-[#758394]">PORTFOLIO WEIGHT</dt>
                      <dd className="mt-1 font-mono text-lg text-white">
                        {pct(selected.datum.weight_micropct, 4)}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-[#758394]">FUND MARKET VALUE</dt>
                      <dd className="mt-1 font-mono text-lg text-white">
                        ${(selected.datum.market_value_micros / 1_000_000_000_000_000).toFixed(2)}B
                      </dd>
                    </div>
                  </>
                ) : selectedActivity ? (
                  <>
                    <div>
                      <dt className="text-[#758394]">REPORTED SHARE VOLUME</dt>
                      <dd className="mt-1 font-mono text-lg text-white">
                        {selectedActivity.reported_share_volume.toLocaleString("en-US")}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-[#758394]">LAST SALE / CHANGE</dt>
                      <dd className="mt-1 font-mono text-lg text-white">
                        {dollars(selectedActivity.last_sale_micros)} /{" "}
                        {pct(selectedActivity.pct_change_micropct)}
                      </dd>
                    </div>
                    <div className="col-span-2">
                      <dt className="text-[#758394]">PROVIDER SYMBOL / OBSERVED</dt>
                      <dd className="mt-1 font-mono text-[#d8dee6]">
                        {selectedActivity.provider_symbol} · {activity.source.provider_date_utc}
                      </dd>
                    </div>
                  </>
                ) : (
                  <div className="col-span-2" data-testid="inactive-structural-receipt">
                    <dt className="text-[#758394]">ACTIVITY ELIGIBILITY</dt>
                    <dd className="mt-1 font-mono text-[#e6bc75]">
                      INACTIVE SINCE {selectedInactive?.effective_at_utc.slice(0, 10)} · NO ZERO
                      IMPUTED
                    </dd>
                  </div>
                )}
                <div className="col-span-2">
                  <dt className="text-[#758394]">CURRENT CLAIM</dt>
                  <dd className="mt-1 text-[#d8dee6]">
                    {mode === "structure"
                      ? "Structural membership and weight only"
                      : selectedActivity
                        ? "One provider-reported trading-activity observation; not flow or relative volume"
                        : "Inactive structural residual; absence is not zero activity"}
                  </dd>
                </div>
              </dl>
            </div>

            <div className="mt-8 border border-[#344050] bg-[#090e15] p-4 text-xs leading-5 text-[#9da9b7]">
              <p className="font-mono text-[10px] font-bold text-[#77b8f7]">AUTHORITY RECEIPT</p>
              <p className="mt-2">
                {mode === "structure"
                  ? `Official IVV holdings · ${market.source.as_of}`
                  : `Nasdaq current observation · ${activity.coverage.observed_rows}/${activity.coverage.eligible_rows} eligible · promotion evidence: NO`}
              </p>
              <p className="mt-2 break-all font-mono text-[9px] text-[#6f7e8e]">
                {mode === "structure" ? market.manifest_hash : activity.snapshot_hash}
              </p>
              <a
                href={mode === "structure" ? market.source.url : activity.source.url}
                target="_blank"
                rel="noreferrer"
                className="mt-3 inline-block text-[#9ccaff] underline underline-offset-4"
              >
                {mode === "structure" ? "OPEN ORIGINAL SOURCE" : "OPEN PROVIDER LOCATOR"}
              </a>
            </div>
          </aside>
        </div>
      </section>
    </main>
  );
}
