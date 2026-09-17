"""The robot's outbox: finished work waiting for one human action.

Policy draws a hard line: the farm may search, rank, verify, plan and write, but
it must not post claims, submit quest answers or confirm payments on the
operator's behalf. That leaves a gap between "the robot found something" and
"money is possible" — this module closes it.

Everything the robot prepares lands here as a single item with an explicit
action for the human ("publish this comment", "send this quest answer"), the
file that holds the text, and a status. Nothing is ever silently dropped: an
item stays `ready` until a person marks it `published` or `skipped`.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from agent.ledger import connect, get_opportunity

#: How the human finishes each kind of work.
HUMAN_ACTIONS: Dict[str, str] = {
    "application": "Опубликовать заявку в задаче от своего имени",
    "quest": "Отправить текст квеста на площадке от своего имени",
    "brief": "Прочитать план, открыть scope и начать работу",
    "dossier": "Открыть досье: файлы-кандидаты и команды запуска уже собраны",
    "task": "Отправить работу на площадке от своего имени",
}

KIND_TITLES: Dict[str, str] = {
    "application": "Заявка",
    "quest": "Черновик квеста",
    "brief": "План работ",
    "dossier": "Досье",
    "task": "Черновик работы",
}

STATUSES = ("ready", "published", "skipped")


def add(
    opportunity_id: str,
    channel: str,
    kind: str,
    title: str,
    path: str = "",
    summary: str = "",
    action: str = "",
) -> int:
    """Queue one artifact. Re-queueing the same (item, kind) returns the old id."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id FROM inbox WHERE opportunity_id=? AND kind=? AND status='ready'",
            (opportunity_id, kind),
        ).fetchone()
        if row:
            return int(row["id"])
        cursor = conn.execute(
            "INSERT INTO inbox(opportunity_id, channel, kind, title, path, action, summary) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                opportunity_id,
                channel,
                kind,
                title,
                path,
                action or HUMAN_ACTIONS.get(kind, "Сделать шаг вручную"),
                summary,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def _row_to_dict(row: Any) -> Dict[str, Any]:
    item = dict(row)
    item["kind_title"] = KIND_TITLES.get(item.get("kind", ""), item.get("kind", ""))
    item["action"] = item.get("action") or HUMAN_ACTIONS.get(item.get("kind", ""), "")
    return item


def has_ready(opportunity_id: str, kind: str) -> bool:
    """True when this exact artifact is already waiting for the human."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM inbox WHERE opportunity_id=? AND kind=? AND status='ready' LIMIT 1",
            (opportunity_id, kind),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def pending(limit: int = 20) -> List[Dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM inbox WHERE status='ready' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(row) for row in rows]


def recent(limit: int = 20) -> List[Dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM inbox ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(row) for row in rows]


def get(item_id: int) -> Optional[Dict[str, Any]]:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM inbox WHERE id=?", (item_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row) if row else None


def resolve(item_id: int, status: str) -> bool:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    conn = connect()
    try:
        cursor = conn.execute(
            "UPDATE inbox SET status=?, resolved_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status='ready'",
            (status, item_id),
        )
        conn.commit()
        resolved = cursor.rowcount > 0
    finally:
        conn.close()

    if resolved and status in ("published", "skipped"):
        item = get(item_id)
        _learn_from_decision(item or {}, status)
    return resolved


def _learn_from_decision(item: Dict[str, Any], status: str) -> None:
    """Решение человека — единственный честный сигнал, что стоит брать дальше."""
    try:
        from agent import learning
        from agent.ledger import get_opportunity

        opportunity_id = str(item.get("opportunity_id") or "")
        if not opportunity_id:
            return
        opportunity = get_opportunity(opportunity_id) or {}
        payload = opportunity.get("payload") or {}
        if isinstance(payload, str):
            import json as _json

            try:
                payload = _json.loads(payload or "{}")
            except Exception:
                payload = {}
        learning.decide(
            opportunity_id,
            status,
            channel=str(opportunity.get("channel") or item.get("channel") or ""),
            playbook=str(payload.get("playbook") or ""),
            reward_usd=float(opportunity.get("reward_usd") or 0.0),
            query=str(payload.get("query") or ""),
        )
        learning.mark_updated()
    except Exception:
        pass  # обучение не должно мешать работе очереди


def counts() -> Dict[str, int]:
    conn = connect()
    try:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM inbox GROUP BY status").fetchall()
    finally:
        conn.close()
    result = {status: 0 for status in STATUSES}
    for row in rows:
        result[row["status"]] = int(row["n"])
    return result


def text(item_id: int) -> str:
    """The exact text the human will publish, taken from the artifact file."""
    from pathlib import Path

    from agent.config import project_root

    item = get(item_id)
    if not item:
        return ""
    raw = item.get("path") or ""
    path = Path(raw)
    if raw and not path.is_absolute():
        path = project_root() / path
    if raw and path.exists():
        return path.read_text(encoding="utf-8")
    opportunity = get_opportunity(item.get("opportunity_id") or "")
    return json.dumps(opportunity, ensure_ascii=False, indent=2) if opportunity else ""


def mark_published(opportunity_id: str) -> None:
    """Called when the operator reports the work as submitted."""
    conn = connect()
    try:
        conn.execute(
            "UPDATE inbox SET status='published', resolved_at=CURRENT_TIMESTAMP "
            "WHERE opportunity_id=? AND status='ready'",
            (opportunity_id,),
        )
        conn.commit()
    finally:
        conn.close()
