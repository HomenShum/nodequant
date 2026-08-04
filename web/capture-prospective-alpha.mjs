import { chromium } from "playwright";
import { readFileSync } from "node:fs";

const packet = JSON.parse(readFileSync("../evidence/prospective-alpha-v3/packet-2026-08.json", "utf8"));
const invalidV1 = JSON.parse(readFileSync("../evidence/prospective-alpha/packet-2026-08.json", "utf8"));
const invalidV2 = JSON.parse(readFileSync("../evidence/prospective-alpha-v2/packet-2026-08.json", "utf8"));
const before = readFileSync("../evidence/prospective-alpha/before/command.txt", "utf8")
  .replace(/[^\x09\x0A\x0D\x20-\x7E]/g, "");
if (packet.status !== "FROZEN_AWAITING_FUTURE_OUTCOMES" || packet.outcome_boundary.outcomes_present !== false) {
  throw new Error("unexpected prospective packet contract");
}
if (!String(invalidV1.status).startsWith("INVALIDATED_") || !String(invalidV2.status).startsWith("INVALIDATED_")) {
  throw new Error("v1/v2 lineage is not visibly invalidated");
}
const esc = (value) => String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;");
const pct = (value) => `${(value * 100).toFixed(1)}%`;
const rows = packet.forecasts.map((forecast) => {
  const allocations = Object.entries(forecast.risky_weights)
    .filter(([, weight]) => weight > 1e-12)
    .map(([symbol, weight]) => `${symbol} ${pct(weight)}`).join(" · ");
  return `<tr><td><b>${esc(forecast.role)}</b><small>${esc(forecast.strategy_id)}</small></td><td>${esc(allocations || "No risky exposure")}</td><td>${pct(forecast.cash_weight)}</td></tr>`;
}).join("");
const html = `<!doctype html><html><head><meta charset="utf-8"><style>
*{box-sizing:border-box}body{margin:0;background:#ece9e1;color:#172128;font-family:Inter,Segoe UI,sans-serif}.page{width:1320px;margin:28px auto;background:#fff;padding:44px 50px;border:1px solid #cbc7bc;box-shadow:0 18px 50px #17212818}.eyebrow{font:700 12px ui-monospace;letter-spacing:.15em;color:#68737b}.headline{font-size:31px;font-weight:780;margin:10px 0 6px}.sub{font-size:16px;color:#53606a;margin-bottom:24px}.split{display:grid;grid-template-columns:.85fr 1.6fr;gap:18px}.pane{border:1px solid #d4d8d9;padding:20px;background:#fafafa}.pane h2{font-size:15px;margin:0 0 14px}.before pre{white-space:pre-wrap;font:12px/1.5 ui-monospace;color:#6d2832}.invalid{margin-top:16px;border:4px solid #b5232d;padding:15px;color:#b5232d;font-weight:800;transform:rotate(-1deg)}.receipt{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:15px}.card{background:#edf3f4;padding:12px}.card small{display:block;font:700 10px ui-monospace;color:#627079}.card strong{display:block;margin-top:6px;font-size:16px;overflow-wrap:anywhere}.good{color:#16745b}table{width:100%;border-collapse:separate;border-spacing:0 7px}td{padding:11px;background:#f0f3f3}td:first-child{width:42%}td small{display:block;font:10px/1.4 ui-monospace;color:#60707a;margin-top:4px}.boundary{margin-top:15px;padding:14px;border-left:5px solid #23705c;background:#eaf5f1;line-height:1.5}.lower{display:grid;grid-template-columns:1fr 1fr;gap:22px;margin-top:24px}.hashes{font:11px/1.6 ui-monospace;background:#edf3f5;padding:15px;overflow-wrap:anywhere}.gate{font-size:14px;line-height:1.6}.note{color:#5c6870;font-size:12px;margin-top:10px}</style></head><body><main class="page">
<div class="eyebrow">NODEQUANT · PROSPECTIVE ALPHA EVIDENCE · 2026-08-04</div><div class="headline">A forecast now exists before its measured return window.</div><div class="sub">This is chronology evidence, not positive alpha evidence. Outcomes are absent and promotion remains false.</div>
<section class="split"><div class="pane before"><h2>BEFORE · no prospective packet runner</h2><pre>${esc(before)}</pre><div class="invalid">V1 + V2 INVALIDATED<br>TEMPORAL + WRITE-ONCE DEFECTS</div></div><div class="pane"><h2>AFTER · v3 frozen forecast</h2><div class="receipt"><div class="card"><small>STATUS</small><strong class="good">AWAITING OUTCOMES</strong></div><div class="card"><small>SIGNAL CUTOFF</small><strong>${packet.data.signal_cutoff}</strong></div><div class="card"><small>OBSERVED THROUGH</small><strong>${packet.data.observed_through}</strong></div><div class="card"><small>OUTCOMES PRESENT</small><strong>${String(packet.outcome_boundary.outcomes_present).toUpperCase()}</strong></div></div><table><tbody>${rows}</tbody></table><div class="boundary"><b>Temporal boundary:</b> sealed ${packet.outcome_boundary.sealed_at_utc}. Entry is the first common close strictly after the seal; measured returns begin with the following close-to-close interval.</div></div></section>
<section class="lower"><div><h2>Content-addressed receipt</h2><div class="hashes">packet ${packet.packet_hash}<br>policy ${packet.policy_hash}<br>evaluator ${packet.evaluator_hash}<br>source result ${packet.source_result_hash}<br>panel ${packet.data.panel_hash}</div></div><div><h2>Future gate</h2><div class="gate">Minimum complete packets: <b>${packet.future_gate.minimum_complete_packets}</b><br>Minimum future sessions: <b>${packet.future_gate.minimum_complete_sessions}</b><br>Packets beating benchmark: <b>${packet.future_gate.minimum_packets_beating_benchmark}</b><br>Bootstrap lower bound must exceed: <b>${pct(packet.future_gate.paired_active_annual_return_95pct_lower_must_exceed)}</b><br>Promotion eligible: <b>${String(packet.promotion_eligible)}</b></div><div class="note">The three risk-weighted comparisons currently produce identical allocations; they are redundant comparisons, not independent confirmations.</div></div></section>
</main></body></html>`;
const browser = await chromium.launch();
const page = await browser.newPage({viewport:{width:1400,height:1050},deviceScaleFactor:1.5});
await page.setContent(html,{waitUntil:"load"});
await page.screenshot({path:"../evidence/prospective-alpha-v3/comparison.png",fullPage:true});
console.log(JSON.stringify({screenshot:"evidence/prospective-alpha-v3/comparison.png",packetHash:packet.packet_hash}));
await browser.close();
