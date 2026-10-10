"""HTTP command routes (T14 — write endpoints).

``POST /api/v1/commands`` accepts a versioned ``CommandRequest`` JSON
body and dispatches it through the existing ``handle_call`` entry point.
The HTTP layer does NOT write SQL itself — it forwards every request
to the application service so the CLI and the UI share the same
validation, idempotency, version checks, and audit-log behaviour.

Write methods enforce:

  * Origin allowlist (no wildcard CORS)
  * Session token via ``X-Memory-Agent-Token`` header (never in URL or
    log)
  * The same ``expected_revision`` / ``idempotency_key`` /
    ``api_version`` validation the CLI uses

``GET /api/v1/jobs/{job_id}`` reads the job row and returns its
current state + progress + result. ``GET`` does not require auth
beyond the loopback bind.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from memory_agent.application.call import (
    _job_status,
    _new_request_id,
    handle_call,
)
from memory_agent.contracts.commands import (
    COMMAND_API_VERSION,
    CommandRequest,
    RequestEnvelope,
)
from memory_agent.infrastructure.files import VaultLayout
from memory_agent.interfaces.http.local_auth import enforce_write_auth


router = APIRouter(prefix="/api/v1")


def _vault(request: Request) -> VaultLayout:
    vault: VaultLayout | None = getattr(request.app.state, "vault", None)
    if vault is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="vault not configured",
        )
    return vault


# ---------------------------------------------------------------------------
# POST /api/v1/commands
# ---------------------------------------------------------------------------


@router.post(
    "/commands",
    dependencies=[Depends(enforce_write_auth)],
)
async def post_command(
    request: Request,
) -> JSONResponse:
    """Dispatch a single ``CommandRequest`` through ``handle_call``.

    The body MUST be a JSON object with at least ``api_version``,
    ``request_id``, and ``operation``. ``handle_call`` validates the
    rest. Any non-2xx ``CommandResponse`` (i.e. ``status="error"``)
    is surfaced to the HTTP caller with the matching HTTP status so
    the UI can render a typed error message.
    """

    vault = _vault(request)
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid JSON body: {exc}",
        )
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="body must be a JSON object",
        )
    outcome = handle_call(
        request=body,
        vault_root=vault.root,
        worker_offline_hint=True,
    )
    response = outcome.response
    if response.get("status") == "error":
        err = response.get("error") or {}
        code = str(err.get("code") or "INTERNAL")
        http_code = _http_status_for_error(code)
        return JSONResponse(content=response, status_code=http_code)
    return JSONResponse(content=response, status_code=200)


# ---------------------------------------------------------------------------
# GET /api/v1/jobs/{job_id}
# ---------------------------------------------------------------------------


@router.get("/jobs/{job_id}")
def get_job(
    request: Request,
    job_id: str,
) -> JSONResponse:
    """Read a job's current state.

    The route is intentionally GET-only: cancel / retry go through
    ``POST /api/v1/commands`` with the ``job.cancel`` / ``job.retry``
    operation so versioned revision checks apply.
    """

    vault = _vault(request)
    if not vault.state_db.exists():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="vault not initialised",
        )
    fake_req = CommandRequest(
        api_version=COMMAND_API_VERSION,
        request_id=f"http_job_status_{job_id}",
        operation="job.status",
        params={"job_id": job_id},
    )
    envelope = RequestEnvelope(
        api_version=COMMAND_API_VERSION,
        request_id=_new_request_id(),
        operation="job.status",
    )
    outcome = _job_status(fake_req, envelope.request_id, vault.root)
    if outcome.response.get("status") == "error":
        err = outcome.response.get("error") or {}
        code = str(err.get("code") or "INTERNAL")
        return JSONResponse(
            content=outcome.response,
            status_code=_http_status_for_error(code),
        )
    return JSONResponse(content=outcome.response, status_code=200)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _http_status_for_error(code: str) -> int:
    """Translate ``ErrorCode`` to HTTP.

    The mapping is intentionally narrow: anything we don't recognise
    becomes ``500 Internal Server Error`` so the operator sees a
    real failure in the HTTP logs instead of a misleading 200.
    """

    mapping: dict[str, int] = {
        "INVALID_REQUEST": 422,
        "INVALID_VERSION": 422,
        "UNKNOWN_OPERATION": 422,
        "IDEMPOTENCY_CONFLICT": 409,
        "SOURCE_UNAVAILABLE": 503,
        "PROVIDER_UNAVAILABLE": 503,
        "BUSY": 423,
        "PARTIAL": 206,
        "INTERNAL": 500,
        "RECOVERY_REQUIRED": 503,
        "NOT_FOUND": 404,
        "LEASE_EXPIRED": 409,
        "WORKER_OFFLINE": 503,
        "UNSUPPORTED": 501,
    }
    return mapping.get(code, 500)


__all__ = ["router", "_http_status_for_error"]