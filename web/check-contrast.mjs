/**
 * Measure the rendered contrast of the verdict badge in both colour schemes.
 *
 * The panel is a trust surface, so "is the PASS badge legible" is a
 * correctness question, not a taste one. This reads computed colours out of a
 * real render and computes WCAG contrast rather than eyeballing a screenshot.
 */

import { chromium } from "playwright";

const parse = (c) => c.match(/[\d.]+/g).slice(0, 3).map(Number);
const lum = (rgb) => {
  const [r, g, b] = rgb.map((v) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};
const ratio = (a, b) => {
  const [x, y] = [lum(a), lum(b)].sort((m, n) => n - m);
  return (x + 0.05) / (y + 0.05);
};

const browser = await chromium.launch();
const out = [];

for (const scheme of ["light", "dark"]) {
  const page = await browser.newPage({ colorScheme: scheme });
  await page.goto("http://localhost:3010", { waitUntil: "networkidle" });
  await page.locator("textarea").fill("first signal");
  await page.locator("textarea").press("Enter");
  const badge = page.locator('[data-testid="decision-packet-verdict"]');
  await badge.waitFor({ timeout: 30_000 });

  const measured = await badge.evaluate((el) => {
    // Resolve through a canvas: computed colours come back in modern spaces
    // like lab(), and parsing those as rgb triples silently produces nonsense
    // contrast numbers. The canvas normalises whatever CSS hands us to sRGB.
    const ctx = document.createElement("canvas").getContext("2d");
    const toRgb = (css) => {
      ctx.clearRect(0, 0, 1, 1);
      ctx.fillStyle = "#000";
      ctx.fillStyle = css;
      ctx.fillRect(0, 0, 1, 1);
      const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data;
      return `rgb(${r}, ${g}, ${b})`;
    };
    const walk = (node) => {
      let n = node;
      while (n && n !== document.documentElement) {
        const bg = getComputedStyle(n).backgroundColor;
        if (bg && bg !== "rgba(0, 0, 0, 0)" && !bg.startsWith("rgba(0, 0, 0, 0")) return bg;
        n = n.parentElement;
      }
      return getComputedStyle(document.body).backgroundColor;
    };
    return { fg: toRgb(getComputedStyle(el).color), bg: toRgb(walk(el)) };
  });

  out.push({
    scheme,
    fg: measured.fg,
    bg: measured.bg,
    contrast: +ratio(parse(measured.fg), parse(measured.bg)).toFixed(2),
    passesWCAG_AA_small: ratio(parse(measured.fg), parse(measured.bg)) >= 4.5,
  });
  await page.close();
}

console.log(JSON.stringify(out, null, 1));
await browser.close();
