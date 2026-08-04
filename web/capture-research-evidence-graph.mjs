import { chromium } from "playwright";

const UI = process.env.UI_URL ?? "http://localhost:3201";
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const consoleErrors = [];
page.on("console", (message) => {
  if (message.type() === "error") consoleErrors.push(message.text());
});
const response = await page.goto(`${UI}/research-graph`, {
  waitUntil: "domcontentloaded",
  timeout: 120000,
});
await page
  .locator("[data-testid=research-graph-canvas]")
  .waitFor({ state: "visible", timeout: 120000 });
await page
  .locator("[data-testid=research-graph-canvas] canvas")
  .first()
  .waitFor({ state: "visible", timeout: 120000 });
await page.waitForTimeout(1200);
const beforeFilter = await page.locator("[data-testid=graph-counts]").innerText();
await page.getByLabel("Search evidence").fill("Developed ex-U.S.");
await page.waitForTimeout(1000);
const afterSearch = await page.locator("[data-testid=graph-counts]").innerText();
await page.getByLabel("Search evidence").fill("no-such-evidence-node-240804");
await page.getByText("NO MATCHING EVIDENCE").waitFor({ state: "visible" });
const emptyState = await page.getByText("NO MATCHING EVIDENCE").innerText();
await page.getByLabel("Search evidence").fill("");
const cutoff = page.getByLabel("Temporal cutoff");
const latestCutoff = await cutoff.getAttribute("max");
await cutoff.fill("0");
await page.waitForTimeout(500);
const earliestCutoff = await page.locator("[data-testid=graph-counts]").innerText();
await cutoff.fill(latestCutoff ?? "0");
await page.getByLabel("Attention mode").selectOption("contradiction");
await page.waitForTimeout(900);
await page.screenshot({ path: "../evidence/research-evidence-graph/after.png", fullPage: true });
console.log(
  JSON.stringify(
    {
      status: response.status(),
      beforeFilter,
      afterSearch,
      emptyState,
      earliestCutoff,
      consoleErrors,
    },
    null,
    2,
  ),
);
await browser.close();
