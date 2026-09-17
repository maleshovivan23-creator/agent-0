from __future__ import annotations

import json
from typing import Any, Dict

import requests

from agent.config import get_env


class TelegramNotifier:
    def __init__(self) -> None:
        self.token = get_env("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = get_env("TELEGRAM_CHAT_ID", "")

    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, message: str) -> bool:
        if not self.enabled():
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"}
        try:
            resp = requests.post(url, data=payload, timeout=10)
            return resp.ok
        except Exception:
            return False
