"""Channel registry.

Only workers listed here can run in the farm. A candidate worker whose
capability is denied by ``agent.policy`` is still registered — so the
dashboard can show *why* it is off — but the orchestrator refuses to execute
it and records the refusal in the ledger.
"""

from __future__ import annotations

from typing import Dict, List, Type

from agent.channels.agent_marketplaces import AgentMarketplacesChannel
from agent.channels.audit_contests import AuditContestsChannel
from agent.channels.base import Channel
from agent.channels.bug_recon import BugReconChannel
from agent.channels.github_bounties import GitHubBountyChannel
from agent.channels.taskmarket import TaskMarketChannel

#: Workers the farm can actually run today.
ACTIVE_CHANNELS: Dict[str, Type[Channel]] = {
    GitHubBountyChannel.name: GitHubBountyChannel,
    AuditContestsChannel.name: AuditContestsChannel,
    AgentMarketplacesChannel.name: AgentMarketplacesChannel,
    TaskMarketChannel.name: TaskMarketChannel,
    BugReconChannel.name: BugReconChannel,
}

#: Tactics that were requested but are permanently disabled, with the policy
#: capability that blocks them. Rendered by the dashboard; never executed.
DISABLED_CHANNELS: List[Dict[str, str]] = [
    {
        "name": "faucet_farm",
        "title": "Ферма крипто-кранов",
        "capability": "faucet_claim_automation",
    },
    {
        "name": "captcha_solver",
        "title": "Решатель капчи",
        "capability": "captcha_bypass",
    },
    {
        "name": "sybil_wallets",
        "title": "Сибил-ферма кошельков",
        "capability": "sybil_multi_wallet",
    },
    {
        "name": "airdrop_farm",
        "title": "Автоматическая airdrop-ферма",
        "capability": "automated_airdrop_farming",
    },
    {
        "name": "auto_exploit",
        "title": "Автоэксплуатация уязвимостей",
        "capability": "auto_exploitation",
    },
    {
        "name": "tos_bypass_bots",
        "title": "Боты на площадках, где автоматизация запрещена",
        "capability": "tos_violating_platform_automation",
    },
]


def build_channel(name: str) -> Channel:
    if name not in ACTIVE_CHANNELS:
        raise KeyError(f"Unknown channel: {name}")
    return ACTIVE_CHANNELS[name]()


def build_all() -> Dict[str, Channel]:
    return {name: cls() for name, cls in ACTIVE_CHANNELS.items()}
