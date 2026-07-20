"""Runtime phases for component-intake v2 (CONTRACT.md §2 amendment):
bundle proof + delta (esbuild --bundle with an isolated deps root), mount
fixture, and a network-QUARANTINED live run through the motion-qa gate.

Execution happens only inside the disposable workspace and a headless
browser whose every non-local request is aborted and recorded — a component
that phones home at runtime reveals itself here even if the static scan
missed it (networkAttempts non-empty => REJECT)."""
from __future__ import annotations

import gzip
import json
import os
import subprocess
from pathlib import Path

from .inspect import _esbuild_path, _scrubbed_env

MOTION_QA = (Path(__file__).resolve().parents[1]
             / "skills" / "motion-qa" / "scripts" / "motion-qa.mjs")

FIXTURE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>intake fixture</title>
<style>@media (prefers-reduced-motion: reduce){{ * {{ animation:none !important;
transition:none !important; }} }}</style></head><body>
<main><h1>intake fixture</h1><p>Component under quarantine mounts below.
Static fallback text keeps the no-JS gate meaningful.</p>
<div id="mount"></div></main>
<script src="{bundle}"></script>
</body></html>
"""

ENTRY_JS = """import React from "react";
import {{ createRoot }} from "react-dom/client";
import Component from "./{entry}";
const el = document.getElementById("mount");
try {{
  createRoot(el).render(React.createElement(Component, {{}}));
}} catch (e) {{
  el.textContent = "mount failed: " + e;
}}
"""


def _deps_root() -> Path | None:
    root = os.environ.get("INTAKE_DEPS_ROOT")
    if root and (Path(root) / "react").is_dir():
        return Path(root)
    return None


def _pick_entry(files: list[dict]) -> str | None:
    code = [f["path"] for f in files
            if f["path"].endswith((".jsx", ".tsx", ".js", ".ts"))]
    return code[0] if len(code) >= 1 else None


def run_runtime(files: list[dict], workspace: Path,
                playwright_project: str | None) -> dict:
    """-> checks-style dict {status, ...}; status unavailable when the
    environment lacks esbuild/deps/playwright — never a silent pass."""
    esbuild = _esbuild_path()
    deps = _deps_root()
    project = playwright_project or os.environ.get("INTAKE_PLAYWRIGHT_PROJECT")
    missing = [what for what, have in (
        ("esbuild (INTAKE_ESBUILD)", esbuild),
        ("deps root with react (INTAKE_DEPS_ROOT)", deps),
        ("playwright project (INTAKE_PLAYWRIGHT_PROJECT)", project),
    ) if not have]
    if missing:
        return {"status": "unavailable", "why": f"missing: {', '.join(missing)}"}
    entry = _pick_entry(files)
    if not entry:
        return {"status": "unavailable", "why": "no JS/TS entry file"}

    build_dir = workspace / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    entry_path = workspace / "src" / "__intake_entry__.jsx"
    entry_path.write_text(ENTRY_JS.format(entry=entry), encoding="utf-8")
    bundle_path = build_dir / "bundle.js"
    meta_path = build_dir / "meta.json"
    cmd = [str(esbuild), str(entry_path), "--bundle", "--format=iife",
           "--loader:.js=jsx", "--loader:.jsx=jsx", "--loader:.ts=tsx",
           "--loader:.tsx=tsx", "--minify",
           f"--outfile={bundle_path}", f"--metafile={meta_path}"]
    env = _scrubbed_env()
    env["NODE_PATH"] = str(deps)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", timeout=120, env=env)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"status": "unavailable", "why": f"esbuild bundle failed to run: {exc}"}
    if proc.returncode != 0:
        return {"status": "fail",
                "failures": [f"bundle: {proc.stderr.strip()[:400]}"]}

    raw = bundle_path.read_bytes()
    gz = len(gzip.compress(raw, 6))
    own_bytes = None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        own_bytes = sum(inp.get("bytes", 0)
                        for path, inp in meta.get("inputs", {}).items()
                        if "node_modules" not in path.replace("\\", "/"))
    except (OSError, ValueError):
        pass

    fixture = workspace / "build" / "fixture.html"
    fixture.write_text(FIXTURE_HTML.format(bundle="bundle.js"),
                       encoding="utf-8")
    url = fixture.resolve().as_uri()
    qa_out = workspace / "qa"
    try:
        qa = subprocess.run(
            ["node", str(MOTION_QA), "--project", str(project),
             "--url", url, "--out", str(qa_out),
             "--scroll-steps", "2", "--settle-ms", "600",
             "--block-network", "1"],
            capture_output=True, text=True, encoding="utf-8", timeout=180)
        qa_env = json.loads(qa.stdout)
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        return {"status": "unavailable", "why": f"motion-qa run failed: {exc}"}
    if qa_env.get("status") == "error":
        return {"status": "unavailable",
                "why": f"motion-qa crashed: {qa_env.get('error', '')[:200]}"}

    checks = qa_env.get("data", {}).get("checks", {})
    net = checks.get("network-quarantine", {})
    mount_failed = False
    try:
        report = json.loads((qa_out / "motion-qa-report.json")
                            .read_text(encoding="utf-8"))
        mount_failed = "mount failed" in json.dumps(report)[:20000]
    except (OSError, ValueError):
        pass

    failures = []
    if net.get("status") == "fail":
        failures.append(f"phones home at runtime: {net.get('detail')}")
    if mount_failed:
        failures.append("component failed to mount in the fixture")
    return {
        "status": "fail" if failures else "ok",
        "failures": failures,
        "bundle": {"raw_bytes": len(raw), "gzip_bytes": gz,
                   "component_own_bytes": own_bytes},
        "qa_verdict": qa_env.get("verdict"),
        "qa_checks": {k: v.get("status") for k, v in checks.items()},
        "network_attempts": net.get("detail"),
        "evidence": [str(qa_out / "motion-qa-report.json"), str(bundle_path)],
    }
