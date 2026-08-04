import { chromium } from "playwright";
import { readFileSync } from "node:fs";

const source = readFileSync("../evidence/crypto-seasonality/comparison-v2.png").toString("base64");
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 2100, height: 1575 } });
await page.setContent(`<!doctype html><style>
*{box-sizing:border-box}body{margin:0;overflow:hidden;background:#eee}img{width:2100px;height:1575px;display:block;filter:grayscale(.75);opacity:.30}.frame{position:absolute;inset:34px;border:28px solid #a40000;transform:rotate(-5deg)}.stamp{position:absolute;left:220px;top:570px;width:1660px;padding:54px 34px;border:20px solid #a40000;background:#fff;color:#a40000;font:900 70px/1.15 Segoe UI;text-align:center;letter-spacing:.035em;transform:rotate(-5deg)}.stamp small{font-size:42px}</style><img src="data:image/png;base64,${source}"><div class="frame"></div><div class="stamp">V2 INVALIDATED<br><small>COINBASE BUCKET START TREATED AS CLOSE INSTANT</small></div>`, { waitUntil: "load" });
await page.screenshot({ path: "../evidence/crypto-seasonality/comparison-v2.png", fullPage: false });
await browser.close();
