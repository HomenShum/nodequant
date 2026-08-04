import { chromium } from "playwright";

const phase = process.env.PROOF_PHASE ?? "after";
const page = await (await chromium.launch()).newPage({ viewport: { width: 1440, height: 900 } });
const errors = []; page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
const response = await page.goto(`${process.env.UI_URL ?? "http://localhost:3201"}/research-graph`, { waitUntil: "domcontentloaded", timeout: 120000 });
await page.locator("[data-testid=research-graph-canvas] canvas").first().waitFor({ state: "visible", timeout: 120000 });
await page.getByLabel("Search evidence").fill("Crypto rebalancing v2 result"); await page.waitForTimeout(800);
if (phase === "before") {
  await page.getByText("NO MATCHING EVIDENCE").waitFor({ state: "visible" });
  await page.screenshot({ path: "../evidence/crypto-rebalancing/before.png", fullPage: true });
  console.log(JSON.stringify({ phase, status: response.status(), empty: true, consoleErrors: errors }, null, 2));
} else {
  const counts = await page.locator("[data-testid=graph-counts]").innerText();
  const inspector = page.locator("[data-testid=research-graph-inspector]");
  const detail = await inspector.innerText();
  if (!detail.includes("CRYPTO_REBALANCING_PREMIUM_FAILS")) throw new Error(`wrong inspector detail: ${detail}`);
  await page.screenshot({ path: "../evidence/crypto-rebalancing/after.png", fullPage: true });
  console.log(JSON.stringify({ phase, status: response.status(), counts, detailSignal: true, consoleErrors: errors }, null, 2));
}
await page.context().browser().close();
