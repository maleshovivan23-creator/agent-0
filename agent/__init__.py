from __future__ import annotations

import argparse
import os
from typing import Iterable, List

from agent.bidding import calculate_bid, should_bid
from agent.config import get_env, get_float, get_bool, load_environment
from agent.db import save_event
from agent.llm import LLMClient
from agent.models import Contract, Task
from agent.platforms.agentworld import AgentWorldPlatform
from agent.platforms.moltmarket import MoltMarketPlatform
from agent.platforms.opentask import OpenTaskPlatform
from agent.telegram import TelegramNotifier


PLATFORM_MAP = {
    "opentask": OpenTaskPlatform,
    "moltmarket": MoltMarketPlatform,
    "agentworld": AgentWorldPlatform,
}


def get_platforms(platform: str | None = None) -> Iterable[object]:
    if platform:
        selected = [platform]
    else:
        priority = get_env("PLATFORM_PRIORITY", "opentask,moltmarket,agentworld").split(",")
        selected = [p.strip() for p in priority if p.strip()]
    for name in selected:
        if name not in PLATFORM_MAP:
            continue
        yield PLATFORM_MAP[name](dry_run=(get_env("RUN_MODE", "dry-run") == "dry-run"))


def run_cycle(platform_name: str | None = None) -> None:
    load_environment()
    llm = LLMClient()
    notifier = TelegramNotifier()
    for platform in get_platforms(platform_name):
        tasks = platform.get_tasks()
        for task in tasks:
            if not should_bid(task):
                continue
            bid = calculate_bid(task, get_float("BID_MARGIN", 1.2))
            response = platform.place_bid(task, bid.bid)
            save_event(
                "bids",
                id=f"{task.platform}-{task.id}-{bid.bid}",
                task_id=task.id,
                platform=task.platform,
                bid=bid.bid,
                reason=bid.reason,
                confidence=bid.confidence,
            )
            message = f"[{task.platform}] bid {bid.bid} on {task.title}"
            notifier.send(message)
            if response.get("ok"):
                print(f"[OK] {task.platform} placed bid: {task.id} @ ${bid.bid}")

        contracts = platform.get_contracts()
        for contract in contracts:
            prompt = (
                f"Task title: {contract.title}\n\n"
                f"Requirements: {contract.requirements}\n\n"
                f"Produce a concise, usable deliverable in a way that could be submitted to the marketplace."
            )
            result = llm.generate(prompt)
            submission = platform.submit_deliverable(contract, result)
            save_event(
                "contracts",
                id=contract.id,
                task_id=contract.task_id,
                platform=contract.platform,
                title=contract.title,
                requirements=contract.requirements,
                budget=contract.budget,
                status=contract.status,
            )
            save_event(
                "submissions",
                id=f"{contract.id}-submission",
                contract_id=contract.id,
                platform=contract.platform,
                status=submission.get("status", "submitted"),
            )
            print(f"[SUBMIT] {contract.platform} submitted {contract.id} -> {submission.get('status')}")

        balance = platform.get_balance()
        if balance > 0:
            wallet = get_env("WALLET_ADDRESS", "0x0000000000000000000000000000000000000000")
            payout = platform.withdraw_to_wallet(wallet)
            save_event(
                "payouts",
                platform=platform.platform_name,
                amount=balance,
                currency="USDC",
                status=payout.get("status", "completed"),
                tx_hash=payout.get("tx_hash"),
            )
            notifier.send(f"[{platform.platform_name}] balance {balance} USDC processed to wallet {wallet}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AGENT-0 marketplace runner")
    parser.add_argument("--platform", choices=["opentask", "moltmarket", "agentworld"], default=None)
    parser.add_argument("--loop", action="store_true", help="Run repeatedly with a five-minute sleep interval.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        while True:
            run_cycle(args.platform)
            if not args.loop:
                break
            import time
            time.sleep(300)
    except KeyboardInterrupt:
        print("Stopping AGENT-0 loop.")


if __name__ == "__main__":
    main()
