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

Exit non-zero on transport/HTTP error or empty output.
"""
import argparse
import json
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--system", default=DEFAULT_SYSTEM)
    ap.add_argument("--infile")
    ap.add_argument("--outfile")
    ap.add_argument("--max-tokens", type=int, default=6000)
    ap.add_argument("--print", action="store_true")
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
    if not text.strip():
        sys.exit(f"[delegate] empty output (stop_reason={stop})")

    print(f"[delegate] model={model} stop={stop} out_tokens={usage.get('output_tokens')}",
          file=sys.stderr)
    if stop == "max_tokens":
        print("[delegate] WARN: truncated at max_tokens — raise --max-tokens",
              file=sys.stderr)

    out = clean_output(text) if a.outfile else text
    if a.outfile:
        with open(a.outfile, "w", encoding="utf-8", newline="\n") as f:
            f.write(out if out.endswith("\n") else out + "\n")
        print(f"[delegate] wrote {a.outfile} ({len(out)} bytes)", file=sys.stderr)
    if a.print or not a.outfile:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
