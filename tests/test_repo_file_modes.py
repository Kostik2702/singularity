"""The executable bit is part of the tree, so losing it breaks a fresh clone
for everyone (issue #9: `./install/install.sh` -> `permission denied`, exit 126,
on the very first command the README documents).

Nothing in git protects that bit — `.gitattributes` cannot set it, only the
index mode can — and it is easy to drop by copying a file through a filesystem
that does not carry modes. So it gets a test.

Reads the INDEX mode (`git ls-files -s`), never the filesystem: on Windows the
working-copy mode is meaningless, but the index mode is what a POSIX clone
receives.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]

# Files a user (or a shim) invokes directly, so they must arrive executable.
MUST_BE_EXECUTABLE = ["install/install.sh"]


def _index_mode(path: str) -> str | None:
    """The mode git stores for `path`, or None when it is not tracked."""
    out = subprocess.run(["git", "ls-files", "-s", "--", path],
                         cwd=str(ENGINE), capture_output=True, text=True,
                         check=False)
    line = out.stdout.strip()
    return line.split(" ", 1)[0] if line else None


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or not (ENGINE / ".git").exists(),
    reason="not a git work tree (sdist/wheel checkout)")


@pytest.mark.parametrize("rel", MUST_BE_EXECUTABLE)
def test_entrypoint_is_executable_in_the_index(rel):
    mode = _index_mode(rel)
    assert mode is not None, f"{rel} is not tracked by git"
    assert mode == "100755", (
        f"{rel} is stored as {mode}; a fresh POSIX clone cannot run it. "
        f"Fix with: git update-index --chmod=+x {rel}")
