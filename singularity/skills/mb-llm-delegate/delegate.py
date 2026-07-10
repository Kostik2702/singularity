#!/usr/bin/env python
"""Delegate a single-shot generation task to the running local model.

Orchestrator (Claude/you) plans, reviews and drives tools; the local model is a
pure text/code generator reached over its Anthropic-compatible /v1/messages
endpoint. No tool-use is sent, so the small model can't loop or mis-call tools.

Usage:
    python delegate.py --task "<instruction>" [--system "<sys>"]
        [--infile <path>]   embed a file's current content into the prompt
        [--outfile <path>]  write the model's returned text (md fences stripped)
        [--max-tokens N]    default 6000
        [--print]           also echo the returned text to stdout
        [--keep-partial]    on max_tokens truncation, save partial text to
                            <outfile>.partial instead of discarding it

Exit codes: 0 success; 1 empty output; 2 truncated at max_tokens; other
non-zero on transport/HTTP error (via sys.exit(str)). --outfile is only ever
written on success, via atomic replace — it is never left partially written
or overwritten with truncated/empty content.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

DEFAULT_SYSTEM = (
    "You are an expert software engineer. When asked for a file, output ONLY the "
    "complete file contents — no markdown fences, no commentary, no explanation.")


def endpoint():
    """(host, port, model) from `mb llm status --json`; fall back to 127.0.0.1:8080."""
    host, port, model = "127.0.0.1", 8080, "local"
    try:
        out = subprocess.run(["mb", "llm", "status", "--json"],
                             capture_output=True, text=True, timeout=20)
        d = json.loads(out.stdout or "{}")
        if d.get("server_alive") is False:
            sys.exit("[delegate] local model is not running — 'mb llm start <model>'")
        port = d.get("server_port") or port
        model = d.get("model") or model
    except FileNotFoundError:
        pass                       # mb not on PATH — use defaults
    except (json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(f"[delegate] warn: could not read mb status ({exc}); using {host}:{port}",
              file=sys.stderr)
    return host, port, model


def call_model(host, port, model, system, task, max_tokens):
    body = {"model": model, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": task}]}
    req = urllib.request.Request(
        f"http://{host}:{port}/v1/messages", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-api-key": "local",
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"[delegate] HTTP {e.code}: {e.read().decode()[:400]}")
    except urllib.error.URLError as e:
        sys.exit(f"[delegate] cannot reach {host}:{port}: {e}")
    text = "".join(b.get("text", "") for b in d.get("content", [])
                   if b.get("type") == "text")     # drop <thinking> blocks
    return text, d.get("stop_reason"), d.get("usage", {})


def clean_output(text):
    """Strip a wrapping markdown fence and/or a leaked <file>...</file> wrapper
    (the model sometimes echoes the prompt's embedding tags)."""
    m = re.search(r"```[a-zA-Z0-9]*\s*\n(.*?)```", text, re.S)
    text = (m.group(1) if m else text).strip()
    m = re.search(r"<file>\s*\n?(.*?)\n?\s*</file>", text, re.S)
    if m:
        text = m.group(1).strip()
    return text


def atomic_write(path, content):
    """Write `content` to `path` without ever leaving a partial/corrupt file:
    write to a sibling temp file, flush+fsync, then atomically replace."""
    body = content if content.endswith("\n") else content + "\n"
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def decide_output(text, stop, outfile, keep_partial=False):
    """Decide what to do with the model's raw response before touching
    `outfile`. Returns (exit_code, message-or-None). Never mutates an
    existing outfile on empty output or max_tokens truncation."""
    if not text.strip():
        return 1, f"[delegate] empty output (stop_reason={stop}) — outfile left untouched"
    if stop == "max_tokens":
        msg = "[delegate] ERROR: truncated at max_tokens — raise --max-tokens; outfile left untouched"
        if outfile and keep_partial:
            partial = f"{outfile}.partial"
            atomic_write(partial, clean_output(text))
            msg += f" (partial saved to {partial})"
        return 2, msg
    if outfile:
        atomic_write(outfile, clean_output(text))
    return 0, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--system", default=DEFAULT_SYSTEM)
    ap.add_argument("--infile")
    ap.add_argument("--outfile")
    ap.add_argument("--max-tokens", type=int, default=6000)
    ap.add_argument("--print", action="store_true")
    ap.add_argument("--keep-partial", action="store_true",
                     help="on max_tokens truncation, save the partial text to "
                          "<outfile>.partial instead of discarding it")
    a = ap.parse_args()

    task = a.task
    if a.infile:
        with open(a.infile, encoding="utf-8") as f:
            current = clean_output(f.read())      # tolerate an already-polluted file
        task = (f"Current contents of {a.infile}:\n\n<file>\n{current}\n</file>\n\n"
                f"{a.task}\n\nReturn ONLY the raw file starting with its first real "
                f"line. Do NOT wrap the output in <file> tags or markdown fences.")

    host, port, model = endpoint()
    text, stop, usage = call_model(host, port, model, a.system, task, a.max_tokens)

    code, msg = decide_output(text, stop, a.outfile, keep_partial=a.keep_partial)

    if text.strip():
        print(f"[delegate] model={model} stop={stop} out_tokens={usage.get('output_tokens')}",
              file=sys.stderr)
    if msg:
        print(msg, file=sys.stderr)
    if code == 0 and a.outfile:
        print(f"[delegate] wrote {a.outfile} ({len(clean_output(text))} bytes)", file=sys.stderr)
    if a.print or not a.outfile:
        sys.stdout.write(text)
    sys.exit(code)


if __name__ == "__main__":
    main()
