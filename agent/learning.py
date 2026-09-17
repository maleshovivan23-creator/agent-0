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
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
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
    samples INTEGER DEFAULT 0,
    detail TEXT,
    day TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_learning_kind ON learning_events(kind, subject);
"""

#: Сколько дней держать наблюдения: дальше рынок меняется и статистика врёт.
OBSERVATION_DAYS = 120


def _ensure_schema() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(learning_events)")}
        if "day" not in existing:
            conn.execute("ALTER TABLE learning_events ADD COLUMN day TEXT")
            conn.execute(
                "UPDATE learning_events SET day = substr(created_at, 1, 10) WHERE day IS NULL"
            )
        if "samples" not in existing:
            conn.execute("ALTER TABLE learning_events ADD COLUMN samples INTEGER DEFAULT 0")
            conn.execute("UPDATE learning_events SET samples = 1 WHERE samples IS NULL OR samples = 0")
        # индекс по дню создаётся после добавления столбца: на старой базе
        # CREATE INDEX с несуществующим столбцом падает
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_learning_day ON learning_events(kind, subject, day)"
        )
        conn.commit()
    finally:
        conn.close()

    _collapse_old_observations()


def _collapse_old_observations() -> None:
    """Схлопнуть наблюдения, накопленные до суточной агрегации.

    Раньше на каждый проход писалась своя строка, а `day` проставился уже при
    миграции: за сутки по одному запросу лежало несколько десятков строк. Если
    оставить их, то суммирующий UPDATE трогает их все сразу и статистика
    раздувается в разы (живой случай: 19 проходов выглядели как 322). Поэтому
    один раз сводим строки суток в самую раннюю, остальные удаляем.
    """
    conn = connect()
    try:
        done = get_state("learning.collapsed")
        if done:
            return
        cursor = conn.execute(
            "SELECT COUNT(*) AS n FROM (SELECT 1 FROM learning_events " 
            "WHERE kind='observation' AND day IS NOT NULL GROUP BY subject, day HAVING COUNT(*) > 1)"
        )
        if cursor.fetchone()["n"]:
            conn.execute(
                "UPDATE learning_events SET value = ("
                "  SELECT SUM(value) FROM learning_events AS o WHERE o.kind='observation' "
                "  AND o.day = learning_events.day AND o.subject = learning_events.subject), "
                "samples = ("
                "  SELECT SUM(samples) FROM learning_events AS o WHERE o.kind='observation' "
                "  AND o.day = learning_events.day AND o.subject = learning_events.subject) "
                "WHERE kind='observation' AND id IN ("
                "  SELECT MIN(id) FROM learning_events WHERE kind='observation' "
                "  AND day IS NOT NULL GROUP BY subject, day)"
            )
            conn.execute(
                "DELETE FROM learning_events WHERE kind='observation' AND day IS NOT NULL "
                "AND id NOT IN (SELECT MIN(id) FROM learning_events WHERE kind='observation' "
                "AND day IS NOT NULL GROUP BY subject, day)"
            )
        # уникальный индекс можно ставить только после схлопывания: пока в базе
        # лежат старые дубли, он не создастся
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_learning_observation_day "
            "ON learning_events(subject, day) WHERE kind = 'observation'"
        )
        conn.commit()
    finally:
        conn.close()
    set_state("learning.collapsed", "1")


def record(kind: str, subject: str, channel: str = "", value: float = 0.0,
           detail: str = "") -> None:
    """Записать событие обучения. Виды: observation, decision, outcome.

    Наблюдения за поисковыми запросами суммируются по суткам: робот делает
    проход каждые пять минут, и сырых строк набежало бы полмиллиона за пару
    месяцев — таблица росла бы быстрее, чем польза от неё. Решения человека и
    выплаты пишутся по одной записи на событие: их терять нельзя.
    """
    _ensure_schema()
    conn = connect()
    try:
        if kind == "observation":
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            # Строка суток ровно одна: обновляем самую раннюю. Условие «day=?»
            # без id обновляло бы все строки суток сразу и считало один проход
            # несколько раз. Уникальный индекс закрывает гонку двух процессов.
            update = (
                "UPDATE learning_events SET value = value + ?, samples = samples + 1, "
                "detail = ? WHERE kind = ? AND subject = ? AND day = ? "
                "AND id = (SELECT MIN(id) FROM learning_events WHERE kind = ? AND "
                "subject = ? AND day = ?)"
            )
            args = (float(value), detail, kind, subject, day, kind, subject, day)
            cursor = conn.execute(update, args)
            if cursor.rowcount == 0:
                try:
                    conn.execute(
                        "INSERT INTO learning_events(kind, subject, channel, value, samples, "
                        "detail, day) VALUES(?, ?, ?, ?, ?, ?, ?)",
                        (kind, subject, channel, float(value), 1, detail, day),
                    )
                except sqlite3.IntegrityError:
                    conn.execute(update, args)
        else:
            conn.execute(
                "INSERT INTO learning_events(kind, subject, channel, value, samples, detail) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (kind, subject, channel, float(value), 1, detail),
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


def prune(days: int = OBSERVATION_DAYS) -> int:
    """Удалить устаревшие наблюдения. Решения и выплаты не трогаем никогда."""
    _ensure_schema()
    conn = connect()
    try:
        cursor = conn.execute(
            "DELETE FROM learning_events WHERE kind = 'observation' "
            "AND created_at < datetime('now', ?)",
            (f"-{int(days)} days",),
        )
        conn.commit()
        return int(cursor.rowcount or 0)
    finally:
        conn.close()


def _stats_for(kind_filter: str = "") -> Dict[str, SubjectStats]:
    _ensure_schema()
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT kind, subject, value, samples, detail FROM learning_events "
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
            # Строка суммирует сутки, поэтому число проходов хранится отдельно
            # в samples: вес считается по проходам, а таблица не растёт.
            item.observations += int(row["samples"] or 1)
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
    set_state("learning.updated_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))


def advise(limit: int = 3) -> Optional[str]:
    """Одна строка совета — или None, если данных ещё нет."""
    lines = [line for line in summary_lines() if not line.startswith("Пока нечего")]
    if not lines:
        return None
    return lines[0]
