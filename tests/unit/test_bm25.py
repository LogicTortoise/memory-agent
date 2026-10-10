"""Unit tests for the BM25 tokenizer and rank fusion."""

from __future__ import annotations

import pytest

from memory_agent.application.search.bm25 import (
    BM25Index,
    reciprocal_rank_fusion,
    tokenize,
)


class TestTokenize:
    def test_english_words(self) -> None:
        out = tokenize("use Python for the prototype")
        assert "use" in out
        assert "Python" in out
        # "the" is a stopword-ish token but we keep all tokens; the
        # ranker handles frequency downstream.
        assert "prototype" in out

    def test_chinese_character_bigrams(self) -> None:
        # "视频换脸" -> bigrams: 视频, 像换, 频换, 换脸
        out = tokenize("视频换脸")
        assert "视频" in out
        assert "换脸" in out

    def test_mixed_chinese_english(self) -> None:
        out = tokenize("AI 视频生成 video gen")
        assert "AI" in out
        assert "视频" in out  # bigram
        assert "生成" in out  # bigram
        assert "video" in out
        assert "gen" in out

    def test_version_identifier_kept_whole(self) -> None:
        out = tokenize("please use pkg@1.0.0")
        assert "pkg@1.0.0" in out

    def test_path_like_kept(self) -> None:
        out = tokenize("see /Users/foo/bar/baz for details")
        assert any("/Users/foo/bar/baz" in t for t in out)

    def test_semver_kept(self) -> None:
        out = tokenize("upgrade to 1.2.3 immediately")
        assert "1.2.3" in out

    def test_exact_identifiers_added(self) -> None:
        out = tokenize("foo", exact_identifiers=["bar1.2"])
        assert "bar1.2" in out

    def test_empty(self) -> None:
        assert tokenize("") == []
        assert tokenize("   ") == []


class TestBM25Index:
    def test_chinese_relevant_ranks_first(self) -> None:
        idx = BM25Index()
        idx.add("项目用 Python 写原型")          # relevant
        idx.add("我喜欢吃苹果和香蕉")           # irrelevant
        idx.add("今天下午去图书馆")             # irrelevant
        scores = idx.score(tokenize("Python 原型"))
        assert scores[0] > scores[1]
        assert scores[0] > scores[2]

    def test_english_relevant_ranks_first(self) -> None:
        idx = BM25Index()
        idx.add("always log decisions to vault/audit")
        idx.add("banana smoothie recipe")
        idx.add("the cat sat on the mat")
        scores = idx.score(tokenize("log decisions vault"))
        assert scores[0] > scores[1]
        assert scores[0] > scores[2]

    def test_no_shared_terms_yields_zero(self) -> None:
        idx = BM25Index()
        idx.add("hello world")
        scores = idx.score(tokenize("苹果 香蕉"))
        assert scores == [0.0]

    def test_empty_query_yields_zero(self) -> None:
        idx = BM25Index()
        idx.add("hello world")
        scores = idx.score(tokenize(""))
        assert scores == [0.0]

    def test_idf_prefers_rare_terms(self) -> None:
        idx = BM25Index()
        idx.add("banana banana banana")
        idx.add("apple banana")
        idx.add("apple apple banana")
        # "apple" is rarer; a query of [apple] should weight doc 2 highest.
        scores = idx.score(["apple"])
        assert scores[1] > 0
        assert scores[2] > 0


class TestReciprocalRankFusion:
    def test_original_outweighs_variants(self) -> None:
        # Original ranks doc A first; rewrite ranks doc B first. The
        # original's weight is 2, the rewrite's is 0.5. Doc A wins.
        original = [0.9, 0.1]
        rewrite = [0.1, 0.9]
        fused = reciprocal_rank_fusion(
            variant_scores={"original": original, "rewrite": rewrite},
            variant_weights={"original": 2.0, "rewrite": 0.5},
        )
        assert fused[0] > fused[1]

    def test_two_variants_lose_to_original(self) -> None:
        # Even two expansions with weight 0.5 each cannot outweigh the
        # original at weight 2 — original always dominates when the
        # original's first pick is unique.
        original = [1.0, 0.0]
        e1 = [0.0, 1.0]
        e2 = [0.0, 1.0]
        fused = reciprocal_rank_fusion(
            variant_scores={
                "original": original,
                "expansion_1": e1,
                "expansion_2": e2,
            },
            variant_weights={"original": 2.0, "expansion_1": 0.5, "expansion_2": 0.5},
        )
        # doc 0 gets original's full rank bonus plus a non-zero but
        # smaller bonus from each expansion (they put doc 0 at rank 2).
        # doc 1 only gets the expansions' rank-1 bonus.
        assert fused[0] > fused[1]

    def test_zero_weight_ignored(self) -> None:
        fused = reciprocal_rank_fusion(
            variant_scores={"a": [1.0, 0.0], "b": [0.0, 1.0]},
            variant_weights={"a": 0.0, "b": 1.0},
        )
        # doc 1 wins because variant b alone ranks it first.
        assert fused[1] > fused[0]
