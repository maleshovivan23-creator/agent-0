"""Scoring must be conservative: no lottery tickets, no joke bounties."""

from __future__ import annotations

from agent.scoring import (
    MAX_PLAUSIBLE_REWARD,
    extract_reward,
    looks_like_junk,
    score_opportunity,
)


def test_extract_reward_prefers_labels() -> None:
    reward, source = extract_reward(["💎 Bounty", "$700"], "Fix the thing", "no amount here")
    assert reward == 700
    assert source == "label:$700"


def test_extract_reward_from_title() -> None:
    reward, source = extract_reward(
        ["bounty"], "[BOUNTY: $1,500] Implement X", "body"
    )
    assert reward == 1500
    assert source == "title"


def test_absurd_amount_is_rejected() -> None:
    reward, source = extract_reward(
        ["bounty"], "[Bounty] $999999999999999999 FOR THE UNIVERSE", ""
    )
    assert reward == 0
    assert source == "none"


def test_junk_filter_blocks_joke_issues() -> None:
    assert looks_like_junk("[Bounty] $999999999999999999 BOUNTY FOR THE UNIVERSE", 0, 1)
    assert looks_like_junk("test issue", 0, 0)
    assert looks_like_junk("Big reward in a brand new repo", 9000, 0)
    assert looks_like_junk("Fix pagination", 300, 4000) is None


def test_score_is_zero_for_unfunded_task() -> None:
    score = score_opportunity(
        labels=["bug"],
        title="Unpaid chore",
        body="x" * 300,
        comments=1,
        repo_stars=10,
        hourly_rate=15.0,
    )
    assert score.reward_usd == 0
    assert score.ev_per_hour <= 0


def test_ev_per_hour_math_is_sane() -> None:
    score = score_opportunity(
        labels=["💎 Bounty", "$500", "good first issue"],
        title="Add integration test",
        body="y" * 800,
        comments=0,
        repo_stars=5000,
        hourly_rate=15.0,
    )
    assert score.reward_usd == 500
    assert 0 < score.probability < 0.5
    expected = score.probability * 500 - score.effort_hours * 15.0 * 0.15
    assert abs(score.expected_value_usd - expected) < 0.02
    assert abs(score.ev_per_hour - score.expected_value_usd / score.effort_hours) < 0.02


def test_competition_increases_effort() -> None:
    quiet = score_opportunity(
        labels=["bounty", "$100"], title="A", body="z" * 500, comments=0,
        repo_stars=100, hourly_rate=15.0,
    )
    crowded = score_opportunity(
        labels=["bounty", "$100"], title="A", body="z" * 500, comments=140,
        repo_stars=100, hourly_rate=15.0,
    )
    assert crowded.effort_hours > quiet.effort_hours
    assert crowded.probability < quiet.probability


def test_triage_probability_overrides_heuristic() -> None:
    """Measured competition must beat the comment-count heuristic."""
    crowded = score_opportunity(
        labels=["bounty", "$700"], title="Fix pagination", body="x" * 500,
        comments=1, repo_stars=300, hourly_rate=15.0,
        triage_probability=0.008, verified_amount=700.0,
    )
    quiet = score_opportunity(
        labels=["bounty", "$700"], title="Fix pagination", body="x" * 500,
        comments=1, repo_stars=300, hourly_rate=15.0,
        triage_probability=0.22, verified_amount=700.0,
    )
    assert crowded.probability == 0.008
    assert quiet.probability == 0.22
    assert crowded.total < quiet.total / 10
    assert crowded.reward_source == "bounty bot в задаче"


def test_fresh_bounty_gets_early_mover_bonus() -> None:
    kwargs = dict(
        labels=["bounty", "$500"], title="Add integration", body="y" * 600,
        comments=0, repo_stars=1000, hourly_rate=15.0, triage_probability=0.2,
    )
    fresh = score_opportunity(**kwargs, age_days=1)
    stale = score_opportunity(**kwargs, age_days=40)
    assert fresh.total > stale.total
    assert "свежая задача" in fresh.rationale
    assert "свежая задача" not in stale.rationale


def test_playbook_choice_shortens_estimated_hours() -> None:
    common = dict(
        labels=["bounty", "$100"], title="A", body="z" * 500,
        comments=0, repo_stars=100, hourly_rate=15.0,
    )
    docs = score_opportunity(**common, playbook_key="documentation")
    generic = score_opportunity(**common, playbook_key="generic")
    assert docs.playbook == "documentation"
    assert docs.playbook_title
    assert docs.effort_hours < generic.effort_hours


def test_max_plausible_boundary() -> None:
    assert MAX_PLAUSIBLE_REWARD == 50_000.0
    reward, _ = extract_reward([], "", f"reward ${MAX_PLAUSIBLE_REWARD + 1}")
    assert reward == 0
