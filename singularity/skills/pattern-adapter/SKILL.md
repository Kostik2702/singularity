---
name: pattern-adapter
description: Адаптация внешнего компонента в проект через adaptation recipe — перенос design/motion-ДНК паттерна без demo-блоба: scaffold рецепта из intake-отчёта, разрешение TODO по методике (tokens, timing, breakpoints, reduced/static fallback, cleanup, lazy), валидация, re-intake после адаптации. Тонкая обёртка над `component-intake recipe`. Use when "адаптируй компонент", adaptation, "перенеси паттерн в проект", после ADAPT-вердикта карантина, до какого-либо кода в implementation branch.
---

# pattern-adapter — ДНК паттерна, не demo-блоб

CLI: `component-intake recipe` (контракт adaptation-recipe/1 в `singularity/intake/recipe.py`). Правило §7 решения: компонент не копируется как есть — создаётся рецепт, и **рецепт с TODO не проходит валидацию** → в implementation branch попадает только полный.

## Порядок

```bash
# 1. Только после intake-вердикта ADAPT (REJECT рецепта не имеет — валидатор не даст)
component-intake recipe --report <workspace>/intake-report.json \
  --out <evidence>/recipe-<candidate>.json --json
# факты (provenance sha256, license, intake requirements) заполнены; суждения = TODO

# 2. Разрешить TODO по методике ниже (руками/скилом), сверяясь с project manifest

# 3. Гейт полноты: exit 0 = рецепт готов, 1 = остались TODO/дыры
component-intake recipe --validate <evidence>/recipe-<candidate>.json

# 4. Реализация по рецепту в чистом branch/worktree; upstream-код и локальные
#    правки различимы (patch_set); затем RE-INTAKE адаптированного результата
#    (E03 --runtime) и visual acceptance
```

## Методика разрешения TODO

- **preserved_intent** — одно предложение: какую дизайн-функцию берём (напр. «постепенный blur контента при скролле»). Не «весь компонент».
- **replacements** — цвета/шрифты/размеры → design tokens проекта; демо-тексты/ассеты НЕ переносятся (лицензия демо-контента ≠ лицензия кода).
- **timing** — durations/easings → motion tokens проекта (единая грамматика, не случайные значения из демо).
- **breakpoints** — desktop/mobile поведение явно; «как в демо» не ответ.
- **reduced_motion** — из intake requirements; вариант обязан сохранять контент (не opacity:0 у важного).
- **static_fallback** — что видит no-JS/бот: смысл и CTA сохраняются.
- **cleanup** — listeners/rAF/observers/контексты при unmount (проверит motion-qa: rAF-after-settle).
- **lazy_strategy** — из intake (heavy deps → island); бюджет чанка.
- **patch_set** — список отличий от upstream: файл, что изменено, зачем.
- **acceptance** — пути скриншотов/perf-evidence, которыми доказана адаптация.

## Гейты

- [ ] Cross-framework порт (react → static и т.п.) — ОТДЕЛЬНОЕ решение владельца, не по умолчанию.
- [ ] Attribution-обязательства лицензии — в license.obligations и в реализацию.
- [ ] После адаптации — re-intake (E03 --runtime) зелёный + visual acceptance владельца.
- [ ] Рецепт + вердикты — в learnings; рецепт идёт в golden registry при promotion (E07).
