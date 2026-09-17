"""Readiness checks: the farm must name what is missing, not just complain."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest
import requests

from agent import doctor


class FakeResponse:
    def __init__(self, payload: Any = None, status_code: int = 200) -> None:
        self._payload = payload or {}
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload


class FakeSession:
    def __init__(self, response: FakeResponse | None = None, error: Exception | None = None) -> None:
        self.response = response or FakeResponse()
        self.error = error

    def get(self, url: str, timeout: int = 0) -> FakeResponse:
        if self.error:
            raise self.error
        return self.response


@pytest.fixture(autouse=True)
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "doctor.db"))


def factory(session: FakeSession):
    return lambda: session


def test_missing_country_blocks_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELIGIBILITY_COUNTRY", raising=False)
    data = doctor.readiness(session_factory=factory(FakeSession()))
    assert not data["can_start"]
    keys = [item["key"] for item in data["blockers"]]
    assert "country" in keys
    assert all(item["fix"] for item in data["next_actions"])


def test_blockers_come_before_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELIGIBILITY_COUNTRY", raising=False)
    monkeypatch.delenv("PAYOUT_WALLET", raising=False)
    data = doctor.readiness(session_factory=factory(FakeSession()))
    statuses = [item["status"] for item in data["next_actions"]]
    assert statuses == sorted(statuses, key=lambda s: 0 if s == doctor.BLOCK else 1)


def test_github_probe_reports_remaining_quota() -> None:
    session = FakeSession(FakeResponse({"resources": {"search": {"remaining": 42}}}))
    check = doctor.check_github_api(session_factory=factory(session))
    assert check.status == doctor.OK
    assert "42" in check.detail


def test_github_probe_failure_is_a_blocker() -> None:
    session = FakeSession(error=requests.ConnectionError("no route"))
    check = doctor.check_github_api(session_factory=factory(session))
    assert check.status == doctor.BLOCK
    assert check.required


def test_rate_limit_403_points_at_the_token() -> None:
    session = FakeSession(FakeResponse({}, status_code=403))
    check = doctor.check_github_api(session_factory=factory(session))
    assert check.status == doctor.BLOCK
    assert "GITHUB_TOKEN" in check.fix


def test_wallet_is_required_when_cards_are_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELIGIBILITY_COUNTRY", "RU")
    monkeypatch.delenv("PAYOUT_WALLET", raising=False)
    check = doctor.check_wallet()
    assert check.status == doctor.WARN
    assert "PAYOUT_WALLET" in check.fix

    monkeypatch.setenv("PAYOUT_WALLET", "0x" + "a" * 40)
    assert doctor.check_wallet().status == doctor.OK

    monkeypatch.setenv("PAYOUT_WALLET", "not-an-address")
    assert doctor.check_wallet().status == doctor.WARN


def test_wallet_is_optional_where_cards_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELIGIBILITY_COUNTRY", "DE")
    monkeypatch.delenv("PAYOUT_WALLET", raising=False)
    assert doctor.check_wallet().status == doctor.OK


def test_a_broken_check_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def exploding() -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(doctor, "CHECKS", [exploding])
    checks = doctor.run_checks()
    assert len(checks) == 1
    assert checks[0].status == doctor.BLOCK


def test_start_plan_is_actionable() -> None:
    plan = " ".join(doctor.start_plan())
    for command in ("проверка", "каналы-выплат", "цикл", "выплата-подтвердить"):
        assert command in plan, f"в плане нет команды {command}"


def test_readiness_serialises_for_the_dashboard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELIGIBILITY_COUNTRY", "DE")
    data = doctor.readiness(session_factory=factory(FakeSession()))
    assert set(data) >= {"checks", "blockers", "warnings", "verdict", "start_plan", "can_start"}
    assert isinstance(data["checks"][0], Dict)
