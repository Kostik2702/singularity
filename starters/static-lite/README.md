# static-lite starter (WEBSITE-FACTORY WSF-10)

Стартер профиля `static-lite` (роутер E01): контентный сайт, эффекты — только
изолированные vanilla-острова. Никакого framework runtime.

## Запечённые конвенции

- **tokens-first**: цвета/шрифты/motion-числа только в `css/tokens.css`;
  модули читают их через `getComputedStyle`.
- **cascade layers**: `reset < primitives < layout` — дополнения в свой слой.
- **модули**: `js/modules/<name>.js` → `init<Name>()` → `[data-module]` →
  `safeInit` в `js/main.js`. Падение модуля не роняет сайт.
- **visual-test контракт**: `?visual-test=1` глушит анимации; каждый canvas
  рисует один seeded-кадр (mulberry32). Determinism проверяется harness'ом.
- **reduced-motion**: site-wide kill-switch уже в `css/main.css`.
- **truth-gate**: `__PLACEHOLDER__`-значения — НЕ факты; заменяются только
  данными владельца.

## Развёртывание нового проекта

1. Скопировать каталог, заменить `__PROJECT_NAME__` и плейсхолдеры (owner).
2. Заполнить `.factory/manifest.json` честно; `stackrouter` подтвердит профиль.
3. `npm install` → `npm run serve`.
4. Скил **visual-regression-bootstrap** — harness одной командой
   (baseline утверждает только владелец).
5. Паттерны — ТОЛЬКО из golden registry (`golden search` → `golden install
   --dest <проект>`) либо через полный путь scout→intake→recipe.
6. Гейты качества перед любым merge: determinism STABLE, motion-qa,
   a11y-audit, fidelity-audit.
