import fs from "node:fs/promises";
import sharp from "sharp";
for (const [source, target, width] of [
  ["../evidence/research-evidence-graph/after.png", "../evidence/research-evidence-graph/notion-current.svg", 820],
  ["../evidence/crypto-rebalancing/comparison.png", "../evidence/crypto-rebalancing/notion-comparison.svg", 950],
]) {
  const image = await sharp(source).resize({ width }).jpeg({ quality: 34, mozjpeg: true }).toBuffer();
  const meta = await sharp(image).metadata();
  await fs.writeFile(target, `<svg xmlns="http://www.w3.org/2000/svg" width="${meta.width}" height="${meta.height}" viewBox="0 0 ${meta.width} ${meta.height}"><image width="100%" height="100%" href="data:image/jpeg;base64,${image.toString("base64")}"/></svg>`);
}
