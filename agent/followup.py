"""Слежение за взятыми задачами: не отдали ли её, пока вы работали.

Самая обидная потеря в bounty — не «не нашёл задачу», а «две недели делал то,
что уже сделал кто-то другой». Публичные данные GitHub это показывают заранее:
соперник пишет ``/attempt``, открывает PR, ссылающийся на задачу; заказчик
закрывает issue или бот платформы объявляет о выплате.

Модуль сравнивает текущее состояние с тем, что ферма видела в прошлый раз, и
сообщает **изменение**, а не абсолютную цифру. «У задачи 3 открытых PR» — не
новость; новость — «с прошлой проверки их стало 3, вас обошли».

Никаких действий от имени человека: только наблюдение и совет.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from agent.config import get_env
from agent.ledger import connect, get_state, set_state
from agent.triage import TriageClient

#: Статусы, за которыми имеет смысл следить: задача уже в работе у человека.
ACTIVE_STATUSES = ("proposed", "working")

#: Сколько дней без движения считать затишьем.
DEFAULT_STALE_DAYS = 10

STATE_KEY = "followup.snapshot"


@dataclass
class Watch:
    opportunity_id: str
    title: str
    channel: str
    reward_usd: float
    status: str
    repo: str = ""
    number: int = 0
    verdict: str = "ok"
    reason: str = ""
    action: str = ""
    attempts_now: int = 0
    attempts_before: int = 0
    open_prs_now: int = 0
    open_prs_before: int = 0
    state: str = "open"
    stale_days: Optional[int] = None
    checked_at: str = ""
    changed: bool = False
    changes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "title": self.title,
            "channel": self.channel,
            "reward_usd": self.reward_usd,
            "status": self.status,
            "repo": self.repo,
            "number": self.number,
            "verdict": self.verdict,
            "reason": self.reason,
            "action": self.action,
            "attempts": self.attempts_now,
            "open_prs": self.open_prs_now,
            "issue_state": self.state,
            "stale_days": self.stale_days,
            "changed": self.changed,
            "changes": self.changes,
            "checked_at": self.checked_at,
        }


def active(limit: int = 20) -> List[Dict[str, Any]]:
    """Задачи, которые человек взял в работу."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, title, channel, reward_usd, status, payload FROM opportunities "
            "WHERE status IN (?, ?) ORDER BY reward_usd DESC LIMIT ?",
            (*ACTIVE_STATUSES, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _parse_github_id(opportunity_id: str) -> Optional[tuple[str, int]]:
    """«github:owner/repo#123» → («owner/repo», 123)."""
    if not opportunity_id.startswith("github:") or "#" not in opportunity_id:
        return None
    body = opportunity_id[len("github:"):]
    repo, _, number = body.rpartition("#")
    if not repo or not number.isdigit():
        return None
    return repo, int(number)


def _load_snapshots() -> Dict[str, Any]:
    return get_state(STATE_KEY, {}) or {}


def _save_snapshots(data: Dict[str, Any]) -> None:
    set_state(STATE_KEY, data)


def _stale_limit() -> int:
    from agent.config import get_float

    return int(get_float("FOLLOWUP_STALE_DAYS", float(DEFAULT_STALE_DAYS)))


def check(triage_client: Optional[TriageClient] = None, limit: int = 20) -> List[Watch]:
    """Проверить активные задачи и вернуть только то, что требует решения.

    Возвращаются все проверенные задачи, но у каждой есть вердикт: ``ok`` —
    ничего не изменилось, остальные — повод что-то сделать.
    """
    token = get_env("GITHUB_TOKEN", "") or ""
    client = triage_client or TriageClient(token=token)
    snapshots = _load_snapshots()
    stale_limit = _stale_limit()
    results: List[Watch] = []
    updated: Dict[str, Any] = {}

    for row in active(limit=limit):
        parsed = _parse_github_id(str(row["id"]))
        if not parsed:
            continue
        repo, number = parsed
        watch = Watch(
            opportunity_id=str(row["id"]),
            title=str(row["title"]),
            channel=str(row["channel"]),
            reward_usd=float(row["reward_usd"] or 0.0),
            status=str(row["status"]),
            repo=repo,
            number=number,
        )
        try:
            verdict = client.triage(
                opportunity_id=str(row["id"]), repo=repo, number=number
            )
        except Exception as exc:  # проверка не должна ломать команду
            watch.verdict = "unknown"
            watch.reason = f"не удалось проверить: {exc.__class__.__name__}"
            results.append(watch)
            continue

        before = snapshots.get(str(row["id"])) or {}
        watch.attempts_now = int(verdict.attempts)
        watch.attempts_before = int(before.get("attempts") or 0)
        watch.open_prs_now = int(verdict.open_prs)
        watch.open_prs_before = int(before.get("open_prs") or 0)
        watch.state = str(verdict.state or "unknown")
        watch.stale_days = verdict.stale_days
        watch.checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        changes: List[str] = []
        if watch.attempts_now > watch.attempts_before and before:
            delta = watch.attempts_now - watch.attempts_before
            changes.append(f"новых заявок от других: {delta}")
        if watch.open_prs_now > watch.open_prs_before and before:
            delta = watch.open_prs_now - watch.open_prs_before
            changes.append(f"новых открытых PR: {delta}")

        if verdict.paid:
            watch.verdict = "lost"
            watch.reason = "бот платформы сообщил о выплате — задача закрыта"
            watch.action = "Прекратить работу и проверить, кто получил выплату."
        elif watch.state == "closed":
            watch.verdict = "closed"
            watch.reason = "задача закрыта на GitHub"
            watch.action = (
                "Проверить, закрыта ли она вашим PR. Если нет — работу прекратить, "
                "время не записывать в доход."
            )
        elif watch.open_prs_now > 0:
            watch.verdict = "rival"
            watch.reason = (
                f"открытых PR по задаче: {watch.open_prs_now}"
                + (f" (было {watch.open_prs_before})" if before else "")
            )
            watch.action = (
                "Посмотреть, что сделал соперник: если его PR сильнее — бросить и "
                "переключиться на свежую задачу; если слабее — добавить в свой PR "
                "то, чего у него нет."
            )
        elif watch.stale_days is not None and watch.stale_days >= stale_limit:
            watch.verdict = "stale"
            watch.reason = f"в задаче нет движения {watch.stale_days} дн."
            watch.action = (
                "Написать короткий вопрос в задаче: актуален ли запрос. "
                "Если ответа нет — переключиться, время дороже."
            )

        watch.changes = changes
        watch.changed = bool(changes)
        if watch.changed and watch.verdict == "ok":
            watch.reason = "; ".join(changes)
            watch.action = "Следить дальше: пока преимущество у вас."
        results.append(watch)
        updated[str(row["id"])] = {
            "attempts": watch.attempts_now,
            "open_prs": watch.open_prs_now,
            "state": watch.state,
            "at": watch.checked_at,
        }

    if updated:
        snapshots.update(updated)
        _save_snapshots(snapshots)
    return results


def alerting(limit: int = 20) -> List[Watch]:
    """Только то, что требует действия прямо сейчас."""
    return [watch for watch in check(limit=limit) if watch.verdict not in ("ok", "unknown")]


def summary_lines(limit: int = 20, client: Optional[TriageClient] = None) -> List[str]:
    """Строки для Telegram и отчёта смены: плохие новости важнее хороших."""
    watches = check(triage_client=client, limit=limit)
    if not watches:
        return []
    lines: List[str] = []
    for watch in watches:
        if watch.verdict in ("ok", "unknown"):
            continue
        mark = "!!" if watch.verdict in ("lost", "closed") else "!"
        lines.append(f"{mark} {watch.title[:60]}: {watch.reason}. {watch.action}")
    return lines


def state(client: Optional[TriageClient] = None) -> Dict[str, Any]:
    """Снимок для дашборда: что проверено и что изменилось."""
    watches = check(triage_client=client, limit=10)
    counts: Dict[str, int] = {}
    for watch in watches:
        counts[watch.verdict] = counts.get(watch.verdict, 0) + 1
    return {
        "watched": len(watches),
        "verdicts": counts,
        "items": [watch.as_dict() for watch in watches],
    }
