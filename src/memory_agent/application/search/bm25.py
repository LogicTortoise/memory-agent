"""In-memory BM25 ranking for recall.

This module is **only** loaded by the recall path. It is never imported
by ``scan``, ``plan_batches``, ``process_batches``, ``commit``,
``reconcile``, or ``maintain``. The v1 invariant is enforced by tests
in ``tests/unit/test_bm25_isolation.py`` which use AST / import
inspection.

Tokenisation
------------
* Chinese is split into character bigrams; ASCII-letter runs inside a
  Chinese phrase are also emitted as English word tokens so
  mixed-language fixtures still rank well.
* English / ASCII text is split on whitespace and a small set of
  punctuation; we keep version-ish identifiers and ``/``-separated path
  fragments whole (so 1.2.3, /Users/foo/bar, pkg@1.0.0 stay exact).
* ``exact_identifiers`` passed in by the planner are emitted verbatim
  so they always participate in the ranking corpus.

Rank fusion
-----------
* Per-variant BM25 scores are combined using reciprocal rank fusion
  ``Σ w_i / (60 + rank_i)``. The original query's weight is **2**, and
  the sum of all rewrite/expansion weights is at most **1**, so the
  original always outweighs the variants together. The keyword
  contribution is added on top with weight **1** so a memory that
  matches the original query by keyword still ranks first when BM25
  reorders it lower.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable

# ----------------------------- tokenisation -----------------------------

# Two-pattern approach: words are emitted broadly (so "Python" and
# "video" are tokens), and special identifiers (versions, paths,
# emails) are emitted as a single whole token so they don't fragment.
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_SPECIAL_RE = re.compile(
    r"\d+(?:\.\d+)+(?:[-+][\w.]+)?"           # 1.2 / 1.2.3 / 1.2.3-rc1
    r"|[A-Za-z0-9_]+@[A-Za-z0-9_.-]+"         # name@host
    r"|/?[A-Za-z0-9_.+-]+/[A-Za-z0-9_.+/-]+"  # path-like
)

# Allowed separators between Chinese characters when emitting bigrams.
_CN_RANGE = (0x4E00, 0x9FFF)  # CJK Unified Ideographs


def _is_cjk(ch: str) -> bool:
    if not ch:
        return False
    cp = ord(ch)
    return _CN_RANGE[0] <= cp <= _CN_RANGE[1]


def tokenize(text: str, *, exact_identifiers: Iterable[str] = ()) -> list[str]:
    """Return the token list for ``text``.

    English / ASCII tokens are split on ``_TOKEN_RE``. Chinese text is
    emitted as character bigrams. ``exact_identifiers`` are added
    verbatim so the ranking corpus retains them even when the regex
    would not catch them in some pathological input.
    """
    if not text:
        return []
    out: list[str] = []
    # Special identifiers (versions, paths, emails) — emit whole.
    for m in _SPECIAL_RE.finditer(text):
        t = m.group(0)
        if t and t not in out:
            out.append(t)
    # English / ASCII word tokens.
    for m in _WORD_RE.finditer(text):
        t = m.group(0)
        if t:
            out.append(t)
    # Chinese character bigrams (skip whitespace, keep runs contiguous).
    i = 0
    while i < len(text):
        ch = text[i]
        if _is_cjk(ch):
            j = i + 1
            while j < len(text) and _is_cjk(text[j]):
                j += 1
            # Emit bigrams over the contiguous CJK run.
            run = text[i:j]
            for k in range(len(run) - 1):
                out.append(run[k:k + 2])
            i = j
        else:
            i += 1
    for ident in exact_identifiers:
        if ident and ident not in out:
            out.append(ident)
    return out


# ----------------------------- corpus + BM25 -----------------------------


@dataclass
class BM25Index:
    """In-memory BM25 index.

    Parameters are tuned for short, curated memory corpora
    (``k1=1.2, b=0.75``). ``tokenizer_version`` is recorded so a
    persisted cache key can include it.
    """

    k1: float = 1.2
    b: float = 0.75
    tokenizer_version: str = "cn-bigram+en-v1"
    _doc_tokens: list[list[str]] = field(default_factory=list)
    _doc_lens: list[int] = field(default_factory=list)
    _avg_doc_len: float = 0.0
    _df: dict[str, int] = field(default_factory=dict)
    _n_docs: int = 0

    def add(self, doc_text: str, *, exact_identifiers: Iterable[str] = ()) -> None:
        tokens = tokenize(doc_text, exact_identifiers=exact_identifiers)
        self._doc_tokens.append(tokens)
        self._doc_lens.append(len(tokens))
        for tok in set(tokens):
            self._df[tok] = self._df.get(tok, 0) + 1
        self._n_docs += 1
        self._avg_doc_len = sum(self._doc_lens) / max(1, self._n_docs)

    def doc_count(self) -> int:
        return self._n_docs

    def score(self, query_tokens: list[str]) -> list[float]:
        """Return a BM25 score per document, in document order."""
        scores = [0.0] * self._n_docs
        if not query_tokens or self._n_docs == 0:
            return scores
        for q in query_tokens:
            df = self._df.get(q, 0)
            if df == 0:
                continue
            idf = math.log(1 + (self._n_docs - df + 0.5) / (df + 0.5))
            for i, doc in enumerate(self._doc_tokens):
                tf = doc.count(q)
                if tf == 0:
                    continue
                dl = self._doc_lens[i]
                denom = tf + self.k1 * (1 - self.b + self.b * dl / max(1e-9, self._avg_doc_len))
                scores[i] += idf * (tf * (self.k1 + 1)) / max(1e-9, denom)
        return scores


# ----------------------------- ranking fusion -----------------------------


def reciprocal_rank_fusion(
    *,
    variant_scores: dict[str, list[float]],
    variant_weights: dict[str, float],
) -> list[float]:
    """Combine per-variant scores via reciprocal rank fusion.

    ``variant_scores`` maps variant_id → per-document score list (in
    document order). ``variant_weights`` maps variant_id → weight. The
    caller is responsible for ensuring the original variant's weight is
    greater than the sum of every other variant's weight.
    """
    n = 0
    for scores in variant_scores.values():
        n = max(n, len(scores))
    fused = [0.0] * n
    for vid, scores in variant_scores.items():
        w = float(variant_weights.get(vid, 0.0))
        if w <= 0 or not scores:
            continue
        # Sort indices by score descending; stable for tie-breaking.
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        for rank, idx in enumerate(order):
            if scores[idx] == 0:
                continue
            fused[idx] += w / (60 + rank + 1)
    return fused
