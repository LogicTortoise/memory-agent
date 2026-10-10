"""Recall-side constraint enforcement.

The recall API is the only path that can broaden scope. Even there, we
hard-refuse a rewrite/expansion that introduces a project, domain, or
time filter the user did not already explicitly include. This module
runs in-process at recall time, before any retrieval.

v1 adds:
  * ``extract_negations`` / ``extract_exact_identifiers`` so the planner
    can keep negation / exact-ID substrings in the plan (so retrieval
    cannot silently drop them).
  * ``validate_plan_filters`` — refuses a plan that:
      - mentions a project ID the user did not supply,
      - mentions a domain ID the user did not supply,
      - drops a negation the user originally expressed,
      - introduces a date constraint the user did not supply.
"""

from __future__ import annotations

import re


class RecallConstraintError(ValueError):
    """Raised when a recall rewrite/expansion would silently broaden the query."""


# A "version-ish" identifier: must contain at least one of the
# distinguishing characters (. @ / - followed by alnum, or a digit
# inside). This keeps common English words ("question", "video") out
# of the exact-id list while keeping versions, paths, emails and
# Git SHAs.
_IDENT_RE = re.compile(
    r"(?:"  # group: distinguishing chars required somewhere
    r"\d+(?:\.\d+)+(?:[-+][\w.]+)?"  # 1.2 / 1.2.3 / 1.2.3-rc1
    r"|[A-Za-z0-9_]+@[A-Za-z0-9_.-]+"  # name@host (path-like / npm)
    r"|/?[A-Za-z0-9_.+-]+/[A-Za-z0-9_.+/-]+"  # /Users/foo/bar
    r"|[A-Za-z0-9_-]*\d[A-Za-z0-9_-]{7,}"  # opaque ID with at least one digit
    r")"
)

# Negation cue words in English + Chinese. The planner captures any
# negation cue phrase the user actually wrote so retrieval can refuse
# variants that lose it.
_NEGATION_TOKENS = (
    "no ", "not ", "never ", "don't ", "do not ", "without ",
    "不要", "不要的", "不要把", "禁止", "禁止的", "不", "无", "没有",
    "避免", "避免的", "排除",
)


def extract_exact_identifiers(query: str) -> list[str]:
    """Return ordered, unique identifiers / versions / paths found in
    ``query``. Used by the planner so retrieval cannot drop them.
    """
    seen: list[str] = []
    for m in _IDENT_RE.finditer(query or ""):
        s = m.group(0)
        if s not in seen:
            seen.append(s)
    return seen


def extract_negations(query: str) -> list[str]:
    """Return negation substrings from ``query``.

    Returns the *literal substring* around each negation cue so the
    retrieval layer can verify a variant preserves it verbatim.
    """
    if not query:
        return []
    out: list[str] = []
    lowered = query.lower()
    for cue in _NEGATION_TOKENS:
        cue_lower = cue.lower()
        start = 0
        while True:
            i = lowered.find(cue_lower, start)
            if i == -1:
                break
            # Capture up to 12 chars after the cue, word-bounded.
            tail = query[i:i + 1 + len(cue) + 12].strip()
            if tail and tail not in out:
                out.append(tail)
            start = i + len(cue)
    return out


def enforce_recall_constraints(
    *,
    original_query: str,
    original_domains: list[str],
    original_projects: list[str],
    rewrite: str | None = None,
    expansions: list[str] | None = None,
) -> None:
    """Hard-refuse rewrite/expansion if it adds new hard filters.

    The hard rules for v1 (and v0.1):
      * the original query must remain non-empty
      * rewrite/expansions may not introduce project/domain IDs the
        caller didn't pass in
    """
    expansions = expansions or []
    if not original_query.strip():
        raise RecallConstraintError("original_query is empty")
    if rewrite is not None:
        _ensure_no_new_filters(rewrite, original_query, original_projects, original_domains)
    for exp in expansions:
        _ensure_no_new_filters(exp, original_query, original_projects, original_domains)


def validate_plan_filters(
    *,
    plan_text: str,
    original_query: str,
    original_projects: list[str],
    original_domains: list[str],
    negations: list[str],
) -> None:
    """Reject a plan variant that:

      * introduces project/domain IDs the caller did not supply,
      * silently drops a negation the user originally expressed,
      * introduces a hard date constraint (year / "after YYYY" / "before YYYY")
        the caller did not supply.
    """
    if not plan_text:
        raise RecallConstraintError("plan_text is empty")
    _ensure_no_new_filters(plan_text, original_query, original_projects, original_domains)
    if not _preserves_negations(plan_text, negations):
        raise RecallConstraintError(
            "plan drops a negation the user originally expressed"
        )
    if _introduces_date_constraint(plan_text, original_query):
        raise RecallConstraintError(
            "plan introduces a date constraint the user did not supply"
        )


def _ensure_no_new_filters(
    candidate: str,
    original_query: str,
    original_projects: list[str],
    original_domains: list[str],
) -> None:
    """Conservative guard: the candidate may not mention IDs the caller
    didn't already include."""
    candidate_ids = _extract_bracketed(candidate) | _extract_proj_dom(candidate)
    original_ids = (
        _extract_bracketed(original_query)
        | set(original_projects)
        | set(original_domains)
    )
    added = candidate_ids - original_ids
    if added:
        raise RecallConstraintError(
            f"recall candidate introduces new identifiers {sorted(added)} "
            "not present in original query or filters"
        )


def _preserves_negations(plan_text: str, negations: list[str]) -> bool:
    """Return True iff every negation substring still appears verbatim
    in ``plan_text`` (or has been explicitly removed by the user, which
    this function does not consider — removal is still a hard fail)."""
    if not negations:
        return True
    plan_lower = plan_text.lower()
    for neg in negations:
        # We compare on the cue phrase only, not the tail. The planner
        # keeps the *full* substring; a plan that keeps the cue but
        # drops the noun is still considered to have lost the negation.
        cue = _first_negation_cue(neg)
        if not cue:
            continue
        if cue.lower() not in plan_lower:
            return False
    return True


def _first_negation_cue(negation_substring: str) -> str:
    for cue in _NEGATION_TOKENS:
        if cue.lower() in negation_substring.lower():
            return cue
    return ""


_DATE_PATTERNS = (
    re.compile(r"\b(19|20)\d{2}\b"),  # any 4-digit year 1900-2099
    re.compile(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b"),  # 2026-10-08
    re.compile(r"\b(?:after|before|since|until|截止|在|之后|之前)\s+(19|20)\d{2}\b", re.I),
)


def _introduces_date_constraint(plan_text: str, original_query: str) -> bool:
    """True iff ``plan_text`` mentions a date the original query did not."""
    plan_dates = _dates_in(plan_text)
    orig_dates = _dates_in(original_query)
    return bool(plan_dates - orig_dates)


def _dates_in(s: str) -> set[str]:
    if not s:
        return set()
    out: set[str] = set()
    for pat in _DATE_PATTERNS:
        for m in pat.finditer(s):
            out.add(m.group(0))
    return out


def _extract_bracketed(s: str) -> set[str]:
    """Extract identifiers like proj_xxx or dom_xxx that appear in []."""
    out: set[str] = set()
    cur = []
    depth = 0
    for ch in s:
        if ch == "[":
            depth += 1
            cur.append("")
            continue
        if ch == "]":
            if cur:
                out.add(cur[-1].strip())
                cur.pop()
            depth = max(0, depth - 1)
            continue
        if depth and cur:
            cur[-1] += ch
    return {x for x in out if x}


def _extract_proj_dom(s: str) -> set[str]:
    """Extract proj_xxx / dom_xxx identifiers wherever they appear."""
    out: set[str] = set()
    for m in re.finditer(r"\b(proj|dom)_[A-Za-z0-9_]+\b", s or ""):
        out.add(m.group(0))
    return out
