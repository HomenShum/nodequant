import sharp from "sharp";

const before = await sharp("../evidence/cftc-flow/before.png").resize({ width: 900 }).toBuffer();
const after = await sharp("../evidence/cftc-flow/after.png").resize({ width: 900 }).toBuffer();
const header = Buffer.from(`<svg width="1840" height="120" xmlns="http://www.w3.org/2000/svg">
  <rect width="1840" height="120" fill="#071422"/>
  <text x="28" y="42" font-family="Arial" font-size="28" font-weight="700" fill="#f7fafc">CFTC evidence graph · stale inspector → causally aligned detail</text>
  <text x="28" y="78" font-family="Arial" font-size="17" fill="#8fb9dc">Before: filtered node and inspector contradicted · After: 1/0 exact filter, invalidation selected, zero console errors</text>
  <text x="28" y="106" font-family="monospace" font-size="14" fill="#36d7c4">154 nodes / 159 edges · graph sha256:9d290f0d9e1942022385aa3eb310aa730a91857b7ab53bca8334597b65f082b2</text>
</svg>`);
await sharp({ create: { width: 1840, height: 820, channels: 3, background: "#071422" } })
  .composite([
    { input: header, top: 0, left: 0 },
    { input: before, top: 130, left: 15 },
    { input: after, top: 130, left: 925 },
  ])
  .png()
  .toFile("../evidence/cftc-flow/comparison.png");
