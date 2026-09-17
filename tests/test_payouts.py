"""Receiving rails: the part that decides whether work turns into money."""

from __future__ import annotations

from agent import payouts


def test_every_rail_is_documented() -> None:
    for rail in payouts.table():
        assert rail["summary"], rail["key"]
        assert rail["steps"], rail["key"]
        assert rail["source"], f"{rail['key']} must cite a source"
        assert rail["requires"], rail["key"]


def test_blocked_country_gets_alternatives_not_a_dead_end() -> None:
    assessment = payouts.recommend("RU")
    assert "stripe_card" in assessment.blocked
    assert assessment.recommended, "must offer working rails instead"
    assert "usdc_wallet" in assessment.recommended
    assert any("USDC" in note or "крипто" in note.lower() for note in assessment.notes)


def test_supported_country_keeps_stripe_first() -> None:
    assessment = payouts.recommend("DE")
    assert assessment.blocked == []
    assert assessment.recommended[0] == "stripe_card"


def test_unknown_country_still_gets_guidance() -> None:
    assessment = payouts.recommend("ZZ")
    assert assessment.recommended
    assert assessment.notes


def test_missing_country_asks_for_one() -> None:
    assessment = payouts.recommend("")
    assert any("--country" in note for note in assessment.notes)


def test_checklist_warns_about_stripe_gap() -> None:
    items = payouts.checklist("RU")
    assert any("Stripe" in item for item in items)
    assert any("налог" in item.lower() for item in items), "taxes must be flagged, not hidden"


def test_supported_stripe_note_mentions_kyc() -> None:
    rail = payouts.rail("stripe_card")
    assert rail is not None
    assert any("KYC" in item for item in rail.requires)
