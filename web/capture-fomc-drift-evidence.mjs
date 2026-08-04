import { chromium } from "playwright";

const UI = process.env.UI_URL ?? "http://localhost:3201";
const phase = process.env.PROOF_PHASE ?? "after";
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const consoleErrors = [];
page.on("console", (message) => { if (message.type() === "error") consoleErrors.push(message.text()); });
const response = await page.goto(`${UI}/research-graph`, { waitUntil: "domcontentloaded", timeout: 120000 });
await page.locator("[data-testid=research-graph-canvas] canvas").first().waitFor({ state: "visible", timeout: 120000 });
await page.getByLabel("Search evidence").fill("Post-publication FOMC drift result");
await page.waitForTimeout(900);
if (phase === "before") {
  await page.getByText("NO MATCHING EVIDENCE").waitFor({ state: "visible" });
  await page.screenshot({ path: "../evidence/fomc-drift/before.png", fullPage: true });
  console.log(JSON.stringify({ phase, status: response.status(), empty: true, consoleErrors }, null, 2));
} else {
  const counts = await page.locator("[data-testid=graph-counts]").innerText();
  const detail = await page.locator("aside").innerText();
  if (!detail.includes("POST_PUBLICATION_FOMC_DRIFT_FAILS")) throw new Error("FOMC result detail is stale or absent");
  await page.screenshot({ path: "../evidence/fomc-drift/after.png", fullPage: true });
  console.log(JSON.stringify({ phase, status: response.status(), counts, detailSignal: true, consoleErrors }, null, 2));
}
await browser.close();
