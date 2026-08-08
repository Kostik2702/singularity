"""C02: symbol-boundary chunking for the code corpus.

Python carries the heaviest coverage on purpose (decision D6 / risk R7): it is
69% of this codebase AND the only language with no ancestor implementation to
port from, so every Python case here is new code being pinned down rather than a
port being re-verified.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from singularity.search import code_chunking
from singularity.search.code_chunking import extract_code_chunks
from singularity.search.engine import SearchEngine

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "code"


def _chunks(name: str, rel: str | None = None):
    path = FIXTURES / name
    return extract_code_chunks(path, rel or f"demo/{name}")


def _by_name(chunks):
    return {c["name"]: c for c in chunks}


# --- Python: the primary path ------------------------------------------------

def test_python_symbols_become_separate_chunks():
    by_name = _by_name(_chunks("sample_service.py"))
    assert by_name["retry_with_backoff"]["kind"] == "function"
    assert by_name["fetch_all"]["kind"] == "function"          # async def
    assert by_name["SessionStore"]["kind"] == "class"
    # a method is its own chunk, not swallowed by the class
    assert by_name["size"]["kind"] == "method"
    assert by_name["key_for"]["kind"] == "method"
    assert by_name["evict_expired"]["kind"] == "method"        # async def in class


def test_python_decorator_is_part_of_the_chunk_not_cut_off():
    """`@app.route("/orders")` above a handler is the single most searchable
    fact about it; a chunk starting at `def` throws it away."""
    by_name = _by_name(_chunks("sample_service.py"))
    assert by_name["size"]["content"].splitlines()[0].strip() == "@property"
    assert by_name["key_for"]["content"].splitlines()[0].strip() == "@staticmethod"


def test_python_docstring_under_the_signature_lands_in_description():
    by_name = _by_name(_chunks("sample_service.py"))
    assert "Retry a call with exponential backoff." in by_name["retry_with_backoff"]["description"]
    assert by_name["size"]["description"] == "Number of live sessions."
    # single-quoted form must work too
    assert by_name["evict_expired"]["description"] == "Drop everything older than the TTL."


def test_python_comment_banner_above_the_signature_lands_in_description():
    desc = _by_name(_chunks("sample_service.py"))["retry_with_backoff"]["description"]
    assert desc.startswith("Backoff helper kept out of the class on purpose:")


def test_python_class_docstring_is_read_forward_not_from_a_banner():
    by_name = _by_name(_chunks("sample_service.py"))
    assert by_name["SessionStore"]["description"] == \
        "Keeps sessions in memory for the lifetime of the process."


def test_python_handles_tabs_for_indentation(tmp_path):
    src = tmp_path / "tabbed.py"
    src.write_text("class Tabbed:\n"
                   "\tDESCRIPTION = 'indented with tabs, not spaces'\n"
                   "\tdef inner(self):\n"
                   "\t\treturn 'a value long enough to survive the minimum'\n",
                   encoding="utf-8")
    by_name = _by_name(extract_code_chunks(src, "demo/tabbed.py"))
    assert by_name["Tabbed"]["kind"] == "class"
    assert by_name["inner"]["kind"] == "method"      # indented -> method, tabs or not


# --- TS/JS: the ported path --------------------------------------------------

def test_typescript_finds_class_interface_const_and_arrow_function():
    by_name = _by_name(_chunks("sample_widget.ts"))
    assert by_name["WidgetProps"]["kind"] == "interface"
    assert by_name["WidgetView"]["kind"] == "class"
    assert by_name["DEFAULT_WIDGET"]["kind"] == "const"       # export const object
    assert by_name["loadWidget"]["kind"] == "const"           # export const arrow fn
    assert by_name["WidgetState"]["kind"] == "enum"
    assert by_name["internalHelper"]["kind"] == "function"


def test_typescript_block_comment_lands_in_description():
    by_name = _by_name(_chunks("sample_widget.ts"))
    assert by_name["WidgetProps"]["description"] == \
        "Public shape of a widget as the API returns it."
    assert by_name["loadWidget"]["description"] == \
        "Fetches a widget and retries once on a 5xx."


def test_a_comment_describing_the_next_symbol_is_not_glued_to_the_previous_one():
    by_name = _by_name(_chunks("sample_widget.ts"))
    assert "Renders one widget" not in by_name["WidgetProps"]["content"]
    assert "Renders one widget" in by_name["WidgetView"]["description"]


# --- line numbers are the contract -------------------------------------------

@pytest.mark.parametrize("name", ["sample_service.py", "sample_widget.ts",
                                  "sample_config.js", "sample_legacy.rb"])
def test_line_span_reproduces_the_content_when_read_back_from_disk(name):
    """Not "the counter looks right" — actually slice the file with the numbers
    the chunk claims and require the text back."""
    path = FIXTURES / name
    lines = path.read_text(encoding="utf-8").splitlines()
    for chunk in _chunks(name):
        assert chunk["line_start"] >= 1
        assert chunk["line_end"] >= chunk["line_start"]
        sliced = "\n".join(lines[chunk["line_start"] - 1:chunk["line_end"]])
        assert sliced.strip("\n") == chunk["content"]


# --- truncation, minimum size, fallback --------------------------------------

def test_body_is_truncated_at_the_line_limit(tmp_path):
    body = "\n".join(f"    value_{i} = {i}" for i in range(200))
    src = tmp_path / "long.py"
    src.write_text(f"def enormous():\n{body}\n", encoding="utf-8")
    chunk = extract_code_chunks(src, "demo/long.py")[0]
    assert len(chunk["content"].splitlines()) == code_chunking.MAX_BODY_LINES
    assert chunk["line_end"] - chunk["line_start"] + 1 == code_chunking.MAX_BODY_LINES


def test_a_symbol_shorter_than_the_minimum_is_dropped(tmp_path):
    src = tmp_path / "stub.py"
    src.write_text("def a():\n    pass\n\n\n"
                   "def much_longer_function_name(one, two):\n    return one + two\n",
                   encoding="utf-8")
    names = [c["name"] for c in extract_code_chunks(src, "demo/stub.py")]
    assert names == ["much_longer_function_name"]


def test_a_file_without_any_boundary_falls_back_to_fixed_windows():
    chunks = _chunks("sample_config.js")
    assert [c["kind"] for c in chunks] == ["lines"]
    assert chunks[0]["name"] == "lines_1"


def test_fallback_windows_overlap_and_are_named_by_their_first_line(tmp_path):
    src = tmp_path / "flat.js"
    src.write_text("\n".join(f"const value_{i} = {i};" for i in range(120)),
                   encoding="utf-8")
    chunks = extract_code_chunks(src, "demo/flat.js")
    starts = [c["line_start"] for c in chunks]
    step = code_chunking.FALLBACK_WINDOW_LINES - code_chunking.FALLBACK_OVERLAP_LINES
    assert starts == [1, 1 + step, 1 + 2 * step, 1 + 3 * step]
    assert [c["name"] for c in chunks] == [f"lines_{s}" for s in starts]
    assert all(c["kind"] == "lines" for c in chunks)


def test_ruby_is_not_claimed_as_supported_and_falls_back(tmp_path):
    """D6: zero `.rb` files exist here, so Ruby boundaries would be untested
    guesses. Falling back is the honest failure mode — inventing `def`/`class`
    cuts that were never exercised is the dishonest one."""
    chunks = _chunks("sample_legacy.rb")
    assert [c["kind"] for c in chunks] == ["lines"]
    assert not [c for c in chunks if c["name"] in {"total", "InvoiceCalculator"}]


# --- degenerate inputs must not raise ----------------------------------------

def test_empty_file_yields_nothing(tmp_path):
    src = tmp_path / "empty.py"
    src.write_text("", encoding="utf-8")
    assert extract_code_chunks(src, "demo/empty.py") == []


def test_whitespace_only_file_yields_nothing(tmp_path):
    src = tmp_path / "blank.py"
    src.write_text("\n\n   \n\t\n", encoding="utf-8")
    assert extract_code_chunks(src, "demo/blank.py") == []


def test_comments_only_file_does_not_raise(tmp_path):
    src = tmp_path / "notes.py"
    src.write_text("\n".join("# just a note about something" for _ in range(30)),
                   encoding="utf-8")
    chunks = extract_code_chunks(src, "demo/notes.py")
    assert all(c["kind"] == "lines" for c in chunks)


def test_broken_encoding_does_not_raise(tmp_path):
    src = tmp_path / "broken.py"
    src.write_bytes(b"def widget_handler(request):\n    return b'\xff\xfe not utf8'\n")
    chunks = extract_code_chunks(src, "demo/broken.py")
    assert [c["name"] for c in chunks] == ["widget_handler"]


def test_missing_file_yields_nothing(tmp_path):
    assert extract_code_chunks(tmp_path / "nope.py", "demo/nope.py") == []


# --- contract with the reading half (already shipped in W03) -----------------

def test_repo_is_the_first_path_segment():
    for chunk in _chunks("sample_service.py", rel="api-gateway/svc/sample.py"):
        assert chunk["repo"] == "api-gateway"


def test_a_file_outside_any_repo_carries_no_repo_key(tmp_path):
    src = tmp_path / "loose.py"
    src.write_text("def loose_top_level_function():\n    return 'something'\n",
                   encoding="utf-8")
    assert "repo" not in extract_code_chunks(src, "loose.py")[0]


def test_chunk_fields_are_the_ones_the_engine_already_reads():
    """`engine._compact_line` reads `kind`/`name` and `_match_filters` reads
    `repo` — both were written during W03 in anticipation of this corpus, so the
    chunker is not free to invent different key names."""
    chunk = _by_name(_chunks("sample_service.py"))["retry_with_backoff"]
    line = SearchEngine._compact_line({"meta": chunk, "score": 0.61, "source": "code"})
    assert line.startswith("demo/sample_service.py | 0.61 | [function] | retry_with_backoff |")
    assert SearchEngine._match_filters(chunk, {"repos": ["demo", "other"]})
    assert not SearchEngine._match_filters(chunk, {"repos": ["other"]})


def test_code_chunks_carry_no_type_key_so_the_kind_label_survives():
    """`_compact_line` prefers `type` over `kind`; a code chunk that grew a
    `type` key would silently start rendering as a note."""
    for chunk in _chunks("sample_service.py"):
        assert "type" not in chunk


def test_chunk_ids_are_unique_within_a_file():
    ids = [c["chunk_id"] for c in _chunks("sample_service.py")]
    assert len(ids) == len(set(ids))


def test_embedded_text_leads_with_path_and_signature():
    chunk = _by_name(_chunks("sample_service.py"))["retry_with_backoff"]
    assert chunk["text"].startswith("demo/sample_service.py — function retry_with_backoff:")
    body = chunk["text"].split(" | ", 1)[1]
    assert len(body) <= code_chunking.EMBED_CODE_CHARS


def test_code_schema_version_is_independent_of_the_markdown_one():
    from singularity.search import chunking
    assert code_chunking.CODE_CHUNK_SCHEMA_VERSION is not chunking.CHUNK_SCHEMA_VERSION
