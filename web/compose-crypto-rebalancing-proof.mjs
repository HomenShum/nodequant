import sharp from "sharp";

const before = await sharp("../evidence/crypto-rebalancing/before.png").resize({ width: 900 }).toBuffer();
const after = await sharp("../evidence/crypto-rebalancing/after.png").resize({ width: 900 }).toBuffer();
const header = Buffer.from(`<svg width="1840" height="128" xmlns="http://www.w3.org/2000/svg">
  <rect width="1840" height="128" fill="#071422"/>
  <text x="28" y="38" font-family="Arial" font-size="27" font-weight="700" fill="#f7fafc">Crypto rebalancing · positive economics, failed robustness</text>
  <text x="28" y="72" font-family="Arial" font-size="17" fill="#8fb9dc">Before: result absent · After: exact result selected, 1/0 filter, zero console errors</text>
  <text x="28" y="101" font-family="Arial" font-size="16" fill="#f0b86e">CAGR 11.07% vs 2.69% · active +5.17% · 2/2 folds · block lower −10.11% · remove-best Sharpe −0.379</text>
  <text x="28" y="122" font-family="monospace" font-size="13" fill="#36d7c4">165 nodes / 172 edges · graph sha256:12f7d8e86ce427798bb0949dd32262f74404f8d2bfda9fbe6c72238251e9863b</text>
</svg>`);
await sharp({ create: { width: 1840, height: 828, channels: 3, background: "#071422" } })
  .composite([{ input: header, top: 0, left: 0 }, { input: before, top: 138, left: 15 }, { input: after, top: 138, left: 925 }])
  .png().toFile("../evidence/crypto-rebalancing/comparison.png");

