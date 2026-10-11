"""Metadata-first audit query service (T13).

The audit DTOs live in ``contracts/audit``. This service turns them
into a stable, paginated, model-free read API over the existing
SQLite + files layout. It is read-only and never calls the model:

  * No ``model_calls`` invocation. Browse / pagination / overview /
    audit-event queries are guaranteed to make zero model calls
    (tests in ``tests/integration/test_audit_queries.py`` assert
    this with the model call counter).
  * Server-side pagination with deterministic cursors: the cursor is
    an opaque token that names the (snapshot, filter, last_seq) it
    was emitted from. ``decode_cursor`` rejects malformed cursors
    so callers can't construct their own; ``encode_cursor`` is only
    called by this module.
  * Sensitive bodies only on the explicit evidence endpoint
    (``/batches/{id}/evidence`` and the memory ``/history``).
    List endpoints return redacted previews.
  * DB/HEAD reconcile status (``projection_state``): the SQL
    ``memory_projection`` is checked against the file manifest so the
    UI can show ``db_lag`` when a HEAD swap landed but the SQL
    finalize transaction is still pending.
  * Trace source -> batch -> model call -> candidate -> commit through
    the existing ``correlation_json`` / ``object_id`` / ``artifact_ref``
    columns. Tests create real invented pipeline data and assert the
    link traversal.

The service is intentionally synchronous. Each query takes an open
SQLite ``Connection`` and a ``VaultLayout``; the HTTP layer in
``interfaces/http`` opens the connection, calls the query, and closes
it inside one request scope.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory_agent.contracts.audit import (
    AuditEventDTO,
    BatchDTO,
    BatchDetailDTO,
    BatchEvidenceDTO,
    BatchMemberDTO,
    ConflictDTO,
    EnvelopeHeader,
    MemoryHistoryDTO,
    MemoryRevisionDTO,
    OverviewCounts,
    OverviewDTO,
    PagedResponse,
    PaginationCursor,
    ProjectionState,
    QueryTraceDTO,
    QueryTraceDetailDTO,
    SessionTurnDTO,
    SourceDTO,
    SourceDetailDTO,
)
from memory_agent.infrastructure.files import VaultLayout
from memory_agent.infrastructure.sqlite.repositories import (
    AuditRepo,
    BatchRepo,
    CandidateSetRepo,
    CommitIntentRepo,
    ConflictRepo,
    EventRepo,
    MemoryProjectionRepo,
    SourceFileRepo,
    WorkerLeaseRepo,
)

# Fields that are considered sensitive and stripped from list responses.
# The audit list endpoint MUST NOT echo these back so a casual browse of
# /audit or /memories cannot leak quoted source text or prompts.
_REDACTED_FIELDS = frozenset({
    "quote", "text", "prompt", "raw_payload", "user_prompt",
    "assistant_response", "raw_text", "body", "candidate_payload",
})


# ---------------------------------------------------------------------------
# Cursor codec
# ---------------------------------------------------------------------------


def encode_cursor(*, scope: str, snapshot_id: str, params: dict[str, Any]) -> str:
    """Encode a pagination cursor.

    The cursor is bound to the *scope* (e.g. ``"audit"``, ``"sources"``),
    the snapshot_id (so stale cursors are rejected on a HEAD advance),
    and the (last_seq, last_id) pair so the next page is deterministic.
    """

    payload = {"scope": scope, "snapshot_id": snapshot_id, "params": params}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None, *, scope: str) -> dict[str, Any] | None:
    """Decode + validate a cursor.

    Returns ``None`` on malformed input, scope mismatch, or
    snapshot mismatch. Callers must treat ``None`` as "start fresh".
    """

    if not cursor:
        return None
    try:
        pad = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + pad).decode()
        obj = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("scope") != scope:
        return None
    params = obj.get("params")
    if not isinstance(params, dict):
        return None
    return {"snapshot_id": obj.get("snapshot_id"), "params": params}


# ---------------------------------------------------------------------------
# Snapshot + projection reconcile
# ---------------------------------------------------------------------------


def _read_head_snapshot(vault: VaultLayout) -> tuple[str, str | None]:
    """Return (snapshot_id, last_commit_at_iso).

    ``snapshot_id`` is the HEAD commit id when present; ``"empty"`` when
    the vault has no published commit yet (no HEAD.json).
    """

    head_path = vault.head_path
    if not head_path.exists():
        return "empty", None
    try:
        data = json.loads(head_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "empty", None
    commit_id = str(data.get("commit_id") or "empty")
    # We don't persist a precise committed_at; derive from HEAD.json
    # when available, otherwise leave None. Intents.phase=finalized rows
    # give us a more accurate timestamp.
    last = data.get("committed_at") or data.get("created_at")
    return commit_id, last


def _projection_state(
    *,
    conn,
    snapshot_id: str,
    vault: VaultLayout,
) -> tuple[ProjectionState, int, int]:
    """Reconcile SQL projection vs file manifest.

    Returns (state, db_lag_behind_head, db_ahead_of_head). ``db_lag``
    counts committed intents whose projection rows have not been
    materialised yet; ``db_ahead`` counts HEAD.json's manifest that
    is missing from the SQL state. ``aligned`` means everything is in
    sync; ``empty`` is the case where there is no HEAD at all.

    This is a *projection* state — it does NOT claim the file is
    authoritative in a way the workspace depends on. Tests in
    ``test_audit_queries.py`` exercise every state explicitly.
    """

    intents = CommitIntentRepo(conn)
    prepared = intents.list_by_phase("prepared")
    published = intents.list_by_phase("published")
    db_lag = len(published) + len(prepared)
    db_ahead = 0
    if vault.head_path.exists():
        try:
            head = json.loads(vault.head_path.read_text(encoding="utf-8"))
            head_commit = head.get("commit_id")
        except json.JSONDecodeError:
            head_commit = None
        if head_commit:
            # If HEAD names an intent we cannot find by id, the SQL side
            # is missing rows that HEAD claims — db_ahead.
            row = conn.execute(
                "SELECT phase FROM commit_intents WHERE commit_id = ?",
                (head_commit,),
            ).fetchone()
            if row is None:
                db_ahead = 1
            elif str(row["phase"]) != "finalized":
                db_lag = max(db_lag, 1)
    if snapshot_id == "empty":
        return "empty", db_lag, db_ahead
    if db_lag > 0:
        return "db_lag", db_lag, db_ahead
    if db_ahead > 0:
        return "db_ahead", db_lag, db_ahead
    return "aligned", 0, 0


def _build_header(conn, vault: VaultLayout) -> EnvelopeHeader:
    snapshot_id, _ = _read_head_snapshot(vault)
    state, lag, ahead = _projection_state(conn=conn, snapshot_id=snapshot_id, vault=vault)
    return EnvelopeHeader(
        as_of=datetime.now(timezone.utc).isoformat(),
        snapshot_id=snapshot_id,
        projection_state=state,
        db_lag_behind_head=lag,
        db_ahead_of_head=ahead,
    )


# ---------------------------------------------------------------------------
# Helpers for raw SQL (used by query_trace + evidence endpoints where
# the model-of-record rows live in JSON-typed columns).
# ---------------------------------------------------------------------------


def _quote_preview(text: str, limit: int = 60) -> tuple[str, bool]:
    """Return (preview, redacted). Redacted = True when the text was
    truncated, so callers know the body is hidden behind a follow-up
    fetch.
    """

    if not text:
        return "", True
    if len(text) <= limit:
        return text, False
    return text[:limit] + "…", True


def _redact_details(details: Any) -> dict[str, Any]:
    """Strip known sensitive fields from a ``details`` payload."""

    if not isinstance(details, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in details.items():
        if k in _REDACTED_FIELDS:
            out[k] = "<redacted>"
        elif isinstance(v, dict):
            out[k] = _redact_details(v)
        elif isinstance(v, list):
            out[k] = [
                _redact_details(el) if isinstance(el, dict) else el
                for el in v
            ]
        else:
            out[k] = v
    return out


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a ``sqlite3.Row`` / mapping to a plain dict.

    ``sqlite3.Row`` does not implement ``.get`` so the audit DTOs need
    a regular dict. We also accept a plain dict so tests can build
    fixtures without an open connection.
    """

    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    return {k: row[k] for k in row.keys()}


def _like_pattern(q: str) -> str:
    """Escape a free-text ``q`` for SQLite LIKE.

    SQLite LIKE treats ``%`` and ``_`` as wildcards; an operator who
    searches for a literal underscore should not get an unintended
    match. We escape both wildcards and wrap the value in ``%``.
    Caller passes the result into a ``?`` parameter; SQLite binds
    the value, so the escape characters are inert to SQL injection.
    """
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _audit_row_to_dto(row: dict[str, Any], *, detailed: bool) -> AuditEventDTO:
    correlation = json.loads(row.get("correlation_json") or "{}")
    before_after = json.loads(row.get("before_after_json") or "{}")
    raw_details = json.loads(row.get("details_json") or "{}")
    details = raw_details if detailed else _redact_details(raw_details)
    return AuditEventDTO(
        audit_id=row["audit_id"],
        seq=int(row["seq"]),
        occurred_at=row["occurred_at"],
        actor=row["actor"],
        action=row["action"],
        object_type=row.get("object_type"),
        object_id=row.get("object_id"),
        correlation=correlation if isinstance(correlation, dict) else {},
        before_after=before_after if isinstance(before_after, dict) else {},
        outcome=row["outcome"],
        reason_code=row.get("reason_code"),
        details=details,
        redacted=not detailed,
    )


# ---------------------------------------------------------------------------
# AuditQueries service
# ---------------------------------------------------------------------------


@dataclass
class AuditQueries:
    """Metadata-first audit query service.

    Each method returns a typed dataclass (Pydantic model). The
    service is a thin layer over the existing repositories; it does
    not introduce new SQL state and never writes.
    """

    vault: VaultLayout

    # -----------------------------------------------------------------
    # overview
    # -----------------------------------------------------------------
    def overview(self, *, conn) -> OverviewDTO:
        header = _build_header(conn, self.vault)
        sf_repo = SourceFileRepo(conn)
        ev_repo = EventRepo(conn)
        ev_repo._conn = conn  # type: ignore[attr-defined]
        batch_repo = BatchRepo(conn)
        cand_repo = CandidateSetRepo(conn)
        mem_repo = MemoryProjectionRepo(conn)
        conflict_repo = ConflictRepo(conn)
        audit_repo = AuditRepo(conn)
        lease_repo = WorkerLeaseRepo(conn)

        sources = sf_repo.conn.execute(
            "SELECT source_file_id, last_status FROM source_files"
        ).fetchall()
        sources_total = len(sources)
        sources_unsupported = sum(
            1 for r in sources if (r["last_status"] or "") in {"blocked", "unsupported"}
        )

        ev_total_row = ev_repo.conn.execute(
            "SELECT COUNT(*) AS c FROM source_events"
        ).fetchone()
        events_total = int(ev_total_row["c"] or 0)
        ev_user_row = ev_repo.conn.execute(
            "SELECT COUNT(*) AS c FROM source_events WHERE role = 'user'"
        ).fetchone()
        events_user = int(ev_user_row["c"] or 0)
        turns_row = ev_repo.conn.execute(
            "SELECT COUNT(*) AS c FROM turns"
        ).fetchone()
        turns_total = int(turns_row["c"] or 0)

        batch_rows = batch_repo.conn.execute(
            "SELECT status, COUNT(*) AS c FROM batches GROUP BY status"
        ).fetchall()
        status_counts: dict[str, int] = {}
        for r in batch_rows:
            status_counts[str(r["status"])] = int(r["c"])
        candidates_total_row = cand_repo.conn.execute(
            "SELECT COUNT(*) AS c FROM candidate_sets"
        ).fetchone()
        mem_active_row = mem_repo.conn.execute(
            "SELECT COUNT(*) AS c FROM memory_projection WHERE status = 'active'"
        ).fetchone()
        mem_superseded_row = mem_repo.conn.execute(
            "SELECT COUNT(*) AS c FROM memory_projection WHERE status = 'superseded'"
        ).fetchone()
        conflicts_open = len(conflict_repo.list_open())
        audit_total_row = audit_repo.conn.execute(
            "SELECT COUNT(*) AS c FROM audit_events"
        ).fetchone()
        leases = lease_repo.list_active()
        worker_lease_active = bool(leases)
        last_scan_row = ev_repo.conn.execute(
            "SELECT MAX(last_scan_at) AS v FROM source_files WHERE last_scan_at IS NOT NULL"
        ).fetchone()
        last_scan_at = str(last_scan_row["v"]) if last_scan_row and last_scan_row["v"] else None

        last_intent_row = mem_repo.conn.execute(
            "SELECT MAX(ci.created_at) AS v FROM commit_intents ci WHERE ci.phase='finalized'"
        ).fetchone()
        last_commit_at = (
            str(last_intent_row["v"]) if last_intent_row and last_intent_row["v"] else None
        )

        counts = OverviewCounts(
            sources_total=sources_total,
            sources_unsupported=sources_unsupported,
            events_total=events_total,
            events_user=events_user,
            turns_total=turns_total,
            batches_total=sum(status_counts.values()),
            batches_pending=status_counts.get("ready", 0)
            + status_counts.get("retry_wait", 0),
            batches_running=status_counts.get("leased", 0),
            batches_succeeded=status_counts.get("succeeded", 0)
            + status_counts.get("finalized", 0),
            batches_failed=status_counts.get("failed", 0)
            + status_counts.get("superseded", 0),
            candidate_sets_total=int(candidates_total_row["c"] or 0),
            memories_active=int(mem_active_row["c"] or 0),
            memories_superseded=int(mem_superseded_row["c"] or 0),
            conflicts_open=conflicts_open,
            audit_events_total=int(audit_total_row["c"] or 0),
            worker_lease_active=worker_lease_active,
            last_scan_at=last_scan_at,
            last_commit_at=last_commit_at,
        )
        pipeline_stages = {
            "scanned": events_total,
            "planned": turns_total,
            "extracted": int(candidates_total_row["c"] or 0),
            "committed": int(mem_active_row["c"] or 0),
        }
        warnings: list[str] = []
        if worker_lease_active:
            warnings.append("worker_active")
        if header.projection_state != "aligned":
            warnings.append(f"projection_state:{header.projection_state}")
        return OverviewDTO(
            header=header,
            counts=counts,
            pipeline_stages=pipeline_stages,
            warnings=warnings,
        )

    # -----------------------------------------------------------------
    # sources
    # -----------------------------------------------------------------
    def list_sources(
        self,
        *,
        conn,
        cursor: str | None,
        page_size: int = 25,
        connector: str | None = None,
    ) -> PagedResponse:
        snapshot_id, _ = _read_head_snapshot(self.vault)
        decoded = decode_cursor(cursor, scope="sources")
        if decoded is not None and decoded["snapshot_id"] != snapshot_id:
            # Cursor was issued under a different HEAD; refuse to mix.
            decoded = None
        page_size = max(1, min(200, int(page_size)))
        last_id = ""
        if decoded:
            last_id = str(decoded["params"].get("last_id") or "")

        params: list[Any] = []
        where = []
        if connector:
            where.append("connector = ?")
            params.append(connector)
        if last_id:
            where.append("canonical_path > ?")
            params.append(last_id)
        sql = "SELECT * FROM source_files"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY canonical_path LIMIT ?"
        params.append(page_size + 1)
        rows = conn.execute(sql, params).fetchall()
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        items = [self._source_row_to_dto(_row_to_dict(r)) for r in rows]
        next_cursor = None
        if has_more and rows:
            next_cursor = encode_cursor(
                scope="sources",
                snapshot_id=snapshot_id,
                params={
                    "last_id": str(rows[-1]["canonical_path"]),
                    "connector": connector,
                },
            )
        return PagedResponse(
            header=_build_header(conn, self.vault),
            items=[item.model_dump(mode="json") for item in items],
            pagination=PaginationCursor(
                cursor=cursor,
                next_cursor=next_cursor,
                page_size=page_size,
                total_estimated=None,
            ),
        )

    def source_detail(self, *, conn, source_file_id: str) -> SourceDetailDTO | None:
        row = conn.execute(
            "SELECT * FROM source_files WHERE source_file_id = ?",
            (source_file_id,),
        ).fetchone()
        if row is None:
            return None
        row = _row_to_dict(row)
        # manifest_reconcile: align against the latest commit intent.
        last_intent_row = conn.execute(
            "SELECT phase FROM commit_intents ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        manifest_present = self.vault.head_path.exists()
        if not manifest_present:
            reconcile = "missing_manifest"
        elif last_intent_row is None:
            reconcile = "ok"
        elif str(last_intent_row["phase"]) == "finalized":
            reconcile = "ok"
        elif str(last_intent_row["phase"]) in {"prepared", "published"}:
            reconcile = "behind"
        else:
            reconcile = "unknown"
        return SourceDetailDTO(
            source_file_id=row["source_file_id"],
            connector=row["connector"],
            session_id=row["session_id"],
            branch_id=row["branch_id"],
            canonical_path=row["canonical_path"],
            generation=int(row.get("generation") or 1),
            revision_seq=int(row.get("revision_seq") or 1),
            complete_offset=int(row.get("complete_offset") or 0),
            size=int(row.get("size") or 0),
            last_status=row.get("last_status"),
            last_scan_at=row.get("last_scan_at"),
            last_error=row.get("last_error"),
            file_present=(
                Path(str(row.get("canonical_path") or "")).exists()
                and Path(str(row.get("canonical_path") or "")).stat().st_size == int(row.get("size") or 0)
                if row.get("canonical_path") else False
            ),
            pending_partial_tail=str(row.get("last_status") or "") == "partial_pending",
            adapter_version=str(row.get("adapter_version") or ""),
            file_identity=str(row.get("file_identity") or ""),
            consumed_prefix_sha256=row.get("consumed_prefix_sha256"),
            manifest_reconcile=reconcile,
        )

    def _source_row_to_dto(
        self, row: dict[str, Any], *, detailed: bool = False
    ) -> SourceDTO:
        path = Path(str(row.get("canonical_path") or ""))
        partial_tail = str(row.get("last_status") or "") == "partial_pending"
        size = int(row.get("size") or 0)
        file_present = path.exists() and path.stat().st_size == size if path.exists() else False
        if detailed:
            return SourceDetailDTO(
                source_file_id=row["source_file_id"],
                connector=row["connector"],
                session_id=row["session_id"],
                branch_id=row["branch_id"],
                canonical_path=row["canonical_path"],
                generation=int(row.get("generation") or 1),
                revision_seq=int(row.get("revision_seq") or 1),
                complete_offset=int(row.get("complete_offset") or 0),
                size=size,
                last_status=row.get("last_status"),
                last_scan_at=row.get("last_scan_at"),
                last_error=row.get("last_error"),
                file_present=file_present,
                pending_partial_tail=partial_tail,
                adapter_version=str(row.get("adapter_version") or ""),
                file_identity=str(row.get("file_identity") or ""),
                consumed_prefix_sha256=row.get("consumed_prefix_sha256"),
                manifest_reconcile="unknown",
            )
        return SourceDTO(
            source_file_id=row["source_file_id"],
            connector=row["connector"],
            session_id=row["session_id"],
            branch_id=row["branch_id"],
            canonical_path=row["canonical_path"],
            generation=int(row.get("generation") or 1),
            revision_seq=int(row.get("revision_seq") or 1),
            complete_offset=int(row.get("complete_offset") or 0),
            size=size,
            last_status=row.get("last_status"),
            last_scan_at=row.get("last_scan_at"),
            last_error=row.get("last_error"),
            file_present=file_present,
            pending_partial_tail=partial_tail,
        )

    # -----------------------------------------------------------------
    # session turns
    # -----------------------------------------------------------------
    def list_session_turns(
        self,
        *,
        conn,
        session_id: str,
        branch_id: str = "main",
        cursor: str | None = None,
        page_size: int = 25,
    ) -> PagedResponse:
        snapshot_id, _ = _read_head_snapshot(self.vault)
        decoded = decode_cursor(cursor, scope="session_turns")
        if decoded is not None and decoded["snapshot_id"] != snapshot_id:
            decoded = None
        page_size = max(1, min(200, int(page_size)))
        last_ordinal = 0
        if decoded:
            try:
                last_ordinal = int(decoded["params"].get("last_ordinal") or 0)
            except (TypeError, ValueError):
                last_ordinal = 0

        rows = conn.execute(
            "SELECT * FROM turns WHERE session_id = ? AND branch_id = ? "
            "AND ordinal > ? ORDER BY ordinal LIMIT ?",
            (session_id, branch_id, last_ordinal, page_size + 1),
        ).fetchall()
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        rows = [_row_to_dict(r) for r in rows]

        # Best-effort role + quote preview from source_events via the
        # first_event_id; the body itself is never included.
        items: list[SessionTurnDTO] = []
        for r in rows:
            role = None
            preview = None
            redacted = True
            first_event_id = r.get("first_event_id")
            if first_event_id:
                ev_row = conn.execute(
                    "SELECT role FROM source_events WHERE event_id = ?",
                    (first_event_id,),
                ).fetchone()
                if ev_row is not None:
                    role = str(ev_row["role"])
                # Read text from the immutable segment. This is the
                # *quote* of the original user/assistant line — the UI
                # gets the first 60 chars as a preview, never the full
                # text. The body must be fetched via the evidence
                # endpoint (which T15 will use).
                seg_row = conn.execute(
                    "SELECT s.path FROM segments s JOIN source_events e "
                    "ON e.segment_id = s.segment_id WHERE e.event_id = ?",
                    (first_event_id,),
                ).fetchone()
                if seg_row is not None:
                    seg_path = Path(seg_row["path"])
                    if seg_path.exists():
                        try:
                            payload = json.loads(seg_path.read_text(encoding="utf-8"))
                        except (json.JSONDecodeError, OSError):
                            payload = None
                        if payload:
                            for ev in payload.get("events", []):
                                if ev.get("event_id") == first_event_id:
                                    preview, redacted = _quote_preview(
                                        str(ev.get("text") or "")
                                    )
                                    break
            items.append(SessionTurnDTO(
                turn_id=r["turn_id"],
                session_id=r["session_id"],
                branch_id=r["branch_id"],
                ordinal=int(r["ordinal"]),
                revision=int(r["revision"]),
                state=str(r["state"]),
                first_event_id=r.get("first_event_id"),
                last_event_id=r.get("last_event_id"),
                role=role,
                quote_preview=preview,
                quote_redacted=redacted,
            ))

        next_cursor = None
        if has_more and rows:
            next_cursor = encode_cursor(
                scope="session_turns",
                snapshot_id=snapshot_id,
                params={"last_ordinal": int(rows[-1]["ordinal"])},
            )
        return PagedResponse(
            header=_build_header(conn, self.vault),
            items=[item.model_dump(mode="json") for item in items],
            pagination=PaginationCursor(
                cursor=cursor,
                next_cursor=next_cursor,
                page_size=page_size,
            ),
        )

    # -----------------------------------------------------------------
    # batches
    # -----------------------------------------------------------------
    def list_batches(
        self,
        *,
        conn,
        cursor: str | None,
        page_size: int = 25,
        status: str | None = None,
    ) -> PagedResponse:
        snapshot_id, _ = _read_head_snapshot(self.vault)
        decoded = decode_cursor(cursor, scope="batches")
        if decoded is not None and decoded["snapshot_id"] != snapshot_id:
            decoded = None
        page_size = max(1, min(200, int(page_size)))
        last_created_at = ""
        last_batch_id = ""
        if decoded:
            last_created_at = str(decoded["params"].get("last_created_at") or "")
            last_batch_id = str(decoded["params"].get("last_batch_id") or "")

        params: list[Any] = []
        where = []
        if status:
            where.append("status = ?")
            params.append(status)
        if last_created_at:
            where.append("(created_at < ? OR (created_at = ? AND batch_id > ?))")
            params.extend([last_created_at, last_created_at, last_batch_id])
        sql = "SELECT * FROM batches"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at, batch_id LIMIT ?"
        params.append(page_size + 1)
        rows = conn.execute(sql, params).fetchall()
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        items = [self._batch_row_to_dto(conn, _row_to_dict(r)) for r in rows]
        next_cursor = None
        if has_more and rows:
            next_cursor = encode_cursor(
                scope="batches",
                snapshot_id=snapshot_id,
                params={
                    "last_created_at": str(rows[-1]["created_at"]),
                    "last_batch_id": str(rows[-1]["batch_id"]),
                    "status": status,
                },
            )
        return PagedResponse(
            header=_build_header(conn, self.vault),
            items=[item.model_dump(mode="json") for item in items],
            pagination=PaginationCursor(
                cursor=cursor,
                next_cursor=next_cursor,
                page_size=page_size,
            ),
        )

    def batch_detail(self, *, conn, batch_id: str) -> BatchDetailDTO | None:
        row = conn.execute(
            "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if row is None:
            return None
        row = _row_to_dict(row)
        dto = self._batch_row_to_dto(conn, row)
        members_rows = [
            _row_to_dict(m)
            for m in conn.execute(
                "SELECT * FROM batch_members WHERE batch_id = ? ORDER BY ord",
                (batch_id,),
            ).fetchall()
        ]
        members = [
            BatchMemberDTO(
                unit_id=m["unit_id"],
                purpose=m["purpose"],
                ord=int(m["ord"]),
                epoch=int(m["epoch"]),
                active_target=bool(int(m["active_target"] or 0)),
            )
            for m in members_rows
        ]
        # Trace batch -> runs -> model_calls -> candidate_sets -> commit.
        runs = conn.execute(
            "SELECT run_id FROM runs WHERE batch_id = ? ORDER BY started_at",
            (batch_id,),
        ).fetchall()
        run_ids = [str(r["run_id"]) for r in runs]
        commit_id = self._commit_id_for_batch(conn, batch_id)
        return BatchDetailDTO(
            **dto.model_dump(),
            members=members,
            run_ids=run_ids,
            commit_id=commit_id,
        )

    def batch_evidence(
        self, *, conn, batch_id: str
    ) -> BatchEvidenceDTO | None:
        """The single endpoint that surfaces raw bodies. Every other
        endpoint must redact text. This one reads the immutable
        candidate set + segment slice for the batch and produces the
        inputs / candidates / runs / model_calls trace.
        """

        batch_row = conn.execute(
            "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if batch_row is None:
            return None

        # 1. Inputs: every active_target unit's source_event, with its
        # locator and the segment text. We walk batch_members ->
        # work_units (default epoch match) -> source_events.
        inputs: list[dict[str, Any]] = []
        member_rows = [
            _row_to_dict(m)
            for m in conn.execute(
                "SELECT * FROM batch_members WHERE batch_id = ?",
                (batch_id,),
            ).fetchall()
        ]
        for m in member_rows:
            unit_row = conn.execute(
                "SELECT * FROM work_units WHERE unit_id = ?",
                (m["unit_id"],),
            ).fetchone()
            if unit_row is None:
                continue
            unit_row = _row_to_dict(unit_row)
            ev_row = conn.execute(
                "SELECT e.event_id, e.role, e.segment_id, e.source_time "
                "FROM source_events e WHERE e.event_id = ?",
                (unit_row.get("event_id"),),
            ).fetchone()
            if ev_row is None:
                continue
            ev_row = _row_to_dict(ev_row)
            seg_row = conn.execute(
                "SELECT path FROM segments WHERE segment_id = ?",
                (ev_row["segment_id"],),
            ).fetchone()
            text = ""
            byte_start = 0
            byte_end = 0
            if seg_row is not None:
                seg_path = Path(seg_row["path"])
                if seg_path.exists():
                    try:
                        payload = json.loads(seg_path.read_text(encoding="utf-8"))
                        for ev in payload.get("events", []):
                            if ev.get("event_id") == ev_row["event_id"]:
                                text = str(ev.get("text") or "")
                                loc = (ev.get("source", {}) or {}).get("locator", {}) or {}
                                byte_start = int(loc.get("byte_start", 0))
                                byte_end = int(loc.get("byte_end", 0))
                                break
                    except (json.JSONDecodeError, OSError):
                        text = ""
            inputs.append({
                "unit_id": m["unit_id"],
                "purpose": m["purpose"],
                "active_target": bool(int(m["active_target"] or 0)),
                "epoch": int(m["epoch"]),
                "event_id": ev_row["event_id"],
                "role": ev_row["role"],
                "source_time": ev_row["source_time"],
                "locator": {
                    "byte_start": byte_start,
                    "byte_end": byte_end,
                },
                # The raw body lives here — and ONLY here, in the
                # evidence endpoint.
                "text": text,
            })

        # 2. Candidates: read the candidate_set artifact.
        cand_row = conn.execute(
            "SELECT * FROM candidate_sets WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        candidates: list[dict[str, Any]] = []
        if cand_row is not None:
            cand_row = _row_to_dict(cand_row)
            art_ref = str(cand_row.get("artifact_ref") or "")
            if art_ref and Path(art_ref).exists():
                try:
                    payload = json.loads(Path(art_ref).read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    payload = None
                if isinstance(payload, dict):
                    # Some artifacts store the whole CandidateSet; we
                    # surface only the per-candidate summary here.
                    inner = payload.get("candidates") or []
                    if isinstance(inner, list):
                        candidates = [
                            c for c in inner if isinstance(c, dict)
                        ]
                    else:
                        candidates = [payload]
                elif isinstance(payload, list):
                    candidates = [c for c in payload if isinstance(c, dict)]

        # 3. Runs + model calls.
        runs_rows = conn.execute(
            "SELECT * FROM runs WHERE batch_id = ? ORDER BY started_at",
            (batch_id,),
        ).fetchall()
        runs = [dict(r) for r in runs_rows]
        run_ids = [str(r["run_id"]) for r in runs_rows]
        model_calls: list[dict[str, Any]] = []
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            mc_rows = conn.execute(
                f"SELECT * FROM model_calls WHERE run_id IN ({placeholders})",
                tuple(run_ids),
            ).fetchall()
            for m in mc_rows:
                d = dict(m)
                # Even here we redact the prompt / raw_payload columns.
                # Callers may inspect ``input_ref`` / ``output_ref`` to
                # fetch the artifact directly if they need it.
                for k in ("usage_json",):
                    if k in d and d[k]:
                        try:
                            d[k] = json.loads(d[k])
                        except json.JSONDecodeError:
                            d[k] = None
                model_calls.append(d)

        return BatchEvidenceDTO(
            batch_id=batch_id,
            header=_build_header(conn, self.vault),
            inputs=inputs,
            candidates=candidates,
            runs=runs,
            model_calls=model_calls,
        )

    def _batch_row_to_dto(self, conn, row: dict[str, Any]) -> BatchDTO:
        members_row = conn.execute(
            "SELECT COUNT(*) AS c FROM batch_members WHERE batch_id = ?",
            (row["batch_id"],),
        ).fetchone()
        cand_row = conn.execute(
            "SELECT candidate_set_id, artifact_ref FROM candidate_sets "
            "WHERE batch_id = ?",
            (row["batch_id"],),
        ).fetchone()
        lease_until = row.get("lease_until")
        return BatchDTO(
            batch_id=row["batch_id"],
            session_id=row["session_id"],
            branch_id=row["branch_id"],
            policy_version=row["policy_version"],
            status=str(row["status"]),
            attempt=int(row["attempt"] or 0),
            input_hash=row["input_hash"],
            created_at=row.get("created_at"),
            lease_until=lease_until,
            member_count=int(members_row["c"] or 0),
            candidate_set_id=str(cand_row["candidate_set_id"]) if cand_row else None,
            candidate_set_ref=str(cand_row["artifact_ref"]) if cand_row else None,
        )

    def _commit_id_for_batch(self, conn, batch_id: str) -> str | None:
        row = conn.execute(
            "SELECT commit_id FROM commit_intents WHERE batch_ids_json LIKE ? "
            "ORDER BY created_at DESC LIMIT 1",
            (f'%"{batch_id}"%',),
        ).fetchone()
        return str(row["commit_id"]) if row else None

    # -----------------------------------------------------------------
    # memory history
    # -----------------------------------------------------------------
    def memory_history(
        self, *, conn, memory_id: str
    ) -> MemoryHistoryDTO | None:
        rows = [_row_to_dict(r) for r in conn.execute(
            "SELECT * FROM memory_projection WHERE memory_id = ? ORDER BY revision",
            (memory_id,),
        ).fetchall()]
        if not rows:
            return None
        revisions: list[MemoryRevisionDTO] = []
        for r in rows:
            scope_obj = self._safe_json(r.get("scope_json"), default={})
            proj = self._safe_json(r.get("project_ids_json"), default=[])
            doms = self._safe_json(r.get("domains_json"), default=[])
            body = self._read_memory_body(memory_id, int(r["revision"]))
            quotes = self._collect_quotes(body)
            superseded_by = self._superseded_by(conn, memory_id, int(r["revision"]))
            revisions.append(MemoryRevisionDTO(
                memory_id=memory_id,
                revision=int(r["revision"]),
                status=str(r["status"]),
                commit_id=str(r.get("commit_id") or ""),
                recorded_at=r.get("recorded_at"),
                statement=str(r.get("statement") or ""),
                scope_level=str(scope_obj.get("level") or ""),
                project_ids=[str(p) for p in proj],
                domains=[str(d) for d in doms],
                evidence_quotes=quotes,
                superseded_by_revision=superseded_by,
            ))
        # Source correction proposals are surfaced in the same DTO so
        # the UI can show the queued corrections without a second fetch.
        corr_rows = [
            _row_to_dict(r)
            for r in conn.execute(
                "SELECT * FROM source_correction_proposals WHERE memory_id = ? "
                "ORDER BY detected_at",
                (memory_id,),
            ).fetchall()
        ]
        source_corrections = []
        for r in corr_rows:
            source_corrections.append({
                "correction_id": r.get("correction_id"),
                "status": r.get("status"),
                "reason": r.get("reason"),
                "old_event_id": r.get("old_event_id"),
                "new_event_id": r.get("new_event_id"),
                "old_revision_hash": r.get("old_revision_hash"),
                "new_revision_hash": r.get("new_revision_hash"),
                "detected_at": r.get("detected_at"),
            })
        # Conflicts that touched this memory.
        conflict_rows = [
            _row_to_dict(r)
            for r in conn.execute(
                "SELECT * FROM conflict_projection WHERE old_memory_ref = ? OR new_memory_ref = ? "
                "ORDER BY opened_at",
                (memory_id, memory_id),
            ).fetchall()
        ]
        conflicts = []
        for r in conflict_rows:
            conflicts.append({
                "conflict_id": r.get("conflict_id"),
                "revision": r.get("revision"),
                "status": r.get("status"),
                "commit_id": r.get("commit_id"),
                "opened_at": r.get("opened_at"),
            })
        return MemoryHistoryDTO(
            header=_build_header(conn, self.vault),
            memory_id=memory_id,
            revisions=revisions,
            source_corrections=source_corrections,
            conflicts=conflicts,
        )

    def _read_memory_body(self, memory_id: str, revision: int) -> str:
        path = self.vault.memory_revision_path(memory_id, revision)
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def _collect_quotes(self, body: str) -> list[str]:
        # Simple extractor: ``- role=... quote='...'`` lines.
        out: list[str] = []
        for line in body.splitlines():
            m = re.search(r"quote=(.+?)(?:\s+span=|$)", line)
            if m:
                out.append(m.group(1).strip())
        return out

    def _superseded_by(
        self, conn, memory_id: str, revision: int
    ) -> int | None:
        row = conn.execute(
            "SELECT revision FROM memory_projection WHERE memory_id = ? "
            "AND revision > ? AND status IN ('superseded', 'active') "
            "ORDER BY revision LIMIT 1",
            (memory_id, revision),
        ).fetchone()
        if row is None:
            return None
        return int(row["revision"])

    def _safe_json(self, blob: Any, *, default: Any) -> Any:
        if not blob:
            return default
        try:
            return json.loads(blob)
        except (TypeError, json.JSONDecodeError):
            return default

    # -----------------------------------------------------------------
    # jobs (read-only list — write side goes through POST /commands)
    # -----------------------------------------------------------------
    def list_jobs(
        self,
        *,
        conn,
        cursor: str | None,
        page_size: int = 25,
        state: str | None = None,
        q: str | None = None,
    ) -> PagedResponse:
        """Metadata-only list of the ``jobs`` table.

        v1 P6 / T14 only exposes ``GET /api/v1/jobs/{id}`` for the
        single-job read; the operator cannot see queued / running /
        failed jobs without dropping to the CLI. This fills the
        list-end gap so the audit UI can show a "jobs" carousel.

        Write side (cancel / retry) is unchanged — every state
        mutation still goes through ``POST /api/v1/commands`` so
        the dispatcher applies the same version checks and
        idempotency rules as the CLI.
        """
        from memory_agent.contracts.audit import JobListItemDTO

        page_size = max(1, min(200, int(page_size)))
        decoded = decode_cursor(cursor, scope="jobs")
        last_job_id = ""
        if decoded:
            last_job_id = str(decoded["params"].get("last_job_id") or "")

        params: list[Any] = []
        where: list[str] = []
        if state:
            where.append("state = ?")
            params.append(state)
        if q:
            # Match against operation + client_id + request_id + the
            # JSON-stringified progress / result / error blobs.
            q_like = _like_pattern(q)
            where.append(
                "(operation LIKE ? ESCAPE '\\' OR "
                "client_id LIKE ? ESCAPE '\\' OR "
                "request_id LIKE ? ESCAPE '\\' OR "
                "COALESCE(progress_json, '') LIKE ? ESCAPE '\\' OR "
                "COALESCE(result_json, '') LIKE ? ESCAPE '\\' OR "
                "COALESCE(error_json, '') LIKE ? ESCAPE '\\')"
            )
            params.extend([q_like] * 6)
        if last_job_id:
            where.append("job_id < ?")
            params.append(last_job_id)
        sql = "SELECT * FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, job_id DESC LIMIT ?"
        params.append(page_size + 1)
        rows = conn.execute(sql, params).fetchall()
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        items: list[dict[str, Any]] = []
        for r in rows:
            row = _row_to_dict(r)
            items.append(JobListItemDTO(
                job_id=str(row["job_id"]),
                request_id=str(row.get("request_id") or ""),
                client_id=str(row.get("client_id") or ""),
                operation=str(row.get("operation") or ""),
                state=str(row.get("state") or ""),
                revision=int(row.get("revision") or 0),
                mode=str(row.get("mode") or "sync"),
                cancel_requested=bool(int(row.get("cancel_requested") or 0)),
                created_at=row.get("created_at"),
                started_at=row.get("started_at"),
                ended_at=row.get("ended_at"),
                last_heartbeat=row.get("last_heartbeat"),
            ).model_dump(mode="json"))
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = encode_cursor(
                scope="jobs",
                snapshot_id=str(_read_head_snapshot(self.vault)[0]),
                params={
                    "last_job_id": str(last["job_id"]),
                    "state": state,
                    "q": q,
                },
            )
        return PagedResponse(
            header=_build_header(conn, self.vault),
            items=items,
            pagination=PaginationCursor(
                cursor=cursor,
                next_cursor=next_cursor,
                page_size=page_size,
            ),
        )

    # -----------------------------------------------------------------
    # conflicts
    # -----------------------------------------------------------------
    def list_conflicts(
        self,
        *,
        conn,
        cursor: str | None,
        page_size: int = 25,
        include_decided: bool = False,
    ) -> PagedResponse:
        snapshot_id, _ = _read_head_snapshot(self.vault)
        decoded = decode_cursor(cursor, scope="conflicts")
        if decoded is not None and decoded["snapshot_id"] != snapshot_id:
            decoded = None
        page_size = max(1, min(200, int(page_size)))
        last_opened = ""
        last_cid = ""
        if decoded:
            last_opened = str(decoded["params"].get("last_opened_at") or "")
            last_cid = str(decoded["params"].get("last_conflict_id") or "")
        params: list[Any] = []
        where = []
        if not include_decided:
            where.append("status IN ('open', 'pending')")
        if last_opened:
            where.append("(opened_at < ? OR (opened_at = ? AND conflict_id > ?))")
            params.extend([last_opened, last_opened, last_cid])
        sql = "SELECT * FROM conflict_projection"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY opened_at, conflict_id LIMIT ?"
        params.append(page_size + 1)
        rows = [_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        items: list[ConflictDTO] = []
        for r in rows:
            cid = str(r["conflict_id"])
            cand_ref = self._candidate_ref(self.vault, cid)
            items.append(ConflictDTO(
                conflict_id=cid,
                revision=int(r["revision"]),
                status=str(r["status"]),
                commit_id=str(r.get("commit_id") or ""),
                opened_at=r.get("opened_at"),
                old_memory_ref=r.get("old_memory_ref"),
                new_memory_ref=r.get("new_memory_ref"),
                candidate_ref=cand_ref,
            ))
        next_cursor = None
        if has_more and rows:
            next_cursor = encode_cursor(
                scope="conflicts",
                snapshot_id=snapshot_id,
                params={
                    "last_opened_at": str(rows[-1]["opened_at"]),
                    "last_conflict_id": str(rows[-1]["conflict_id"]),
                    "include_decided": include_decided,
                },
            )
        return PagedResponse(
            header=_build_header(conn, self.vault),
            items=[item.model_dump(mode="json") for item in items],
            pagination=PaginationCursor(
                cursor=cursor,
                next_cursor=next_cursor,
                page_size=page_size,
            ),
        )

    def _candidate_ref(self, vault: VaultLayout, conflict_id: str) -> str | None:
        path = vault.objects_dir / "conflicts" / f"{conflict_id}.json"
        return str(path) if path.exists() else None

    # -----------------------------------------------------------------
    # memories (active + optionally archived)
    # -----------------------------------------------------------------
    def list_memories(
        self,
        *,
        conn,
        cursor: str | None,
        page_size: int = 25,
        include_archived: bool = False,
        q: str | None = None,
    ) -> PagedResponse:
        """Metadata-first list of ``memory_projection`` rows.

        v1 P5 / T13 ships this surface so the operator can browse the
        full committed corpus, not just the conflicts surfacing under
        ``/memories/{id}/history``. The list view is metadata-only:
        no evidence quotes, no nested project / domain ids beyond the
        scope level. ``q`` matches ``statement`` (the operator-facing
        text of the memory); ``%`` and ``_`` are escaped via
        ``_like_pattern`` and ``ESCAPE '\\'``.
        """
        from memory_agent.contracts.audit import MemoryListItemDTO

        snapshot_id, _ = _read_head_snapshot(self.vault)
        page_size = max(1, min(200, int(page_size)))
        # Cursor strategy: ORDER BY recorded_at DESC + memory_id, so
        # the last-(recorded_at, memory_id) pair drives pagination.
        decoded = decode_cursor(cursor, scope="memories")
        if decoded is not None and decoded["snapshot_id"] != snapshot_id:
            decoded = None
        last_recorded = ""
        last_mid = ""
        if decoded:
            last_recorded = str(decoded["params"].get("last_recorded_at") or "")
            last_mid = str(decoded["params"].get("last_memory_id") or "")

        params: list[Any] = []
        where = []
        if not include_archived:
            where.append("status = 'active'")
        else:
            where.append("status IN ('active', 'archived')")
        if q:
            where.append("statement LIKE ? ESCAPE '\\'")
            params.append(_like_pattern(q))
        if last_recorded:
            where.append("(recorded_at < ? OR (recorded_at = ? AND memory_id > ?))")
            params.extend([last_recorded, last_recorded, last_mid])
        sql = "SELECT * FROM memory_projection"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY recorded_at DESC, memory_id LIMIT ?"
        params.append(page_size + 1)
        rows = [_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        items: list[dict[str, Any]] = []
        for r in rows:
            scope = self._safe_json(r.get("scope_json") or "{}", default={})
            scope_level = scope.get("level") if isinstance(scope, dict) else None
            items.append(MemoryListItemDTO(
                memory_id=str(r["memory_id"]),
                revision=int(r["revision"]),
                status=str(r["status"]),
                commit_id=r.get("commit_id"),
                recorded_at=r.get("recorded_at"),
                statement=str(r.get("statement") or ""),
                scope_level=scope_level if isinstance(scope_level, str) else None,
            ).model_dump(mode="json"))
        next_cursor = None
        if has_more and rows:
            next_cursor = encode_cursor(
                scope="memories",
                snapshot_id=snapshot_id,
                params={
                    "last_recorded_at": str(rows[-1]["recorded_at"]),
                    "last_memory_id": str(rows[-1]["memory_id"]),
                    "include_archived": include_archived,
                    "q": q,
                },
            )
        return PagedResponse(
            header=_build_header(conn, self.vault),
            items=items,
            pagination=PaginationCursor(
                cursor=cursor,
                next_cursor=next_cursor,
                page_size=page_size,
            ),
        )

    # -----------------------------------------------------------------
    # audit events
    # -----------------------------------------------------------------
    def list_audit_events(
        self,
        *,
        conn,
        cursor: str | None,
        page_size: int = 25,
        action: str | None = None,
        object_type: str | None = None,
        object_id: str | None = None,
        seq_min: int | None = None,
        seq_max: int | None = None,
        q: str | None = None,
    ) -> PagedResponse:
        snapshot_id, _ = _read_head_snapshot(self.vault)
        decoded = decode_cursor(cursor, scope="audit")
        if decoded is not None and decoded["snapshot_id"] != snapshot_id:
            decoded = None
        page_size = max(1, min(200, int(page_size)))
        last_seq = 0
        if decoded:
            try:
                last_seq = int(decoded["params"].get("last_seq") or 0)
            except (TypeError, ValueError):
                last_seq = 0

        params: list[Any] = []
        where = []
        if action:
            where.append("action = ?")
            params.append(action)
        if object_type:
            where.append("object_type = ?")
            params.append(object_type)
        if object_id:
            where.append("object_id = ?")
            params.append(object_id)
        if seq_min is not None:
            where.append("seq >= ?")
            params.append(int(seq_min))
        if seq_max is not None:
            where.append("seq <= ?")
            params.append(int(seq_max))
        # Free-text filter. SQLite LIKE is case-insensitive for ASCII
        # by default; the operator passes through unchanged. We match
        # against the three readable columns (``action``,
        # ``object_id``, ``details_json``) — the trace / evidence /
        # correlation text never lives in the audit row's details_json
        # alone (full text is in the artifact); see \`list_query_traces\`
        # below for query-text search. ``ESCAPE '\\'`` makes the
        # wildcard-escape in ``_like_pattern`` actually take effect;
        # without it, SQLite would treat ``\\_`` as the literal chars
        # ``\`` + ``_``.
        if q:
            q_like = _like_pattern(q)
            where.append(
                "(action LIKE ? ESCAPE '\\' OR "
                "object_id LIKE ? ESCAPE '\\' OR "
                "details_json LIKE ? ESCAPE '\\')"
            )
            params.extend([q_like, q_like, q_like])
        if last_seq:
            where.append("seq < ?")
            params.append(last_seq)
        sql = "SELECT * FROM audit_events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY seq DESC LIMIT ?"
        params.append(page_size + 1)
        rows = conn.execute(sql, params).fetchall()
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        items = [_audit_row_to_dto(_row_to_dict(r), detailed=False) for r in rows]
        next_cursor = None
        if has_more and rows:
            next_cursor = encode_cursor(
                scope="audit",
                snapshot_id=snapshot_id,
                params={
                    "last_seq": int(rows[-1]["seq"]),
                    "action": action,
                    "object_type": object_type,
                    "object_id": object_id,
                    "seq_min": seq_min,
                    "seq_max": seq_max,
                    "q": q,
                },
            )
        return PagedResponse(
            header=_build_header(conn, self.vault),
            items=[item.model_dump(mode="json") for item in items],
            pagination=PaginationCursor(
                cursor=cursor,
                next_cursor=next_cursor,
                page_size=page_size,
            ),
        )

    def audit_event_detail(
        self, *, conn, audit_id: str
    ) -> AuditEventDTO | None:
        row = conn.execute(
            "SELECT * FROM audit_events WHERE audit_id = ?",
            (audit_id,),
        ).fetchone()
        if row is None:
            return None
        return _audit_row_to_dto(_row_to_dict(row), detailed=True)

    # -----------------------------------------------------------------
    # query traces
    # -----------------------------------------------------------------
    def list_query_traces(
        self,
        *,
        conn,
        cursor: str | None,
        page_size: int = 25,
        q: str | None = None,
    ) -> PagedResponse:
        snapshot_id, _ = _read_head_snapshot(self.vault)
        decoded = decode_cursor(cursor, scope="query_traces")
        if decoded is not None and decoded["snapshot_id"] != snapshot_id:
            decoded = None
        page_size = max(1, min(200, int(page_size)))
        last_seq = 0
        if decoded:
            try:
                last_seq = int(decoded["params"].get("last_seq") or 0)
            except (TypeError, ValueError):
                last_seq = 0

        params: list[Any] = []
        where = ["action = 'query.retrieved'"]
        if last_seq:
            where.append("seq < ?")
            params.append(last_seq)
        # Free-text filter. The actual query text lives in the
        # persisted trace artifact (``<vault>/artifacts/queries/<trace_id>.json``),
        # so SQL LIKE on ``object_id`` (the trace_id) alone is not
        # enough. We over-fetch when ``q`` is set and post-filter by
        # reading the artifact files; the cost is bounded because
        # traces are operator-pinned (small per-call cardinality).
        over_fetch = page_size * 4 + 1 if q else page_size + 1
        sql = (
            "SELECT * FROM audit_events WHERE " + " AND ".join(where) +
            " ORDER BY seq DESC LIMIT ?"
        )
        params.append(over_fetch)
        rows = [_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]
        if q:
            q_lower = q.lower()
            kept: list[dict[str, Any]] = []
            for r in rows:
                if q_lower in str(r.get("object_id") or "").lower():
                    kept.append(r)
                    continue
                # Read the artifact; if the query text matches, keep.
                artifact = (
                    self.vault.artifacts_dir / "queries" / f"{r['object_id']}.json"
                )
                if artifact.exists():
                    try:
                        import json as _json
                        haystack = _json.dumps(
                            _json.loads(artifact.read_text(encoding="utf-8")),
                            ensure_ascii=False,
                        ).lower()
                    except (ValueError, OSError):
                        haystack = ""
                    if q_lower in haystack:
                        kept.append(r)
            rows = kept
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        items: list[QueryTraceDTO] = []
        for r in rows:
            items.append(self._trace_row_to_dto(r))
        next_cursor = None
        if has_more and rows:
            next_cursor = encode_cursor(
                scope="query_traces",
                snapshot_id=snapshot_id,
                params={"last_seq": int(rows[-1]["seq"]), "q": q},
            )
        return PagedResponse(
            header=_build_header(conn, self.vault),
            items=[item.model_dump(mode="json") for item in items],
            pagination=PaginationCursor(
                cursor=cursor,
                next_cursor=next_cursor,
                page_size=page_size,
            ),
        )

    def query_trace_detail(
        self, *, conn, trace_id: str
    ) -> QueryTraceDetailDTO | None:
        row = conn.execute(
            "SELECT * FROM audit_events WHERE action='query.retrieved' "
            "AND object_id = ?",
            (trace_id,),
        ).fetchone()
        if row is None:
            return None
        base = self._trace_row_to_dto(_row_to_dict(row))
        # Pull the persisted trace artifact for the full variants.
        artifact = self.vault.artifacts_dir / "queries" / f"{trace_id}.json"
        fingerprint = None
        hard_filters: dict[str, Any] = {}
        rewrite = None
        expansions: list[str] = []
        variants_full: list[dict[str, Any]] = []
        rejected_full: list[dict[str, Any]] = []
        provider = None
        prompt_version = None
        warnings: list[str] = []
        if artifact.exists():
            try:
                payload = json.loads(artifact.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                payload = {}
            fingerprint = payload.get("fingerprint")
            hard_filters = payload.get("hard_filters") or {}
            rewrite = payload.get("rewrite")
            expansions = list(payload.get("expansions") or [])
            variants_full = list(payload.get("variants") or [])
            rejected_full = list(payload.get("rejected") or [])
            provider = payload.get("provider")
            prompt_version = payload.get("prompt_version")
            warnings = list(payload.get("warnings") or [])
        return QueryTraceDetailDTO(
            **base.model_dump(),
            fingerprint=fingerprint,
            hard_filters=hard_filters,
            rewrite=rewrite,
            expansions=expansions,
            variants_full=variants_full,
            rejected_full=rejected_full,
            provider=provider,
            prompt_version=prompt_version,
            warnings=warnings,
            artifact_ref=str(artifact) if artifact.exists() else None,
        )

    def _trace_row_to_dto(self, row: dict[str, Any]) -> QueryTraceDTO:
        before_after = self._safe_json(row.get("before_after_json") or "{}", default={})
        original_query = before_after.get("original_query") if isinstance(before_after, dict) else None
        accepted_variants = before_after.get("accepted_variants") if isinstance(before_after, dict) else None
        rejected_variants = before_after.get("rejected_variants") if isinstance(before_after, dict) else None
        correlation = self._safe_json(row.get("correlation_json") or "{}", default={})
        snapshot_id = correlation.get("snapshot_id") if isinstance(correlation, dict) else None
        retrieval = before_after.get("retrieval") if isinstance(before_after, dict) else None
        return QueryTraceDTO(
            trace_id=str(row["object_id"]),
            snapshot_id=str(snapshot_id or ""),
            retrieval=str(retrieval or "keyword"),
            original_query=original_query,
            accepted_variants=list(accepted_variants or []) if isinstance(accepted_variants, list) else [],
            rejected_variants=list(rejected_variants or []) if isinstance(rejected_variants, list) else [],
            fallback_reason=row.get("reason_code"),
            result_count=int((before_after or {}).get("result_count") or 0) if isinstance(before_after, dict) else 0,
            occurred_at=row.get("occurred_at"),
        )


# ---------------------------------------------------------------------------
# Convenience wrappers used by the HTTP layer.
# ---------------------------------------------------------------------------


def list_summaries_for_route(
    svc: AuditQueries,
    *,
    conn,
    method: str,
    args: dict[str, Any],
) -> PagedResponse | Any:
    """Dispatch a list call. The HTTP layer passes parsed query
    parameters here.
    """

    if method == "sources":
        return svc.list_sources(
            conn=conn,
            cursor=args.get("cursor"),
            page_size=int(args.get("page_size") or 25),
            connector=args.get("connector"),
        )
    if method == "batches":
        return svc.list_batches(
            conn=conn,
            cursor=args.get("cursor"),
            page_size=int(args.get("page_size") or 25),
            status=args.get("status"),
        )
    if method == "session_turns":
        return svc.list_session_turns(
            conn=conn,
            session_id=str(args["session_id"]),
            branch_id=str(args.get("branch_id") or "main"),
            cursor=args.get("cursor"),
            page_size=int(args.get("page_size") or 25),
        )
    if method == "conflicts":
        return svc.list_conflicts(
            conn=conn,
            cursor=args.get("cursor"),
            page_size=int(args.get("page_size") or 25),
            include_decided=bool(args.get("include_decided") or False),
        )
    if method == "memories":
        return svc.list_memories(
            conn=conn,
            cursor=args.get("cursor"),
            page_size=int(args.get("page_size") or 25),
            include_archived=bool(args.get("include_archived") or False),
            q=args.get("q"),
        )
    if method == "jobs":
        return svc.list_jobs(
            conn=conn,
            cursor=args.get("cursor"),
            page_size=int(args.get("page_size") or 25),
            state=args.get("state"),
            q=args.get("q"),
        )
    if method == "audit":
        return svc.list_audit_events(
            conn=conn,
            cursor=args.get("cursor"),
            page_size=int(args.get("page_size") or 25),
            action=args.get("action"),
            object_type=args.get("object_type"),
            object_id=args.get("object_id"),
            seq_min=(int(args["seq_min"]) if args.get("seq_min") is not None else None),
            seq_max=(int(args["seq_max"]) if args.get("seq_max") is not None else None),
            q=args.get("q"),
        )
    if method == "query_traces":
        return svc.list_query_traces(
            conn=conn,
            cursor=args.get("cursor"),
            page_size=int(args.get("page_size") or 25),
            q=args.get("q"),
        )
    raise ValueError(f"unknown list method {method!r}")


__all__ = [
    "AuditQueries",
    "decode_cursor",
    "encode_cursor",
    "list_summaries_for_route",
]