from __future__ import annotations

from typing import Any, Dict, List

from agent.models import Contract, Task
from agent.platforms.base import BasePlatform


class AgentWorldPlatform(BasePlatform):
    platform_name = "agentworld"

    def get_tasks(self) -> List[Task]:
        if self.dry_run:
            return [
                Task(
                    id="aw_001",
                    platform=self.platform_name,
                    title="Community marketing task",
                    description="Draft a short post for a new agent in the city economy ecosystem.",
                    category="writing",
                    budget=5.0,
                    competition=0.6,
                    difficulty=0.4,
                ),
                Task(
                    id="aw_002",
                    platform=self.platform_name,
                    title="NPC task review",
                    description="Review a simple social scenario and produce a recommended strategy.",
                    category="research",
                    budget=8.0,
                    competition=0.5,
                    difficulty=0.5,
                ),
            ]
        return []

    def place_bid(self, task: Task, bid: float) -> Dict[str, Any]:
        if self.dry_run:
            return {"ok": True, "dry_run": True, "task_id": task.id, "bid": bid, "status": "queued"}
        return super().place_bid(task, bid)

    def get_contracts(self) -> List[Contract]:
        if self.dry_run:
            return [
                Contract(
                    id="aw_contract_001",
                    platform=self.platform_name,
                    task_id="aw_001",
                    title="Community marketing task",
                    requirements="Write a short social-first copy with a clear CTA and value proposition.",
                    budget=5.0,
                    status="accepted",
                )
            ]
        return []

    def submit_deliverable(self, contract: Contract, result: str) -> Dict[str, Any]:
        if self.dry_run:
            return {"ok": True, "dry_run": True, "contract_id": contract.id, "status": "submitted"}
        return super().submit_deliverable(contract, result)
