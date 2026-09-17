"""Channel base class.

A channel ("worker" in the farm metaphor) declares exactly one capability it
needs from the policy gate. The orchestrator asks ``agent.policy`` about that
capability *before* the channel is allowed to touch the network. A channel
whose capability is denied can never be constructed into a running worker.
"""

from __future__ import annotations

from typing import Any, Dict, List

from agent.ledger import Opportunity
from agent.policy import Decision, evaluate


class Channel:
    #: short machine name, used in the ledger
    name: str = "channel"
    #: human title for the dashboard
    title: str = "Channel"
    #: the single capability this channel needs
    capability: str = "read_public_data"
    #: one-line description of what the worker actually does
    description: str = ""

    def __init__(self) -> None:
        self.decision: Decision = evaluate(self.capability)

    @property
    def allowed(self) -> bool:
        return self.decision.allowed

    def harvest(self, limit: int = 25) -> List[Opportunity]:  # pragma: no cover - interface
        raise NotImplementedError

    def status(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "capability": self.capability,
            "description": self.description,
            "allowed": self.allowed,
            "policy_label": self.decision.label,
            "policy_reason": self.decision.reason,
            "requirements": self.decision.requirements,
        }
