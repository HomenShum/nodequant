import { chromium } from "playwright";

const UI = process.env.UI_URL ?? "http://localhost:3201";
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
await page.goto(`${UI}/research-graph`, { waitUntil: "domcontentloaded", timeout: 120000 });
await page.locator("[data-testid=research-graph-canvas] canvas").first().waitFor({ state: "visible", timeout: 120000 });
await page.getByLabel("Search evidence").fill("CFTC accounting-contract invalidation");
await page.waitForTimeout(900);
const detail = await page.locator("aside").innerText();
await page.screenshot({ path: "../evidence/cftc-flow/before.png", fullPage: true });
console.log(JSON.stringify({ staleFactorInspector: detail.includes("Developed ex-U.S. factor replication") }, null, 2));
await browser.close();
