#!/usr/bin/env node
/**
 * fidelity-audit — deterministic evidence collector for design-fidelity audits
 * (WEBSITE-FACTORY WSF-07). Captures per-viewport full-page screenshots and DOM
 * metrics of a live page in ?visual-test=1 mode, plus design-mockup dimensions,
 * and emits one typed JSON envelope. The JUDGMENT (A/B categorization, verdict)
 * is deliberately NOT here — it belongs to the skill/human reading the output.
 *
 * Zero own dependencies: Playwright is resolved from the AUDITED project's own
 * node_modules (the project that has a test harness already has Playwright).
 *
 * Usage:
 *   node fidelity-audit.mjs --project <root> --url http://localhost:8743
 *        --out <dir-outside-project> [--design <mockup.png>]
 *        [--viewports 1440x900,768x1024,390x844] [--page /index.html]
 */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, writeFileSync, renameSync, existsSync } from "node:fs";
import { resolve, join, sep } from "node:path";
import process from "node:process";

const SCHEMA = "fidelity-audit/1";

function fail(msg) {
  // tool error — mirror imgqa CONTRACT: errors are typed JSON on stdout, exit 2
  console.log(JSON.stringify({ schema_version: SCHEMA, status: "error", error: msg }));
  process.exit(2);
}

function parseArgs(argv) {
  const args = { viewports: "1440x900,768x1024,390x844", page: "/index.html" };
  for (let i = 0; i < argv.length; i++) {
    const key = argv[i];
    if (!key.startsWith("--")) fail(`unexpected argument: ${key}`);
    args[key.slice(2)] = argv[i + 1];
    i++;
  }
  for (const req of ["project", "url", "out"]) {
    if (!args[req]) fail(`--${req} is required`);
  }
  return args;
}

function pngSize(path) {
  const buf = readFileSync(path);
  if (buf.length < 24 || buf.readUInt32BE(12) !== 0x49484452) fail(`${path} is not a PNG`);
  return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
}

const args = parseArgs(process.argv.slice(2));
const projectRoot = resolve(args.project);
const outDir = resolve(args.out);
// output policy (imgqa CONTRACT §3): never write into the audited tree
if ((outDir + sep).startsWith(projectRoot + sep) || outDir === projectRoot) {
  fail(`--out ${outDir} is inside the audited project ${projectRoot} — forbidden`);
}
mkdirSync(outDir, { recursive: true });

let chromium, playwrightVersion;
try {
  const projectRequire = createRequire(join(projectRoot, "package.json"));
  ({ chromium } = projectRequire("playwright"));
  playwrightVersion = projectRequire("playwright/package.json").version;
} catch (e) {
  try {
    const projectRequire = createRequire(join(projectRoot, "package.json"));
    ({ chromium } = projectRequire("@playwright/test"));
    playwrightVersion = projectRequire("@playwright/test/package.json").version;
  } catch (e2) {
    fail(`cannot resolve playwright from ${projectRoot}/node_modules — the audited ` +
         `project must have its own Playwright install (harness). ${e2.message}`);
  }
}

const viewports = args.viewports.split(",").map((s) => {
  const m = s.trim().match(/^(\d+)x(\d+)$/);
  if (!m) fail(`bad viewport ${s}: expected WxH`);
  return { width: +m[1], height: +m[2], name: s.trim() };
});

// force deterministic mode regardless of what the caller passed
const pagePath = args.page.includes("visual-test")
  ? args.page
  : args.page + (args.page.includes("?") ? "&" : "?") + "visual-test=1";
const url = args.url.replace(/\/$/, "") + pagePath;

const design = args.design
  ? { path: resolve(args.design), ...pngSize(resolve(args.design)) }
  : null;

const browser = await chromium.launch();
const results = [];
try {
  for (const vp of viewports) {
    const context = await browser.newContext({ viewport: { width: vp.width, height: vp.height } });
    const page = await context.newPage();
    const consoleErrors = [];
    page.on("pageerror", (err) => consoleErrors.push(String(err.message)));
    await page.goto(url, { waitUntil: "networkidle" });

    // scroll-through with REAL scroll events (NIKA lesson: CDP fullPage does
    // not scroll — lazy content/reveals below the fold never trigger), then
    // return to top for a stable capture
    await page.evaluate(async () => {
      const step = window.innerHeight;
      for (let y = 0; y <= document.documentElement.scrollHeight; y += step) {
        window.scrollTo(0, y);
        await new Promise((r) => setTimeout(r, 60));
      }
      window.scrollTo(0, 0);
      await new Promise((r) => setTimeout(r, 200));
    });

    const metrics = await page.evaluate(() => {
      const doc = document.documentElement;
      const grab = (el) => {
        const rect = el.getBoundingClientRect();
        const cs = getComputedStyle(el);
        return {
          sel: el.id ? `#${el.id}` : el.tagName.toLowerCase(),
          top: Math.round(rect.top + window.scrollY),
          height: Math.round(rect.height),
          paddingInline: [cs.paddingLeft, cs.paddingRight],
          paddingBlock: [cs.paddingTop, cs.paddingBottom],
        };
      };
      return {
        pageHeight: doc.scrollHeight,
        clientWidth: doc.clientWidth,
        scrollWidth: doc.scrollWidth,
        horizontalOverflow: doc.scrollWidth > doc.clientWidth,
        visualTestMode: doc.dataset.visualTest === "1",
        sections: [...document.querySelectorAll("header, main > section, footer")].map(grab),
      };
    });

    const shotName = `fidelity.${vp.name}.png`;
    // tmp must keep the .png extension — Playwright infers the image format
    // from the path suffix and rejects unknown extensions
    const tmp = join(outDir, `.${shotName}.${process.pid}.tmp.png`);
    await page.screenshot({ path: tmp, fullPage: true });
    renameSync(tmp, join(outDir, shotName));

    results.push({
      viewport: vp.name,
      screenshot: join(outDir, shotName),
      consoleErrors,
      ...metrics,
      proportion: +(metrics.pageHeight / vp.width).toFixed(3),
    });
    await context.close();
  }
} catch (e) {
  await browser.close();
  fail(`audit run failed: ${e.message}`);
}
await browser.close();

const envelope = {
  schema_version: SCHEMA,
  status: "ok",
  tool: { node: process.version, playwright: playwrightVersion },
  inputs: { project: projectRoot, url, out: outDir, design: design?.path ?? null },
  data: {
    design: design
      ? { width: design.width, height: design.height,
          proportion: +(design.height / design.width).toFixed(3) }
      : null,
    viewports: results,
  },
};
const metricsPath = join(outDir, "fidelity-metrics.json");
writeFileSync(metricsPath + ".tmp", JSON.stringify(envelope, null, 2));
renameSync(metricsPath + ".tmp", metricsPath);
console.log(JSON.stringify({ ...envelope, outputs: [metricsPath, ...results.map(r => r.screenshot)] }));
