import { chromium } from "playwright";
import { mkdirSync } from "node:fs";
mkdirSync("evidence", { recursive: true });
const browser = await chromium.launch();
const page = await browser.newPage({
  viewport: { width: 1100, height: 1000 }, deviceScaleFactor: 2,
  colorScheme: process.argv.includes("--dark") ? "dark" : "light",
});
await page.goto("http://localhost:3010", { waitUntil: "networkidle" });
await page.locator("textarea").fill("review the backtest");
await page.locator("textarea").press("Enter");
const panel = page.locator('[data-testid="backtest-review"]');
await panel.waitFor({ state: "attached", timeout: 30000 });
await page.waitForTimeout(700);
const out = {
  verdict: await panel.getAttribute("data-verdict"),
  blockers: await panel.getAttribute("data-blockers"),
  codes: await panel.locator("[data-blocker]").evaluateAll(n => n.map(x => x.getAttribute("data-blocker"))),
  hasCurve: await panel.locator('[data-testid="backtest-curve"]').count(),
  htmlClass: await page.evaluate(() => document.documentElement.className),
};
await page.screenshot({ path: `evidence/${process.argv[2] ?? "review"}-full.png`, fullPage: true });
console.log(JSON.stringify(out));
await browser.close();
