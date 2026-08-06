---
type: reference
title: "Phase 1.5 — Grill"
tags: [workflow, phase, singularity]
created: 2026-07-03
status: active
---

# Phase 1.5 — Grill (гейт перед планом/дизайном)

Родитель: `_router.md`. Исполняется skill'ом `grill`.

- [ ] Запусти skill `grill` (auto-режим по умолчанию для соло; normal — если пользователь на связи и задача мутная).
- [ ] Self-resolve first: скрытые допущения, неопределённые термины, границы scope — сначала ищи ответы сам (код + `mb search`), к пользователю — только survivors.
- [ ] Результат — блок `## Grill` в learnings: Q → A (source), открытые вопросы со статусом OPEN.
- [ ] Гейт: план не пишется, пока OPEN-вопросы, влияющие на архитектуру, не закрыты или явно приняты как риск.

**Выход:** допущения выявлены и закрыты → `phase-2-planning.md`.
