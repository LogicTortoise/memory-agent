"""Memory status lifecycle rules."""

from __future__ import annotations

from typing import Iterable


class StatusTransitionError(ValueError):
    """Raised when an attempted status transition is not allowed."""


# Allowed transitions (current -> set of allowed next states).
# v0.1 is conservative: only paths the project actually exercises are allowed.
_ALLOWED: dict[str, set[str]] = {
    "candidate": {"needs_confirmation", "active", "rejected"},
    "needs_confirmation": {"active", "rejected"},
    "active": {"superseded", "expired", "archived"},
    "rejected": set(),
    "superseded": {"archived"},
    "expired": {"archived"},
    "archived": set(),
}


def assert_valid_status_transition(current: str, next_state: str) -> None:
    """Raise StatusTransitionError when the transition is not in the allowed map."""
    if current == next_state:
        return  # idempotent
    allowed = _ALLOWED.get(current, set())
    if next_state not in allowed:
        raise StatusTransitionError(
            f"cannot transition memory status {current!r} -> {next_state!r}; "
            f"allowed next states: {sorted(allowed) or 'none'}"
        )


def allowed_next_states(current: str) -> Iterable[str]:
    return tuple(_ALLOWED.get(current, set()))