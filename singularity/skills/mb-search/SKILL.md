---
name: mb-search
description: Semantic search over the user's Obsidian memory vault (Singularity, CLI mb). Use when the user says sem, fm, "find it in memory", "find in memory", "what do we know about", or BEFORE any grep/Read discovery of prior knowledge, decisions, howtos, incidents, project docs. The index is crosslingual - a query finds a note whatever language either is written in.
---

# mb-search — semantic search over the memory

The vault resolves itself (`--vault` → `MB_HOME` → `~/.singularity.json`). The daemon answers in ~10-30 ms warm; if it is dead the client starts it itself (the first request takes ~10 s) or answers cold.

## The SEM-FIRST rule

- [ ] For ANY knowledge search (memory, decisions, past tasks, howtos) — `mb search` first, and only then grep/Glob/Read as a fallback.
- [ ] Skipping sem is allowed only when the path is already known: named by the user, obtained from a previous result, or determined by the task itself.

## Commands

```bash
mb search "how is search configured"      # top-5, compact output (token thrift)
mb search "reload bug" --top 10           # more results
mb search "..." --type decision           # decisions only (learning|decision|howto|incident|rule|reference)
mb search "..." --tags search,daemon      # filter by tags (frontmatter + inline)
mb search "..." --links "Doctor_Plan"     # only notes linked to [[Doctor_Plan]]
mb search "..." --path-prefix 03_Tasks/   # only inside a folder
mb search "..." --json                    # full objects (for processing)
mb search "..." --full                    # no excerpt truncation
```

Compact output: `file | score | [type] | title/section | excerpt(150)`. A score below ~0.3 is a weak match; rephrase the query. The embedding model is multilingual, so the query does not have to be in the language the note was written in, and languages may be mixed within one query.

## Maintenance (when search behaves oddly)

```bash
mb daemon status          # is the daemon alive, and on which port
mb index update           # incremental top-up (usually unnecessary — the watcher handles it)
mb daemon reload          # re-read the index
mb doctor --fix           # full diagnosis plus repair
```

- [ ] Cite a result to the user as `file | section`; never dump raw lines.
