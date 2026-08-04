import { chromium } from "playwright";
import { readFileSync } from "node:fs";

const result = JSON.parse(readFileSync("../evidence/breadth-first-alpha-v4/result.json", "utf8"));
const before = readFileSync("../evidence/breadth-first-alpha/before.txt", "utf8");
if (result.version !== "nodequant-alpha-breadth-result-v1" || result.promotion_eligible !== false) {
  throw new Error("unexpected alpha-breadth result contract");
}
const breadth = result.search_breadth;
const rep = result.replication;
const champion = rep.trials[rep.champion_id];
const baseline = rep.trials[rep.baseline_id];
if (!champion || !baseline || !Number.isInteger(breadth.total_measured_trials)) {
  throw new Error("scoreboard inputs are incomplete");
}
const quality = result.data.replication_quality;
const qualityRows = Object.values(quality.symbols);
const nulls = qualityRows.reduce((sum, row) => sum + row.null_rows, 0);
const duplicates = qualityRows.reduce((sum, row) => sum + row.duplicate_rows, 0);
const invalid = qualityRows.reduce((sum, row) => sum + row.invalid_rows, 0);
const noncommon = Object.values(quality.noncommon_rows_dropped).reduce((sum, value) => sum + value, 0);
const championSpec = champion.candidate;
const championLabel = championSpec.rule === "composite_momentum"
  ? `Composite momentum · top ${championSpec.top_n}`
  : championSpec.id.replaceAll("__", " · ").replaceAll("_", " ");
const winners = [...result.discovery.family_winners]
  .sort((a, b) => b.robust_excess_sharpe_floor - a.robust_excess_sharpe_floor);
const maxFloor = Math.max(...winners.map((row) => Math.abs(row.robust_excess_sharpe_floor)));
const bars = winners.map((row) => {
  const width = Math.max(2, Math.abs(row.robust_excess_sharpe_floor) / maxFloor * 240);
  const negative = row.robust_excess_sharpe_floor < 0;
  const color = row.discovery_survivor ? "#16745b" : negative ? "#b94b4b" : "#74818c";
  const status = row.discovery_survivor ? "✓ SURVIVOR" : negative ? "× NEGATIVE" : "— NOT SURVIVOR";
  return `<div class="barrow"><span>${row.family.replaceAll("_", " ")}</span><i style="width:${width}px;background:${color}"></i><b>${row.robust_excess_sharpe_floor.toFixed(3)}</b><em>${status}</em></div>`;
}).join("");
const escape = (value) => value.replaceAll("&", "&amp;").replaceAll("<", "&lt;");
const card = (label, value, cls = "") => `<div class="card"><small>${label}</small><strong class="${cls}">${value}</strong></div>`;

const html = `<!doctype html><html><head><meta charset="utf-8"><style>
*{box-sizing:border-box}body{margin:0;background:#ece9e1;color:#172128;font-family:Inter,Segoe UI,sans-serif}.page{width:1320px;margin:28px auto;background:#fff;padding:42px 48px;border:1px solid #cdc8bc;box-shadow:0 18px 50px #17212818}.eyebrow{font:700 12px ui-monospace;letter-spacing:.16em;color:#6d7780}.headline{font-size:30px;font-weight:780;margin:10px 0 6px}.sub{color:#53606a;font-size:16px;margin-bottom:24px}.beforeafter{display:grid;grid-template-columns:1fr 1.7fr;gap:18px}.pane{border:1px solid #d5d8d9;padding:20px;background:#fafafa}.pane h2{font-size:15px;margin:0 0 14px}.before pre{white-space:pre-wrap;font:12px/1.55 ui-monospace;color:#6d2832}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.card{background:#f0f4f4;padding:12px}.card small{display:block;font:700 10px ui-monospace;color:#65717a}.card strong{display:block;font-size:25px;margin-top:6px}.bad{color:#b23c45}.compare{display:grid;grid-template-columns:1.35fr 1fr 1fr;gap:7px;margin-top:14px}.compare div{padding:8px;background:#f1f3f3}.compare .head{font-weight:700}.warning{margin-top:14px;padding:12px;background:#fff0e8;border-left:5px solid #ca663b}.lower{display:grid;grid-template-columns:1.2fr 1fr;gap:24px;margin-top:26px}h3{font-size:16px;margin:0 0 12px}.barrow{height:27px;display:grid;grid-template-columns:190px 250px 50px 110px;gap:5px;align-items:center;font-size:11px}.barrow i{height:12px;border-radius:3px}.barrow b{text-align:right}.barrow em{font:700 9px ui-monospace;color:#53606a}.facts{line-height:1.55;font-size:14px}.receipt{font:11px/1.5 ui-monospace;background:#edf3f5;padding:14px;overflow-wrap:anywhere}.legend{font-size:12px;color:#66727b;margin-top:10px}</style></head><body><main class="page">
<div class="eyebrow">NODEQUANT · BREADTH-FIRST ALPHA RESEARCH · 2026-08-03</div><div class="headline">Positive panel winner found; uncertainty prevents an alpha claim.</div><div class="sub">The observable changed from no breadth runner to ${breadth.registered_discovery_candidates} candidates, ${breadth.strategy_families} families, an evaluator-bound pre-fetch seal, and ${breadth.total_measured_trials} preserved measurements.</div>
<section class="beforeafter"><div class="pane before"><h2>BEFORE · preserved pre-edit command capture</h2><pre>${escape(before)}</pre></div><div class="pane"><h2>AFTER · measured replication scoreboard</h2><div class="grid">${card("DISCOVERY", breadth.registered_discovery_candidates)}${card("FAMILIES", breadth.strategy_families)}${card("TOTAL TRIALS", breadth.total_measured_trials)}${card("VERDICT", rep.status, rep.status === "PASS" ? "" : "bad")}</div><div class="compare"><div></div><div class="head">${championLabel}</div><div class="head">Always-long</div><div>Robust excess-Sharpe floor</div><div>${champion.robust_excess_sharpe_floor.toFixed(3)}</div><div>${baseline.robust_excess_sharpe_floor.toFixed(3)}</div><div>Excess Sharpe</div><div>${champion.all_period.excess_sharpe.toFixed(3)}</div><div>${baseline.all_period.excess_sharpe.toFixed(3)}</div><div>Net CAGR</div><div>${(champion.all_period.net_cagr*100).toFixed(2)}%</div><div>${(baseline.all_period.net_cagr*100).toFixed(2)}%</div><div>Maximum drawdown</div><div>${(champion.all_period.max_drawdown*100).toFixed(2)}%</div><div>${(baseline.all_period.max_drawdown*100).toFixed(2)}%</div></div><div class="warning"><b>Why this is not alpha evidence:</b> ${rep.folds_beating_baseline} of ${Object.keys(champion.folds).length} folds beat baseline; paired block-bootstrap 95% lower bound on annual active return = <b>${(rep.paired_active_annual_return_bootstrap_95pct_lower*100).toFixed(2)}%</b>.</div></div></section>
<section class="lower"><div><h3>Discovery family winners · robust excess-Sharpe floor</h3>${bars}<div class="legend">Status is encoded in text and color; every family winner remains in the trial record.</div></div><div><h3>Evidence receipt</h3><div class="receipt">selection ${result.discovery.selection_seal.seal_hash}<br>evaluator ${result.discovery.selection_seal.evaluator_hash}<br>replication panel ${result.data.replication_panel_hash}<br>result ${result.result_hash}<br><br>${quality.common_rows.toLocaleString()} common rows · ${quality.first_common_date} → ${quality.last_common_date}<br>${nulls} null · ${duplicates} duplicate · ${invalid} invalid · ${noncommon} noncommon</div><ul class="facts"><li>${result.limitations[0]}</li><li>The replication set and evaluator were sealed before final-universe fetch.</li><li>Independent raw-JSON/Numpy recount reproduced the current metrics.</li><li>Promotion eligible: <b>${String(result.promotion_eligible)}</b>.</li></ul></div></section>
</main></body></html>`;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1400, height: 1140 }, deviceScaleFactor: 1.5 });
await page.setContent(html, { waitUntil: "load" });
await page.screenshot({ path: "../evidence/breadth-first-alpha-v4/comparison.png", fullPage: true });
console.log(JSON.stringify({ screenshot: "evidence/breadth-first-alpha-v4/comparison.png", resultHash: result.result_hash }));
await browser.close();
