from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Task:
    id: str
    platform: str
    title: str
    description: str
    category: str
    budget: float
    currency: str = "USDC"
    competition: float = 0.0
    difficulty: float = 0.5
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BidRecommendation:
    task_id: str
    platform: str
    bid: float
    reason: str
    confidence: float


@dataclass
class Contract:
    id: str
    platform: str
    task_id: str
    title: str
    requirements: str
    budget: float
    status: str = "pending"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Payout:
    platform: str
    amount: float
    currency: str = "USDC"
    status: str = "pending"
    tx_hash: Optional[str] = None
