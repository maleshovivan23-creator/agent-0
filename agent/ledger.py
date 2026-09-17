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
    fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
    first_seen TEXT,
    last_seen TEXT
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

CREATE TABLE IF NOT EXISTS hours (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    opportunity_id TEXT,
    hours REAL NOT NULL,
    note TEXT,
    logged_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id TEXT,
    channel TEXT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    path TEXT,
    action TEXT,
    summary TEXT,
    status TEXT DEFAULT 'ready',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox(status);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
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
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Догоняем схему для баз, созданных прошлыми версиями.

    Столбцы добавляются, а не пересоздаются: в базе лежит история часов и выплат,
    терять её из-за новой версии нельзя.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(opportunities)")}
    for column in ("first_seen", "last_seen"):
        if column not in existing:
            conn.execute(f"ALTER TABLE opportunities ADD COLUMN {column} TEXT")
    conn.execute(
        "UPDATE opportunities SET first_seen = COALESCE(first_seen, fetched_at), "
        "last_seen = COALESCE(last_seen, fetched_at)"
    )
    # индекс создаётся здесь, а не в SCHEMA: на старой базе столбца ещё нет
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_opportunities_first_seen ON opportunities(first_seen)"
    )
    conn.commit()


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


def upsert_opportunities(items: List[Opportunity], fresh_only: bool = False) -> int:
    """Insert or refresh opportunities. Returns how many rows were touched.

    ``fresh_only=True`` возвращает только те, которых в базе ещё не было, — именно
    эта цифра отвечает на вопрос «что появилось нового», а не «сколько строк
    обновилось». Свежие задачи и есть деньги: занятые разбирают в первые часы.
    """
    if not items:
        return 0
    conn = connect()
    new_ids: List[str] = []
    try:
        known = {
            row["id"]
            for row in conn.execute(
                "SELECT id FROM opportunities WHERE id IN (%s)"
                % ",".join("?" * len(items)),
                [item.id for item in items],
            )
        }
        now = _utcnow()
        for item in items:
            row = item.to_row()
            row["first_seen"] = now
            row["last_seen"] = now
            cols = ", ".join(row.keys())
            placeholders = ", ".join(f":{k}" for k in row)
            conn.execute(
                f"INSERT INTO opportunities ({cols}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET "
                f"score=excluded.score, reward_usd=excluded.reward_usd, "
                f"rationale=excluded.rationale, fetched_at=excluded.fetched_at, "
                f"payload=excluded.payload, status=excluded.status, "
                f"last_seen=excluded.last_seen",
                row,
            )
            if item.id not in known:
                new_ids.append(item.id)
        conn.commit()
    finally:
        conn.close()
    return len(new_ids) if fresh_only else len(items)


def fresh_opportunities(minutes: int = 180, limit: int = 20) -> List[Dict[str, Any]]:
    """Что появилось за последние N минут — по первому появлению, а не по отдаче API."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, channel, title, url, reward_usd, score, first_seen, status "
            "FROM opportunities "
            "WHERE first_seen >= datetime('now', ?) AND status='queued' "
            "ORDER BY first_seen DESC LIMIT ?",
            (f"-{int(minutes)} minutes", limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def age_hours(first_seen: Optional[str]) -> Optional[float]:
    """Сколько часов задача известна ферме (по первому появлению)."""
    if not first_seen:
        return None
    try:
        stamp = first_seen.replace("Z", "+00:00")
        moment = datetime.fromisoformat(stamp)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return round((datetime.now(timezone.utc) - moment).total_seconds() / 3600.0, 1)


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
    row = None
    try:
        cur = conn.execute(
            "UPDATE payouts SET verified=1, verified_at=? WHERE id=?",
            (_utcnow(), payout_id),
        )
        if cur.rowcount:
            row = conn.execute(
                "SELECT channel, amount, currency FROM payouts WHERE id=?", (payout_id,)
            ).fetchone()
        conn.commit()
    finally:
        conn.close()

    if row is not None:
        # Подтверждённые деньги — единственный настоящий результат: по ним робот
        # понимает, какой канал и плейбук реально приносят доход.
        try:
            from agent import learning

            learning.outcome(
                channel=row["channel"],
                amount=float(row["amount"] or 0.0),
                subject=row["channel"],
                detail=f"{row['amount']} {row['currency']} подтверждено",
            )
            learning.mark_updated()
        except Exception:
            pass
    return row is not None


def set_status(opportunity_id: str, status: str) -> bool:
    """Move an opportunity through the funnel: queued -> working -> submitted -> paid/dropped."""
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE opportunities SET status=? WHERE id=?", (status, opportunity_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_opportunity(opportunity_id: str) -> Optional[Dict[str, Any]]:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM opportunities WHERE id=?", (opportunity_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def log_hours(channel: str, hours: float, opportunity_id: str = "", note: str = "") -> int:
    """Record time actually spent. This is what makes the effective rate real."""
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO hours (channel, opportunity_id, hours, note, logged_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (channel, opportunity_id, hours, note, _utcnow()),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def analytics() -> Dict[str, Any]:
    """Income analytics: what the farm actually earns per hour of your time.

    The headline number is the *effective hourly rate*: verified income divided
    by hours you logged. Anything else (queue value, pipeline EV) is an
    estimate and is labelled as such.
    """
    conn = connect()
    try:
        def scalar(sql: str, params: tuple = ()) -> float:
            row = conn.execute(sql, params).fetchone()
            value = row[0] if row and row[0] is not None else 0.0
            return float(value)

        verified = scalar("SELECT SUM(amount) FROM payouts WHERE verified=1")
        claimed = scalar("SELECT SUM(amount) FROM payouts WHERE verified=0")
        hours = scalar("SELECT SUM(hours) FROM hours")

        pipeline_rows = conn.execute(
            "SELECT id, title, channel, reward_usd, score, rationale, payload, url "
            "FROM opportunities WHERE status='queued' ORDER BY score DESC LIMIT 15"
        ).fetchall()
        pipeline_ev = 0.0
        pipeline_hours = 0.0
        pipeline_items: List[Dict[str, Any]] = []
        for row in pipeline_rows:
            payload = json.loads(row["payload"] or "{}")
            expected = float(payload.get("expected_value_usd") or 0.0)
            effort = float(payload.get("effort_hours") or 0.0)
            # A negative expected value is a "do not touch", not pipeline value.
            pipeline_ev += max(0.0, expected)
            pipeline_hours += effort if expected > 0 else 0.0
            pipeline_items.append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "channel": row["channel"],
                    "reward_usd": row["reward_usd"],
                    "score": row["score"],
                    "expected_value_usd": round(expected, 2),
                    "effort_hours": effort,
                    "url": row["url"],
                }
            )

        by_channel = []
        for row in conn.execute(
            "SELECT o.channel AS channel, COUNT(*) AS n, SUM(o.reward_usd) AS advertised, "
            "AVG(o.score) AS avg_score "
            "FROM opportunities o WHERE o.status='queued' GROUP BY o.channel "
            "ORDER BY advertised DESC"
        ).fetchall():
            channel = row["channel"]
            channel_verified = scalar(
                "SELECT SUM(amount) FROM payouts WHERE verified=1 AND channel=?", (channel,)
            )
            channel_hours = scalar("SELECT SUM(hours) FROM hours WHERE channel=?", (channel,))
            by_channel.append(
                {
                    "channel": channel,
                    "queued": row["n"],
                    "advertised_usd": round(float(row["advertised"] or 0), 2),
                    "avg_score": round(float(row["avg_score"] or 0), 2),
                    "verified_usd": round(channel_verified, 2),
                    "hours": round(channel_hours, 2),
                    "effective_usd_per_hour": (
                        round(channel_verified / channel_hours, 2) if channel_hours else 0.0
                    ),
                }
            )

        attractive = sum(
            1 for item in pipeline_items if item["expected_value_usd"] > 0
        )
        return {
            "verified_usd": round(verified, 2),
            "claimed_usd": round(claimed, 2),
            "attractive_count": attractive,
            "rejected_count": len(pipeline_items) - attractive,
            "hours_logged": round(hours, 2),
            "effective_usd_per_hour": round(verified / hours, 2) if hours else 0.0,
            "pipeline_ev_usd": round(pipeline_ev, 2),
            "pipeline_hours": round(pipeline_hours, 1),
            "pipeline_ev_per_hour": (
                round(pipeline_ev / pipeline_hours, 2) if pipeline_hours else 0.0
            ),
            "pipeline": pipeline_items,
            "by_channel": by_channel,
            "estimate_note": (
                "pipeline_ev_usd — оценка ожидаемой выручки по очереди, а не гарантия: "
                "она считается из вероятности успеха и суммы награды."
            ),
        }
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
        dropped = int(scalar("SELECT COUNT(*) FROM opportunities WHERE status='dropped'"))
        working = int(scalar("SELECT COUNT(*) FROM opportunities "
                             "WHERE status IN ('working','proposed','submitted')"))
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
            "opportunities_dropped": dropped,
            "opportunities_in_progress": working,
            "verified_usd": round(verified_usd, 2),
            "claimed_usd": round(claimed_usd, 2),
            "payouts": payouts,
            "runs": runs,
            "channels": channels,
            "policy_blocks": blocked,
        }
    finally:
        conn.close()


# ------------------------------------------------------------------ key/value


def set_state(key: str, value: Any) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO state(key, value, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), _utcnow()),
        )
        conn.commit()
    finally:
        conn.close()


def get_state(key: str, default: Any = None) -> Any:
    conn = connect()
    try:
        row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return default


def all_state() -> Dict[str, Any]:
    conn = connect()
    try:
        rows = conn.execute("SELECT key, value, updated_at FROM state").fetchall()
    finally:
        conn.close()
    out: Dict[str, Any] = {}
    for row in rows:
        try:
            out[row["key"]] = {"value": json.loads(row["value"]), "updated_at": row["updated_at"]}
        except Exception:
            out[row["key"]] = {"value": row["value"], "updated_at": row["updated_at"]}
    return out
