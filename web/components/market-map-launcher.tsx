export type MarketMapSectorSummary = {
  constituentCount: number;
  key: string;
  label: string;
  topTickers: string[];
  weightMicropct: number;
};

export type MarketMapSummary = {
  asOf: string;
  claimBoundary: string;
  equities: number;
  manifestHash: string;
  sectors: MarketMapSectorSummary[];
  sourceName: string;
};

type Rect = {
  sector: MarketMapSectorSummary;
  height: number;
  width: number;
  x: number;
  y: number;
};

const PREVIEW_WIDTH = 680;
const PREVIEW_HEIGHT = 174;

const COLORS: Record<string, { fill: string; line: string }> = {
  "Information Technology": { fill: "#183c64", line: "#77b8f7" },
  Financials: { fill: "#5a345f", line: "#cf99d7" },
  "Consumer Discretionary": { fill: "#6b3c2f", line: "#eea07e" },
  "Communication Services": { fill: "#384777", line: "#9eacef" },
  "Health Care": { fill: "#245b53", line: "#83d4c3" },
  Industrials: { fill: "#5d512b", line: "#d9c47b" },
  "Consumer Staples": { fill: "#315b35", line: "#91ce91" },
  Energy: { fill: "#623f20", line: "#e1a861" },
  Utilities: { fill: "#344d55", line: "#9abec8" },
  "Real Estate": { fill: "#4d3e58", line: "#bba4ca" },
  Materials: { fill: "#4f5037", line: "#bfc28c" },
};

function splitLayout(
  sectors: MarketMapSectorSummary[],
  x: number,
  y: number,
  width: number,
  height: number,
): Rect[] {
  if (sectors.length === 0) return [];
  if (sectors.length === 1) return [{ sector: sectors[0], x, y, width, height }];

  const total = sectors.reduce((sum, sector) => sum + sector.weightMicropct, 0);
  let cumulative = 0;
  let split = 1;
  let closest = Number.POSITIVE_INFINITY;
  for (let index = 1; index < sectors.length; index += 1) {
    cumulative += sectors[index - 1].weightMicropct;
    const distance = Math.abs(total / 2 - cumulative);
    if (distance < closest) {
      closest = distance;
      split = index;
    }
  }

  const first = sectors.slice(0, split);
  const second = sectors.slice(split);
  const ratio = first.reduce((sum, sector) => sum + sector.weightMicropct, 0) / total;
  if (width >= height) {
    const firstWidth = width * ratio;
    return [
      ...splitLayout(first, x, y, firstWidth, height),
      ...splitLayout(second, x + firstWidth, y, width - firstWidth, height),
    ];
  }

  const firstHeight = height * ratio;
  return [
    ...splitLayout(first, x, y, width, firstHeight),
    ...splitLayout(second, x, y + firstHeight, width, height - firstHeight),
  ];
}

function shortLabel(label: string) {
  return label
    .replace("Information Technology", "Technology")
    .replace("Consumer Discretionary", "Consumer Disc.")
    .replace("Communication Services", "Communication")
    .replace("Consumer Staples", "Staples");
}

export function MarketMapLauncher({ summary }: { summary: MarketMapSummary }) {
  const rects = splitLayout(summary.sectors, 0, 0, PREVIEW_WIDTH, PREVIEW_HEIGHT);

  return (
    <section
      className="w-full overflow-hidden rounded-xl border border-slate-700 bg-[#0a0f17] text-left text-slate-100 shadow-[0_18px_45px_-28px_rgba(15,23,42,0.85)]"
      data-testid="market-map-launcher"
    >
      <div className="flex flex-col gap-3 border-b border-slate-700/80 px-4 py-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="font-mono text-[9px] font-bold tracking-[0.2em] text-sky-300">
            NODEQUANT / U.S. LARGE-CAP MAP
          </p>
          <h2 className="mt-1 text-lg font-semibold tracking-tight text-white">
            {summary.equities} equities. {summary.sectors.length} sectors. One surface.
          </h2>
          <p className="mt-1 text-xs leading-5 text-slate-400">
            Area is official portfolio weight. Inspect the market structure before asking the agent.
          </p>
        </div>
        <span className="w-fit border border-amber-400/50 bg-amber-400/10 px-2 py-1 font-mono text-[9px] font-bold tracking-[0.12em] text-amber-200">
          STRUCTURE ONLY
        </span>
      </div>

      <a
        href="/market-map"
        className="group block p-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-sky-300"
        aria-label={`Explore the full market map of ${summary.equities} equities in ${summary.sectors.length} sectors`}
        data-testid="market-map-launcher-link"
      >
        <svg
          viewBox={`0 0 ${PREVIEW_WIDTH} ${PREVIEW_HEIGHT}`}
          role="img"
          aria-label={`Structural preview of ${summary.equities} U.S. large-cap equities grouped into ${summary.sectors.length} sectors`}
          className="block h-auto w-full border border-slate-700 bg-[#070b11]"
        >
          {rects.map((rect) => {
            const color = COLORS[rect.sector.label] ?? { fill: "#334155", line: "#94a3b8" };
            const showDetails = rect.width > 105 && rect.height > 50;
            const showLabel = rect.width > 68 && rect.height > 30;
            return (
              <g key={rect.sector.key}>
                <rect
                  x={rect.x + 1.5}
                  y={rect.y + 1.5}
                  width={Math.max(0, rect.width - 3)}
                  height={Math.max(0, rect.height - 3)}
                  fill={color.fill}
                  stroke={color.line}
                  strokeOpacity="0.65"
                />
                {showLabel ? (
                  <text
                    x={rect.x + 8}
                    y={rect.y + 18}
                    fill="#f8fafc"
                    fontFamily="ui-monospace, monospace"
                    fontSize="10"
                    fontWeight="700"
                  >
                    {shortLabel(rect.sector.label)}
                  </text>
                ) : null}
                {showDetails ? (
                  <>
                    <text
                      x={rect.x + 8}
                      y={rect.y + 34}
                      fill="#cbd5e1"
                      fontFamily="ui-monospace, monospace"
                      fontSize="9"
                    >
                      {rect.sector.constituentCount} names
                    </text>
                    <text
                      x={rect.x + 8}
                      y={rect.y + 50}
                      fill={color.line}
                      fontFamily="ui-monospace, monospace"
                      fontSize="9"
                    >
                      {rect.sector.topTickers.join(" · ")}
                    </text>
                  </>
                ) : null}
              </g>
            );
          })}
        </svg>
      </a>

      <div className="flex flex-col gap-2 border-t border-slate-700/80 px-4 py-3 font-mono text-[9px] text-slate-400 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex flex-wrap gap-x-3 gap-y-1">
          <span>AS OF {summary.asOf}</span>
          <span>{summary.sourceName}</span>
          <span title={summary.manifestHash}>HASH {summary.manifestHash.slice(7, 19)}</span>
        </div>
        <a
          href="/market-map"
          className="w-fit font-bold tracking-[0.08em] text-sky-300 underline decoration-sky-300/40 underline-offset-4 hover:text-white"
        >
          EXPLORE FULL MARKET MAP →
        </a>
      </div>
      <p className="sr-only">Claim boundary: {summary.claimBoundary}</p>
    </section>
  );
}
