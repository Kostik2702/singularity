"""GCLI-06: cross-platform edge cases for the Gemini integration — command quoting
and paths with spaces / Unicode. These run on the existing 3-OS × 3-Py CI matrix
(the whole Gemini suite does, via monkeypatched HOME + register_skills._link_dir),
so this file pins the specific quoting/encoding invariants that platform drift
would break."""
import importlib.util
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]


def _load(name, sub="install"):
    spec = importlib.util.spec_from_file_location(name, ENGINE / sub / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


register_gemini = _load("register_gemini")
client_registration = _load("client_registration")


def test_hook_command_quotes_paths_with_spaces(monkeypatch):
    """The hook command embeds the interpreter and script paths; both must be
    double-quoted so a space (e.g. C:\\Program Files) doesn't split the argv."""
    monkeypatch.setattr(register_gemini, "PYTHON", Path(r"C:\Program Files\py\python.exe"))
    monkeypatch.setattr(register_gemini, "ENGINE", Path(r"D:\My Vault\engine"))
    entry = register_gemini._hook_entry("gemini_session_start.py", "x")
    cmd = entry["hooks"][0]["command"]
    assert cmd.count('"') == 4                       # two quoted path segments
    assert cmd.startswith('"') and cmd.endswith('.py"')
    assert "Program Files" in cmd and "My Vault" in cmd


def test_profile_body_preserves_unicode_and_space_vault():
    vault = Path(r"D:\Проект Vault\memory brain")
    body = register_gemini._profile_body(vault)
    assert str(vault) in body
    assert register_gemini.BEGIN in body and register_gemini.END in body


def test_managed_block_roundtrip_with_unicode_vault(tmp_path):
    vault = Path(r"D:\Проект Vault\memory brain")
    body = register_gemini._profile_body(vault)
    f = tmp_path / "GEMINI.md"
    assert client_registration.merge_managed_block(f, body, register_gemini.BEGIN,
                                                    register_gemini.END) == "created"
    first = f.read_bytes()
    # refresh must be byte-stable and keep the Unicode path intact
    assert client_registration.merge_managed_block(f, body, register_gemini.BEGIN,
                                                    register_gemini.END) == "refreshed"
    assert f.read_bytes() == first
    assert "Проект Vault" in f.read_text(encoding="utf-8")


def test_resolves_into_with_spaces_and_unicode(tmp_path):
    root = tmp_path / "engine dir" / "скиллы"
    (root / "s").mkdir(parents=True)
    assert client_registration.resolves_into(root / "s", root) is True
    assert client_registration.resolves_into(tmp_path / "other", root) is False
