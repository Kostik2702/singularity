"""The benchmark proxy does not flatter the engine (CODE-GRAPH P0, critic C40).

Two ways a proxy makes a resolver look fast were named in council: copies of
one repository (every name ambiguous) and flattened paths (imports stop
resolving). Each test plants one of them and checks the builder refuses it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tools.code_graph_proxy import build, main  # noqa: E402


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def test_identical_content_enters_once_whichever_source_it_came_from(tmp_path):
    a = _tree(tmp_path / "a", {"pkg/util.py": "def f():\n    return 1\n"})
    b = _tree(tmp_path / "b", {"lib/util.py": "def f():\n    return 1\n",
                               "lib/other.py": "def g():\n    return 2\n"})
    m = build([("a", a), ("b", b)], tmp_path / "out", target=10)
    assert m["files"] == 2
    assert m["sources"]["b"]["duplicates"] == 1


def test_directory_structure_survives_so_imports_can_resolve(tmp_path):
    src = _tree(tmp_path / "s", {"pkg/__init__.py": "from .core import run\n",
                                 "pkg/core.py": "def run():\n    return 0\n"})
    build([("s", src)], tmp_path / "out", target=10)
    assert (tmp_path / "out" / "s" / "pkg" / "core.py").is_file()
    assert (tmp_path / "out" / "s" / "pkg" / "__init__.py").is_file()


def test_a_project_tree_does_not_contribute_its_node_modules(tmp_path):
    src = _tree(tmp_path / "proj", {"src/app.ts": "export const a = 1;\n",
                                    "node_modules/dep/index.js": "module.exports = 1;\n"})
    m = build([("proj", src)], tmp_path / "out", target=10)
    assert m["files"] == 1


def test_the_build_is_deterministic_and_stops_at_the_target(tmp_path):
    src = _tree(tmp_path / "s", {f"m{i:02d}.py": f"x = {i}\n" for i in range(20)})
    one = build([("s", src)], tmp_path / "o1", target=7)
    two = build([("s", src)], tmp_path / "o2", target=7)
    assert one["files"] == two["files"] == 7
    assert one["fingerprint"] == two["fingerprint"]


def test_a_short_proxy_exits_non_zero_rather_than_pass_for_the_real_size(tmp_path):
    src = _tree(tmp_path / "s", {"a.py": "x = 1\n"})
    code = main(["build", "--out", str(tmp_path / "out"), "--source", f"s={src}",
                 "--target", "5"])
    assert code == 3
    manifest = json.loads((tmp_path / "out.manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"] == 1
