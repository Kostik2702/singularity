#!/usr/bin/env node
/**
 * a11y-audit — deterministic axe-core runner for live pages (WSF-05).
 *
 * Evidence-only: numbers and violation nodes; the JUDGMENT (what to fix, what
 * is a deliberate owner decision) belongs to the skill/human. Exact WCAG
 * contrast math is NOT here — that is `imgqa contrast` (GT-C1 4.93 case).
 *
 * Zero own dependencies (fidelity-audit pattern): playwright AND axe-core are
 * resolved from the AUDITED project's node_modules; axe.min.js is injected
 * from disk — no network, no CDN.
 *
 * Envelope a11y-audit/1. Evaluative exits (imgqa CONTRACT mirror):
 *   0 = no violations at/above --fail-impact   1 = violations found
 *   2 = tool error (never a domain verdict)
 *
 * Usage:
 *   node a11y-audit.mjs --project <root> --url http://localhost:8743
 *        --out <dir-outside-project> [--page /index.html]
 *        [--tags wcag2a,wcag2aa,wcag21aa] [--fail-impact serious]
 *        [--visual-test 1]
 */
import { createRequire } from "node:module";
import { mkdirSync, writeFileSync, renameSync } from "node:fs";
import { resolve, join, sep } from "node:path";
import process from "node:process";

const SCHEMA = "a11y-audit/1";
const IMPACT_ORDER = ["minor", "moderate", "serious", "critical"];

function fail(msg) {
  console.log(JSON.stringify({ schema_version: SCHEMA, status: "error", error: msg }));
  process.exit(2);
}

function parseArgs(argv) {
  const args = {
    page: "/index.html",
    tags: "wcag2a,wcag2aa,wcag21aa",
    "fail-impact": "serious",
    "visual-test": "1",
  };
  for (let i = 0; i < argv.length; i++) {
    const key = argv[i];
    if (!key.startsWith("--")) fail(`unexpected argument: ${key}`);
    args[key.slice(2)] = argv[i + 1];
    i++;
  }
  for (const req of ["project", "url", "out"]) {
    if (!args[req]) fail(`--${req} is required`);
  }
  if (!IMPACT_ORDER.includes(args["fail-impact"])) {
    fail(`--fail-impact must be one of ${IMPACT_ORDER.join(",")}`);
  }
  return args;
}

const args = parseArgs(process.argv.slice(2));
const projectRoot = resolve(args.project);
const outDir = resolve(args.out);
if ((outDir + sep).startsWith(projectRoot + sep) || outDir === projectRoot) {
  fail(`--out ${outDir} is inside the audited project ${projectRoot} — forbidden`);
}
mkdirSync(outDir, { recursive: true });

const projectRequire = createRequire(join(projectRoot, "package.json"));
let chromium, playwrightVersion;
try {
  ({ chromium } = projectRequire("playwright"));
  playwrightVersion = projectRequire("playwright/package.json").version;
} catch {
  try {
    ({ chromium } = projectRequire("@playwright/test"));
    playwrightVersion = projectRequire("@playwright/test/package.json").version;
  } catch {
    fail(`cannot resolve playwright from ${projectRoot}/node_modules`);
  }
}
let axePath, axeVersion;
try {
  axePath = projectRequire.resolve("axe-core/axe.min.js");
  axeVersion = projectRequire("axe-core/package.json").version;
} catch {
  fail(`cannot resolve axe-core from ${projectRoot}/node_modules — ` +
       `npm i -D axe-core in the audited project`);
}

function atomicWriteJson(obj, dest) {
  const tmp = dest + `.tmp-${process.pid}`;
  writeFileSync(tmp, JSON.stringify(obj, null, 1), "utf8");
  renameSync(tmp, dest);
}

const sep2 = args.page.includes("?") ? "&" : "?";
const pageUrl = args.url.replace(/\/$/, "") + args.page +
  (args["visual-test"] === "0" ? "" : `${sep2}visual-test=1`);

const browser = await chromium.launch({ headless: true });
// process.exit inside try would skip finally and leave a zombie browser —
// collect the envelope + exit code, close the browser, THEN print and exit.
let envelopeOut = null;
let exitCode = 2;
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const response = await page.goto(pageUrl, { waitUntil: "networkidle", timeout: 45000 });
  if (!response || !response.ok()) {
    throw new Error(`page load failed: ${pageUrl} -> ${response ? response.status() : "no response"}`);
  }
  await page.addScriptTag({ path: axePath });
  const tags = args.tags.split(",").map((t) => t.trim()).filter(Boolean);
  const raw = await page.evaluate(async (runTags) => {
    // eslint-disable-next-line no-undef
    return await axe.run(document, { runOnly: { type: "tag", values: runTags } });
  }, tags);

  // deterministic subset: axe's own result carries timestamps/env — strip them
  const violations = raw.violations.map((v) => ({
    id: v.id,
    impact: v.impact,
    help: v.help,
    helpUrl: v.helpUrl,
    nodes: v.nodes.length,
    sample_targets: v.nodes.slice(0, 5).map((n) => n.target.join(" ")),
  }));
  const byImpact = {};
  for (const impact of IMPACT_ORDER) byImpact[impact] = 0;
  for (const v of violations) if (v.impact in byImpact) byImpact[v.impact] += 1;
  const threshold = IMPACT_ORDER.indexOf(args["fail-impact"]);
  const failing = violations.filter(
    (v) => IMPACT_ORDER.indexOf(v.impact) >= threshold);

  const report = {
    schema_version: SCHEMA,
    page: pageUrl,
    tags,
    axe_version: axeVersion,
    playwright_version: playwrightVersion,
    counts: { violations: violations.length, passes: raw.passes.length,
              incomplete: raw.incomplete.length, by_impact: byImpact },
    violations,
    incomplete: raw.incomplete.map((v) => ({ id: v.id, nodes: v.nodes.length })),
  };
  const reportPath = join(outDir, "a11y-report.json");
  atomicWriteJson(report, reportPath);

  const verdict = failing.length === 0
    ? { result: "PASS", reason: `0 violations at/above ${args["fail-impact"]}` }
    : { result: "FAIL",
        reason: `${failing.length} violations at/above ${args["fail-impact"]}: ` +
                failing.map((v) => v.id).join(", ") };
  envelopeOut = {
    schema_version: SCHEMA, command: "audit", status: "ok",
    tool: { axe: axeVersion, playwright: playwrightVersion },
    inputs: { url: pageUrl, tags, fail_impact: args["fail-impact"] },
    data: report.counts, verdict, outputs: [reportPath],
  };
  exitCode = failing.length === 0 ? 0 : 1;
} catch (e) {
  envelopeOut = { schema_version: SCHEMA, status: "error",
                  error: `audit crashed: ${e && e.message ? e.message : e}` };
  exitCode = 2;
} finally {
  await browser.close();
}
console.log(JSON.stringify(envelopeOut));
process.exit(exitCode);
