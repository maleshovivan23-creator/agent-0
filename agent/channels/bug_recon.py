"""Scope-gated passive recon worker.

Bug bounty automation done honestly means: automate the boring, safe half
(inventory, fingerprints, missing headers, report skeleton) and leave
exploitation to a human. This worker will not run at all until the operator
supplies ``data/scope.yaml`` naming the program and the exact hosts that are
authorized. Everything about the design is fail-closed:

* no scope file  -> refuse to start;
* host not listed -> refused per-target and recorded as a policy block;
* passive only   -> DNS, TLS certificate, HTTP response headers, security.txt;
* rate limited   -> a hard delay between requests to a host;
* no payloads, no fuzzing, no path brute force, no exploitation.

The output is a triage draft in ``reports/`` that a human reviews before
anything is submitted to a program.
"""

from __future__ import annotations

import json
import socket
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from agent.channels.base import Channel
from agent.config import get_env, project_root
from agent.http import build_session
from agent.ledger import Opportunity, record_action

SECURITY_HEADERS = [
    "strict-transport-security",
    "content-security-policy",
    "x-content-type-options",
    "x-frame-options",
    "referrer-policy",
    "permissions-policy",
]


@dataclass
class ScopeTarget:
    host: str
    program: str = ""
    notes: str = ""


@dataclass
class Scope:
    authorization: Dict[str, str] = field(default_factory=dict)
    targets: List[ScopeTarget] = field(default_factory=list)
    rate_limit_seconds: float = 3.0
    passive_only: bool = True
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        authorized_by = (self.authorization or {}).get("authorized_by", "").strip()
        program = (self.authorization or {}).get("program_url", "").strip()
        return bool(authorized_by and program and self.targets)


def scope_path() -> Path:
    custom = get_env("SCOPE_FILE", "") or ""
    if custom:
        return Path(custom)
    return project_root() / "data" / "scope.yaml"


def load_scope(path: Optional[Path] = None) -> Optional[Scope]:
    """Load and validate the authorization file.

    Supports a tiny YAML subset (enough for a flat file) so the project does
    not need a YAML dependency. If PyYAML is installed, it is used instead.
    """
    path = path or scope_path()
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")

    data: Dict[str, Any]
    try:  # pragma: no cover - optional dependency
        import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
    except Exception:
        data = _mini_yaml(text)

    authorization = data.get("authorization") or {}
    targets = [
        ScopeTarget(
            host=str(t.get("host", "")).strip(),
            program=str(t.get("program", "")).strip(),
            notes=str(t.get("notes", "")).strip(),
        )
        for t in (data.get("targets") or [])
        if isinstance(t, dict) and t.get("host")
    ]
    return Scope(
        authorization={k: str(v) for k, v in authorization.items()},
        targets=targets,
        rate_limit_seconds=float(data.get("rate_limit_seconds", 3.0) or 3.0),
        passive_only=bool(data.get("passive_only", True)),
        raw=data,
    )


def _mini_yaml(text: str) -> Dict[str, Any]:
    """Minimal parser for the scope file shape (no general YAML support)."""
    data: Dict[str, Any] = {"authorization": {}, "targets": []}
    section = ""
    current: Optional[Dict[str, str]] = None
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")):
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if key == "targets":
                section = "targets"
                continue
            if key == "authorization":
                section = "authorization"
                continue
            section = "scalars"
            data[key] = _coerce(value)
            continue
        stripped = line.strip()
        if section == "authorization":
            key, _, value = stripped.partition(":")
            data["authorization"][key.strip()] = value.strip()
        elif section == "targets":
            if stripped.startswith("-"):
                current = {}
                data["targets"].append(current)
                stripped = stripped[1:].strip()
                if not stripped:
                    continue
            if current is not None and ":" in stripped:
                key, _, value = stripped.partition(":")
                current[key.strip()] = value.strip()
    return data


def _coerce(value: str) -> Any:
    lowered = value.lower()
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    try:
        return float(value)
    except ValueError:
        return value


class BugReconChannel(Channel):
    name = "bug_recon"
    title = "Пассивная разведка в авторизованном scope"
    capability = "passive_recon_authorized_scope"
    description = (
        "Только хосты из data/scope.yaml. DNS, TLS, HTTP-заголовки, "
        "security.txt. Никакого фаззинга и эксплуатации. Результат — "
        "черновик отчёта для ручной проверки."
    )

    def __init__(self, scope: Optional[Scope] = None, passives_override: bool = False) -> None:
        super().__init__()
        self.scope = scope if scope is not None else load_scope()
        self.passives_override = passives_override
        self.last_error = ""
        self._session = build_session("AGENT-0-passive-recon (authorized scope only)")

    @property
    def ready(self) -> bool:
        return bool(self.allowed and self.scope and self.scope.complete)

    def harvest(self, limit: int = 25) -> List[Opportunity]:
        """Recon is not a source of tasks, but it *is* a source of leads."""
        if not self.ready:
            return []

        scope = self.scope
        assert scope is not None
        leads: List[Opportunity] = []

        for target in scope.targets[: max(1, limit)]:
            findings = self._probe(target, scope)
            if not findings:
                continue
            # A missing security header is context, not a vulnerability. Only
            # informational leads are emitted; no exploit claims are made.
            interesting = [f for f in findings if f.get("interesting")]
            if not interesting:
                continue
            lead_id = f"recon:{target.host}:{datetime.now(timezone.utc):%Y%m%d}"
            leads.append(
                Opportunity(
                    id=lead_id,
                    channel=self.name,
                    title=f"Разведданные: {target.host}",
                    url="",
                    repo=target.host,
                    reward_usd=0.0,
                    reward_source="none",
                    score=float(len(interesting)),
                    rationale="; ".join(f["title"] for f in interesting),
                    payload={"findings": findings, "program": target.program},
                )
            )
            self._write_report(target, findings)
        return leads

    # ------------------------------------------------------------- probing

    def _probe(self, target: ScopeTarget, scope: Scope) -> List[Dict[str, Any]]:
        findings: List[Dict[str, Any]] = []
        host = target.host
        url = host if host.startswith(("http://", "https://")) else f"https://{host}"
        bare = host.split("://")[-1].split("/")[0].split(":")[0]

        # 1. DNS
        try:
            infos = socket.getaddrinfo(bare, 443, proto=socket.IPPROTO_TCP)
            ips = sorted({info[4][0] for info in infos})
            findings.append(
                {"title": "DNS", "detail": f"{bare} -> {', '.join(ips)}", "interesting": False}
            )
        except socket.gaierror:
            findings.append(
                {
                    "title": "DNS",
                    "detail": f"{bare} не разрешается из этой среды (сеть песочницы ограничена)",
                    "interesting": False,
                }
            )
            return findings

        # 2. TLS certificate
        try:
            context = ssl.create_default_context()
            with socket.create_connection((bare, 443), timeout=8) as sock:
                with context.wrap_socket(sock, server_hostname=bare) as tls:
                    cert = tls.getpeercert()
            subject = dict(x[0] for x in cert.get("subject", [])).get("commonName", "?")
            issuer = dict(x[0] for x in cert.get("issuer", [])).get("organizationName", "?")
            not_after = cert.get("notAfter", "?")
            findings.append(
                {
                    "title": "TLS-сертификат",
                    "detail": f"CN={subject}, issuer={issuer}, not_after={not_after}",
                    "interesting": False,
                }
            )
        except Exception as exc:
            findings.append(
                {
                    "title": "TLS-сертификат",
                    "detail": f"не получен: {exc.__class__.__name__}",
                    "interesting": False,
                }
            )

        # 3. HTTP headers (single request, no payload)
        try:
            response = self._session.get(url, timeout=10, allow_redirects=True)
            headers = {k.lower(): v for k, v in response.headers.items()}
            missing = [h for h in SECURITY_HEADERS if h not in headers]
            fingerprint = {
                key: headers[key]
                for key in ("server", "x-powered-by", "via", "cf-ray", "x-vercel-id")
                if key in headers
            }
            findings.append(
                {
                    "title": "HTTP-отпечаток",
                    "detail": json.dumps(fingerprint, ensure_ascii=False) or "нет явных заголовков",
                    "interesting": False,
                }
            )
            findings.append(
                {
                    "title": "Отсутствуют security-заголовки",
                    "detail": ", ".join(missing) or "все на месте",
                    "interesting": len(missing) >= 3,
                    "note": "Это не уязвимость сама по себе — только контекст для человека.",
                }
            )
        except requests.RequestException as exc:
            findings.append(
                {
                    "title": "HTTP",
                    "detail": f"недоступно из среды песочницы: {exc.__class__.__name__}",
                    "interesting": False,
                }
            )

        record_action(
            channel=self.name,
            capability=self.capability,
            target=bare,
            allowed=True,
            reason=f"хост в авторизованном scope программы {target.program or 'n/a'}",
        )
        if scope.rate_limit_seconds:
            import time

            time.sleep(min(scope.rate_limit_seconds, 5.0))
        return findings

    # ------------------------------------------------------------- reporting

    def _write_report(self, target: ScopeTarget, findings: List[Dict[str, Any]]) -> Path:
        reports = project_root() / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = reports / f"recon-{target.host.replace('/', '_').replace(':', '_')}-{stamp}.md"
        lines = [
            f"# Черновик разведки: {target.host}",
            "",
            f"- Программа: {target.program or 'не указана'}",
            f"- Время: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            "- Тип: только пассивная разведка, эксплуатация не проводилась",
            "",
            "## Наблюдения",
            "",
        ]
        for finding in findings:
            lines.append(f"### {finding['title']}")
            lines.append("")
            lines.append(f"- {finding['detail']}")
            if finding.get("note"):
                lines.append(f"- Примечание: {finding['note']}")
            lines.append("")
        lines += [
            "## Что делает человек",
            "",
            "1. Проверить, что хост действительно в scope программы и активное тестирование разрешено.",
            "2. Провести ручную проверку гипотез (автоматизация только подсказала направление).",
            "3. Оформить отчёт по правилам программы и отправить от своего аккаунта.",
            "",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
