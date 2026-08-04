import { chromium } from "playwright";
import { readFileSync } from "node:fs";

const source = readFileSync("../evidence/prospective-alpha-v2/comparison.png").toString("base64");
const browser = await chromium.launch();
const page = await browser.newPage({viewport:{width:1400,height:1148},deviceScaleFactor:1.5});
await page.setContent(`<!doctype html><html><head><style>*{box-sizing:border-box}body{margin:0;background:#eee;overflow:hidden}.old{width:1400px;opacity:.18;filter:grayscale(1)}.frame{position:fixed;inset:28px;border:16px solid #b40000;transform:rotate(-6deg);z-index:2}.stamp{position:fixed;left:170px;top:440px;width:1060px;padding:34px;border:12px solid #b40000;color:#b40000;background:#fff;font:900 50px/1.15 Arial;text-align:center;transform:rotate(-6deg);z-index:3}</style></head><body><img class="old" src="data:image/png;base64,${source}"><div class="frame"></div><div class="stamp">V2 INVALIDATED<br>WRITE-ONCE + POLICY CHRONOLOGY DEFECTS</div></body></html>`);
await page.screenshot({path:"../evidence/prospective-alpha-v2/comparison.png",fullPage:false});
await browser.close();
