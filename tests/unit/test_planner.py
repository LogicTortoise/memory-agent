"""Unit tests for the typed rewrite provider and the recall planner."""

from __future__ import annotations

import pytest

from memory_agent.application.search.planner import (
    PlannerOutcome,
    fingerprint_for,
    plan_query,
)
from memory_agent.application.search.rewrite_provider import (
    IdentityRewriteProvider,
    RewriteProviderError,
    StaticRewriteProvider,
)
from memory_agent.contracts.query import QueryVariant


class TestIdentityRewriteProvider:
    def test_no_rewrite_or_expansion(self) -> None:
        p = IdentityRewriteProvider()
        assert p.rewrite(original_query="x", hard_filters={}) is None
        assert p.expansions(original_query="x", hard_filters={}) == []


class TestStaticRewriteProvider:
    def test_returns_configured_values(self) -> None:
        p = StaticRewriteProvider(
            rewrite_value="video face replacement",
            expansion_values=("identity-preserving video gen", "video retargeting"),
        )
        assert p.rewrite(original_query="x", hard_filters={}) == "video face replacement"
        assert len(p.expansions(original_query="x", hard_filters={})) == 2

    def test_raising_provider(self) -> None:
        p = StaticRewriteProvider(raise_on_call=True)
        with pytest.raises(RewriteProviderError):
            p.rewrite(original_query="x", hard_filters={})


class TestPlanner:
    def test_planner_without_provider_keeps_only_original(self) -> None:
        outcome = plan_query(
            original_query="what did I say about videos",
            hard_filters={"project_ids": [], "domain_ids": []},
            rewrite="off",
            expansions=[],
            provider=None,
        )
        assert outcome.fallback_reason == "no_provider_wired"
        assert len(outcome.plan.variants) == 1
        assert outcome.plan.variants[0].origin == "original"

    def test_planner_with_provider_accepts_rewrite(self) -> None:
        provider = StaticRewriteProvider(
            rewrite_value="query for video face replacement rules",
            expansion_values=("identity-preserving video gen", "video retargeting"),
        )
        outcome = plan_query(
            original_query="rules about AI video face swap",
            hard_filters={"project_ids": [], "domain_ids": []},
            rewrite="auto",
            expansions=["*"],
            provider=provider,
        )
        assert outcome.fallback_reason is None
        origins = sorted(v.origin for v in outcome.plan.variants)
        # Original + 1 rewrite + 2 expansions = [expansion, expansion, original, rewrite]
        assert "rewrite" in origins
        assert "expansion" in origins

    def test_provider_failure_triggers_fallback(self) -> None:
        provider = StaticRewriteProvider(raise_on_call=True)
        outcome = plan_query(
            original_query="x",
            hard_filters={},
            rewrite="auto",
            expansions=["*"],
            provider=provider,
        )
        assert outcome.fallback_reason is not None
        assert outcome.fallback_reason.startswith("provider_failed")
        assert outcome.plan.variants[0].origin == "original"

    def test_identical_to_original_rejected(self) -> None:
        provider = StaticRewriteProvider(
            rewrite_value="hello",
            expansion_values=("hello",),
        )
        outcome = plan_query(
            original_query="hello",
            hard_filters={},
            rewrite="auto",
            expansions=["*"],
            provider=provider,
        )
        # rewrite and expansion that equal the original are recorded
        # in the rejection log.
        origins_accepted = [v.origin for v in outcome.plan.variants]
        assert "rewrite" not in origins_accepted
        assert "expansion" not in origins_accepted
        rejection_origins = [v.origin for v in outcome.rejected]
        assert "rewrite" in rejection_origins
        assert "expansion" in rejection_origins

    def test_at_most_two_expansions(self) -> None:
        provider = StaticRewriteProvider(
            rewrite_value="x",
            expansion_values=("a", "b", "c", "d"),
        )
        outcome = plan_query(
            original_query="original",
            hard_filters={},
            rewrite="auto",
            expansions=["*"],
            provider=provider,
        )
        accepted_expansions = [v for v in outcome.plan.variants if v.origin == "expansion"]
        assert len(accepted_expansions) <= 2


class TestFingerprintStability:
    def test_same_inputs_same_fingerprint(self) -> None:
        provider = StaticRewriteProvider(rewrite_value="x", expansion_values=("a",))
        o1 = plan_query(
            original_query="hello",
            hard_filters={"project_ids": ["p1"], "domain_ids": []},
            rewrite="auto",
            expansions=["*"],
            provider=provider,
        )
        o2 = plan_query(
            original_query="hello",
            hard_filters={"project_ids": ["p1"], "domain_ids": []},
            rewrite="auto",
            expansions=["*"],
            provider=provider,
        )
        assert fingerprint_for(o1.plan) == fingerprint_for(o2.plan)
