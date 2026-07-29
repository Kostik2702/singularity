# WIKI-LOOP — дизайн слоя источников, ingest-цикла и интеллект-lint

**Эпик:** `SINGULARITY-WIKI`, задачи `W00…W12`
**Спека:** vault `03_Tasks/SINGULARITY_WIKI_LOOP_IMPLEMENTATION.md`
**Решения:** vault `decisions/WIKI-LOOP D1…D7` (owner: user, 2026-07-28)
**Baseline:** [`wiki-loop-baseline.md`](wiki-loop-baseline.md)

Документ фиксирует контракты и точную карту интеграции с существующим кодом.
Каждое утверждение о текущем состоянии — с `file:line` по коду на коммите
`ac1d807` (v0.4.0).

---

## 1. Что строим

Метод Карпатого (LLM-wiki) в терминах Singularity:

| Элемент метода | Реализация | Задача |
|---|---|---|
| Raw sources — иммутабельные внешние документы | `.mb/sources/<YYYY>/<slug>--<id8>.md` + реестр `.mb/sources.json` | W01, W02 |
| Поиск по сырью, изолированный от wiki | второй корпус `.mb/sources_index/` | W03 |
| Провенанс «заметка ← источник» | frontmatter `sources: [<id>]` + `derived_notes` в реестре | W04 |
| Ingest: источник → summary → правка 10–15 связанных страниц | `mb ingest plan` (кандидаты) + скил `mb-ingest` (правки) | W05, W09 |
| Парсимый лог операций | `.mb/ingest-log.jsonl` | W06 |
| Contradiction lint | `mb curator contradictions` | W07 |
| «Что исследовать дальше» | `mb curator gaps` | W08 |

**Не реализуем осознанно:** `index.md`-first навигация — её заменяет векторный
поиск (`mb search`, ~20 мс тёплый); отдельный `log.md` — роль поделена между
`changelog/` (человеку) и `.mb/*.jsonl` (машине), см. D3.

---

## 2. Инварианты (проверяемые)

1. **Офлайн-ядро.** Ни один новый модуль не открывает сеть (D4). `mb ingest add`
   отказывает на `http(s)://` с exit 2.
2. **Report-only.** `ingest_plan.py`, `contradictions.py`, `gaps.py` не имеют ни
   одной функции записи. `sources.py` пишет ТОЛЬКО под `.mb/` (D5).
   Проверяется статически: `tests/test_wiki_loop_report_only.py`.
3. **Write-once raw.** Существующий `stored_path` не перезаписывается никогда
   (D6).
4. **Изоляция корпусов.** `curator`/`digest` читают только
   `cfg.memory_index_dir` и физически не видят источники (D1/D2).
   Проверяется регрессией на побайтовое равенство их JSON.
5. **Атомарность.** Всякая запись состояния — `config.atomic_write_json()`
   (`config.py:329`) или tmp+replace.
6. **`mb curator` без подкоманды не меняется.** Новые подкоманды опциональны
   (argparse subparsers НЕ `required`).

---

## 3. Контракт реестра `.mb/sources.json`

```json
{
  "schema_version": 1,
  "sources": {
    "<source_id>": {
      "title": "Название источника",
      "sha256": "<hex сырого текста>",
      "bytes": 12345,
      "chars": 6789,
      "origin": {"kind": "file|stdin|connector|external",
                 "ref": "<abs path|url|connector:doc>"},
      "stored_path": ".mb/sources/2026/slug--ab12cd34.md",
      "stored": true,
      "content_type": "text/markdown",
      "project": null,
      "tags": [],
      "ingested_at": "2026-07-28T18:40:00",
      "status": "new|partial|processed",
      "derived_notes": [],
      "supersedes": null,
      "superseded_by": null
    }
  }
}
```

* `source_id = sha256(normalize(text))[:12]` — контент-адресуемо, дедуп
  бесплатный. `normalize` = `capture.normalize_gist()` (`capture.py:25-29`:
  lowercase → убрать пунктуацию → схлопнуть пробелы), поэтому «тот же текст с
  другими пробелами» не порождает дубль.
* `sha256` в записи — хеш **сырого** текста (не нормализованного): он
  различает версии одного `origin.ref`, которые нормализация схлопнула бы.
* `stored: false` — ретро-запись из `mb ingest backfill` (W10.1): текста нет,
  есть только факт «эта заметка выведена вот отсюда».
* Чтение — **толерантное** (битый/отсутствующий → `{"sources": {}}`, как
  `digest._read_journal`, `digest.py:36-46`), запись — **строгая**, через
  `atomic_write_json`.

### Хранение raw

`.mb/sources/<YYYY>/<slug>--<id8>.md`:

```
---
type: source
source_id: ab12cd34ef56
title: "..."
origin: {kind: file, ref: "D:/docs/x.md"}
sha256: <hex>
ingested_at: 2026-07-28T18:40:00
immutable: true
---
<текст побайтно как есть, без чистки markdown>
```

Разделитель между frontmatter и телом — стандартный `---\n`. Тело НЕ
нормализуется: raw обязан быть воспроизводим побайтно.

### Slug-санитайзер (Windows)

Запрещённые символы `<>:"/\|?*` и управляющие символы `\x00-\x1f` → `-`;
зарезервированные имена `CON PRN AUX NUL COM1..9 LPT1..9` (в любом регистре,
с любым расширением) → префикс `_`; хвостовые точки/пробелы срезаются
(Windows их молча отбрасывает); длина ≤ 60 символов; NFC-нормализация;
кириллица допустима (vault уже кириллический); пустой результат → `source`.

---

## 4. Карта интеграции с существующим кодом

### 4.1 `singularity/config.py`

| Точка | Что делаем |
|---|---|
| `config.py:11-13` docstring «All runtime state lives inside the vault under `.mb/`» | **Исправить**: `.mb/sources/` — единственные невосстановимые данные (D1, R8) |
| `config.py:38-65` `DEFAULT_CONFIG` | +9 ключей (см. §6) |
| `config.py:93-165` `validate_config` | +правила типов/диапазонов для новых ключей |
| `config.py:162` `known = set(DEFAULT_CONFIG) \| …` | новые ключи попадают автоматически (они в `DEFAULT_CONFIG`), warning'ов не будет |
| `config.py:168-177` `migrate_config` | bump `SCHEMA_VERSION` 1 → 2; пользовательские значения не переписывать |
| `config.py:195-200` derived-paths | +`sources_dir`, `sources_index_dir` — никаких склеек путей по месту |
| `config.py:244-246` `lock_dir` | становится пер-корпусным (`lock_dir` остаётся как есть для `memory`, добавляется `corpus_lock_dir(name)`) |
| `config.py:329-337` `atomic_write_json` | переиспользуем как есть |

### 4.2 `singularity/search/chunking.py`

| Точка | Что делаем |
|---|---|
| `chunking.py:25` `CHUNK_SCHEMA_VERSION = 3` | **НЕ трогаем.** Схема vault не меняется → полный rebuild боевого индекса не нужен (D2) |
| `chunking.py:180-246` `extract_chunks` | переиспользуем; для корпуса источников `type` форсится в `source`, в чанк кладётся `source_id` из frontmatter — через тонкую обёртку `sources_chunker`, сам `extract_chunks` не меняется |
| `chunking.py:249-254` `should_skip` | доказательство изоляции: отсекает по ПЕРВОМУ компоненту, `.mb` в `skip_dirs` (`config.py:63`) |

### 4.3 `singularity/search/indexer.py` — основной объём W03

| Точка | Сейчас | Становится |
|---|---|---|
| `indexer.py:188-198` `_scan_vault(cfg)` | жёстко `cfg.vault` + `should_skip(skip_dirs, skip_parts)` | `_scan_corpus(corpus)` — корень и skip-правила из `Corpus` |
| `indexer.py:220-246` `rebuild`/`_rebuild` | жёстко `cfg.memory_index_dir` | `rebuild(cfg, corpus=...)` |
| `indexer.py:264-326` `update`/`_update` | то же | `update(cfg, only_files, corpus=...)` |
| `indexer.py:153-180` `_writer_lock(cfg)` | один общий `cfg.lock_dir` | `_writer_lock(cfg, corpus)` — пер-корпусный путь; **семантика cross-process mkdir-лока, takeover по `STALE_LOCK_SECONDS` и правило «проигравший НИКОГДА не удаляет чужой лок» сохраняются без изменений** |
| `indexer.py:72-81` `index_files` | generic по `index_dir` | без изменений |
| `indexer.py:126-150` `_commit_generation` | generic по `index_dir` | без изменений → crash-safety достаётся бесплатно |
| `indexer.py:84-108` `load_committed_index` | generic | без изменений |
| `indexer.py:249-261` `inspect` | `cfg.memory_index_dir` | +опциональный корпус |

`Corpus` — dataclass: `name`, `root`, `index_dir`, `skip_dirs`, `skip_parts`,
`chunker`. Два экземпляра: `memory_corpus(cfg)` и `sources_corpus(cfg)`.
Обратная совместимость: все публичные функции сохраняют текущую сигнатуру с
`corpus=None` → memory-корпус.

### 4.4 `singularity/search/engine.py`

| Точка | Что делаем |
|---|---|
| `engine.py:27-31` поля | +`src_embs`, `src_chunks` |
| `engine.py:53-56` `reload()` | +`self._load_one(cfg.sources_index_dir)`; возвращаемый dict += `"sources"` |
| `engine.py:97-120` `_search_source` | без изменений (generic) |
| `engine.py:103-105` whitelist фильтров | `has_filters` учитывает новые ключи, если появятся |
| `engine.py:162-178` `search()` | корпус источников участвует ТОЛЬКО при `req["include_sources"]` |
| `engine.py:145-159` `_compact_line` | метка корпуса: `item["source"] == "sources"` попадает в `tag`, если у чанка нет `type` — для source-чанков `type` = `source`, поэтому метка видна как `[source]` |

**Флаг обязан пройти весь путь** (пропуск любого звена = флаг молча
игнорируется):
`cli.py:47-63` (`--with-sources`) → `cli.py:224-239` (payload) →
`search/client.py:228-241` (`search`) и `client.py:215-225` (`_cold_search`) →
`search/daemon.py:44-48` (`cmd == "search"`) → `engine.search()`.
Тест на каждое звено, включая cold-fallback (`tests/test_client_fallback.py`).

### 4.5 `singularity/curator.py` и `singularity/digest.py`

**Не трогаем вообще** в W03: `curator._note_embeddings` (`curator.py:142`),
`curator._duplicate_pairs` (`curator.py:262`), `digest.plan` (`digest.py:151`)
читают `cfg.memory_index_dir` и физически не могут увидеть второй корпус.
Требуется только регрессионный тест на побайтовую идентичность вывода.

Изменения приходят позже и точечно:
* W04 — `curator.validate()` (`curator.py:100-127`): +2 ключа `issues`
  (`source_not_in_registry`, `orphan_source`), существующие ключи и exit-код не
  меняются;
* W05 — вынос `_pool` (`digest.py:71-78`), `_note_embeddings` mean-pool
  (`curator.py:134-157`) и `_blockwise_pairs` (`curator.py:227-252`) в
  `singularity/search/similarity.py` под регрессионный тест «вывод не изменился».

### 4.6 `singularity/nightly.py`

| Точка | Что делаем |
|---|---|
| `nightly.py:175-184` `run()` | +шаги `ingest_index`, `gaps`, `ingest_pending`; каждый под существующим `_guard` (`nightly.py:54-58`) |
| `nightly.py:133-170` `_step_trend` | trend-строка += `sources`, `unprocessed_sources`, `contradiction_candidates`, `open_questions` |
| `nightly.py:114-130` `_step_markers` | рядом — ретенция `.mb/ingest-log.jsonl` по `ingest_log_retention_days` |

Причина отдельного шага `ingest_index`: watcher (`watch.py`) корпус источников
НЕ смотрит — `.mb` вне его зоны (прямое следствие D1). Догон делают
`mb ingest add` (через тёплый демон), `mb ingest index` и nightly.

### 4.7 `singularity/hooks/vault_guard.py` + `singularity/vault_guard.py`

| Точка | Что делаем |
|---|---|
| `vault_guard.py:25-27` `MANAGED_ROOTS`/`EXEMPT_ROOTS` | +`immutable_root(vault, abs_path, roots)` — резолвнутый путь, `..`-обход не проходит |
| `hooks/vault_guard.py:59-76` ветвление | `immutable_root` проверяется ПЕРВЫМ и даёт `deny` **даже при `vault_guard_mode: audit`** (D6) |
| `hooks/vault_guard.py:46` | `vault_guard_enabled: false` → по-прежнему полный no-op (честно задокументировано) |

### 4.8 `singularity/backup.py` — критично по D1

| Точка | Сейчас | Становится |
|---|---|---|
| `backup.py:39-58` `create` | копирует только `config.json` | +`.mb/sources.json` и весь `.mb/sources/**` с чек-суммами в манифесте; при превышении `backup_max_sources_mb` — предупреждение и явный `--with-sources` |
| `backup.py:76-87` `verify` | проверяет `files` | проверяет и raw-файлы |
| `backup.py:90-119` `restore` | config + указатель поколения | +реестр и raw; **НИКОГДА не удаляет существующие файлы**, только добавляет недостающие |

### 4.9 `singularity/connectors/base.py`

`NormalizedDoc` (`connectors/base.py:26`) — существующий контракт нормализованного
документа. Ingest принимает его как `origin.kind = "connector"`,
`origin.ref = "<connector>:<doc_id>"`. Коннекторы остаются OFF by default;
ядро работает при `connectors: {}` (D4, [[06_Integrations_Layer]]).

### 4.10 `doctor.py`, `status.py`, `stats.py`, `hooks/session_start.py`

| Файл | Что добавляем |
|---|---|
| `doctor.py` | блок sources: **FAIL** при «реестр непустой — каталога нет» и «запись в реестре без файла»; WARN при осиротевшем raw, устаревшем `sources_index`, расхождении моделей корпусов, `.mb/sources` под git-ignore. `--fix` только безопасное и **никогда не удаляет raw** |
| `status.py:105-120` `snapshot` | +`sources` секция |
| `stats.py:16-61` `compute` | +`sources: {documents, chunks, indexed_at}` |
| `hooks/session_start.py` | nag при `unprocessed_sources > 0`, rate-limit — существующий `nightly_nag_rate_limit_hours` (`config.py:57`), бюджет `session_context_max_lines` (`config.py:48`) не превышать |

---

## 5. Целевой поток

```
title Ingest-цикл (один источник)

participant Пользователь as U
participant "Агент / skill mb-ingest" as A
participant "mb CLI (движок)" as CLI
participant ".mb/sources/ (raw, вне Obsidian)" as S
participant ".mb/sources_index/ + memory_index" as IDX
participant "wiki-заметки" as W

U->A: "проглоти этот документ"
A->CLI: mb ingest add <path|--stdin> --title ... --origin ...
CLI->S: write-once .mb/sources/<YYYY>/<slug>--<id8>.md (frontmatter + raw verbatim)
CLI->CLI: .mb/sources.json (atomic tmp+replace)
CLI->IDX: инкрементальный апдейт sources_index (через тёплый демон)
CLI-->A: {source_id, duplicate: false, stored_path, indexed: true|pending}
A->CLI: mb ingest index (если indexed=pending — догнать явно)
A->CLI: mb ingest plan <source_id> --json
CLI->IDX: чтение обоих committed generation (без модели, без записи)
CLI-->A: {summary_target, touch[<=15], new_pages, conflicts}
A->U: takeaways + список страниц к правке
U-->A: подтверждение / корректировка списка
A->W: summary-заметка (frontmatter sources: [<id>])
A->W: правки N существующих заметок (add-only, с цитатой)
A->CLI: mb ingest mark <id> --notes a.md,b.md --status processed
CLI->CLI: derived_notes + status + строка в .mb/ingest-log.jsonl
A->W: строка в changelog/YYYY-MM.md
A-->U: отчёт: что записано, что осталось открытым
```

---

## 6. Новые конфиг-ключи (schema_version 2)

| Ключ | Тип | Default | Диапазон | Смысл |
|---|---|---|---|---|
| `ingest_index_sources` | bool | `true` | — | kill-switch второго корпуса |
| `ingest_max_source_chars` | int | `400000` | 1…10 000 000 | защита от мусора; превышение = отказ, усечение запрещено |
| `ingest_fanout_top_k` | int | `15` | 1…100 | потолок предложений `plan.touch` (D7) |
| `ingest_fanout_threshold` | float | `0.45` | 0.0…1.0 | мин. косинус кандидата (симметрично `digest_score_threshold`) |
| `ingest_log_retention_days` | int | `365` | 1…3650 | ретенция `.mb/ingest-log.jsonl` |
| `immutable_roots` | list[str] | `[".mb/sources"]` | — | пути, где guard даёт deny даже в audit (D6) |
| `gap_link_min_inbound` | int | `2` | 1…100 | порог входящих для `missing_pages` |
| `gap_learning_age_days` | int | `30` | 1…3650 | возраст непереваренного learning |
| `lint_llm_adjudication` | bool | `false` | — | батч-adjudication через `mb llm` (опционально) |
| `backup_max_sources_mb` | int | `200` | 1…100 000 | порог предупреждения при бэкапе raw |

---

## 7. Атомарность журнала `.mb/ingest-log.jsonl` (решение W00)

**Выбрано: read-modify-write целиком через tmp+replace**, тот же приём, что
`nightly._step_trend` (`nightly.py:147-169`).

Обоснование против `O_APPEND` одной строкой: `O_APPEND` атомарен для записей
меньше `PIPE_BUF` только на POSIX; на Windows гарантии слабее, а строка с
длинным списком `notes` легко выходит за безопасный размер. Файл небольшой
(одна строка на ingest, ретенция 365 дней), поэтому переписывание целиком
дешевле, чем класс труднодиагностируемых порванных строк. Тот же приём уже
проверен на `nightly-history.jsonl` и `reflect-history.jsonl`.

Дополнительно: запись сериализуется тем же пер-корпусным mkdir-локом
(`ingest.lock`), чтобы два параллельных `mb ingest mark` не потеряли строку.

---

## 8. Порядок и релизы

```text
W00 решения + дизайн-док
 └─ W01 реестр + write-once (.mb/sources/)
     ├─ W02 CLI mb ingest add|list|show|export
     │   └─ W03 второй корпус (Corpus + .mb/sources_index/)
     │       ├─ W05 mb ingest plan
     │       │   └─ W06 mb ingest mark + .mb/ingest-log.jsonl
     │       │       └─ W09 скил mb-ingest
     │       ├─ W07 детектор противоречий
     │       └─ W08 mb curator gaps
     └─ W04 иммутабельность + провенанс
 W10 миграции      ← W01–W04
 W11 интеграция    ← W06, W08
 W12 E2E + релиз   ← всё
```

R1 = W00–W04 · R2 = W05, W06, W09 · R3 = W07, W08 · R4 = W10–W12.
