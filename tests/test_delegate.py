"""Unit tests for the mb-llm-delegate skill's outfile-safety contract (REV-1).

delegate.py lives under a hyphenated skill directory, so it is loaded via
importlib rather than a normal package import.
"""
import importlib.util
import os
from pathlib import Path

DELEGATE_PATH = (Path(__file__).resolve().parents[1] / "singularity" / "skills" /
                  "mb-llm-delegate" / "delegate.py")

_spec = importlib.util.spec_from_file_location("delegate", DELEGATE_PATH)
delegate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(delegate)


def test_max_tokens_truncation_leaves_existing_outfile_untouched(tmp_path):
    outfile = tmp_path / "app.html"
    outfile.write_text("ORIGINAL CONTENT\n", encoding="utf-8")

    code, msg = delegate.decide_output("partial garbage...", "max_tokens", str(outfile))

    assert code == 2
    assert "outfile left untouched" in msg
    assert outfile.read_text(encoding="utf-8") == "ORIGINAL CONTENT\n"
    assert not (tmp_path / "app.html.partial").exists()


def test_max_tokens_with_keep_partial_saves_sibling_file_and_preserves_outfile(tmp_path):
    outfile = tmp_path / "app.html"
    outfile.write_text("ORIGINAL CONTENT\n", encoding="utf-8")

    code, msg = delegate.decide_output("<truncated code", "max_tokens", str(outfile),
                                        keep_partial=True)

    assert code == 2
    partial = tmp_path / "app.html.partial"
    assert partial.exists()
    assert partial.read_text(encoding="utf-8") == "<truncated code\n"
    assert outfile.read_text(encoding="utf-8") == "ORIGINAL CONTENT\n"
    assert "partial saved to" in msg


def test_empty_output_leaves_existing_outfile_untouched(tmp_path):
    outfile = tmp_path / "app.html"
    outfile.write_text("ORIGINAL CONTENT\n", encoding="utf-8")

    code, msg = delegate.decide_output("   \n  ", "end_turn", str(outfile))

    assert code == 1
    assert "empty output" in msg
    assert outfile.read_text(encoding="utf-8") == "ORIGINAL CONTENT\n"


def test_empty_output_no_preexisting_outfile_does_not_create_one(tmp_path):
    outfile = tmp_path / "app.html"

    code, _ = delegate.decide_output("", "end_turn", str(outfile))

    assert code == 1
    assert not outfile.exists()


def test_successful_response_atomically_replaces_outfile(tmp_path):
    outfile = tmp_path / "app.html"
    outfile.write_text("OLD\n", encoding="utf-8")

    code, msg = delegate.decide_output("<html>NEW</html>", "end_turn", str(outfile))

    assert code == 0
    assert msg is None
    assert outfile.read_text(encoding="utf-8") == "<html>NEW</html>\n"
    # no leftover temp file
    assert list(tmp_path.iterdir()) == [outfile]


def test_successful_response_strips_markdown_fence_and_file_wrapper(tmp_path):
    outfile = tmp_path / "app.html"
    text = "```html\n<file>\n<html>NEW</html>\n</file>\n```"

    code, _ = delegate.decide_output(text, "end_turn", str(outfile))

    assert code == 0
    assert outfile.read_text(encoding="utf-8") == "<html>NEW</html>\n"


def test_no_outfile_success_does_not_touch_filesystem(tmp_path):
    code, msg = delegate.decide_output("hello", "end_turn", None)
    assert code == 0
    assert msg is None
    assert list(tmp_path.iterdir()) == []


def test_atomic_write_does_not_leave_temp_file_on_success(tmp_path):
    target = tmp_path / "out.txt"
    delegate.atomic_write(str(target), "content")
    assert target.read_text(encoding="utf-8") == "content\n"
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_write_replace_is_atomic_swap_not_in_place_truncate(tmp_path):
    target = tmp_path / "out.txt"
    target.write_text("OLD LONGER CONTENT HERE\n", encoding="utf-8")
    tmp_marker = tmp_path / f"out.txt.{os.getpid()}.tmp"

    delegate.atomic_write(str(target), "new")

    assert target.read_text(encoding="utf-8") == "new\n"
    assert not tmp_marker.exists()
