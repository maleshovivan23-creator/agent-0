"""Отчёты площадок, снятые на раннере GitHub Actions.

Ферма живёт в песочнице, где домены площадок закрыты, а цикл площадок идёт на
раннере GitHub — оттуда сеть открыта. Мост между ними один и тот же для всех
площадок: workflow сохраняет JSON-отчёт в ``snapshots/``, а ферма читает его
локально и показывает задачи в очереди с пометкой источника и возраста.

Здесь живёт общая часть: путь, время съёмки, возраст, устаревание и разбор
обёртки. Каждая площадка добавляет своё: как выглядит одна её задача.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.config import project_root

#: Отчёт старше этого срока считается устаревшим: задачи разбирают за часы.
FRESH_HOURS = 24.0


@dataclass
class Snapshot:
    """Прочитанный отчёт: строки задач плюс честная информация о свежести."""

    path: str
    generated_at: str = ""
    age_hours: Optional[float] = None
    rows: List[Dict[str, Any]] = field(default_factory=list)
    payload: Dict[str, Any] = field(default_factory=dict)
    problem: str = ""
    fresh_hours: float = FRESH_HOURS

    @property
    def stale(self) -> bool:
        return self.age_hours is not None and self.age_hours > self.fresh_hours

    @property
    def usable(self) -> bool:
        return bool(self.rows) and not self.problem

    def note(self, what: str = "данные") -> str:
        """Одна строка для журнала: откуда данные и насколько они свежие."""
        if self.problem:
            return self.problem
        when = self.generated_at or "время неизвестно"
        age = "" if self.age_hours is None else f", {self.age_hours:.1f}ч назад"
        tail = " — отчёт устарел, проверьте задачи на площадке" if self.stale else ""
        return f"{what} из отчёта GitHub Actions ({when}{age}){tail}"


def read(rel_path: str, fresh_hours: float = FRESH_HOURS,
         keys: tuple = ("tasks", "quests", "items"),
         root: Optional[Path] = None) -> Snapshot:
    """Прочитать отчёт площадки, не ходя в сеть.

    ``keys`` — имена полей, под которыми площадка пишет список задач: у
    AgentHansa это ``quests``, у Taskmarket — ``tasks``.

    ``root`` нужен вызывающему модулю, чтобы можно было подменить каталог проекта
    в тестах: иначе отчёт читался бы из настоящего дерева.
    """
    target = (root or project_root()) / rel_path
    if not target.exists():
        return Snapshot(path=str(target), fresh_hours=fresh_hours, problem=(
            f"отчёта ещё нет: включите цикл в GitHub Actions (.github/workflows/hansa.yml) — "
            f"он сохранит {rel_path}"
        ))
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        return Snapshot(path=str(target), fresh_hours=fresh_hours,
                        problem=f"отчёт не читается: {exc}")
    if not isinstance(payload, dict):
        return Snapshot(path=str(target), fresh_hours=fresh_hours,
                        problem="отчёт неожиданного формата")

    generated = str(payload.get("generated_at") or "")
    rows: Any = []
    for key in keys:
        candidate = payload.get(key)
        if isinstance(candidate, dict):
            candidate = candidate.get("items") or candidate.get(key)
        if isinstance(candidate, list):
            rows = candidate
            break

    return Snapshot(
        path=str(target),
        generated_at=generated,
        age_hours=age_hours(generated),
        rows=[row for row in rows if isinstance(row, dict)],
        payload=payload,
        fresh_hours=fresh_hours,
    )


def age_hours(generated: str) -> Optional[float]:
    """Сколько часов назад снят отчёт. Неизвестное время — это не ноль часов."""
    if not generated:
        return None
    try:
        moment = datetime.fromisoformat(str(generated).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - moment).total_seconds() / 3600.0, 2)
