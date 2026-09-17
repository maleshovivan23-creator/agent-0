"""Money accounting for the farm.

Design rule: a number only counts as income when it can be tied to a
verifiable payout record (transaction hash, invoice id, platform receipt).
Everything else is "claimed" and is reported separately.

This module exists because "заработок" is the whole point of the project, and
an agent that can freely write numbers into a database will eventually write
numbers that are not true. So the agent can record *opportunities* and
*claims*, but marking something as verified requires an explicit human action
(``python -m agent.main payout-verify <id>``).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.config import get_env

SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    repo TEXT,
    reward_usd REAL DEFAULT 0,
    reward_source TEXT,
    score REAL DEFAULT 0,
    rationale TEXT,
    status TEXT DEFAULT 'queued',
    payload TEXT,
    fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    capability TEXT,
    found INTEGER DEFAULT 0,
    kept INTEGER DEFAULT 0,
    policy TEXT DEFAULT 'allowed',
    note TEXT,
    started_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS payouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT DEFAULT 'USD',
    evidence TEXT,
    note TEXT,
    verified INTEGER DEFAULT 0,
    recorded_at TEXT DEFAULT CURRENT_TIMESTAMP,
    verified_at TEXT
);

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    capability TEXT NOT NULL,
    target TEXT,
    allowed INTEGER NOT NULL,
    reason TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_path() -> Path:
    path = Path(get_env("DB_PATH", "data/agent.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path()))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


@dataclass
class Opportunity:
    id: str
    channel: str
    title: str
    url: str = ""
    repo: str = ""
    reward_usd: float = 0.0
    reward_source: str = ""
    score: float = 0.0
    rationale: str = ""
    status: str = "queued"
    payload: Dict[str, Any] = field(default_factory=dict)
    fetched_at: str = ""

    def to_row(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "channel": self.channel,
            "title": self.title,
            "url": self.url,
            "repo": self.repo,
            "reward_usd": self.reward_usd,
            "reward_source": self.reward_source,
            "score": self.score,
            "rationale": self.rationale,
            "status": self.status,
            "payload": json.dumps(self.payload, ensure_ascii=False),
            "fetched_at": self.fetched_at or _utcnow(),
        }


def upsert_opportunities(items: List[Opportunity]) -> int:
    """Insert or refresh opportunities. Returns how many rows were touched."""
    if not items:
        return 0
    conn = connect()
    try:
        for item in items:
            row = item.to_row()
            cols = ", ".join(row.keys())
            placeholders = ", ".join(f":{k}" for k in row)
            conn.execute(
                f"INSERT INTO opportunities ({cols}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET "
                f"score=excluded.score, reward_usd=excluded.reward_usd, "
                f"rationale=excluded.rationale, fetched_at=excluded.fetched_at, "
                f"payload=excluded.payload",
                row,
            )
        conn.commit()
    finally:
        conn.close()
    return len(items)


def top_opportunities(limit: int = 20, channel: Optional[str] = None) -> List[sqlite3.Row]:
    conn = connect()
    try:
        if channel:
            rows = conn.execute(
                "SELECT * FROM opportunities WHERE channel=? AND status='queued' "
                "ORDER BY score DESC LIMIT ?",
                (channel, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM opportunities WHERE status='queued' "
                "ORDER BY score DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return list(rows)
    finally:
        conn.close()


def record_run(
    channel: str,
    capability: str,
    found: int,
    kept: int,
    policy: str = "allowed",
    note: str = "",
) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO runs (channel, capability, found, kept, policy, note, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (channel, capability, found, kept, policy, note, _utcnow()),
        )
        conn.commit()
    finally:
        conn.close()


def record_action(channel: str, capability: str, target: str, allowed: bool, reason: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO actions (channel, capability, target, allowed, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (channel, capability, target, 1 if allowed else 0, reason, _utcnow()),
        )
        conn.commit()
    finally:
        conn.close()


def record_payout(
    channel: str,
    amount: float,
    currency: str = "USD",
    evidence: str = "",
    note: str = "",
) -> int:
    """Record a *claimed* payout. It does not count as income until verified."""
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO payouts (channel, amount, currency, evidence, note, verified, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, 0, ?)",
            (channel, amount, currency, evidence, note, _utcnow()),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def verify_payout(payout_id: int) -> bool:
    """Human-only step: mark a claimed payout as verified income."""
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE payouts SET verified=1, verified_at=? WHERE id=?",
            (_utcnow(), payout_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def summary() -> Dict[str, Any]:
    """Ledger summary. Verified income is reported separately from claims."""
    conn = connect()
    try:
        def scalar(sql: str, default: float = 0.0) -> float:
            row = conn.execute(sql).fetchone()
            value = row[0] if row and row[0] is not None else default
            return float(value)

        total_opps = int(scalar("SELECT COUNT(*) FROM opportunities"))
        queued = int(scalar("SELECT COUNT(*) FROM opportunities WHERE status='queued'"))
        verified_usd = scalar("SELECT SUM(amount) FROM payouts WHERE verified=1")
        claimed_usd = scalar("SELECT SUM(amount) FROM payouts WHERE verified=0")
        payouts = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM payouts ORDER BY id DESC LIMIT 20"
            ).fetchall()
        ]
        runs = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT 20"
            ).fetchall()
        ]
        channels = [
            dict(r)
            for r in conn.execute(
                "SELECT channel, COUNT(*) AS n, SUM(reward_usd) AS reward, "
                "AVG(score) AS avg_score, MAX(fetched_at) AS last_seen "
                "FROM opportunities GROUP BY channel ORDER BY reward DESC"
            ).fetchall()
        ]
        blocked = [
            dict(r)
            for r in conn.execute(
                "SELECT channel, capability, target, reason, created_at FROM actions "
                "WHERE allowed=0 ORDER BY id DESC LIMIT 20"
            ).fetchall()
        ]
        return {
            "opportunities_total": total_opps,
            "opportunities_queued": queued,
            "verified_usd": round(verified_usd, 2),
            "claimed_usd": round(claimed_usd, 2),
            "payouts": payouts,
            "runs": runs,
            "channels": channels,
            "policy_blocks": blocked,
        }
    finally:
        conn.close()
