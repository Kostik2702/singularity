// Deployed by visual-regression-bootstrap. Proves the ?visual-test=1 contract:
// two consecutive captures must be BYTE-IDENTICAL per viewport. If UNSTABLE,
// the page still has a nondeterminism source (running canvas, animation,
// lazy-load, video frame) — fix the page, do not loosen the tolerance.
import { chromium } from "@playwright/test";
import { createHash } from "node:crypto";

const VIEWPORTS = [
  { name: "mobile-390", width: 390, height: 844 },
  { name: "tablet-768", width: 768, height: 1024 },
  { name: "desktop-1440", width: 1440, height: 900 },
];
// --disable-gpu: byte-identity needs a deterministic rasterizer. With GPU on,
// the FIRST page of a fresh browser can raster before the GPU process is
// ready (software path), the second on GPU -> uniform ±1 LSB over the whole
// canvas and a false UNSTABLE (seen on matrix-3d-web mobile-390). Software
// raster still catches every real nondeterminism source (animation, time,
// lazy-load) — which is this tool's actual job.
const b = await chromium.launch({ args: ["--disable-gpu"] });
let allStable = true;
for (const vp of VIEWPORTS) {
  const hashes = [];
  for (let run = 0; run < 2; run++) {
    const page = await b.newPage({ viewport: { width: vp.width, height: vp.height } });
    await page.goto("http://127.0.0.1:__PORT__/index.html?visual-test=1");
    await page.waitForLoadState("networkidle");
    await page.evaluate(() => document.fonts.ready);
    const buf = await page.screenshot({ fullPage: true });
    hashes.push(createHash("sha256").update(buf).digest("hex").slice(0, 16));
    await page.close();
  }
  const stable = hashes[0] === hashes[1];
  if (!stable) allStable = false;
  console.log(`${vp.name}: run1=${hashes[0]} run2=${hashes[1]} -> ${stable ? "STABLE" : "UNSTABLE"}`);
}
await b.close();
process.exit(allStable ? 0 : 1);
