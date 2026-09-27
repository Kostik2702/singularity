---
name: mb-code
description: Structural code graph of a repository (Singularity, CLI mb code) - who calls a function, what it calls, what a change breaks, dead code, architecture. Use when the user asks "who calls X", "where is X used", "what does this change affect", impact, blast radius, callers, callees, call chain, trace, dead code, unused functions, architecture, modules, hotspots - or BEFORE grep/Read whenever the question is about relations between symbols in a repository that has a graph (the SessionStart line "Code graph: ..." says so).
---

# mb-code — the structural code graph

One SQLite graph per repository (Python, TypeScript/JavaScript/TSX): definitions, imports, calls, inheritance. Each graph belongs to one repository: the one your working directory is in, or `--project <id>`. Semantic search over code is still `mb search --with-code` (skill mb-search). This graph answers the structural questions search cannot.

- [ ] Off by default: `mb code status` shows whether it is on and which repositories have a graph. Nothing built for this repository? `mb code build` from inside it (seconds for a normal repository, minutes for 100k files).
- [ ] Every answer first compares the graph with the working tree and writes small changes in (up to 200 files). `--no-refresh` skips that step, for councils and for many queries in a row.

## Which verb

| Question | Command |
|---|---|
| Who calls it? | `mb code callers <symbol>` |
| What does it call? | `mb code callees <symbol>` |
| The chain, several levels deep | `mb code trace <symbol> --direction callers\|callees --depth N` (N ≤ 5) |
| Where is something named like this | `mb code find <text>` (substring of a name or a qname) |
| What a file defines | `mb code outline <path/relative/to/repo>` |
| The code of one symbol | `mb code snippet <symbol>` |
| What my change touches | `mb code impact [--base main] [--depth N]` |
| What nothing uses | `mb code deadcode [--unknown]` |
| How the repository splits into parts | `mb code arch [--top N]` |
| Counts, tiers, versions | `mb code schema` |
| Catch up after a big change | `mb code update` |
| Ad-hoc read-only SQL | `mb code sql` — only when your `mb` has it; check `mb code --help` first |

`<symbol>` is a qname (`pkg/core.py::Config.save`), `Class.method`, or a name only one definition carries. A shared name is refused with the candidates listed: pick one, never guess. Add `--json` for the full envelope; lists are paged (`--limit`, `--offset`).

`impact` compares with HEAD (the uncommitted change) or with the merge-base of `--base` and HEAD. It returns the changed symbols, their callers up to `--depth`, the tests reached, call sites of deleted names (`possibly_broken`), and `coverage`. It writes the whole diff into the graph first, or refuses and gives the count (more than 2000 files): run `mb code update`, then ask again.

## Reading an answer honestly

These rules are why the graph is worth asking. Break one and the answer turns into a guess that sounds like a fact.

- [ ] **Freshness.** `freshness.checked: false` means the tree was not compared (`--no-refresh`, or a failed scan). `stale_files` / `stale_count` are files the graph knows differ from the tree, and items from them carry `"stale": true`. `stale_all` means the whole graph may be off and says why (too many changes, a manifest or `.gitignore` changed, an old schema); a background `mb code update` is usually already running (`background: true`). Say so when you report, and re-ask after the update rather than presenting a stale answer as current.
- [ ] **Missing edges are not missing relations.** An edge exists only where the resolver could prove the target. A call it could not place goes to `unresolved`, and `callers` returns the same-name ones as `unresolved_candidates`: possible callers through dynamic dispatch or an untyped receiver. "No callers" is true only when both `callers` and `unresolved_candidates` are empty. `impact`'s `coverage.unresolved_same_name_sites` is the same caveat for a change.
- [ ] **Tiers.** Each edge carries the rule that produced it. `scope`, `import`, `self`, `class-attr` are proven. `local-type`, `annotation`, `attr-type` come from a type seen in the code. `name-unique` is the weakest: only one first-party definition has that name. Name the tier when a conclusion rests on a weak edge.
- [ ] **deadcode has two buckets.** `dead`: no call or inheritance edge, no unresolved call of that name anywhere, the name is not mentioned as a value, no decorator, not an entry point or TS export, not an override. `unknown`: an uncalled definition with a reason (`unresolved same-name calls`, `name mentioned`, `decorated`, `exported`, `override`, `accessor`). Only `dead` is a finding. Read `unknown` with `--unknown`, and never report it as dead.
- [ ] **Outside the graph.** Files the repository ignores, vendored and secret-looking files, files over the size limit, and languages beyond Python/TS/JS are not in it (`impact`'s `coverage.outside_graph`). "Not in the graph" is not "not used".
- [ ] **arch** communities are computed at the full build. `computed.updates_since` counts the in-place updates since, and files added since belong to no community.
- [ ] A truncated list says so (`has_more`, `next_offset`, `truncation_reason`). Page on, or say it was cut.

## When it refuses

- `code graph is off` — `mb config set code_graph_enabled true`, only if the user wants it.
- `no code graph covers <dir>` — `mb code build` from inside the repository (or `--all`).
- `cannot build here` — the parser is missing from this interpreter; the message gives the install line.
- The graph has an old schema or other parser versions — the answer says so; `mb code build` rebuilds it.

- [ ] Cite results as `file:line` + qname; state the tier and the freshness when they matter to the conclusion.
