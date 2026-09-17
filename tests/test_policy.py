"""Policy gate behaviour. These tests protect the project's core promise."""

from __future__ import annotations

import pytest

from agent.policy import DENIED, PolicyViolation, assert_allowed, evaluate, report


def test_denied_tactics_are_denied() -> None:
    for capability in DENIED:
        decision = evaluate(capability)
        assert decision.allowed is False
        assert decision.alternatives, f"{capability} must suggest a safer alternative"


def test_faucets_and_sybil_are_denied_with_reasons() -> None:
    faucet = evaluate("faucet_claim_automation")
    assert not faucet.allowed
    assert "0.008" in faucet.reason or "$0.001" in faucet.reason
    assert faucet.requirements, "a denial must state the ToS/legal risk"

    sybil = evaluate("sybil_multi_wallet")
    assert not sybil.allowed
    assert "18.6" in sybil.reason


def test_unknown_capability_fails_closed() -> None:
    decision = evaluate("totally_new_idea")
    assert decision.allowed is False
    assert "Fail-closed" in decision.reason


def test_assert_allowed_requires_explicit_satisfaction() -> None:
    with pytest.raises(PolicyViolation):
        assert_allowed("passive_recon_authorized_scope")

    decision = assert_allowed(
        "passive_recon_authorized_scope", satisfied=["authorized_scope"]
    )
    assert decision.allowed

    with pytest.raises(PolicyViolation):
        assert_allowed("submit_deliverable_human_approved")


def test_submission_always_needs_a_human() -> None:
    decision = evaluate("submit_deliverable_human_approved")
    assert decision.allowed
    assert "human_approval" in decision.requirements


def test_report_lists_requested_tactics() -> None:
    data = report()
    assert data["denied"] and data["allowed"] and data["requested"]
    statuses = {item["request"]: item["status"] for item in data["requested"]}
    assert any(status == "blocked" for status in statuses.values())
    assert any(status.startswith("allowed") for status in statuses.values())
