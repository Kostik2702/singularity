"""mb llm — supervise a local llama-server (+ optional router) for Claude Code.

State lives in <vault>/.mb/llm.state.json; process control is delegated to
singularity.platform.proc (cross-platform detached spawn/kill).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

from .platform import proc


class LLMConfigError(Exception):
    """Missing/invalid [llm] config section or a referenced path is absent."""


def _llm_cfg(cfg) -> dict:
    data = cfg.get("llm") or {}
    if not data:
        raise LLMConfigError(
            'no [llm] config — add an "llm" section to .mb/config.json '
            "(server_bin + models)")
    return data


def build_server_argv(llm: dict, model_name: str) -> list[str]:
    server_bin = llm.get("server_bin")
    if not server_bin:
        raise LLMConfigError("llm.server_bin not set in config")
    models = llm.get("models") or {}
    model = models.get(model_name)
    if model is None:
        avail = ", ".join(models) or "(none)"
        raise LLMConfigError(f"unknown model '{model_name}' (have: {avail})")
    gguf = model.get("gguf")
    if not gguf:
        raise LLMConfigError(f"model '{model_name}' has no gguf path")
    host = llm.get("server_host", "127.0.0.1")
    port = int(llm.get("server_port", 8080))
    args = model.get("args") or []
    return [str(server_bin), "-m", str(gguf),
            "--host", str(host), "--port", str(port), *[str(a) for a in args]]


def validate_paths(llm: dict, model_name: str) -> None:
    server_bin = Path(llm["server_bin"])
    if not server_bin.exists():
        raise LLMConfigError(f"server_bin not found: {server_bin}")
    gguf = Path(llm["models"][model_name]["gguf"])
    if not gguf.exists():
        raise LLMConfigError(f"gguf not found: {gguf}")


def has_chat_template(llm: dict, model_name: str) -> bool:
    args = llm["models"][model_name].get("args") or []
    return any(a == "--jinja" or str(a).startswith("--chat-template") for a in args)


def state_path(cfg) -> Path:
    return cfg.mb_dir / "llm.state.json"


def _log(cfg, name: str) -> Path:
    return cfg.mb_dir / f"llm-{name}.log"


def load_state(cfg) -> dict | None:
    try:
        return json.loads(state_path(cfg).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_state(cfg, state: dict) -> None:
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    state_path(cfg).write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clear_state(cfg) -> None:
    state_path(cfg).unlink(missing_ok=True)


def poll_health(url: str, timeout_s: float, interval: float = 1.0,
                 pid: int | None = None) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        if pid is not None and not proc.is_running(pid):
            return False
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if 200 <= resp.status < 300:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def stop(cfg) -> int:
    """Stop the router (if any) then the server; clear state. Idempotent."""
    state = load_state(cfg)
    if not state:
        print("not running")
        return 0
    if state.get("router_pid"):
        proc.kill(state["router_pid"])
    if state.get("server_pid"):
        proc.kill(state["server_pid"])
    clear_state(cfg)
    print("stopped")
    return 0


def start(cfg, model_name: str) -> int:
    try:
        llm = _llm_cfg(cfg)
        argv = build_server_argv(llm, model_name)
        validate_paths(llm, model_name)
    except LLMConfigError as exc:
        print(f"[mb] {exc}", file=sys.stderr)
        return 1

    if not has_chat_template(llm, model_name):
        print("[mb] warning: model args lack --jinja/--chat-template; "
              "tool-calling may not work", file=sys.stderr)

    # switch: free VRAM by stopping any live server first
    existing = load_state(cfg)
    if existing and (proc.is_running(existing.get("server_pid") or -1)
                      or proc.is_running(existing.get("router_pid") or -1)):
        print(f"[mb] stopping active model '{existing.get('model')}' first")
        stop(cfg)

    timeout = float(llm.get("health_timeout_s", 120))
    host = llm.get("server_host", "127.0.0.1")
    port = int(llm.get("server_port", 8080))
    ready = llm.get("server_ready_path", "/health")

    try:
        server_pid = proc.spawn_detached(argv, _log(cfg, "server"))
    except Exception as exc:
        print(f"[mb] failed to start server: {exc}", file=sys.stderr)
        clear_state(cfg)
        return 1

    if not poll_health(f"http://{host}:{port}{ready}", timeout, pid=server_pid):
        proc.kill(server_pid)
        clear_state(cfg)
        print(f"[mb] server not healthy in {timeout:g}s — see {_log(cfg, 'server')}",
              file=sys.stderr)
        return 1

    state = {
        "model": model_name,
        "server_pid": server_pid,
        "server_port": port,
        "router_pid": None,
        "router_port": None,
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }

    router = llm.get("router")
    if router:
        r_host = router.get("host", "127.0.0.1")
        r_port = int(router.get("port", 3456))
        r_ready = router.get("ready_path", "/health")
        try:
            r_cmd = router.get("cmd")
            if not r_cmd:
                raise LLMConfigError("router.cmd not set")
            router_pid = proc.spawn_detached([str(a) for a in r_cmd],
                                             _log(cfg, "router"))
        except Exception as exc:
            print(f"[mb] failed to start router: {exc}", file=sys.stderr)
            proc.kill(server_pid)
            clear_state(cfg)
            return 1

        if not poll_health(f"http://{r_host}:{r_port}{r_ready}", timeout, pid=router_pid):
            proc.kill(router_pid)
            proc.kill(server_pid)
            clear_state(cfg)
            print(f"[mb] router not healthy in {timeout:g}s — see {_log(cfg, 'router')}",
                  file=sys.stderr)
            return 1
        state["router_pid"] = router_pid
        state["router_port"] = r_port

    write_state(cfg, state)
    print(f"[mb] started '{model_name}'")
    if router:
        print(f"export ANTHROPIC_BASE_URL=http://{router.get('host', '127.0.0.1')}:"
              f"{int(router.get('port', 3456))}")
    else:
        print(f"OpenAI-compatible endpoint: http://{host}:{port}/v1")
        print("Run Claude Code on it: mb llm code")
    return 0


def status(cfg, as_json: bool = False) -> int:
    state = load_state(cfg)
    if not state:
        print("not running")
        return 0
    server_alive = proc.is_running(state.get("server_pid") or -1)
    router_alive = (proc.is_running(state["router_pid"])
                    if state.get("router_pid") else None)
    if as_json:
        print(json.dumps({**state, "server_alive": server_alive,
                          "router_alive": router_alive},
                         ensure_ascii=False, indent=2))
        return 0
    print(f"model:  {state.get('model')}")
    print(f"server: pid {state.get('server_pid')} "
          f"({'alive' if server_alive else 'DEAD'}) port {state.get('server_port')}")
    if state.get("router_pid"):
        print(f"router: pid {state['router_pid']} "
              f"({'alive' if router_alive else 'DEAD'}) port {state.get('router_port')}")
    print(f"since:  {state.get('started_at')}")
    if not server_alive:
        print("[mb] server pid is dead — run 'mb llm stop' to clear stale state")
    return 0


def list_models(cfg) -> int:
    try:
        llm = _llm_cfg(cfg)
    except LLMConfigError as exc:
        print(f"[mb] {exc}", file=sys.stderr)
        return 1
    models = llm.get("models") or {}
    if not models:
        print("[mb] no models in config — add to llm.models in .mb/config.json")
        return 1
    state = load_state(cfg)
    active = state.get("model") if state else None
    for name, spec in models.items():
        mark = " (active)" if name == active else ""
        print(f"{name}{mark}  {spec.get('gguf')}")
    return 0


def logs(cfg, which: str = "server", n: int = 40) -> int:
    path = _log(cfg, which)
    if not path.exists():
        print(f"[mb] no log yet: {path}")
        return 0
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-n:]:
        print(line)
    return 0


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


_DRIVE_ABS = re.compile(r"^[A-Za-z]:")


def _unsafe_entry(name: str) -> bool:
    """True iff an archive entry could escape `target` on extraction — POSIX
    absolute, Windows drive-absolute, or containing a '..' segment (checked
    after normalizing backslashes to forward slashes, since pathlib treats
    both as separators on Windows)."""
    norm = name.replace("\\", "/")
    if norm.startswith("/") or _DRIVE_ABS.match(norm):
        return True
    return ".." in norm.split("/")


def unpack_zip(zip_path: Path, target: Path) -> None:
    """Extract into target, stripping a single top-level directory if present."""
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        if any(_unsafe_entry(n) for n in names):
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


# --- mb llm setup -----------------------------------------------------------

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
    if server_bin_ok(server_bin):
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
