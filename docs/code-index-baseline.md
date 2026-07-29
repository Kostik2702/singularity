# CODE-INDEX — baseline ДО существования корпуса кода

> **Этот срез снят ДО того, как корпус кода начал существовать.** На момент
> замера `.mb/code_index/` отсутствовал физически, `code_repos_dir` не был
> задан, а `mb search --code-only` отвечал заглушкой «not implemented yet».
> Всё, что здесь записано — это поведение `mb search` по чистой памяти.

Снято **2026-07-29** на боевом vault (275 заметок), ветка `release/v0.5.0` от
`d79f047` (WIKI-LOOP смёржен), Windows 11 Pro 26200, Python 3.10 из рабочего
окружения проекта.

Назначение — задача **C00** эпика [[SINGULARITY_CODE_INDEX_IMPLEMENTATION]]:
дать доказательную базу, по которой решение **D2** (`--with-code` как opt-in)
можно пересмотреть цифрами, а не ощущением. Результат сравнения — в
[code-index.md](code-index.md) §6.

---

## 1. Воспроизведение

```powershell
python tools/code_index_baseline.py capture --out docs/code-index-baseline.json
python tools/code_index_baseline.py render                  # таблицы ниже
python tools/code_index_baseline.py capture --both --out pair.json
python tools/code_index_baseline.py compare pair.json       # диф вытеснения
```

Машиночитаемый срез лежит рядом: **`docs/code-index-baseline.json`** (10
запросов × топ-5, со `score`, `chunk_id`, корпусом и таймингами).

### ⚠️ Как это сравнивать правильно

Vault живой. Любая новая заметка законно меняет абсолютные `score`, поэтому
**побайтовое сравнение с этим файлом через месяц ничего не докажет**.
Доказательная пара для D2 — это `capture --both`: один и тот же vault, одна и
та же модель, одна переменная (`--with-code`), обе выдачи сняты подряд в одной
сессии. Файл в `docs/` — исторический протокол «как было до корпуса кода»,
а не эталон для diff.

Исключение — гейт C04 «дефолтная выдача не изменилась»: он проверялся
непосредственно после реализации, пока vault не менялся, и дал **0/10
расхождений** (см. [code-index.md](code-index.md) §6).

## 2. Состояние системы на момент замера

| Метрика | Значение |
|---|---|
| Заметок в vault | 275 (0 неиндексированных) |
| Чанков памяти | 3216 (11.69 на заметку, 0 дубликатов) |
| Проиндексировано текста | 2 026 466 символов |
| `chunk_schema` памяти | 3 |
| По типам | reference 2038, learning 625, decision 233, changelog 130, rule 105, incident 53, howto 32 |
| Корпус sources | 3 документа (processed), 33 чанка |
| **Корпус кода** | **не существует** |
| Модель | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` |
| Тестов | **1492 passed, 5 skipped** за 106.59 s (собрано 1497) |
| Версия | `mb` 0.5.0 |

5 пропущенных — opt-in маркеры `ml_smoke` / `gemini_e2e` / `grok_e2e` /
`imgqa_gt` (`pyproject.toml`).

## 3. Контрольные запросы

Десять **реальных** вопросов к памяти, а не синтетика: каждый — форма того,
что действительно спрашивают во время работы над Singularity. Колонка
«ожидаемый тип» документирует заявку на покрытие (decision / howto / incident
/ reference / rule / learning); дрейф самого типа — тоже сигнал.

Часть запросов возвращает меньше пяти строк — это не ошибка, а отсечка
`min_score = 0.30`: движок предпочитает вернуть три релевантных попадания,
а не пять с двумя случайными.

### Q01 — «почему индекс пересобирается полностью» · ожидаемый тип: `decision`

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
| 3 | 0.5929 | `01_Genevra_Analysis/memory_system/Semantic_Search_Deep_Dive.md` | 3. search-all.py | memory |
| 4 | 0.5728 | `01_Genevra_Analysis/memory_system/Semantic_Search_Deep_Dive.md` | 2. Модель эмбеддингов | memory |

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
| 2 | 0.4621 | `01_Genevra_Analysis/memory_system/Semantic_Search_Deep_Dive.md` | 1. Архитектура | memory |
| 3 | 0.4403 | `learnings/active/STORYFORGE-RUNTIME.md` | 2026-07-20 10:10 | memory |
| 4 | 0.4311 | `learnings/active/STORYFORGE-QUALITY.md` | 2026-07-20 10:37 | memory |
| 5 | 0.4194 | `rules/Rule - repair-проход обязан снова пройти gate.md` | Почему | memory |

**Тайминги:** тёплый движок 13.5–18.1 мс на запрос (среднее 15.3 мс),
wall-clock через CLI 181–205 мс (модель уже загружена в демоне; разница —
старт процесса Python).

## 4. Чего этот baseline НЕ покрывает

* качество выдачи ПО КОДУ — эти десять запросов адресованы памяти; они
  измеряют вытеснение, а не полезность корпуса кода. Отдельная проба
  код-ориентированными запросами — в [code-index.md](code-index.md) §5;
* тайминги полного `mb index rebuild` по памяти (не пересобиралась —
  в этом и был смысл гейта C01);
* `mb doctor` / `mb selftest` — фиксируются в release gate C07.
