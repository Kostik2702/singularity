"""Chunker v2: definition boundaries from the graph's extractors (CODE-GRAPH P7).

Three properties carry the design, each pinned here by a test that was seen
failing on a planted defect:

* a unit is a definition at module level or directly in a class -- a closure
  stays in its function, an interface member in its interface;
* no text is lost: every non-blank line of a file lands in a chunk, a long
  unit is windowed instead of cut at MAX_BODY_LINES, and the module header
  (imports, docstring, constants) that v1 never emitted is chunked the v1
  fallback way;
* a chunk's (line_start, line_end) reproduces its content from disk, with
  lines counted the way the parser counts them.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from singularity.search import code_chunking as v1
from singularity.search import code_chunking_v2 as v2
from singularity.search.engine import SearchEngine

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "code"

LONG_PY = ("def enormous():\n"
           + "".join(f"    value_{i} = {i}\n" for i in range(200))
           + "    return 'UniqueTailMarker42'\n")

NESTED_PY = '''"""Module docstring v1 never indexed: the file's purpose in one line."""
import os
import sys

TIMEOUT = 30


def outer(items):
    """Outer does the work."""
    def helper(x):
        return x * 2

    class Local:
        pass

    return [helper(i) for i in items]


class Store(Base):
    def put(self, key, value):
        self._data[key] = value

    limit = 5
'''

DECORATED_PY = '''import pytest


@pytest.mark.parametrize("name,expected", [
    ("a", 1),
    ("b", 2),
])
def test_values(name, expected):
    assert len(name) == expected - 0


class Model:
    @property
    def size(self):
        return len(self.items)

    @size.setter
    def size(self, value):
        self.items = [None] * value
'''

SERVICE_TS = """import { Injectable } from '@nestjs/common';

@Injectable()
export class OrderService {
  @Get('/orders')
  list(limit: number): Order[] {
    return this.repo.find({ take: limit });
  }
  get count(): number { return this.repo.size(); }
}

export interface Order {
  id: string;
  total(): number;
  onChange: (v: number) => void;
}

export const loadOrders = async (ids: string[]) => {
  return Promise.all(ids.map(fetchOrder));
};
"""


def _write(tmp_path: Path, name: str, text: str | bytes) -> Path:
    path = tmp_path / name
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8", newline="")
    return path


def _chunks(path: Path, rel: str | None = None):
    return v2.extract_code_chunks(path, rel or f"demo/{path.name}")


def _by_qname(chunks):
    return {c["qname"].split("::", 1)[1]: c for c in chunks if "qname" in c}


def _lines(path: Path) -> list[str]:
    return v2._split_lines(path.read_bytes().decode("utf-8", errors="ignore"))


SOURCES = {"long.py": LONG_PY, "nested.py": NESTED_PY, "decorated.py": DECORATED_PY,
           "service.ts": SERVICE_TS}


def _all_cases(tmp_path: Path) -> list[Path]:
    paths = [FIXTURES / n for n in ("sample_service.py", "sample_widget.ts", "sample_config.js")]
    paths += [_write(tmp_path, n, t) for n, t in SOURCES.items()]
    # CRLF on disk (how a Windows checkout stores every fixture) and a form feed,
    # which `str.splitlines` counts as a line break and the parser does not
    paths.append(_write(tmp_path, "crlf.py", NESTED_PY.replace("\n", "\r\n").encode("utf-8")))
    paths.append(_write(tmp_path, "formfeed.py",
                        "import os\n\x0c\ndef after_form_feed(arg):\n    return os.path.join(arg, 'x')\n"))
    return paths


# --- units -------------------------------------------------------------------

def test_python_units_are_the_extractors_definitions():
    by_q = _by_qname(_chunks(FIXTURES / "sample_service.py"))
    assert {q: c["kind"] for q, c in by_q.items()} == {
        "retry_with_backoff": "function", "fetch_all": "function", "SessionStore": "class",
        "SessionStore.size": "method", "SessionStore.key_for": "method",
        "SessionStore.evict_expired": "method"}
    assert by_q["SessionStore.size"]["qname"] == "demo/sample_service.py::SessionStore.size"
    assert by_q["SessionStore.size"]["name"] == "size"        # what the result line prints


def test_the_embedded_head_names_a_method_by_its_class():
    chunk = _by_qname(_chunks(FIXTURES / "sample_service.py"))["SessionStore.size"]
    assert chunk["text"].startswith("demo/sample_service.py — method SessionStore.size: "
                                    "Number of live sessions. | ")
    assert len(chunk["text"].split(" | ", 1)[1]) <= v1.EMBED_CODE_CHARS


def test_descriptions_are_v1s_so_only_the_boundaries_differ():
    """The eval of v2 against v1 must measure boundaries, not a new doc format."""
    old = {c["name"]: c.get("description") for c in v1.extract_code_chunks(
        FIXTURES / "sample_service.py", "demo/sample_service.py")}
    new = {c["name"]: c.get("description") for c in _chunks(FIXTURES / "sample_service.py")
           if "qname" in c}
    assert new == old


def test_decorators_open_the_chunk_including_a_multi_line_one(tmp_path):
    by_q = _by_qname(_chunks(_write(tmp_path, "decorated.py", DECORATED_PY)))
    # v1's rule (contiguous `@` lines) stops at `])`, the line right above the
    # def, and would leave the whole decorator to the module remainder
    lines = by_q["test_values"]["content"].splitlines()
    top = lines.index('@pytest.mark.parametrize("name,expected", [')
    assert lines[top:top + 5] == ['@pytest.mark.parametrize("name,expected", [',
                                  '    ("a", 1),', '    ("b", 2),', '])',
                                  'def test_values(name, expected):']
    size = [ln.strip() for ln in by_q["Model.size"]["content"].splitlines()]
    assert size[size.index("def size(self):") - 1] == "@property"
    assert "@size.setter" in size and "self.items = [None] * value" in size   # both spans


def test_a_closure_and_a_local_class_stay_inside_their_function(tmp_path):
    by_q = _by_qname(_chunks(_write(tmp_path, "nested.py", NESTED_PY)))
    assert "outer.helper" not in by_q and "outer.Local" not in by_q
    assert "def helper(x):" in by_q["outer"]["content"]
    assert "class Local:" in by_q["outer"]["content"]
    assert by_q["outer"]["content"].rstrip().endswith("return [helper(i) for i in items]")


def test_a_bare_class_header_travels_with_its_first_method(tmp_path):
    """`class Store(Base):` alone is below the minimum. Glued BACKWARD it would
    sit at the tail of `outer`, an unrelated function."""
    by_q = _by_qname(_chunks(_write(tmp_path, "nested.py", NESTED_PY)))
    assert "class Store(Base):" not in by_q["outer"]["content"]
    assert by_q["Store.put"]["content"].splitlines()[0] == "class Store(Base):"
    assert by_q["Store.put"]["content"].splitlines()[-1].strip() == "limit = 5"   # tail stays in class


def test_typescript_class_members_are_units_interface_members_are_not(tmp_path):
    by_q = _by_qname(_chunks(_write(tmp_path, "service.ts", SERVICE_TS)))
    assert by_q["OrderService.list"]["kind"] == "method"
    assert by_q["OrderService.list"]["content"].splitlines()[0].strip() == "@Get('/orders')"
    assert by_q["OrderService.count"]["kind"] == "getter"
    assert by_q["OrderService"]["content"].splitlines()[0] == "@Injectable()"
    assert "Order.total" not in by_q and "total(): number;" in by_q["Order"]["content"]
    assert by_q["loadOrders"]["kind"] == "function"          # arrow function in a const


# --- no text lost ------------------------------------------------------------

def test_every_non_blank_line_lands_in_a_chunk(tmp_path):
    for path in _all_cases(tmp_path):
        lines = _lines(path)
        covered: set[int] = set()
        for chunk in _chunks(path):
            covered.update(range(chunk["line_start"], chunk["line_end"] + 1))
        lost = [i + 1 for i, text in enumerate(lines) if text.strip() and i + 1 not in covered]
        assert lost == [], f"{path.name}: lines {lost} are in no chunk"


def test_line_span_reproduces_the_content_when_read_back_from_disk(tmp_path):
    for path in _all_cases(tmp_path):
        lines = _lines(path)
        for chunk in _chunks(path):
            assert 1 <= chunk["line_start"] <= chunk["line_end"] <= len(lines)
            assert "\n".join(lines[chunk["line_start"] - 1:chunk["line_end"]]) == chunk["content"]


def test_a_form_feed_does_not_shift_line_numbers(tmp_path):
    path = _write(tmp_path, "formfeed.py",
                  "import os\n\x0c\ndef after_form_feed(arg):\n    return os.path.join(arg, 'x')\n")
    (chunk,) = [c for c in _chunks(path) if c.get("qname")]
    assert chunk["content"].endswith("return os.path.join(arg, 'x')")
    assert chunk["line_end"] == 4          # `splitlines` would say 5


def test_a_long_unit_is_windowed_not_truncated(tmp_path):
    chunks = _chunks(_write(tmp_path, "long.py", LONG_PY))
    windows = [c for c in chunks if c.get("qname") == "demo/long.py::enormous"]
    assert len(windows) > 1
    assert "UniqueTailMarker42" in windows[-1]["content"]        # v1 dropped it
    assert all(len(w["content"].splitlines()) <= v1.MAX_BODY_LINES for w in windows)
    for a, b in zip(windows, windows[1:], strict=False):
        assert b["line_start"] <= a["line_end"]                  # overlap: context spans the seam
    assert {w["kind"] for w in windows} == {"function"}


def test_the_module_header_is_chunked_the_v1_fallback_way(tmp_path):
    chunks = _chunks(_write(tmp_path, "nested.py", NESTED_PY))
    head = chunks[0]
    assert head["kind"] == "lines" and head["name"] == "lines_1" and "qname" not in head
    assert "Module docstring v1 never indexed" in head["content"]
    assert "TIMEOUT = 30" in head["content"]


def test_a_stub_below_the_minimum_is_kept_inside_its_neighbour(tmp_path):
    path = _write(tmp_path, "stub.py", "def a():\n    pass\n\n\n"
                                       "def much_longer_function_name(one, two):\n"
                                       "    return one + two\n")
    chunks = _chunks(path)
    assert [c["name"] for c in chunks] == ["much_longer_function_name"]
    assert chunks[0]["content"].startswith("def a():")           # v1 dropped `a` entirely


# --- degenerate input ----------------------------------------------------------

@pytest.mark.parametrize("text", ["", "\n\n  \n", "x = 1\n"])
def test_empty_blank_or_tiny_files_yield_nothing(tmp_path, text):
    assert _chunks(_write(tmp_path, "t.py", text)) == []


def test_broken_source_is_recovered_not_refused(tmp_path):
    path = _write(tmp_path, "broken.py", "def fine_function(arg):\n    return arg + 1\n\n\n"
                                         "def broken(:\n    pass\n\n\n"
                                         "def also_fine(value):\n    return value * 2\n")
    names = [c["name"] for c in _chunks(path)]
    assert "fine_function" in names and "also_fine" in names


def test_an_extractor_failure_on_one_file_warns_and_keeps_its_text(tmp_path, monkeypatch, caplog):
    path = _write(tmp_path, "nested.py", NESTED_PY)
    extractors = v2.load_extractors()

    class Exploding:
        @staticmethod
        def extract(rel, data):
            raise RuntimeError("node shape nobody expected")

    monkeypatch.setattr(v2, "load_extractors", lambda: {**extractors, "python": Exploding})
    with caplog.at_level(logging.WARNING, logger="mb"):
        chunks = _chunks(path)
    assert chunks and all(c["kind"] == "lines" for c in chunks)
    assert "demo/nested.py" in caplog.text


def test_a_grammar_that_cannot_load_is_a_refusal_not_v1(tmp_path, monkeypatch):
    """Poison the grammar module and forget the extractor, so the import runs
    for real and fails the way a missing wheel does."""
    monkeypatch.delitem(sys.modules, "singularity.codegraph.extract_py")
    monkeypatch.setitem(sys.modules, "tree_sitter_python", None)
    with pytest.raises(v2.GrammarPinRefused, match="cannot load"):
        _chunks(FIXTURES / "sample_service.py")


# --- the reading half --------------------------------------------------------

def test_chunks_fit_what_the_engine_already_reads():
    chunk = _by_qname(_chunks(FIXTURES / "sample_service.py"))["retry_with_backoff"]
    line = SearchEngine._compact_line({"meta": chunk, "score": 0.61, "source": "code"})
    assert line.startswith("demo/sample_service.py | 0.61 | [function] | retry_with_backoff |")
    assert chunk["repo"] == "demo"
    assert all("type" not in c for c in _chunks(FIXTURES / "sample_service.py"))


def test_chunk_ids_are_unique_within_a_file(tmp_path):
    for path in _all_cases(tmp_path):
        ids = [c["chunk_id"] for c in _chunks(path)]
        assert len(ids) == len(set(ids))


def test_the_v2_schema_is_not_v1s():
    assert v2.CODE_CHUNK_SCHEMA_VERSION != v1.CODE_CHUNK_SCHEMA_VERSION
