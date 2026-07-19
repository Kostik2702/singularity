#!/usr/bin/env node
/**
 * motion-qa — motion/3D performance + accessibility gate (WSF-E06 v1).
 *
 * Evidence collector + budget gate for a live page: scripted scroll
 * timeline, long tasks, layout shifts, frame pacing, animation-frame leak
 * detection (rAF still firing after scroll settles on a supposedly idle
 * page is a typed signal), WebGL context census + forced context-loss
 * recovery probe, reduced-motion emulation diff, no-JS content check.
 *
 * Playwright resolves from the AUDITED project's node_modules (WSF-05/07
 * pattern, zero own deps). Envelope motion-qa/1. Evaluative exits:
 *   0 = all applied budgets PASS   1 = budget FAIL   2 = tool error
 * Metrics the environment cannot measure are typed "unavailable" and make
 * the overall verdict at best ADAPT-equivalent (NEVER silently PASS) —
 * unavailable budgets are listed in verdict.reason.
 *
 * Usage:
 *   node motion-qa.mjs --project <root> --url <http(s)|file url>
 *        --out <dir-outside-project> [--budgets budgets.json]
 *        [--scroll-steps 8] [--settle-ms 1200]
 */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, writeFileSync, renameSync } from "node:fs";
import { resolve, join, sep } from "node:path";
import process from "node:process";

const SCHEMA = "motion-qa/1";

function fail(msg) {
  console.log(JSON.stringify({ schema_version: SCHEMA, status: "error", error: msg }));
  process.exit(2);
}

function parseArgs(argv) {
  const args = { "scroll-steps": "8", "settle-ms": "1200" };
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

const DEFAULT_BUDGETS = {
  // Frozen numbers belong to the OWNER after a baseline spike (decision §9);
  // these defaults are deliberately generous smoke-level guards.
  max_long_tasks: 5,            // tasks >50ms during scripted scroll
  max_longest_task_ms: 200,
  max_cls: 0.1,
  max_raf_after_settle: 2,      // rAF ticks in 500ms AFTER scroll settled
  max_webgl_contexts: 2,
  require_context_recovery: true,
  require_reduced_motion_variant: true,
  require_nojs_content: true,
};

const args = parseArgs(process.argv.slice(2));
const projectRoot = resolve(args.project);
const outDir = resolve(args.out);
if ((outDir + sep).startsWith(projectRoot + sep) || outDir === projectRoot) {
  fail(`--out ${outDir} is inside the audited project ${projectRoot} — forbidden`);
}
mkdirSync(outDir, { recursive: true });

let budgets = DEFAULT_BUDGETS;
if (args.budgets) {
  try {
    budgets = { ...DEFAULT_BUDGETS,
                ...JSON.parse(readFileSync(args.budgets, "utf8")) };
  } catch (e) {
    fail(`cannot read --budgets: ${e.message}`);
  }
}

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

function atomicWriteJson(obj, dest) {
  const tmp = dest + `.tmp-${process.pid}`;
  writeFileSync(tmp, JSON.stringify(obj, null, 1), "utf8");
  renameSync(tmp, dest);
}

const INSTRUMENT = `(() => {
  window.__mq = { longTasks: [], cls: 0, rafCount: 0, rafProbe: null };
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries())
        window.__mq.longTasks.push({ duration: Math.round(e.duration) });
    }).observe({ type: "longtask", buffered: true });
  } catch (e) { window.__mq.longTasksUnavailable = String(e); }
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries())
        if (!e.hadRecentInput) window.__mq.cls += e.value;
    }).observe({ type: "layout-shift", buffered: true });
  } catch (e) { window.__mq.clsUnavailable = String(e); }
})();`;

const browser = await chromium.launch({ headless: true });
let out = null;
let exitCode = 2;
const networkAttempts = [];
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  if (args["block-network"] === "1") {
    // quarantine mode (intake runtime): nothing leaves the machine — every
    // non-local request is aborted AND recorded as evidence. A component
    // that phones home reveals itself here even if the static scan missed it.
    await page.route("**", (route) => {
      const url = route.request().url();
      if (url.startsWith("file://") || url.includes("://localhost")
          || url.includes("://127.0.0.1") || url.startsWith("data:")) {
        return route.continue();
      }
      networkAttempts.push(url.slice(0, 200));
      return route.abort();
    });
  }
  await page.addInitScript(INSTRUMENT);
  // "load", not "networkidle": pages with streaming media (NIKA video) never
  // go network-idle and would time out (caught on the first live GT run)
  const response = await page.goto(args.url, { waitUntil: "load", timeout: 45000 });
  await page.waitForTimeout(800);
  if (args.url.startsWith("http") && (!response || !response.ok())) {
    throw new Error(`page load failed: ${args.url}`);
  }

  // scripted scroll timeline
  const steps = parseInt(args["scroll-steps"], 10);
  const docHeight = await page.evaluate(() =>
    Math.max(document.body.scrollHeight, document.documentElement.scrollHeight));
  for (let i = 1; i <= steps; i++) {
    await page.evaluate((y) => window.scrollTo({ top: y }), (docHeight / steps) * i);
    await page.mouse.move(200 + i * 80, 300 + (i % 3) * 100);
    await page.waitForTimeout(150);
  }
  await page.waitForTimeout(parseInt(args["settle-ms"], 10));

  // rAF leak probe AFTER settle: how many frames does the page request in 500ms
  const rafAfterSettle = await page.evaluate(async () => {
    let count = 0;
    const origRaf = window.requestAnimationFrame.bind(window);
    const orig = window.requestAnimationFrame;
    window.requestAnimationFrame = (cb) => { count += 1; return orig.call(window, cb); };
    await new Promise((res) => setTimeout(res, 500));
    window.requestAnimationFrame = orig;
    void origRaf;
    return count;
  });

  const metrics = await page.evaluate(() => ({
    longTasks: window.__mq.longTasks,
    longTasksUnavailable: window.__mq.longTasksUnavailable || null,
    cls: Math.round(window.__mq.cls * 1000) / 1000,
    clsUnavailable: window.__mq.clsUnavailable || null,
  }));

  // WebGL census + forced loss/recovery probe
  const webgl = await page.evaluate(async () => {
    const canvases = [...document.querySelectorAll("canvas")];
    let contexts = 0;
    let recovery = null;
    for (const c of canvases) {
      const gl = c.getContext("webgl2") || c.getContext("webgl");
      if (!gl) continue;
      contexts += 1;
      const ext = gl.getExtension("WEBGL_lose_context");
      if (ext && recovery === null) {
        recovery = await new Promise((res) => {
          let restored = false;
          c.addEventListener("webglcontextrestored", () => { restored = true; }, { once: true });
          c.addEventListener("webglcontextlost", (e) => e.preventDefault(), { once: true });
          ext.loseContext();
          setTimeout(() => { try { ext.restoreContext(); } catch { /* noop */ } }, 100);
          setTimeout(() => res(restored), 800);
        });
      }
    }
    return { canvases: canvases.length, contexts, recovery };
  });

  // reduced-motion emulation: does the page change at all?
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.waitForTimeout(300);
  const reduced = await page.evaluate(() => {
    const anims = document.getAnimations ? document.getAnimations() : [];
    const running = anims.filter((a) => a.playState === "running").length;
    const acknowledged = [...document.styleSheets].some((s) => {
      try {
        return [...s.cssRules].some((r) =>
          r.media && [...r.media].join(",").includes("prefers-reduced-motion"));
      } catch { return false; }
    });
    const hasText = (document.body.innerText || "").trim().length > 20;
    return { runningAnimations: running, cssAcknowledges: acknowledged, hasText };
  });
  await page.emulateMedia({ reducedMotion: null });

  // no-JS: fresh context with JS disabled — is meaningful content present?
  const nojsCtx = await browser.newContext({ javaScriptEnabled: false });
  if (args["block-network"] === "1") {
    await nojsCtx.route("**", (route) => {
      const url = route.request().url();
      if (url.startsWith("file://") || url.includes("://localhost")
          || url.includes("://127.0.0.1") || url.startsWith("data:")) {
        return route.continue();
      }
      networkAttempts.push(url.slice(0, 200));
      return route.abort();
    });
  }
  const nojsPage = await nojsCtx.newPage();
  let nojs = { unavailable: null, textLength: 0 };
  try {
    await nojsPage.goto(args.url, { timeout: 30000 });
    nojs.textLength = await nojsPage.evaluate(
      () => (document.body.innerText || "").trim().length);
  } catch (e) {
    nojs.unavailable = String(e).slice(0, 200);
  }
  await nojsCtx.close();

  // ---- budget verdict ----
  const checks = {};
  const failures = [];
  const unavailable = [];

  function gate(name, ok, detail, isUnavailable = false) {
    checks[name] = { status: isUnavailable ? "unavailable" : ok ? "pass" : "fail", detail };
    if (isUnavailable) unavailable.push(name);
    else if (!ok) failures.push(`${name}: ${detail}`);
  }

  if (metrics.longTasksUnavailable) {
    gate("long-tasks", false, metrics.longTasksUnavailable, true);
  } else {
    const longest = Math.max(0, ...metrics.longTasks.map((t) => t.duration));
    gate("long-tasks",
         metrics.longTasks.length <= budgets.max_long_tasks
         && longest <= budgets.max_longest_task_ms,
         `${metrics.longTasks.length} tasks (budget ${budgets.max_long_tasks}), longest ${longest}ms (budget ${budgets.max_longest_task_ms}ms)`);
  }
  if (metrics.clsUnavailable) gate("cls", false, metrics.clsUnavailable, true);
  else gate("cls", metrics.cls <= budgets.max_cls,
            `CLS ${metrics.cls} (budget ${budgets.max_cls})`);
  gate("raf-after-settle", rafAfterSettle <= budgets.max_raf_after_settle,
       `${rafAfterSettle} rAF ticks in 500ms after settle (budget ${budgets.max_raf_after_settle}) — a busy loop on an idle page is a leak`);
  gate("webgl-contexts", webgl.contexts <= budgets.max_webgl_contexts,
       `${webgl.contexts} live contexts on ${webgl.canvases} canvases (budget ${budgets.max_webgl_contexts})`);
  if (budgets.require_context_recovery) {
    if (webgl.contexts === 0) gate("webgl-recovery", true, "no WebGL contexts — n/a");
    else if (webgl.recovery === null) gate("webgl-recovery", false,
      "WEBGL_lose_context unavailable", true);
    else gate("webgl-recovery", webgl.recovery,
      webgl.recovery ? "context restored after forced loss"
                     : "context NOT restored after forced loss");
  }
  if (budgets.require_reduced_motion_variant) {
    gate("reduced-motion",
         reduced.cssAcknowledges || reduced.runningAnimations === 0,
         `css acknowledges: ${reduced.cssAcknowledges}, running animations under reduce: ${reduced.runningAnimations}`);
  }
  gate("reduced-motion-content", reduced.hasText,
       "content text present under reduced motion");
  if (budgets.require_nojs_content) {
    if (nojs.unavailable) gate("nojs", false, nojs.unavailable, true);
    else gate("nojs", nojs.textLength > 20,
              `no-JS text length ${nojs.textLength} — meaning and CTA must survive`);
  }

  if (args["block-network"] === "1") {
    gate("network-quarantine", networkAttempts.length === 0,
         networkAttempts.length
           ? `blocked outbound attempts: ${[...new Set(networkAttempts)].slice(0, 5).join(", ")}`
           : "no outbound network attempts");
  }

  const result = failures.length ? "FAIL" : unavailable.length ? "PASS-WITH-UNAVAILABLE" : "PASS";
  const report = {
    schema_version: SCHEMA, url: args.url, budgets,
    playwright_version: playwrightVersion,
    metrics: { ...metrics, rafAfterSettle, webgl, reduced, nojs, docHeight,
               networkAttempts },
    checks,
  };
  const reportPath = join(outDir, "motion-qa-report.json");
  atomicWriteJson(report, reportPath);
  out = {
    schema_version: SCHEMA, command: "audit", status: "ok",
    tool: { playwright: playwrightVersion },
    inputs: { url: args.url, budgets: args.budgets || "defaults" },
    data: { checks, unavailable },
    verdict: {
      result,
      reason: failures.join("; ")
        || (unavailable.length ? `unavailable metrics: ${unavailable.join(", ")} — not a full PASS` : "all budgets green"),
    },
    outputs: [reportPath],
  };
  exitCode = failures.length ? 1 : 0;
} catch (e) {
  out = { schema_version: SCHEMA, status: "error",
          error: `audit crashed: ${e && e.message ? e.message : e}` };
  exitCode = 2;
} finally {
  await browser.close();
}
console.log(JSON.stringify(out));
process.exit(exitCode);
