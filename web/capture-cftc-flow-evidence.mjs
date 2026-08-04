import { chromium } from "playwright";

const UI = process.env.UI_URL ?? "http://localhost:3201";
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const consoleErrors = [];
page.on("console", (message) => {
  if (message.type() === "error") consoleErrors.push(message.text());
});
const response = await page.goto(`${UI}/research-graph`, { waitUntil: "domcontentloaded", timeout: 120000 });
await page.locator("[data-testid=research-graph-canvas] canvas").first().waitFor({ state: "visible", timeout: 120000 });
await page.getByLabel("Search evidence").fill("CFTC accounting-contract invalidation");
await page.waitForTimeout(900);
const counts = await page.locator("[data-testid=graph-counts]").innerText();
const detail = await page.locator("aside").innerText();
if (!detail.includes("INVALID_EVALUATOR_ACCOUNTING_CONTRACT")) {
  throw new Error(`filtered graph kept a stale inspector: ${detail.slice(0, 160)}`);
}
await page.screenshot({ path: "../evidence/cftc-flow/after.png", fullPage: true });
console.log(JSON.stringify({ status: response.status(), counts, detailSignal: detail.includes("INVALID_EVALUATOR_ACCOUNTING_CONTRACT"), consoleErrors }, null, 2));
await browser.close();
