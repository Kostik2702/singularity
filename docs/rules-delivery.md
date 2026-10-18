# Rules delivery — where each rule actually acts

The registry of "rule → the point where it is spoken". The machine half is
`singularity/machine/rules_delivery.py` (`DELIVERIES`, `check()`), and the check
is `tests/test_rules_delivery.py`. This document explains the scheme; the list of
injections is not duplicated here by hand, because two copies drift — read the
module.

## The split everything rests on

| | Lives in | What is there | If it disappears |
|---|---|---|---|
| **Knowledge** | the vault (`rules/`, `decisions/`, `learnings/`) | why a rule exists: the incident, the cost, the origin, the owner | recall drops, the machine's behaviour does not change |
| **Mechanism** | the engine (`singularity/`) | the text that is read at the moment of action | the machine loses the rule — that is a defect |

A rule that must act cannot live in the vault. Swapping, emptying or losing the
vault are legal states: it is memory, not part of execution. Until 2026-08-05
eight subagents read `AGENT_SHARED_PREAMBLE.md` out of `{{VAULT}}/rules/`, which
made their entire rule set a property of which vault was configured; on a machine
with no vault they would have started with no rules at all, and silently.

## The four surfaces

| Surface | Who reads it | File | When to add here |
|---|---|---|---|
| SessionStart fragment | the main thread, every session | `singularity/machine/hooks_state.py` | the rule is universal AND the failure happened in the main thread |
| Preamble §1-§8 | every subagent (§1+§2 mandatory) | `singularity/agents/AGENT_SHARED_PREAMBLE.md` | the rule applies to any agent work |
| Agent file | one agent | `singularity/agents/<name>.md` | the rule is about the role, not about work in general |
| SKILL.md | whoever does that work | `singularity/skills/<skill>/SKILL.md` | the rule is about a class of task |

A fifth, not textual: **a docstring plus a test next to the code** — for when the
rule is about how that code gets written (`hooks must never use load_config`,
`index readers resolve through index_files`). The test IS the delivery.

## Budget

SessionStart is paid for in every session. The figure is not kept in prose: it
went 18% out of date in two weeks and nobody noticed. The aggregate ceiling on
the fragments is an executable gate, `test_the_fragments_have_an_aggregate_ceiling_too`
(`tests/test_rules_delivery.py`); raising it takes a change that raises this line
too. As of 2026-10-18 the fragments the gate counts occupy ~4.6k characters,
ceiling 4700 (raised from 4150 by the owner for the delegation fragment). The gate
does not count `MEASURED_AXIS_FRAGMENT` (~0.7k): every session actually pays ~5.3k.
Counting it needs the owner's decision on a new ceiling or a cut.
`CAPTURE_HOOKS_FRAGMENT` is the size unit: the test requires a new fragment to be
smaller than two of them. That is why a fragment states a **class** of failure
rather than one bullet per case: "green is not the same as verified" covers the
exit code behind a pipe, the blind invariant, and the check that skipped itself.

The preamble is budgeted differently — lazy-load: everyone reads §1+§2, and
§3-§8 arrive on triggers named in the agent's file. A universal rule goes in §2,
a narrow one in its own section. Precedent: the exit-code rule was first put in
§7, and a coverage check showed that exactly one agent out of eight had an
explicit trigger for §7.

## The order for adding a rule

1. `mb note new rule "<title>"` — the knowledge into the vault: the incident, the
   cost, the GATE question "would this rule have caught precisely this failure?".
2. Name the point from the table above. Several points — one source, injected.
3. Write the text **executably**: the action in the line itself, not a reference.
   At the moment a command is written, nobody follows a link.
4. Check the counter-text: if the opposite is already written at that point, the
   instruction wins. Precedent: `mb-memory/SKILL.md` explicitly permitted the
   bypass the guard flags; and `planning-council` estimated in human-hours while
   the rule about agent sessions was in force.
5. Add the entry to `DELIVERIES` and **check the test with a planted violation** —
   remove the injection, watch it go red, put it back.
6. Add a "Where it is grafted" section, naming the point, to the vault note.

## Rules with no point, and rules that are not ours

Two lists sit beside `DELIVERIES`, both in the same module:

- **`WITHOUT_A_POINT`** — the rule is true but the surface is gone. Its first
  occupant arrived immediately: v0.12.0 deleted `reel` together with its skill,
  and the rule about the `aeval` envelope lost both of its points in one commit.
  The entry stays visible on purpose — a silently deleted line reads exactly like
  a rule that was never delivered, and the difference matters if the tool returns.
- **`NOT_OURS_TO_DELIVER`** — rules of other projects (singularity-ui), of the
  video engine, Windows specifics on a macOS machine, and code invariants already
  delivered by a docstring with a test. These are not gaps in Singularity: they
  have a different executor, a different platform, or delivery straight in the
  code.

Together they give a balance that adds up: 72 notes = delivered + orphaned + not
ours. Without those lists, "untouched" and "covered somewhere else" are
indistinguishable.

## What the registry does not catch

A rule that does not exist yet. The registry closes the gap between knowledge and
action; it does not hunt for unknown defects. And it checks the **presence** of
the text, not that the model followed it: the marker is in place means the rule
was delivered, no more.
