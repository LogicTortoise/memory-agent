"""Quote/span evidence validation.

A piece of evidence is *only* valid when its quoted text is exactly the
Unicode code-point slice of the source text identified by (span_start,
span_end). We never normalise, lowercase, or trim the source text.
"""

from __future__ import annotations

from typing import Iterable


class EvidenceValidationError(ValueError):
    """Raised when an EvidenceRef does not exactly match its source slice."""


def slice_unicode(text: str, start: int, end: int) -> str:
    """Return text[start:end] using Unicode code points.

    Pydantic already guarantees non-negative ints; we additionally reject
    out-of-range slices here because Pydantic can't enforce that against
    the actual text.
    """
    if start < 0 or end < 0:
        raise EvidenceValidationError(f"negative slice: start={start} end={end}")
    if end < start:
        raise EvidenceValidationError(f"end < start: start={start} end={end}")
    if end > len(text):
        raise EvidenceValidationError(
            f"end={end} exceeds text length {len(text)}"
        )
    return text[start:end]


def assert_quote_matches_slice(
    *,
    quote: str,
    source_text: str,
    span_start: int,
    span_end: int,
    role: str,
    origin: str,
) -> None:
    """Raise EvidenceValidationError unless quote == source_text[start:end]."""
    if role != "user":
        # v0.1 only commits memory from explicit user evidence.
        raise EvidenceValidationError(
            f"role={role!r} is not user; auto-commit refuses non-user evidence"
        )
    if origin not in {"user_session", "replayed_history"}:
        raise EvidenceValidationError(
            f"origin={origin!r} cannot become user memory"
        )
    actual = slice_unicode(source_text, span_start, span_end)
    if actual != quote:
        raise EvidenceValidationError(
            f"quote mismatch: expected {quote!r}, source slice is {actual!r}"
        )


def validate_evidence_list(
    *,
    evidence: Iterable,
    source_text_lookup,
) -> None:
    """Validate a list of EvidenceRef against a callable returning source text."""
    for ev in evidence:
        text = source_text_lookup(ev.source_event_id, ev.source_revision_hash)
        assert_quote_matches_slice(
            quote=ev.quote,
            source_text=text,
            span_start=ev.span_start,
            span_end=ev.span_end,
            role=ev.role,
            origin=ev.origin,
        )