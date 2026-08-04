import { Assistant } from "./assistant";
import market from "./market-map/data.json";

export default function Home() {
  const marketMapSummary = {
    asOf: market.source.as_of,
    claimBoundary: market.claim_boundary,
    equities: market.universe.equities,
    manifestHash: market.manifest_hash,
    sectors: market.sectors.map((sector) => ({
      constituentCount: sector.constituent_count,
      key: sector.key,
      label: sector.label,
      topTickers: sector.holdings.slice(0, 4).map((holding) => holding.ticker),
      weightMicropct: sector.weight_micropct,
    })),
    sourceName: market.source.name,
  };

  return <Assistant marketMapSummary={marketMapSummary} />;
}
