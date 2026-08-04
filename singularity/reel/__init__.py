"""Reel editing engine — the deterministic half of the `reel-editor` skill.

Everything here is measurement and rendering; every judgment call (which shot,
which register, is the emotional ladder right) stays in the skill and, above it,
with the owner. The method these modules serve lives in the vault:

  howto/Монтаж пачки i2v-клипов в один ролик - пошаговый плейбук
  howto/Промо-текст поверх вертикального ролика - типографика, motion и проверка контраста
  reference/Вспышки и фотосенситивность в short-form - гейт и метод проверки
  reference/Субтитры для вертикального видео · Motion design для short-form
  reference/Антипаттерны short-form монтажа _ QA чек-лист
  reference/Эмоциональная непрерывность сцены

Consult it with `mb search` before editing anything — the numbers below were
derived from real cuts and are recorded there with their evidence.
"""
from __future__ import annotations

ENVELOPE = "reel/1"
__all__ = ["ENVELOPE"]
