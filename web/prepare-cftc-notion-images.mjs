import fs from "node:fs/promises";
import sharp from "sharp";

const jobs = [
  ["../evidence/research-evidence-graph/after.png", "../evidence/research-evidence-graph/notion-current.svg", 820],
  ["../evidence/cftc-flow/comparison.png", "../evidence/cftc-flow/notion-comparison.svg", 950],
];
for (const [source, target, width] of jobs) {
  const image = await sharp(source).resize({ width }).jpeg({ quality: 36, mozjpeg: true }).toBuffer();
  const metadata = await sharp(image).metadata();
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${metadata.width}" height="${metadata.height}" viewBox="0 0 ${metadata.width} ${metadata.height}"><image width="100%" height="100%" href="data:image/jpeg;base64,${image.toString("base64")}"/></svg>`;
  await fs.writeFile(target, svg);
}
