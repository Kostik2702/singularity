---
name: motion-3d-implementer
description: Реализация motion/3D-слайса из принятого adaptation recipe (или явно помеченного greenfield spike) — выбор самого дешёвого достаточного runtime (CSS → Web Animations → Canvas2D → GSAP → R3F → prerendered media), единая motion-грамматика, lifecycle-гигиена, progressive enhancement; каждая реализация закрывается motion-qa + determinism + fidelity evidence. Use when "реализуй секцию/эффект", implement slice, вёрстка motion/3D по рецепту, после pattern-adapter и до visual-приёмки.
---

# motion-3d-implementer — согласованная сцена, не коллекция эффектов

Вход строго один из двух: **валидный recipe** (`component-intake recipe --validate` exit 0) или **greenfield spike, явно помеченный** в learnings. Free-form «сделай красиво» — не вход.

> **Правило нулевого кода QA:** проверки — только существующими гейтами (motion-qa, check-visual-determinism, a11y-audit, fidelity-audit, imgqa). 0 новых скриптов за реализацию.

## 1. Выбор runtime — самый дешёвый достаточный (решение §8)

Спускайся сверху; каждый шаг вниз — только с причиной, записанной в learnings:

1. **CSS / Web Animations** — reveal/hover/state transitions.
2. **Canvas2D island** — плотные процедурные 2D (rain, glitch wall). Прецедент GT: letter-glitch порт = Canvas2D, 3KB, вместо 302KB React-пути (spike E01).
3. **GSAP** — сложные timeline/pin/scrub (только при реальной оркестровке).
4. **R3F/three** — только при scene graph / camera / materials из брифа; всегда lazy island + static fallback (constraints роутера S3).
5. **Prerendered media** — если интерактивность не нужна и real-time дороже.

Профиль проекта (stackrouter) — потолок: в static-lite React не появляется ради эффекта.

## 2. Обязательные свойства реализации (инварианты viewer'а)

- **Motion-грамматика:** durations/easings/тики — из токенов проекта или чисел рецепта; случайные значения «на глаз» = дефект.
- **Детерминизм:** `?visual-test=1` даёт байт-стабильный кадр (seeded PRNG, анимации off) — контракт harness WSF-08.
- **Lifecycle:** destroy() с cancelAnimationFrame + removeEventListener + observer.disconnect; пауза вне viewport (IntersectionObserver). Проверяет motion-qa `raf-after-settle`.
- **Reduced-motion:** статичный содержательный кадр + live-реакция на смену media query; site-wide CSS kill-switch обязателен.
- **No-JS:** смысл и CTA секции живут в разметке, не в canvas.
- **Wall-time, не кадры:** всё «за время» (распад частиц, fade, скорость) считается через `delta = time - lastTime` с clamp ~100 мс, движение нормализуется `pos += v * (delta/16.7)`. rAF не гарантирует 60fps: под троттлингом (headless-прогон, слабая машина, энергосбережение) «0.5 с при 60fps» превращается в 5-10 реальных секунд — декор виснет, e2e флейкают. Признак: константа вида `decay = 1/40` «на кадр», `alpha += STEP` без delta; тест стабилен в изоляции и флейкает в полном прогоне.
- **Иконка, которая должна краситься контекстом, инжектится как inline `<svg>`:** `<img src="icon.svg">` не наследует `currentColor` и CSS-переменные страницы. Делать системно (модуль-инжектор + пометка `data-inline-icon`), а не точечно; иначе иконки чернеют на цветных подложках.
- **Атрибуция лицензии** — в шапке кода, если рецепт требует.

## 3. Порядок

```bash
# 0. Recipe валиден (или spike помечен)
component-intake recipe --validate <recipe.json>          # exit 0 обязателен
# 1. Реализация в feature branch проекта по patch_set рецепта
# 2. Evidence-цикл (каждый круг, cap 3 итерации -> эскалация владельцу):
npm run check:visual-determinism                          # STABLE обязателен
node .../motion-qa.mjs --project <proj> --url <live> --budgets <proj budgets>
node .../fidelity-audit.mjs ... (скриншот для приёмки)
# 3. СТОП перед baseline: visual-приёмка и baseline — только владелец (GATE-5)
```

## GT-прецедент (эталон исполнения)

letter-glitch-wall в matrix-3d-web (2026-07-19): из accepted recipe, Canvas2D island, все инварианты §2 выполнены, motion-qa PASS 9/9, determinism STABLE ×3, два пойманных гейтами гэпа исправлены в том же цикле. Реализация обязана выдерживать это же качество evidence.
