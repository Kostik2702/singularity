"""Inspection phases for component-intake (CONTRACT.md §2). Pure functions
over the fetched payload — nothing here executes candidate code."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

LIFECYCLE_SCRIPTS = {"preinstall", "install", "postinstall", "prepare",
                     "prepublish", "prepublishOnly"}
HEAVY_ENGINES = {"three", "gsap", "@react-three/fiber", "@react-three/drei",
                 "motion", "framer-motion", "animejs"}
NETWORK_ALLOWLIST = ("fonts.googleapis.com", "fonts.gstatic.com")
# no console window per child (singularity.platform.proc.NO_WINDOW; the factory
# imports nothing from the engine, so the expression is repeated)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_EVAL_RE = re.compile(r"\beval\s*\(|new\s+Function\s*\(")
_NET_RE = re.compile(
    r"\bfetch\s*\(|XMLHttpRequest|WebSocket\s*\(|navigator\.sendBeacon|"
    r"https?://[a-z0-9.-]+", re.IGNORECASE)
_URL_RE = re.compile(r"https?://([a-z0-9.-]+)", re.IGNORECASE)
_ANIM_RE = re.compile(
    r"requestAnimationFrame|@keyframes|animation\s*:|transition\s*:")
_REDUCED_RE = re.compile(r"prefers-reduced-motion", re.IGNORECASE)
_STORAGE_RE = re.compile(r"document\.cookie|localStorage|sessionStorage")

SPDX_HINTS = {
    "mit license": "MIT", "apache license": "Apache-2.0",
    "bsd 3-clause": "BSD-3-Clause", "mozilla public license": "MPL-2.0",
    "commons clause": "custom: Commons Clause",
    "cc0": "CC0-1.0", "the unlicense": "Unlicense", "isc license": "ISC",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def snapshot_files(files: list[dict]) -> list[dict]:
    out = []
    for f in files:
        content = f.get("content") or ""
        out.append({
            "path": f.get("path") or f.get("target") or "unnamed",
            "bytes": len(content.encode("utf-8")),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        })
    return out


def classify_license(record_license: str | None, files: list[dict]) -> dict:
    """§2.4: E02 record > LICENSE file > SPDX header scan."""
    if record_license and record_license.strip().lower() not in ("", "unknown"):
        return {"license": record_license.strip(),
                "source": "candidate-record (E02 provenance chain)"}
    for f in files:
        name = (f.get("path") or "").lower()
        if "license" in Path(name).name:
            text = (f.get("content") or "").lower()
            for hint, spdx in SPDX_HINTS.items():
                if hint in text:
                    return {"license": spdx, "source": f"LICENSE file: {name}"}
            return {"license": "custom", "source": f"LICENSE file: {name}"}
    for f in files:
        head = (f.get("content") or "")[:600].lower()
        for hint, spdx in SPDX_HINTS.items():
            if hint in head:
                return {"license": spdx,
                        "source": f"SPDX header in {f.get('path')}"}
    return {"license": "unknown", "source": "no license evidence found"}


def scan_scripts(payload: dict) -> list[str]:
    """§2.5: lifecycle scripts in an embedded package.json = REJECT."""
    findings = []
    for f in payload.get("files", []):
        if Path(f.get("path") or "").name != "package.json":
            continue
        try:
            pkg = json.loads(f.get("content") or "{}")
        except ValueError:
            findings.append(f"{f.get('path')}: unparseable package.json")
            continue
        bad = sorted(set(pkg.get("scripts", {})) & LIFECYCLE_SCRIPTS)
        if bad:
            findings.append(f"{f.get('path')}: lifecycle scripts {bad}")
    return findings


def scan_static(files: list[dict]) -> dict:
    """§2.6: eval/network/storage scan. Returns reject[] and risks[]."""
    reject, risks = [], []
    for f in files:
        path = f.get("path") or "?"
        content = f.get("content") or ""
        if _EVAL_RE.search(content):
            reject.append(f"{path}: eval/new Function")
        for m in _NET_RE.finditer(content):
            frag = m.group(0)
            host_m = _URL_RE.match(frag) if frag.lower().startswith("http") else None
            if host_m:
                host = host_m.group(1).lower()
                if not any(host.endswith(a) for a in NETWORK_ALLOWLIST):
                    reject.append(f"{path}: network reference {host}")
            else:
                reject.append(f"{path}: runtime network API {frag.strip()[:40]}")
        if _STORAGE_RE.search(content):
            risks.append(f"{path}: browser storage access")
    # dedup, stable
    return {"reject": sorted(set(reject)), "risks": sorted(set(risks))}


def _dep_name(spec: str) -> str:
    """'gsap@^3.13.0' -> 'gsap'; '@react-three/fiber@9' -> '@react-three/fiber'
    (live E02 data carries version specifiers)."""
    spec = spec.strip()
    at = spec.rfind("@")
    if at > 0:  # 0 would be a scoped package's own @
        return spec[:at]
    return spec


def review_deps(dependencies: list[str]) -> dict:
    names = {_dep_name(d) for d in dependencies}
    heavy = sorted(names & HEAVY_ENGINES)
    engines = [d for d in heavy if d in ("three", "gsap", "motion",
                                         "framer-motion", "animejs")]
    requirements = []
    risks = []
    if heavy:
        requirements.append(f"heavy deps {heavy}: lazy-island + budget check")
    if len(engines) > 1:
        risks.append(f"mixed animation engines in one candidate: {engines}")
    return {"heavy": heavy, "requirements": requirements, "risks": risks}


def _esbuild_path() -> str | None:
    env = os.environ.get("INTAKE_ESBUILD")
    if env and Path(env).is_file():
        return env
    return shutil.which("esbuild") or shutil.which("esbuild.cmd")


def _scrubbed_env() -> dict:
    keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "COMSPEC", "PATHEXT")
    return {k: v for k, v in os.environ.items() if k.upper() in keep}


def check_build(files: list[dict], workspace: Path) -> dict:
    """§2.8: esbuild transform proof (no execution). unavailable != PASS."""
    exe = _esbuild_path()
    code_files = [f for f in files
                  if (f.get("path") or "").endswith((".js", ".jsx", ".ts", ".tsx"))]
    if not code_files:
        return {"status": "unavailable", "why": "no code files to transform"}
    if not exe:
        return {"status": "unavailable",
                "why": "esbuild not found (set INTAKE_ESBUILD) — verdict capped at ADAPT"}
    failures = []
    for f in code_files:
        src = workspace / "src" / (f.get("path") or "file.jsx")
        loader = src.suffix.lstrip(".")
        try:
            proc = subprocess.run(
                [exe, str(src), f"--loader:.{loader}={'jsx' if loader in ('js', 'jsx') else 'tsx'}",
                 "--outfile=" + str(workspace / "build" / (src.name + ".out.js"))],
                capture_output=True, text=True, encoding="utf-8",
                timeout=60, env=_scrubbed_env(), creationflags=_NO_WINDOW)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {"status": "unavailable", "why": f"esbuild run failed: {exc}"}
        if proc.returncode != 0:
            failures.append(f"{f.get('path')}: {proc.stderr.strip()[:300]}")
    if failures:
        return {"status": "fail", "failures": failures}
    return {"status": "ok", "transformed": len(code_files)}


def check_reduced_motion(files: list[dict]) -> dict:
    """§2.9: animating code must acknowledge prefers-reduced-motion."""
    animates = any(_ANIM_RE.search(f.get("content") or "") for f in files)
    handles = any(_REDUCED_RE.search(f.get("content") or "") for f in files)
    if animates and not handles:
        return {"status": "adapt",
                "requirement": "add prefers-reduced-motion variant "
                               "(component animates, no handling found)"}
    return {"status": "ok", "animates": animates, "handles": handles}
