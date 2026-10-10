"""HTTP audit routes (T14 — read-only endpoints).

Every endpoint here is GET-only, requires no auth beyond the
loopback bind, and never calls the model. Pagination uses opaque
server-side cursors; ``as_of`` / ``snapshot_id`` / ``projection_state``
are returned in the envelope header of every response.

Routes (paths match docs/10 §"本地审计 HTTP 接口"):

    /api/v1/overview
    /api/v1/sources
    /api/v1/sources/{id}
    /api/v1/sessions/{id}/turns
    /api/v1/batches
    /api/v1/batches/{id}
    /api/v1/batches/{id}/evidence
    /api/v1/memories/{id}/history
    /api/v1/conflicts
    /api/v1/audit
    /api/v1/audit/{id}
    /api/v1/queries
    /api/v1/queries/{trace_id}

The static UI asset path ``/`` is also served when a packaged
``ui-dist`` directory is present; otherwise a tiny placeholder page
is served so a curious operator opening ``http://127.0.0.1:<port>``
does not get an opaque 404. T15 will replace this with the real
React UI.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse, Response

from memory_agent.application.audit_queries import (
    AuditQueries,
    decode_cursor,
    list_summaries_for_route,
)
from memory_agent.infrastructure.sqlite import open_state_db


router = APIRouter(prefix="/api/v1")


def _audit_service(request: Request) -> AuditQueries:
    svc: AuditQueries | None = getattr(request.app.state, "audit", None)
    if svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="audit service not configured",
        )
    return svc


def _open_conn(request: Request):
    """Open a SQLite connection from the configured vault.

    The connection is closed at the end of the request via FastAPI's
    response lifecycle. We yield it through a dependency so an
    exception in the route still triggers cleanup.
    """

    vault = getattr(request.app.state, "vault", None)
    if vault is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="vault not configured",
        )
    if not vault.state_db.exists():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="vault not initialised",
        )
    conn = open_state_db(vault.state_db)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _validate_cursor(cursor: str | None, scope: str) -> str | None:
    """Reject cursors that aren't ours.

    A malformed cursor must not turn into a 500; we reject it as a
    400 with a stable code so the client knows to drop and refresh.
    """

    if cursor is None:
        return None
    decoded = decode_cursor(cursor, scope=scope)
    if decoded is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid or unknown cursor: {cursor!r}",
        )
    return cursor


# ---------------------------------------------------------------------------
# /api/v1/overview
# ---------------------------------------------------------------------------


@router.get("/overview")
def overview(
    request: Request,
    svc: AuditQueries = Depends(_audit_service),
    conn=Depends(_open_conn),
) -> dict[str, Any]:
    body = svc.overview(conn=conn)
    return body.model_dump(mode="json")


# ---------------------------------------------------------------------------
# /api/v1/sources
# ---------------------------------------------------------------------------


@router.get("/sources")
def list_sources(
    request: Request,
    cursor: str | None = Query(default=None),
    page_size: int = Query(default=25, ge=1, le=200),
    connector: str | None = Query(default=None),
    svc: AuditQueries = Depends(_audit_service),
    conn=Depends(_open_conn),
) -> dict[str, Any]:
    cursor = _validate_cursor(cursor, "sources")
    body = list_summaries_for_route(
        svc, conn=conn, method="sources",
        args={"cursor": cursor, "page_size": page_size, "connector": connector},
    )
    return body.model_dump(mode="json")


@router.get("/sources/{source_file_id}")
def source_detail(
    request: Request,
    source_file_id: str,
    svc: AuditQueries = Depends(_audit_service),
    conn=Depends(_open_conn),
) -> dict[str, Any]:
    dto = svc.source_detail(conn=conn, source_file_id=source_file_id)
    if dto is None:
        raise HTTPException(status_code=404, detail="source not found")
    return dto.model_dump(mode="json")


# ---------------------------------------------------------------------------
# /api/v1/sessions/{id}/turns
# ---------------------------------------------------------------------------


@router.get("/sessions/{session_id}/turns")
def list_session_turns(
    request: Request,
    session_id: str,
    cursor: str | None = Query(default=None),
    page_size: int = Query(default=25, ge=1, le=200),
    branch_id: str = Query(default="main"),
    svc: AuditQueries = Depends(_audit_service),
    conn=Depends(_open_conn),
) -> dict[str, Any]:
    cursor = _validate_cursor(cursor, "session_turns")
    body = list_summaries_for_route(
        svc, conn=conn, method="session_turns",
        args={
            "cursor": cursor, "page_size": page_size,
            "session_id": session_id, "branch_id": branch_id,
        },
    )
    return body.model_dump(mode="json")


# ---------------------------------------------------------------------------
# /api/v1/batches
# ---------------------------------------------------------------------------


@router.get("/batches")
def list_batches(
    request: Request,
    cursor: str | None = Query(default=None),
    page_size: int = Query(default=25, ge=1, le=200),
    status: str | None = Query(default=None),
    svc: AuditQueries = Depends(_audit_service),
    conn=Depends(_open_conn),
) -> dict[str, Any]:
    cursor = _validate_cursor(cursor, "batches")
    body = list_summaries_for_route(
        svc, conn=conn, method="batches",
        args={"cursor": cursor, "page_size": page_size, "status": status},
    )
    return body.model_dump(mode="json")


@router.get("/batches/{batch_id}")
def batch_detail(
    request: Request,
    batch_id: str,
    svc: AuditQueries = Depends(_audit_service),
    conn=Depends(_open_conn),
) -> dict[str, Any]:
    dto = svc.batch_detail(conn=conn, batch_id=batch_id)
    if dto is None:
        raise HTTPException(status_code=404, detail="batch not found")
    return dto.model_dump(mode="json")


@router.get("/batches/{batch_id}/evidence")
def batch_evidence(
    request: Request,
    batch_id: str,
    svc: AuditQueries = Depends(_audit_service),
    conn=Depends(_open_conn),
) -> dict[str, Any]:
    """The single endpoint that surfaces raw bodies. The list /
    detail routes above are *metadata-only* and never include the
    ``text`` field. The HTTP layer here is the same boundary the
    audit_queries module enforces.
    """

    dto = svc.batch_evidence(conn=conn, batch_id=batch_id)
    if dto is None:
        raise HTTPException(status_code=404, detail="batch not found")
    return dto.model_dump(mode="json")


# ---------------------------------------------------------------------------
# /api/v1/memories/{id}/history
# ---------------------------------------------------------------------------


@router.get("/memories/{memory_id}/history")
def memory_history(
    request: Request,
    memory_id: str,
    svc: AuditQueries = Depends(_audit_service),
    conn=Depends(_open_conn),
) -> dict[str, Any]:
    dto = svc.memory_history(conn=conn, memory_id=memory_id)
    if dto is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return dto.model_dump(mode="json")


# ---------------------------------------------------------------------------
# /api/v1/conflicts
# ---------------------------------------------------------------------------


@router.get("/conflicts")
def list_conflicts(
    request: Request,
    cursor: str | None = Query(default=None),
    page_size: int = Query(default=25, ge=1, le=200),
    include_decided: bool = Query(default=False),
    svc: AuditQueries = Depends(_audit_service),
    conn=Depends(_open_conn),
) -> dict[str, Any]:
    cursor = _validate_cursor(cursor, "conflicts")
    body = list_summaries_for_route(
        svc, conn=conn, method="conflicts",
        args={
            "cursor": cursor, "page_size": page_size,
            "include_decided": include_decided,
        },
    )
    return body.model_dump(mode="json")


# ---------------------------------------------------------------------------
# /api/v1/audit
# ---------------------------------------------------------------------------


@router.get("/audit")
def list_audit_events(
    request: Request,
    cursor: str | None = Query(default=None),
    page_size: int = Query(default=25, ge=1, le=200),
    action: str | None = Query(default=None),
    object_type: str | None = Query(default=None),
    object_id: str | None = Query(default=None),
    seq_min: int | None = Query(default=None),
    seq_max: int | None = Query(default=None),
    svc: AuditQueries = Depends(_audit_service),
    conn=Depends(_open_conn),
) -> dict[str, Any]:
    cursor = _validate_cursor(cursor, "audit")
    body = list_summaries_for_route(
        svc, conn=conn, method="audit",
        args={
            "cursor": cursor, "page_size": page_size,
            "action": action, "object_type": object_type,
            "object_id": object_id, "seq_min": seq_min, "seq_max": seq_max,
        },
    )
    return body.model_dump(mode="json")


@router.get("/audit/{audit_id}")
def audit_event_detail(
    request: Request,
    audit_id: str,
    svc: AuditQueries = Depends(_audit_service),
    conn=Depends(_open_conn),
) -> dict[str, Any]:
    dto = svc.audit_event_detail(conn=conn, audit_id=audit_id)
    if dto is None:
        raise HTTPException(status_code=404, detail="audit event not found")
    return dto.model_dump(mode="json")


# ---------------------------------------------------------------------------
# /api/v1/queries
# ---------------------------------------------------------------------------


@router.get("/queries")
def list_query_traces(
    request: Request,
    cursor: str | None = Query(default=None),
    page_size: int = Query(default=25, ge=1, le=200),
    svc: AuditQueries = Depends(_audit_service),
    conn=Depends(_open_conn),
) -> dict[str, Any]:
    cursor = _validate_cursor(cursor, "query_traces")
    body = list_summaries_for_route(
        svc, conn=conn, method="query_traces",
        args={"cursor": cursor, "page_size": page_size},
    )
    return body.model_dump(mode="json")


@router.get("/queries/{trace_id}")
def query_trace_detail(
    request: Request,
    trace_id: str,
    svc: AuditQueries = Depends(_audit_service),
    conn=Depends(_open_conn),
) -> dict[str, Any]:
    dto = svc.query_trace_detail(conn=conn, trace_id=trace_id)
    if dto is None:
        raise HTTPException(status_code=404, detail="query trace not found")
    return dto.model_dump(mode="json")


# ---------------------------------------------------------------------------
# static UI: placeholder page until T15 lands
# ---------------------------------------------------------------------------


PLACEHOLDER_HTML = """<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\" />
    <title>memory-agent ui</title>
    <style>
      :root { color-scheme: light dark; }
      body {
        font-family: -apple-system, system-ui, sans-serif;
        margin: 4rem auto; max-width: 36rem; padding: 0 1.5rem;
        line-height: 1.55; color: #20304a; background: #f5f7fa;
      }
      @media (prefers-color-scheme: dark) {
        body { color: #e5ebf3; background: #182230; }
        code { background: #223044; color: #e5ebf3; }
        a { color: #a6c9f5; }
      }
      h1 { font-weight: 600; font-size: 1.4rem; margin-bottom: 0.5rem; }
      .pill { display: inline-block; padding: 0.15rem 0.5rem;
              border-radius: 999px; background: #315d92; color: white;
              font-size: 0.8rem; }
      pre { background: #fff; padding: 0.75rem; border-radius: 6px;
            overflow: auto; border: 1px solid #d6dee8; }
    </style>
  </head>
  <body>
    <h1><span class=\"pill\">placeholder</span> memory-agent ui</h1>
    <p>The T15 React UI is not yet built. The HTTP API below this page is the
    P5/P6 read/write boundary; you can hit it directly with curl.</p>
    <p>Try one of:</p>
    <pre>curl http://127.0.0.1:{port}/api/v1/overview
curl http://127.0.0.1:{port}/api/v1/sources
curl http://127.0.0.1:{port}/api/v1/batches
curl http://127.0.0.1:{port}/api/v1/audit
curl http://127.0.0.1:{port}/api/v1/conflicts</pre>
    <p>Write endpoints (POST <code>/api/v1/commands</code>) require a
    session token in <code>X-Memory-Agent-Token</code> and an
    <code>Origin</code> matching the configured allowlist.</p>
  </body>
</html>
"""


@router.get("/", include_in_schema=False)
def placeholder_page(request: Request) -> Response:
    port = getattr(request.app.state, "port", 8420)
    body = PLACEHOLDER_HTML.replace("{port}", str(port))
    return Response(content=body, media_type="text/html")


# Second, root-level router for the placeholder page so it lives at
# ``/`` rather than ``/api/v1/``. The prefix on ``router`` above is
# reserved for the audit / job endpoints.
root_router = APIRouter()


@root_router.get("/", include_in_schema=False)
def root_placeholder_page(request: Request) -> Response:
    port = getattr(request.app.state, "port", 8420)
    body = PLACEHOLDER_HTML.replace("{port}", str(port))
    return Response(content=body, media_type="text/html")


def serve_ui_dist(ui_dist: Path | None) -> Any:
    """Mount ``/`` to the packaged UI when present.

    The fallback placeholder page is the default; T15 will replace it
    with a real Vite build. We keep the placeholder so a freshly
    installed ``memory ui`` does not 404 on the root URL.
    """

    if ui_dist is None or not ui_dist.exists() or not ui_dist.is_dir():
        return None
    index = ui_dist / "index.html"
    if not index.exists():
        return None

    from starlette.staticfiles import StaticFiles

    try:
        return StaticFiles(directory=str(ui_dist), html=True, check_dir=True)
    except Exception:
        return None


__all__ = ["router", "root_router", "serve_ui_dist"]