"""A guarded root without a `mb note new` type is an order to do the impossible.

`vault_guard` flags any NEW file created in a managed knowledge root with a
client's Write tool and points at `mb note new` / `mb learn` instead (on by
default since v0.6.3). That advice only holds while every one of those roots
actually HAS a sanctioned way to create a file in it — otherwise the guard
warns about the only route available, the warning becomes noise, and the habit
it is meant to build gets trained away.

That was the state on 2026-08-04: `_moc/` and `changelog/` were guarded with no
`note new` type behind them. `changelog/` would have hit it on the first day of
the next month, when `changelog/YYYY-MM.md` does not exist yet.

Also pins the skill's own pre-write gate, since the skill is what the assistant
actually reads (VSO-02).
"""
import re
from pathlib import Path

import pytest

from singularity.machine import vault_guard

ENGINE = Path(__file__).resolve().parents[1]
SKILL = ENGINE / "singularity" / "skills" / "mb-memory" / "SKILL.md"

# Notes arrive here by being MOVED out of learnings/active, never by creation,
# so there is nothing for `note new` to offer.
ARRIVES_BY_MOVE = {"learnings/archive"}


def _folder_by_type() -> dict[str, str]:
    """The literal from `commands.memory.cmd_note`, read from source.

    Reached by parsing rather than by calling: `cmd_note` needs a Config, a
    vault and argparse args to run, and the invariant under test is about the
    table itself."""
    src = (ENGINE / "singularity" / "commands" / "memory.py").read_text(encoding="utf-8")
    block = re.search(r"folder_by_type = \{(.*?)\}", src, re.S)
    assert block, "cmd_note no longer declares folder_by_type as a literal"
    return dict(re.findall(r'"([^"]+)":\s*"([^"]+)"', block.group(1)))


def test_every_guarded_root_has_a_note_new_type():
    covered = set(_folder_by_type().values())
    missing = [r for r in vault_guard.MANAGED_ROOTS
               if r not in covered and r not in ARRIVES_BY_MOVE]
    assert not missing, (
        f"guarded roots with no `mb note new` type: {missing} — either add a "
        f"type or stop guarding the root")


def test_note_types_point_at_real_managed_roots():
    """The other direction: a type pointing at an unguarded folder means the
    two lists have drifted."""
    roots = set(vault_guard.MANAGED_ROOTS)
    stray = {t: f for t, f in _folder_by_type().items() if f not in roots}
    assert not stray, f"note types outside the guarded roots: {stray}"


@pytest.mark.parametrize("type_name", ["moc", "changelog"])
def test_the_roots_that_were_missing_are_named(type_name):
    """Named explicitly so removing them fails loudly rather than shrinking
    coverage back to what it was."""
    assert type_name in _folder_by_type()


# --- the skill is what the assistant actually reads --------------------------

def test_skill_gate_sends_new_files_through_the_cli():
    body = SKILL.read_text(encoding="utf-8")
    assert "mb note new" in body
    assert "GATE" in body, "the pre-write gate heading is gone"
    # The old wording ("крупные записи — редактируй .md напрямую") is what
    # authorized the bypass this gate exists to stop.
    assert "редактируй `.md` напрямую" not in body


def test_skill_does_not_hardcode_a_vault_path():
    """It opened with one machine's Windows vault path, which is wrong on every
    POSIX host — the same class as issue #14, in the file the assistant reads
    first. Any drive-letter path here is that mistake returning."""
    body = SKILL.read_text(encoding="utf-8")
    assert not re.search(r"[A-Za-z]:\\", body), \
        "a machine-specific absolute path is back in the skill"
