/**
 * End-to-end check of the assistant-ui demo with real keyboard input.
 *
 * Playwright exercises the same controlled composer path as a person typing,
 * then verifies every required streamed panel in the live DOM.
 *
 *   node e2e-check.mjs
 */

import { chromium } from "playwright";

const UI = process.env.UI_URL ?? "http://localhost:3000";
const QUERY = "How are melanoma trials distributed across phases?";
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

const chatCalls = [];
const consoleErrors = [];
page.on("request", (request) => {
  if (request.url().includes("/api/trialscope-chat")) {
    chatCalls.push({ method: request.method(), url: request.url() });
  }
});
page.on("console", (message) => {
  if (message.type() === "error") consoleErrors.push(message.text());
});
page.on("pageerror", (error) => consoleErrors.push(String(error)));

await page.goto(UI, { waitUntil: "domcontentloaded" });
const composer = page.locator('textarea[name="input"]');
await composer.waitFor({ state: "visible", timeout: 15_000 });
await composer.pressSequentially(QUERY, { delay: 12 });

const send = page.getByRole("button", { name: "Send message" });
const enabledAfterTyping = !(await send.isDisabled());
if (!enabledAfterTyping) {
  throw new Error("Send stayed disabled after real keyboard input");
}

await send.click();
await page.getByText("Reconciliation", { exact: true }).waitFor({
  state: "visible",
  timeout: 90_000,
});

const bodyText = await page.locator("body").innerText();
const markers = [
  "PHASE2",
  "Reconciliation",
  "bucket sum",
  "3,602",
  "3,738",
  "Sources",
  "API calls",
  "every number replayable",
];
const missing = markers.filter((marker) => !bodyText.includes(marker));

await page.screenshot({
  path: "evidence/assistant-ui-e2e/e2e-check-3000.png",
  fullPage: true,
});
await browser.close();

console.log(
  JSON.stringify(
    {
      url: UI,
      enabledAfterTyping,
      chatCalls,
      missingMarkers: missing,
      consoleErrors,
    },
    null,
    2,
  ),
);

if (chatCalls.length !== 1 || missing.length > 0 || consoleErrors.length > 0) {
  process.exit(1);
}
