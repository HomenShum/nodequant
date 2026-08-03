/**
 * Assert the node finder's behaviour against the same renderer the chat embed
 * uses (app/neuromap.py), loaded directly rather than through the sandboxed
 * iframe -- whose DOM is deliberately unreachable, see capture-neuromap.mjs.
 *
 *   node probe-node-finder.mjs   (needs /tmp/neuromap-fixture.html)
 */
import { chromium } from "playwright";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 760 } });
await page.goto("file://D:/VSCode Projects/stock-ai-dipsea/web/evidence/neuromap-embed/fixture.html");
await page.waitForFunction(() => window.cy && window.cy.nodes().length > 0, { timeout: 20_000 });

const lit = () => page.evaluate(() => window.cy.elements(".hot").length);
const faded = () => page.evaluate(() => window.cy.elements(".faded").length);
const hint = () => page.locator("#findhint").textContent();
const find = page.locator("#find");
const out = {};

out.totalNodes = await page.evaluate(() => window.cy.nodes().length);

await find.fill("nivo");
await page.waitForTimeout(250);
out.match_nivo = { lit: await lit(), hint: await hint() };

// Case-insensitivity is the whole point of a type-to-search over proper nouns.
await find.fill("NIVO");
await page.waitForTimeout(250);
out.match_NIVO_uppercase = { lit: await lit(), hint: await hint() };

// A multi-node match must also light the edges BETWEEN matches, or a pair of
// connected sponsors loses the relationship that makes the pair interesting.
await find.fill("cancer");
await page.waitForTimeout(250);
out.match_multi = { lit: await lit(), hint: await hint() };

// No match must LOOK empty. A fully lit graph reads as "no filter applied".
await find.fill("zzzznotathing");
await page.waitForTimeout(250);
out.no_match = { lit: await lit(), faded: await faded(), hint: await hint() };

await find.press("Escape");
await page.waitForTimeout(250);
out.after_escape = { lit: await lit(), faded: await faded(), value: await find.inputValue() };

await browser.close();
console.log(JSON.stringify(out, null, 2));
