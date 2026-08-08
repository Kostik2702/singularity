# CODE-INDEX — baseline from BEFORE the code corpus existed

> **This slice was taken BEFORE the code corpus came into existence.** At the
> time of the measurement `.mb/code_index/` physically did not exist,
> `code_repos_dir` was unset, and `mb search --code-only` answered with a "not
> implemented yet" stub. Everything recorded here is the behaviour of
> `mb search` over memory alone.

Taken **2026-07-29** on the production vault (275 notes), branch
`release/v0.5.0` off `d79f047` (WIKI-LOOP merged), Windows 11 Pro 26200, Python
3.10 from the project's working environment.

Its purpose is task **C00** of the [[SINGULARITY_CODE_INDEX_IMPLEMENTATION]]
epic: to give an evidential basis on which decision **D2** (`--with-code` as
opt-in) can be revisited by figures rather than by feel. The result of the
comparison is in [code-index.md](code-index.md) §6.

---

## 1. Reproduction

```powershell
python tools/code_index_baseline.py capture --out docs/code-index-baseline.json
python tools/code_index_baseline.py render                  # the tables below
python tools/code_index_baseline.py capture --both --out pair.json
python tools/code_index_baseline.py compare pair.json       # the displacement diff
```

The machine-readable slice sits alongside: **`docs/code-index-baseline.json`**
(10 queries × top-5, with `score`, `chunk_id`, corpus and timings).

### ⚠️ How to compare this correctly

The vault is alive. Any new note legitimately changes the absolute `score`, so
**a byte-for-byte comparison against this file a month from now proves nothing**.
The evidential pair for D2 is `capture --both`: the same vault, the same model,
one variable (`--with-code`), both result sets taken back to back in one session.
The file in `docs/` is a historical record of "how things were before the code
corpus", not a reference for a diff.

The exception is gate C04, "the default results did not change": it was checked
immediately after the implementation, while the vault was unchanged, and gave
**0/10 divergences** (see [code-index.md](code-index.md) §6).

## 2. State of the system at the time of the measurement

| Metric | Value |
|---|---|
| Notes in the vault | 275 (0 unindexed) |
| Memory chunks | 3216 (11.69 per note, 0 duplicates) |
| Text indexed | 2 026 466 characters |
| memory `chunk_schema` | 3 |
| By type | reference 2038, learning 625, decision 233, changelog 130, rule 105, incident 53, howto 32 |
| Sources corpus | 3 documents (processed), 33 chunks |
| **Code corpus** | **does not exist** |
| Model | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` |
| Tests | **1492 passed, 5 skipped** in 106.59 s (1497 collected) |
| Version | `mb` 0.5.0 |

The 5 skipped are the opt-in markers `ml_smoke` / `gemini_e2e` / `grok_e2e` /
`imgqa_gt` (`pyproject.toml`).

## 3. The control queries

Ten **real** questions to the memory, not synthetic ones: each is the shape of
what actually gets asked while working on Singularity. The "expected type"
column documents the coverage claim (decision / howto / incident / reference /
rule / learning); a drift in the type is itself a signal.

> The query strings and the result rows below are kept **verbatim**, in the
> language they were issued and returned in. They are recorded output naming
> real notes in the vault: rewriting them would turn a measurement into a
> fiction, and the next re-record would flip them back anyway.

Some queries return fewer than five rows — that is not an error but the
`min_score = 0.30` cutoff: the engine prefers to return three relevant hits
rather than five with two accidental ones.

### Q01 — «почему индекс пересобирается полностью» · expected type: `decision`

| # | score | file | section | corpus |
|---|---|---|---|---|
| 1 | 0.5059 | `decisions/CODE-INDEX D3 - пер-корпусная схема чанков.md` | Обязательное условие: миграция без простоя | memory |
| 2 | 0.4947 | `03_Tasks/SINGULARITY_CODE_INDEX_IMPLEMENTATION.md` | C06 — Приватность и бюджеты | memory |
| 3 | 0.4928 | `rules/Rule - every index reader must resolve through index_files….md` | Правило | memory |
| 4 | 0.4805 | `changelog/2026-07.md` | 2026-07-28 (2) — SINGULARITY-WIKI | memory |
| 5 | 0.4735 | `howto/Ingest-цикл - от документа до обновлённой wiki.md` | Известные грабли | memory |

### Q02 — «как добавить новый корпус в семантический поиск» · `howto`

| # | score | file | section | corpus |
|---|---|---|---|---|
| 1 | 0.6381 | `02_New_System_Design/03_Semantic_Vector_Search.md` | — | memory |
| 2 | 0.6108 | `03_Tasks/TASK-06_Search_Client_And_TokenEconomy.md` | Ссылки | memory |
| 3 | 0.5929 | `01_Predecessor_Analysis/memory_system/Semantic_Search_Deep_Dive.md` | 3. search-all.py | memory |
| 4 | 0.5728 | `01_Predecessor_Analysis/memory_system/Semantic_Search_Deep_Dive.md` | 2. Модель эмбеддингов | memory |

### Q03 — «Glob на Windows молча возвращает пустой результат» · `incident`

| # | score | file | section | corpus |
|---|---|---|---|---|
| 1 | 0.6901 | `incidents/Error - Glob subdir pattern silent empty on Windows.md` | What happened | memory |
| 2 | 0.6900 | `incidents/Error - Glob subdir pattern silent empty on Windows.md` | Context | memory |

### Q04 — «atomic commit поколений индекса и защита от torn write» · `reference`

| # | score | file | section | corpus |
|---|---|---|---|---|
| 1 | 0.5096 | `reference/Singularity Incoming Fork Integration Specification.md` | 9. Functional requirements | memory |
| 2 | 0.4866 | `03_Tasks/SINGULARITY_CODE_INDEX_IMPLEMENTATION.md` | 2. Архитектурные решения | memory |
| 3 | 0.4758 | `learnings/active/SINGULARITY-AUDIT.md` | 2026-07-11 14:09 | memory |
| 4 | 0.4609 | `learnings/active/NIKA-3D-WEB.md` | 2026-07-15 13:42 | memory |
| 5 | 0.4592 | `learnings/active/INTELLIGO-HT-PLAN.md` | 2026-07-22 13:10 | memory |

### Q05 — «TLS MITM сертификат при загрузке модели с Hugging Face» · `incident`

| # | score | file | section | corpus |
|---|---|---|---|---|
| 1 | 0.4775 | `reference/photorealism-settings.md` | Sampler Settings by Architecture | memory |
| 2 | 0.4739 | `03_Tasks/SINGULARITY-HF-CACHE-WATCHER-ISSUE.md` | Root cause | memory |
| 3 | 0.4690 | `learnings/active/WEBSITE-FACTORY.md` | 2026-07-18 10:57 | memory |
| 4 | 0.4574 | `howto/TLS-прокси на этой машине - плейбук обходов по инструментам.md` | — | memory |
| 5 | 0.4536 | `reference/DESIGN-KleinPhoto-nodePack.md` | Dependencies & Testing | memory |

### Q06 — «что обязательно записывать в память после задачи» · `rule`

| # | score | file | section | corpus |
|---|---|---|---|---|
| 1 | 0.6433 | `04_Plans/Implementation_Plan.md` | 1. Стратегия | memory |
| 2 | 0.6321 | `03_Tasks/TASK-09_Auto_Capture_Memory.md` | Цель | memory |
| 3 | 0.6218 | `02_New_System_Design/00_Vision_And_Principles.md` | 4. Цели (измеримые) | memory |
| 4 | 0.6172 | `03_Tasks/TASK-09_Auto_Capture_Memory.md` | Техспецификация | memory |
| 5 | 0.6163 | `03_Tasks/_Task_Index.md` | Список задач | memory |

### Q07 — «как устроен nightly прогон и из каких шагов состоит» · `reference`

| # | score | file | section | corpus |
|---|---|---|---|---|
| 1 | 0.4891 | `03_Tasks/SINGULARITY_INCOMING_FORK_IMPLEMENTATION.md` | T12 — Nightly orchestration | memory |
| 2 | 0.4814 | `03_Tasks/SINGULARITY_INCOMING_FORK_IMPLEMENTATION.md` | T12 — Nightly orchestration | memory |
| 3 | 0.4774 | `03_Tasks/SINGULARITY_WIKI_LOOP_IMPLEMENTATION.md` | W11 — Интеграция | memory |
| 4 | 0.4566 | `learnings/active/INTELLIGO-HT-PLAN.md` | 2026-07-21 13:30 | memory |
| 5 | 0.4543 | `rules/Session end with remaining epic tasks….md` | Почему | memory |

### Q08 — «нужен ли watcher за репозиториями кода» · `decision`

| # | score | file | section | corpus |
|---|---|---|---|---|
| 1 | 0.6723 | `decisions/CODE-INDEX D4 - без watcher по коду.md` | Отклонённая альтернатива | memory |
| 2 | 0.6601 | `decisions/CODE-INDEX D4 - без watcher по коду.md` | — | memory |
| 3 | 0.6037 | `03_Tasks/SINGULARITY_CODE_INDEX_IMPLEMENTATION.md` | 6. Риски | memory |
| 4 | 0.6021 | `learnings/active/GENERAL.md` | 2026-07-28 23:33 | memory |

### Q09 — «markdown chunking overlapping windows and minimum chunk size» · `reference`

| # | score | file | section | corpus |
|---|---|---|---|---|
| 1 | 0.4995 | `learnings/active/SINGULARITY-AUDIT.md` | 2026-07-11 14:02 | memory |
| 2 | 0.4739 | `03_Tasks/SINGULARITY_AUDIT_BACKLOG.md` | AUD-03 Windowed chunking | memory |
| 3 | 0.4320 | `incidents/CSS grid minimum size blocks inner scroll container.md` | Фикс и предотвращение | memory |
| 4 | 0.4198 | `incidents/CSS grid minimum size blocks inner scroll container.md` | — | memory |

### Q10 — «почему поиск отвечает медленно и что такое холодный fallback» · `learning`

| # | score | file | section | corpus |
|---|---|---|---|---|
| 1 | 0.4685 | `learnings/active/SINGULARITY-AUDIT.md` | 2026-07-11 15:08 | memory |
| 2 | 0.4621 | `01_Predecessor_Analysis/memory_system/Semantic_Search_Deep_Dive.md` | 1. Архитектура | memory |
| 3 | 0.4403 | `learnings/active/STORYFORGE-RUNTIME.md` | 2026-07-20 10:10 | memory |
| 4 | 0.4311 | `learnings/active/STORYFORGE-QUALITY.md` | 2026-07-20 10:37 | memory |
| 5 | 0.4194 | `rules/Rule - repair-проход обязан снова пройти gate.md` | Почему | memory |

**Timings:** the warm engine takes 13.5–18.1 ms per query (mean 15.3 ms), and
wall-clock through the CLI is 181–205 ms (the model is already loaded in the
daemon; the difference is Python process start-up).

## 4. What this baseline does NOT cover

* the quality of results FOR CODE — these ten queries address memory; they
  measure displacement, not the usefulness of the code corpus. A separate probe
  with code-oriented queries is in [code-index.md](code-index.md) §5;
* the timings of a full `mb index rebuild` over memory (it was not rebuilt —
  that was the whole point of gate C01);
* `mb doctor` / `mb selftest` — those are recorded in release gate C07.
