import { chromium } from "playwright";
import { readFileSync } from "node:fs";

const source = readFileSync("../evidence/breadth-first-alpha/comparison.png").toString("base64");
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 2040, height: 1782 } });
await page.setContent(`<!doctype html><style>*{box-sizing:border-box}body{margin:0}img{width:2040px;height:1782px;display:block;filter:grayscale(.65);opacity:.38}.mark{position:absolute;inset:0;display:grid;place-items:center;border:36px solid #a40000;color:#a40000;font:900 92px/1 Segoe UI;letter-spacing:.08em;text-align:center;text-shadow:0 3px #fff;transform:rotate(-13deg)}.mark span{border:18px solid #a40000;padding:40px;background:#ffffffd9}</style><img src="data:image/png;base64,${source}"><div class="mark"><span>INVALIDATED<br><small>BASELINE DELAY ASYMMETRY</small></span></div>`, { waitUntil: "load" });
await page.screenshot({ path: "../evidence/breadth-first-alpha/comparison.png" });
await browser.close();
