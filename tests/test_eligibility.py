"""Payout eligibility: the check that prevents working for nothing."""

from __future__ import annotations

from agent import eligibility


def test_stripe_blocked_country_is_flagged_with_alternatives() -> None:
    verdict = eligibility.assess("RU")
    assert verdict.status == "blocked"
    assert verdict.usable is False
    assert verdict.alternatives, "must offer a crypto-native alternative"
    assert "Stripe" in verdict.reason


def test_supported_country_is_ok() -> None:
    for country in ("DE", "US", "PL", "KZ", "GE"):
        verdict = eligibility.assess(country)
        assert verdict.status == "ok", country
        assert verdict.usable is True


def test_business_only_country_warns() -> None:
    verdict = eligibility.assess("IN")
    assert verdict.status == "business_only"
    assert "юрлиц" in verdict.reason or "ИП" in verdict.reason


def test_crypto_rail_works_regardless_of_country() -> None:
    for country in ("RU", "DE", "IR"):
        verdict = eligibility.assess(country, "crypto_usdc")
        assert verdict.status == "ok"
        assert verdict.usable is True


def test_unknown_country_is_honest_about_uncertainty() -> None:
    verdict = eligibility.assess("ZZ")
    assert verdict.status == "unknown"
    assert "проверьте" in verdict.reason.lower()


def test_recommended_rail_switches_for_blocked_countries() -> None:
    assert eligibility.recommend_rail("DE") == "stripe_connect"
    assert eligibility.recommend_rail("RU") == "crypto_usdc"
    assert eligibility.recommend_rail("IN") == "crypto_usdc"


def test_preflight_is_the_same_gate_used_before_claiming() -> None:
    assert eligibility.preflight("RU").usable is False
    assert eligibility.preflight("DE").usable is True


def test_verification_date_is_declared() -> None:
    assert eligibility.VERIFIED_ON
    assert eligibility.summary_table()
