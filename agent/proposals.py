"""Claim texts for bounty tasks, shared by the CLI and the autopilot.

The farm never posts these itself. It prepares the exact text, checks that the
payout channel can actually reach the operator's country, and hands the result
to a human — that is the difference between automation and inventing work.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from agent import eligibility, playbooks
from agent.config import get_env


def _payload(opportunity: Dict[str, Any]) -> Dict[str, Any]:
    import json

    raw = opportunity.get("payload")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {}


def application_text(
    opportunity: Dict[str, Any],
    country: Optional[str] = None,
    rail: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the /attempt comment plus the payout verdict for this task.

    ``ok=False`` means the task must not be started: either the payment rail
    does not serve the operator's country, or triage says the bounty is taken.
    """
    payload = _payload(opportunity)
    country = (country or get_env("ELIGIBILITY_COUNTRY", "") or "").strip().upper()
    if not country:
        return {
            "ok": False,
            "reason": "не указана страна выплаты (ELIGIBILITY_COUNTRY)",
            "fix": "python -m agent.main doctor",
        }

    chosen_rail = rail or eligibility.recommend_rail(country)
    verdict = eligibility.preflight(country, chosen_rail)
    result: Dict[str, Any] = {
        "ok": verdict.usable,
        "country": verdict.country_name,
        "rail": eligibility.RAILS[chosen_rail]["title"],
        "status": verdict.status,
        "reason": verdict.reason,
        "source": verdict.source,
        "alternatives": list(verdict.alternatives),
    }
    if not verdict.usable:
        return result

    triage = payload.get("triage_verdict")
    if triage in ("paid", "closed", "assigned"):
        result.update({"ok": False, "reason": f"триаж: {triage} — задача уже занята или закрыта"})
        return result

    number = str(opportunity["id"]).rsplit("#", 1)[-1] if "#" in str(opportunity["id"]) else "N"
    playbook = playbooks.get(payload.get("playbook") or "generic")
    lines = [f"/attempt #{number}", "", f"**План:** {playbook.title}"]
    for index, step in enumerate(playbook.steps[:4], 1):
        lines.append(f"{index}. {step}")
    lines += [
        "",
        "**Оценка сроков:** [часы, которые вы готовы назвать]",
        "**Готовность:** [что уже проверено локально]",
    ]

    result["text"] = "\n".join(lines)
    result["playbook"] = playbook.key if hasattr(playbook, "key") else payload.get("playbook", "generic")
    result["triage"] = triage or "нет триажа"
    result["contested"] = triage == "contested"
    result["checklist"] = [
        "Проверить, что задача всё ещё открыта и сумма подтверждена.",
        "Опубликовать текст заявки от своего имени.",
        f"Отметить статус: python -m agent.main status-set {opportunity['id']} working",
        f"План работ: python -m agent.main plan {opportunity['id']}",
    ]
    return result
