"""The real-machine-config guard in conftest must witness, never write.

It once restored the file it saw change. A legitimate `mb project set` made by
another process during a test run looked like the test's own write and was
rolled back, taking two ticket prefixes with it. Run in a subprocess session
with a faked HOME, so the machine's real config is never involved."""
import os
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]

INNER = '''
from singularity.config import CONFIG_FILE      # binds the REAL path: the guard's blind spot

def test_writes_the_real_path():
    CONFIG_FILE.write_text("CLOBBERED", encoding="utf-8")
'''


def test_guard_fails_the_offender_and_leaves_the_file_as_found(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    real = home / ".singularity.json"
    real.write_text("{}", encoding="utf-8")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "conftest.py").write_text(
        (ENGINE / "tests" / "conftest.py").read_text(encoding="utf-8")
        .replace("parents[1]", "parents[0]"), encoding="utf-8")
    (proj / "test_inner.py").write_text(INNER, encoding="utf-8")
    (proj / "singularity").symlink_to(ENGINE / "singularity", target_is_directory=True)
    (proj / "install").symlink_to(ENGINE / "install", target_is_directory=True)
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                          "test_inner.py"], cwd=proj, env=env, capture_output=True,
                         text=True, timeout=120)
    assert run.returncode != 0, run.stdout
    assert "REAL machine config" in run.stdout
    assert real.read_text(encoding="utf-8") == "CLOBBERED", "the guard rolled the file back"
