"""Наблюдение за роботом: что сломалось, пока вас не было.

Автономный режим полезен ровно до тех пор, пока кто-то замечает, что он встал.
Модуль проверяет четыре вещи, каждая из которых в реальности означает потерю денег:

* робот перестал отмечаться (процесс упал, машина уснула, кончилось место);
* канал несколько проходов подряд возвращает ошибку или ноль находок;
* лимит GitHub исчерпан, и следующий проход будет пустым;
* готовые тексты лежат в очереди слишком долго — это узкое место уже не у робота,
  а у человека, и об этом нужно напомнить.

Ничего не «чинится» автоматически: наблюдение сообщает, человек решает.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from agent import inbox, learning
from agent.config import get_env, get_float, load_environment, project_root
from agent.ledger import all_state, connect

#: Сколько часов готовый текст может ждать, прежде чем робот начнёт напоминать.
DEFAULT_NAG_HOURS = 12.0

#: Сколько проходов подряд без находок считаем затишьем канала.
DEFAULT_QUIET_RUNS = 6


@dataclass
class Problem:
    key: str
    title: str
    detail: str
    advice: str
    severity: str = "warning"  # warning | critical

    def as_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "detail": self.detail,
            "advice": self.advice,
            "severity": self.severity,
        }


def _hours_since(stamp: Optional[str]) -> Optional[float]:
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - moment).total_seconds() / 3600.0


def check_heartbeat() -> Optional[Problem]:
    last = (all_state().get("autopilot.last_tick") or {}).get("value")
    age = _hours_since(last)
    if age is None:
        return None  # автопилот не запускали — это не поломка, а решение
    limit = get_float("AUTOPILOT_MAX_INTERVAL", 3600.0) / 3600.0 * 2.5
    if age <= max(limit, 2.0):
        return None
    return Problem(
        "heartbeat",
        "Робот молчит",
        f"последний проход был {age:.1f} ч назад",
        "проверьте процесс: systemctl status agent0-autopilot, "
        "журнал: journalctl -u agent0-autopilot -n 50",
        severity="critical",
    )


def expected_quiet(channel: str) -> str:
    """Канал молчит по делу, а не из-за поломки: ключ не задан, файла нет.

    Такие каналы не считаются проблемой — иначе дозор каждый день «находил» бы
    одну и ту же незаполненную настройку, и к его сообщениям перестали бы
    относиться серьёзно.
    """
    if channel == "bug_recon" and not (project_root() / "data" / "scope.yaml").exists():
        return "нет data/scope.yaml — разведка по программам не запрашивалась"
    if channel == "agent_marketplaces" and not (get_env("AGENTHANSA_API_KEY", "") or "").strip():
        return "нет ключа площадки — третье направление ждёт AGENTHANSA_API_KEY"
    return ""


def check_channels(quiet_runs: Optional[int] = None) -> List[Problem]:
    threshold = quiet_runs or int(get_float("WATCHDOG_QUIET_RUNS", DEFAULT_QUIET_RUNS))
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT channel, found, kept, note, started_at FROM runs "
            "ORDER BY id DESC LIMIT 200"
        ).fetchall()
    finally:
        conn.close()

    by_channel: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        by_channel.setdefault(row["channel"], []).append(dict(row))

    problems: List[Problem] = []
    for channel, runs in by_channel.items():
        recent = runs[:threshold]
        if len(recent) < threshold:
            continue
        # Ожидаемое молчание (нет ключа, нет файла авторизации) отменяет только
        # жалобу на отсутствие находок. Ошибки — не «тишина»: канал мог неделю
        # отдавать 403, и об этом человек обязан узнать.
        if not expected_quiet(channel) and all(int(run["found"] or 0) == 0 for run in recent):
            note = (recent[0].get("note") or "").strip()
            problems.append(
                Problem(
                    "channel_quiet",
                    f"Канал {channel} не находит ничего {len(recent)} проходов",
                    note[:160] or "все проходы вернули ноль находок",
                    "проверьте условия поиска: возможно, нужны новые запросы "
                    "или ключ площадки (python -m agent.main проверка)",
                )
            )
        errors = [
            run for run in recent
            if "ошибка:" in (run.get("note") or "").lower()
            or "network" in (run.get("note") or "").lower()
        ]
        if len(errors) >= max(2, threshold // 2):
            problems.append(
                Problem(
                    "channel_errors",
                    f"Канал {channel} часто падает",
                    f"{len(errors)} из {len(recent)} проходов с ошибкой: "
                    f"{(errors[0].get('note') or '').split('ошибка:', 1)[-1][:140].strip()}",
                    "проверьте сеть и доступ к площадке; канал временно можно "
                    "отключить: python -m agent.main цикл --channel <канал>",
                )
            )
    return problems


def check_quota() -> Optional[Problem]:
    state = (all_state().get("github.rate_limit") or {}).get("value") or {}
    if not state:
        return None
    remaining = int(state.get("remaining") or 0)
    reset = state.get("reset")
    if remaining > 0:
        return None
    when = ""
    if reset:
        try:
            moment = datetime.fromtimestamp(int(reset), tz=timezone.utc)
            when = f", лимит восстановится в {moment:%H:%M} UTC"
        except (TypeError, ValueError, OSError):
            when = ""
    return Problem(
        "github_quota",
        "Лимит поиска GitHub исчерпан",
        f"остаток 0{when}",
        "добавьте GITHUB_TOKEN в .env (настройка --token) — лимит вырастет "
        "с 60 до 5000 запросов в час",
        severity="critical" if not state.get("token") else "warning",
    )


def check_inbox_age(nag_hours: Optional[float] = None) -> Optional[Problem]:
    limit = nag_hours or get_float("WATCHDOG_NAG_HOURS", DEFAULT_NAG_HOURS)
    pending = inbox.pending(limit=50)
    if not pending:
        return None
    ages = [(_hours_since(item.get("created_at")), item) for item in pending]
    aged = [(age, item) for age, item in ages if age is not None and age >= limit]
    if not aged:
        return None
    oldest_age, oldest = max(aged, key=lambda pair: pair[0])
    return Problem(
        "inbox_aging",
        f"Готовые тексты ждут отправки: {len(aged)} шт.",
        f"самый старый — #{oldest['id']} «{oldest['title'][:60]}» "
        f"({oldest_age:.0f} ч назад подготовлен)",
        "робот сделал свою часть; отправка занимает минуту: "
        f"python -m agent.main входящие show {oldest['id']}",
    )


def check_missing_setup() -> List[Problem]:
    problems: List[Problem] = []
    if not (get_env("ELIGIBILITY_COUNTRY", "") or "").strip():
        problems.append(
            Problem(
                "no_country",
                "Не указана страна получения денег",
                "без неё ферма не готовит заявки: неизвестно, дойдёт ли платёж",
                "python -m agent.main настройка --country DE",
                severity="critical",
            )
        )
    return problems


def inspect(nag_hours: Optional[float] = None) -> List[Problem]:
    """Полный осмотр. Ни один сбой проверки не должен ломать осмотр."""
    load_environment()
    problems: List[Problem] = []
    for checker in (check_heartbeat, check_quota, check_missing_setup):
        try:
            result = checker()
        except Exception:
            continue
        if result is None:
            continue
        if isinstance(result, list):
            problems.extend(result)
        else:
            problems.append(result)
    try:
        problems.extend(check_channels())
    except Exception:
        pass
    try:
        aging = check_inbox_age(nag_hours)
        if aging:
            problems.append(aging)
    except Exception:
        pass
    return problems


def nudge_lines(nag_hours: Optional[float] = None) -> List[str]:
    """Короткие строки для Telegram: только то, что требует действия."""
    lines: List[str] = []
    for problem in inspect(nag_hours):
        mark = "!!" if problem.severity == "critical" else "!"
        lines.append(f"{mark} {problem.title}: {problem.advice}")
    advice = learning.advise()
    if advice:
        lines.append(f"обучение: {advice}")
    return lines


def notes() -> List[str]:
    """Каналы, которые молчат по уважительной причине — не поломка, но и не работа."""
    result: List[str] = []
    for channel in ("bug_recon", "agent_marketplaces"):
        reason = expected_quiet(channel)
        if reason:
            result.append(f"{channel}: {reason}")
    return result


def state() -> Dict[str, Any]:
    problems = inspect()
    return {
        "status": "critical" if any(p.severity == "critical" for p in problems)
        else ("warning" if problems else "ok"),
        "problems": [problem.as_dict() for problem in problems],
        "notes": notes(),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
