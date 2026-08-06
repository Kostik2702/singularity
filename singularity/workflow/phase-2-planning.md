---
type: reference
title: "Phase 2 — Planning"
tags: [workflow, phase, singularity]
created: 2026-07-03
status: active
---

# Phase 2 — Planning

Родитель: `_router.md`. Для S-задач — `[➖]`.

- [ ] Декомпозиция на шаги с проверяемым результатом каждого (для XL — полноценная разбивка с зависимостями).
- [ ] Риски: минимум 1 gap найди в собственном плане (порт «try to find at least one gap») — что сломается первым? какой edge case не покрыт?
- [ ] Затрагивается бизнес-логика/поведение? → явное решение с owner (Never Decide Alone); рефакторинг сохраняет поведение бит-в-бит.
- [ ] Для арх. решений — заметка type=decision: альтернативы, выбор, почему, owner+source.
- [ ] План одобрен (пользователем — или, в автономном режиме, помечен `ASSUMED plan from investigation`).
- [ ] Авто-append: план + риски в learnings.

**Выход:** план с рисками → `phase-3-implementation.md`.
