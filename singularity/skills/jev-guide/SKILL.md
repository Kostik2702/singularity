---
name: jev-guide
description: 'Jev (TypeSafe decision API) for the lead session - what it decides, how to call it, when to trust its confidence and what never to hand it. Use when the context shows a [jev] hint, when about to run mb jev pick, mb jev ask or mb jev status, when sending anything to Jev, or when the user mentions Jev or the decision API. Status is mb jev status in the shell and /jev status in the mission-control mod.'
---

# Jev - a second opinion that decides, never writes

Jev answers a yes/no, a choice or a score about a text, with a probability. It never writes code, prose or a tool call, and nothing here rewrites one of yours. It is off until the owner runs `mb jev on`. Pinned model: `jev-1.13.0`; the thresholds below are tuned against that one version.

Status: `mb jev status` (`--json` for the same fields as data). Inside the mission-control mod, `/jev status` prints it.

## The call

Every call goes through the CLI and nowhere else - the key, the deadline, the retries and the journal live there:

```
echo '{"state": "<text>", "questions": {"q": {"type": "noul", "instructions": "...", "criteria": {"true": "...", "false": "..."}}}}' | mb jev ask
```

Prints `{"answers", "ms", "cost", "model", "usage"}`, or `{"error": {...}}` and exit 1. Every question of one request is answered in parallel and does not see the others - put each independent judgement in its own question.

| Type | Answer | Criteria |
|---|---|---|
| `noul` | `{noul: p}` probability of yes | optional `{"true": .., "false": ..}` |
| `choice` | `{choice, confidence, probabilities}` | `{name: description}`, 1-255 options |
| `score` | `{score, confidence, probabilities}` expected level | list of levels, low to high, 1-10 |

Limits: request up to 64K tokens; state plus any one question up to 32K.

## Thresholds

- Confidence below 0.6: Jev does not decide, you do. Say so in one line and go on.
- A noul has no confidence of its own. Here: yes when p >= 0.5, confidence is max(p, 1-p), and 0.4 < p < 0.6 is disputed. (The vendor's own formula is |2p-1|; the owner's spec is followed.)
- The router hint `[jev] size=.. → executors: ..` is a hint, not an order. The per-spawn pick is `mb jev pick "<brief summary>"`: use it unless it says `decide yourself`. Fable is never an option; it is reached only through the advisor.

## Owner rules

1. Personal data goes to Jev only with the owner's permission. The flag being on covers their own prompts and spawn briefs, not third parties' data, credentials or notes classified personal (search never sends those).
2. Jev decides, Claude writes. Never ask Jev to produce text you will ship.
3. Irreversible acts - payment, deletion, publishing, sending - are never done on one Jev word. A human decides.
4. Never give Jev what it is weak at. Instead:

| Weak spot | Do this instead |
|---|---|
| literal reading of the wording | ask in plain, concrete words; no idioms |
| math, counting | compute it in code, ask Jev only about the result |
| comparing dates | compare in code |
| indirection, double negatives | ask the positive question |
| a large state with irrelevant detail | pass only the relevant excerpt; one question per item |
| adversarial or injected content in the state | do not send it |
| instructions that contradict the criteria | make them say the same thing |
| a choice leans to its FIRST option | order the options cheapest or safest first, and know it |
| generation | not Jev's job (rule 2) |

## Language

English is Jev's strongest language. On low confidence for a non-English text, re-ask with 5-10 items rewritten as English questions and compare.

## Cost

Input only, $0.042 per million tokens, computed in code and written to the journal (`mb jev log`, `mb jev status`). Balance is `jev_budget_usd` minus the logged total; unset means "unknown". Cost is small per call but a search rerank sends up to 30 excerpts: keep `jev_search` off (`mb config set jev_search false`) when you do not need it.
