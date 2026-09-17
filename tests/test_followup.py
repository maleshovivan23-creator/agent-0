"""Слежение: ферма замечает, что задачу отнимают или закрывают."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from agent import followup
from agent.ledger import Opportunity, set_status, upsert_opportunities
from agent.triage import Triage


@pytest.fixture(autouse=True)
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "followup.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setenv("FOLLOWUP_STALE_DAYS", "10")
    return path


class FakeTriageClient:
    def __init__(self, verdict: Triage) -> None:
        self.verdict = verdict
        self.calls: List[str] = []

    def triage(self, *, opportunity_id: str, repo: str, number: int, **_: Any) -> Triage:
        self.calls.append(opportunity_id)
        return self.verdict


def verdict(**overrides: Any) -> Triage:
    data: Dict[str, Any] = {
        "opportunity_id": "github:acme/parser#7",
        "repo": "acme/parser",
        "number": 7,
        "state": "open",
        "attempts": 1,
        "open_prs": 0,
        "stale_days": 1,
    }
    data.update(overrides)
    return Triage(**data)


def seed(status: str = "working") -> None:
    upsert_opportunities([
        Opportunity(id="github:acme/parser#7", channel="github_bounties",
                    title="Fix the tokenizer", reward_usd=500.0, payload={})
    ])
    set_status("github:acme/parser#7", status)


def test_nothing_to_watch_before_work_starts() -> None:
    seed("queued")
    assert followup.check(triage_client=FakeTriageClient(verdict())) == []


def test_task_in_work_is_watched_and_reported_as_ok() -> None:
    seed("working")
    client = FakeTriageClient(verdict())
    watches = followup.check(triage_client=client)
    assert len(watches) == 1
    assert watches[0].verdict == "ok"
    assert followup.summary_lines(client=client) == [], "спокойные задачи не шумят в Telegram"


def test_a_rival_pull_request_changes_the_verdict() -> None:
    seed("working")
    client = FakeTriageClient(verdict())
    followup.check(triage_client=client)          # первый замер: PR нет
    client.verdict = verdict(open_prs=2)
    watches = followup.check(triage_client=client)

    watch = watches[0]
    assert watch.verdict == "rival"
    assert watch.open_prs_now == 2
    assert "новых открытых PR: 2" in watch.changes
    assert "переключиться" in watch.action


def test_new_attempts_by_others_are_reported_as_a_change() -> None:
    seed("working")
    client = FakeTriageClient(verdict(attempts=2))
    followup.check(triage_client=client)
    client.verdict = verdict(attempts=9)
    watch = followup.check(triage_client=client)[0]
    assert "новых заявок от других: 7" in watch.changes


def test_closed_issue_tells_the_human_to_stop() -> None:
    seed("working")
    watch = followup.check(triage_client=FakeTriageClient(verdict(state="closed")))[0]
    assert watch.verdict == "closed"
    assert "прекратить" in watch.action.lower()


def test_paid_bounty_is_a_loss_not_a_win() -> None:
    seed("working")
    client = FakeTriageClient(verdict(paid=True))
    watch = followup.check(triage_client=client)[0]
    assert watch.verdict == "lost"
    lines = followup.summary_lines(limit=5, client=client)
    assert any("!!" in line for line in lines), "потеря должна кричать, а не шептать"


def test_stale_task_gets_a_polite_question_advice() -> None:
    seed("working")
    watch = followup.check(triage_client=FakeTriageClient(verdict(stale_days=21)))[0]
    assert watch.verdict == "stale"
    assert "вопрос" in watch.action


def test_failed_check_does_not_break_the_others() -> None:
    seed("working")

    class Broken:
        def triage(self, **_: Any) -> Triage:
            raise RuntimeError("сеть недоступна")

    watches = followup.check(triage_client=Broken())
    assert watches[0].verdict == "unknown"
    assert "не удалось" in watches[0].reason


def test_only_github_tasks_are_watched() -> None:
    upsert_opportunities([
        Opportunity(id="contest:acme/2026-01-x", channel="audit_contests",
                    title="Контест", reward_usd=2000.0, payload={})
    ])
    set_status("contest:acme/2026-01-x", "working")
    assert followup.check(triage_client=FakeTriageClient(verdict())) == []


def test_snapshot_survives_between_checks() -> None:
    seed("working")
    followup.check(triage_client=FakeTriageClient(verdict(attempts=4)))
    # второй вызов с тем же клиентом не должен показывать «новые заявки»
    watch = followup.check(triage_client=FakeTriageClient(verdict(attempts=4)))[0]
    assert watch.changes == []
    assert watch.attempts_before == 4
