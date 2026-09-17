"""Многозадачный режим: робот ведёт несколько заказов одновременно.

Обычный проход (``автопилот``/``цикл``) готовит артефакты по одному: собрал,
записал, пошёл дальше. Многозадачный режим отличается тремя вещами:

1. **Берёт несколько задач сразу.** Пул потоков ведёт N заказов параллельно, и
   падение одной задачи не останавливает остальные — ошибка попадает в журнал,
   а не в лицо оператору.
2. **Сам решает, что может закрыть.** Для каждой задачи видно, кто её закрывает:
   робот (текст, черновик, досье, план — всё это он делает сам) или человек
   (аудит-контест, разведка по чужому scope). Робот берёт только своё, и в этом
   журнале видно, почему остальное оставлено человеку.
3. **Ведёт журнал заказов.** ``reports/multitask-<дата>.md``: что взято, во что
   превратилось, сколько заняло, что осталось за человеком и какой ровно командой
   это отправляется. Часы работы робота и часы человека в журнале разделены:
   складывать их в один счётчик значило бы врать в ставке $/час.

Граница остаётся прежней и не прячется: робот доводит работу до готового текста
и доказательства, а отправка на площадку (публикация заявки, ответ на квест,
работа с кошельком) — одно действие человека, потому что ключ площадки и
ответственность за текст принадлежат ему. Модуль только убирает остальную работу.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agent.config import get_float, project_root
from agent.farm import floor_report, next_actions
from agent.ledger import get_opportunity, set_status

#: Сколько заказов робот ведёт одновременно, если не сказано иначе.
DEFAULT_PARALLEL = 3

#: Сколько минут на один заказ, прежде чем считать его зависшим.
DEFAULT_TIMEOUT = 180.0

#: Канал → (вид артефакта, что именно делает робот). У этих каналов работа робота
#: заканчивается готовым текстом или планом, который человек проверяет и отправляет.
AUTO_ROUTES = {
    "agent_marketplaces": ("quest", "робот пишет черновик ответа, человек отправляет"),
    "github_bounties": ("application", "робот пишет заявку и план работ, человек публикует"),
    "audit_contests": ("brief", "робот готовит досье и план работ — где править и что запускать"),
}
#: Вид артефакта для каналов без явного маршрута.
AUTO_KINDS = {channel: kind for channel, (kind, _reason) in AUTO_ROUTES.items()}

#: Каналы, где нужен человек с его руками: разведка по чужому scope, выплаты.
HUMAN_REASONS = {
    "bug_recon": "нужен data/scope.yaml и авторизация программы — это подпись человека",
}


@dataclass
class WorkItem:
    """Один заказ в работе."""

    id: str
    title: str
    channel: str
    kind: str
    reward_usd: float = 0.0
    ev_per_hour: float = 0.0
    owner: str = "робот"          # робот | человек
    reason: str = ""
    steps: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "channel": self.channel,
            "kind": self.kind, "reward_usd": self.reward_usd,
            "ev_per_hour": self.ev_per_hour, "owner": self.owner,
            "reason": self.reason, "steps": list(self.steps),
        }


@dataclass
class WorkResult:
    item_id: str
    kind: str
    ok: bool
    seconds: float = 0.0
    path: str = ""
    note: str = ""
    error: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.item_id, "kind": self.kind, "ok": self.ok,
            "seconds": round(self.seconds, 2), "path": self.path,
            "note": self.note, "error": self.error,
        }


def capability(channel: str, payload: Optional[Dict[str, Any]] = None) -> tuple[str, str]:
    """Кто закрывает задачу: робот (``auto``) или человек (``human``).

    Робот берёт то, что доводит до готового текста своими силами. Всё, что
    требует решения человека (анализ кода контеста, авторизация в программе),
    помечается честно — иначе «многозадачность» превращается в имитацию работы.
    """
    payload = payload or {}
    if channel in HUMAN_REASONS:
        return "human", HUMAN_REASONS[channel]
    if payload.get("expired"):
        return "human", "задание закрыто — работа не нужна"
    if channel in AUTO_ROUTES:
        return "auto", AUTO_ROUTES[channel][1]
    return "human", "нет автоматического маршрута для этого канала"


def plan(limit: int = 4, channels: Optional[List[str]] = None,
         min_ev_per_hour: Optional[float] = None) -> Dict[str, Any]:
    """Собрать список заказов в работу: что берёт робот и что остаётся человеку.

    Задачи берутся из той же очереди, что показывает ``дальше``: порог ценности
    уважается, ничего «на всякий случай» в план не попадает.
    """
    actions = next_actions(limit=max(limit * 2, limit + 2), min_ev_per_hour=min_ev_per_hour)
    items: List[WorkItem] = []
    left: List[Dict[str, Any]] = []
    for action in actions:
        if channels and action["channel"] not in channels:
            continue
        opportunity = get_opportunity(str(action["id"]))
        if not opportunity:
            continue
        payload = opportunity.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload or "{}")
            except Exception:
                payload = {}
        owner, reason = capability(str(action["channel"]), payload or {})
        item = WorkItem(
            id=str(action["id"]),
            title=str(action["title"]),
            channel=str(action["channel"]),
            kind=AUTO_KINDS.get(str(action["channel"]), "dossier"),
            reward_usd=float(action.get("reward_usd") or 0.0),
            ev_per_hour=float(action.get("ev_per_hour") or 0.0),
            owner=owner,
            reason=reason,
            steps=list(action.get("next") or []),
        )
        if owner == "auto" and len(items) < limit:
            items.append(item)
        else:
            left.append(item.as_dict())

    report = floor_report(min_ev_per_hour=min_ev_per_hour)
    return {
        "items": [item.as_dict() for item in items],
        "left_to_human": left,
        "floor": report["floor"],
        "queued": report["queued"],
        "above_floor": report["above"],
        "parallel": int(get_float("MULTITASK_PARALLEL", DEFAULT_PARALLEL)),
    }


def _default_worker(item: WorkItem) -> WorkResult:
    """Выполнить один заказ: собрать готовый артефакт и положить его во входящие."""
    from agent import autopilot

    opportunity = get_opportunity(item.id)
    if not opportunity:
        return WorkResult(item_id=item.id, kind=item.kind, ok=False,
                          error="задача исчезла из базы")
    contribution = autopilot._prepare_one(opportunity)
    if contribution is None:
        return WorkResult(item_id=item.id, kind=item.kind, ok=True,
                          note="готовый артефакт уже ждёт человека")
    if contribution.get("skipped"):
        return WorkResult(item_id=item.id, kind=item.kind, ok=False,
                          note=str(contribution.get("reason") or "пропущено"))
    return WorkResult(item_id=item.id, kind=item.kind, ok=True,
                      path=str(contribution.get("path") or ""),
                      note=f"во входящих №{contribution.get('inbox_id')}")


def run(items: List[Dict[str, Any]], parallel: Optional[int] = None,
        timeout: Optional[float] = None,
        worker: Optional[Callable[[WorkItem], WorkResult]] = None,
        take: bool = True, journal: bool = True) -> Dict[str, Any]:
    """Провести заказы параллельно и вернуть журнал прохода.

    ``take=True`` помечает взятые задачи рабочими: робот действительно берёт их,
    а не показывает список. Публикация и выплаты по-прежнему за человеком.
    """
    workers = parallel if parallel is not None else int(get_float("MULTITASK_PARALLEL", DEFAULT_PARALLEL))
    workers = max(1, min(workers, 12))
    per_item = timeout if timeout is not None else get_float("MULTITASK_TIMEOUT", DEFAULT_TIMEOUT)
    work = [WorkItem(**item) if isinstance(item, dict) else item for item in items]
    do = worker or _default_worker
    started = time.time()

    lock = threading.Lock()
    results: List[WorkResult] = []

    def one(item: WorkItem) -> WorkResult:
        begin = time.time()
        try:
            result = do(item)
        except Exception as exc:  # одна сломанная задача не роняет проход
            result = WorkResult(item_id=item.id, kind=item.kind, ok=False,
                                error=f"{exc.__class__.__name__}: {exc}")
        result.seconds = time.time() - begin
        return result

    if take:
        for item in work:
            try:
                set_status(item.id, "working")
            except Exception:
                pass

    if work:
        with ThreadPoolExecutor(max_workers=min(workers, len(work))) as pool:
            futures = {pool.submit(one, item): item for item in work}
            for future, item in futures.items():
                try:
                    result = future.result(timeout=per_item)
                except FutureTimeout:
                    result = WorkResult(item_id=item.id, kind=item.kind, ok=False,
                                        error=f"не уложился в {per_item:.0f} с")
                except Exception as exc:
                    result = WorkResult(item_id=item.id, kind=item.kind, ok=False,
                                        error=f"{exc.__class__.__name__}: {exc}")
                with lock:
                    results.append(result)

    payload = {
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seconds": round(time.time() - started, 2),
        "parallel": workers,
        "taken": [item.id for item in work] if take else [],
        "results": [result.as_dict() for result in sorted(results, key=lambda r: r.item_id)],
        "ok": sum(1 for result in results if result.ok),
        "failed": sum(1 for result in results if not result.ok),
    }
    if journal:
        payload["journal"] = str(write_journal(payload))
    payload["report"] = summarize(payload)
    return payload


def write_journal(payload: Dict[str, Any]) -> Path:
    """Журнал прохода: его читает человек, поэтому он на русском и с командами."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = project_root() / "reports" / f"multitask-{stamp}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(summarize(payload), encoding="utf-8")
    return path


def summarize(payload: Dict[str, Any]) -> str:
    lines = [
        f"# Многозадачный проход — {payload.get('started_at', '')}",
        "",
        f"- Взято в работу: {len(payload.get('taken') or [])} "
        f"(одновременно {payload.get('parallel')})",
        f"- Готово: {payload.get('ok', 0)} · с ошибкой: {payload.get('failed', 0)}",
        f"- Заняло: {payload.get('seconds', 0)} с",
        "",
        "## Что сделано роботом",
        "",
    ]
    for result in payload.get("results") or []:
        mark = "готово" if result["ok"] else "ошибка"
        detail = result["path"] or result["note"] or result["error"] or ""
        lines.append(f"- {mark}: `{result['id']}` → {detail} ({result['seconds']} с)")
    lines += [
        "",
        "## Что остаётся человеку",
        "",
        "- Отправка на площадку: `площадка отправить <id> --файл <черновик> --подтверждаю`",
        "- Публикация заявки от своего имени: ключ площадки и ответственность за текст — ваши",
        "- Часы своей работы: `python -m agent.main часы --channel <канал> --hours <часы>`",
        "- Подтверждение выплаты: `python -m agent.main выплата-подтвердить <id>` — только это доход",
        "",
        "> Часы робота и часы человека считаются отдельно: складывать их значило бы",
        "> завышать ставку $/час.",
        "",
    ]
    return "\n".join(lines)


def state() -> Dict[str, Any]:
    """Сколько заказов сейчас в работе у человека (готовые артефакты)."""
    from agent import inbox

    waiting = inbox.pending(limit=100)
    return {
        "waiting_for_human": len(waiting),
        "kinds": sorted({str(item.get("kind")) for item in waiting}),
        "parallel": int(get_float("MULTITASK_PARALLEL", DEFAULT_PARALLEL)),
    }


def command(limit: int = 4, channels: Optional[List[str]] = None,
            parallel: Optional[int] = None, dry_run: bool = False) -> Dict[str, Any]:
    """Один проход многозадачного режима: план → работа → журнал."""
    planned = plan(limit=limit, channels=channels)
    if dry_run:
        return {"planned": planned, "results": None}
    outcome = run(planned["items"], parallel=parallel)
    outcome["planned"] = planned
    return outcome
