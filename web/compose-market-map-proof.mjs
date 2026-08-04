import sharp from "sharp";

const width = 820;
const header = 54;
const before = await sharp("../evidence/market-activity/before-research-graph.png")
  .resize({ width })
  .png()
  .toBuffer();
const after = await sharp("../evidence/market-activity/after-full-market-map-final.png")
  .resize({ width })
  .png()
  .toBuffer();
const beforeMeta = await sharp(before).metadata();
const afterMeta = await sharp(after).metadata();
const bodyHeight = Math.max(beforeMeta.height ?? 0, afterMeta.height ?? 0);
const labels =
  Buffer.from(`<svg width="${width * 2}" height="${header}" xmlns="http://www.w3.org/2000/svg">
  <rect width="100%" height="100%" fill="#080b12"/>
  <text x="24" y="34" fill="#9ea9b8" font-family="monospace" font-size="18" font-weight="700">BEFORE / 166-NODE RESEARCH CATALOG</text>
  <text x="${width + 24}" y="34" fill="#77b8f7" font-family="monospace" font-size="18" font-weight="700">AFTER / 504-EQUITY MARKET SURFACE</text>
</svg>`);
await sharp({
  create: { width: width * 2, height: header + bodyHeight, channels: 3, background: "#080b12" },
})
  .composite([
    { input: labels, left: 0, top: 0 },
    { input: before, left: 0, top: header },
    { input: after, left: width, top: header },
  ])
  .png()
  .toFile("../evidence/market-activity/before-after-comparison.png");
