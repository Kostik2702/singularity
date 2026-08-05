---
name: component-intake
description: Карантин внешнего компонента до какого-либо попадания в проект — fetch без исполнения, sha256-провенанс, лицензионный классификатор, запрет lifecycle-скриптов, скан скрытых network/eval, обзор зависимостей, esbuild-проверка, вердикт PASS/ADAPT/REJECT. Тонкая обёртка над CLI `component-intake`. Use when "проверь компонент", "можно ли брать этот компонент", intake, quarantine, перед установкой/адаптацией любого внешнего кода из shortlist.
---

# component-intake — карантин перед адаптацией

CLI `component-intake` (движок, контракт `singularity/intake/CONTRACT.md`, component-intake/1). **v1 = inspect-first**: код кандидата НИКОГДА не исполняется, lifecycle-скрипты всегда denied, npm install отсутствует как класс. Потолок вердикта v1 — ADAPT (runtime-фазы typed unavailable; unavailable ≠ PASS).

> **Правило нулевого кода:** fetch/hash/скан/вердикт — субкоманда `run`. 0 новых скриптов.

## Порядок

```bash
# всегда через provenance-цепочку E02 (лицензия из реестра провайдера):
INTAKE_PROTECTED_ROOTS=<project> component-intake run \
  --candidates <candidates.json из pattern-scout> --id <candidate_id> \
  --workspace <disposable-dir ВНЕ проекта> --json
# exit 0 = PASS (недостижим в v1), 1 = ADAPT|REJECT (читать verdict.result), 2 = инструмент
```

## Как читать вердикт

- **REJECT** — не адаптируем: license unknown / lifecycle-скрипты / скрытый network / eval / build fail. Причины в `data.reject_reasons`. REJECT кандидата ≠ дефект конвейера.
- **ADAPT** — можно в implementation branch ТОЛЬКО через adaptation recipe (E04); `data.requirements` — обязательный вход рецепта (reduced-motion variant, lazy-island для heavy deps, runtime evidence v2).
- `checks.build.status=unavailable` — поставить `INTAKE_ESBUILD` на esbuild.exe любого vite-проекта; unavailable капит вердикт, не пропускает.
- `data.checks.snapshot` (sha256 + origin + retrieved_at) — провенанс, он обязан попасть в recipe/golden registry.

## Гейты

- [ ] Кандидат без записи в candidates.json (нет provenance-цепочки) = license unknown = REJECT — не «подсказывать» лицензию руками без проверки источника.
- [ ] Классификатор читает файл `LICENSE`, а не поле GitHub: `Other`/`NOASSERTION` = «автоопределение не узнало», не «лицензии нет». Если кандидат уже был кем-то рекомендован без чтения текста — рекомендация не является провенансом.
- [ ] `INTAKE_PROTECTED_ROOTS` выставлен на целевой проект ВСЕГДА.
- [ ] REJECT не оспаривается скилом; спорные случаи — вопрос владельцу с evidence.
- [ ] Вердикт и requirements — в learnings (`mb learn`); intake-report.json — evidence для E04.
