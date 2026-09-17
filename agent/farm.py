"""Farm orchestrator.

Responsibilities:

1. decide which workers may run (policy gate) and which are refused;
2. run the allowed workers, collecting opportunities;
3. persist everything in the ledger;
4. never let a worker touch the network if its capability is not allowed.

The orchestrator is intentionally boring: the interesting decisions live in
``policy`` (what is allowed at all) and ``scoring`` (what is worth doing).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent.channels import ACTIVE_CHANNELS, DISABLED_CHANNELS, build_channel
from agent.config import get_env, get_float
from agent.ledger import Opportunity, record_action, record_run, upsert_opportunities
from agent.policy import REQUESTED_TACTICS, evaluate, report as policy_report


@dataclass
class CycleResult:
    channels: List[Dict[str, Any]] = field(default_factory=list)
    found: int = 0
    kept: int = 0
    blocked: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "channels": self.channels,
            "found": self.found,
            "kept": self.kept,
            "blocked": self.blocked,
            "notes": self.notes,
        }


def run_channel(name: str, limit: int = 25) -> Dict[str, Any]:
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
            "top": [],
        }

    # Extra gate for channels that need an authorization file.
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
            "error": note,
            "top": [],
        }

    kept = upsert_opportunities(items)
    elapsed = round(time.time() - started, 1)
    record_run(
        name,
        channel.capability,
        found=len(items),
        kept=kept,
        policy="allowed",
        note=f"{elapsed}s",
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
        }
        for item in sorted(items, key=lambda o: o.score, reverse=True)[:10]
    ]

    return {
        "name": name,
        "title": channel.title,
        "capability": channel.capability,
        "allowed": True,
        "ready": True,
        "found": len(items),
        "kept": kept,
        "elapsed_seconds": elapsed,
        "error": getattr(channel, "last_error", ""),
        "top": top,
    }


def run_cycle(channels: Optional[List[str]] = None, limit: int = 25) -> CycleResult:
    """One pass over the farm."""
    names = channels or list(ACTIVE_CHANNELS.keys())
    result = CycleResult()

    for name in names:
        if name not in ACTIVE_CHANNELS:
            result.notes.append(f"Неизвестный воркер: {name}")
            continue
        outcome = run_channel(name, limit=limit)
        result.channels.append(outcome)
        result.found += int(outcome.get("found", 0))
        result.kept += int(outcome.get("kept", 0))
        if not outcome.get("allowed"):
            result.blocked.append(
                {
                    "channel": name,
                    "capability": outcome.get("capability"),
                    "reason": outcome.get("policy_reason"),
                }
            )
        if outcome.get("error"):
            result.notes.append(f"{name}: {outcome['error']}")

    return result


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


def overview() -> Dict[str, Any]:
    """Full farm state, used by the dashboard and `python -m agent.main status`."""
    return {
        "channels": disabled_report(),
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
        "min_score": get_float("MIN_SCORE", 0.0),
        "timeout_seconds": int(get_env("HTTP_TIMEOUT", "20") or 20),
    }
