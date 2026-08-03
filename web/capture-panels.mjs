/**
 * Capture the decision-packet panel as visual evidence.
 *
 * DOM assertions prove structure; they do not prove a panel is readable. This
 * renders it in a real browser and writes a PNG per state, including the state
 * that matters most on a trust surface -- the one where verification did not
 * run and the panel must not look like an approval.
 *
 *   node capture-panels.mjs <label> [--theme dark]
 */

import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const label = process.argv[2] ?? "panel";
const dark = process.argv.includes("--dark");
const OUT = "evidence";
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({
  viewport: { width: 1100, height: 900 },
  deviceScaleFactor: 2, // legible text in the PNG
  colorScheme: dark ? "dark" : "light",
});

await page.goto("http://localhost:3010", { waitUntil: "networkidle" });

await page.locator("textarea").fill("show me the first signal");
await page.locator("textarea").press("Enter");

const panel = page.locator('[data-testid="decision-packet"]');
await panel.waitFor({ state: "attached", timeout: 30_000 });
await page.waitForTimeout(600); // let fonts settle before the shot

const verdict = await panel.getAttribute("data-verdict");
const checks = await panel.locator("[data-check]").count();
const notRun = await panel.locator('[data-status="NOT_RUN"]').count();

await panel.screenshot({ path: `${OUT}/${label}.png` });
await page.screenshot({ path: `${OUT}/${label}-full.png`, fullPage: true });

console.log(
  JSON.stringify({ label, verdict, checks, notRun, theme: dark ? "dark" : "light" })
);

await browser.close();
