"""Versioned CLI request/response contracts.

Phase C extends the v0.1/v1-phase-B envelope with the discriminated
``CommandRequest`` used by ``memory call --request-file - --json``:

  * ``mode="sync"`` runs the operation inline and returns the result
    directly on stdout (no worker required).
  * ``mode="async"`` durably persists the request as a job (so the
    worker can claim it later) and returns ``status="queued"`` plus a
    ``job_id``. The same operation queue and worker lease state are
    used for the optional ``review`` and explicit ``recall`` paths.
  * ``(client_id, idempotency_key)`` is bound to a canonical
    ``request_hash``. Same key + same params => existing job returned.
    Same key + changed params => ``IDEMPOTENCY_CONFLICT`` (exit 4).
  * Unknown fields on the envelope or on any operation params block
    are rejected with ``INVALID_REQUEST``.

The envelope keeps the v0.1 keys so existing ``init`` / ``scan`` /
``batch.plan`` / ``batch.process`` / ``maintain`` / ``recall`` /
``conflicts`` / ``decide`` / ``doctor`` CLI flags remain valid.
"""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, RootModel

COMMAND_API_VERSION = 1

OperationKind = Literal[
    "init",
    "scan",
    "batch.plan",
    "batch.process",
    "maintain",
    "recall",
    "conflicts",
    "decide",
    "doctor",
    "review",
    "capture",
    "conflict.decide",
    "source_correction.decide",
    "job.cancel",
    "job.retry",
    "job.status",
    "privacy.forget",
    "privacy.list",
    "privacy.receipt",
    "privacy.unblock",
    "backup.create",
    "backup.list",
    "backup.restore",
]

ErrorCode = Literal[
    "INVALID_REQUEST",
    "INVALID_VERSION",
    "UNKNOWN_OPERATION",
    "IDEMPOTENCY_CONFLICT",
    "SOURCE_UNAVAILABLE",
    "PROVIDER_UNAVAILABLE",
    "BUSY",
    "PARTIAL",
    "INTERNAL",
    "RECOVERY_REQUIRED",
    "NOT_FOUND",
    "LEASE_EXPIRED",
    "WORKER_OFFLINE",
    "UNSUPPORTED",
]

CommandMode = Literal["sync", "async"]
JobState = Literal[
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "needs_decision",
    "superseded",
]


class RequestEnvelope(BaseModel):
    """Common envelope for all CLI requests.

    Phase C adds ``mode`` (sync/async) and keeps the v0.1 keys.
    """

    model_config = ConfigDict(extra="forbid")

    api_version: int
    request_id: str
    client_id: str = "local_cli"
    idempotency_key: str | None = None
    operation: OperationKind
    mode: CommandMode = "sync"

    @field_validator("api_version")
    @classmethod
    def _api_version_ok(cls, v: int) -> int:
        if v != COMMAND_API_VERSION:
            raise ValueError(f"unsupported api_version={v}; expected {COMMAND_API_VERSION}")
        return v


# ---------------------------------------------------------------------------
# Per-operation params (discriminated by ``operation``).
# ---------------------------------------------------------------------------


class InitParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roots: dict[str, str] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)


class ScanParams(BaseModel):
    """v1 phase B contract.

    * ``mode`` is ``"incremental"`` (default) or ``"backfill"`` (records
      a ``backfill_runs`` row, does NOT advance ``complete_offset``).
    * ``since_iso`` / ``until_iso`` are tz-aware ISO 8601; naive
      strings are rejected at validation time.
    * ``roots`` is required so the scanner never falls back to home.
    """

    model_config = ConfigDict(extra="forbid")

    roots: dict[str, str] = Field(default_factory=dict)
    mode: Literal["incremental", "backfill"] = "incremental"
    since_iso: str | None = None
    until_iso: str | None = None
    timezone: str | None = None
    max_bytes_per_file: int = Field(default=1_048_576, ge=4096, le=64 * 1024 * 1024)

    @field_validator("since_iso", "until_iso")
    @classmethod
    def _tz_aware(cls, v: str | None) -> str | None:
        if v is None:
            return v
        raw = v.strip()
        if not raw:
            raise ValueError("timestamp must not be empty")
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = __import__("datetime").datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"not an ISO 8601 timestamp: {v!r}") from exc
        if dt.tzinfo is None:
            raise ValueError(
                f"naive timestamp {v!r} not allowed; pass UTC offset or 'Z'"
            )
        return v


class BatchPlanParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    branch_id: str = "main"
    max_turns_per_batch: int = Field(default=5, ge=1, le=50)
    max_input_tokens: int = Field(default=6000, ge=512, le=64_000)


class BatchProcessParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_ids: list[str] = Field(default_factory=list)
    max_batches: int = Field(default=5, ge=1, le=200)


class MaintainParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roots: dict[str, str] = Field(default_factory=dict)
    max_batches: int = Field(default=5, ge=1, le=200)
    max_turns_per_batch: int = Field(default=5, ge=1, le=50)
    max_input_tokens: int = Field(default=6000, ge=512, le=64_000)


class ReviewParams(BaseModel):
    """Weekly / monthly review proposals.

    Phase C: review is **explicitly unsupported** in v1 (per the user's
    Phase C scope) — it returns ``UNSUPPORTED`` when the worker or
    dispatcher sees it. The schema is defined so ``call`` can refuse
    with a machine-readable reason rather than crash on unknown fields.
    """

    model_config = ConfigDict(extra="forbid")

    period: Literal["week", "month"]
    domain: str | None = None
    budget: dict[str, Any] = Field(default_factory=dict)


class RecallParams(BaseModel):
    """v1 contract:

    * ``retrieval`` defaults to ``"keyword"``; ``"bm25"`` is opt-in.
    * ``rewrite`` / ``expand`` default to ``"off"`` — a model-backed
      rewrite is never used unless the caller explicitly opts in AND a
      typed provider is wired.
    * ``cursor`` is opaque; the server decides staleness by fingerprint
      + snapshot, never by trusting the client.
    """

    model_config = ConfigDict(extra="forbid")

    query: str
    project_ids: list[str] = Field(default_factory=list)
    domain_ids: list[str] = Field(default_factory=list)
    limit: int = Field(default=8, ge=1, le=100)
    include_archived: bool = False
    retrieval: Literal["keyword", "bm25"] = "keyword"
    rewrite: Literal["off", "auto"] = "off"
    expand: Literal["off", "auto"] = "off"
    cursor: str | None = None
    retrieval_limit: int = Field(default=30, ge=1, le=200)
    audit: bool = True


class CaptureParams(BaseModel):
    """External import entry point. Phase C exposes the contract but
    the dispatcher accepts it for forward compatibility — actual
    ingest is out of scope for Phase C (the user said "Do not implement
    Telegram, HTTP/UI or backup in this task")."""

    model_config = ConfigDict(extra="forbid")

    events: list[dict[str, Any]] = Field(default_factory=list)


class ConflictsParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_decided: bool = False


class DecideParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conflict_id: str
    proposal_revision: int = Field(ge=1)
    action: Literal["adopt_new", "keep_old", "scope_split", "defer"]
    note: str | None = None
    expected_memory_revisions: list[str] = Field(default_factory=list)


class ConflictDecideParams(DecideParams):
    """Alias of ``decide`` used by ``memory call`` so the operation
    literal stays discriminated (``conflict.decide`` vs ``decide``).
    """

    model_config = ConfigDict(extra="forbid")


class SourceCorrectionDecideParams(BaseModel):
    """User decision on a ``source_correction_proposals`` row.

    ``expected_proposal_revision`` is the optimistic-concurrency token
    returned by the previous ``source_correction.list`` / discovery
    pass; a stale value is rejected with ``INVALID_REQUEST`` and the
    row is not touched.
    """

    model_config = ConfigDict(extra="forbid")

    correction_id: str
    action: Literal["keep_old", "adopt_new", "scope_split", "defer"]
    expected_proposal_revision: int = Field(default=1, ge=1)
    note: str | None = None


class JobStatusParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str


class JobCancelParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    expected_revision: int = Field(ge=1)


class JobRetryParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    expected_revision: int = Field(ge=1)


# Discriminated union: callers can use ``CommandRequest.model_validate``
# and pydantic picks the right branch via ``operation`` matching.
OPERATION_PARAM_SCHEMA = {
    "init": InitParams,
    "scan": ScanParams,
    "batch.plan": BatchPlanParams,
    "batch.process": BatchProcessParams,
    "maintain": MaintainParams,
    "review": ReviewParams,
    "recall": RecallParams,
    "capture": CaptureParams,
    "conflicts": ConflictsParams,
    "decide": DecideParams,
    "conflict.decide": ConflictDecideParams,
    "source_correction.decide": SourceCorrectionDecideParams,
    "job.status": JobStatusParams,
    "job.cancel": JobCancelParams,
    "job.retry": JobRetryParams,
    "doctor": RootModel[Any],  # empty params
}

# Late-bound imports for privacy / backup contracts so this module
# stays importable without dragging the privacy/backup schemas into
# every caller.
def _late_schemas() -> dict[str, Any]:
    from memory_agent.contracts.privacy import (
        PrivacyForgetParams,
        PrivacyListParams,
        PrivacyReceiptParams,
        PrivacyUnblockParams,
    )
    from memory_agent.contracts.backup import (
        BackupCreateParams,
        BackupListParams,
        BackupRestoreParams,
    )
    return {
        "privacy.forget": PrivacyForgetParams,
        "privacy.list": PrivacyListParams,
        "privacy.receipt": PrivacyReceiptParams,
        "privacy.unblock": PrivacyUnblockParams,
        "backup.create": BackupCreateParams,
        "backup.list": BackupListParams,
        "backup.restore": BackupRestoreParams,
    }

_OPERATION_PARAM_SCHEMA_EXTRA: dict[str, Any] | None = None


def get_operation_param_schema() -> dict[str, Any]:
    """Return the merged schema registry, lazily binding late imports."""
    global _OPERATION_PARAM_SCHEMA_EXTRA
    if _OPERATION_PARAM_SCHEMA_EXTRA is None:
        _OPERATION_PARAM_SCHEMA_EXTRA = _late_schemas()
    merged = dict(OPERATION_PARAM_SCHEMA)
    merged.update(_OPERATION_PARAM_SCHEMA_EXTRA)
    return merged


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deadline_seconds: int | None = Field(default=None, ge=1, le=86_400)
    max_model_calls: int | None = Field(default=None, ge=0, le=10_000)
    max_batches: int | None = Field(default=None, ge=1, le=10_000)


class Activation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["external_script", "explicit_command", "user_ui", "worker", "system"]
    actor: str | None = None


class CommandRequest(BaseModel):
    """Top-level versioned JSON request.

    ``params`` is a free-form object validated against the schema
    registered for ``operation`` via ``OPERATION_PARAM_SCHEMA``.
    Unknown fields on ``params`` are rejected.
    """

    model_config = ConfigDict(extra="forbid")

    api_version: int
    request_id: str
    client_id: str = "local_cli"
    idempotency_key: str | None = None
    operation: OperationKind
    mode: CommandMode = "sync"
    params: dict[str, Any] = Field(default_factory=dict)
    limits: Limits | None = None
    activation: Activation | None = None

    @field_validator("api_version")
    @classmethod
    def _api_version_ok(cls, v: int) -> int:
        if v != COMMAND_API_VERSION:
            raise ValueError(f"unsupported api_version={v}; expected {COMMAND_API_VERSION}")
        return v


def validate_params(operation: str, params: dict[str, Any]) -> BaseModel:
    """Validate ``params`` against the schema registered for ``operation``.

    Returns the validated pydantic model so callers can pull typed
    fields. Raises ``ValueError`` on unknown fields / wrong shapes;
    the dispatcher translates that into ``INVALID_REQUEST``.
    """
    schema = get_operation_param_schema().get(operation)
    if schema is None:
        raise ValueError(f"unknown operation: {operation!r}")
    # RootModel has no model_validate of dict-style; use direct parsing.
    if isinstance(schema, type) and issubclass(schema, RootModel):
        return schema.model_validate(params)
    return schema.model_validate(params)


# ---------------------------------------------------------------------------
# Legacy aliases (kept so v0.1 / phase-A callers still import cleanly).
# ---------------------------------------------------------------------------


# Backwards-compatible aliases — the original phase-A/B names. These are
# the same shape as the new ``*Params`` classes so existing imports keep
# working.
ScanRequest = ScanParams
RecallRequest = RecallParams
DecideRequest = DecideParams


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class ResponseError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str
    retryable: bool = False


class JobProgress(BaseModel):
    """Machine-readable progress emitted while a job runs."""

    model_config = ConfigDict(extra="forbid")

    stage: str
    discovered_events: int = 0
    planned_batches: int = 0
    completed_batches: int = 0
    extracted_candidates: int = 0
    committed_memories: int = 0
    pending_conflicts: int = 0
    model_calls: int = 0


class CommandResponse(BaseModel):
    """The single stdout JSON shape for every CLI command."""

    model_config = ConfigDict(extra="forbid")

    api_version: int
    request_id: str
    operation: str
    status: Literal["ok", "queued", "running", "partial", "error", "cancelled"]
    job_id: str | None = None
    job_state: str | None = None
    progress: JobProgress | None = None
    result: dict[str, Any] | None = None
    error: ResponseError | None = None
    warnings: list[str] = Field(default_factory=list)

    @field_validator("api_version")
    @classmethod
    def _api_version_ok(cls, v: int) -> int:
        if v != COMMAND_API_VERSION:
            raise ValueError(f"unsupported api_version={v}; expected {COMMAND_API_VERSION}")
        return v


def response_to_dict(r: CommandResponse) -> dict[str, Any]:
    return r.model_dump(mode="json")


def response_from_dict(data: dict[str, Any]) -> CommandResponse:
    return CommandResponse.model_validate(data)