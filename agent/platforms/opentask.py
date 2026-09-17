from __future__ import annotations

from typing import Any, Dict, List

from agent.models import Contract, Task
from agent.platforms.base import BasePlatform


class OpenTaskPlatform(BasePlatform):
    platform_name = "opentask"

    def get_tasks(self) -> List[Task]:
        if self.dry_run:
            return [
                Task(
                    id="ot_001",
                    platform=self.platform_name,
                    title="Quick market research summary",
                    description="Summarize the latest AI agent marketplace landscape and show category trends.",
                    category="research",
                    budget=42.0,
                    competition=0.7,
                    difficulty=0.6,
                ),
                Task(
                    id="ot_002",
                    platform=self.platform_name,
                    title="Data analysis snapshot",
                    description="Convert data into a concise business summary with key insights.",
                    category="data_analysis",
                    budget=88.0,
                    competition=0.5,
                    difficulty=0.8,
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
                    id="ot_contract_001",
                    platform=self.platform_name,
                    task_id="ot_001",
                    title="Quick market research summary",
                    requirements="Write a concise report with competitor observations and action items.",
                    budget=42.0,
                    status="accepted",
                )
            ]
        return []

    def submit_deliverable(self, contract: Contract, result: str) -> Dict[str, Any]:
        if self.dry_run:
            return {"ok": True, "dry_run": True, "contract_id": contract.id, "status": "submitted"}
        return super().submit_deliverable(contract, result)
