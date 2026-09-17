"""Farm orchestrator.

Responsibilities:

1. decide which workers may run (policy gate) and which are refused;
2. run the allowed workers, collecting opportunities;
3. **triage** the best candidates: is the bounty still claimed by nobody, and
   will the payout actually reach the operator;
4. re-score with the measured probability, persist everything in the ledger;
5. never let a worker touch the network if its capability is not allowed.

Triage is what turns a list of "open bounty" links into a short list of work
worth doing. It costs two GitHub calls per candidate, so it is applied only to
the top ``TRIAGE_BUDGET`` items of a cycle, not to everything found.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent.channels import ACTIVE_CHANNELS, DISABLED_CHANNELS, build_channel
from agent.config import get_env, get_float
from agent.ledger import (
    Opportunity,
    record_action,
    record_run,
    upsert_opportunities,
)
from agent.policy import REQUESTED_TACTICS, evaluate, report as policy_report
from agent.scoring import score_opportunity
from agent.triage import TriageClient

#: Channels whose items support the /attempt + linked-PR triage.
TRIAGEABLE = {"github_bounties"}

#: SQLite writes are serialised across worker threads; harvesting stays parallel.
_DB_LOCK = threading.Lock()

#: Русские названия воркеров — чтобы не запоминать внутренние имена.
CHANNEL_ALIASES: Dict[str, str] = {
    "гитхаб": "github_bounties",
    "задачи": "github_bounties",
    "контесты": "audit_contests",
    "аудит": "audit_contests",
    "площадки": "agent_marketplaces",
    "квесты": "agent_marketplaces",
    "разведка": "bug_recon",
}


def resolve_channel(name: Optional[str]) -> Optional[str]:
    """Accept both the internal worker name and its Russian equivalent."""
    if not name:
        return name
    return CHANNEL_ALIASES.get(name.strip().lower(), name)


@dataclass
class CycleResult:
    channels: List[Dict[str, Any]] = field(default_factory=list)
    found: int = 0
    new: int = 0
    kept: int = 0
    dropped: int = 0
    triaged: int = 0
    pipeline_ev_usd: float = 0.0
    blocked: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    #: Сколько задач в топе оказались убыточными (в выручку не попали).
    low_value_top: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "channels": self.channels,
            "found": self.found,
            "new": self.new,
            "kept": self.kept,
            "dropped": self.dropped,
            "triaged": self.triaged,
            "pipeline_ev_usd": round(self.pipeline_ev_usd, 2),
            "low_value_top": self.low_value_top,
            "blocked": self.blocked,
            "notes": self.notes,
        }


def _triage_items(items: List[Opportunity], budget: int) -> tuple[List[Opportunity], int, int]:
    """Attach measured competition data to the most promising candidates.

    Returns (items, triaged_count, dropped_count). Items whose bounty is already
    paid or closed are marked ``dropped`` instead of ``queued``.
    """
    if budget <= 0 or not items:
        return items, 0, 0

    client = TriageClient(token=get_env("GITHUB_TOKEN", "") or "")
    hourly_rate = get_float("OPERATOR_HOURLY_RATE", 15.0)
    triaged = 0
    dropped = 0

    ordered = sorted(items, key=lambda o: o.score, reverse=True)
    for item in ordered[:budget]:
        number = 0
        if "#" in item.id:
            try:
                number = int(item.id.rsplit("#", 1)[1])
            except ValueError:
                number = 0
        if not number:
            continue
        triaged += 1
        verdict = client.triage(
            opportunity_id=item.id,
            repo=item.repo,
            number=number,
            created_at=(item.payload or {}).get("created_at"),
            updated_at=(item.payload or {}).get("updated_at"),
            issue_state="open",
        )

        labels = list((item.payload or {}).get("labels") or [])
        body = str((item.payload or {}).get("body") or "")
        rescored = score_opportunity(
            labels=labels,
            title=item.title,
            body=body,
            comments=int((item.payload or {}).get("comments") or 0),
            repo_stars=int((item.payload or {}).get("stars") or 0),
            hourly_rate=hourly_rate,
            triage_probability=verdict.probability,
            verified_amount=verdict.verified_amount,
            age_days=(item.payload or {}).get("age_days"),
        )

        item.score = rescored.total
        item.reward_usd = rescored.reward_usd
        item.reward_source = rescored.reward_source
        item.rationale = rescored.rationale + " | триаж: " + "; ".join(verdict.notes or ["чисто"])
        item.payload = {
            **(item.payload or {}),
            "triage": verdict.as_dict(),
            "triage_verdict": verdict.verdict,
            "triage_notes": verdict.notes,
            "attempts": verdict.attempts,
            "open_prs": verdict.open_prs,
            "funder": verdict.funder,
            "verified_amount": verdict.verified_amount,
            "effort_hours": rescored.effort_hours,
            "probability": verdict.probability,
            "ev_per_hour": rescored.ev_per_hour,
            "expected_value_usd": rescored.expected_value_usd,
            "playbook": rescored.playbook,
        }

        if verdict.verdict in ("paid", "closed"):
            item.status = "dropped"
            dropped += 1
            record_action(
                item.channel,
                "read_public_data",
                item.id,
                False,
                f"триаж: {verdict.verdict} — {'; '.join(verdict.notes)}",
            )

    return items, triaged, dropped


def run_channel(
    name: str,
    limit: int = 25,
    triage_budget: Optional[int] = None,
    do_triage: bool = True,
) -> Dict[str, Any]:
    """Run a single worker. Refuses politely if policy says no."""
    channel = build_channel(name)

    if not channel.allowed:
        reason = f"{channel.decision.label}: {channel.decision.reason}"
        record_action(name, channel.capability, "-", False, reason)
        record_run(name, channel.capability, found=0, kept=0, policy="blocked", note=reason)
        return {
            "name": name,
            "title": channel.title,
            "capability": channel.capability,
            "allowed": False,
            "policy_label": channel.decision.label,
            "policy_reason": channel.decision.reason,
            "requirements": channel.decision.requirements,
            "found": 0,
            "kept": 0,
            "dropped": 0,
            "top": [],
        }

    if channel.decision.requirements:
        ready = getattr(channel, "ready", True)
        if not ready:
            reason = (
                "Канал разрешён политикой, но не готов: "
                "нужен файл авторизации data/scope.yaml с программой и хостами."
            )
            record_run(name, channel.capability, 0, 0, policy="not_ready", note=reason)
            return {
                "name": name,
                "title": channel.title,
                "capability": channel.capability,
                "allowed": True,
                "ready": False,
                "policy_label": channel.decision.label,
                "policy_reason": reason,
                "requirements": channel.decision.requirements,
                "found": 0,
                "kept": 0,
                "dropped": 0,
                "top": [],
            }

    started = time.time()
    try:
        items: List[Opportunity] = channel.harvest(limit=limit)
    except Exception as exc:  # a worker crash must not kill the farm
        note = f"worker error: {exc.__class__.__name__}: {exc}"
        record_run(name, channel.capability, 0, 0, policy="error", note=note)
        return {
            "name": name,
            "title": channel.title,
            "capability": channel.capability,
            "allowed": True,
            "found": 0,
            "kept": 0,
            "dropped": 0,
            "error": note,
            "top": [],
        }

    budgets = {
        "github_bounties": triage_budget
        if triage_budget is not None
        else int(get_env("TRIAGE_BUDGET", "6") or 6),
        "audit_contests": 0,
        "agent_marketplaces": 0,
        "bug_recon": 0,
    }
    triaged = dropped = 0
    if do_triage and name in TRIAGEABLE:
        items, triaged, dropped = _triage_items(items, budgets.get(name, 0))

    elapsed = round(time.time() - started, 1)
    with _DB_LOCK:
        new = upsert_opportunities(items, fresh_only=True)
        kept = upsert_opportunities(items)
        record_run(
            name,
            channel.capability,
            found=len(items),
            kept=kept,
            policy="allowed",
            note=f"{elapsed}s, новых {new}, триаж {triaged}, отсеяно {dropped}",
        )

    top = [
        {
            "id": item.id,
            "title": item.title,
            "url": item.url,
            "reward_usd": item.reward_usd,
            "score": item.score,
            "rationale": item.rationale,
            "status": item.status,
            "playbook": (item.payload or {}).get("playbook", ""),
            "ev_per_hour": (item.payload or {}).get("ev_per_hour", 0.0),
            "expected_value_usd": (item.payload or {}).get("expected_value_usd", 0.0),
            "triage_verdict": (item.payload or {}).get("triage_verdict", ""),
        }
        for item in sorted(items, key=lambda o: o.score, reverse=True)[:10]
        if item.status == "queued"
    ]

    return {
        "name": name,
        "title": channel.title,
        "capability": channel.capability,
        "allowed": True,
        "ready": True,
        "found": len(items),
        "kept": kept,
        "dropped": dropped,
        "triaged": triaged,
        "elapsed_seconds": elapsed,
        "error": getattr(channel, "last_error", ""),
        "notes": list(getattr(channel, "notes", []) or []),
        "top": top,
    }


def run_cycle(
    channels: Optional[List[str]] = None,
    limit: int = 25,
    do_triage: bool = True,
    parallel: bool = True,
) -> CycleResult:
    """One pass over the farm.

    The three earning directions are harvested in parallel: each worker spends
    most of its time waiting on HTTP, so running them together cuts a cycle down
    to the duration of the slowest one. Persistence is serialised.
    """
    names = channels or list(ACTIVE_CHANNELS.keys())
    names = [resolve_channel(name) or name for name in names]
    result = CycleResult()

    valid = [name for name in names if name in ACTIVE_CHANNELS]
    for name in names:
        if name not in ACTIVE_CHANNELS:
            result.notes.append(f"Неизвестный воркер: {name}")

    outcomes: List[Dict[str, Any]] = []
    if parallel and len(valid) > 1:
        with ThreadPoolExecutor(max_workers=min(4, len(valid))) as pool:
            futures = [pool.submit(run_channel, name, limit, None, do_triage) for name in valid]
            for future in futures:
                try:
                    outcomes.append(future.result())
                except Exception as exc:  # a worker must never kill the cycle
                    result.notes.append(f"воркер упал: {exc.__class__.__name__}: {exc}")
    else:
        for name in valid:
            outcomes.append(run_channel(name, limit=limit, do_triage=do_triage))

    for outcome in outcomes:
        result.channels.append(outcome)
        result.found += int(outcome.get("found", 0))
        result.new += int(outcome.get("new", 0))
        result.kept += int(outcome.get("kept", 0))
        result.dropped += int(outcome.get("dropped", 0))
        result.triaged += int(outcome.get("triaged", 0))
        for item in outcome.get("top", []):
            # Отрицательная оценка означает «грабить себя дороже, чем заработать»:
            # в ожидаемую выручку такие задачи не идут, но остаются в списке отсева.
            result.pipeline_ev_usd += max(0.0, float(item.get("expected_value_usd") or 0.0))
            if float(item.get("ev_per_hour") or 0.0) < 0:
                result.low_value_top += 1
        if not outcome.get("allowed"):
            result.blocked.append(
                {
                    "channel": outcome.get("name"),
                    "capability": outcome.get("capability"),
                    "reason": outcome.get("policy_reason"),
                }
            )
        if outcome.get("error"):
            result.notes.append(f"{outcome.get('name')}: {outcome['error']}")
        for note in outcome.get("notes", []):
            result.notes.append(f"{outcome.get('name')}: {note}")

    return result


def next_actions(limit: int = 5, min_ev_per_hour: Optional[float] = None) -> List[Dict[str, Any]]:
    """What the operator should actually do next, in order.

    This is the farm's answer to "как сделать доход больше": not a bigger
    harvest, but a short, ordered list of the highest expected value per hour,
    with the exact next command for each item.

    Items whose measured expected value per hour is below the floor are *not*
    recommended. Showing them as "work worth doing" would be the same lie the
    whole project is built to avoid — the honest place for them is the
    "низкая ценность" list in ``low_value``.
    """
    from agent.ledger import age_hours, connect

    floor = min_ev_per_hour if min_ev_per_hour is not None else get_float("MIN_EV_PER_HOUR", 3.0)

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, title, channel, url, reward_usd, score, rationale, payload, "
            "status, first_seen "
            "FROM opportunities WHERE status='queued' ORDER BY score DESC LIMIT 60",
        ).fetchall()
    finally:
        conn.close()

    actions: List[Dict[str, Any]] = []
    for row in rows:
        if len(actions) >= limit:
            break
        payload = row["payload"] or "{}"
        try:
            import json

            payload = json.loads(payload)
        except Exception:
            payload = {}
        playbook = payload.get("playbook") or "generic"
        verdict = payload.get("triage_verdict") or "нет триажа"
        ev_hour = float(payload.get("ev_per_hour") or 0.0)
        if ev_hour < floor:
            continue
        steps = [
            f"python -m agent.main plan {row['id']}",
            "сделать работу по плану",
            f"python -m agent.main hours-add --channel {row['channel']} --hours N --id {row['id']}",
            f"python -m agent.main payout-add --channel {row['channel']} --amount X --evidence 'PR #N merged'",
        ]
        first_seen = None
        try:
            first_seen = row["first_seen"]
        except (IndexError, KeyError):
            first_seen = None
        age = age_hours(first_seen or payload.get("first_seen"))
        actions.append(
            {
                "id": row["id"],
                "age_hours": age,
                "is_new": age is not None and age <= 24.0,
                "title": row["title"],
                "channel": row["channel"],
                "url": row["url"],
                "reward_usd": row["reward_usd"],
                "score": row["score"],
                "playbook": playbook,
                "triage": verdict,
                "ev_per_hour": ev_hour,
                "expected_value_usd": payload.get("expected_value_usd", 0.0),
                "effort_hours": payload.get("effort_hours", 0.0),
                "why": row["rationale"],
                "next": steps,
            }
        )
    return actions


def low_value(limit: int = 5, min_ev_per_hour: Optional[float] = None) -> List[Dict[str, Any]]:
    """Items the farm deliberately does NOT recommend, and why."""
    from agent.ledger import connect

    floor = min_ev_per_hour if min_ev_per_hour is not None else get_float("MIN_EV_PER_HOUR", 3.0)
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, title, channel, reward_usd, score, rationale, payload, first_seen "
            "FROM opportunities WHERE status='queued' ORDER BY score DESC LIMIT 60",
        ).fetchall()
    finally:
        conn.close()

    items: List[Dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except Exception:
            payload = {}
        ev_hour = float(payload.get("ev_per_hour") or 0.0)
        if ev_hour >= floor:
            continue
        items.append(
            {
                "id": row["id"],
                "title": row["title"],
                "channel": row["channel"],
                "reward_usd": row["reward_usd"],
                "score": row["score"],
                "ev_per_hour": ev_hour,
                "triage": payload.get("triage_verdict") or "нет триажа",
                "attempts": payload.get("attempts"),
                "open_prs": payload.get("open_prs"),
            }
        )
        if len(items) >= limit:
            break
    return items


def disabled_report() -> List[Dict[str, Any]]:
    """Everything requested but refused, with the reason. For the dashboard."""
    disabled: List[Dict[str, Any]] = []
    for entry in DISABLED_CHANNELS:
        decision = evaluate(entry["capability"])
        disabled.append(
            {
                "name": entry["name"],
                "title": entry["title"],
                "capability": entry["capability"],
                "allowed": decision.allowed,
                "label": decision.label,
                "reason": decision.reason,
                "risk": decision.requirements[0] if decision.requirements else "",
                "instead": decision.alternatives[0] if decision.alternatives else "",
            }
        )
    return disabled


def channel_status() -> List[Dict[str, Any]]:
    """Status of every registered worker, allowed or not."""
    statuses: List[Dict[str, Any]] = []
    for name in ACTIVE_CHANNELS:
        channel = build_channel(name)
        status = channel.status()
        status["ready"] = getattr(channel, "ready", True)
        statuses.append(status)
    return statuses


def overview() -> Dict[str, Any]:
    """Full farm state, used by the dashboard and `python -m agent.main status`."""
    from agent.directions import portfolio

    return {
        "directions": portfolio(),
        "channels": channel_status(),
        "disabled": disabled_report(),
        "policy": policy_report(),
        "requested": [
            {
                "request": r.request,
                "capability": r.capability,
                "status": r.status,
                "comment": r.comment,
            }
            for r in REQUESTED_TACTICS
        ],
        "hourly_rate": get_float("OPERATOR_HOURLY_RATE", 15.0),
        "triage_budget": int(get_env("TRIAGE_BUDGET", "6") or 6),
        "min_score": get_float("MIN_SCORE", 0.0),
    }
