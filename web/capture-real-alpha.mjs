import { chromium } from "playwright";
import { readFileSync } from "node:fs";

const result = JSON.parse(readFileSync("../evidence/real-alpha-search/result-g2.json", "utf8"));
const test = result.test;
const holdout = result.opened_holdout;
const years = Object.entries(result.year_slices.rows);
const maxAbs = Math.max(...years.map(([, row]) => Math.abs(row.active_annual_return)));
const yearBars = years.map(([year, row]) => {
  const width = Math.abs(row.active_annual_return) / maxAbs * 220;
  const x = row.active_annual_return >= 0 ? 300 : 300 - width;
  const color = row.active_annual_return >= 0 ? "#2e7d61" : "#b44949";
  return `<g transform="translate(0 ${Number(year) - 2008 << 5})"><text x="0" y="17">${year}</text><line x1="300" x2="300" y1="0" y2="22" stroke="#7d8794"/><rect x="${x}" y="4" width="${width}" height="14" rx="3" fill="${color}"/><text x="540" y="17" text-anchor="end">${(row.active_annual_return * 100).toFixed(1)}%</text></g>`;
}).join("");

const html = `<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;background:#f4f1e9;color:#172026;font-family:Inter,Segoe UI,sans-serif}.page{width:1120px;margin:32px auto;padding:42px;background:#fff;border:1px solid #d7d2c7;box-shadow:0 12px 35px #17202618}.eyebrow{font:700 12px ui-monospace;letter-spacing:.16em;color:#69737c}.verdict{margin:12px 0 24px;padding:18px 22px;border-left:6px solid #b44949;background:#fbefec;font-size:24px;font-weight:750}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.card{border:1px solid #d7d2c7;padding:16px}.label{font:600 12px ui-monospace;color:#69737c}.value{font-size:29px;font-weight:760;margin-top:8px}.good{color:#2e7d61}.bad{color:#b44949}h2{font-size:18px;margin:30px 0 12px}.compare{display:grid;grid-template-columns:170px 1fr 1fr;gap:8px}.compare div{padding:10px;background:#f3f5f5}.note{margin-top:24px;padding:16px;background:#eef3f5;font-size:14px;line-height:1.5}svg{width:560px;height:590px;overflow:visible}.cols{display:grid;grid-template-columns:1fr 560px;gap:28px}.facts{font-size:15px;line-height:1.6}.facts li{margin:9px 0}</style></head><body><main class="page">
<div class="eyebrow">NODEQUANT · REAL DAILY DATA · GENERATION 2 · 2026-08-03</div>
<div class="verdict">No robust positive alpha found — promising test result failed the final holdout.</div>
<div class="grid"><div class="card"><div class="label">REGISTERED TRIALS</div><div class="value">18</div></div><div class="card"><div class="label">GEN-2 TEST</div><div class="value good">PASS</div></div><div class="card"><div class="label">FINAL HOLDOUT</div><div class="value bad">FAIL</div></div></div>
<h2>Risk-adjusted comparison</h2><div class="compare"><div></div><div><b>Composite top-2</b></div><div><b>Always long</b></div><div>2020–2022 test</div><div>${test.champion.excess_sharpe.toFixed(3)} Sharpe</div><div>${test.always_long.excess_sharpe.toFixed(3)} Sharpe</div><div>2023–2025 holdout</div><div>${holdout.champion.excess_sharpe.toFixed(3)} Sharpe</div><div>${holdout.always_long.excess_sharpe.toFixed(3)} Sharpe</div><div>Holdout CAGR</div><div>${(holdout.champion.net_cagr*100).toFixed(1)}%</div><div>${(holdout.always_long.net_cagr*100).toFixed(1)}%</div></div>
<div class="cols"><section><h2>What survived</h2><ul class="facts"><li>Genuine adjusted-close panel: <b>${result.data.quality.common_rows.toLocaleString()} common daily rows</b>, 2007–2025.</li><li>Test remained positive at 25 bp one-way turnover costs.</li><li>Every test leave-one-asset-out slice stayed nonnegative.</li><li>Frozen-byte rerun reproduced result hash <code>${result.result_hash.slice(0,24)}…</code>.</li></ul><h2>What killed the claim</h2><ul class="facts"><li>Only <b>${result.year_slices.positive_active_years} of ${result.year_slices.observed_years}</b> full years beat always-long.</li><li>Holdout Sharpe trailed by <b>${(holdout.always_long.excess_sharpe-holdout.champion.excess_sharpe).toFixed(3)}</b>.</li><li>Paired block-bootstrap active-return lower bound: <b class="bad">${(holdout.paired_active_annual_return_bootstrap_95pct_lower*100).toFixed(1)}%</b>.</li><li>Secondary Yahoo observations remain diagnostic, never promotion evidence.</li></ul></section><section><h2>Annual active return vs always-long</h2><svg viewBox="0 0 560 590"><line x1="300" x2="300" y1="0" y2="578" stroke="#b8bec4"/>${yearBars}</svg></section></div>
<div class="note"><b>Interpretation:</b> rotation sometimes provides crisis/regime value, but this candidate did not deliver persistent alpha. This is research analysis, not investment advice or a return guarantee.</div>
</main></body></html>`;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 1180 }, deviceScaleFactor: 1.5 });
await page.setContent(html, { waitUntil: "load" });
await page.screenshot({ path: "../evidence/real-alpha-search/after.png", fullPage: true });
console.log(JSON.stringify({ verdict: result.verdict, resultHash: result.result_hash, screenshot: "evidence/real-alpha-search/after.png" }));
await browser.close();
