# `mb llm code` + `mb llm setup` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One command runs Claude Code against the running local llama-server (`mb llm code`); one command auto-installs/repairs the whole local-LLM environment (`mb llm setup`), including llama.cpp Windows-CUDA download.

**Architecture:** Extend `singularity/llm.py` (pure helpers + orchestration, house style: `[mb] ...` stderr lines, int exit codes) and wire two subcommands into the existing `llm` subparser in `singularity/cli.py`. No router — llama-server natively serves Anthropic `/v1/messages` (verified live 2026-07-09); Claude Code connects via env overlay.

**Tech Stack:** Python stdlib only (`urllib`, `zipfile`, `ssl`, `shutil`, `subprocess`); `truststore` opportunistic (TLS-MITM machine); pytest + monkeypatch (no network, no real binaries in tests).

**Spec:** `docs/superpowers/specs/2026-07-09-mb-llm-code-and-setup-design.md`

## Global Constraints

- Branch: `feat/mb-llm-supervisor`; repo `D:\Singularity`; run tests with `.venv/Scripts/python.exe -m pytest`.
- Stdlib only; `truststore` via guarded import, never a hard dependency.
- Errors: single actionable `[mb] ...` line on stderr, never a traceback; exit codes `0` ok / `1` operational failure.
- `ANTHROPIC_BASE_URL` is a bare origin (`http://host:port`) — **no `/v1` suffix**.
- Set only `ANTHROPIC_AUTH_TOKEN` (never also `ANTHROPIC_API_KEY` — auth conflict).
- Downloads: never to C:; default install dir `E:/llama.cpp`; partial files cleaned on error.
- `mb llm setup` is idempotent — second run on a healthy env changes nothing and exits 0.
- All new code in `singularity/llm.py`; tests appended to `tests/test_llm.py` (existing conventions: `_llm(tmp_path)` cfg-dict factory, `Config(vault=tmp_path, data={...})`, monkeypatch `llm.proc.*`).

## File Structure

- `singularity/llm.py` — modify: new imports (`os`, `re`, `shutil`, `ssl`, `subprocess`, `zipfile`); new constants + 9 functions (`resolve_auth_token`, `code`, `fix_model_args`, `pick_release_assets`, `_ssl_context`, `fetch_json`, `download`, `unpack_zip`, `server_bin_ok`, `setup`); one hint line in `start()`.
- `singularity/cli.py` — modify: `code` + `setup` subparsers (after `llm logs`, ~line 78); dispatch branches in `cmd_llm` (~line 228).
- `singularity/config.py` — modify: `Config.save()` becomes atomic (tmp + `os.replace`).
- `tests/test_llm.py` — modify: new test sections per task.
- `tests/test_config.py` — modify: one atomic-save test.

---

### Task 1: Token chain + `code()` core

**Files:**
- Modify: `singularity/llm.py` (imports at top; functions after `logs`/end of file)
- Test: `tests/test_llm.py`

**Interfaces:**
- Produces: `resolve_auth_token(llm: dict, environ: Mapping) -> str`; `code(cfg, extra: list[str] | None = None) -> int`; module constant `DEFAULT_AUTH_TOKEN = "local"`. Task 2 wires `code` into the CLI.
- Consumes: existing `_llm_cfg`, `load_state`, `proc.is_running`, `LLMConfigError`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_llm.py`)

```python
# --- mb llm code -----------------------------------------------------------
import types


def test_resolve_auth_token_chain(tmp_path):
    # env wins over config; config wins over default
    assert llm.resolve_auth_token({"auth_token": "cfg"},
                                  {"ANTHROPIC_AUTH_TOKEN": "env"}) == "env"
    assert llm.resolve_auth_token({"auth_token": "cfg"}, {}) == "cfg"
    assert llm.resolve_auth_token({}, {}) == "local"
    assert llm.resolve_auth_token({"auth_token": ""}, {}) == "local"  # empty -> default


def _code_ready(tmp_path, monkeypatch, alive=True):
    cfg = Config(vault=tmp_path, data={"llm": {
        "server_bin": "x", "server_host": "127.0.0.1", "server_port": 8080,
        "models": {}}})
    llm.write_state(cfg, {"model": "qwythos9b", "server_pid": 111,
                          "server_port": 8080})
    monkeypatch.setattr(llm.proc, "is_running", lambda pid: alive)
    return cfg


def test_code_no_state_returns_1(tmp_path, monkeypatch, capsys):
    cfg = Config(vault=tmp_path, data={"llm": {"server_bin": "x", "models": {}}})
    assert llm.code(cfg) == 1
    assert "mb llm start" in capsys.readouterr().err


def test_code_dead_server_returns_1(tmp_path, monkeypatch, capsys):
    cfg = _code_ready(tmp_path, monkeypatch, alive=False)
    assert llm.code(cfg) == 1
    assert "mb llm start" in capsys.readouterr().err


def test_code_missing_claude_cli_returns_1(tmp_path, monkeypatch, capsys):
    cfg = _code_ready(tmp_path, monkeypatch)
    monkeypatch.setattr(llm.shutil, "which", lambda name: None)
    assert llm.code(cfg) == 1
    assert "npm install -g @anthropic-ai/claude-code" in capsys.readouterr().err


def test_code_runs_claude_with_env_overlay_and_passthrough(tmp_path, monkeypatch):
    cfg = _code_ready(tmp_path, monkeypatch)
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    calls = {}

    def fake_run(argv, env=None):
        calls["argv"], calls["env"] = argv, env
        return types.SimpleNamespace(returncode=7)

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    rc = llm.code(cfg, ["--", "--resume"])       # leading REMAINDER "--" stripped
    assert rc == 7                               # claude's exit code propagated
    assert calls["argv"] == ["/bin/claude", "--resume"]
    env = calls["env"]
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8080"   # bare origin
    assert env["ANTHROPIC_AUTH_TOKEN"] == "local"
    assert env["ANTHROPIC_MODEL"] == "qwythos9b"
    assert env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] == "1"
    assert "PATH" in env                         # os.environ inherited


def test_code_env_token_not_clobbered(tmp_path, monkeypatch):
    cfg = _code_ready(tmp_path, monkeypatch)
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "user-token")
    seen = {}
    monkeypatch.setattr(llm.subprocess, "run",
                        lambda argv, env=None: seen.update(env=env)
                        or types.SimpleNamespace(returncode=0))
    assert llm.code(cfg) == 0
    assert seen["env"]["ANTHROPIC_AUTH_TOKEN"] == "user-token"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd D:/Singularity && .venv/Scripts/python.exe -m pytest tests/test_llm.py -k "resolve_auth or code_" -v`
Expected: FAIL/ERROR — `AttributeError: module 'singularity.llm' has no attribute 'resolve_auth_token'` (etc.)

- [ ] **Step 3: Implement in `singularity/llm.py`**

Add to the import block at the top (`llm.py:6-16`):

```python
import os
import shutil
import subprocess
```

Append after `logs` (end of module):

```python
# --- mb llm code -----------------------------------------------------------

DEFAULT_AUTH_TOKEN = "local"  # dummy; local server without --api-key ignores auth


def resolve_auth_token(llm: dict, environ) -> str:
    """env ANTHROPIC_AUTH_TOKEN > config llm.auth_token > "local" (first non-empty)."""
    return (environ.get("ANTHROPIC_AUTH_TOKEN")
            or llm.get("auth_token")
            or DEFAULT_AUTH_TOKEN)


def code(cfg, extra: list[str] | None = None) -> int:
    """Run Claude Code (foreground, inherits stdio) against the running local model."""
    try:
        llm = _llm_cfg(cfg)
    except LLMConfigError as exc:
        print(f"[mb] {exc}", file=sys.stderr)
        return 1
    state = load_state(cfg)
    if not state or not proc.is_running(state.get("server_pid") or -1):
        print("[mb] no local model running — run 'mb llm start <model>'",
              file=sys.stderr)
        return 1
    claude = shutil.which("claude")
    if not claude:
        print("[mb] 'claude' CLI not found — npm install -g @anthropic-ai/claude-code",
              file=sys.stderr)
        return 1
    host = llm.get("server_host", "127.0.0.1")
    port = state.get("server_port") or int(llm.get("server_port", 8080))
    env = dict(os.environ)
    env.update({
        # bare origin — Claude Code appends /v1/messages itself
        "ANTHROPIC_BASE_URL": f"http://{host}:{port}",
        "ANTHROPIC_AUTH_TOKEN": resolve_auth_token(llm, os.environ),
        "ANTHROPIC_MODEL": str(state.get("model")),
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    })
    args = list(extra or [])
    if args[:1] == ["--"]:                 # argparse.REMAINDER keeps the separator
        args = args[1:]
    return subprocess.run([claude, *args], env=env).returncode
```

Note: the local variable `llm` shadows nothing harmful here — the module refers to itself only via already-bound names; this mirrors `start()`'s existing `llm = _llm_cfg(cfg)` style.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd D:/Singularity && .venv/Scripts/python.exe -m pytest tests/test_llm.py -v`
Expected: all PASS (new + pre-existing).

- [ ] **Step 5: Commit**

```bash
cd D:/Singularity && git add singularity/llm.py tests/test_llm.py && git commit -m "feat(llm): mb llm code core — env overlay + auth token chain"
```

---

### Task 2: CLI wiring for `code` + start hint

**Files:**
- Modify: `singularity/cli.py:76-78` (subparser), `singularity/cli.py:227-229` (dispatch)
- Modify: `singularity/llm.py:199-203` (`start()` output hint)
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `llm.code(cfg, extra)` from Task 1.
- Produces: CLI `mb llm code [claude_args...]`; `start()` prints `Run Claude Code on it: mb llm code`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_llm.py`)

```python
def test_cli_llm_code_dispatches_with_remainder(tmp_path, monkeypatch):
    cfg = Config(vault=tmp_path, data={})
    monkeypatch.setattr(cli, "load_config", lambda vault_arg: cfg)
    seen = {}
    monkeypatch.setattr("singularity.llm.code",
                        lambda c, extra: seen.update(extra=extra) or 0)
    assert cli.main(["llm", "code", "--", "--resume"]) == 0
    assert seen["extra"] == ["--", "--resume"]


def test_start_prints_code_hint(tmp_path, monkeypatch, capsys):
    cfg = _ready_llm(tmp_path)
    monkeypatch.setattr(llm.proc, "spawn_detached", lambda argv, log: 4242)
    monkeypatch.setattr(llm, "poll_health", lambda *a, **k: True)
    monkeypatch.setattr(llm.proc, "is_running", lambda pid: False)
    assert llm.start(cfg, "qwen") == 0
    assert "mb llm code" in capsys.readouterr().out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd D:/Singularity && .venv/Scripts/python.exe -m pytest tests/test_llm.py -k "cli_llm_code or code_hint" -v`
Expected: FAIL — argparse `invalid choice: 'code'`; hint assertion fails.

- [ ] **Step 3: Implement**

`singularity/cli.py` — after the `logs` subparser block (line 78), add:

```python
    llc = llsub.add_parser("code", help="run Claude Code on the local model")
    llc.add_argument("claude_args", nargs=argparse.REMAINDER,
                     help="extra args passed to claude (use -- before flags)")
```

(`argparse` is already imported in cli.py.)

`singularity/cli.py` — in `cmd_llm` before `return 2` (line 229), add:

```python
    if args.llm_action == "code":
        return llm.code(cfg, args.claude_args)
```

`singularity/llm.py` — in `start()`, the no-router `else` branch currently:

```python
    else:
        print(f"OpenAI-compatible endpoint: http://{host}:{port}/v1")
```

becomes:

```python
    else:
        print(f"OpenAI-compatible endpoint: http://{host}:{port}/v1")
        print("Run Claude Code on it: mb llm code")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd D:/Singularity && .venv/Scripts/python.exe -m pytest tests/test_llm.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd D:/Singularity && git add singularity/cli.py singularity/llm.py tests/test_llm.py && git commit -m "feat(cli): mb llm code subcommand + start hint"
```

---

### Task 3: `fix_model_args` — valued `-fa` auto-fix

**Files:**
- Modify: `singularity/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Produces: `fix_model_args(args: list[str]) -> tuple[list[str], list[str]]` — (new args, human-readable fixes applied; empty fixes ⇒ args unchanged). Consumed by `setup()` in Task 6.

- [ ] **Step 1: Write the failing tests**

```python
# --- mb llm setup: args sanity ---------------------------------------------

def test_fix_model_args_bare_fa_before_flag():
    fixed, fixes = llm.fix_model_args(["-ngl", "99", "-fa", "--jinja"])
    assert fixed == ["-ngl", "99", "-fa", "on", "--jinja"]
    assert fixes == ["-fa -> -fa on"]


def test_fix_model_args_bare_fa_last():
    fixed, fixes = llm.fix_model_args(["-fa"])
    assert fixed == ["-fa", "on"] and len(fixes) == 1


def test_fix_model_args_valued_fa_untouched():
    for val in ("on", "off", "auto"):
        fixed, fixes = llm.fix_model_args(["-fa", val, "--jinja"])
        assert fixed == ["-fa", val, "--jinja"] and fixes == []


def test_fix_model_args_long_form_and_no_fa():
    assert llm.fix_model_args(["--flash-attn", "--jinja"])[0] == \
        ["--flash-attn", "on", "--jinja"]
    args = ["-ngl", "99", "--jinja"]
    assert llm.fix_model_args(args) == (args, [])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd D:/Singularity && .venv/Scripts/python.exe -m pytest tests/test_llm.py -k fix_model_args -v`
Expected: FAIL — `no attribute 'fix_model_args'`.

- [ ] **Step 3: Implement** (append to `singularity/llm.py`)

```python
# --- mb llm setup -----------------------------------------------------------

_VALUED_FLASH_ATTN = ("-fa", "--flash-attn")   # llama-server >= b9xxx: needs on|off|auto


def fix_model_args(args: list[str]) -> tuple[list[str], list[str]]:
    """Repair known argv pitfalls; returns (new_args, fixes). Currently: a bare
    -fa/--flash-attn swallows the next flag as its value — insert 'on'."""
    out: list[str] = []
    fixes: list[str] = []
    for i, a in enumerate(args):
        out.append(a)
        if a in _VALUED_FLASH_ATTN:
            nxt = args[i + 1] if i + 1 < len(args) else None
            if nxt is None or str(nxt).startswith("-"):
                out.append("on")
                fixes.append(f"{a} -> {a} on")
    return out, fixes
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd D:/Singularity && .venv/Scripts/python.exe -m pytest tests/test_llm.py -k fix_model_args -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd D:/Singularity && git add singularity/llm.py tests/test_llm.py && git commit -m "feat(llm): fix_model_args repairs bare -fa (valued flag since llama.cpp b99xx)"
```

---

### Task 4: `pick_release_assets` — GitHub release asset selection

**Files:**
- Modify: `singularity/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Produces: `pick_release_assets(assets: list[dict]) -> tuple[dict, dict | None]` — `(main_zip_asset, cudart_zip_asset_or_None)`; each asset is a GitHub API object (uses keys `"name"`, `"browser_download_url"`). Raises `LLMConfigError` listing asset names when no Windows-CUDA-x64 zip matches. Consumed by `setup()` in Task 6.

- [ ] **Step 1: Write the failing tests**

```python
# --- mb llm setup: release asset selection ----------------------------------

def _asset(name):
    return {"name": name, "browser_download_url": f"https://gh/{name}"}


def test_pick_release_assets_single_match_with_cudart():
    assets = [_asset("llama-b9999-bin-win-cpu-x64.zip"),
              _asset("llama-b9999-bin-win-cuda-12.4-x64.zip"),
              _asset("cudart-llama-bin-win-cuda-12.4-x64.zip"),
              _asset("llama-b9999-bin-macos-arm64.zip")]
    main, cudart = llm.pick_release_assets(assets)
    assert main["name"] == "llama-b9999-bin-win-cuda-12.4-x64.zip"
    assert cudart["name"] == "cudart-llama-bin-win-cuda-12.4-x64.zip"


def test_pick_release_assets_prefers_highest_cuda():
    assets = [_asset("llama-b9999-bin-win-cuda-11.7-x64.zip"),
              _asset("llama-b9999-bin-win-cuda-12.4-x64.zip")]
    main, cudart = llm.pick_release_assets(assets)
    assert "12.4" in main["name"] and cudart is None


def test_pick_release_assets_no_match_raises_with_names():
    with pytest.raises(llm.LLMConfigError) as exc:
        llm.pick_release_assets([_asset("llama-b9999-bin-macos-arm64.zip")])
    assert "macos-arm64" in str(exc.value)   # available names listed for debugging
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd D:/Singularity && .venv/Scripts/python.exe -m pytest tests/test_llm.py -k pick_release -v`
Expected: FAIL — `no attribute 'pick_release_assets'`.

- [ ] **Step 3: Implement** (append to `singularity/llm.py`; add `import re` to the top import block)

```python
_WIN_CUDA_ZIP = re.compile(r"bin-win-cuda.*x64\.zip$")
_CUDA_VER = re.compile(r"cuda-?(\d+(?:\.\d+)?)")


def pick_release_assets(assets: list[dict]) -> tuple[dict, dict | None]:
    """Pick the Windows-CUDA-x64 server zip (highest CUDA version) and its
    matching cudart runtime zip, if published. Naming drifts across releases —
    on no match, raise with the available names so the pattern can be updated."""
    def cuda_ver(name: str) -> float:
        m = _CUDA_VER.search(name)
        return float(m.group(1)) if m else 0.0

    mains = [a for a in assets
             if _WIN_CUDA_ZIP.search(a["name"])
             and not a["name"].startswith("cudart-")]
    if not mains:
        names = ", ".join(a["name"] for a in assets) or "(no assets)"
        raise LLMConfigError(f"no win-cuda-x64 zip in release assets: {names}")
    main = max(mains, key=lambda a: cuda_ver(a["name"]))
    cudarts = [a for a in assets
               if a["name"].startswith("cudart-") and _WIN_CUDA_ZIP.search(a["name"])]
    cudart = next((a for a in cudarts
                   if cuda_ver(a["name"]) == cuda_ver(main["name"])),
                  cudarts[0] if cudarts else None)
    return main, cudart
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd D:/Singularity && .venv/Scripts/python.exe -m pytest tests/test_llm.py -k pick_release -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd D:/Singularity && git add singularity/llm.py tests/test_llm.py && git commit -m "feat(llm): pick_release_assets selects win-cuda zip from llama.cpp release"
```

---

### Task 5: Network/zip helpers — `_ssl_context`, `fetch_json`, `download`, `unpack_zip`, `server_bin_ok`

**Files:**
- Modify: `singularity/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Produces:
  - `_ssl_context() -> ssl.SSLContext | None` — truststore context if importable (TLS-MITM machine), else `None` (urllib default).
  - `fetch_json(url: str, ctx=None) -> dict` — GET with GitHub headers, parse JSON.
  - `download(url: str, dest: Path, ctx=None) -> None` — streamed to `dest.part`, atomic rename, partial cleanup on error, MiB progress prints.
  - `unpack_zip(zip_path: Path, target: Path) -> None` — extract; strip single top-level dir; reject path traversal.
  - `server_bin_ok(path: Path) -> bool` — `--version` exits 0 within 30s.
- Consumed by `setup()` in Task 6.

- [ ] **Step 1: Write the failing tests**

```python
# --- mb llm setup: net/zip helpers ------------------------------------------
import io
import zipfile as _zipfile


def _make_zip(tmp_path, entries, name="a.zip"):
    """entries: {archive_name: content_bytes}"""
    p = tmp_path / name
    with _zipfile.ZipFile(p, "w") as zf:
        for n, data in entries.items():
            zf.writestr(n, data)
    return p


def test_unpack_zip_flattens_single_top_dir(tmp_path):
    z = _make_zip(tmp_path, {"llama-b9999/llama-server.exe": b"exe",
                             "llama-b9999/lib/ggml.dll": b"dll"})
    target = tmp_path / "out"
    llm.unpack_zip(z, target)
    assert (target / "llama-server.exe").read_bytes() == b"exe"
    assert (target / "lib" / "ggml.dll").read_bytes() == b"dll"


def test_unpack_zip_root_files_kept_as_is(tmp_path):
    z = _make_zip(tmp_path, {"llama-server.exe": b"exe", "ggml.dll": b"dll"})
    target = tmp_path / "out"
    llm.unpack_zip(z, target)
    assert (target / "llama-server.exe").exists() and (target / "ggml.dll").exists()


def test_unpack_zip_rejects_traversal(tmp_path):
    z = _make_zip(tmp_path, {"../evil.txt": b"x"})
    with pytest.raises(llm.LLMConfigError):
        llm.unpack_zip(z, tmp_path / "out")


def test_download_writes_atomically_and_cleans_partial(tmp_path, monkeypatch):
    class _Resp(io.BytesIO):
        headers = {"Content-Length": "4"}
        def __enter__(self): return self
        def __exit__(self, *a): return False

    dest = tmp_path / "f.zip"
    monkeypatch.setattr(llm.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(b"data"))
    llm.download("https://x/f.zip", dest)
    assert dest.read_bytes() == b"data"
    assert not dest.with_suffix(".zip.part").exists()

    def boom(*a, **k):
        raise OSError("net down")
    monkeypatch.setattr(llm.urllib.request, "urlopen", boom)
    with pytest.raises(OSError):
        llm.download("https://x/g.zip", tmp_path / "g.zip")
    assert not (tmp_path / "g.zip.part").exists()   # partial cleaned


def test_server_bin_ok(tmp_path, monkeypatch):
    exe = tmp_path / "llama-server.exe"
    assert llm.server_bin_ok(exe) is False          # missing -> OSError -> False
    exe.write_text("x")
    monkeypatch.setattr(llm.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=0))
    assert llm.server_bin_ok(exe) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd D:/Singularity && .venv/Scripts/python.exe -m pytest tests/test_llm.py -k "unpack_zip or test_download or server_bin_ok" -v`
Expected: FAIL — missing attributes.

- [ ] **Step 3: Implement** (append to `singularity/llm.py`; add `import ssl` and `import zipfile` to the top import block)

```python
def _ssl_context():
    """truststore (OS cert store) if available — this machine sits behind a
    TLS-intercepting proxy; plain certifi bundles fail on GitHub downloads."""
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return None


def fetch_json(url: str, ctx=None) -> dict:
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "singularity-mb"})
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download(url: str, dest: Path, ctx=None) -> None:
    """Stream url to dest via a .part file (atomic finish, partial cleanup)."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60, context=ctx) as resp, \
                open(tmp, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r[mb] {dest.name}: {done >> 20}/{total >> 20} MiB",
                          end="", flush=True)
            if total:
                print()
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def unpack_zip(zip_path: Path, target: Path) -> None:
    """Extract into target, stripping a single top-level directory if present."""
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        if any(n.startswith("/") or ".." in n.split("/") for n in names):
            raise LLMConfigError(f"unsafe path in archive: {zip_path.name}")
        tops = {n.split("/", 1)[0] for n in names}
        prefix = (tops.copy().pop() + "/"
                  if len(tops) == 1 and all("/" in n for n in names) else "")
        for name in names:
            rel = name[len(prefix):] if name.startswith(prefix) else name
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)


def server_bin_ok(path: Path) -> bool:
    """True iff the binary exists and `--version` exits 0 (sanity, not health)."""
    try:
        return subprocess.run([str(path), "--version"], capture_output=True,
                              timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd D:/Singularity && .venv/Scripts/python.exe -m pytest tests/test_llm.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd D:/Singularity && git add singularity/llm.py tests/test_llm.py && git commit -m "feat(llm): net/zip helpers for setup (truststore ssl, atomic download, safe unzip)"
```

---

### Task 6: `Config.save` atomic + `setup()` orchestration + CLI wiring

**Files:**
- Modify: `singularity/config.py:103-108` (`save`)
- Modify: `singularity/llm.py` (constants + `setup`)
- Modify: `singularity/cli.py` (subparser + dispatch)
- Test: `tests/test_config.py`, `tests/test_llm.py`

**Interfaces:**
- Consumes: `fix_model_args` (T3), `pick_release_assets` (T4), `_ssl_context`/`fetch_json`/`download`/`unpack_zip`/`server_bin_ok` (T5), `has_chat_template` (existing).
- Produces: `setup(cfg, dir_override: str | None = None) -> int`; CLI `mb llm setup [--dir PATH]`; constants `DEFAULT_INSTALL_DIR = "E:/llama.cpp"`, `LLAMA_RELEASE_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"`.

- [ ] **Step 1: Write the failing tests**

`tests/test_config.py` (append; mirror its existing imports):

```python
def test_save_is_atomic_no_tmp_left(tmp_path):
    cfg = Config(vault=tmp_path, data={"llm": {"server_port": 1}})
    cfg.save()
    assert json.loads(cfg.config_path.read_text(encoding="utf-8"))["llm"]["server_port"] == 1
    leftovers = [p for p in cfg.config_path.parent.iterdir()
                 if p.name.startswith("config") and p.suffix != ".json"]
    assert leftovers == []
```

`tests/test_llm.py` (append):

```python
# --- mb llm setup: orchestration --------------------------------------------

def _setup_env(tmp_path, monkeypatch, *, bin_ok=True, gguf_exists=True,
               claude=True, models=None):
    gguf = tmp_path / "m.gguf"
    if gguf_exists:
        gguf.write_text("x")
    if models is None:
        models = {"m1": {"gguf": str(gguf), "args": ["-fa", "--jinja"]}}
    exe = tmp_path / "bin" / "llama-server.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("x")
    cfg = Config(vault=tmp_path, data={"llm": {
        "server_bin": str(exe), "server_host": "127.0.0.1",
        "server_port": 8080, "models": models}})
    monkeypatch.setattr(llm, "server_bin_ok", lambda p: bin_ok)
    monkeypatch.setattr(llm.shutil, "which",
                        lambda name: "/bin/claude" if claude else None)
    return cfg


def test_setup_healthy_env_all_ok_exit_0(tmp_path, monkeypatch, capsys):
    cfg = _setup_env(tmp_path, monkeypatch)
    assert llm.setup(cfg) == 0
    out = capsys.readouterr().out
    assert "fixed" in out           # -fa repaired on first run
    # idempotent: second run fixes nothing
    assert llm.setup(cfg) == 0
    assert cfg.data["llm"]["models"]["m1"]["args"] == ["-fa", "on", "--jinja"]


def test_setup_creates_missing_llm_section(tmp_path, monkeypatch):
    cfg = Config(vault=tmp_path, data={})
    monkeypatch.setattr(llm, "server_bin_ok", lambda p: True)
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/bin/claude")
    assert llm.setup(cfg, dir_override=str(tmp_path / "inst")) == 0
    saved = json.loads(cfg.config_path.read_text(encoding="utf-8"))
    assert saved["llm"]["server_port"] == 8080
    assert str(tmp_path / "inst") in saved["llm"]["server_bin"]


def test_setup_missing_gguf_warns_but_exit_0(tmp_path, monkeypatch, capsys):
    cfg = _setup_env(tmp_path, monkeypatch, gguf_exists=False)
    assert llm.setup(cfg) == 0
    assert "m.gguf" in capsys.readouterr().out    # warned with the path


def test_setup_downloads_when_binary_broken(tmp_path, monkeypatch, capsys):
    cfg = _setup_env(tmp_path, monkeypatch, bin_ok=False)
    monkeypatch.setattr(llm.sys, "platform", "win32")
    release = {"assets": [
        {"name": "llama-b9999-bin-win-cuda-12.4-x64.zip",
         "browser_download_url": "https://gh/main.zip"},
        {"name": "cudart-llama-bin-win-cuda-12.4-x64.zip",
         "browser_download_url": "https://gh/cudart.zip"}]}
    downloaded = []
    monkeypatch.setattr(llm, "fetch_json", lambda url, ctx=None: release)
    monkeypatch.setattr(llm, "download",
                        lambda url, dest, ctx=None: downloaded.append(url)
                        or dest.write_bytes(b"z"))
    unpacked = []
    monkeypatch.setattr(llm, "unpack_zip",
                        lambda z, t: unpacked.append(z.name))
    # after "unpack", the verify probe must succeed:
    monkeypatch.setattr(llm, "server_bin_ok",
                        lambda p: bool(unpacked))    # ok only post-unpack
    assert llm.setup(cfg) == 0
    assert downloaded == ["https://gh/main.zip", "https://gh/cudart.zip"]
    assert len(unpacked) == 2


def test_setup_posix_missing_binary_is_blocking(tmp_path, monkeypatch, capsys):
    cfg = _setup_env(tmp_path, monkeypatch, bin_ok=False)
    monkeypatch.setattr(llm.sys, "platform", "linux")
    assert llm.setup(cfg) == 1
    assert "manual" in capsys.readouterr().out.lower()


def test_cli_llm_setup_dispatches(tmp_path, monkeypatch):
    cfg = Config(vault=tmp_path, data={})
    monkeypatch.setattr(cli, "load_config", lambda vault_arg: cfg)
    seen = {}
    monkeypatch.setattr("singularity.llm.setup",
                        lambda c, dir_override=None: seen.update(d=dir_override) or 0)
    assert cli.main(["llm", "setup", "--dir", "X:/lc"]) == 0
    assert seen["d"] == "X:/lc"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd D:/Singularity && .venv/Scripts/python.exe -m pytest tests/test_llm.py -k setup -v && .venv/Scripts/python.exe -m pytest tests/test_config.py -k atomic -v`
Expected: FAIL — `no attribute 'setup'`; atomic test may PASS already only if leftovers check trivially holds — verify it runs.

- [ ] **Step 3: Implement**

`singularity/config.py` — replace `save()` body (lines 103-108):

```python
    def save(self) -> None:
        self.mb_dir.mkdir(parents=True, exist_ok=True)
        merged = {**DEFAULT_CONFIG, **self.data}
        tmp = self.config_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        tmp.replace(self.config_path)
```

(`test_save_is_atomic_no_tmp_left`'s leftover scan now has teeth: a crash between write and replace leaves `config.json.tmp`, never a torn `config.json`.)

`singularity/llm.py` — append:

```python
DEFAULT_INSTALL_DIR = "E:/llama.cpp"       # big files never on C: (machine rule)
LLAMA_RELEASE_API = ("https://api.github.com/repos/"
                     "ggml-org/llama.cpp/releases/latest")
_SERVER_EXE = "llama-server.exe"


def _default_llm_section(install_dir: Path) -> dict:
    return {
        "server_bin": str(install_dir / _SERVER_EXE),
        "server_host": "127.0.0.1",
        "server_port": 8080,
        "server_ready_path": "/health",
        "health_timeout_s": 300,
        "models": {},
    }


def setup(cfg, dir_override: str | None = None) -> int:
    """Verify/repair the local-LLM environment. Idempotent. Exit 0 unless a
    blocking item remains (binary unavailable and not auto-installable)."""
    lines: list[tuple[str, str]] = []      # (level, message)

    def report(level: str, msg: str) -> None:
        lines.append((level, msg))
        print(f"[{level}] {msg}")

    changed = False
    section = cfg.data.get("llm")
    if not section:
        install_dir = Path(dir_override or DEFAULT_INSTALL_DIR)
        section = _default_llm_section(install_dir)
        cfg.data["llm"] = section
        changed = True
        report("fixed", "created llm config section "
                        f"(server_bin -> {section['server_bin']})")
    else:
        report("ok", "llm config section present")

    # --- llama.cpp binary --------------------------------------------------
    server_bin = Path(section["server_bin"])
    target_dir = Path(dir_override) if dir_override else server_bin.parent
    if server_bin.exists() and server_bin_ok(server_bin):
        report("ok", f"llama-server works: {server_bin}")
    elif sys.platform == "win32":
        try:
            ctx = _ssl_context()
            release = fetch_json(LLAMA_RELEASE_API, ctx)
            main, cudart = pick_release_assets(release.get("assets") or [])
            target_dir.mkdir(parents=True, exist_ok=True)
            for asset in filter(None, (main, cudart)):
                dest = target_dir / asset["name"]
                report("fixed", f"downloading {asset['name']}")
                download(asset["browser_download_url"], dest, ctx)
                unpack_zip(dest, target_dir)
                dest.unlink(missing_ok=True)
            exes = sorted(target_dir.rglob(_SERVER_EXE))
            new_bin = exes[0] if exes else target_dir / _SERVER_EXE
            if not server_bin_ok(new_bin):
                raise LLMConfigError(
                    f"downloaded binary failed --version: {new_bin}")
            section["server_bin"] = str(new_bin)
            changed = True
            report("fixed", f"installed llama-server -> {new_bin}")
        except (LLMConfigError, OSError, urllib.error.URLError) as exc:
            report("fail", f"llama.cpp install failed: {exc} — "
                           f"download manually into {target_dir}")
    else:
        report("fail", f"llama-server missing at {server_bin} — install "
                       "manually (auto-download is Windows-only): "
                       "https://github.com/ggml-org/llama.cpp/releases")

    # --- model args sanity + gguf presence ---------------------------------
    models = section.get("models") or {}
    if not models:
        report("warn", 'model registry empty — add e.g. "models": '
                       '{"name": {"gguf": "E:/models/x.gguf", '
                       '"args": ["-ngl", "99", "-fa", "on", "--jinja"]}}')
    for name, model in models.items():
        fixed_args, fixes = fix_model_args(list(model.get("args") or []))
        if fixes:
            model["args"] = fixed_args
            changed = True
            report("fixed", f"{name}: " + "; ".join(fixes))
        if not has_chat_template({"models": {name: model}}, name):
            report("warn", f"{name}: args lack --jinja/--chat-template — "
                           "tool-calling won't work")
        gguf = Path(model.get("gguf") or "")
        if gguf.exists():
            report("ok", f"{name}: gguf present")
        else:
            report("warn", f"{name}: gguf missing: {gguf}")

    # --- claude CLI ---------------------------------------------------------
    if shutil.which("claude"):
        report("ok", "claude CLI in PATH")
    else:
        report("warn", "claude CLI not found — "
                       "npm install -g @anthropic-ai/claude-code "
                       "(blocks 'mb llm code' only)")

    if changed:
        cfg.save()

    counts = {lvl: sum(1 for l, _ in lines if l == lvl)
              for lvl in ("ok", "fixed", "warn", "fail")}
    print(f"[mb] setup: {counts['ok']} ok, {counts['fixed']} fixed, "
          f"{counts['warn']} warnings, {counts['fail']} blocking")
    return 1 if counts["fail"] else 0
```

`singularity/cli.py` — after the `code` subparser (Task 2), add:

```python
    llset = llsub.add_parser("setup", help="verify/install local-LLM environment")
    llset.add_argument("--dir", default=None,
                       help="llama.cpp install dir (default: E:/llama.cpp)")
```

and in `cmd_llm`:

```python
    if args.llm_action == "setup":
        return llm.setup(cfg, dir_override=args.dir)
```

- [ ] **Step 4: Run the full suite**

Run: `cd D:/Singularity && .venv/Scripts/python.exe -m pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd D:/Singularity && git add singularity/llm.py singularity/cli.py singularity/config.py tests/test_llm.py tests/test_config.py && git commit -m "feat(llm): mb llm setup — idempotent env verify/repair + llama.cpp auto-install"
```

---

### Task 7: Manual E2E verification (real machine)

**Files:** none (verification only; fixes go back through the relevant task's cycle)

**Interfaces:** consumes everything above; produces evidence for verification-before-completion.

- [ ] **Step 1: `mb llm setup` on the live machine**

Run: `cd D:/Singularity && .venv/Scripts/mb.exe llm setup`
Expected: `llama-server works` ok; models `-fa` already valued (no-op); `qwen27b: gguf missing: E:/models/ThinkingCap...` warning; `claude CLI in PATH` ok; exit 0. Second run → identical output, `0 fixed`.

- [ ] **Step 2: `mb llm code` interactive smoke**

Precondition: `mb llm status` shows qwythos9b alive (else `mb llm start qwythos9b`).
Run: `cd D:/Singularity && .venv/Scripts/mb.exe llm code`
In the session: ask one trivial question, then exit. Verify:
- reply arrives (slow is fine — 9B thinking model);
- `mb llm logs server -n 20` shows the corresponding `POST /v1/messages`;
- exit code of `mb llm code` equals claude's own exit.

- [ ] **Step 3: Real download path (one-time)**

Run: `ren "E:\llama.cpp\llama-server.exe" llama-server.exe.bak` then `cd D:/Singularity && .venv/Scripts/mb.exe llm setup`
Expected: GitHub fetch → progress lines → unpack → `installed llama-server -> ...`; `server_bin` updated in `.mb/config.json`; exit 0. (Keeps the new build; delete `.bak` after `mb llm start qwythos9b` succeeds against it. If the new build breaks startup: restore `.bak`, set `server_bin` back, file the issue.)

- [ ] **Step 4: Record evidence + update memory**

Capture outputs of steps 1–3 in the final report. Then ask the user: "Do you want to update memory?" (per phase-completion protocol) — insight candidates: native `/v1/messages` discovery (no router needed), deprecated `ANTHROPIC_SMALL_FAST_MODEL`.

---

## Self-Review

**Spec coverage:** code() env overlay + token chain → T1; CLI + hint → T2; `-fa` auto-fix → T3; asset selection → T4; truststore/download/unpack/verify → T5; config section create/atomic write/gguf/claude checks/summary/exit codes + POSIX blocking → T6; manual E2E incl. real download → T7. Non-goals honored (no HF downloads, no router install, no keyring). ✓
**Placeholders:** none — every step has full code/commands. ✓
**Type consistency:** `resolve_auth_token(llm, environ)` used in T1 `code()`; `fix_model_args` tuple unpacked in T6; `pick_release_assets` returns `(dict, dict|None)` consumed by T6 loop via `filter(None, ...)`; `server_bin_ok(Path)` monkeypatched with same arity in tests. ✓
