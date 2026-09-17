from __future__ import annotations

import requests

from agent.config import get_env


class LLMClient:
    def __init__(self) -> None:
        self.base_url = get_env("OLLAMA_BASE_URL", "http://localhost:11434")

    def generate(self, prompt: str, model: str = "llama3.1") -> str:
        if not self.is_available():
            return self._fallback_response(prompt)

        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
        }
        try:
            response = requests.post(f"{self.base_url}/api/generate", json=payload, timeout=30)
            response.raise_for_status()
            data = response.json()
            return data.get("response", self._fallback_response(prompt))
        except Exception:
            return self._fallback_response(prompt)

    def is_available(self) -> bool:
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=5)
            return response.ok
        except Exception:
            return False

    @staticmethod
    def _fallback_response(prompt: str) -> str:
        return (
            "Dry-run result: task executed with a safe placeholder response. "
            "This message is used when Ollama is not running or the local model is unavailable."
        )
