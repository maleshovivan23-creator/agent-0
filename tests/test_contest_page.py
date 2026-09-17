"""Разбор страницы контеста: пул, даты, код — из README, без догадок."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent import contest_page

CODE4RENA = """# Jupiter Lend audit details
- Total Prize Pool: $107,000 in USDC
    - HM awards: up to $96,000 in USDC
    - QA awards: $4,000 in USDC
- Join the audit
- Starts February 12, 2026 20:00 UTC
- Ends March 13, 2026 20:00 UTC

### Important notes
"""

SHERLOCK = """# Tare contest details

- Join [Sherlock Discord](https://discord.gg/MABEWyASkp)
- Submit findings using the **Issues** page
"""


def at(text: str = "") -> datetime:
    return datetime(2026, 2, 20, tzinfo=timezone.utc)


def test_code4rena_page_gives_pool_dates_and_code() -> None:
    page = contest_page.parse(CODE4RENA + "\nCode: https://github.com/Instadapp/fluid-solana-programs\n")
    assert page.prize_pool_usd == 107_000
    assert page.starts_at is not None and page.starts_at.startswith("2026-02-12")
    assert page.ends_at is not None and page.ends_at.startswith("2026-03-13")
    assert page.code_repos == ["Instadapp/fluid-solana-programs"]


def test_hours_left_is_counted_from_the_end_date() -> None:
    page = contest_page.parse(CODE4RENA, now=at("2026-02-20"))
    # с 20 февраля 00:00 до 13 марта 20:00 — 21 день и 20 часов
    assert page.hours_left == pytest.approx(21 * 24 + 20, abs=1)


def test_expired_contest_is_flagged_not_hidden() -> None:
    page = contest_page.parse(CODE4RENA, now=datetime(2026, 9, 17, tzinfo=timezone.utc))
    assert page.hours_left < 0
    assert any("закончился" in note for note in page.notes)


def test_near_deadline_is_warned_about() -> None:
    page = contest_page.parse(CODE4RENA, now=datetime(2026, 3, 12, 12, tzinfo=timezone.utc))
    assert page.hours_left is not None and page.hours_left < 48
    assert any("до конца" in note for note in page.notes)


def test_page_without_numbers_stays_empty() -> None:
    page = contest_page.parse(SHERLOCK)
    assert page.prize_pool_usd is None
    assert page.ends_at is None
    assert page.hours_left is None
    assert not page.found_anything


def test_small_pool_is_called_out() -> None:
    text = "- Total Prize Pool: $3,500 in USDC\n- Ends March 13, 2026 20:00 UTC\n"
    page = contest_page.parse(text)
    assert page.prize_pool_usd == 3500
    assert any("пул небольшой" in note for note in page.notes)


def test_markdown_link_to_code_is_detected() -> None:
    text = "Details: [Code](https://github.com/acme/protocol-core)\nEnds March 13, 2026 20:00 UTC\n"
    page = contest_page.parse(text)
    assert page.code_repos == ["acme/protocol-core"]


def test_empty_input_does_not_crash() -> None:
    page = contest_page.parse("")
    assert page.as_dict()["prize_pool_usd"] is None
