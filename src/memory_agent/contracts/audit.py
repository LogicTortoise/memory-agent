"""Audit query DTOs (T13).

These are the wire shapes the HTTP API serves to the UI. They are
*metadata-only* — sensitive bodies (full event text, full memory
text, full segment bytes) are deliberately not exposed in the list
or overview endpoints. A separate, explicit detail fetch is required
to read raw body content (e.g. ``/batches/{id}/evidence`` and
``/memories/{id}/history``), and even those endpoints redact fields
that are not required to interpret the link.

Every list DTO carries a server-side ``pagination`` block. The
``cursor`` is an opaque, deterministic token bound to the query
parameters and the snapshot id; clients must echo it back unmodified
to fetch the next page. ``as_of`` and ``snapshot_id`` make every DTO
explicitly anchored to a vault state, and ``projection_state``
reports whether the SQL projection is in lock-step with the file
manifest (``aligned``), strictly behind (``db_lag``) or strictly
ahead (``db_ahead`` — only possible when a prepare/publish step
crashed between HEAD swap and SQL finalize).

Sources, batches, and memories trace ``source -> batch -> model call
-> candidate -> commit`` through stored refs (``correlation_json``,
``object_id``, ``artifact_ref``). Tests pin this end-to-end against
an invented pipeline run.
"""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Existing storage-shape audit event (kept for backwards compatibility).
# The T13 query DTOs live below this block.
# ---------------------------------------------------------------------------

AUDIT_SCHEMA_VERSION = 1

AuditOutcome = Literal["success", "failure", "skipped"]
AuditAction = Literal[
    "source.discovered",
    "source.read",
    "source.rewritten",
    "source.error",
    "ingest.segment_committed",
    "batch.planned",
    "batch.claimed",
    "batch.extracted",
    "batch.retried",
    "batch.failed",
    "batch.superseded",
    "memory.proposed",
    "memory.committed",
    "conflict.opened",
    "conflict.decided",
    "query.planned",
    "query.retrieved",
    "recovery.finalized",
    "privacy.deleted",
]


class AuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    audit_id: str
    action: AuditAction
    actor: str
    occurred_at: str
    correlation: dict[str, Any] = Field(default_factory=dict)
    object_ref: dict[str, Any] = Field(default_factory=dict)
    outcome: AuditOutcome
    reason_code: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("schema_version")
    @classmethod
    def _version_ok(cls, v: int) -> int:
        if v != AUDIT_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version={v}; expected {AUDIT_SCHEMA_VERSION}")
        return v


def audit_to_dict(a: AuditEvent) -> dict[str, Any]:
    return a.model_dump(mode="json")


# ---------------------------------------------------------------------------
# T13 audit query DTOs.
#
# These are the wire shapes the HTTP API serves to the UI. They are
# *metadata-only* — sensitive bodies (full event text, full memory
# text, full segment bytes) are deliberately not exposed in the list
# or overview endpoints. A separate, explicit detail fetch is required
# to read raw body content (e.g. ``/batches/{id}/evidence`` and
# ``/memories/{id}/history``), and even those endpoints redact fields
# that are not required to interpret the link.
#
# Every list DTO carries a server-side ``pagination`` block. The
# ``cursor`` is an opaque, deterministic token bound to the query
# parameters and the snapshot id; clients must echo it back unmodified
# to fetch the next page. ``as_of`` and ``snapshot_id`` make every DTO
# explicitly anchored to a vault state, and ``projection_state``
# reports whether the SQL projection is in lock-step with the file
# manifest (``aligned``), strictly behind (``db_lag``) or strictly
# ahead (``db_ahead`` — only possible when a prepare/publish step
# crashed between HEAD swap and SQL finalize).
# ---------------------------------------------------------------------------

AUDIT_QUERY_SCHEMA_VERSION = 1

ProjectionState = Literal["aligned", "db_lag", "db_ahead", "empty"]


class PaginationCursor(BaseModel):
    """Opaque pagination token. The HTTP layer encodes it; clients must
    not parse or construct it themselves.
    """

    model_config = ConfigDict(extra="forbid")

    cursor: str | None = None
    next_cursor: str | None = None
    page_size: int = Field(ge=1, le=200)
    total_estimated: int | None = None


class EnvelopeHeader(BaseModel):
    """Common header for every audit DTO.

    ``as_of`` is the wall-clock time the read was taken; ``snapshot_id``
    is the HEAD commit id the read was anchored to (or ``"empty"`` when
    the vault has no published commits yet).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = AUDIT_QUERY_SCHEMA_VERSION
    as_of: str
    snapshot_id: str
    projection_state: ProjectionState
    db_lag_behind_head: int = Field(default=0, ge=0)
    db_ahead_of_head: int = Field(default=0, ge=0)


class OverviewCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources_total: int = 0
    sources_unsupported: int = 0
    events_total: int = 0
    events_user: int = 0
    turns_total: int = 0
    batches_total: int = 0
    batches_pending: int = 0
    batches_running: int = 0
    batches_succeeded: int = 0
    batches_failed: int = 0
    candidate_sets_total: int = 0
    memories_active: int = 0
    memories_superseded: int = 0
    conflicts_open: int = 0
    audit_events_total: int = 0
    worker_lease_active: bool = False
    last_scan_at: str | None = None
    last_commit_at: str | None = None


class OverviewDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    header: EnvelopeHeader
    counts: OverviewCounts
    pipeline_stages: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class SourceDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_file_id: str
    connector: str
    session_id: str
    branch_id: str
    canonical_path: str
    generation: int
    revision_seq: int
    complete_offset: int
    size: int
    last_status: str | None = None
    last_scan_at: str | None = None
    last_error: str | None = None
    file_present: bool
    # Sensitive: only set when the source row's manifest lag is large
    # enough to warrant surfacing in a list view. The full segment path
    # is *never* included.
    pending_partial_tail: bool = False


class SourceDetailDTO(SourceDTO):
    """Returned only by the explicit detail endpoint. Body content is
    still not included here — ``/sessions/{id}/turns`` and
    ``/batches/{id}/evidence`` are the only paths that surface raw
    text.
    """

    model_config = ConfigDict(extra="forbid")

    adapter_version: str | None = None
    file_identity: str | None = None
    consumed_prefix_sha256: str | None = None
    manifest_reconcile: Literal[
        "ok", "behind", "ahead", "missing_manifest", "unknown"
    ] = "unknown"


class SessionTurnDTO(BaseModel):
    """One turn row in a session.

    The ``quote_preview`` is the first N characters of the user's
    original text. Full text is *never* returned by this DTO — the UI
    has to call a follow-up endpoint (out of scope for v1 P5/P6) to
    expand the body. Tests assert the preview is truncated.
    """

    model_config = ConfigDict(extra="forbid")

    turn_id: str
    session_id: str
    branch_id: str
    ordinal: int
    revision: int
    state: str
    first_event_id: str | None
    last_event_id: str | None
    role: str | None = None
    quote_preview: str | None = None
    quote_redacted: bool = True


class BatchMemberDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_id: str
    purpose: str
    ord: int
    epoch: int
    active_target: bool


class BatchDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str
    session_id: str
    branch_id: str
    policy_version: str
    status: str
    attempt: int
    input_hash: str
    created_at: str | None
    lease_until: str | None = None
    member_count: int = 0
    candidate_set_id: str | None = None
    candidate_set_ref: str | None = None


class BatchDetailDTO(BatchDTO):
    model_config = ConfigDict(extra="forbid")

    members: list[BatchMemberDTO] = Field(default_factory=list)
    run_ids: list[str] = Field(default_factory=list)
    commit_id: str | None = None


class BatchEvidenceDTO(BaseModel):
    """The only audit DTO that surfaces raw quoted text.

    Even here, the *body* is the exact slice persisted in the segment
    file at write time; the locator points at the immutable segment,
    not at the memory markdown. This keeps the UI honest about
    provenance.
    """

    model_config = ConfigDict(extra="forbid")

    batch_id: str
    header: EnvelopeHeader
    inputs: list[dict[str, Any]] = Field(default_factory=list)
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    runs: list[dict[str, Any]] = Field(default_factory=list)
    model_calls: list[dict[str, Any]] = Field(default_factory=list)


class MemoryRevisionDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str
    revision: int
    status: str
    commit_id: str | None
    recorded_at: str | None
    statement: str
    scope_level: str | None
    project_ids: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    evidence_quotes: list[str] = Field(default_factory=list)
    superseded_by_revision: int | None = None


class MemoryListItemDTO(BaseModel):
    """Metadata-only summary for the /memories list view.

    Lighter than ``MemoryRevisionDTO`` — no evidence quotes, no
    superseded_by_revision chain, no nested project / domain ids
    beyond the scope level. The list endpoint surfaces this; the
    detail endpoint /memories/{id}/history still returns the
    full revision chain via ``MemoryHistoryDTO``.
    """
    model_config = ConfigDict(extra="forbid")

    memory_id: str
    revision: int
    status: str
    commit_id: str | None
    recorded_at: str | None
    statement: str
    scope_level: str | None


class JobListItemDTO(BaseModel):
    """Metadata-only summary for the /jobs list view.

    Excludes progress / result / error bodies (those grow with
    each batch and would balloon the list response). The
    operator-facing UI can still poll ``GET /api/v1/jobs/{id}``
    for the full job state.
    """
    model_config = ConfigDict(extra="forbid")

    job_id: str
    request_id: str
    client_id: str
    operation: str
    state: str
    revision: int
    mode: str
    cancel_requested: bool
    created_at: str | None
    started_at: str | None
    ended_at: str | None
    last_heartbeat: str | None


class MemoryHistoryDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    header: EnvelopeHeader
    memory_id: str
    revisions: list[MemoryRevisionDTO] = Field(default_factory=list)
    source_corrections: list[dict[str, Any]] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)


class ConflictDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conflict_id: str
    revision: int
    status: str
    commit_id: str | None
    opened_at: str | None
    old_memory_ref: str | None
    new_memory_ref: str | None
    candidate_ref: str | None = None


class AuditEventDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_id: str
    seq: int
    occurred_at: str
    actor: str
    action: str
    object_type: str | None
    object_id: str | None
    correlation: dict[str, Any] = Field(default_factory=dict)
    before_after: dict[str, Any] = Field(default_factory=dict)
    outcome: str
    reason_code: str | None
    # Detailed only: ``details`` may include free-form payloads. The
    # list endpoint returns a sanitised subset that strips known
    # sensitive fields (e.g. ``quote``, ``text``, ``prompt``).
    details: dict[str, Any] = Field(default_factory=dict)
    redacted: bool = True


class QueryTraceDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str
    snapshot_id: str
    retrieval: str
    original_query: str | None = None
    accepted_variants: list[str] = Field(default_factory=list)
    rejected_variants: list[str] = Field(default_factory=list)
    fallback_reason: str | None = None
    result_count: int = 0
    occurred_at: str | None = None


class QueryTraceDetailDTO(QueryTraceDTO):
    """Returned only by the explicit detail endpoint; surfaces the
    raw variants and provider name.
    """

    model_config = ConfigDict(extra="forbid")

    fingerprint: str | None = None
    hard_filters: dict[str, Any] = Field(default_factory=dict)
    rewrite: str | None = None
    expansions: list[str] = Field(default_factory=list)
    variants_full: list[dict[str, Any]] = Field(default_factory=list)
    rejected_full: list[dict[str, Any]] = Field(default_factory=list)
    provider: str | None = None
    prompt_version: str | None = None
    warnings: list[str] = Field(default_factory=list)
    artifact_ref: str | None = None


class PagedResponse(BaseModel):
    """Generic paginated envelope shared by every list endpoint."""

    model_config = ConfigDict(extra="forbid")

    header: EnvelopeHeader
    items: list[Any] = Field(default_factory=list)
    pagination: PaginationCursor