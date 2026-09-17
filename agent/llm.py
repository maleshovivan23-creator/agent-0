"""LLM access for the farm's sub-agents.

Three providers, chosen automatically:

* ``anthropic`` — used when ``ANTHROPIC_API_KEY`` is set (highest quality);
* ``ollama``    — used when a local Ollama server answers (free, private);
* ``none``      — deterministic fallback built from playbooks.

The fallback matters: the farm must produce a useful work brief on a machine
with no model at all, because "the LLM is down" is not a reason to stop
earning. Quality degrades, the pipeline does not.

Never logs or transmits secrets. ``ANTHROPIC_API_KEY`` is read from the
environment only.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent.config import get_env
from agent.http import build_session


@dataclass
class Completion:
    text: str
    provider: str
    model: str
    ok: bool
    error: str = ""


class LLMClient:
    def __init__(self, provider: Optional[str] = None, timeout: int = 90) -> None:
        self.timeout = timeout
        self.anthropic_key = get_env("ANTHROPIC_API_KEY", "") or ""
        self.ollama_base = (get_env("OLLAMA_BASE_URL", "http://localhost:11434") or "").rstrip("/")
        self.provider = provider or (get_env("LLM_PROVIDER", "auto") or "auto").lower()
        self.session = build_session("AGENT-0/2.0")
        self._ollama_checked: Optional[bool] = None

    # ---------------------------------------------------------------- probing

    def ollama_models(self) -> List[str]:
        try:
            response = self.session.get(f"{self.ollama_base}/api/tags", timeout=4)
            if response.ok:
                return [m.get("name", "") for m in response.json().get("models", [])]
        except Exception:
            pass
        return []

    def resolve_provider(self) -> str:
        if self.provider != "auto":
            return self.provider
        if self.anthropic_key:
            return "anthropic"
        if self._ollama_checked is None:
            self._ollama_checked = bool(self.ollama_models())
        return "ollama" if self._ollama_checked else "none"

    def describe(self) -> str:
        provider = self.resolve_provider()
        if provider == "anthropic":
            return f"anthropic ({get_env('ANTHROPIC_MODEL', 'claude-sonnet-4-5')})"
        if provider == "ollama":
            models = self.ollama_models()
            return f"ollama ({models[0] if models else 'локальная модель'})"
        return "без модели (детерминированные шаблоны)"

    # ------------------------------------------------------------- providers

    def _anthropic(self, prompt: str, max_tokens: int) -> Completion:
        model = get_env("ANTHROPIC_MODEL", "claude-sonnet-4-5") or "claude-sonnet-4-5"
        try:
            response = self.session.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=self.timeout,
            )
            if response.status_code != 200:
                return Completion("", "anthropic", model, False, f"HTTP {response.status_code}")
            blocks = response.json().get("content", [])
            text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            return Completion(text.strip(), "anthropic", model, bool(text))
        except Exception as exc:
            return Completion("", "anthropic", model, False, exc.__class__.__name__)

    def _ollama(self, prompt: str, max_tokens: int) -> Completion:
        models = self.ollama_models()
        model = get_env("OLLAMA_MODEL", "") or (models[0] if models else "")
        if not model:
            return Completion("", "ollama", "", False, "нет локальной модели")
        try:
            response = self.session.post(
                f"{self.ollama_base}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"num_predict": max_tokens, "temperature": 0.3},
                },
                timeout=self.timeout,
            )
            if not response.ok:
                return Completion("", "ollama", model, False, f"HTTP {response.status_code}")
            text = (response.json().get("response") or "").strip()
            return Completion(text, "ollama", model, bool(text))
        except Exception as exc:
            return Completion("", "ollama", model, False, exc.__class__.__name__)

    # ----------------------------------------------------------------- public

    def generate(self, prompt: str, max_tokens: int = 1400) -> Completion:
        provider = self.resolve_provider()
        if provider == "anthropic":
            result = self._anthropic(prompt, max_tokens)
            if result.ok:
                return result
            fallback = self._ollama(prompt, max_tokens) if self.ollama_models() else result
            return fallback
        if provider == "ollama":
            result = self._ollama(prompt, max_tokens)
            if result.ok:
                return result
            if self.anthropic_key:
                return self._anthropic(prompt, max_tokens)
            return result
        return Completion("", "none", "", False, "LLM не настроена")

    def generate_json(self, prompt: str, max_tokens: int = 900) -> Optional[Dict[str, Any]]:
        """Ask for JSON and parse it leniently (models like to add prose)."""
        completion = self.generate(prompt + "\n\nОтвечай строго JSON без пояснений.", max_tokens)
        if not completion.ok:
            return None
        match = re.search(r"\{.*\}", completion.text, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None


def client() -> LLMClient:
    return LLMClient()
