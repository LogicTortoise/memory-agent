"""Domain rule tests."""

from __future__ import annotations

import pytest

from memory_agent.domain.batching import (
    BatchPolicyInput,
    TurnSample,
    heuristic_tokens,
    is_oversize,
    plan_batches,
    plan_one_batch,
)
from memory_agent.domain.evidence import (
    EvidenceValidationError,
    assert_quote_matches_slice,
    slice_unicode,
)
from memory_agent.domain.lifecycle import (
    StatusTransitionError,
    assert_valid_status_transition,
)
from memory_agent.domain.query_policy import (
    RecallConstraintError,
    enforce_recall_constraints,
)
from memory_agent.domain.scope import (
    Scope,
    normalize_scope,
    scopes_intersect,
)


def _t(text, *, session="s1", branch="main", context_only=False, complete=True, role="user"):
    return TurnSample(
        turn_id=f"turn::{text[:32]}",
        session_id=session, branch_id=branch,
        role=role, text=text, estimated_tokens=heuristic_tokens(text),
        is_complete=complete, context_only=context_only,
    )


class TestBatching:
    def test_twenty_turns_four_batches(self):
        turns = [_t(f"user says {i}") for i in range(20)]
        batches = plan_batches(turns, BatchPolicyInput())
        assert len(batches) == 4
        assert sum(len(b) for b in batches) == 20

    def test_twelve_turns_three_batches(self):
        turns = [_t(f"user says {i}") for i in range(12)]
        batches = plan_batches(turns, BatchPolicyInput())
        assert len(batches) == 3
        assert sum(len(b) for b in batches) == 12

    def test_tools_dont_count_as_target(self):
        # 2 user turns + 40 tool messages interleaved.
        turns = []
        for i in range(2):
            turns.append(_t(f"user says {i}"))
            for j in range(20):
                turns.append(_t(f"tool output {i}.{j}", role="tool", context_only=True))
        batches = plan_batches(turns, BatchPolicyInput())
        # All targets go into one batch (2 user + 40 context); the 40
        # context don't count as targets.
        assert len(batches) == 1
        target_count = sum(1 for t in batches[0] if not t.context_only)
        assert target_count == 2

    def test_oversize_single_turn_becomes_batch(self):
        turns = [_t("x" * 10_000)]  # ~2500 tokens -> not oversize, but makes it small
        # Now create an oversize turn explicitly:
        big = TurnSample(
            turn_id="big", session_id="s", branch_id="main", role="user",
            text="x" * 200, estimated_tokens=6001, is_complete=True,
        )
        policy = BatchPolicyInput(max_input_tokens=6000)
        assert is_oversize(big, policy.max_input_tokens)
        # oversize -> single-element batch with reason='oversize'.
        batch, idx, reason = plan_one_batch([big, _t("next")], policy, 0)
        assert len(batch) == 1
        assert reason == "oversize"
        assert idx == 1

    def test_replan_no_progress_safety(self):
        # context_only never leads -> make the planner exit safely.
        turns = [
            _t("pre", context_only=True),
            _t("real user message"),
        ]
        batches = plan_batches(turns, BatchPolicyInput())
        assert len(batches) == 1

    def test_token_cap(self):
        # 10 turns of 1000 tokens each, max=2500 -> 2-3 turns per batch
        turns = [
            TurnSample(turn_id=f"t{i}", session_id="s", branch_id="main",
                        role="user", text="x", estimated_tokens=1000, is_complete=True)
            for i in range(10)
        ]
        batches = plan_batches(turns, BatchPolicyInput(max_turns_per_batch=5, max_input_tokens=2500))
        # 1000*5=5000 > 2500, so max 2 turns per batch -> 5 batches
        assert len(batches) == 5


class TestEvidence:
    def test_slice_unicode(self):
        assert slice_unicode("hello", 0, 5) == "hello"
        with pytest.raises(EvidenceValidationError):
            slice_unicode("hi", 0, 5)

    def test_quote_matches_slice(self):
        assert_quote_matches_slice(
            quote="hello",
            source_text="hello world",
            span_start=0, span_end=5,
            role="user", origin="user_session",
        )

    def test_quote_mismatch_rejected(self):
        with pytest.raises(EvidenceValidationError):
            assert_quote_matches_slice(
                quote="hellx",
                source_text="hello world",
                span_start=0, span_end=4,
                role="user", origin="user_session",
            )

    def test_non_user_role_rejected(self):
        with pytest.raises(EvidenceValidationError):
            assert_quote_matches_slice(
                quote="hello",
                source_text="hello world",
                span_start=0, span_end=5,
                role="assistant", origin="assistant_response",
            )


class TestScope:
    def test_global_intersects_global(self):
        assert scopes_intersect(Scope(level="global"), Scope(level="global"))

    def test_global_intersects_project(self):
        assert scopes_intersect(
            Scope(level="global"),
            Scope(level="project", project_ids=["p1"]),
        )

    def test_same_project_intersect(self):
        assert scopes_intersect(
            Scope(level="project", project_ids=["p1"]),
            Scope(level="project", project_ids=["p1"]),
        )

    def test_different_projects_no_intersect(self):
        assert not scopes_intersect(
            Scope(level="project", project_ids=["p1"]),
            Scope(level="project", project_ids=["p2"]),
        )

    def test_excludes_break_intersection(self):
        assert not scopes_intersect(
            Scope(level="global", excludes=["p1"]),
            Scope(level="project", project_ids=["p1"]),
        )

    def test_normalize_dedup(self):
        s = normalize_scope(Scope(
            level="project", project_ids=["b", "a", "a"], domain_ids=["y", "x"],
        ))
        assert s.project_ids == ["a", "b"]
        assert s.domain_ids == ["x", "y"]


class TestLifecycle:
    def test_candidate_to_active_allowed(self):
        assert_valid_status_transition("candidate", "active")

    def test_candidate_to_needs_confirmation_allowed(self):
        assert_valid_status_transition("candidate", "needs_confirmation")

    def test_active_to_superseded_allowed(self):
        assert_valid_status_transition("active", "superseded")

    def test_archived_to_active_not_allowed(self):
        with pytest.raises(StatusTransitionError):
            assert_valid_status_transition("archived", "active")


class TestQueryPolicy:
    def test_empty_query_rejected(self):
        with pytest.raises(RecallConstraintError):
            enforce_recall_constraints(
                original_query="   ", original_domains=[],
                original_projects=[], rewrite=None, expansions=[],
            )

    def test_rewrite_with_new_id_rejected(self):
        with pytest.raises(RecallConstraintError):
            enforce_recall_constraints(
                original_query="show me p1 stuff", original_domains=[],
                original_projects=[], expansions=["[proj_other]"],
            )

    def test_plain_rewrite_allowed(self):
        enforce_recall_constraints(
            original_query="show me p1 stuff",
            original_domains=[],
            original_projects=["p1"],
            rewrite="display p1 items",
            expansions=[],
        )