"""Shared HTTP helpers.

Real-world problem this solves: a freshly created virtualenv ships its own
``certifi`` bundle, which can be older than the machine's trust store. In
locked-down environments (containers, sandboxes, corporate networks) that
produces ``SSLError: unable to get local issuer certificate`` while ``curl``
works fine.

So: prefer an explicit ``CA_BUNDLE`` from the environment, otherwise use the
system trust store when one exists, and only fall back to ``certifi``.
Certificate verification is never disabled.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import requests

from agent.config import get_env

SYSTEM_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian / Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",  # RHEL / Fedora
    "/etc/ssl/cert.pem",  # Alpine / macOS homebrew
    "/usr/local/etc/openssl/cert.pem",  # macOS
)


def ca_bundle() -> Optional[str]:
    """Path to a CA bundle, or None to let requests use its default."""
    explicit = get_env("CA_BUNDLE", "") or os.getenv("REQUESTS_CA_BUNDLE", "")
    if explicit and Path(explicit).exists():
        return explicit
    for candidate in SYSTEM_BUNDLES:
        if Path(candidate).exists():
            return candidate
    return None


def build_session(user_agent: str, headers: Optional[dict] = None) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})
    if headers:
        session.headers.update(headers)

    bundle = ca_bundle()
    if bundle:
        # requests reads this attribute on every request.
        session.verify = bundle  # type: ignore[attr-defined]
    return session


def describe_tls() -> str:
    """Human-readable line for `agent.main status`."""
    bundle = ca_bundle()
    return f"CA bundle: {bundle}" if bundle else "CA bundle: стандартный (certifi)"
