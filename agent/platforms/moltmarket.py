from __future__ import annotations

from typing import Any, Dict, List

from agent.models import Contract, Task
from agent.platforms.base import BasePlatform


class MoltMarketPlatform(BasePlatform):
    platform_name = "moltmarket"

    def get_tasks(self) -> List[Task]:
        if self.dry_run:
            return [
                Task(
                    id="mm_001",
                    platform=self.platform_name,
                    title="Agent profile description",
                    description="Write a short profile description for an autonomous marketing worker.",
                    category="writing",
                    budget=16.0,
                    competition=0.8,
                    difficulty=0.4,
                ),
                Task(
                    id="mm_002",
                    platform=self.platform_name,
                    title="Prompt optimization",
                    description="Improve a prompt for AI agent productivity and output calibration.",
                    category="ai_agent",
                    budget=22.0,
                    competition=0.7,
                    difficulty=0.6,
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
                    id="mm_contract_001",
                    platform=self.platform_name,
                    task_id="mm_002",
                    title="Prompt optimization",
                    requirements="Refine the prompt and suggest three better versions for production use.",
                    budget=22.0,
                    status="accepted",
                )
            ]
        return []

    def submit_deliverable(self, contract: Contract, result: str) -> Dict[str, Any]:
        if self.dry_run:
            return {"ok": True, "dry_run": True, "contract_id": contract.id, "status": "submitted"}
        return super().submit_deliverable(contract, result)
