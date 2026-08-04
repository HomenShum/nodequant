import sharp from "sharp";

const before = await sharp("../evidence/fomc-drift/before.png").resize({ width: 900 }).toBuffer();
const after = await sharp("../evidence/fomc-drift/after.png").resize({ width: 900 }).toBuffer();
const header = Buffer.from(`<svg width="1840" height="128" xmlns="http://www.w3.org/2000/svg">
  <rect width="1840" height="128" fill="#071422"/>
  <text x="28" y="38" font-family="Arial" font-size="27" font-weight="700" fill="#f7fafc">FOMC post-publication drift · absent → measured contradiction</text>
  <text x="28" y="72" font-family="Arial" font-size="17" fill="#8fb9dc">Before: no result node · After: exact 1-node filter, failing verdict selected, zero console errors</text>
  <text x="28" y="101" font-family="Arial" font-size="16" fill="#f0b86e">5 bp/side: active mean −6.675 bp · Sharpe −0.172 · 0/2 positive folds · block lower −26.556 bp</text>
  <text x="28" y="122" font-family="monospace" font-size="13" fill="#36d7c4">161 nodes / 167 edges · graph sha256:8e0a99804dd46a7776425da22c64568fecb9feece457f0648f61d53759738b1b</text>
</svg>`);

await sharp({ create: { width: 1840, height: 828, channels: 3, background: "#071422" } })
  .composite([
    { input: header, top: 0, left: 0 },
    { input: before, top: 138, left: 15 },
    { input: after, top: 138, left: 925 },
  ])
  .png()
  .toFile("../evidence/fomc-drift/comparison.png");

