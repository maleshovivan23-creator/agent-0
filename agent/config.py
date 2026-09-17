from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_environment() -> None:
    env_path = _project_root() / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        load_dotenv(_project_root() / ".env.example")


def get_env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    return value


def get_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "y", "on"}


def get_float(name: str, default: float = 0.0) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default
