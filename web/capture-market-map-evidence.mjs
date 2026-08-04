import { chromium } from "playwright";
import fs from "node:fs/promises";

const origin = process.env.MARKET_MAP_ORIGIN;
if (!origin || !/^http:\/\/(127\.0\.0\.1|localhost):\d+$/.test(origin)) {
  throw new Error("MARKET_MAP_ORIGIN must be an explicit local production origin");
}
const output = "../evidence/market-activity/after-full-market-map-final.png";
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1728, height: 1117 } });
const consoleErrors = [];
page.on("console", (message) => {
  if (message.type() === "error") consoleErrors.push(message.text());
});
const response = await page.goto(`${origin}/market-map`, { waitUntil: "networkidle" });
if (!response || response.status() !== 200) throw new Error("market map did not return HTTP 200");
await page.getByTestId("market-treemap").waitFor();
const tickers = await page
  .locator("[data-ticker]")
  .evaluateAll((nodes) => nodes.map((node) => node.getAttribute("data-ticker")));
if (tickers.length !== 504 || new Set(tickers).size !== 504) {
  throw new Error(`expected 504 unique tiles, received ${tickers.length}/${new Set(tickers).size}`);
}
const initialCounts = await page.getByTestId("market-map-counts").innerText();
if (initialCounts !== "504 EQUITIES / 11 SECTORS / 504 IN FOCUS") {
  throw new Error(`unexpected initial reconciliation: ${initialCounts}`);
}
await page.getByRole("textbox", { name: "Search market map" }).fill("MU");
if (
  (await page.getByTestId("market-map-counts").innerText()) !==
  "504 EQUITIES / 11 SECTORS / 1 IN FOCUS"
) {
  throw new Error("exact ticker search did not isolate one tile");
}
if ((await page.getByTestId("market-map-inspector").getByRole("heading").innerText()) !== "MU") {
  throw new Error("exact ticker search did not move the inspector to MU");
}
await page.getByRole("textbox", { name: "Search market map" }).fill("");
await page.getByTestId("market-map-inspector").getByRole("button", { name: "HEALTH CARE" }).click();
if (!(await page.getByTestId("market-map-counts").innerText()).endsWith("60 IN FOCUS")) {
  throw new Error("sector isolation did not match the official manifest");
}
await page.getByTestId("market-map-inspector").getByRole("button", { name: "ALL 11" }).click();
if (consoleErrors.length) throw new Error(`console errors: ${consoleErrors.join(" | ")}`);
await page.screenshot({ path: output, fullPage: true });
await fs.writeFile(
  "../evidence/market-activity/browser-proof.json",
  `${JSON.stringify(
    {
      origin,
      viewport: { width: 1728, height: 1117 },
      http_status: response.status(),
      tiles: tickers.length,
      unique_tickers: new Set(tickers).size,
      exact_ticker_search: "MU -> 1 IN FOCUS",
      health_care_filter: "60 IN FOCUS",
      console_errors: consoleErrors.length,
    },
    null,
    2,
  )}\n`,
);
await browser.close();
