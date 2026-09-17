"""Autopilot: the part that keeps working when nobody is watching.

The farm is designed around a hard rule — the robot does everything except the
three actions that must stay human: claiming a task, submitting work, and
confirming that money arrived. Autopilot closes the loop around that rule:

* runs a full harvest pass on its own cadence, faster when fresh tasks appear
  and slower when the market is quiet (a fixed 5-minute timer wastes quota at
  03:00 and is too slow at 15:00);
* triages candidates, plans the best ones, drafts claim texts and quest answers;
* puts each finished artifact in the outbox with one explicit human action;
* writes a shift report so a person can read in 30 seconds what the robot did;
* keeps a heartbeat and a kill switch, so it can run for weeks unattended.

It never posts, never submits, and never marks income as received.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agent import inbox, proposals, quests
from agent.config import get_env, get_float, project_root
from agent.farm import next_actions, run_cycle
from agent.ledger import get_opportunity, get_state, set_state, summary

#: Touch this file (or set AUTOPILOT_ENABLED=false) to stop the robot now.
KILL_SWITCH_NAME = "STOP"

DEFAULT_MIN_INTERVAL = 300.0
DEFAULT_MAX_INTERVAL = 3600.0


def kill_switch_path() -> Path:
    return project_root() / "data" / KILL_SWITCH_NAME


def stop_reason() -> str:
    if kill_switch_path().exists():
        return f"найден файл остановки {kill_switch_path().name}"
    if (get_env("AUTOPILOT_ENABLED", "true") or "").lower() in ("0", "false", "no", "off"):
        return "AUTOPILOT_ENABLED=false"
    return ""


def enabled() -> bool:
    return not stop_reason()


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def intervals() -> tuple[float, float]:
    low = get_float("AUTOPILOT_MIN_INTERVAL", DEFAULT_MIN_INTERVAL)
    high = get_float("AUTOPILOT_MAX_INTERVAL", DEFAULT_MAX_INTERVAL)
    if high < low:
        high = low
    return max(60.0, low), max(60.0, high)


@dataclass
class TickResult:
    started_at: str
    finished_at: str = ""
    duration_s: float = 0.0
    found: int = 0
    kept: int = 0
    triaged: int = 0
    prepared: List[Dict[str, Any]] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    next_interval: float = DEFAULT_MIN_INTERVAL
    pipeline_ev_usd: float = 0.0
    stopped: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": round(self.duration_s, 1),
            "found": self.found,
            "kept": self.kept,
            "triaged": self.triaged,
            "prepared": self.prepared,
            "skipped": self.skipped,
            "notes": self.notes,
            "next_interval": round(self.next_interval),
            "pipeline_ev_usd": round(self.pipeline_ev_usd, 2),
            "stopped": self.stopped,
        }


def _safe_name(value: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")


def _write_artifact(name: str, text: str) -> str:
    path = project_root() / "reports" / "inbox"
    path.mkdir(parents=True, exist_ok=True)
    file_path = path / f"{_safe_name(name)}.md"
    file_path.write_text(text, encoding="utf-8")
    return str(file_path.relative_to(project_root()))


def prepare(limit: Optional[int] = None) -> tuple[List[Dict[str, Any]], List[str]]:
    """Turn the best open tasks into artifacts a human can finish in one click.

    Nothing is created for tasks that are already taken or whose payout cannot
    reach the operator — that would be manufactured busywork.
    """
    cap = limit if limit is not None else int(get_float("AUTOPILOT_PREPARE", 3))
    prepared: List[Dict[str, Any]] = []
    skipped: List[str] = []

    for action in next_actions(limit=max(cap * 3, 6)):
        if len(prepared) >= cap:
            break
        opportunity = get_opportunity(str(action["id"]))
        if not opportunity:
            continue
        try:
            contribution = _prepare_one(opportunity)
        except Exception as exc:  # one bad item must not stop the robot
            skipped.append(f"{action['id']}: {exc.__class__.__name__}: {exc}")
            continue
        if contribution is None:
            skipped.append(f"{action['id']}: артефакт уже в очереди")
        elif contribution.get("skipped"):
            skipped.append(f"{action['id']}: {contribution['reason']}")
        else:
            prepared.append(contribution)
    return prepared, skipped


def _prepare_one(opportunity: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    channel = str(opportunity.get("channel") or "")
    payload = opportunity.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload or "{}")
        except Exception:
            payload = {}
    payload = payload or {}

    kind_hint = {"agent_marketplaces": "quest", "github_bounties": "application",
                 "audit_contests": "brief"}.get(channel)
    if kind_hint and inbox.has_ready(str(opportunity["id"]), kind_hint):
        return None

    if channel == "agent_marketplaces":
        path = quests.save_submission(opportunity)
        result = quests.submission_markdown(opportunity)
        item_id = inbox.add(
            opportunity_id=str(opportunity["id"]),
            channel=channel,
            kind="quest",
            title=str(opportunity["title"]),
            path=str(path.relative_to(project_root())),
            summary=(
                f"${float(opportunity.get('reward_usd') or 0):,.0f} · "
                f"{result['words']} слов · {result['source']}"
            ),
        )
        return {
            "inbox_id": item_id,
            "kind": "quest",
            "opportunity_id": str(opportunity["id"]),
            "title": str(opportunity["title"]),
            "path": str(path.relative_to(project_root())),
        }

    if channel == "github_bounties":
        result = proposals.application_text(opportunity)
        if not result.get("ok"):
            return {"skipped": True, "reason": str(result.get("reason"))}
        text = result["text"]
        if result.get("contested"):
            text = (
                "> Конкуренция высокая: заявок уже много. Публикуйте только если успеваете "
                "сделать работу быстро.\n\n" + text
            )
        path = _write_artifact(f"application-{opportunity['id']}", text)
        item_id = inbox.add(
            opportunity_id=str(opportunity["id"]),
            channel=channel,
            kind="application",
            title=str(opportunity["title"]),
            path=path,
            summary=(
                f"${float(opportunity.get('reward_usd') or 0):,.0f} · "
                f"{result.get('rail')} · триаж {result.get('triage')}"
            ),
        )
        return {
            "inbox_id": item_id,
            "kind": "application",
            "opportunity_id": str(opportunity["id"]),
            "title": str(opportunity["title"]),
            "path": path,
        }

    if channel == "audit_contests":
        from agent.subagents import build_brief

        brief = build_brief(opportunity)
        path = _write_artifact(f"brief-{opportunity['id']}", brief.to_markdown())
        item_id = inbox.add(
            opportunity_id=str(opportunity["id"]),
            channel=channel,
            kind="brief",
            title=str(opportunity["title"]),
            path=path,
            summary=f"${float(opportunity.get('reward_usd') or 0):,.0f} · плейбук {brief.playbook}",
        )
        return {
            "inbox_id": item_id,
            "kind": "brief",
            "opportunity_id": str(opportunity["id"]),
            "title": str(opportunity["title"]),
            "path": path,
        }

    return {"skipped": True, "reason": f"канал {channel} не готовит артефакты"}


def notify(lines: List[str]) -> None:
    from agent.telegram import TelegramNotifier

    notifier = TelegramNotifier()
    if notifier.enabled() and lines:
        notifier.send("\n".join(lines))


def tick(prepare_limit: Optional[int] = None) -> TickResult:
    """One autonomous pass: harvest → rank → prepare → report."""
    started = time.time()
    result = TickResult(started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    if not enabled():
        result.stopped = True
        result.notes.append(f"остановлено: {stop_reason()}")
        result.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        set_state("autopilot.last_result", result.as_dict())
        return result

    cycle = run_cycle()
    result.found = cycle.found
    result.kept = cycle.kept
    result.triaged = cycle.triaged
    result.pipeline_ev_usd = cycle.pipeline_ev_usd
    result.notes.extend(cycle.notes[:5])
    result.notes.extend(f"{b['channel']}: отказано — {b['reason'][:80]}" for b in cycle.blocked[:3])

    prepared, skipped = prepare(prepare_limit)
    result.prepared = prepared
    result.skipped = skipped[:8]

    low, high = intervals()
    previous = float(get_state("autopilot.interval", low) or low)
    if prepared:
        result.next_interval = low
    elif result.found == 0:
        result.next_interval = _clamp(previous * 1.5, low, high)
    else:
        result.next_interval = _clamp(previous, low, high)

    ticks = int(get_state("autopilot.ticks", 0) or 0) + 1
    prepared_total = int(get_state("autopilot.prepared_total", 0) or 0) + len(prepared)
    result.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result.duration_s = time.time() - started

    set_state("autopilot.ticks", ticks)
    set_state("autopilot.prepared_total", prepared_total)
    set_state("autopilot.interval", result.next_interval)
    set_state("autopilot.last_tick", result.finished_at)
    set_state("autopilot.last_result", result.as_dict())
    set_state("autopilot.inbox_ready", inbox.counts()["ready"])

    if prepared or result.kept:
        lines = [f"AGENT-0: проход завершён (найдено {result.found}, новый EV ${result.pipeline_ev_usd:,.0f})"]
        for item in prepared:
            lines.append(f"готово к публикации: {item['title'][:70]} → python -m agent.main inbox")
        notify(lines)
    return result


def run(
    ticks: Optional[int] = None,
    on_tick: Optional[Callable[[TickResult], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run the autopilot until stopped, the tick budget is spent, or Ctrl+C."""
    count = 0
    while True:
        if not enabled():
            if on_tick:
                on_tick(tick())
            return 0
        result = tick()
        count += 1
        if on_tick:
            on_tick(result)
        if ticks is not None and count >= ticks:
            return 0
        try:
            sleep(result.next_interval)
        except KeyboardInterrupt:
            return 0


def status() -> Dict[str, Any]:
    """Heartbeat for the dashboard and the health endpoint."""
    last = get_state("autopilot.last_tick")
    age: Optional[float] = None
    if last:
        try:
            moment = datetime.fromisoformat(str(last))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - moment).total_seconds()
        except Exception:
            age = None
    low, high = intervals()
    interval = float(get_state("autopilot.interval", low) or low)
    reason = stop_reason()
    if reason:
        state = "stopped"
    elif age is None:
        state = "idle"
    elif age <= max(interval * 2.5, 900):
        state = "running"
    else:
        state = "stale"
    return {
        "state": state,
        "stop_reason": reason,
        "enabled": enabled(),
        "last_tick": last,
        "last_tick_age_s": round(age) if age is not None else None,
        "interval": round(interval),
        "min_interval": round(low),
        "max_interval": round(high),
        "ticks": int(get_state("autopilot.ticks", 0) or 0),
        "prepared_total": int(get_state("autopilot.prepared_total", 0) or 0),
        "inbox_ready": inbox.counts()["ready"],
        "last_result": get_state("autopilot.last_result"),
    }


def health() -> Dict[str, Any]:
    """Machine-readable health for uptime checks: ok / degraded / stopped."""
    data = status()
    problems: List[str] = []
    if data["state"] in ("idle", "stale"):
        problems.append("автопилот не отмечался давно" if data["state"] == "stale"
                        else "автопилот ещё не запускался")
    if data["stop_reason"]:
        problems.append(data["stop_reason"])
    db_ok = True
    try:
        summary()
    except Exception as exc:
        db_ok = False
        problems.append(f"база недоступна: {exc.__class__.__name__}")
    status_value = "stopped" if data["stop_reason"] else ("ok" if not problems else "degraded")
    return {
        "status": status_value,
        "problems": problems,
        "db": db_ok,
        "autopilot": data,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def shift_report(hours: float = 24.0) -> str:
    """What the robot did while the operator was away."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    data = summary()
    counts = inbox.counts()
    auto = status()

    lines = [
        f"# Смена за последние {hours:g} ч (с {since:%Y-%m-%d %H:%M} UTC)",
        "",
        f"- Проходов автопилота: {auto['ticks']} · интервал {auto['interval']}с · "
        f"состояние: {auto['state']}",
        f"- Подтверждённый доход: ${data['verified_usd']:,.2f} · "
        f"заявлено: ${data['claimed_usd']:,.2f}",
        f"- В очереди: {data['opportunities_queued']} · "
        f"готово к публикации: {counts['ready']} · отправлено: {counts['published']}",
    ]

    last = auto.get("last_result") or {}
    if last:
        lines += [
            f"- Последний проход: найдено {last.get('found', 0)}, "
            f"подготовлено {len(last.get('prepared', []))}, "
            f"EV конвейера ${float(last.get('pipeline_ev_usd') or 0):,.0f} "
            f"({last.get('started_at', '')})",
        ]
        for note in (last.get("notes") or [])[:3]:
            lines.append(f"  - примечание: {note}")

    pending = inbox.pending(limit=5)
    lines += ["", "## Ждут одного действия", ""]
    if not pending:
        lines.append("- очередь пуста: запустите `python -m agent.main autopilot --once`")
    for item in pending:
        summary_text = item.get("summary") or ""
        lines.append(
            f"- **{item['kind_title']}** {summary_text} — {item['title'][:70]}\n"
            f"  действие: {item['action']}\n"
            f"  текст: `python -m agent.main inbox show {item['id']}`"
        )

    from agent.farm import low_value, next_actions

    top = next_actions(limit=3)
    lines += ["", "## Что брать в работу", ""]
    for action in top:
        lines.append(
            f"- {action['title'][:70]} — ${float(action['reward_usd'] or 0):,.0f}, "
            f"EV/час ${float(action['ev_per_hour'] or 0):,.1f}, канал {action['channel']}"
        )
    rejected = low_value(limit=3)
    if rejected:
        lines += ["", "## Отсеяно как нерентабельное", ""]
        for item in rejected:
            lines.append(f"- {item['title'][:60]} — ${item['ev_per_hour']:,.2f}/час")

    lines += [
        "",
        "## Остановить робота",
        "",
        f"`touch data/{KILL_SWITCH_NAME}` — мгновенная остановка, "
        "или `AUTOPILOT_ENABLED=false` в .env.",
    ]
    return "\n".join(lines)
