import fs from "node:fs/promises";
import sharp from "sharp";

for (const [source, target, width, quality] of [
  [
    "../evidence/market-activity/after-full-market-map-final.png",
    "../evidence/market-activity/notion-market-map-current.svg",
    900,
    24,
  ],
  [
    "../evidence/market-activity/before-after-comparison.png",
    "../evidence/market-activity/notion-market-map-comparison.svg",
    1000,
    24,
  ],
  [
    "../evidence/market-activity/before-after-nextjs-chat.png",
    "../evidence/market-activity/notion-nextjs-chat-market-map.svg",
    1000,
    28,
  ],
]) {
  const image = await sharp(source).resize({ width }).jpeg({ quality, mozjpeg: true }).toBuffer();
  const metadata = await sharp(image).metadata();
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${metadata.width}" height="${metadata.height}" viewBox="0 0 ${metadata.width} ${metadata.height}"><image width="100%" height="100%" href="data:image/jpeg;base64,${image.toString("base64")}"/></svg>`;
  await fs.writeFile(target, svg);
}
