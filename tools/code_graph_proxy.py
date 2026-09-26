"""Build the 100k-file benchmark proxy for the code graph (CODE-GRAPH P0).

The owner's performance budget is stated for a 100k-file repository, and no
repository of that size is on this machine. The council measured on a proxy
assembled from real third-party sources, and the critic named the two ways
such a proxy lies:

* COPIES of one repository make every name occur N times, so every call is
  ambiguous and the resolver's cheap "skip" path dominates -- speed looks
  better than it will be. Here every file is content-deduplicated (sha256):
  each byte-identical file enters once, whichever source it came from.
* FLATTENED paths break relative imports, so the resolver has less to do than
  in a real tree. Here each source's directory structure is preserved under
  `<out>/<tag>/`, so a package's imports still resolve within it.

Sources are passed on the command line, never written into this file: they
are paths on one machine, and the engine does not carry those.

Deterministic: sources in the order given, directories and files sorted, the
first `--target` distinct files win. The manifest records a fingerprint over
(relative path, content hash) of every file, so two builds are comparable by
one string.

Usage:
    python tools/code_graph_proxy.py build --out E:/tmp/code-graph-proxy-100k \
        --source py_site=<site-packages> --source py_bliss=E:/blissful-tuner \
        --source nm_thsaas=E:/Project/talking-head-saas/node_modules ... \
        --target 100000 --git
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

CODE_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
PRUNE = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", "dist", "build", ".next",
         ".turbo", ".cache", "coverage"}
MAX_FILE_BYTES = 1_000_000     # a megabyte of "source" is a bundle or a data blob
MAX_OUT_PATH = 240             # below Windows MAX_PATH with LongPathsEnabled=0


def _walk(src: Path):
    """Code files under `src` in a deterministic order. A source that IS a
    node_modules keeps its node_modules children; any other prunes them, so a
    project tree contributes its own code and not its dependencies twice."""
    inside_nm = src.name == "node_modules"
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in PRUNE and (inside_nm or d != "node_modules"))
        for name in sorted(filenames):
            if name.lower().endswith(CODE_SUFFIXES):
                yield Path(dirpath) / name


def build(sources: list[tuple[str, Path]], out: Path, target: int) -> dict:
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"[proxy] {out} is not empty; refusing to mix two builds")
    out.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    fingerprint = hashlib.sha256()
    per_source: dict[str, dict] = {}
    total = 0
    started = time.perf_counter()
    for tag, src in sources:
        stats = {"root": str(src), "files": 0, "bytes": 0, "duplicates": 0,
                 "too_big": 0, "path_too_long": 0, "unreadable": 0}
        for path in _walk(src):
            if total >= target:
                break
            try:
                size = path.stat().st_size
                if size > MAX_FILE_BYTES:
                    stats["too_big"] += 1
                    continue
                data = path.read_bytes()
            except OSError:
                stats["unreadable"] += 1
                continue
            digest = hashlib.sha256(data).hexdigest()
            if digest in seen:
                stats["duplicates"] += 1
                continue
            rel = Path(tag) / path.relative_to(src)
            dest = out / rel
            if len(str(dest)) > MAX_OUT_PATH:
                stats["path_too_long"] += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            seen.add(digest)
            fingerprint.update(f"{rel.as_posix()}\0{digest}\n".encode())
            stats["files"] += 1
            stats["bytes"] += size
            total += 1
        per_source[tag] = stats
    by_suffix: dict[str, int] = {}
    for p in out.rglob("*"):
        if p.is_file():
            by_suffix[p.suffix.lower()] = by_suffix.get(p.suffix.lower(), 0) + 1
    return {"target": target, "files": total, "fingerprint": fingerprint.hexdigest(),
            "by_suffix": dict(sorted(by_suffix.items())), "sources": per_source,
            "seconds": round(time.perf_counter() - started, 1)}


def _git_commit(out: Path) -> str:
    run = lambda *a: subprocess.run(["git", "-C", str(out), *a], check=True,  # noqa: E731
                                    capture_output=True, text=True).stdout.strip()
    run("init", "-q")
    run("add", "-A")
    run("-c", "user.name=code-graph-proxy", "-c", "user.email=proxy@localhost",
        "commit", "-q", "-m", "proxy build")
    return run("rev-parse", "--short", "HEAD")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--out", required=True)
    b.add_argument("--source", action="append", required=True, metavar="TAG=DIR")
    b.add_argument("--target", type=int, default=100_000)
    b.add_argument("--git", action="store_true", help="commit the proxy so git-based detection can be measured")
    args = ap.parse_args(argv)

    sources: list[tuple[str, Path]] = []
    for spec in args.source:
        tag, sep, root = spec.partition("=")
        if not sep or not tag or not Path(root).is_dir():
            print(f"[proxy] bad --source {spec!r}: want TAG=<existing directory>", file=sys.stderr)
            return 2
        sources.append((tag, Path(root)))
    out = Path(args.out)
    manifest = build(sources, out, args.target)
    if args.git:
        manifest["git_head"] = _git_commit(out)
    if manifest["files"] < args.target:
        print(f"[proxy] only {manifest['files']} distinct files for a target of {args.target} "
              f"-- add sources; a smaller proxy measures a smaller repository", file=sys.stderr)
    (out.parent / f"{out.name}.manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("files", "fingerprint", "by_suffix", "seconds")}, indent=1))
    return 0 if manifest["files"] >= args.target else 3


if __name__ == "__main__":
    raise SystemExit(main())
