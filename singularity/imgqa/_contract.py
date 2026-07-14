"""Shared contract plumbing for imgqa subcommands (CONTRACT.md schema 1).

Everything a subcommand needs to stay inside the frozen contract lives here:
envelope building with version evidence, exit-code mapping, the --out
filesystem policy (canonicalize, refuse source-tree writes, atomic replace)
and deterministic output naming.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

EXIT_PASS = 0
EXIT_FAIL = 1          # evaluative commands only
EXIT_TOOL_ERROR = 2

SUBPROCESS_TIMEOUT = 60
SUBPROCESS_TIMEOUT_LONG = 300  # sheet, cutout


class ContractError(Exception):
    """Tool-level failure -> exit 2. Never a domain verdict."""


def tool_versions() -> dict:
    """Version evidence. Must never crash — the envelope (including ERROR
    envelopes about missing deps) is built through this function, so a missing
    library reports as null instead of taking the JSON contract down with it."""
    from . import __version__
    versions = {"imgqa": __version__}
    for key, module in (("pillow", "PIL"), ("numpy", "numpy"), ("scipy", "scipy")):
        try:
            versions[key] = __import__(module).__version__
        except ImportError:
            versions[key] = None
    return versions


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


def resolve_out(out: str | None, input_paths: list[Path]) -> Path:
    """CONTRACT.md §3: --out required, canonicalized, never the source tree.

    Rejected (F8): the input's own directory, its ancestors, its DESCENDANTS,
    and anything under IMGQA_PROTECTED_ROOTS (os.pathsep-separated roots that
    calling skills set to the audited project, e.g. the NIKA tree).
    """
    if not out:
        raise ContractError("--out is required for this subcommand (contract §3)")
    out_dir = Path(out).resolve()
    for src in input_paths:
        src_dir = src.resolve().parent
        if out_dir == src_dir or out_dir in src_dir.parents or src_dir in out_dir.parents:
            raise ContractError(
                f"--out {out_dir} is inside/above the source directory of "
                f"{src} — in-place writes are forbidden (contract §3)")
    for root in filter(None, os.environ.get("IMGQA_PROTECTED_ROOTS", "").split(os.pathsep)):
        protected = Path(root).resolve()
        if out_dir == protected or protected in out_dir.parents:
            raise ContractError(
                f"--out {out_dir} is inside protected root {protected} "
                f"(IMGQA_PROTECTED_ROOTS, contract §3)")
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def atomic_write_bytes(payload: bytes, dest: Path) -> None:
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


def atomic_save_image(im, dest: Path, **save_kwargs) -> None:
    import io
    buf = io.BytesIO()
    fmt = save_kwargs.pop("format", None) or dest.suffix.lstrip(".").upper().replace("JPG", "JPEG")
    im.save(buf, format=fmt, **save_kwargs)
    atomic_write_bytes(buf.getvalue(), dest)


def out_name(input_path: Path, subcommand: str, qualifier: str | None,
             ext: str) -> str:
    """Deterministic naming: <stem>.<subcommand>[.<qualifier>].<ext> (§3)."""
    parts = [input_path.stem, subcommand]
    if qualifier:
        parts.append(qualifier)
    return ".".join(parts) + "." + ext.lstrip(".")


def parse_bbox(spec: str, image_size: tuple[int, int] | None = None
               ) -> tuple[int, int, int, int]:
    """CONTRACT.md §4: bbox as X,Y,W,H -> returns (x0, y0, x1, y1).

    With image_size=(w,h) the bbox must lie FULLY inside the image (F3):
    negative coords silently address from the end in numpy, and an
    out-of-bounds box makes defringe a successful no-op — both are silent
    wrong answers, so a cropped/empty intersection is a tool error. Callers
    building generous margins must clamp BEFORE passing the bbox.
    """
    try:
        x, y, w, h = (int(v) for v in spec.split(","))
    except ValueError as exc:
        raise ContractError(f"bad --bbox {spec!r}: expected X,Y,W,H integers") from exc
    if x < 0 or y < 0:
        raise ContractError(f"bad --bbox {spec!r}: negative origin")
    if w <= 0 or h <= 0:
        raise ContractError(f"bad --bbox {spec!r}: width/height must be positive")
    if image_size is not None:
        iw, ih = image_size
        if x + w > iw or y + h > ih:
            raise ContractError(
                f"--bbox {spec!r} exceeds image bounds {iw}x{ih} — clamp it "
                f"before calling (a cropped bbox would silently mis-measure)")
    return x, y, x + w, y + h


# Resource guard (F7 / contract §3): in-process commands have no wall-clock
# timeout, so runaway cost is bounded by refusing oversized inputs instead.
MAX_IMAGE_PIXELS = 120_000_000


def load_rgba(path: Path):
    from PIL import Image
    if not path.is_file():
        raise ContractError(f"input not found: {path}")
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS  # DecompressionBombError above this
    try:
        im = Image.open(path)
        if im.width * im.height > MAX_IMAGE_PIXELS:
            raise ContractError(
                f"image {im.width}x{im.height} exceeds the {MAX_IMAGE_PIXELS} "
                f"pixel resource guard (contract §3)")
        return im.convert("RGBA")
    except Image.DecompressionBombError as exc:
        raise ContractError(f"decompression bomb guard: {exc}") from exc


def emit(env: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(env, ensure_ascii=False))
        return
    line = f"[imgqa {env['command']}] {env['status']}"
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
