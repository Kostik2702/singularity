#!/usr/bin/env node
/**
 * bootstrap.mjs — deploy the visual-regression harness into a target project
 * (WEBSITE-FACTORY WSF-08).
 *
 * Usage: node bootstrap.mjs --project <root> [--port 8788] [--force]
 *
 * Copies template assets (playwright config, visual spec, determinism checker,
 * BASELINE-POLICY.md), patches package.json (scripts + devDependencies) and
 * prints a typed JSON envelope with the follow-up commands. Does NOT run npm —
 * installs are the caller's explicit step (network, browser downloads).
 */
import { cpSync, existsSync, mkdirSync, readFileSync, writeFileSync, renameSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import process from "node:process";

const SCHEMA = "vr-bootstrap/1";
const ASSETS = join(dirname(fileURLToPath(import.meta.url)), "..", "assets");

function fail(msg) {
  console.log(JSON.stringify({ schema_version: SCHEMA, status: "error", error: msg }));
  process.exit(2);
}

const args = { port: "8788", force: false };
const argv = process.argv.slice(2);
for (let i = 0; i < argv.length; i++) {
  if (argv[i] === "--force") { args.force = true; continue; }
  if (!argv[i].startsWith("--")) fail(`unexpected argument ${argv[i]}`);
  args[argv[i].slice(2)] = argv[i + 1]; i++;
}
if (!args.project) fail("--project is required");
const root = resolve(args.project);
if (!existsSync(join(root, "index.html"))) {
  fail(`${root} has no index.html — bootstrap targets an existing static page ` +
       `(the page must also implement the ?visual-test=1 determinism contract)`);
}

const PLACED = [
  ["playwright.config.js", "tests/playwright.config.js"],
  ["tests/visual/visual.spec.js", "tests/visual/visual.spec.js"],
  ["tools/check-visual-determinism.mjs", "tools/check-visual-determinism.mjs"],
  ["BASELINE-POLICY.md", "BASELINE-POLICY.md"],
];
const written = [];
for (const [src, dst] of PLACED) {
  const target = join(root, dst);
  if (existsSync(target) && !args.force) {
    fail(`${target} already exists — refusing to overwrite (use --force after reviewing)`);
  }
  mkdirSync(dirname(target), { recursive: true });
  const body = readFileSync(join(ASSETS, src), "utf-8").replaceAll("__PORT__", args.port);
  writeFileSync(target + ".tmp", body);
  renameSync(target + ".tmp", target);
  written.push(target);
}

// patch package.json (create if absent)
const pkgPath = join(root, "package.json");
const pkg = existsSync(pkgPath)
  ? JSON.parse(readFileSync(pkgPath, "utf-8"))
  : { name: "site", private: true };
pkg.scripts = {
  ...pkg.scripts,
  serve: `http-server -p ${args.port} -c-1 --cors`,
  test: "playwright test -c tests/playwright.config.js",
  "test:visual": "playwright test -c tests/playwright.config.js tests/visual",
  "test:update-baseline": "playwright test -c tests/playwright.config.js tests/visual --update-snapshots",
  "check:visual-determinism": "node tools/check-visual-determinism.mjs",
};
pkg.devDependencies = {
  ...pkg.devDependencies,
  // same major as the NIKA harness so an existing browser cache is reused
  "@playwright/test": pkg.devDependencies?.["@playwright/test"] ?? "^1.49.1",
  "http-server": pkg.devDependencies?.["http-server"] ?? "^14.1.1",
};
writeFileSync(pkgPath + ".tmp", JSON.stringify(pkg, null, 2) + "\n");
renameSync(pkgPath + ".tmp", pkgPath);
written.push(pkgPath);

console.log(JSON.stringify({
  schema_version: SCHEMA,
  status: "ok",
  inputs: { project: root, port: args.port },
  outputs: written,
  next_steps: [
    `cd "${root}" && npm install`,
    `set PLAYWRIGHT_BROWSERS_PATH=D:\\playwright-browsers (keep browsers off C:)`,
    `npx playwright install chromium firefox webkit`,
    `npm run serve  (or let the config's webServer start it)`,
    `npm run check:visual-determinism  -> must print STABLE for every viewport`,
    `npm run test:update-baseline  (INITIAL baseline; owner reviews screenshots)`,
    `NEGATIVE TEST (mandatory): make a visible change -> npm run test:visual must go RED -> revert -> green`,
  ],
}));
