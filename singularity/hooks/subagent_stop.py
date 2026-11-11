"""Claude SubagentStop hook: send a subagent back once if its contract is unmet.

The subagent's own transcript (`agent_transcript_path`, not the parent's
`transcript_path`) is checked by singularity.machine.agent_contract. Every run
of an agent that HAS a contract is logged to <vault>/.mb/agent-contract.jsonl:
that file is the compliance metric. Any failure ends in exit 0 with no output.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main() -> int:
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="ignore").strip()
        event = json.loads(raw or "{}")
        from singularity.config import Config, read_config_tolerant, resolve_vault
        from singularity.machine import agent_contract

        agent_type = str(event.get("agent_type") or "")
        if agent_type not in agent_contract.CONTRACTS:
            return 0
        vault = resolve_vault(None)
        if vault is None or not vault.is_dir():
            return 0
        retry = bool(event.get("stop_hook_active"))
        enforce = Config(vault=vault, data=read_config_tolerant()).get("agent_contract_enforce") is not False
        names = agent_contract.missing(agent_type, Path(str(event.get("agent_transcript_path", ""))),
                                       str(event.get("last_assistant_message") or ""),
                                       str(event.get("cwd") or ""))
        blocked = bool(names) and enforce and not retry
        verdict = "error" if names is None else ("missing" if names else "ok")
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "session": event.get("session_id"), "agent_id": event.get("agent_id"),
                 "agent_type": agent_type, "verdict": verdict, "missing": names or [],
                 "blocked": blocked, "retry": retry}
        log = vault / ".mb" / "agent-contract.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if blocked:
            sys.stdout.reconfigure(encoding="utf-8")
            print(json.dumps({"decision": "block", "reason": agent_contract.reason(names)},
                             ensure_ascii=False))
    except Exception:  # noqa: BLE001 - a broken hook must never trap an agent
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
