# WIKI-LOOP — baseline BEFORE the changes

Taken **2026-07-28** on the production vault `D:\obsidian\memory brain`, branch
`feat/wiki-loop` off `ac1d807` (v0.4.0), Windows 11 Pro 26200, Python from the
project's working environment.

Purpose: the reference for the W03 regression (corpus isolation), W05 (the
`similarity.py` refactor) and W10 (the migration must not rebuild the main
index).

---

## 1. Commands to reproduce

```powershell
mb stats --json
mb curator --json     > baseline-curator.json
mb digest plan --json > baseline-digest.json
mb index rebuild                       # timing
python -m pytest -q --tb=no            # the full suite
```

## 2. The numbers

### Index and vault

| Metric | Value |
|---|---|
| Notes in the vault (indexable) | 262 |
| Notes that reached the index | 257 (5 yield 0 chunks: sections shorter than `MIN_CHUNK_CHARS=50`, `chunking.py:18,219`) |
| Chunks | 3044 |
| Characters indexed | 1 926 788 |
| Average chunks per note | 11.84 |
| Duplicate chunks (by content sha1) | 0 |
| `chunk_schema` | 3 (`chunking.py:25`) |
| Embeddings size | 4.7 MB |
| By type | reference 1962, learning 615, decision 158, changelog 125, rule 105, incident 53, howto 26 |

### Timings

| Operation | Time |
|---|---|
| `mb index rebuild` (full, 262 files → 3044 chunks) | **174 266 ms** (2 min 54 s) |
| `mb curator --json` | **1 360 ms** |
| `mb digest plan --json` | **265 ms** |
| `mb stats --json` | **1 067 ms** |
| The full `pytest` suite | **102.82 s** |

### Tests

```
1155 passed, 5 skipped in 102.82s
```

Test count BEFORE the epic: **1155** (the 5 skipped are opt-in markers
`ml_smoke`/`gemini_e2e`/`grok_e2e`/`imgqa_gt`, `pyproject.toml:38-44`).

### Configuration at the time of the snapshot

| Key | Value |
|---|---|
| `schema_version` | 1 |
| `vault_guard_enabled` | `false` (⇒ the guard is a complete no-op; see D6: the real guarantee is write-once in the engine) |
| `vault_guard_mode` | `"audit"` |
| `personal_roots` | `[]` |
| `mb` version | 0.4.0 |

---

## 3. Reference artefacts for byte-for-byte comparison

Kept outside the repository (not committed — they contain the path to a personal
vault):

| File | SHA-256 |
|---|---|
| `baseline-curator.json` | `22444048828409957ACE7B66AA983E76794E8CAF9A51C7FDB20D3F077BEC9345` |
| `baseline-digest.json` | `2E8E3BFA81E3AF73C34A7328B2CE7058404A0E257A3CE886315B1C0C498173FF` |

⚠️ **Important about comparison.** These hashes are valid only while the vault's
content is unchanged. The vault is alive: any new note (including the 7 notes
`decisions/WIKI-LOOP D1…D7` added in W00) legitimately changes what
`curator`/`digest` print.

So the main W03 gate is stated **relatively**, not absolutely:
> take `curator --json` and `digest plan --json` IMMEDIATELY before filling
> `.mb/sources/`, then repeat right after — the output must be byte-for-byte
> identical.

The absolute hashes above are for something else: proving that the
`similarity.py` refactor (W05) and the config migration (W10) did not change the
output while the vault was unchanged — there the comparison happens within one
run (before/after in a single session).

---

## 4. What this baseline does NOT cover

* `.mb/sources_index/` — it does not exist yet; its first build is measured in
  W10.3 and recorded in `docs/UPGRADE.md`.
* The time of `mb curator contradictions` on the production vault — measured in
  W07 (acceptance requires the budget to be recorded, and a cap on the number of
  pairs introduced if it is exceeded).
* `mb doctor`/`mb selftest` — green at the time of the snapshot; the detailed
  output is recorded in the W12 release gate.
