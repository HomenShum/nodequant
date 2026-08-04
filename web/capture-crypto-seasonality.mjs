import { chromium } from "playwright";
import { readFileSync } from "node:fs";

const result = JSON.parse(readFileSync("../evidence/crypto-seasonality/result-v2.json", "utf8"));
const beforeBytes = readFileSync("../evidence/crypto-seasonality/before/command.txt");
const before = beforeBytes[0] === 0xff && beforeBytes[1] === 0xfe
  ? beforeBytes.toString("utf16le").replace(/^\ufeff/, "")
  : beforeBytes.toString("utf8").replace(/^\ufeff/, "");
if (result.version !== "nodequant-crypto-seasonality-result-v1" || result.promotion_eligible !== false) {
  throw new Error("unexpected crypto result contract");
}
const replication = result.replication;
const champion = replication.trials[replication.champion_id];
const baseline = replication.trials[replication.baseline_id];
const quality = result.data.replication_quality;
if (!champion || !baseline || result.search_breadth.total_measured_trials !== 111) {
  throw new Error("crypto scoreboard inputs are incomplete");
}
if (replication.status !== "FAIL" || result.verdict !== "NO_REPLICATION_SURVIVOR") {
  throw new Error("crypto verdict differs from the frozen negative-result contract");
}
const pct = (value, digits = 2) => `${(value * 100).toFixed(digits)}%`;
const num = (value, digits = 3) => Number(value).toFixed(digits);
const escape = (value) => value.replaceAll("&", "&amp;").replaceAll("<", "&lt;");
const checkLabels = {
  robust_floor_improves: "Robust floor improves by at least 0.10",
  minimum_fold_breadth: "Beats buy-and-hold in at least 2/3 years",
  positive_net_cagr: "Positive net CAGR",
  paired_active_bootstrap_lower_positive: "Paired bootstrap lower bound is positive",
};
const checks = Object.entries(replication.checks).map(([key, passed]) =>
  `<div class="check ${passed ? "pass" : "fail"}"><b>${passed ? "PASS" : "FAIL"}</b><span>${checkLabels[key]}</span></div>`
).join("");
const winners = result.discovery_family_winners.map((winner) =>
  `<tr><td>${winner.family.replaceAll("_", " ")}</td><td>${winner.candidate.id}</td><td>${num(winner.robust_net_sharpe_floor)}</td><td>${num(winner.floor_improvement)}</td><td>${winner.folds_beating_baseline}/3</td><td>${winner.discovery_survivor ? "YES" : "NO"}</td></tr>`
).join("");
const html = `<!doctype html><html><head><meta charset="utf-8"><style>
*{box-sizing:border-box}body{margin:0;background:#e9e7df;color:#172126;font-family:Inter,Segoe UI,sans-serif}.page{width:1320px;margin:26px auto;background:#fff;border:1px solid #c9c6bc;box-shadow:0 18px 52px #1c282c20;padding:40px 46px}.eyebrow{font:700 11px ui-monospace;letter-spacing:.17em;color:#66747a}.headline{font-size:30px;line-height:1.12;font-weight:780;margin:10px 0 8px}.sub{font-size:15px;color:#56636a;max-width:1080px;margin-bottom:22px}.two{display:grid;grid-template-columns:.9fr 1.7fr;gap:18px}.pane{border:1px solid #d4d7d5;padding:19px;background:#fafafa}.pane h2,.section h2{font-size:14px;margin:0 0 13px}.before pre{white-space:pre-wrap;font:11px/1.5 ui-monospace;color:#7a3038}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:9px}.card{background:#eef2f1;padding:12px}.card small{display:block;font:700 9px ui-monospace;color:#647177}.card strong{display:block;font-size:23px;margin-top:5px}.bad{color:#ad3f45}.compare{display:grid;grid-template-columns:1.3fr 1fr 1fr;gap:6px;margin-top:13px}.compare div{padding:7px 9px;background:#f0f2f1;font-size:12px}.compare .head{font-weight:750}.lead{margin-top:13px;padding:12px;background:#edf6f1;border-left:5px solid #2e8161;font-size:13px}.lead b{color:#17664d}.checks{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:12px}.check{display:flex;gap:9px;align-items:center;padding:8px 10px;font-size:11px}.check b{font:800 9px ui-monospace}.check.pass{background:#e6f3ec;color:#17664d}.check.fail{background:#f8e9e8;color:#9e343a}.lower{display:grid;grid-template-columns:1.45fr .8fr;gap:22px;margin-top:24px}.section{border-top:2px solid #263238;padding-top:14px}table{width:100%;border-collapse:collapse;font-size:10px}th{text-align:left;color:#647078;font:700 9px ui-monospace;padding:7px;border-bottom:1px solid #bbc1c0}td{padding:7px;border-bottom:1px solid #e0e3e2}.receipt{font:10px/1.55 ui-monospace;background:#edf2f3;padding:13px;overflow-wrap:anywhere}.note{font-size:11px;line-height:1.5;color:#56636a;margin-top:10px}.warn{margin-top:16px;padding:12px 14px;background:#fff1e5;border-left:5px solid #ce6b35;font-size:12px}</style></head><body><main class="page">
<div class="eyebrow">NODEQUANT / INDEPENDENT-MECHANISM TEST / 2026-08-04</div>
<div class="headline">Wednesday improved ETH risk efficiency; it did not survive the frozen alpha gate.</div>
<div class="sub">BTC 2020-2022 selected one rule per family. An externally timestamped authority then opened untouched ETH 2023-2025 data. Values below are loaded from the sealed result, not typed into this page.</div>
<section class="two"><div class="pane before"><h2>BEFORE / preserved observable</h2><pre>${escape(before)}</pre></div><div class="pane"><h2>AFTER / ETH replication measurement</h2><div class="cards"><div class="card"><small>DISCOVERY CANDIDATES</small><strong>${result.search_breadth.discovery_candidates}</strong></div><div class="card"><small>MECHANISM FAMILIES</small><strong>${result.search_breadth.families}</strong></div><div class="card"><small>TOTAL MEASURED TRIALS</small><strong>${result.search_breadth.total_measured_trials}</strong></div><div class="card"><small>VERDICT</small><strong class="bad">${replication.status}</strong></div></div>
<div class="compare"><div></div><div class="head">Wednesday only</div><div class="head">ETH buy-and-hold</div><div>Net CAGR</div><div>${pct(champion.all_period.net_cagr)}</div><div>${pct(baseline.all_period.net_cagr)}</div><div>Net Sharpe</div><div>${num(champion.all_period.net_sharpe)}</div><div>${num(baseline.all_period.net_sharpe)}</div><div>Maximum drawdown</div><div>${pct(champion.all_period.max_drawdown)}</div><div>${pct(baseline.all_period.max_drawdown)}</div><div>Robust Sharpe floor</div><div>${num(champion.robust_net_sharpe_floor)}</div><div>${num(baseline.robust_net_sharpe_floor)}</div></div>
<div class="lead"><b>Measured lead, not alpha:</b> Wednesday reduced maximum-drawdown magnitude by ${(100*(Math.abs(baseline.all_period.max_drawdown)-Math.abs(champion.all_period.max_drawdown))).toFixed(2)} percentage points and raised full-period Sharpe by ${num(champion.all_period.net_sharpe-baseline.all_period.net_sharpe)}, but earned lower CAGR and lacked temporal breadth.</div><div class="checks">${checks}</div></div></section>
<section class="lower"><div class="section"><h2>BTC discovery winners frozen into replication</h2><table><thead><tr><th>FAMILY</th><th>CANDIDATE</th><th>ROBUST FLOOR</th><th>VS BASELINE</th><th>FOLD WINS</th><th>SURVIVOR</th></tr></thead><tbody>${winners}</tbody></table><div class="warn"><b>Why the ETH lead failed:</b> robust-floor improvement = ${num(champion.robust_net_sharpe_floor-baseline.robust_net_sharpe_floor)} against a 0.100 minimum; annual fold wins = ${replication.folds_beating_baseline}/3; paired active annual-return bootstrap 95% lower bound = ${pct(replication.paired_active_annual_return_bootstrap_95pct_lower)}.</div></div><div class="section"><h2>Evidence receipt</h2><div class="receipt">policy ${result.policy_hash}<br>evaluator ${result.evaluator_hash}<br>selection ${result.selection_seal_hash}<br>ETH panel ${result.data.replication_panel_hash}<br>result ${result.result_hash}<br><br>${quality.usable_hours.toLocaleString()} / ${quality.expected_hours.toLocaleString()} hours<br>${quality.missing_hours} missing / ${quality.duplicate_rows} duplicate / ${quality.invalid_rows} invalid<br>${champion.all_period.days.toLocaleString()} eligible UTC days</div><div class="note">Public Coinbase candles are diagnostic, not executable quotes. BTC-to-ETH changes asset and time, but not venue. Promotion eligible: <b>${String(result.promotion_eligible)}</b>.</div></div></section>
</main></body></html>`;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1400, height: 1050 }, deviceScaleFactor: 1.5 });
await page.setContent(html, { waitUntil: "load" });
await page.screenshot({ path: "../evidence/crypto-seasonality/comparison-v2.png", fullPage: true });
console.log(JSON.stringify({ screenshot: "evidence/crypto-seasonality/comparison-v2.png", resultHash: result.result_hash }));
await browser.close();
