---
name: motion-qa
description: Гейт качества motion/3D живой страницы — scripted scroll timeline, long tasks, CLS, rAF-утечки после простоя, census WebGL-контекстов + принудительная потеря/восстановление, reduced-motion эмуляция, no-JS выживаемость контента; бюджеты из JSON с owner sign-off. Тонкая обёртка над scripts/motion-qa.mjs. Use when "проверь анимации/перф", motion QA, "почему греется/тормозит", перед merge motion/3D секции, приёмка слайса конвейера.
---

# motion-qa — цена движения, измеренная

Скрипт `scripts/motion-qa.mjs` (envelope motion-qa/1; Playwright из node_modules проекта). Evaluative: exit 0 = бюджеты зелёные, 1 = FAIL (список в verdict.reason), 2 = инструмент. **Unsupported-метрика = typed `unavailable` и итог не PASS** (максимум PASS-WITH-UNAVAILABLE) — недоизмеренное не проходит молча.

> **Правило нулевого кода:** timeline/longtasks/CLS/rAF/WebGL/reduced/no-JS — уже в скрипте. 0 новых скриптов.

```bash
node "D:\Singularity\singularity\skills\motion-qa\scripts\motion-qa.mjs" \
  --project "<корень проекта>" --url http://localhost:PORT/index.html \
  --out "<evidence ВНЕ проекта>" [--budgets budgets.json]
# waitUntil=load (не networkidle — стриминговое медиа никогда не idle, урок NIKA)
```

## Бюджеты (решение §9: числа замораживает ВЛАДЕЛЕЦ после baseline)

Дефолты — щедрые smoke-гарды: long tasks ≤5/≤200ms, CLS ≤0.1, rAF после
простоя ≤2/500ms, WebGL contexts ≤2, recovery обязателен, reduced-motion
вариант обязателен, no-JS контент обязателен. Проектный `budgets.json` —
единственный способ их менять; правка = owner sign-off, фиксировать в vault.

## Как читать

- `raf-after-settle` FAIL на живом фоне — не всегда баг: непрерывная фоновая
  анимация может быть B-решением владельца; вынести с evidence на
  owner-бюджет (напр. «пауза при out-of-viewport/idle»), не глушить гейт.
- `webgl-recovery` v1 доказывает восстановление БРАУЗЕРНОГО контекста;
  восстановление СЦЕНЫ приложения — v2 (typed unavailable не появится —
  фикс recovery-listener'ов проверять руками до v2).
- Негативные фикстуры гейта — `fixtures/` (leak-raf, no-reduced,
  many-contexts, clean): прогнать их = доказать, что гейт жив, прежде чем
  верить его PASS на проекте.
- Вердикт+finding'и — в learnings; report.json — evidence для WSF-11 viewer.
