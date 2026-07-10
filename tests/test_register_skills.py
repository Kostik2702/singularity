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


def test_link_dir_replaces_identical_real_dir_automatically(tmp_path):
    if not _mklink_supported(tmp_path):
        import pytest; pytest.skip("no symlink/junction support on host")
    src = tmp_path / "src"; src.mkdir(); (src / "SKILL.md").write_text("same")
    dst = tmp_path / "dst"; dst.mkdir(); (dst / "SKILL.md").write_text("same")
    assert register_skills._link_dir(src, dst) == "linked"
    assert (dst / "SKILL.md").read_text() == "same"       # identical copy -> auto-replaced by link


def test_link_dir_never_deletes_differing_real_dir_backs_up_instead(tmp_path):
    if not _mklink_supported(tmp_path):
        import pytest; pytest.skip("no symlink/junction support on host")
    src = tmp_path / "src"; src.mkdir(); (src / "SKILL.md").write_text("new repo content")
    dst = tmp_path / "dst"; dst.mkdir(); (dst / "SKILL.md").write_text("USER'S OWN EDITS")

    status = register_skills._link_dir(src, dst)

    assert status == "backed-up"
    # dst is now the link to repo source — original user content NOT lost
    assert (dst / "SKILL.md").read_text() == "new repo content"
    backups = list(tmp_path.glob("dst.backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "SKILL.md").read_text() == "USER'S OWN EDITS"
    assert (backups[0] / ".migration-manifest.txt").exists()


def test_link_dir_backup_never_collides_on_repeated_conflicts(tmp_path):
    if not _mklink_supported(tmp_path):
        import pytest; pytest.skip("no symlink/junction support on host")
    src = tmp_path / "src"; src.mkdir(); (src / "SKILL.md").write_text("repo")
    dst = tmp_path / "dst"

    dst.mkdir(); (dst / "SKILL.md").write_text("edit A")
    register_skills._link_dir(src, dst)
    dst.unlink()                                            # simulate a second stale real dir
    dst.mkdir(); (dst / "SKILL.md").write_text("edit B")
    register_skills._link_dir(src, dst)

    backups = sorted(tmp_path.glob("dst.backup-*"))
    assert len(backups) == 2
    contents = {(b / "SKILL.md").read_text() for b in backups}
    assert contents == {"edit A", "edit B"}


def test_link_dir_replaces_stale_junction_without_touching_its_old_target(tmp_path):
    if os.name != "nt":
        import pytest; pytest.skip("junction reparse-point behaviour is Windows-only")
    if not _mklink_supported(tmp_path):
        import pytest; pytest.skip("no symlink/junction support on host")
    import _winapi
    src = tmp_path / "src"; src.mkdir(); (src / "SKILL.md").write_text("current repo")
    old_target = tmp_path / "old_target"; old_target.mkdir()
    (old_target / "SKILL.md").write_text("old target content — must survive")
    dst = tmp_path / "dst"
    _winapi.CreateJunction(str(old_target), str(dst))    # stale junction -> old_target

    status = register_skills._link_dir(src, dst)

    assert status == "linked"
    assert (dst / "SKILL.md").read_text() == "current repo"
    # the junction's OLD target must be untouched — rmdir on the reparse point
    # must not have recursed into and deleted what it pointed at
    assert (old_target / "SKILL.md").read_text() == "old target content — must survive"


def test_dirs_equal_detects_content_and_structural_differences(tmp_path):
    a = tmp_path / "a"; a.mkdir(); (a / "x.txt").write_text("1")
    b = tmp_path / "b"; b.mkdir(); (b / "x.txt").write_text("1")
    assert register_skills._dirs_equal(a, b) is True

    (b / "x.txt").write_text("2")
    assert register_skills._dirs_equal(a, b) is False

    (b / "x.txt").write_text("1"); (b / "extra.txt").write_text("x")
    assert register_skills._dirs_equal(a, b) is False
