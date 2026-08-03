/**
 * What actually happens when you interact with the network graph in the chat UI?
 *
 * Reading the component says "nothing but the trace disclosure". This asserts it
 * against the live DOM, because a grep for onClick misses delegated handlers,
 * anchor tags, and anything a library attaches at runtime -- and the answer here
 * is going into a demo, so it needs to be observed rather than inferred.
 *
 *   node probe-graph-interaction.mjs
 */

import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const UI = process.env.UI_URL ?? "http://localhost:3000";
const OUT = "evidence/graph-interaction";
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const findings = {};

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
await page.waitForTimeout(1500);

// --- 1. Is there any search or filter input on the page at all? -----------
findings.searchInputsInResult = await page.evaluate(() => {
  const inputs = [...document.querySelectorAll("input, [role=searchbox]")];
  return inputs.map((el) => ({
    type: el.getAttribute("type"),
    placeholder: el.getAttribute("placeholder"),
    // The thread-list sidebar has its own search; only inputs INSIDE the
    // message matter for "can I search the graph".
    insideMessage: !!el.closest("[data-testid=message], .aui-assistant-message"),
  }));
});

// --- 2. Do node chips carry any handler, href, or affordance? -------------
const chip = page.getByText("Pembrolizumab", { exact: false }).first();
findings.nodeChip = await chip.evaluate((el) => {
  const style = getComputedStyle(el);
  return {
    tag: el.tagName,
    cursor: style.cursor,
    role: el.getAttribute("role"),
    tabIndex: el.tabIndex,
    href: el.getAttribute("href"),
    closestInteractive: el.closest("a,button,[onclick],[role=button]")?.tagName ?? null,
  };
});

const beforeClick = await page.locator("body").innerText();
await chip.click({ force: true });
await page.waitForTimeout(700);
const afterClick = await page.locator("body").innerText();
findings.nodeClickChangedPage = beforeClick !== afterClick;
findings.nodeClickChangedUrl = page.url() !== UI && page.url() !== `${UI}/`;
await page.screenshot({ path: `${OUT}/after-node-click.png`, fullPage: true });

// --- 3. The one thing that IS interactive: the trace disclosure -----------
const disclosure = page.getByText(/API calls/).first();
findings.traceBeforeExpand = (await page.locator("body").innerText()).includes(
  "clinicaltrials.gov/api/v2",
);
await disclosure.click();
await page.waitForTimeout(700);
const expanded = await page.locator("body").innerText();
findings.traceAfterExpand = expanded.includes("clinicaltrials.gov/api/v2");
findings.traceShowsCountTotal = expanded.includes("countTotal");
await page.screenshot({ path: `${OUT}/trace-expanded.png`, fullPage: true });

// --- 4. Keyboard reachability of the graph content ------------------------
findings.focusableInsideGraph = await page.evaluate(() => {
  const headings = [...document.querySelectorAll("*")].filter((el) =>
    /co-occurring trials/i.test(el.textContent ?? "") && el.children.length < 3,
  );
  const panel = headings[0]?.closest("div")?.parentElement;
  if (!panel) return "panel not found";
  return panel.querySelectorAll("a,button,input,[tabindex]:not([tabindex='-1'])")
    .length;
});

await browser.close();
console.log(JSON.stringify(findings, null, 2));
