"""The P3 equivalence oracle: an updated graph equals a full build (UNMET j).

Each sequence grows a small random project -- Python and TypeScript files
drawn from ONE vocabulary of paths and names, so imports hit and miss,
names collide and part again, bases and re-exports cross files -- then
applies random edits step by step: a file rewritten, deleted, created at a
path something already imports, moved, broken and fixed; in git mode also
commits, staged edits and reverts to HEAD. After every step the unit is
refreshed in place (`update.refresh`, the query path), and a full build of
the same tree is taken in a second vault. Every table an answer or a later
update reads -- files, nodes, edges, unresolved, deps, lookups -- must be
equal outside the stale set the update declared (it declares none at this
size).

    python tools/code_graph_oracle.py --sequences 200 --steps 6 --out docs/code-graph-p3-oracle.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

PY_PATHS = ["pkg/__init__.py", "pkg/a.py", "pkg/b.py", "pkg/c.py", "pkg/sub/__init__.py", "pkg/sub/d.py",
            "pkg/sub/e.py", "tests/test_a.py", "tools/script.py"]
TS_PATHS = ["web/index.ts", "web/a.ts", "web/b.ts", "web/c.tsx", "web/lib/index.ts", "web/lib/d.ts",
            "web/a.test.ts", "web/e.js"]
FUNCS = ["run", "helper", "load", "store", "parse"]
CLASSES = ["Engine", "Repo", "Base", "Service"]
METHODS = ["start", "find", "store", "run"]


def _module(path: str) -> str:
    return path[:-3].replace("/__init__", "").replace("/", ".")


def py_source(rng: random.Random, path: str) -> str:
    lines: list[str] = []
    for _ in range(rng.randint(0, 3)):
        target = rng.choice(PY_PATHS + ["pkg/missing.py", "pkg/sub/f.py"])
        mod = _module(target)
        form = rng.randint(0, 4)
        if form == 0:
            lines.append(f"from {mod} import {rng.choice(FUNCS + CLASSES)}")
        elif form == 1:
            lines.append(f"import {mod}")
        elif form == 2 and "." in mod:
            head, _, last = mod.rpartition(".")
            lines.append(f"from {head} import {last}")
        elif form == 3:
            lines.append(f"from {mod} import *")
        else:
            lines.append(f"from {mod} import {rng.choice(FUNCS)} as {rng.choice(FUNCS)}_alias")
    for _ in range(rng.randint(0, 2)):
        name = rng.choice(CLASSES)
        base = rng.choice(CLASSES + ["object", "dict", ""])
        lines.append(f"class {name}({base}):" if base and base != name else f"class {name}:")
        lines.append("    def __init__(self):")
        lines.append(f"        self.repo = {rng.choice(CLASSES)}()")
        for m in rng.sample(METHODS, rng.randint(1, 3)):
            ann = rng.choice(CLASSES)
            lines.append(f"    def {m}(self, other: {ann}):")
            lines += [f"        {c}" for c in _py_calls(rng)] or ["        pass"]
    for fn in rng.sample(FUNCS, rng.randint(0, 3)):
        lines.append(f"def {fn}(x, s):")
        if rng.random() < 0.3:
            lines.append(f"    def {rng.choice(FUNCS)}():")
            lines.append("        return 1")
        if rng.random() < 0.3:
            lines += ["    s = s.replace('a', 'b')"] * rng.randint(2, 5)
        if rng.random() < 0.4:
            lines.append(f"    x = {rng.choice(CLASSES)}()")
        lines += [f"    {c}" for c in _py_calls(rng)] or ["    pass"]
    if rng.random() < 0.15:
        lines.append("def broken(:")
    return "\n".join(lines) + "\n"


def _py_calls(rng: random.Random) -> list[str]:
    out = []
    for _ in range(rng.randint(0, 4)):
        form = rng.randint(0, 5)
        if form == 0:
            out.append(f"{rng.choice(FUNCS)}()")
        elif form == 1:
            out.append(f"x.{rng.choice(METHODS)}()")
        elif form == 2:
            out.append(f"self.{rng.choice(METHODS)}()" if rng.random() < 0.5 else "super().start()")
        elif form == 3:
            out.append(f"{rng.choice(['pkg.a', 'pkg.sub.d', 'd', 'b'])}.{rng.choice(FUNCS)}()")
        elif form == 4:
            out.append(f"self.repo.{rng.choice(METHODS)}()")
        else:
            out.append(f"s.{rng.choice(['replace', 'visit', 'helper'])}()")
    return out


def _rel(src: str, dst: str) -> str:
    rel = os.path.relpath(dst, os.path.dirname(src) or ".").replace("\\", "/")
    rel = rel.rsplit(".", 1)[0]
    return rel if rel.startswith(".") else "./" + rel


def ts_source(rng: random.Random, path: str) -> str:
    lines: list[str] = []
    for _ in range(rng.randint(0, 3)):
        target = rng.choice(TS_PATHS + ["web/missing.ts", "web/lib/f.ts"])
        spec = _rel(path, target)
        form = rng.randint(0, 4)
        if form == 0:
            lines.append(f"import {{ {rng.choice(FUNCS + CLASSES)} }} from '{spec}';")
        elif form == 1:
            lines.append(f"import * as ns{rng.randint(0, 2)} from '{spec}';")
        elif form == 2:
            lines.append(f"export * from '{spec}';")
        elif form == 3:
            lines.append(f"export {{ {rng.choice(FUNCS)} as {rng.choice(FUNCS)} }} from '{spec}';")
        else:
            lines.append(f"import {{ {rng.choice(FUNCS)} as alias{rng.randint(0, 2)} }} from '{spec}';")
    for _ in range(rng.randint(0, 2)):
        name = rng.choice(CLASSES)
        if rng.random() < 0.3:
            lines.append(f"export interface {name}I {{ {rng.choice(METHODS)}(): void; repo: {rng.choice(CLASSES)} }}")
        base = rng.choice(CLASSES + [""])
        ext = f" extends {base}" if base and base != name else ""
        lines.append(f"export class {name}{ext} {{")
        lines.append(f"  repo: {rng.choice(CLASSES)};")
        lines.append(f"  constructor() {{ super(); this.cur = new {rng.choice(CLASSES)}(); }}")
        for m in rng.sample(METHODS, rng.randint(1, 3)):
            lines.append(f"  {m}(other: {rng.choice(CLASSES)}): void {{ {' '.join(_ts_calls(rng))} }}")
        lines.append("}")
    for fn in rng.sample(FUNCS, rng.randint(0, 3)):
        lines.append(f"export function {fn}(x: {rng.choice(CLASSES)}): void {{ {' '.join(_ts_calls(rng))} }}")
    if rng.random() < 0.15:
        lines.append("export function broken( {")
    return "\n".join(lines) + "\n"


def _ts_calls(rng: random.Random) -> list[str]:
    out = []
    for _ in range(rng.randint(0, 4)):
        form = rng.randint(0, 4)
        if form == 0:
            out.append(f"{rng.choice(FUNCS)}();")
        elif form == 1:
            out.append(f"x.{rng.choice(METHODS)}();")
        elif form == 2:
            out.append(f"this.{rng.choice(METHODS)}();")
        elif form == 3:
            out.append(f"this.repo.{rng.choice(METHODS)}();")
        else:
            out.append(f"ns{rng.randint(0, 2)}.{rng.choice(FUNCS)}();")
    return out


def source(rng: random.Random, path: str) -> str:
    return py_source(rng, path) if path.endswith(".py") else ts_source(rng, path)


class Project:
    def __init__(self, root: Path, rng: random.Random, git: bool):
        self.root, self.rng, self.git = root, rng, git
        self.files: dict[str, str] = {}

    def write(self, rel: str, text: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        self.files[rel] = text

    def delete(self, rel: str) -> None:
        (self.root / rel).unlink(missing_ok=True)
        self.files.pop(rel, None)

    def sh(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, check=True)

    def commit(self) -> None:
        self.sh("add", "-A")
        self.sh("-c", "user.email=o@o", "-c", "user.name=o", "commit", "-q", "--allow-empty", "-m", "s")

    def seed(self) -> None:
        for rel in self.rng.sample(PY_PATHS, 6) + self.rng.sample(TS_PATHS, 5):
            self.write(rel, source(self.rng, rel))
        self.write("web/package.json", json.dumps({"name": "@o/web"}))
        if self.git:
            self.sh("init", "-q")
            self.commit()

    def edit(self) -> list[str]:
        """1-3 random edits; returns what was done, for the report."""
        done = []
        for _ in range(self.rng.randint(1, 3)):
            op = self.rng.random()
            existing = sorted(self.files)
            code = [p for p in existing if p.endswith((".py", ".ts", ".tsx", ".js"))]
            if op < 0.35 and code:
                rel = self.rng.choice(code)
                self.write(rel, source(self.rng, rel))
                done.append(f"rewrite {rel}")
            elif op < 0.50 and code:
                rel = self.rng.choice(code)
                self.delete(rel)
                done.append(f"delete {rel}")
            elif op < 0.65:
                rel = self.rng.choice(PY_PATHS + TS_PATHS + ["pkg/missing.py", "pkg/sub/f.py",
                                                             "web/missing.ts", "web/lib/f.ts"])
                self.write(rel, source(self.rng, rel))
                done.append(f"create {rel}")
            elif op < 0.75 and code:
                src = self.rng.choice(code)
                pool = PY_PATHS if src.endswith(".py") else TS_PATHS
                dst = self.rng.choice([p for p in pool if p != src])
                text = self.files[src]
                self.delete(src)
                self.write(dst, text)
                done.append(f"move {src} -> {dst}")
            elif op < 0.85 and code:
                rel = self.rng.choice(code)
                text = self.files[rel]
                broken = "def broken(:\n" if rel.endswith(".py") else "export function broken( {\n"
                self.write(rel, text.replace(broken, "") if broken in text else text + broken)
                done.append(f"break/fix {rel}")
            elif self.git and op < 0.92:
                self.commit()
                done.append("commit")
            elif self.git and code:
                rel = self.rng.choice(code)
                try:
                    self.sh("checkout", "HEAD", "--", rel)
                    self.files[rel] = (self.root / rel).read_text(encoding="utf-8")
                    done.append(f"revert {rel}")
                except subprocess.CalledProcessError:
                    done.append(f"revert {rel} (not in HEAD)")
            elif code:
                rel = self.rng.choice(code)
                self.write(rel, self.files[rel] + "\ndef helper():\n    return 9\n" if rel.endswith(".py")
                           else self.files[rel] + "\nexport function helper(): void {}\n")
                done.append(f"duplicate helper in {rel}")
        return done


def dump(cfg, key: str) -> dict[str, list]:
    from singularity.codegraph import store
    con = store.connect_ro(store.current_db(cfg, key))
    try:
        path = {r[0]: r[1] for r in con.execute("SELECT id, path FROM files")}
        stale = {r[0] for r in con.execute("SELECT path FROM stale")}

        def rows(sql: str) -> list[tuple]:
            return sorted(tuple(map(str, (path[r[0]], *r[1:]))) for r in con.execute(sql)
                          if path[r[0]] not in stale)
        return {
            "files": sorted((r[0], r[1], r[2], r[3], str(r[4])) for r in con.execute(
                "SELECT path, lang, content_hash, parse_status, is_test FROM files")),
            "nodes": rows("SELECT file_id, qname, name, kind, parent, spans, signature, doc1, decorators,"
                          " exported, local FROM nodes"),
            "edges": rows("SELECT file_id, src, dst, kind, tier, callee, line FROM edges"),
            "unresolved": rows("SELECT file_id, src, name, receiver, line FROM unresolved"),
            "deps": rows("SELECT d.file_id, p.path FROM deps d JOIN dep_paths p ON p.id = d.path_id"),
            "lookups": rows("SELECT file_id, name FROM lookups"),
        }
    finally:
        con.close()


def _diff(a: dict[str, list], b: dict[str, list]) -> dict[str, dict]:
    out = {}
    for k in a:
        if a[k] != b[k]:
            sa, sb = set(a[k]), set(b[k])
            out[k] = {"update_only": sorted(sa - sb)[:5], "build_only": sorted(sb - sa)[:5],
                      "counts": [len(a[k]), len(b[k])]}
    return out


def run_sequence(seed: int, steps: int, git: bool, base: Path) -> dict:
    from singularity.codegraph import build, scope, update
    from singularity.config import Config
    rng = random.Random(seed)
    work = base / f"s{seed}"
    root = work / "Project" / "unit"
    root.mkdir(parents=True)
    proj = Project(root, rng, git)
    proj.seed()
    inc = Config(vault=work / "vault-update", data={"code_repos_dir": str(work / "Project"),
                                                    "code_graph_enabled": True})
    full = Config(vault=work / "vault-full", data=dict(inc.data))
    (unit,) = scope.discover_units(inc)
    build.build_unit(inc, unit, workers=1)
    log: list[dict] = []
    for step in range(steps):
        edits = proj.edit()
        fr = update.refresh(inc, unit.key, background_ok=False)
        entry = {"step": step, "edits": edits, "updated": fr.updated, "stale": fr.stale_files,
                 "stale_all": fr.stale_all, "note": fr.note}
        if fr.stale_all:
            # a rebuild-class change (a manifest, .gitignore): the update refuses, as designed;
            # the full build is what the background job would run
            build.build_unit(inc, unit, workers=1)
            entry["rebuilt"] = True
        (u2,) = scope.discover_units(full)
        build.build_unit(full, u2, workers=1)
        diff = _diff(dump(inc, unit.key), dump(full, u2.key))
        entry["equal"] = not diff
        if diff:
            entry["diff"] = diff
        log.append(entry)
        if diff:
            break
    return {"seed": seed, "git": git, "steps": log, "equal": all(e["equal"] for e in log)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sequences", type=int, default=200)
    ap.add_argument("--steps", type=int, default=6)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    os.environ["MB_CODE_NO_BACKGROUND"] = "1"
    base = Path(tempfile.mkdtemp(prefix="cg-oracle-"))
    started = time.perf_counter()
    results = []
    try:
        for i in range(args.sequences):
            seed = args.seed + i
            try:
                results.append(run_sequence(seed, args.steps, git=i % 2 == 0, base=base))
            except Exception as exc:          # a crash is a failed sequence, with its seed
                import traceback
                results.append({"seed": seed, "git": i % 2 == 0, "steps": [], "equal": False,
                                "error": "".join(traceback.format_exception_only(type(exc), exc)).strip(),
                                "trace": traceback.format_exc()[-3000:]})
            if not results[-1]["equal"]:
                print(json.dumps(results[-1], indent=1)[:4000])
    finally:
        shutil.rmtree(base, ignore_errors=True)
    failed = [r["seed"] for r in results if not r["equal"]]
    edits = sum(len(s["edits"]) for r in results for s in r["steps"])
    summary = {"sequences": len(results), "steps": sum(len(r["steps"]) for r in results), "edits": edits,
               "git_sequences": sum(r["git"] for r in results), "failed_seeds": failed,
               "rebuild_steps": sum(1 for r in results for s in r["steps"] if s.get("rebuilt")),
               "seconds": round(time.perf_counter() - started, 1)}
    print(json.dumps(summary))
    if args.out:
        Path(args.out).write_text(json.dumps(report(summary, results), indent=1) + "\n", encoding="utf-8")
    return 1 if failed else 0


def report(summary: dict, results: list[dict]) -> dict:
    """What goes into docs/: the summary, how often each kind of edit was
    exercised, one line per sequence, and every failure in full."""
    kinds: dict[str, int] = {}
    for r in results:
        for s in r["steps"]:
            for e in s["edits"]:
                k = e.split(" ", 1)[0]
                kinds[k] = kinds.get(k, 0) + 1
    return {"summary": summary, "edit_kinds": dict(sorted(kinds.items())),
            "sequences": [[r["seed"], "git" if r["git"] else "plain", len(r["steps"]),
                           sum(len(s["edits"]) for s in r["steps"]),
                           sum(len(s.get("updated") or []) for s in r["steps"]), r["equal"]] for r in results],
            "sequence_columns": ["seed", "mode", "steps", "edits", "files updated in place", "equal"],
            "failures": [r for r in results if not r["equal"]]}


if __name__ == "__main__":
    raise SystemExit(main())
