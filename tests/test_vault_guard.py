"""T08: client-neutral vault-guard core (singularity/vault_guard.py)."""
import pytest

from singularity.machine import vault_guard


@pytest.fixture
def vault(tmp_path):
    return tmp_path


# --- vault_relative --------------------------------------------------------------

def test_vault_relative_inside(vault):
    p = vault / "learnings" / "active" / "T.md"
    assert vault_guard.vault_relative(vault, p) == "learnings/active/T.md"


def test_vault_relative_outside_returns_none(vault, tmp_path_factory):
    other = tmp_path_factory.mktemp("elsewhere")
    assert vault_guard.vault_relative(vault, other / "x.md") is None


# --- is_config_json ----------------------------------------------------------------

def test_is_config_json_true(vault):
    assert vault_guard.is_config_json(vault, vault / ".mb" / "config.json")


def test_is_config_json_false_for_other_mb_files(vault):
    assert not vault_guard.is_config_json(vault, vault / ".mb" / "context.json")


def test_is_config_json_false_outside_vault(vault, tmp_path_factory):
    other = tmp_path_factory.mktemp("elsewhere")
    assert not vault_guard.is_config_json(vault, other / ".mb" / "config.json")


# --- new_file_managed_root -----------------------------------------------------------

def test_new_file_in_managed_root_flagged(vault):
    target = vault / "learnings" / "active" / "NEW-TASK.md"
    assert vault_guard.new_file_managed_root(vault, target) == "learnings/active"


def test_existing_file_never_flagged(vault):
    d = vault / "howto"; d.mkdir(parents=True)
    p = d / "existing.md"
    p.write_text("x", encoding="utf-8")
    assert vault_guard.new_file_managed_root(vault, p) is None


def test_templates_carve_out_exempt(vault):
    target = vault / "templates" / "new.md"
    assert vault_guard.new_file_managed_root(vault, target) is None


def test_compaction_plans_carve_out_exempt(vault):
    target = vault / "compaction-plans" / "handoff.md"
    assert vault_guard.new_file_managed_root(vault, target) is None


def test_unmanaged_root_not_flagged(vault):
    target = vault / "random-scratch-dir" / "x.md"
    assert vault_guard.new_file_managed_root(vault, target) is None


def test_file_outside_vault_not_flagged(vault, tmp_path_factory):
    other = tmp_path_factory.mktemp("elsewhere")
    assert vault_guard.new_file_managed_root(vault, other / "learnings" / "x.md") is None


@pytest.mark.parametrize("root", vault_guard.MANAGED_ROOTS)
def test_every_managed_root_is_flagged(vault, root):
    target = vault / root / "new-file.md"
    assert vault_guard.new_file_managed_root(vault, target) == root
