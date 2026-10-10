"""Scope intersection and containment rules.

Scope is *intersection across facets*, not a flat priority list. We
explicitly avoid "more specific wins" or "newest wins" semantics — those
are explicitly disallowed by the project design.
"""

from __future__ import annotations

from typing import Iterable

from memory_agent.contracts.candidates import Scope as _Scope

Scope = _Scope


def normalize_scope(scope: Scope) -> Scope:
    """Deduplicate and sort scope IDs.

    Does NOT collapse across levels; that would change semantics.
    """
    return Scope(
        level=scope.level,
        project_ids=sorted(set(scope.project_ids)),
        domain_ids=sorted(set(scope.domain_ids)),
        task_ids=sorted(set(scope.task_ids)),
        conditions=sorted(set(scope.conditions)),
        excludes=sorted(set(scope.excludes)),
    )


def scope_contains_project(scope: Scope, project_id: str) -> bool:
    """A 'global' scope matches everything; the rest must include project_id."""
    if scope.level == "global":
        return True
    if scope.level == "project":
        return project_id in scope.project_ids
    # For domain/task/session scopes, project containment is unknown — caller
    # must decide. We return False so "unknown" does not become True.
    return True


def scopes_intersect(a: Scope, b: Scope) -> bool:
    """Conservative intersection: True only when there is non-empty overlap.

    'unknown' is *not* automatically treated as intersect; we err on the
    side of caution and return False when neither side declares enough
    information.
    """
    if a.level == "global" or b.level == "global":
        # Global intersects with everything; but excludes still apply.
        return _excludes_compatible(a, b)

    if a.level == b.level:
        # Same level: explicit IDs must intersect (empty -> unknown).
        if a.level == "project":
            return _any_in_common(a.project_ids, b.project_ids) and _excludes_compatible(a, b)
        if a.level == "domain":
            return _any_in_common(a.domain_ids, b.domain_ids) and _excludes_compatible(a, b)
        if a.level == "task":
            return _any_in_common(a.task_ids, b.task_ids) and _excludes_compatible(a, b)
        if a.level == "session":
            return _excludes_compatible(a, b)
    # Mixed levels: only intersect if explicit ID sets overlap.
    # Otherwise: unknown — return False so the caller asks the user.
    if (
        _any_in_common(a.project_ids, b.project_ids)
        or _any_in_common(a.domain_ids, b.domain_ids)
        or _any_in_common(a.task_ids, b.task_ids)
    ):
        return _excludes_compatible(a, b)
    return False


def _any_in_common(a: Iterable[str], b: Iterable[str]) -> bool:
    sa, sb = set(a), set(b)
    return bool(sa & sb)


def _excludes_compatible(a: Scope, b: Scope) -> bool:
    """If either side excludes an ID the other side positively asserts,
    the two scopes cannot be considered compatible. Conservative.

    Excludes against a 'global' scope means 'exclude this ID from being
    treated as global'. We therefore refuse intersection when global's
    excludes include something the other side asserts.
    """
    a_pos = set(a.project_ids) | set(a.domain_ids) | set(a.task_ids)
    b_pos = set(b.project_ids) | set(b.domain_ids) | set(b.task_ids)
    a_excl = set(a.excludes)
    b_excl = set(b.excludes)
    if a_excl & b_pos or b_excl & a_pos:
        return False
    return True