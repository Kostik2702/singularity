"""`deadcode` and `arch`: whole-graph answers (CODE-GRAPH P4). Read-only,
parser-free, like `query`.

deadcode. A definition nothing calls or inherits from is a CANDIDATE, not a
verdict: the graph sees calls it could resolve, and a function can be used in
ways that are not a resolvable call. So every candidate is sorted into one of
two answers:
* `dead` -- no call or inheritance edge, no call site of the same name left
  unresolved anywhere, its name not mentioned anywhere outside its own
  definition, not decorated, not an entry point, not exported beyond the unit,
  not an override;
* `unknown`, with the reason -- the resolver left a same-name call unplaced
  (dynamic dispatch, an untyped receiver), the name is mentioned as a value
  (a callback, a dispatch table, a decorator argument), a decorator may
  register it, a base class may call it, a TypeScript export may be imported
  from outside the unit.
A name the unique-name tier refuses to guess (`run`, `get`, the stoplists)
leaves its call sites unresolved, so a common method name is never `dead` on
the strength of a missing edge: it is `unknown` and says why (UNMET h).

arch. File communities precomputed at build (see arch.py), sized and ranked
here, with the cross-community coupling and the most-called symbols. An
update does not recompute them: the answer says how many updates it is behind.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from . import languages
from .query import page

_DUNDER = re.compile(r"^__\w+__$")
_CANDIDATE_KINDS = ("function", "method", "class", "getter", "setter")
_TYPELIKE = ("interface", "type", "enum")


# --- entry points ---------------------------------------------------------------

def _script_targets(root: Path) -> list[str]:
    """`module:function` targets of pyproject's scripts and entry points."""
    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[str] = []
    for m in re.finditer(r"^\[project\.(?:scripts|gui-scripts|entry-points\.[^\]]+)\]\s*$(.*?)(?=^\[|\Z)",
                         text, re.M | re.S):
        out += re.findall(r"^\s*[\w.\-]+\s*=\s*[\"']([\w.]+:[\w.]+)", m.group(1), re.M)
    return out


def _package_entry_files(root: Path, manifests: list[str]) -> list[str]:
    """Files a package.json names as what others run or import: main, module,
    bin, exports -- as unit-relative paths, extension-agnostic stems."""
    out: list[str] = []

    def leaves(v: Any) -> list[str]:
        if isinstance(v, str):
            return [v]
        if isinstance(v, dict):
            return [x for y in v.values() for x in leaves(y)]
        if isinstance(v, list):
            return [x for y in v for x in leaves(y)]
        return []
    for rel in manifests:
        if not rel.endswith("package.json"):
            continue
        try:
            data = json.loads((root / rel).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        base = rel.rsplit("/", 1)[0] if "/" in rel else ""
        for key in ("main", "module", "types", "bin", "exports"):
            for target in leaves(data.get(key)):
                path = "/".join(p for p in (base, target.lstrip("./")) if p)
                out.append(re.sub(r"\.(d\.ts|[cm]?[jt]sx?)$", "", path))
    return out


# Android (CODE-GRAPH D3): the framework starts what the manifest declares and
# inflates what a layout names; neither is a call the graph can see.
_MANIFEST_COMPONENT = re.compile(
    r"<(?:activity|activity-alias|service|receiver|provider|application|instrumentation)\b[^>]*?"
    r"android:name\s*=\s*\"([^\"]+)\"", re.S)
_RES_TAG = re.compile(r"<([A-Za-z_][\w]*(?:\.[A-Za-z_][\w$]*)+)[\s/>]")
_RES_ATTR = re.compile(r"(?:android:name|class|tools:context|app:layout_behavior|android:onClick|"
                       r"app:argType|android:targetActivity)\s*=\s*\"([^\"@{]+)\"")
# a Gradle script names classes the build hands to a tool: a test runner, a filter
_GRADLE_CLASS = re.compile(r"[\"']((?:[a-z_]\w*\.)+[A-Z]\w*)[\"']")
# a Java resource names classes a library loads by reflection: a `*.properties`
# setting, a ServiceLoader registration in META-INF/services
_RESOURCE_CLASS = re.compile(r"\b((?:[a-z_]\w*\.)+[A-Z][\w$]*)\b")


def _source_sets(con) -> list[str]:
    """`<module>/src/<set>` of every JVM file: where its manifest and res/ live."""
    out = set()
    for (path,) in con.execute("SELECT path FROM files WHERE lang = 'jvm'"):
        if languages.source_set(path) is not None:
            parts = path.split("/")
            out.add("/".join(parts[:parts.index("src") + 2]))
    return sorted(out)


def android_names(con, root: Path) -> tuple[set[str], set[str]]:
    """(dotted class names the manifests declare or the resources name,
    method names a layout's android:onClick names) -- read from disk at query
    time, never stored: they decide nothing a resolution depends on."""
    classes: set[str] = set()
    handlers: set[str] = set()
    sources = _source_sets(con)
    for module in sorted({s.rsplit("/src/", 1)[0] if "/src/" in s else "" for s in sources}):
        for script in ("build.gradle.kts", "build.gradle"):
            try:
                classes |= set(_GRADLE_CLASS.findall((root / module / script).read_text(
                    encoding="utf-8", errors="replace")))
            except OSError:
                continue
    for source in sources:
        base = root / source
        try:
            text = (base / "AndroidManifest.xml").read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        classes |= {m.lstrip(".").replace("$", ".") for m in _MANIFEST_COMPONENT.findall(text)}
        resources = base / "resources"
        if resources.is_dir():
            for f in resources.rglob("*"):
                if f.is_file() and (f.suffix == ".properties" or f.parent.name == "services"):
                    try:
                        classes |= {m.replace("$", ".") for m in _RESOURCE_CLASS.findall(
                            f.read_text(encoding="utf-8", errors="replace"))}
                    except OSError:
                        continue
        res = base / "res"
        if not res.is_dir():
            continue
        for xml in res.rglob("*.xml"):
            try:
                body = xml.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            classes |= {t.replace("$", ".") for t in _RES_TAG.findall(body)}
            for value in _RES_ATTR.findall(body):
                value = value.strip()
                if "." in value:
                    classes.add(value.lstrip(".").replace("$", "."))
                elif value.isidentifier():
                    handlers.add(value)
    return classes, handlers


def _jvm_entries(con, root: Path) -> tuple[set[str], set[str]]:
    """(JVM qnames something outside the graph runs, method names a layout
    names): the manifest's components and the classes resources name --
    matched by fully qualified name, or its tail when the manifest is
    relative to a namespace the graph does not read -- and every `main`."""
    if con.execute("SELECT 1 FROM files WHERE lang = 'jvm' LIMIT 1").fetchone() is None:
        return set(), set()
    names, handlers = android_names(con, root)
    types = languages.family("jvm").containers
    entries: set[str] = set()
    for qname, kind, signature, package in con.execute(
            "SELECT n.qname, n.kind, n.signature, f.package FROM nodes n JOIN files f ON f.id = n.file_id"
            f" WHERE f.lang = 'jvm' AND (n.kind IN ({','.join('?' * len(types))}) OR n.name = 'main')", types):
        local = qname.split("::", 1)[1]
        if local == "main" or (local.endswith(".main") and "static" in signature):
            entries.add(qname)                       # Kotlin's top-level main, Java's static main
            continue
        if kind not in types:
            continue
        fqn = f"{package}.{local}" if package else local
        if any(fqn == n or fqn.endswith("." + n) for n in names):
            entries.add(qname)
    return entries, handlers


def entry_points(con, root: Path, meta: dict[str, Any], jvm: set[str] | None = None) -> set[str]:
    """Qnames something outside the graph runs: pyproject scripts and entry
    points, every definition in a file a package.json exposes, and (JVM) the
    Android components and resources' classes and every `main` -- `jvm` when
    the caller has them already."""
    paths = {r[0] for r in con.execute("SELECT path FROM files")}
    entries: set[str] = set(_jvm_entries(con, root)[0] if jvm is None else jvm)
    for target in _script_targets(root):
        module, _, func = target.partition(":")
        stem = module.replace(".", "/")
        for cand in (f"{stem}.py", f"{stem}/__init__.py", f"src/{stem}.py", f"src/{stem}/__init__.py"):
            if cand in paths:
                entries.add(f"{cand}::{func}")
    stems = set(_package_entry_files(root, list((meta.get("watched") or {}).keys())))
    if stems:
        exposed = [p for p in paths if re.sub(r"\.(d\.ts|[cm]?[jt]sx?)$", "", p) in stems]
        for chunk in (exposed[i:i + 500] for i in range(0, len(exposed), 500)):
            entries |= {r[0] for r in con.execute(
                "SELECT n.qname FROM nodes n JOIN files f ON f.id = n.file_id WHERE n.exported = 1 AND f.path IN "
                f"({','.join('?' * len(chunk))})", chunk)}
    return entries


# --- deadcode ---------------------------------------------------------------------

def deadcode(con, root: Path, meta: dict[str, Any], limit: int = 50, offset: int = 0,
             show_unknown: bool = False) -> dict[str, Any]:
    jvm_entries, handlers = _jvm_entries(con, root)
    entries = entry_points(con, root, meta, jvm=jvm_entries)
    # a JVM class whose constructor is annotated (`@Inject`) is built by a framework
    built_by_framework = {parent for parent, decorators in con.execute(
        "SELECT n.parent, n.decorators FROM nodes n JOIN files f ON f.id = n.file_id"
        " WHERE f.lang = 'jvm' AND n.name = 'constructor'") if json.loads(decorators)}
    kinds = {r[0]: r[1] for r in con.execute("SELECT qname, kind FROM nodes WHERE kind IN ('class', 'interface',"
                                             " 'type', 'enum', 'object')")}
    unresolved = Counter((lang, name) for lang, name in con.execute(
        "SELECT f.lang, u.name FROM unresolved u JOIN files f ON f.id = u.file_id"))
    mentioned = {(lang, name): n for lang, name, n in con.execute("SELECT lang, name, files FROM name_refs")}
    inherits: dict[str, list[str]] = defaultdict(list)
    for src, dst in con.execute("SELECT src, dst FROM edges WHERE kind = 'INHERITS'"):
        inherits[src].append(dst)
    members: dict[str, set[str]] = defaultdict(set)
    rows = con.execute(
        "SELECT n.qname, n.name, n.kind, n.parent, n.decorators, n.exported, n.bases, n.spans, f.path, f.lang,"
        " f.is_test FROM nodes n JOIN files f ON f.id = n.file_id WHERE n.kind IN"
        f" ({','.join('?' * len(_CANDIDATE_KINDS))}) AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.dst = n.qname"
        " AND e.kind IN ('CALLS', 'INHERITS'))", _CANDIDATE_KINDS).fetchall()
    for name, parent in con.execute("SELECT name, parent FROM nodes WHERE parent IS NOT NULL"):
        members[parent].add(name)
    class_bases = {q: json.loads(b) for q, b in con.execute(
        "SELECT qname, bases FROM nodes WHERE kind IN ('class', 'object', 'enum')")}

    def overrides(klass: str, name: str, bases: list[str]) -> str | None:
        seen, todo = set(), list(inherits.get(klass, []))
        while todo:
            b = todo.pop()
            if b in seen:
                continue
            seen.add(b)
            if name in members.get(b, ()):
                return f"overrides {b.split('::', 1)[1]}.{name}"
            todo += inherits.get(b, [])
        if len(bases) > len(inherits.get(klass, [])):
            return "its class extends a base outside the graph, which may call it"
        return None

    dead: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    for q, name, kind, parent, decorators, exported, _bases, spans, path, lang, is_test in rows:
        parent_kind = kinds.get(parent or "")
        if parent_kind in _TYPELIKE:
            continue                               # a signature in an interface: nothing to be dead
        if (_DUNDER.match(name) or q in entries or name == "constructor"
                or (is_test and (name.startswith("test") or (parent or "").split(".")[-1].startswith("Test")))
                or (is_test and lang == "jvm" and kind == "class" and parent is None)):
            continue                               # alive by declaration (a JVM test class: its runner builds it)
        why: tuple[str, str] | None = None             # (reason key, what to tell the reader)
        if kind in ("getter", "setter"):
            why = ("accessor", "an accessor is read or assigned, never called")
        elif json.loads(decorators):
            why = ("decorated", "a decorator may register or wrap it")
        elif q in built_by_framework:
            why = ("decorated", "its constructor is annotated: a framework may build it")
        elif lang == "jvm" and name in handlers:
            why = ("android resource", "a layout names it (android:onClick)")
        elif unresolved[(lang, name)]:
            why = ("unresolved same-name calls",
                   f"{unresolved[(lang, name)]} call site(s) of this name the resolver could not place")
        elif mentioned.get((lang, name)):
            why = ("name mentioned", f"its name is mentioned in {mentioned[(lang, name)]} file(s): "
                                     "a callback, a table, a re-export")
        elif exported and languages.family(lang).exports_leave_unit:
            why = ("exported", "code outside this unit may import it")
        elif kind == "method" and parent_kind in ("class", "object", "enum"):
            text = overrides(parent, name, class_bases.get(parent, []))
            why = ("override", text) if text else None
        item = {"qname": q, "kind": kind, "file": path, "line": json.loads(spans)[0][0]}
        if why:
            reasons[why[0]] += 1
            unknown.append({**item, "why": why[1]})
        else:
            dead.append(item)
    dead.sort(key=lambda d: (d["file"], d["line"]))
    unknown.sort(key=lambda d: (d["file"], d["line"]))
    shown = dead[offset:offset + limit]
    _mark_unused_imports(con, shown)
    out: dict[str, Any] = {
        "dead": page(shown, len(dead), offset, limit),
        "unknown": {"total": len(unknown), "by_reason": dict(reasons.most_common())},
        "entry_points": len(entries), "candidates": len(rows),
        "note": "`dead`: nothing calls, inherits, names or registers it and nothing outside the graph is "
                "declared to run it. `imported_by` lists files that import it without using it: remove "
                "those imports with it. A candidate, not a verdict -- reflection, string-built names, code "
                "in other file types (a Python block inside a Markdown skill) and code outside this unit "
                "are invisible to a static graph."}
    if show_unknown:
        out["unknown"]["items"] = page(unknown[offset:offset + limit], len(unknown), offset, limit)
    return out


def _mark_unused_imports(con, items: list[dict[str, Any]]) -> None:
    """An import is not a use -- a function imported and never called is still
    dead -- but deleting it alone breaks the import (the P4 audit found two
    such). Name the importing files on each dead item. Only for the page
    shown: it reads the importers' stored facts."""
    from .model import decode_facts
    for item in items:
        path, _, rest = item["qname"].partition("::")
        if "." in rest:                              # a method: imported through its class, not by name
            continue
        importers = [r[0] for r in con.execute(
            "SELECT DISTINCT f.id FROM edges e JOIN files f ON f.id = e.file_id"
            " WHERE e.kind = 'IMPORTS' AND e.dst = ?", (f"{path}::<module>",))]
        found = []
        for fid in importers:
            row = con.execute("SELECT f.path, x.blob FROM facts x JOIN files f ON f.id = x.file_id"
                              " WHERE x.file_id = ?", (fid,)).fetchone()
            if row and any(b.attr == rest for b in decode_facts(row[1]).imports):
                found.append(row[0])
        if found:
            item["imported_by"] = sorted(found)


# --- arch -------------------------------------------------------------------------

def arch_view(con, meta: dict[str, Any], top: int = 10) -> dict[str, Any]:
    info = meta.get("arch") or {}
    labels = dict(con.execute("SELECT community, label FROM community_labels").fetchall())
    sizes = dict(con.execute("SELECT community, count(*) FROM communities GROUP BY community").fetchall())
    files_total = con.execute("SELECT count(*) FROM files").fetchone()[0]
    in_community = sum(sizes.values())
    # every edge between two files, folded to (file community, file community)
    file_comm = dict(con.execute("SELECT f.path, c.community FROM communities c JOIN files f ON f.id = c.file_id"))
    coupling: Counter[tuple[int, int]] = Counter()
    fan_in: Counter[str] = Counter()
    for src_path, dst in con.execute(
            "SELECT f.path, e.dst FROM edges e JOIN files f ON f.id = e.file_id WHERE e.kind IN ('CALLS', 'INHERITS')"):
        dst_path = dst.split("::", 1)[0]
        if dst_path == src_path:
            continue
        fan_in[dst_path] += 1
        a, b = file_comm.get(src_path), file_comm.get(dst_path)
        if a is not None and b is not None and a != b:
            coupling[(a, b) if a < b else (b, a)] += 1
    by_comm: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for path, c in file_comm.items():
        by_comm[c].append((fan_in.get(path, 0), path))
    comms = [{"community": c, "label": labels.get(c, "?"), "files": n,
              "core_files": [p for _, p in sorted(by_comm[c], key=lambda x: (-x[0], x[1]))[:5]]}
             for c, n in sorted(sizes.items(), key=lambda x: (-x[1], x[0]))]
    hot = [{"symbol": q, "callers": n} for q, n in con.execute(
        "SELECT dst, count(*) AS n FROM edges WHERE kind = 'CALLS' GROUP BY dst ORDER BY n DESC, dst LIMIT ?",
        (top,))]
    behind = int(meta.get("seq", 0)) - int(info.get("computed_at_seq", 0))
    return {
        "communities": page(comms[:top], len(comms), 0, top),
        "isolated_files": files_total - in_community,
        "coupling": [{"between": [labels.get(a, str(a)), labels.get(b, str(b))], "communities": [a, b], "edges": n}
                     for (a, b), n in coupling.most_common(top)],
        "hotspot_files": [{"file": p, "callers_from_other_files": n} for p, n in fan_in.most_common(top)],
        "hotspot_symbols": hot,
        "computed": {"at": meta.get("built_at"), "seed": info.get("seed"), "updates_since": behind,
                     "note": info.get("note") or (
                         f"communities are computed at the full build; {behind} in-place update(s) since, "
                         "so files added since are not in any community" if behind else None)}}
