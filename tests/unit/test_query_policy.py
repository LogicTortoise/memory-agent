"""Unit tests for the recall-side constraint enforcement."""

from __future__ import annotations

import pytest

from memory_agent.domain.query_policy import (
    RecallConstraintError,
    enforce_recall_constraints,
    extract_exact_identifiers,
    extract_negations,
    validate_plan_filters,
)


class TestExactIdentifierExtraction:
    def test_extracts_semver(self) -> None:
        ids = extract_exact_identifiers("upgrade to 1.2.3 today")
        assert "1.2.3" in ids

    def test_extracts_path(self) -> None:
        ids = extract_exact_identifiers("see /Users/foo/bar for context")
        assert any("Users/foo/bar" in i for i in ids)

    def test_extracts_email_or_npm(self) -> None:
        ids = extract_exact_identifiers("use pkg@1.0.0")
        assert "pkg@1.0.0" in ids

    def test_no_identifiers(self) -> None:
        ids = extract_exact_identifiers("just a regular question")
        assert ids == []


class TestNegationExtraction:
    def test_english_negation(self) -> None:
        negs = extract_negations("do not include image gen settings")
        assert any("not " in n.lower() for n in negs)

    def test_chinese_negation(self) -> None:
        negs = extract_negations("不要图片生图的设置")
        assert any("不要" in n for n in negs)

    def test_no_negation(self) -> None:
        assert extract_negations("everything is fine") == []


class TestEnforceRecallConstraints:
    def test_empty_query_rejected(self) -> None:
        with pytest.raises(RecallConstraintError):
            enforce_recall_constraints(
                original_query="   ",
                original_domains=[],
                original_projects=[],
            )

    def test_rewrite_adding_project_id_rejected(self) -> None:
        with pytest.raises(RecallConstraintError):
            enforce_recall_constraints(
                original_query="what do I want",
                original_domains=[],
                original_projects=[],
                rewrite="show proj_xyz memory",
            )

    def test_expansion_adding_domain_id_rejected(self) -> None:
        with pytest.raises(RecallConstraintError):
            enforce_recall_constraints(
                original_query="what do I want",
                original_domains=[],
                original_projects=[],
                expansions=["dom_abc rules apply"],
            )

    def test_passes_when_filters_match(self) -> None:
        enforce_recall_constraints(
            original_query="what does proj_foo want",
            original_domains=[],
            original_projects=["proj_foo"],
            rewrite="what does proj_foo prefer",
        )


class TestValidatePlanFilters:
    def test_drops_negation(self) -> None:
        negs = extract_negations("do not include image gen")
        with pytest.raises(RecallConstraintError):
            validate_plan_filters(
                plan_text="include image gen",
                original_query="do not include image gen",
                original_projects=[],
                original_domains=[],
                negations=negs,
            )

    def test_preserves_negation(self) -> None:
        negs = extract_negations("不要图片生图的设置")
        validate_plan_filters(
            plan_text="查询时不要图片生图",
            original_query="不要图片生图的设置",
            original_projects=[],
            original_domains=[],
            negations=negs,
        )

    def test_introduces_date_constraint(self) -> None:
        with pytest.raises(RecallConstraintError):
            validate_plan_filters(
                plan_text="memories from 2024 onwards",
                original_query="what did I say last week",
                original_projects=[],
                original_domains=[],
                negations=[],
            )

    def test_passes_when_filters_match(self) -> None:
        validate_plan_filters(
            plan_text="what did I say in 2024",
            original_query="what did I say in 2024",
            original_projects=[],
            original_domains=[],
            negations=[],
        )
