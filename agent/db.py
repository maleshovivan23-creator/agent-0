from __future__ import annotations

import sqlite3
from pathlib import Path

from agent.config import get_env


DB_PATH = get_env("DB_PATH", "data/agent.db")


def ensure_db() -> str:
    db_path = Path(DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT,
            platform TEXT,
            title TEXT,
            description TEXT,
            category TEXT,
            budget REAL,
            currency TEXT,
            competition REAL,
            difficulty REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bids (
            id TEXT,
            task_id TEXT,
            platform TEXT,
            bid REAL,
            reason TEXT,
            confidence REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS contracts (
            id TEXT,
            task_id TEXT,
            platform TEXT,
            title TEXT,
            requirements TEXT,
            budget REAL,
            status TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS submissions (
            id TEXT,
            contract_id TEXT,
            platform TEXT,
            status TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS payouts (
            platform TEXT,
            amount REAL,
            currency TEXT,
            status TEXT,
            tx_hash TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()
    return str(db_path)


def save_event(table: str, **values: object) -> None:
    db_path = ensure_db()
    conn = sqlite3.connect(db_path)
    cols = ", ".join(values.keys())
    placeholders = ", ".join(["?" for _ in values])
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", tuple(values.values()))
    conn.commit()
    conn.close()


def get_db_connection() -> sqlite3.Connection:
    db_path = ensure_db()
    return sqlite3.connect(db_path)
