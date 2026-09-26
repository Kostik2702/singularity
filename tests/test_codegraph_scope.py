"""Which repositories become graph units, and which of their files are read
(CODE-GRAPH D1, D-3 and D-4).

Each case plants what the council found in the field: a nested repository
whose edits `git status` never names, a gitignored file the index would
otherwise carry, a committed `node_modules`, an owner-specific exclusion that
belongs in config, test files the semantic corpus drops but impact analysis
needs, and an extra root that went missing.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from singularity.codegraph import scope
from singularity.config import Config


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _repo(root: Path, files: dict[str, str], *, commit: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    if commit:
        _git(root, "add", "-A")
        _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return root


def _cfg(tmp_path: Path, **data) -> Config:
    return Config(vault=tmp_path / "vault", data={"code_repos_dir": str(tmp_path / "Project"), **data})


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "Project"
    _repo(root / "api", {
        "app/main.py": "def main():\n    pass\n",
        "app/main_test.py": "def test_main():\n    pass\n",
        "web/types.d.ts": "export declare function f(): void;\n",
        "node_modules/dep/index.js": "module.exports = 1;\n",
        "build/out.py": "x = 1\n",
        "lib/secret_keys.py": "KEY = 'x'\n",
        "vendor-sdk-1.2.3/engine.py": "def engine():\n    pass\n",
        "README.md": "# not code\n",
        ".gitignore": "generated/\n",
        "generated/gen.py": "def gen():\n    pass\n",
    })
    (root / "api" / "app" / "untracked_new.py").write_text("def fresh():\n    pass\n", encoding="utf-8")
    _repo(root / "api" / "plugins" / "inner", {"core.py": "def core():\n    pass\n"})
    scratch = root / "scratch"
    (scratch / "tools").mkdir(parents=True)
    (scratch / "tools" / "run.py").write_text("def run():\n    pass\n", encoding="utf-8")
    _repo(scratch / "nested", {"lib.py": "def lib():\n    pass\n"})
    _repo(root / "test-project", {"x.py": "def x():\n    pass\n"})
    _repo(root / "owner-private", {"p.py": "def p():\n    pass\n"})
    return root


def _units(cfg) -> dict[str, scope.Unit]:
    return {u.key: u for u in scope.discover_units(cfg)}


def test_every_git_toplevel_and_every_non_git_child_is_a_unit(tmp_path, project):
    units = _units(_cfg(tmp_path))
    assert units["api"].is_git and units["api/plugins/inner"].is_git
    assert not units["scratch"].is_git and units["scratch/nested"].is_git
    assert "test-project" not in units                      # SKIP_REPOS, as for code_index


def test_owner_specific_exclusions_come_from_config(tmp_path, project):
    assert "owner-private" in _units(_cfg(tmp_path))
    assert "owner-private" not in _units(_cfg(tmp_path, code_skip_dirs=["owner-private"]))


def test_the_file_set_is_what_git_sees_minus_the_filters(tmp_path, project):
    units = _units(_cfg(tmp_path))
    files = {f.path: f for f in scope.list_files(units["api"], _cfg(tmp_path))}
    assert set(files) == {"app/main.py", "app/main_test.py", "app/untracked_new.py", "web/types.d.ts"}
    assert files["app/main_test.py"].is_test and files["web/types.d.ts"].is_test
    assert not files["app/main.py"].is_test


def test_a_nested_repository_belongs_to_itself_not_to_its_parent(tmp_path, project):
    """`git status` in the parent shows only `?? plugins/inner/` whatever
    changes inside it, so reading those files as the parent's would index
    what its detector can never see (UNMET n)."""
    cfg = _cfg(tmp_path)
    units = _units(cfg)
    assert not any(f.path.startswith("plugins/") for f in scope.list_files(units["api"], cfg))
    assert [f.path for f in scope.list_files(units["api/plugins/inner"], cfg)] == ["core.py"]
    assert [f.path for f in scope.list_files(units["scratch"], cfg)] == ["tools/run.py"]


def test_an_extra_root_is_a_unit_and_a_missing_one_fails_loudly(tmp_path, project):
    mono = _repo(tmp_path / "elsewhere" / "mono", {"svc/a.py": "def a():\n    pass\n"})
    units = _units(_cfg(tmp_path, code_graph_extra_roots=[str(mono)]))
    extra = [u for u in units.values() if u.root == mono]
    assert len(extra) == 1 and extra[0].is_git and extra[0].key.startswith("extra/")
    with pytest.raises(scope.GraphRootMissing):
        scope.discover_units(_cfg(tmp_path, code_graph_extra_roots=[str(tmp_path / "gone")]))


def test_a_missing_repositories_root_fails_loudly(tmp_path):
    with pytest.raises(scope.GraphRootMissing):
        scope.discover_units(Config(vault=tmp_path, data={"code_repos_dir": str(tmp_path / "nope")}))


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows construct")
def test_a_repository_attached_by_junction_is_a_unit(tmp_path, project):
    real = _repo(tmp_path / "outside" / "engine", {"e.py": "def e():\n    pass\n"})
    subprocess.run(["cmd", "/c", "mklink", "/J", str(project / "Engine"), str(real)],
                   check=True, capture_output=True)
    units = _units(_cfg(tmp_path))
    assert units["Engine"].is_git
    assert [f.path for f in scope.list_files(units["Engine"], _cfg(tmp_path))] == ["e.py"]
