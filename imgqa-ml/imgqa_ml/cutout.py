"""python -m imgqa_ml.cutout <image> --out <dir> [--json] [--model NAME]

Background removal via rembg/BiRefNet. Emits the typed JSON payload the imgqa
dispatch validates (validate_cutout_payload): schema_version / status /
command="cutout" / outputs (files inside --out) / data. Any failure prints a
status=error payload and exits non-zero — the dispatch maps that to exit 2
(tool error), never to "background was already clean".
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import NoReturn

SCHEMA_VERSION = 2  # matches imgqa CONTRACT.md — bump together
DEFAULT_MODEL = "birefnet-general"


def _die(msg: str) -> NoReturn:
    print(json.dumps({"schema_version": SCHEMA_VERSION, "command": "cutout",
                      "status": "error", "error": msg}))
    sys.exit(1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="imgqa_ml.cutout")
    ap.add_argument("image")
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--json", action="store_true")  # accepted for symmetry; output is always JSON
    args = ap.parse_args(argv)

    src = Path(args.image)
    if not src.is_file():
        _die(f"input not found: {src}")
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # weights live next to the venv on D: (owner rule: never on C:); the env
    # var wins so tests/callers can redirect
    os.environ.setdefault("U2NET_HOME", str(Path(sys.prefix) / "models"))

    try:
        from PIL import Image
        from rembg import new_session, remove
    except ImportError as exc:
        _die(f"ML venv is missing dependencies: {exc}")

    try:
        t0 = time.time()
        session = new_session(args.model)
        t_session = time.time() - t0
        im = Image.open(src)
        t0 = time.time()
        result = remove(im, session=session)
        t_infer = time.time() - t0
    except Exception as exc:  # noqa: BLE001 — every crash is a typed error payload
        _die(f"{type(exc).__name__}: {exc}")

    dest = out_dir / f"{src.stem}.cutout.png"
    fd, tmp = tempfile.mkstemp(dir=str(out_dir), prefix=f".{dest.stem}.", suffix=".png")
    os.close(fd)
    result.save(tmp, format="PNG")
    os.replace(tmp, dest)

    print(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "command": "cutout",
        "status": "ok",
        "outputs": [str(dest)],
        "data": {
            "model": args.model,
            "input_size": list(im.size),
            "session_s": round(t_session, 2),
            "inference_s": round(t_infer, 2),
            "u2net_home": os.environ["U2NET_HOME"],
        },
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
