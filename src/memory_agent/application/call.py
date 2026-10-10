"""Versioned ``memory call`` dispatcher (Phase C).

The CLI ``memory call --request-file - --json`` reads a single JSON
``CommandRequest`` from stdin (or a file path), dispatches it via the
sync or async path, and writes exactly one JSON ``CommandResponse`` to
stdout. Diagnostics go to stderr.

Contract:

  * ``mode="sync"`` runs the operation inline and never opens a worker
    connection. Same-key replay returns the original job/result (the
    job is recorded as ``succeeded`` after the inline run, so the
    next async call with the same key also sees the cached outcome).
  * ``mode="async"`` durably inserts the request as a job (state
    ``queued``) and returns ``status="queued"`` + ``job_id``. The
    job is claimable by ``memory worker`` or by the long-running
    ``memory worker`` loop.
  * ``(client_id, idempotency_key)`` is bound to a canonical
    ``request_hash``. Same key + same params returns the original
    job/result. Same key + different params raises
    ``IDEMPOTENCY_CONFLICT`` (exit 4).
  * Unknown fields on the envelope or any ``params`` block are
    rejected with ``INVALID_REQUEST``; unknown operations with
    ``UNKNOWN_OPERATION``.
  * Job lifecycle is durable across worker restarts; the worker may
    reclaim expired leases and stale ``running`` jobs whose
    ``last_heartbeat`` is older than the lease window.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory_agent.application.dispatch import dispatch as legacy_dispatch
from memory_agent.application.recovery import recover_unfinished_intents
from memory_agent.application.source_correction import (
    decide_proposal,
    discover_affected_memories,
    list_pending_for_memory,
    refresh_overlay,
)
from memory_agent.contracts.commands import (
    COMMAND_API_VERSION,
    CommandMode,
    CommandRequest,
    CommandResponse,
    JobProgress,
    ResponseError,
    validate_params,
)
from memory_agent.infrastructure.files import VaultLayout
from memory_agent.infrastructure.sqlite import open_state_db
from memory_agent.infrastructure.sqlite.repositories import (
    AuditRepo,
    JobRepo,
    SourceCorrectionProposalRepo,
)


_IDEMPOTENCY_CONFLICT_CODE = "IDEMPOTENCY_CONFLICT"


class CallError(ValueError):
    """Wraps an INVALID_REQUEST / IDEMPOTENCY_CONFLICT result.

    Callers (CLI / tests) translate this into the matching exit code.
    """

    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def canonical_request_hash(req: CommandRequest) -> str:
    """Stable hash over the request envelope + params.

    ``api_version``, ``request_id``, ``client_id``,
    ``idempotency_key`` and ``operation`` are not part of the hash —
    only ``mode``, ``limits``, ``activation`` and the validated
    ``params`` dict are. Re-rendering the same logical request through
    a different ``request_id`` therefore yields the same hash so the
    idempotency lookup is request-id-stable.
    """
    payload = {
        "mode": req.mode,
        "operation": req.operation,
        "limits": req.limits.model_dump(mode="json") if req.limits else None,
        "activation": req.activation.model_dump(mode="json") if req.activation else None,
        "params": json.loads(_canonical_json(req.params)),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _canonical_json(obj: Any) -> str:
    """Render a JSON-encodable object with sorted keys."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str, *parts: str) -> str:
    h = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{h[:16]}"


@dataclass
class CallOutcome:
    """What ``handle_call`` returned. JSON-serialisable as a dict."""

    response: dict[str, Any]
    job_id: str | None = None


def handle_call(
    *,
    request: dict[str, Any] | CommandRequest,
    vault_root: Path,
    worker_offline_hint: bool = True,
) -> CallOutcome:
    """Parse + dispatch one versioned ``CommandRequest``.

    Returns a :class:`CallOutcome` whose ``response`` is the
    JSON-ready ``CommandResponse``. Callers may write it verbatim to
    stdout. Sync runs use the legacy dispatcher (which never calls a
    worker); async runs insert a durable job that the worker can
    claim later.

    The ``vault_root`` is threaded through every internal helper
    (``_job_status``, ``_job_cancel``, ``_job_retry``) so there is no
    module-global mutable state. Multi-client callers (e.g. a future
    FastAPI loopback) can call this from concurrent threads against
    distinct vaults without colliding on a shared holder.
    """
    # 1. Parse.
    if isinstance(request, dict):
        try:
            req = CommandRequest.model_validate(request)
        except Exception as exc:
            return _error_outcome(
                request_id=str(request.get("request_id", "")) or _new_request_id(),
                operation=str(request.get("operation", "unknown")),
                code="INVALID_REQUEST",
                message=f"bad request envelope: {exc}",
            )
    else:
        req = request

    request_id = req.request_id or _new_request_id()
    operation = req.operation

    # 2. Validate ``params`` against the operation's schema.
    try:
        validated_params = validate_params(operation, req.params)
    except ValueError as exc:
        return _error_outcome(
            request_id=request_id,
            operation=operation,
            code="INVALID_REQUEST",
            message=f"{operation}: {exc}",
        )

    # 3. Special-case review (Phase C repair-2: review is no longer
    #    UNSUPPORTED — it routes through the worker's conservative
    #    review pipeline. The async path enqueues it as a normal
    #    ``review`` job; the sync path runs the proposal inline and
    #    returns it in the response).
    if operation == "review":
        vault = VaultLayout(vault_root)
        return _enqueue_review(req, vault, request_id)

    # 4. Job state ops (job.status, job.cancel, job.retry) do not
    #    enqueue a new job; they read/mutate the existing jobs row.
    if operation == "job.status":
        return _job_status(req, request_id, vault_root)
    if operation == "job.cancel":
        return _job_cancel(req, request_id, vault_root)
    if operation == "job.retry":
        return _job_retry(req, request_id, vault_root)

    # 5. Idempotency: same key + same hash → return original job.
    vault = VaultLayout(vault_root)
    if not vault.state_db.exists():
        return _error_outcome(
            request_id=request_id,
            operation=operation,
            code="RECOVERY_REQUIRED",
            message="vault not initialised; run `memory init` first",
        )

    req_hash = canonical_request_hash(req)
    job_repo = _open_jobs(vault)
    existing = job_repo.get_by_idempotency(req.client_id, req.idempotency_key or "")
    if existing is not None and not (req.idempotency_key is None or req.idempotency_key == ""):
        prev_hash = existing.get("request_hash", "")
        if prev_hash != req_hash:
            return _error_outcome(
                request_id=request_id,
                operation=operation,
                code=_IDEMPOTENCY_CONFLICT_CODE,
                message=(
                    f"idempotency_key {req.idempotency_key!r} already used "
                    f"with different params (job_id={existing['job_id']})"
                ),
            )
        # Replay: return the original outcome. For async jobs the
        # caller can poll the original job_id.
        return _replay_outcome(existing, request_id)

    # 6. Async path: durably queue the job and return queued.
    if req.mode == "async":
        return _enqueue_job(req, validated_params, req_hash, vault, request_id)

    # 7. Sync path: dispatch inline and persist the outcome so a
    #    follow-up async call sees the cached job.
    return _run_sync(req, validated_params, req_hash, vault, request_id)


def _replay_outcome(existing: dict[str, Any], request_id: str) -> CallOutcome:
    state = existing.get("state", "queued")
    progress = _decode_json(existing.get("progress_json") or "{}")
    result = _decode_json(existing.get("result_json") or "null")
    error = _decode_json(existing.get("error_json") or "null")
    status = "ok" if state == "succeeded" else (
        "running" if state == "running" else (
            "queued" if state == "queued" else (
                "cancelled" if state == "cancelled" else "error"
            )
        )
    )
    response = CommandResponse(
        api_version=COMMAND_API_VERSION,
        request_id=request_id,
        operation=existing.get("operation") or "unknown",
        status=status,
        job_id=existing.get("job_id"),
        job_state=state,
        progress=JobProgress(**progress) if progress else None,
        result=result,
        error=ResponseError(**error) if error else None,
        warnings=[],
    )
    return CallOutcome(response=response.model_dump(mode="json"),
                       job_id=existing.get("job_id"))


def _enqueue_job(
    req: CommandRequest,
    validated_params: Any,
    req_hash: str,
    vault: VaultLayout,
    request_id: str,
) -> CallOutcome:
    job_id = _new_id("job", req_hash, request_id)
    conn = open_state_db(vault.state_db)
    try:
        recover_unfinished_intents(vault=vault, conn=conn)
        job_repo = JobRepo(conn)
        # Persist a ``params_ref`` artifact so the worker can fetch the
        # validated payload without re-running validation.
        params_ref = _persist_params_artifact(
            vault=vault, job_id=job_id, req=req, params=validated_params,
        )
        job_repo.insert({
            "job_id": job_id,
            "request_id": request_id,
            "client_id": req.client_id,
            "idempotency_key": req.idempotency_key,
            "request_hash": req_hash,
            "operation": req.operation,
            "params_ref": params_ref,
            "state": "queued",
            "mode": req.mode,
        })
    finally:
        try:
            conn.close()
        except Exception:
            pass
    response = CommandResponse(
        api_version=COMMAND_API_VERSION,
        request_id=request_id,
        operation=req.operation,
        status="queued",
        job_id=job_id,
        job_state="queued",
        progress=JobProgress(stage="queued"),
        warnings=["worker_offline"] if _no_active_lease(vault) else [],
        result=None,
        error=None,
    )
    return CallOutcome(response=response.model_dump(mode="json"), job_id=job_id)


def _run_sync(
    req: CommandRequest,
    validated_params: Any,
    req_hash: str,
    vault: VaultLayout,
    request_id: str,
) -> CallOutcome:
    job_id = _new_id("job", req_hash, request_id)
    conn = open_state_db(vault.state_db)
    audit = AuditRepo(conn)
    try:
        recover_unfinished_intents(vault=vault, conn=conn)
        job_repo = JobRepo(conn)
        params_ref = _persist_params_artifact(
            vault=vault, job_id=job_id, req=req, params=validated_params,
        )
        # Same-key replay safety: if a sync job was previously
        # persisted for this exact (client, key, hash), return it.
        existing = (
            job_repo.get_by_idempotency(req.client_id, req.idempotency_key or "")
            if req.idempotency_key
            else None
        )
        if existing is not None and existing.get("request_hash") == req_hash:
            return _replay_outcome(existing, request_id)
        job_repo.insert({
            "job_id": job_id,
            "request_id": request_id,
            "client_id": req.client_id,
            "idempotency_key": req.idempotency_key,
            "request_hash": req_hash,
            "operation": req.operation,
            "params_ref": params_ref,
            "state": "running",
            "mode": req.mode,
        })
        job_repo.mark_started(job_id)
        # Dispatch.
        try:
            params_dict = validated_params.model_dump(mode="json") \
                if hasattr(validated_params, "model_dump") \
                else (validated_params.root if hasattr(validated_params, "root") else dict(validated_params))
            legacy = legacy_dispatch(
                operation=req.operation,
                params=params_dict,
                request_id=request_id,
                client_id=req.client_id,
                idempotency_key=req.idempotency_key,
                vault_root=vault.root,
            )
        except Exception as exc:  # noqa: BLE001
            err = {"code": "INTERNAL", "message": f"{type(exc).__name__}: {exc}"}
            job_repo.set_error(job_id, err)
            audit.append({
                "audit_id": _new_id("aud", job_id, "error"),
                "actor": "cli",
                "action": f"{req.operation}.failed",
                "occurred_at": _now_iso(),
                "object_type": "job",
                "object_id": job_id,
                "correlation": {"job_id": job_id},
                "before_after": {},
                "outcome": "error",
                "reason_code": "INTERNAL",
                "details": {"error": err},
            })
            return CallOutcome(response=_err_response(
                request_id, req.operation, "INTERNAL", err["message"],
            ).model_dump(mode="json"))
        # Build the success response.
        status = legacy.get("status", "ok")
        if status == "ok":
            job_repo.set_result(job_id, legacy.get("result") or {})
            audit.append({
                "audit_id": _new_id("aud", job_id, "ok"),
                "actor": "cli",
                "action": f"{req.operation}.succeeded",
                "occurred_at": _now_iso(),
                "object_type": "job",
                "object_id": job_id,
                "correlation": {"job_id": job_id},
                "before_after": {},
                "outcome": "success",
                "reason_code": None,
                "details": {"result_keys": list((legacy.get("result") or {}).keys())},
            })
            return CallOutcome(response=CommandResponse(
                api_version=COMMAND_API_VERSION,
                request_id=request_id,
                operation=req.operation,
                status="ok",
                job_id=job_id,
                job_state="succeeded",
                progress=JobProgress(stage="succeeded"),
                result=legacy.get("result"),
                warnings=legacy.get("warnings", []),
                error=None,
            ).model_dump(mode="json"), job_id=job_id)
        # Sync path error.
        err = legacy.get("error") or {"code": "INTERNAL", "message": "unknown"}
        job_repo.set_error(job_id, err)
        audit.append({
            "audit_id": _new_id("aud", job_id, "error"),
            "actor": "cli",
            "action": f"{req.operation}.failed",
            "occurred_at": _now_iso(),
            "object_type": "job",
            "object_id": job_id,
            "correlation": {"job_id": job_id},
            "before_after": {},
            "outcome": "error",
            "reason_code": err.get("code"),
            "details": err,
        })
        return CallOutcome(response=CommandResponse(
            api_version=COMMAND_API_VERSION,
            request_id=request_id,
            operation=req.operation,
            status="error",
            job_id=job_id,
            job_state="failed",
            progress=JobProgress(stage="failed"),
            result=legacy.get("result"),
            warnings=legacy.get("warnings", []),
            error=ResponseError(**err),
        ).model_dump(mode="json"), job_id=job_id)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _enqueue_review(
    req: CommandRequest, vault: VaultLayout, request_id: str,
) -> CallOutcome:
    """Dispatch a ``review`` request.

    Phase C repair-2:

      * ``mode="async"`` queues a durable ``review`` job; the
        worker drains it (calling ``run_review``).
      * ``mode="sync"`` runs the same conservative review pipeline
        inline and returns the proposal in the response, persisting
        the outcome so a follow-up async call with the same
        idempotency key sees the cached result.
    """
    from memory_agent.contracts.commands import ReviewParams
    try:
        params = ReviewParams.model_validate(req.params)
    except Exception as exc:
        return _error_outcome(
            request_id=request_id, operation="review",
            code="INVALID_REQUEST", message=f"review: {exc}",
        )
    if req.mode == "async":
        # Use the generic enqueue path so the worker drains the job.
        # We bypass the params validation in ``_enqueue_job`` (it
        # calls ``validate_params`` again which would re-parse
        # ``params``); the validated ``ReviewParams`` instance is
        # passed through directly.
        req_hash = canonical_request_hash(req)
        return _enqueue_job(req, params, req_hash, vault, request_id)
    # Sync path: run inline, persist outcome, return the proposal.
    from memory_agent.application.worker import run_review
    req_hash = canonical_request_hash(req)
    job_id = _new_id("job", req_hash, request_id)
    conn = open_state_db(vault.state_db)
    try:
        recover_unfinished_intents(vault=vault, conn=conn)
        # Same-key replay safety.
        job_repo = JobRepo(conn)
        existing = (
            job_repo.get_by_idempotency(req.client_id, req.idempotency_key or "")
            if req.idempotency_key else None
        )
        if existing is not None and existing.get("request_hash") == req_hash:
            return _replay_outcome(existing, request_id)
        params_ref = _persist_params_artifact(
            vault=vault, job_id=job_id, req=req, params=params,
        )
        job_repo.insert({
            "job_id": job_id,
            "request_id": request_id,
            "client_id": req.client_id,
            "idempotency_key": req.idempotency_key,
            "request_hash": req_hash,
            "operation": "review",
            "params_ref": params_ref,
            "state": "running",
            "mode": req.mode,
        })
        job_repo.mark_started(job_id)
        try:
            review = run_review(
                vault_root=vault.root, period=params.period,
                worker_id=req.client_id,
            )
        except Exception as exc:  # noqa: BLE001
            err = {"code": "INTERNAL", "message": f"{type(exc).__name__}: {exc}"}
            job_repo.set_error(job_id, err)
            return CallOutcome(response=_err_response(
                request_id, "review", "INTERNAL", err["message"],
            ).model_dump(mode="json"))
        job_repo.set_result(job_id, review)
        AuditRepo(conn).append({
            "audit_id": _new_id("aud", job_id, "ok"),
            "actor": "cli",
            "action": "review.succeeded",
            "occurred_at": _now_iso(),
            "object_type": "job",
            "object_id": job_id,
            "correlation": {"job_id": job_id},
            "before_after": {},
            "outcome": "success",
            "reason_code": None,
            "details": {"period": params.period, "proposal_count":
                        review.get("proposal_count", 0)},
        })
        return CallOutcome(response=CommandResponse(
            api_version=COMMAND_API_VERSION,
            request_id=request_id,
            operation="review",
            status="ok",
            job_id=job_id,
            job_state="succeeded",
            progress=JobProgress(stage="succeeded"),
            result=review,
            warnings=[],
            error=None,
        ).model_dump(mode="json"), job_id=job_id)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _job_status(
    req: CommandRequest, request_id: str, vault_root: Path,
) -> CallOutcome:
    job_id = req.params.get("job_id") or ""
    if not job_id:
        return _error_outcome(request_id, "job.status", "INVALID_REQUEST",
                              "job.status: 'job_id' is required")
    vault = VaultLayout(vault_root)
    if not vault.state_db.exists():
        return _error_outcome(request_id, "job.status", "RECOVERY_REQUIRED",
                              "vault not initialised")
    conn = open_state_db(vault.state_db)
    try:
        job_repo = JobRepo(conn)
        row = job_repo.get(job_id)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if row is None:
        return _error_outcome(request_id, "job.status", "NOT_FOUND",
                              f"job_id {job_id!r} not found")
    progress = _decode_json(row.get("progress_json") or "{}")
    result = _decode_json(row.get("result_json") or "null")
    error = _decode_json(row.get("error_json") or "null")
    state = row.get("state", "queued")
    status = "ok" if state == "succeeded" else (
        "running" if state == "running" else (
            "queued" if state == "queued" else (
                "cancelled" if state == "cancelled" else "error"
            )
        )
    )
    return CallOutcome(response=CommandResponse(
        api_version=COMMAND_API_VERSION,
        request_id=request_id,
        operation="job.status",
        status=status,
        job_id=job_id,
        job_state=state,
        progress=JobProgress(**progress) if progress else None,
        result=result,
        error=ResponseError(**error) if error else None,
        warnings=(["cancel_does_not_rollback_published_commits"]
                  if state == "succeeded" else []),
    ).model_dump(mode="json"), job_id=job_id)


def _job_cancel(
    req: CommandRequest, request_id: str, vault_root: Path,
) -> CallOutcome:
    job_id = req.params.get("job_id") or ""
    expected_revision = req.params.get("expected_revision", 1)
    if not job_id:
        return _error_outcome(request_id, "job.cancel", "INVALID_REQUEST",
                              "job.cancel: 'job_id' is required")
    vault = VaultLayout(vault_root)
    conn = open_state_db(vault.state_db)
    try:
        job_repo = JobRepo(conn)
        existing = job_repo.get(job_id)
        if existing is None:
            return _error_outcome(request_id, "job.cancel", "NOT_FOUND",
                                  f"job_id {job_id!r} not found")
        if int(existing.get("revision") or 0) != int(expected_revision):
            return _error_outcome(request_id, "job.cancel", "INVALID_REQUEST",
                                  f"expected_revision {expected_revision} is stale; "
                                  f"current is {existing.get('revision')}")
        state = existing.get("state")
        if state in {"succeeded", "failed", "cancelled"}:
            # Terminal. Cancel is idempotent: return the existing state.
            return _job_status(req, request_id, vault_root)
        # queued → mark cancel_requested; running → mark cancel_requested
        # and let the worker observe it on the next heartbeat.
        ok = job_repo.request_cancel(job_id, int(expected_revision))
        if not ok:
            return _error_outcome(request_id, "job.cancel", "INVALID_REQUEST",
                                  "expected_revision is stale; retry with the current revision")
        audit = AuditRepo(conn)
        audit.append({
            "audit_id": _new_id("aud", job_id, "cancel"),
            "actor": "cli",
            "action": "job.cancel_requested",
            "occurred_at": _now_iso(),
            "object_type": "job",
            "object_id": job_id,
            "correlation": {"job_id": job_id},
            "before_after": {"state": state},
            "outcome": "success",
            "reason_code": None,
            "details": {"expected_revision": expected_revision},
        })
        return CallOutcome(response=CommandResponse(
            api_version=COMMAND_API_VERSION,
            request_id=request_id,
            operation="job.cancel",
            status="ok",
            job_id=job_id,
            job_state="cancel_requested",
            result={
                "job_id": job_id,
                "state": "cancel_requested",
                "cancel_requested": True,
                "committed": state == "succeeded",
            },
            warnings=["cancel_does_not_rollback_published_commits"],
            error=None,
        ).model_dump(mode="json"), job_id=job_id)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _job_retry(
    req: CommandRequest, request_id: str, vault_root: Path,
) -> CallOutcome:
    job_id = req.params.get("job_id") or ""
    expected_revision = req.params.get("expected_revision", 1)
    if not job_id:
        return _error_outcome(request_id, "job.retry", "INVALID_REQUEST",
                              "job.retry: 'job_id' is required")
    vault = VaultLayout(vault_root)
    conn = open_state_db(vault.state_db)
    try:
        job_repo = JobRepo(conn)
        existing = job_repo.get(job_id)
        if existing is None:
            return _error_outcome(request_id, "job.retry", "NOT_FOUND",
                                  f"job_id {job_id!r} not found")
        if int(existing.get("revision") or 0) != int(expected_revision):
            return _error_outcome(request_id, "job.retry", "INVALID_REQUEST",
                                  f"expected_revision {expected_revision} is stale; "
                                  f"current is {existing.get('revision')}")
        state = existing.get("state")
        if state not in {"failed", "cancelled"}:
            return _error_outcome(request_id, "job.retry", "INVALID_REQUEST",
                                  f"job state {state!r} is not retryable; "
                                  "only failed / cancelled jobs can be retried")
        # Re-queue the job by resetting state to 'queued'.
        cur = conn.execute(
            "UPDATE jobs SET state='queued', cancel_requested=0, "
            "ended_at=NULL, error_json=NULL, revision=revision+1 "
            "WHERE job_id=? AND revision=?",
            (job_id, int(expected_revision)),
        )
        if cur.rowcount != 1:
            return _error_outcome(request_id, "job.retry", "INVALID_REQUEST",
                                  "expected_revision is stale; retry with the current revision")
        audit = AuditRepo(conn)
        audit.append({
            "audit_id": _new_id("aud", job_id, "retry"),
            "actor": "cli",
            "action": "job.retry_requested",
            "occurred_at": _now_iso(),
            "object_type": "job",
            "object_id": job_id,
            "correlation": {"job_id": job_id},
            "before_after": {"state": state},
            "outcome": "success",
            "reason_code": None,
            "details": {"expected_revision": expected_revision},
        })
        return CallOutcome(response=CommandResponse(
            api_version=COMMAND_API_VERSION,
            request_id=request_id,
            operation="job.retry",
            status="queued",
            job_id=job_id,
            job_state="queued",
            result={"job_id": job_id, "state": "queued"},
            warnings=[],
            error=None,
        ).model_dump(mode="json"), job_id=job_id)
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _open_jobs(vault: VaultLayout) -> JobRepo:
    conn = open_state_db(vault.state_db)
    return JobRepo(conn)


def _persist_params_artifact(
    *,
    vault: VaultLayout,
    job_id: str,
    req: CommandRequest,
    params: Any,
) -> str:
    """Persist the validated params so the worker can read them later."""
    artifacts = vault.artifacts_dir
    artifacts.mkdir(parents=True, exist_ok=True)
    payload = {
        "api_version": COMMAND_API_VERSION,
        "job_id": job_id,
        "request_id": req.request_id,
        "client_id": req.client_id,
        "operation": req.operation,
        "mode": req.mode,
        "params": params.model_dump(mode="json")
        if hasattr(params, "model_dump")
        else params,
        "limits": req.limits.model_dump(mode="json") if req.limits else None,
        "activation": req.activation.model_dump(mode="json") if req.activation else None,
    }
    path = artifacts / f"{job_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return str(path)


def _load_params_artifact(vault: VaultLayout, params_ref: str) -> dict[str, Any] | None:
    path = Path(params_ref)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _decode_json(blob: str) -> Any:
    if not blob:
        return None
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return None


def _no_active_lease(vault: VaultLayout) -> bool:
    conn = open_state_db(vault.state_db)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM worker_leases WHERE expires_at >= ?",
            (_now_iso(),),
        ).fetchone()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return int(row["c"] or 0) == 0


def _new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:12]}"


def _error_outcome(
    request_id: str,
    operation: str,
    code: str,
    message: str,
) -> CallOutcome:
    response = _err_response(request_id, operation, code, message)
    return CallOutcome(response=response.model_dump(mode="json"))


def _err_response(
    request_id: str, operation: str, code: str, message: str,
) -> CommandResponse:
    return CommandResponse(
        api_version=COMMAND_API_VERSION,
        request_id=request_id or _new_request_id(),
        operation=operation,
        status="error",
        error=ResponseError(code=code, message=message, retryable=False),
    )


# Public re-exports for tests.
__all__ = [
    "CallError",
    "CallOutcome",
    "canonical_request_hash",
    "handle_call",
]