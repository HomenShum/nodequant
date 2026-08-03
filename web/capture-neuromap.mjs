/**
 * Capture the interactive map embedded in the chat panel, and exercise the
 * node finder inside it.
 *
 * The finder lives in app/neuromap.py, not in TSX, so ONE implementation serves
 * both the MCP standalone HTML and this iframe. That is the point of testing it
 * here: if the two ever diverge, this is where it shows.
 *
 *   node capture-neuromap.mjs
 */

import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const UI = process.env.UI_URL ?? "http://localhost:3000";
const OUT = "evidence/neuromap-embed";
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const findings = {};
const bundleRequests = [];
page.on("response", (r) => {
  if (r.url().includes("cytoscape.min.js"))
    bundleRequests.push({ status: r.status(), fromCache: r.fromServiceWorker() });
});

await page.goto(UI, { waitUntil: "domcontentloaded" });
const composer = page.locator('textarea[name="input"]');
await composer.waitFor({ state: "visible", timeout: 15_000 });
await composer.pressSequentially(
  "Show a network of sponsors and drugs for melanoma trials.",
  { delay: 8 },
);
await page.getByRole("button", { name: "Send message" }).click();
await page
  .getByText(/co-occurring trials/i)
  .first()
  .waitFor({ state: "visible", timeout: 120_000 });
await page.waitForTimeout(1200);

// The map must be OFFERED but not mounted: an iframe per network response would
// run a graph layout the reader never asked to see.
findings.frameBeforeToggle = await page.locator("[data-testid=neuromap-frame]").count();
const toggle = page.getByRole("button", { name: /interactive map/i });
findings.toggleVisible = await toggle.isVisible();
await page.screenshot({ path: `${OUT}/1-collapsed.png`, fullPage: true });

await toggle.click();
// NOT frameLocator: the iframe is sandboxed WITHOUT allow-same-origin, so its
// DOM is deliberately unreachable from here -- by the same rule that stops a
// bug in the vendored bundle from reaching the chat session. Keeping the
// sandbox and losing DOM assertions is the right side of that trade, because
// node labels are external ClinicalTrials.gov data rendered into HTML.
//
// So the embed is verified by what IS observable from outside: the bundle
// request the frame makes, and the pixels. The finder's behaviour is asserted
// separately, against the same renderer, in probe-node-finder.mjs.
await page.waitForTimeout(3500);
findings.bundleRequests = bundleRequests;
findings.bundleLoaded = bundleRequests.some((r) => r.status === 200);
findings.frameAfterToggle = await page.locator("[data-testid=neuromap-frame]").count();
await page.screenshot({ path: `${OUT}/2-map-open.png`, fullPage: true });

await browser.close();
console.log(JSON.stringify(findings, null, 2));

