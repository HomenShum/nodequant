/**
 * Thread lifecycle: new thread, switching, history persistence, thread search.
 *
 * Missing from the evidence set entirely until it was asked for, which is the
 * point -- the chart panels got captured because they are what the assignment
 * names, and the shell around them carried a whole conversation model that
 * nobody had looked at.
 *
 *   node capture-threads.mjs
 */
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const UI = process.env.UI_URL ?? "http://localhost:3000";
const OUT = "evidence/threads";
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const f = {};
const ask = async (q, settle) => {
  const composer = page.locator('textarea[name="input"]');
  await composer.waitFor({ state: "visible", timeout: 15_000 });
  await composer.pressSequentially(q, { delay: 6 });
  await page.getByRole("button", { name: "Send message" }).click();
  await page.getByText(settle).first().waitFor({ state: "visible", timeout: 120_000 });
  await page.waitForTimeout(1200);
};
const threadCount = () =>
  page.locator("[data-testid=threadlist-item], aside a, aside li").count();

await page.goto(UI, { waitUntil: "domcontentloaded" });
await page.screenshot({ path: `${OUT}/1-empty-state.png`, fullPage: true });
f.threadsAtStart = await threadCount();

await ask("How are melanoma trials distributed across phases?", "Reconciliation");
await page.screenshot({ path: `${OUT}/2-first-thread-answered.png`, fullPage: true });

// New Thread must produce an EMPTY composer, not carry the last turn over.
await page.getByRole("button", { name: /new thread/i }).click();
await page.waitForTimeout(900);
f.newThreadHandlerFired = await page.evaluate(()=>window.__newThreadFired ?? 0);
  f.bodyAfterNewThreadHasOldChart = (await page.locator("body").innerText()).includes("PHASE2");
await page.screenshot({ path: `${OUT}/3-new-thread-empty.png`, fullPage: true });

await ask("How many pembrolizumab trials are there?", "every number replayable");
await page.screenshot({ path: `${OUT}/4-second-thread.png`, fullPage: true });
f.threadsAfterTwo = await threadCount();

// Switching back must restore the FIRST thread's answer, not re-run it.
const items = page.locator("aside li, [data-testid=threadlist-item]");
if ((await items.count()) > 1) {
  await items.nth(1).click();
  await page.waitForTimeout(1200);
  const body = await page.locator("body").innerText();
  f.switchedBackShowsFirstChart = body.includes("PHASE2");
  await page.screenshot({ path: `${OUT}/5-switched-back.png`, fullPage: true });
}

// Thread search: filter-as-you-type over conversations (distinct from the node
// finder inside the graph -- same pattern, different scope).
const search = page.getByPlaceholder(/search threads/i);
f.threadSearchExists = await search.isVisible().catch(() => false);
if (f.threadSearchExists) {
  await search.fill("pembro");
  await page.waitForTimeout(700);
  f.threadsMatchingPembro = await threadCount();
  await page.screenshot({ path: `${OUT}/6-thread-search.png`, fullPage: true });
}

// Reload: does history survive, or is it in-memory only?
await page.reload({ waitUntil: "domcontentloaded" });
await page.waitForTimeout(2000);
f.threadsAfterReload = await threadCount();
f.bodyAfterReloadHasAnyChart = (await page.locator("body").innerText()).includes("PHASE2");
await page.screenshot({ path: `${OUT}/7-after-reload.png`, fullPage: true });

await browser.close();
console.log(JSON.stringify(f, null, 2));
