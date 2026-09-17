from __future__ import annotations

from typing import Any, Dict, List

from agent.config import get_env, get_float
from agent.models import Contract, Task


class BasePlatform:
    platform_name: str = "base"
    dry_run: bool = True

    def __init__(self, dry_run: bool | None = None):
        self.dry_run = dry_run if dry_run is not None else True

    def get_tasks(self) -> List[Task]:
        return []

    def place_bid(self, task: Task, bid: float) -> Dict[str, Any]:
        return {"ok": True, "dry_run": self.dry_run, "task_id": task.id, "bid": bid}

    def get_contracts(self) -> List[Contract]:
        return []

    def submit_deliverable(self, contract: Contract, result: str) -> Dict[str, Any]:
        return {"ok": True, "dry_run": self.dry_run, "contract_id": contract.id, "status": "submitted"}

    def get_balance(self) -> float:
        return 0.0

    def withdraw_to_wallet(self, wallet: str) -> Dict[str, Any]:
        return {"ok": True, "dry_run": self.dry_run, "wallet": wallet}
