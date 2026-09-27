"""Secret audit of a built code graph (CODE-GRAPH D1; CODE-INDEX D5, layer 3).

D5's first two layers -- the extension allowlist and the `*secret*` /
`*credential*` path filters, both still applied by scope -- are hypotheses
about where secrets live. This is the layer that checks the fact: it reads the
graph that was actually written. `graph.db` is a file in `.mb/` that anything
can read without the parser or the model, so a key hard-coded in a repository
is at rest there for as long as the graph lives.

What is scanned, and why all of it
----------------------------------
The decision names the node columns a secret reaches most directly:
`signature` (a default value, `def connect(token="...")`), `decorators` (a
decorator's arguments as written) and `doc1` (a docstring's first line).
`bases` is the fourth column holding source text as written (`class
C(make_base(key="..."))`) and is scanned with them. `decorators` and `bases`
are JSON lists in the table and are decoded first: JSON escaping turns
`token="..."` into `token=\\"...\\"`, which the assignment rule does not match.

The stored facts (one zlib+JSON blob per file, model.encode_facts) are
scanned too. They sit in the same file, readable with `zlib` and `json`, and
hold source text the node columns do not:
* a call's receiver: the whole object expression as written, unbounded --
  `Session(headers={"Authorization": "Bearer ..."}).get(url)`;
* type hints: annotations, constructors, aliased names, as written;
* import and export specifiers and the names they bind;
* a definition's declared return type;
* `refs` (FileFacts.refs): every name the file mentions AND every string
  literal shaped like a name. `AKIA...` and `ghp_...` are name-shaped, so a
  key hard-coded anywhere in a file, not only in a signature, lands here.
The blob's definitions repeat the node columns (rows.node_rows writes both
from the same FileFacts), so they are read from the table only: the copy that
queries serve, one finding per value. Every other text column is derived from
the scanned ones or is a path: `unresolved.receiver` is a call's receiver,
`name_refs` counts `refs`, `lookups` and `edges.callee` are identifiers the
refs hold, `dep_paths` are module paths probed from import specifiers.

Detector
--------
The engine's one detector, knowledge.redact -- the shapes `mb learn` masks
before it writes -- with its honest boundary: known shapes, protection against
accident, not against an attacker. One addition, forced by the graph rather
than by a new idea of what a secret is: signature and doc1 are cut at 200
characters (extract_py._SIG_MAX), so a PEM key hard-coded as a default arrives
with its BEGIN line and without its END line, and redact's private-key rule,
which needs both, cannot fire. A BEGIN ... PRIVATE KEY line followed by key
material that redact left unmasked is reported as `private-key-header` -- the
marker tools/code_index_audit.py greps the code index for.

A finding carries the qname, the field, the detector kind and an excerpt of
the MASKED text. It never carries the value: a report that prints the secret
it found has moved the leak, not closed it (redact.py).

Cost, and the batch pre-check
-----------------------------
Measured on the 44k-file proxy graph (984 MB, 580k nodes, 8M fact texts):
one redact call per text took 9 s for the nodes and 32 s for the facts, on
top of 9 s of JSON decoding. Two things bring the whole audit to 24 s. A text
already found clean is not checked again (_Scanner.clean). The rest are
checked in batches joined by `_SEP`, and only a batch with a finding is
scanned text by text for the report. The batch is sound -- a clean batch has
no text with a finding -- because
no match of a redact rule can run from one text into the next across `_SEP`,
a NUL between two newlines. Every rule's character classes exclude the
newline; the only parts that take one are the `\\s` runs of the bearer and
assignment rules, and each must be followed by what the NUL is not (a token
character, `:` or `=`) or, in the assignment's value, by twelve characters
that are not whitespace, which the second newline cuts short. The private-key
block rule crosses anything, but a match of it is a finding: the batch is
dirty and its texts are scanned one by one. So a batch with no finding holds,
in each text's stretch, exactly the matches that text has alone, judged alike:
redact reads the teaching-line exemption by line number, the batch keeps each
text's lines intact, and a text holding a line break other than `\\n` (redact
counts `\\n`, str.splitlines counts more, and the two would disagree) is never
batched. tests/test_codegraph_audit.py pins the equivalence.

Findings the owner accepted
---------------------------
The audit knows shapes, not intent: a redactor's own tests hold fake keys of
every shape it masks, and they are reported like real ones. The owner accepts
them one finding at a time, never by file or path -- a real key added to the
same test file must still be reported. Each finding carries a `fingerprint`,
sha256 of its definition, field and exact text, cut to 16 hex; the ones listed
in `code_graph_audit_allow` are counted as `allowed`, not as findings, and
doctor K2 is quiet about them. Any change to the text (another key beside the
fake one, a new value) or to where it stands makes a new fingerprint, and the
finding is back. The fingerprint is of text that looked like a secret; the
config keeps it off the shared example (config._EXAMPLE_REDACTED_KEYS).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import zlib
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..knowledge.redact import MASK, redact
from . import store
from .scope import GraphError

RECORD_KEY = "audit"             # the meta row doctor K2 reads
_SEP = "\n\x00\n"
_BATCH_CHARS = 4096              # redact's per-finding work grows with the text, so batches stay small
_CONTEXT = 40                    # characters of masked context either side of the first mask
_RECORD_WAIT_SECONDS = 5.0
# Line breaks str.splitlines honours besides "\n": a text holding one is scanned alone (module doc).
_OTHER_BREAKS = frozenset("\r\x0b\x0c\x1c\x1d\x1e\x85") | {chr(0x2028), chr(0x2029)}
_CACHED_CHARS = 256              # clean texts up to this long are remembered: see _Scanner.clean
# A BEGIN line redact did not mask (its block rule leaves BEGIN...MASK...END), followed by key
# material, possibly after a written "\n": what a 200-character signature keeps of a key.
_KEY_HEADER = re.compile(r"(-----BEGIN [A-Z ]*?PRIVATE KEY-----)(?:\s|\\[nr])*[A-Za-z0-9+/]{16,}.*", re.DOTALL)


@dataclass(frozen=True)
class Finding:
    qname: str      # the definition, or `<path>::<module>` for what belongs to the file
    field: str      # nodes.<column> or facts.<part>
    kind: str       # the detector rule: redact's kind, or private-key-header
    excerpt: str    # the text around the first mask, every secret in it masked
    fingerprint: str  # of this definition, field and exact text: what `code_graph_audit_allow` lists


def fingerprint(qname: str, fld: str, text: str) -> str:
    """One finding's identity for the owner's allowlist (see the module doc)."""
    return hashlib.sha256("\x00".join((qname, fld, text)).encode("utf-8")).hexdigest()[:16]


def _check(text: str) -> tuple[list[str], str]:
    """(detector kinds found in `text`, `text` with each of them masked)."""
    r = redact(text)
    kinds = [f.kind for f in r.findings]
    masked = r.text
    if _KEY_HEADER.search(masked):
        kinds.append("private-key-header")
        masked = _KEY_HEADER.sub(lambda m: m.group(1) + MASK, masked)
    return kinds, masked


def _excerpt(masked: str) -> str:
    at = masked.find(MASK)
    if at < 0:
        return MASK
    start, end = max(0, at - _CONTEXT), min(len(masked), at + len(MASK) + _CONTEXT)
    cut = ("…" if start else "") + " ".join(masked[start:end].split()) + ("…" if end < len(masked) else "")
    # The cut is checked once more: cutting must not leave anything redact would still mask.
    if redact(cut).text != cut or _KEY_HEADER.search(cut):
        return MASK
    return cut


def _dirty(texts: list[str]) -> bool:
    joined = _SEP.join(texts)
    return bool(redact(joined).findings) or _KEY_HEADER.search(joined) is not None


class _Scanner:
    """Collects (qname, field, text) items into batches and keeps the findings.

    `clean` remembers the texts already found clean. A text's verdict depends
    on the text alone, so one seen clean in any file is clean everywhere; a
    text with a finding is never remembered and is reported wherever it
    occurs. On the proxy graph 89 MB of fact texts held 19 MB of distinct
    ones (`self`, `os.path`, `str` in every file): the facts scan went from
    27 s to 5 s. Only short texts are kept -- the long ones rarely repeat."""

    def __init__(self) -> None:
        self.findings: list[Finding] = []
        self.clean: set[str] = set()
        self._batch: list[tuple[str, str, str]] = []
        self._size = 0

    def add(self, qname: str, fld: str, text: str | None) -> None:
        if not text or text in self.clean:
            return
        if not _OTHER_BREAKS.isdisjoint(text):
            self._one(qname, fld, text)
            return
        self._batch.append((qname, fld, text))
        self._size += len(text)
        if self._size >= _BATCH_CHARS:
            self.flush()

    def flush(self) -> None:
        batch, self._batch, self._size = self._batch, [], 0
        if batch and _dirty([t for _, _, t in batch]):
            for item in batch:
                self._one(*item)
        else:
            self.clean.update(t for _, _, t in batch if len(t) <= _CACHED_CHARS)

    def _one(self, qname: str, fld: str, text: str) -> None:
        kinds, masked = _check(text)
        if not kinds:
            if len(text) <= _CACHED_CHARS:
                self.clean.add(text)
            return
        excerpt, fp = _excerpt(masked), fingerprint(qname, fld, text)
        self.findings.extend(Finding(qname, fld, k, excerpt, fp) for k in dict.fromkeys(kinds))


def scan_items(items: Iterable[tuple[str, str, str]]) -> list[Finding]:
    """Findings among (qname, field, text) items (see the module doc)."""
    s = _Scanner()
    for item in items:
        s.add(*item)
    s.flush()
    return s.findings


def _scan_nodes(con: sqlite3.Connection, s: _Scanner) -> None:
    for qname, signature, doc1, decorators, bases in con.execute(
            "SELECT qname, signature, doc1, decorators, bases FROM nodes"):
        s.add(qname, "nodes.signature", signature)
        s.add(qname, "nodes.doc1", doc1)
        for column, raw in (("nodes.decorators", decorators), ("nodes.bases", bases)):
            if not raw or raw == "[]":
                continue
            try:
                values = json.loads(raw)
            except ValueError:
                values = [raw]              # not the JSON list this schema writes: scan it as it is
            for v in values if isinstance(values, list) else [values]:
                s.add(qname, column, v if isinstance(v, str) else json.dumps(v))


def _scan_facts(path: str, blob: bytes, s: _Scanner) -> None:
    """Every source text of one stored FileFacts outside its definitions
    (model.encode_facts: path, lang, degraded, definitions, imports, calls,
    hints, exports, refs)."""
    _path, _lang, _degraded, defs, imports, calls, hints, exports, refs = json.loads(zlib.decompress(blob))
    for d in defs:
        s.add(d[0], "facts.returns", d[10])
    for part, bindings in (("facts.imports", imports), ("facts.exports", exports)):
        for scope, local, spec, _level, attr in bindings:
            s.add(scope, part, spec)
            s.add(scope, part, local)
            s.add(scope, part, attr)
    for caller, _line, _callee, receiver, _klass in calls:
        s.add(caller, "facts.calls.receiver", receiver)
    for scope, _target, type_expr, _source, _origin in hints:
        s.add(scope, "facts.hints", type_expr)
    module, clean = f"{path}::<module>", s.clean
    for name in refs:
        if name not in clean:               # most of the 7M refs of a large graph: skip the call
            s.add(module, "facts.refs", name)


def scan(con: sqlite3.Connection, allowed: Iterable[str] = ()) -> dict[str, Any]:
    """The audit of the graph open on `con`; reads only. A graph of another
    schema is refused, not half read: its columns and blob layout differ.
    Findings whose fingerprint is in `allowed` are reported apart, as
    `allowed`, and are not counted."""
    started = time.perf_counter()
    meta = store.read_meta(con)
    if meta.get("schema_version") != store.SCHEMA_VERSION:
        raise GraphError(f"the graph has schema {meta.get('schema_version')} and this engine "
                         f"{store.SCHEMA_VERSION}: rebuild it with `mb code build`, which audits it")
    s = _Scanner()
    _scan_nodes(con, s)
    for path, blob in con.execute("SELECT f.path, fa.blob FROM facts fa LEFT JOIN files f ON f.id = fa.file_id"):
        try:
            _scan_facts(path or "?", blob, s)
        except (zlib.error, ValueError, TypeError) as exc:
            # a part the audit cannot read is a part it did not check: never a clean result
            raise GraphError(f"the stored facts of {path} cannot be read ({exc}): rebuild the graph") from exc
    s.flush()
    accepted = frozenset(allowed)
    findings = [f for f in s.findings if f.fingerprint not in accepted]
    kept = [f for f in s.findings if f.fingerprint in accepted]
    return {"count": len(findings), "by_kind": dict(sorted(Counter(f.kind for f in findings).items())),
            "findings": [asdict(f) for f in findings],
            "allowed": len(kept), "allowed_findings": [asdict(f) for f in kept],
            "files": con.execute("SELECT count(*) FROM files").fetchone()[0],
            "nodes": con.execute("SELECT count(*) FROM nodes").fetchone()[0],
            "seq": meta.get("seq", 0), "seconds": round(time.perf_counter() - started, 2)}


def record(db: Path, report: dict[str, Any]) -> bool:
    """Keep the count in the graph's meta, where doctor K2 reads it without
    scanning again: a 44k-file graph takes half a minute to audit. The counts
    only, never a finding. `seq` is not moved -- nothing the queries read
    changed. False when another writer held the graph too long."""
    row = {"findings": report["count"], "by_kind": report["by_kind"], "at_seq": report["seq"],
           "allowed": report.get("allowed", 0), "at": time.time()}
    try:
        with store.write_transaction(db, wait=_RECORD_WAIT_SECONDS) as con:
            con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)", (RECORD_KEY, json.dumps(row)))
    except store.GraphLocked:
        return False
    return True


def audit_db(db: Path, *, keep: bool = True, allowed: Iterable[str] = ()) -> dict[str, Any]:
    """Scan the graph in `db`; with `keep`, record the result in its meta.
    One read transaction: an update applied meanwhile is not half seen, and
    the `seq` recorded is the state that was scanned. `allowed`: the owner's
    accepted fingerprints (`code_graph_audit_allow`)."""
    con = store.connect_ro(db)
    try:
        con.execute("BEGIN")
        report = scan(con, allowed)
    finally:
        con.close()
    report["recorded"] = record(db, report) if keep else False
    return report


def recorded(meta: dict[str, Any]) -> dict[str, Any] | None:
    """The last audit's counts, as `record` kept them, or None if never audited."""
    row = meta.get(RECORD_KEY)
    return row if isinstance(row, dict) and isinstance(row.get("findings"), int) else None
