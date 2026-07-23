"""GRK-00 reusable contract probe for the Grok Build CLI.

Re-captures the evidence behind tests/fixtures/grok/ against the currently
installed `grok` binary, fully offline and fully isolated:

- builds a throwaway HOME / USERPROFILE / GROK_HOME tree with sentinel
  artifacts (AGENTS.md, rules, skills, hooks, config.toml);
- serves a local fake OpenAI-compatible endpoint (`/v1/chat/completions`)
  so authenticated turns never touch xAI or real credentials;
- runs headless scenarios (basic turn, tool call, stop-gate block,
  subagent spawn, streaming) and dumps every hook payload;
- leaves all raw artifacts in the output dir for manual redaction into
  tests/fixtures/grok/.

Never reads or writes the real ~/.grok or ~/.claude. Windows caveat (observed
on grok 0.2.111): the Claude-compat agents/hooks/plugins scanners resolve the
profile dir via WinAPI and IGNORE the env overrides, so on Windows the real
~/.claude is still *scanned* by grok itself; this probe only ever *writes*
inside its own tree.

Usage:
    python tools/grok_contract_probe.py --out D:/grk-probe [--grok PATH]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8399

HOOKDUMP = '''\
import json, os, sys, time
label = sys.argv[1] if len(sys.argv) > 1 else "unknown"
raw = sys.stdin.read()
try:
    payload = json.loads(raw)
except Exception:
    payload = {"_raw": raw[:4000]}
rec = {"label": label, "stdin": payload,
       "env": {k: v for k, v in os.environ.items()
               if k.startswith("GROK_") or k == "CLAUDE_PROJECT_DIR"}}
outdir = os.environ["PROBE_HOOKLOG"]
os.makedirs(outdir, exist_ok=True)
with open(os.path.join(outdir, "%s-%d.json" % (label, time.time_ns())), "w",
          encoding="utf-8") as f:
    json.dump(rec, f, ensure_ascii=False, indent=1)
'''

STOPGATE = '''\
import json, os, sys
payload = json.loads(sys.stdin.read())
with open(os.path.join(os.environ["PROBE_HOOKLOG"], "stopgate.jsonl"), "a",
          encoding="utf-8") as f:
    f.write(json.dumps(payload) + "\\n")
if payload.get("reason") == "end_turn" and not payload.get("stopHookActive"):
    print(json.dumps({"decision": "block",
                      "reason": "SENTINEL-STOP-BLOCK: run one more round"}))
'''


class FakeModel(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible chat endpoint with scenario markers."""

    log_path: Path  # set on the class before serving

    def log_message(self, *args):
        pass

    def _log(self, entry):
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._log({"method": "GET", "path": self.path})
        self._json(200, {"object": "list",
                         "data": [{"id": "probe", "object": "model"}]})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            body = {}
        self._log({"method": "POST", "path": self.path, "body": body})
        if "chat/completions" not in self.path:
            self._json(404, {"error": {"message": "unknown path"}})
            return

        users = [str(m.get("content", "")) for m in body.get("messages", [])
                 if m.get("role") == "user"]
        already = any(m.get("role") == "tool" for m in body.get("messages", []))
        rid = "chatcmpl-" + uuid.uuid4().hex[:12]

        if any("SUBAGENT" in u for u in users) and not already:
            args = {"prompt": "Reply with one word: done",
                    "description": "probe subagent",
                    "subagent_type": "general-purpose", "background": False}
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_" + uuid.uuid4().hex[:8], "type": "function",
                 "function": {"name": "spawn_subagent",
                              "arguments": json.dumps(args)}}]}
            finish = "tool_calls"
        elif any("TOOLCALL" in u for u in users) and not already:
            args = {"command": "echo PROBE-TOOL-OK", "description": "probe echo"}
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_" + uuid.uuid4().hex[:8], "type": "function",
                 "function": {"name": "run_terminal_command",
                              "arguments": json.dumps(args)}}]}
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": "PROBE-REPLY-OK"}
            finish = "stop"

        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            base = {"id": rid, "object": "chat.completion.chunk",
                    "created": int(time.time()), "model": "probe"}
            if finish == "tool_calls":
                call = message["tool_calls"][0]
                deltas = [
                    {"role": "assistant", "tool_calls": [
                        {"index": 0, "id": call["id"], "type": "function",
                         "function": {"name": call["function"]["name"],
                                      "arguments": ""}}]},
                    {"tool_calls": [{"index": 0, "function": {
                        "arguments": call["function"]["arguments"]}}]},
                ]
            else:
                deltas = [{"role": "assistant", "content": "PROBE-REPLY-OK"}]
            for delta in deltas:
                chunk = {**base, "choices": [
                    {"index": 0, "delta": delta, "finish_reason": None}]}
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            final = {**base, "choices": [
                {"index": 0, "delta": {}, "finish_reason": finish}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                          "total_tokens": 15}}
            self.wfile.write(b"data: " + json.dumps(final).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self._json(200, {
                "id": rid, "object": "chat.completion",
                "created": int(time.time()), "model": "probe",
                "choices": [{"index": 0, "message": message,
                             "finish_reason": finish}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                          "total_tokens": 15}})


def build_iso_tree(iso: Path) -> dict:
    """Create the isolated home tree with sentinel artifacts. BOM-less UTF-8
    everywhere: grok silently skips BOM'd hook JSON and mis-parses BOM'd
    SKILL.md frontmatter (observed on 0.2.111)."""
    home = iso / "home"
    grokhome = iso / "grokhome"
    proj = iso / "proj"
    hooklog = iso / "hooklog"
    for d in (home, grokhome / "skills" / "probe-native", grokhome / "hooks",
              grokhome / "rules", proj, hooklog):
        d.mkdir(parents=True, exist_ok=True)
    (home / "SENTINEL.txt").write_text("probe-sentinel", encoding="utf-8")
    (grokhome / "AGENTS.md").write_text(
        "# SENTINEL-GROKHOME-AGENTS\nGlobal rule sentinel.\n", encoding="utf-8")
    (grokhome / "rules" / "probe-rule.md").write_text(
        "SENTINEL-GROKHOME-RULESDIR\n", encoding="utf-8")
    (grokhome / "skills" / "probe-native" / "SKILL.md").write_text(
        "---\nname: probe-native\ndescription: native sentinel skill\n---\n"
        "SENTINEL-NATIVE-SKILL\n", encoding="utf-8")
    (grokhome / "config.toml").write_text(
        '[cli]\nauto_update = false\n\n'
        '[features]\ntelemetry = false\nremote_fetch = false\n\n'
        '[models]\ndefault = "probe-model"\n\n'
        '[model.probe-model]\nmodel = "probe"\n'
        f'base_url = "http://127.0.0.1:{PORT}/v1"\n'
        'name = "Probe Model"\napi_key = "dummy-key-probe"\n'
        'context_window = 32000\n', encoding="utf-8")
    (iso / "hookdump.py").write_text(HOOKDUMP, encoding="utf-8")
    (iso / "stopgate.py").write_text(STOPGATE, encoding="utf-8")
    return {"home": home, "grokhome": grokhome, "proj": proj, "hooklog": hooklog}


def hook_json(iso: Path, events: dict) -> None:
    """Write GROK_HOME/hooks/probe.json. Windows contract: unquoted argv0,
    forward slashes; never a bare double-quoted path, never $env: tokens."""
    python = Path(sys.executable).as_posix()
    hooks = {}
    for event, script in events.items():
        cmd = f"{python} {(iso / script[0]).as_posix()}"
        if len(script) > 1:
            cmd += " " + script[1]
        hooks[event] = [{"hooks": [{"type": "command", "command": cmd}]}]
    (iso / "grokhome" / "hooks" / "probe.json").write_text(
        json.dumps({"hooks": hooks}, indent=1), encoding="utf-8")


def run_grok(grok: str, tree: dict, iso: Path, prompt: str, *extra) -> subprocess.CompletedProcess:
    import os
    env = {**os.environ,
           "HOME": str(tree["home"]), "USERPROFILE": str(tree["home"]),
           "GROK_HOME": str(tree["grokhome"]),
           "GROK_DISABLE_AUTOUPDATER": "1",
           "PROBE_HOOKLOG": str(tree["hooklog"])}
    cmd = [grok, "-p", prompt, "-m", "probe-model", *extra]
    return subprocess.run(cmd, cwd=tree["proj"], env=env,
                          capture_output=True, text=True, timeout=300)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="output/workspace dir (wiped)")
    parser.add_argument("--grok", default="grok", help="grok binary path")
    args = parser.parse_args()

    iso = Path(args.out)
    if iso.exists():
        shutil.rmtree(iso)
    tree = build_iso_tree(iso)

    FakeModel.log_path = iso / "requests.log"
    server = ThreadingHTTPServer(("127.0.0.1", PORT), FakeModel)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    version = subprocess.run([args.grok, "--version"], capture_output=True,
                             text=True).stdout.strip()
    print(f"probing: {version}")
    results = {"version": version, "scenarios": {}}

    def record(name, proc):
        results["scenarios"][name] = {
            "exit": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-2000:],
        }
        print(f"  {name}: exit={proc.returncode}")

    # 1. discovery
    import os
    env = {**os.environ, "HOME": str(tree["home"]),
           "USERPROFILE": str(tree["home"]),
           "GROK_HOME": str(tree["grokhome"]),
           "GROK_DISABLE_AUTOUPDATER": "1"}
    inspect = subprocess.run([args.grok, "inspect", "--json"], cwd=tree["proj"],
                             env=env, capture_output=True, text=True, timeout=120)
    (iso / "inspect.json").write_text(inspect.stdout, encoding="utf-8")
    record("inspect", inspect)

    # 2. basic headless json + hook payload capture
    hook_json(iso, {"SessionStart": ("hookdump.py", "SessionStart"),
                    "UserPromptSubmit": ("hookdump.py", "UserPromptSubmit"),
                    "PreToolUse": ("hookdump.py", "PreToolUse"),
                    "PostToolUse": ("hookdump.py", "PostToolUse"),
                    "SessionEnd": ("hookdump.py", "SessionEnd")})
    record("basic_json", run_grok(args.grok, tree, iso, "Say hello",
                                  "--output-format", "json"))

    # 3. tool call round (PreToolUse/PostToolUse payloads)
    record("toolcall", run_grok(args.grok, tree, iso,
                                "TOOLCALL please run the probe tool",
                                "--yolo", "--output-format", "json"))

    # 4. stop-gate block
    hook_json(iso, {"Stop": ("stopgate.py",)})
    record("stopgate", run_grok(args.grok, tree, iso, "Say hello",
                                "--output-format", "json"))

    # 5. subagent spawn
    hook_json(iso, {})
    record("subagent", run_grok(args.grok, tree, iso,
                                "SUBAGENT please spawn the probe subagent",
                                "--yolo", "--output-format", "json"))

    # 6. streaming
    record("stream", run_grok(args.grok, tree, iso, "Say hello",
                              "--output-format", "streaming-json"))

    server.shutdown()
    (iso / "probe_results.json").write_text(
        json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"artifacts: {iso} (hooklog/, requests.log, inspect.json, "
          f"grokhome/sessions/, probe_results.json)")
    failures = [n for n, r in results["scenarios"].items() if r["exit"] != 0]
    if failures:
        print(f"FAILED scenarios: {failures}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
