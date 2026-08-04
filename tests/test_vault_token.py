"""`{{VAULT}}` — a path in agent/skill text is filled in at install time.

Those sources are Markdown instructions: they cannot read a config at runtime,
so a path they name has to be baked in by someone. It used to be baked in
literally, with the same string repeated in `register_agents` as a sentinel to
substitute back out — a pairing that had to be kept in sync by hand across ~30
files and silently stopped matching when the authoring machine moved drives.

A token cannot drift. These tests pin that, and pin the ownership marker that
lets `mb uninstall` still recognise a rendered copy as ours.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ENGINE / "install"))

_spec = importlib.util.spec_from_file_location(
    "vault_token_test", ENGINE / "install" / "vault_token.py")
vault_token = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vault_token)

import register_skills  # noqa: E402


@pytest.fixture
def skill(tmp_path):
    src = tmp_path / "src" / "mb-memory"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text(
        "Vault: `{{VAULT}}`. Watcher indexes automatically.\n", encoding="utf-8")
    (src / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n binary {{VAULT}}")
    return src


# --- no source may still carry a literal path --------------------------------

def test_no_agent_or_skill_source_names_a_vault_path():
    """Structural: the whole point is that the sources stop holding paths."""
    import re

    offenders = []
    for folder in ("agents", "skills"):
        for path in (ENGINE / "singularity" / folder).rglob("*.md"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if re.search(r"[A-Za-z]:[\\/]obsidian[\\/]memory brain", text):
                offenders.append(path.name)
    assert not offenders, f"literal vault path still present in: {offenders}"


def test_the_sentinel_is_the_token_not_a_path():
    import register_agents
    assert register_agents.TEMPLATE_VAULT == vault_token.TOKEN


def test_the_token_is_always_followed_by_forward_slashes():
    """The sources were authored on Windows, so the paths after the token used
    backslashes. Substituting a POSIX vault into `{{VAULT}}\\rules\\` yields
    `/Users/k/vault\\rules\\`, which is not a path on any OS. Forward slashes
    work on both."""
    offenders = []
    for folder in ("agents", "skills"):
        for path in (ENGINE / "singularity" / folder).rglob("*.md"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if vault_token.TOKEN + "\\" in text:
                offenders.append(path.name)
    assert not offenders, f"backslash after the token in: {offenders}"


def test_the_token_survives_only_where_a_path_is_actually_needed():
    """A mention that merely IDENTIFIES the vault does not need its path —
    `mb search` already knows where it is, and naming it there is the habit
    that put one machine's drive letter into 18 files. The token is for places
    the reader must locate a concrete file or folder.

    Guard shape: every surviving occurrence has a path segment after it."""
    import re

    bare = []
    for folder in ("agents", "skills"):
        for path in (ENGINE / "singularity" / folder).rglob("*.md"):
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                for m in re.finditer(re.escape(vault_token.TOKEN) + r"(.?)", line):
                    if m.group(1) != "/":
                        bare.append(f"{path.name}: {line.strip()[:70]}")
    assert not bare, ("the vault is named without a path after it — drop the "
                      f"mention instead: {bare}")


# --- rendering ----------------------------------------------------------------

def test_render_substitutes_in_text_and_leaves_binaries_alone(skill, tmp_path):
    dst = tmp_path / "out"
    vault = tmp_path / "my vault"

    vault_token.render_tree(skill, dst, vault)

    assert str(vault) in (dst / "SKILL.md").read_text(encoding="utf-8")
    assert vault_token.TOKEN not in (dst / "SKILL.md").read_text(encoding="utf-8")
    # a .png is copied byte-for-byte: substituting inside one would corrupt it
    assert (dst / "logo.png").read_bytes() == (skill / "logo.png").read_bytes()


def test_tree_without_a_token_is_left_to_the_linker(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "SKILL.md").write_text("no path here\n", encoding="utf-8")
    assert vault_token.tree_has_token(plain) is False


def test_render_is_idempotent_and_follows_a_moved_vault(skill, tmp_path):
    dst = tmp_path / "out"
    vault_token.render_tree(skill, dst, tmp_path / "first")
    vault_token.render_tree(skill, dst, tmp_path / "second")
    text = (dst / "SKILL.md").read_text(encoding="utf-8")
    assert str(tmp_path / "second") in text
    assert "first" not in text


# --- ownership ----------------------------------------------------------------

def test_a_rendered_copy_is_recognised_as_ours(skill, tmp_path):
    dst = tmp_path / "out"
    vault_token.render_tree(skill, dst, tmp_path / "v")
    assert vault_token.is_rendered_copy(dst, skill) is True
    assert register_skills.is_owned(dst, skill) is True


def test_a_foreign_directory_is_never_ours(tmp_path, skill):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "SKILL.md").write_text("someone else's\n", encoding="utf-8")
    assert vault_token.is_rendered_copy(foreign, skill) is False
    assert register_skills.is_owned(foreign, skill) is False


def test_a_marker_naming_another_engine_is_not_ours(tmp_path, skill):
    """Two clones on one machine must not uninstall each other's skills."""
    dst = tmp_path / "out"
    vault_token.render_tree(skill, dst, tmp_path / "v")
    marker = json.loads((dst / vault_token.MARKER).read_text(encoding="utf-8"))
    marker["source"] = str(tmp_path / "another-engine" / "mb-memory")
    (dst / vault_token.MARKER).write_text(json.dumps(marker), encoding="utf-8")

    assert vault_token.is_rendered_copy(dst, skill) is False


def test_a_corrupt_marker_is_not_ours(tmp_path, skill):
    dst = tmp_path / "out"
    vault_token.render_tree(skill, dst, tmp_path / "v")
    (dst / vault_token.MARKER).write_text("{not json", encoding="utf-8")
    assert vault_token.is_rendered_copy(dst, skill) is False


# --- the deploy path ----------------------------------------------------------

def test_link_dir_renders_a_token_skill_and_marks_it(skill, tmp_path):
    dst = tmp_path / "deployed"
    status = register_skills._link_dir(skill, dst, tmp_path / "v")
    assert status == "rendered"
    assert (dst / vault_token.MARKER).is_file()
    assert vault_token.TOKEN not in (dst / "SKILL.md").read_text(encoding="utf-8")


def test_link_dir_preserves_a_differing_local_directory(skill, tmp_path):
    """The backup contract survives the switch to rendering."""
    dst = tmp_path / "deployed"
    dst.mkdir()
    (dst / "SKILL.md").write_text("my own edits\n", encoding="utf-8")

    status = register_skills._link_dir(skill, dst, tmp_path / "v")

    assert status == "backed-up"
    backups = list(tmp_path.glob("deployed.backup-*"))
    assert backups and "my own edits" in (backups[0] / "SKILL.md").read_text(
        encoding="utf-8")
