"""Unit tests for install/register_skills — the engine-owned skill deployer."""
import importlib.util
import os
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "register_skills", ENGINE / "install" / "register_skills.py")
register_skills = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(register_skills)


def _mklink_supported(tmp_path):
    """Skip link-behaviour asserts on hosts where neither symlink nor junction
    can be created (rare; register_skills copies as a last resort there)."""
    src = tmp_path / "_probe_src"; src.mkdir()
    dst = tmp_path / "_probe_dst"
    try:
        os.symlink(src, dst, target_is_directory=True); return True
    except OSError:
        pass
    if os.name == "nt":
        try:
            import _winapi
            _winapi.CreateJunction(str(src), str(dst)); return True
        except OSError:
            return False
    return False


def test_every_owned_skill_exists_in_repo():
    for name in register_skills.SKILLS:
        assert (register_skills.SKILLS_SRC / name).is_dir(), f"missing repo skill: {name}"


def test_registry_excludes_unmanaged_skills():
    assert "comfyui-photorealism" not in register_skills.SKILLS   # non-engine, must stay loose
    assert len(set(register_skills.SKILLS)) == len(register_skills.SKILLS)  # no dupes


def test_link_dir_creates_and_is_idempotent(tmp_path):
    if not _mklink_supported(tmp_path):
        import pytest; pytest.skip("no symlink/junction support on host")
    src = tmp_path / "src"; src.mkdir(); (src / "SKILL.md").write_text("x")
    dst = tmp_path / "dst"
    assert register_skills._link_dir(src, dst) == "linked"
    assert (dst / "SKILL.md").read_text() == "x"          # readable through the link
    assert register_skills._link_dir(src, dst) == "ok"    # second run: no-op


def test_link_dir_replaces_stale_real_dir(tmp_path):
    if not _mklink_supported(tmp_path):
        import pytest; pytest.skip("no symlink/junction support on host")
    src = tmp_path / "src"; src.mkdir(); (src / "SKILL.md").write_text("new")
    dst = tmp_path / "dst"; dst.mkdir(); (dst / "SKILL.md").write_text("stale loose copy")
    assert register_skills._link_dir(src, dst) == "linked"
    assert (dst / "SKILL.md").read_text() == "new"        # loose dir replaced by link to repo
