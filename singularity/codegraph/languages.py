"""The languages the code graph reads, in one table (CODE-GRAPH D3).

A FAMILY is what resolves together, and what `files.lang` records: `python`;
`ts` for TypeScript and JavaScript; `jvm` for Java and Kotlin, which share one
namespace of fully qualified names and call each other inside one module.
Every module that used to branch "python, or else ts" asks this table
instead, so a family is added here plus its own extractor and resolver, not
in ten places.

Parser-free and import-cheap: the query path reads it. An extractor is named
by its module and imported only where a file is parsed; a resolver likewise.
The kinds below are literal data rather than imports of the resolvers'
constants (the query path must not load a resolver to classify a row);
`tests/test_codegraph_languages.py` holds each copy equal to its resolver's.
"""
from __future__ import annotations

import importlib
import os
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Family:
    lang: str
    extractors: tuple[tuple[str, str], ...]   # (suffix, module of singularity.codegraph that parses it)
    containers: tuple[str, ...]               # kinds a definition inside is not local (tables.is_local)
    never_guessed: tuple[str, ...]            # kinds the unique-name tier never targets
    resolver: str                             # "module:Class" in singularity.codegraph
    workspace: bool = False                   # the resolver maps package specifiers (package.json, tsconfig)
    exports_leave_unit: bool = False          # an `exported` definition may be imported from outside the unit
    chunk_containers: tuple[str, ...] = ("class",)   # chunker v2: kinds whose members get chunks of their own
    packages: bool = False                    # names resolve through `files.package`, not the file's path


# Order is the order a build resolves the families in and writes their rows:
# Python first, as before this table existed (Louvain numbers communities by
# the order it meets nodes, and a number is what people refer to).
FAMILIES: tuple[Family, ...] = (
    Family("python", ((".py", "extract_py"),), containers=("class",), never_guessed=(),
           resolver="resolve_py:PyResolver"),
    Family("ts", tuple((s, "extract_ts") for s in (".ts", ".tsx", ".js", ".jsx")),
           containers=("class", "interface", "type", "enum"),
           never_guessed=("interface", "type", "enum", "getter", "setter"),
           resolver="resolve_ts:TsResolver", workspace=True, exports_leave_unit=True),
    Family("jvm", ((".java", "extract_java"), (".kt", "extract_kotlin")),
           containers=("class", "interface", "enum", "object"),
           never_guessed=("class", "interface", "enum", "object"),
           resolver="resolve_jvm:JvmResolver", packages=True,
           chunk_containers=("class", "interface", "enum", "object")),
)
LANGS: tuple[str, ...] = tuple(f.lang for f in FAMILIES)
_BY_LANG = {f.lang: f for f in FAMILIES}
_BY_SUFFIX = {suffix: (f, module) for f in FAMILIES for suffix, module in f.extractors}
SUFFIXES: frozenset[str] = frozenset(_BY_SUFFIX)


def family(lang: str) -> Family:
    return _BY_LANG[lang]


# Pseudo-paths a JVM resolution reads (see tables), and a file's presence or
# content answers: `jvm-package:<p>` -- whether and which files declare
# package p -- and `jvm-name:<p>:<n>` -- which top-level definitions (or
# Kotlin facades) package p calls n. A resolver asks the second for every
# name it looks up in a package, so an edit re-resolves only the readers of
# a name that entered or left the package, never every reader of it (see
# update); the first only for "is this a first-party package at all".
PACKAGE_DEP = "jvm-package:"
NAME_DEP = "jvm-name:"


def membership(lang: str, package: str | None) -> set[str]:
    """The pseudo-path a file's presence in its package answers for, in a
    family that resolves through packages; nothing otherwise."""
    return {PACKAGE_DEP + (package or "")} if family(lang).packages else set()


def name_key(package: str | None, name: str) -> str:
    return f"{NAME_DEP}{package or ''}:{name}"


def name_keys(lang: str, package: str | None, names) -> set[str]:
    """The pseudo-paths of `names` in the package, in a family that resolves
    through packages; nothing otherwise."""
    return {name_key(package, n) for n in names} if family(lang).packages else set()


def facade(facts: Any) -> str | None:
    """The class Java calls a Kotlin file's top level through (extract_kotlin
    records it as an export from module ''), or None."""
    if not family(facts.lang).packages:
        return None
    return next((e.local for e in facts.exports if not e.module and e.attr is None), None)


def top_names(facts: Any) -> set[str]:
    """What a JVM file answers for in its package: the names of its top-level
    definitions, and its facade."""
    names = {d.name for d in facts.definitions if d.parent is None}
    fac = facade(facts)
    return names | {fac} if fac else names


# Gradle's source sets: `src/<set>/java|kotlin/...`. A test set is `test`,
# `androidTest`, `testFixtures` or `sharedTest`, alone or with a flavor or
# build type after it (`androidTestGplay`, `testDebug`).
_TEST_SET = re.compile(r"(?:androidTest|testFixtures|sharedTest|test)(?:[A-Z]\w*)?")


def source_set(path: str) -> str | None:
    """`app/src/gplay/java/...` -> `gplay`; None outside a `src/<set>/` layout."""
    parts = path.split("/")
    for i, part in enumerate(parts[:-2]):
        if part == "src":
            return parts[i + 1]
    return None


def is_test_set(source: str) -> bool:
    return bool(_TEST_SET.fullmatch(source))


def _suffix(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def lang_of(path: str) -> str | None:
    """The family of a file by its suffix, None for a file no family reads."""
    hit = _BY_SUFFIX.get(_suffix(path))
    return hit[0].lang if hit else None


def extract(path: str, data: bytes) -> tuple[str, Any]:
    """(lang, FileFacts) of one file. Its extractor -- and so the parser -- is
    imported here, on first use. A suffix no family reads is a KeyError: the
    universe (scope) never hands one over."""
    fam, module = _BY_SUFFIX[_suffix(path)]
    return fam.lang, importlib.import_module(f".{module}", __package__).extract(path, data)


def load_extractors() -> None:
    """Import every extractor, which builds every grammar's `Language` -- the
    point where a missing wheel or an ABI the core refuses fails."""
    for module in sorted({m for _f, m in _BY_SUFFIX.values()}):
        importlib.import_module(f".{module}", __package__)


def resolver(lang: str, *, facts: dict | None = None, test_paths: Any = frozenset(),
             source: Any = None, workspace: tuple[list, list] | None = None) -> Any:
    """The family's resolver: over `facts` in memory (a full build) or over a
    `source` reading the stored graph (an update). `workspace` is
    (packages, aliases), handed only to a family that maps package specifiers."""
    fam = family(lang)
    kwargs: dict[str, Any] = {"source": source} if source is not None else {"facts": facts,
                                                                              "test_paths": test_paths}
    if fam.workspace and workspace is not None:
        kwargs["packages"], kwargs["aliases"] = workspace
    module, _, cls = fam.resolver.partition(":")
    return getattr(importlib.import_module(f".{module}", __package__), cls)(**kwargs)
