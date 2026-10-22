"""SING-14: two `mb` processes saving the machine config must not lose edits.

Each child behaves like one `mb` invocation per edit: load the config, change a
key, save. A short pause between load and save widens the read-modify-write
window, so a save that does not lock and re-read loses keys reliably."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from singularity import config as config_mod

_CHILD = r"""
import random, sys, time
from pathlib import Path
from singularity import config as config_mod

config_mod.CONFIG_FILE = config_mod.POINTER_FILE = Path(sys.argv[1])
vault, tag, n, shared = sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5] == "1"
for i in range(n):
    cfg = config_mod.load_config(vault)
    time.sleep(random.uniform(0, 0.004))
    cfg.data["conflict_key" if shared else f"{tag}_{i}"] = f"{tag}-{i}"
    cfg.save()
"""


def _run_two(tmp_path: Path, shared: bool) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg_file = tmp_path / "machine.json"
    cfg_file.write_text(json.dumps({"vault_path": str(vault)}), encoding="utf-8")
    root = Path(config_mod.__file__).resolve().parents[1]
    procs = [subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(cfg_file), str(vault), tag, "20",
         "1" if shared else "0"],
        cwd=root, stderr=subprocess.PIPE, text=True) for tag in ("a", "b")]
    for p in procs:
        _, err = p.communicate(timeout=120)
        assert p.returncode == 0, err
    return cfg_file


def test_parallel_saves_of_different_keys_all_survive(tmp_path):
    cfg_file = _run_two(tmp_path, shared=False)
    data = json.loads(cfg_file.read_text(encoding="utf-8"))
    missing = [f"{t}_{i}" for t in "ab" for i in range(20) if f"{t}_{i}" not in data]
    assert not missing, f"lost edits: {missing}"
    assert not cfg_file.with_name(cfg_file.name + ".lock").exists()


def test_parallel_saves_of_one_key_last_writer_wins_and_file_is_valid(tmp_path):
    cfg_file = _run_two(tmp_path, shared=True)
    data = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert data["conflict_key"] in {"a-19", "b-19"}
    assert data["vault_path"] == str(tmp_path / "vault")


def test_save_applies_only_own_changes_and_merges_deletions(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg_file = config_mod.CONFIG_FILE        # redirected by the autouse fixture
    cfg_file.write_text(json.dumps(
        {"vault_path": str(vault), "keep": 1, "drop": 2,
         "projects": {"x": {"path": "/x"}, "y": {"path": "/y"}}}), encoding="utf-8")
    mine = config_mod.load_config(str(vault))
    # another process saves in between
    other = json.loads(cfg_file.read_text(encoding="utf-8"))
    other["theirs"] = 3
    other["projects"]["y"]["path"] = "/y2"
    cfg_file.write_text(json.dumps(other), encoding="utf-8")

    del mine.data["drop"]
    mine.data["projects"]["x"]["path"] = "/x2"
    mine.save()

    data = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert data["theirs"] == 3 and data["keep"] == 1 and "drop" not in data
    assert data["projects"] == {"x": {"path": "/x2"}, "y": {"path": "/y2"}}


def _projects_config(vault):
    cfg_file = config_mod.CONFIG_FILE
    cfg_file.write_text(json.dumps(
        {"vault_path": str(vault),
         "projects": {"p1": {"path": "/p1"}, "p2": {"path": "/p2"}}}), encoding="utf-8")
    return cfg_file


def test_merge_that_would_be_invalid_is_refused_without_writing(tmp_path):
    """SING-25 / M4: two `project set` with one prefix each pass their own check."""
    import pytest

    vault = tmp_path / "vault"
    vault.mkdir()
    cfg_file = _projects_config(vault)
    first = config_mod.load_config(str(vault))
    second = config_mod.load_config(str(vault))
    first.data["projects"]["p1"]["ticket_prefix"] = "ABC"
    second.data["projects"]["p2"]["ticket_prefix"] = "ABC"
    first.save()
    with pytest.raises(config_mod.ConfigError) as exc:
        second.save()
    assert "already used" in " ".join(exc.value.lines)
    data = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert data["projects"]["p1"]["ticket_prefix"] == "ABC"
    assert "ticket_prefix" not in data["projects"]["p2"]
    assert config_mod.validate_config(data)["errors"] == []
    config_mod.load_config(str(vault))           # still loads: exit 2 is gone


def test_preexisting_errors_do_not_block_a_repairing_save(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg_file = _projects_config(vault)
    broken = json.loads(cfg_file.read_text(encoding="utf-8"))
    broken["projects"]["p1"]["ticket_prefix"] = broken["projects"]["p2"]["ticket_prefix"] = "ABC"
    cfg = config_mod.Config(vault=vault, data=json.loads(json.dumps(broken)))
    cfg._baseline = json.loads(json.dumps(broken))
    cfg_file.write_text(json.dumps(broken), encoding="utf-8")
    cfg.data["projects"]["p2"]["ticket_prefix"] = "XYZ"
    cfg.save()
    data = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert data["projects"]["p2"]["ticket_prefix"] == "XYZ"


def test_break_stale_lock_leaves_fresh_and_breaks_old(tmp_path):
    import os
    import time

    from singularity.platform import fslock

    lock = tmp_path / "x.lock"
    lock.mkdir()
    assert fslock.break_stale_lock(lock, 10) is False and lock.exists()
    old = time.time() - 60
    os.utime(lock, (old, old))
    assert fslock.break_stale_lock(lock, 10) is True and not lock.exists()
    assert not list(tmp_path.iterdir())          # no tombstone, no breaker left


def test_stale_break_does_not_remove_a_fresh_lock_taken_in_the_gap(tmp_path, monkeypatch):
    """SING-25 / L2: between 'judged stale' and the removal another waiter broke
    the old lock and a new holder created a fresh one under the same name."""
    import os
    import time

    from singularity.platform import fslock

    lock = tmp_path / "x.lock"
    lock.mkdir()
    old = time.time() - 60
    os.utime(lock, (old, old))
    real_rename = os.rename

    def racing(src, dst):
        if Path(src) == lock:
            lock.rmdir()
            lock.mkdir()                          # the new holder's FRESH lock
        return real_rename(src, dst)

    monkeypatch.setattr(fslock.os, "rename", racing)
    assert fslock.break_stale_lock(lock, 10) is False
    assert lock.is_dir(), "a fresh lock was deleted by a stale-break"


def test_project_set_and_add_report_a_refused_save_without_traceback(
        tmp_path, monkeypatch, capsys):
    """SING-25: another `mb` took the prefix between our check and our save."""
    from singularity import cli

    vault = tmp_path / "vault"
    vault.mkdir()
    cfg_file = _projects_config(vault)
    holder = {"cfg": config_mod.load_config(str(vault))}
    monkeypatch.setattr(cli, "load_config", lambda vault_arg=None: holder["cfg"])
    other = json.loads(cfg_file.read_text(encoding="utf-8"))
    other["projects"]["p2"]["ticket_prefix"] = "ABC"
    cfg_file.write_text(json.dumps(other), encoding="utf-8")

    assert cli.main(["project", "set", "p1", "--ticket-prefix", "ABC"]) == 1
    err = capsys.readouterr().err
    assert "not saved" in err and "already used" in err and "Traceback" not in err
    # a fresh `mb` process that read the file before the other edit
    cfg = config_mod.Config(vault=vault, data=json.loads(json.dumps(
        {"projects": {"p1": {"path": "/p1"}, "p2": {"path": "/p2"}}})))
    cfg._baseline = json.loads(json.dumps(cfg.data))
    holder["cfg"] = cfg
    assert cli.main(["project", "add", "p3", str(tmp_path), "--ticket-prefix", "ABC"]) == 1
    assert "not saved" in capsys.readouterr().err
    assert json.loads(cfg_file.read_text(encoding="utf-8")) == other
