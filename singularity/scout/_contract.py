"""Contract plumbing for design-scout (CONTRACT.md schema 1): envelope with
version evidence, exit mapping, --out filesystem policy, atomic writes."""
from __future__ import annotations

import json
import os
import platform
import tempfile
from pathlib import Path

EXIT_PASS = 0
EXIT_FAIL = 1          # evaluative commands only (probe, selftest)
EXIT_TOOL_ERROR = 2

ENGINE_ROOT = Path(__file__).resolve().parents[2]


class ContractError(Exception):
    """Tool-level failure -> exit 2. Never a domain verdict."""


def tool_versions() -> dict:
    """Version evidence; must never crash (error envelopes flow through it)."""
    from . import __version__
    return {"design-scout": __version__, "python": platform.python_version()}


def envelope(command: str, inputs: dict, data: dict | None = None,
             verdict: dict | None = None, outputs: list[str] | None = None,
             error: str | None = None) -> dict:
    from .cli import SCHEMA_VERSION
    env = {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "status": "error" if error else "ok",
        "tool": tool_versions(),
        "inputs": inputs,
    }
    if data is not None:
        env["data"] = data
    if verdict is not None:
        env["verdict"] = verdict
    if outputs is not None:
        env["outputs"] = outputs
    if error is not None:
        env["error"] = error
    return env


def resolve_out(out: str | None) -> Path:
    """CONTRACT.md §3: --out required, canonicalized, never inside the engine
    repo or any DSCOUT_PROTECTED_ROOTS root (calling skills set this to the
    target project tree — the tool physically cannot write into it)."""
    if not out:
        raise ContractError("--out is required for this subcommand (contract §3)")
    out_dir = Path(out).resolve()
    protected = [ENGINE_ROOT]
    protected += [Path(r).resolve() for r in
                  filter(None, os.environ.get("DSCOUT_PROTECTED_ROOTS", "").split(os.pathsep))]
    for root in protected:
        if out_dir == root or root in out_dir.parents or out_dir in root.parents:
            raise ContractError(
                f"--out {out_dir} is inside/above protected root {root} "
                f"(contract §3)")
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def atomic_write_bytes(payload: bytes, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=f".{dest.name}.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(text: str, dest: Path) -> None:
    atomic_write_bytes(text.encode("utf-8"), dest)


def emit(env: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(env, ensure_ascii=False))
        return
    line = f"[design-scout {env['command']}] {env['status']}"
    if env.get("verdict") is not None:
        line += f" | verdict: {env['verdict'].get('result')}" \
                + (f" ({env['verdict'].get('reason')})" if env['verdict'].get('reason') else "")
    if env.get("outputs"):
        line += f" | outputs: {len(env['outputs'])}"
    if env.get("error"):
        line += f" | error: {env['error']}"
    print(line)
    for key, val in (env.get("data") or {}).items():
        print(f"  {key}: {json.dumps(val, ensure_ascii=False)}")
