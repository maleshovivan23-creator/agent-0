"""Обучение робота на решениях человека.

Ферма не может знать заранее, какой поисковый запрос приносит работу, а какой
только мусор, — это выясняется по факту. Модуль запоминает три вещи:

* **наблюдение** — сколько новых задач дал запрос в проходе;
* **решение** — что человек сделал с подготовленным текстом (отправил или пропустил);
* **результат** — сколько денег реально подтверждено по каналу и плейбуку.

Дальше это превращается в вес: запросы, из которых выходили отправленные заявки и
деньги, идут в начало очереди; запросы без пользы опускаются, но не исчезают —
рынок меняется, и «мёртвый» сегодня запрос может ожить через месяц. Никаких
случайных решений: только статистика по фактам, с минимальным числом наблюдений.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent.ledger import connect, get_state, set_state

#: Меньше этого числа наблюдений — вес нейтральный: статистики ещё нет.
MIN_OBSERVATIONS = 3

#: Вес не выходит за эти границы: даже худший запрос получает шанс.
MIN_WEIGHT = 0.3
MAX_WEIGHT = 2.0

#: Норма полезности на один проход: ниже — запрос опускается, выше — поднимается.
BASELINE_YIELD = 1.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS learning_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    channel TEXT,
    value REAL DEFAULT 0,
    detail TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_learning_kind ON learning_events(kind, subject);
"""


def _ensure_schema() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def record(kind: str, subject: str, channel: str = "", value: float = 0.0,
           detail: str = "") -> None:
    """Записать событие обучения. Виды: observation, decision, outcome."""
    _ensure_schema()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO learning_events(kind, subject, channel, value, detail) "
            "VALUES(?, ?, ?, ?, ?)",
            (kind, subject, channel, float(value), detail),
        )
        conn.commit()
    finally:
        conn.close()


def observe(query: str, new_items: int, channel: str = "github_bounties") -> None:
    """Отметить, сколько новых задач принёс запрос в этом проходе."""
    if not query:
        return
    record("observation", query, channel=channel, value=float(new_items))


def decide(opportunity_id: str, action: str, channel: str = "", playbook: str = "",
           reward_usd: float = 0.0, query: str = "") -> None:
    """Решение человека: отправил (published) или пропустил (skipped).

    Решение привязывается к поисковому запросу, который нашёл задачу: иначе
    робот знал бы, что задача отклонена, но не понимал бы, какой запрос её
    принёс. Без запроса событие остаётся привязанным к самой задаче.
    """
    if not opportunity_id and not query:
        return
    record(
        "decision",
        query or opportunity_id,
        channel=channel,
        value=1.0 if action == "published" else 0.0,
        detail=json.dumps(
            {
                "action": action,
                "playbook": playbook,
                "reward_usd": reward_usd,
                "opportunity_id": opportunity_id,
            },
            ensure_ascii=False,
        ),
    )


def outcome(channel: str, amount: float, subject: str = "", detail: str = "") -> None:
    """Подтверждённые деньги: единственный настоящий результат."""
    record("outcome", subject or channel, channel=channel, value=float(amount), detail=detail)


@dataclass
class SubjectStats:
    subject: str
    observations: int = 0
    new_items: float = 0.0
    decisions: int = 0
    published: int = 0
    skipped: int = 0
    earned_usd: float = 0.0

    @property
    def yield_score(self) -> float:
        """Полезность запроса: свежие задачи плюс отправленные заявки, минус отказы."""
        return round(
            self.new_items * 0.5 + self.published * 3.0 - self.skipped * 0.5, 3
        )

    @property
    def weight(self) -> float:
        """1.0 — нейтрально; 0.7 — запрос пустой, 1.5+ — приносит работу.

        Считается полезность на один проход относительно нормы: пустой запрос
        опускается, продуктивный поднимается, но не исчезает совсем.
        """
        if self.observations < MIN_OBSERVATIONS:
            return 1.0
        per_observation = self.yield_score / max(1, self.observations)
        raw = 1.0 + (per_observation - BASELINE_YIELD) / 3.0
        return round(max(MIN_WEIGHT, min(MAX_WEIGHT, raw)), 2)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "subject": self.subject,
            "observations": self.observations,
            "new_items": round(self.new_items, 1),
            "decisions": self.decisions,
            "published": self.published,
            "skipped": self.skipped,
            "earned_usd": round(self.earned_usd, 2),
            "weight": self.weight,
        }


def _stats_for(kind_filter: str = "") -> Dict[str, SubjectStats]:
    _ensure_schema()
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT kind, subject, value, detail FROM learning_events "
            + ("WHERE kind=?" if kind_filter else ""),
            (kind_filter,) if kind_filter else (),
        ).fetchall()
    finally:
        conn.close()

    stats: Dict[str, SubjectStats] = {}
    for row in rows:
        subject = row["subject"]
        item = stats.setdefault(subject, SubjectStats(subject=subject))
        if row["kind"] == "observation":
            item.observations += 1
            item.new_items += float(row["value"] or 0)
        elif row["kind"] == "decision":
            item.decisions += 1
            if float(row["value"] or 0) > 0:
                item.published += 1
            else:
                item.skipped += 1
        elif row["kind"] == "outcome":
            item.earned_usd += float(row["value"] or 0)
    return stats


def query_stats() -> List[SubjectStats]:
    """Статистика по поисковым запросам (те, о которых есть наблюдения)."""
    stats = [item for item in _stats_for().values() if item.observations]
    stats.sort(key=lambda item: (item.weight, item.new_items), reverse=True)
    return stats


def query_weights() -> Dict[str, float]:
    return {item.subject: item.weight for item in query_stats()}


def order_queries(queries: List[str]) -> List[str]:
    """Разложить запросы так, чтобы продуктивные шли первыми.

    Порядок, а не отбрасывание: бюджет запросов тратится сначала на то, что
    приносило деньги, но остальные продолжают проверяться.
    """
    weights = query_weights()
    indexed = list(enumerate(queries))
    indexed.sort(key=lambda pair: (-weights.get(pair[1], 1.0), pair[0]))
    return [query for _, query in indexed]


def playbook_stats() -> List[SubjectStats]:
    """Статистика по плейбукам: где мы реально зарабатывали."""
    _ensure_schema()
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT detail, value, channel FROM learning_events WHERE kind IN ('decision','outcome')"
        ).fetchall()
    finally:
        conn.close()

    stats: Dict[str, SubjectStats] = {}
    for row in rows:
        try:
            detail = json.loads(row["detail"] or "{}")
        except Exception:
            detail = {}
        key = detail.get("playbook") or row["channel"] or "без плейбука"
        item = stats.setdefault(key, SubjectStats(subject=key))
        item.decisions += 1
        if float(row["value"] or 0) > 0:
            item.published += 1
        else:
            item.skipped += 1
    result = list(stats.values())
    result.sort(key=lambda item: item.published, reverse=True)
    return result


def summary_lines() -> List[str]:
    """Что робот понял: короткий отчёт для CLI и дашборда."""
    lines: List[str] = []
    queries = query_stats()
    if not queries:
        return [
            "Пока нечего обобщать: робот начнёт учиться, когда вы отправите или "
            "пропустите первые задания (входящие-отправлено / входящие-пропустить).",
        ]

    best = queries[0]
    lines.append(
        f"Продуктивный запрос: «{best.subject[:60]}» — новых задач {best.new_items:.0f}, "
        f"отправлено {best.published}, пропущено {best.skipped}, вес {best.weight}"
    )
    weak = [item for item in queries if item.weight < 1.0 and item.observations >= MIN_OBSERVATIONS]
    for item in weak[:3]:
        lines.append(
            f"Мало пользы от «{item.subject[:60]}»: {item.observations} проходов, "
            f"отправлено {item.published}, пропущено {item.skipped} — запрос уйдёт в конец очереди."
        )

    playbooks = [item for item in playbook_stats() if item.decisions]
    if playbooks:
        top = playbooks[0]
        lines.append(
            f"Лучше всего заходит работа типа «{top.subject}»: отправлено {top.published} "
            f"из {top.decisions}."
        )
    return lines


def totals() -> Dict[str, Any]:
    """Итоги по всем событиям, включая деньги без привязки к запросу."""
    stats = _stats_for()
    return {
        "observations": sum(item.observations for item in stats.values()),
        "decisions": sum(item.decisions for item in stats.values()),
        "published": sum(item.published for item in stats.values()),
        "skipped": sum(item.skipped for item in stats.values()),
        "earned_usd": round(sum(item.earned_usd for item in stats.values()), 2),
    }


def learned_state() -> Dict[str, Any]:
    """Снимок для дашборда и health-проверки."""
    queries = query_stats()
    totals_data = totals()
    return {
        "queries": [item.as_dict() for item in queries[:10]],
        "playbooks": [item.as_dict() for item in playbook_stats()[:10]],
        **totals_data,
        "updated_at": get_state("learning.updated_at"),
    }


def mark_updated() -> None:
    from datetime import datetime, timezone

    set_state("learning.updated_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))


def advise(limit: int = 3) -> Optional[str]:
    """Одна строка совета — или None, если данных ещё нет."""
    lines = [line for line in summary_lines() if not line.startswith("Пока нечего")]
    if not lines:
        return None
    return lines[0]
