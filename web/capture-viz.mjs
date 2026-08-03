/**
 * Capture every visualization type rendering in the live chat UI.
 *
 * `e2e-check.mjs` proves ONE query renders. That was the right test when the
 * question was "does the data-part stream reach the client at all" -- the bug it
 * was written for dropped every part because the `start` frame carried no
 * messageId. It is the wrong evidence for a claim about three chart types.
 *
 * Each case below asserts markers that a DIFFERENT renderer must produce, so a
 * pass means that renderer ran -- not that the page loaded. The network case in
 * particular asserts node and edge counts, because the failure it guards against
 * shipped once already: reusing CITATION_FIELDS for entity extraction omitted
 * InterventionName and produced a graph with nodes, zero edges, and no error.
 *
 *   node capture-viz.mjs
 */

import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const UI = process.env.UI_URL ?? "http://localhost:3000";
const OUT = "evidence/viz-types";
mkdirSync(OUT, { recursive: true });

const CASES = [
  {
    slug: "bar-chart",
    query: "How are melanoma trials distributed across phases?",
    // Reconciliation is the panel that proves the overlap warning fired.
    settle: "Reconciliation",
    markers: ["PHASE2", "bucket sum", "Sources", "every number replayable"],
  },
  {
    slug: "time-series",
    query: "How many melanoma trials started each year since 2015?",
    settle: "Sources",
    // A year label is the renderer-specific signal: a bar chart of phases can
    // never produce one, so this cannot pass on the wrong renderer.
    markers: ["2015", "2019", "Sources", "every number replayable"],
  },
  {
    slug: "network-graph",
    query: "Show a network of sponsors and drugs for melanoma trials.",
    // NOT "Sources". The network renderer has no Sources heading -- its
    // citations hang off edges, not off a bucket list -- so waiting for one
    // timed out at 120s against a graph that had rendered correctly 80s
    // earlier. The marker was wrong, not the product.
    // Regex, case-insensitive: the heading is painted uppercase by CSS
    // text-transform, so the DOM text is not what the screenshot shows and
    // getByText(..., {exact:true}) on the visible string never matches.
    settle: /co-occurring trials/i,
    // "N nodes, M edges" is the load-bearing assertion: a 0-edge graph still
    // renders its nodes and still looks like a graph, which is exactly how the
    // ENTITY_FIELDS defect shipped unnoticed. Regex, so a graph that collapses
    // to zero edges fails rather than matching the literal word "edges".
    markers: ["co-occurrence within", "every number replayable"],
    patterns: [/showing \d+ nodes, [1-9]\d* edges/],
  },
];

const results = [];
const browser = await chromium.launch();

for (const testCase of CASES) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const consoleErrors = [];
  page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text()));
  page.on("pageerror", (e) => consoleErrors.push(String(e)));

  const started = Date.now();
  let missing = [];
  let error = null;
  try {
    await page.goto(UI, { waitUntil: "domcontentloaded" });
    const composer = page.locator('textarea[name="input"]');
    await composer.waitFor({ state: "visible", timeout: 15_000 });
    // Real keystrokes. Three synthetic-input approaches failed against this
    // composer before Playwright's keyboard worked first try -- the "broken
    // composer" was an automation artifact, not a product bug.
    await composer.pressSequentially(testCase.query, { delay: 8 });
    await page.getByRole("button", { name: "Send message" }).click();

    await page
      .getByText(
        testCase.settle,
        testCase.settle instanceof RegExp ? undefined : { exact: true },
      )
      .first()
      .waitFor({ state: "visible", timeout: 120_000 });
    // The trace panel streams after the chart; without this the screenshot
    // catches a half-rendered turn and the evidence is of nothing.
    await page.waitForTimeout(1500);

    const body = await page.locator("body").innerText();
    missing = testCase.markers.filter((m) => !body.includes(m));
    for (const re of testCase.patterns ?? []) {
      if (!re.test(body)) missing.push(String(re));
    }
  } catch (exc) {
    error = String(exc).split("\n")[0];
  }

  await page.screenshot({ path: `${OUT}/${testCase.slug}.png`, fullPage: true });
  results.push({
    slug: testCase.slug,
    query: testCase.query,
    seconds: Math.round((Date.now() - started) / 100) / 10,
    missingMarkers: missing,
    consoleErrors,
    error,
    ok: !error && missing.length === 0 && consoleErrors.length === 0,
  });
  console.log(
    `${results.at(-1).ok ? "OK  " : "FAIL"} ${testCase.slug.padEnd(14)} ` +
      `${results.at(-1).seconds}s ` +
      (missing.length ? `missing=${missing.join(",")} ` : "") +
      (error ?? ""),
  );
  await page.close();
}

await browser.close();
console.log("\n" + JSON.stringify(results, null, 2));
if (results.some((r) => !r.ok)) process.exit(1);
