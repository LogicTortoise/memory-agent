"""Recall contracts: QueryPlan, RecallItem, RecallResult, QueryTrace.

v1 extends the v0.1 baseline with:
  * ``retrieval`` (keyword | bm25) and ``rewrite`` / ``expand`` (off |
    auto) on the request, with strict unknown-field rejection
  * provenance on each non-original ``QueryVariant``
  * ``cursor`` for honest pagination, bound to (fingerprint, snapshot)
  * truncated / next_cursor on the result, with explicit
    ``required_constraints_complete`` semantics

BM25 is **only** used when ``retrieval=bm25`` is passed on an explicit
recall request. BM25 is **never** invoked from scan, batch, reconcile,
maintain, or any non-recall path.
"""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

QUERY_SCHEMA_VERSION = 1

RetrievalKind = Literal["keyword", "bm25"]
RewritePolicy = Literal["off", "auto"]
ExpandPolicy = Literal["off", "auto"]


class QueryVariant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variant_id: str
    origin: Literal["original", "rewrite", "expansion"]
    text: str
    accepted: bool
    reason: str | None = None
    # Provenance is required for any non-original variant; the original
    # query always has provider="user" and prompt_version="original".
    provider: str = "user"
    prompt_version: str = "original"


class QueryPlan(BaseModel):
    """Plan emitted by the query pipeline before any retrieval.

    v1 invariants:

      * ``original_query`` is preserved verbatim.
      * ``rewrite`` is at most one (or absent).
      * ``expansions`` has at most two entries (or empty).
      * ``exact_identifiers`` are preserved exactly (versions, paths, IDs,
        numbers); ``negations`` are preserved as substrings so the
        retrieval layer cannot lose them silently.
      * ``hard_filters`` are the project/domain/time filters the user
        actually supplied — never derived from rewrite/expansion.
    """

    model_config = ConfigDict(extra="forbid")

    original_query: str
    hard_filters: dict[str, Any] = Field(default_factory=dict)
    exact_identifiers: list[str] = Field(default_factory=list)
    negations: list[str] = Field(default_factory=list)
    rewrite: str | None = None
    expansions: list[str] = Field(default_factory=list)
    variants: list[QueryVariant] = Field(default_factory=list)

    @field_validator("rewrite")
    @classmethod
    def _single_rewrite(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("rewrite must be empty string or None, not whitespace")
        return v

    @field_validator("expansions")
    @classmethod
    def _two_or_fewer(cls, v: list[str]) -> list[str]:
        if len(v) > 2:
            raise ValueError("at most 2 expansions allowed in v1")
        return v


class RecallItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str
    revision: int
    statement: str
    score: float
    quote: str
    locator: dict[str, Any]
    scope: dict[str, Any]
    # v1: record which variants surfaced this item and the BM25 score
    # contribution per variant. The variant_id list is the persisted
    # trace for the item; ``null`` means surfaced by keyword only.
    surfaced_by: list[str] = Field(default_factory=list)
    bm25_contribution: float = 0.0
    keyword_contribution: float = 0.0


class QueryTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str
    # fingerprint binds to (original_query, hard_filters, retrieval,
    # variants). Any change makes the cursor stale.
    fingerprint: str
    variants: list[QueryVariant]
    snapshot_id: str
    # Rejected variants are recorded so the UI can show why a
    # rewrite/expansion was dropped.
    rejected: list[QueryVariant] = Field(default_factory=list)
    # Machine-readable reason for fallback to original-only retrieval.
    fallback_reason: str | None = None
    retrieval: RetrievalKind = "keyword"


class RecallResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    snapshot_id: str
    query_trace: QueryTrace
    items: list[RecallItem]
    truncated: bool = False
    next_cursor: str | None = None
    # ``required_constraints_complete`` is False if the result was
    # truncated before all hard-filter constraints could be satisfied.
    required_constraints_complete: bool = True
    warnings: list[str] = Field(default_factory=list)
    # ``stale_cursor_rejected`` is set when the caller passed a cursor
    # whose fingerprint or snapshot no longer matches the current HEAD.
    stale_cursor_rejected: bool = False

    @field_validator("schema_version")
    @classmethod
    def _version_ok(cls, v: int) -> int:
        if v != QUERY_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version={v}; expected {QUERY_SCHEMA_VERSION}")
        return v


def recall_to_dict(r: RecallResult) -> dict[str, Any]:
    return r.model_dump(mode="json")


def recall_from_dict(data: dict[str, Any]) -> RecallResult:
    return RecallResult.model_validate(data)
