# Singularity

Personal AI memory system (engine codename **Singularity**, CLI **`mb`** — memory brain):
Obsidian-native automatic memory + multilingual semantic search + disciplined
skills/workflows for Claude Code. Cross-platform: Windows / macOS / Linux.

Reference architecture reverse-engineered from the Genevra system; full design
docs live in the Obsidian vault (`02_New_System_Design`, `03_Tasks`, `04_Plans`).

## Install

```powershell
# Windows
.\install\install.ps1
```
```sh
# macOS / Linux
./install/install.sh
```

## Usage

```
mb search "как настроить поиск"     # semantic search (RU/EN), warm ~100-200ms
mb search "chunking" --type howto --tags search --links "Semantic_Search"
mb daemon start|stop|status|reload
mb index rebuild|update
mb watch                            # on-save incremental indexing
mb note new decision "Название"
mb learn "инсайт" --task MB-03
mb doctor [--fix] [--heavy]
mb init                             # bind vault + create folder tree
```

Vault resolution: `--vault` > env `MB_HOME` > `~/.singularity.json` pointer.
All runtime state lives in `<vault>/.mb/` (indexes, daemon port/pid/log, config).

## Layout

```
singularity/
├── cli.py            # mb entry point
├── config.py         # vault discovery + .mb/config.json
├── watch.py          # watchdog on-save updater
├── doctor.py         # install verification (PASS/WARN/FAIL/FIXED/SKIP/AUTH)
├── search/
│   ├── chunking.py   # md -> ## section chunks (+wikilinks/tags/aliases meta)
│   ├── indexer.py    # full rebuild + incremental (mtime+size diff)
│   ├── encoder.py    # sentence-transformers, normalized embeddings
│   ├── engine.py     # cosine search, filters, trim_meta, compact format
│   ├── daemon.py     # warm TCP 127.0.0.1 daemon, ndjson protocol
│   └── client.py     # auto-start, retry, COLD fallback
└── platform/         # ALL OS-specific code (scheduler/proc/secrets)
```

## Tests

```
python -m pytest tests -q     # contract tests, no ML deps required
```
