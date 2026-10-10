"""Command dispatcher.

Translates a CommandResponse envelope into one of:
  - init / scan / batch.plan / batch.process / maintain
  - recall / conflicts / decide / doctor

Errors are always returned as a CommandResponse with status=error. The
CLI prints exactly one CommandResponse as JSON on stdout.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory_agent.application.commit import CommitService
from memory_agent.application.doctor import run_doctor
from memory_agent.application.plan_batches import plan_batches_for_session
from memory_agent.application.privacy import (
    PrivacyError as PrivacySvcError,
    PrivacyService,
    ensure_privacy_head,
)
from memory_agent.application.backup import BackupError, BackupService
from memory_agent.application.process_batches import process_batch
from memory_agent.application.reconcile import Reconciler
from memory_agent.application.recall import RecallService
from memory_agent.application.recovery import recover_unfinished_intents
from memory_agent.application.scan import (
    default_adapters,
    scan_once,
)
from memory_agent.application.search.rewrite_provider import (
    get_default_rewrite_provider,
)
from memory_agent.application.source_correction import (
    CorrectionCommitError,
    decide_proposal,
)
from memory_agent.contracts.candidates import EvidenceRef, Scope
from memory_agent.contracts.commands import (
    COMMAND_API_VERSION,
    CommandResponse,
    DecideRequest,
    RecallRequest,
    RequestEnvelope,
    ResponseError,
    ScanRequest,
)
from memory_agent.contracts.events import (
    ConversationEvent,
    SourceLocator,
    SourceRef,
)
from memory_agent.contracts.memory import MemoryAtom, MemoryStatus
from memory_agent.domain.batching import (
    DEFAULT_MAX_INPUT_TOKENS,
    DEFAULT_MAX_TURNS_PER_BATCH,
    BatchPolicyInput,
)
from memory_agent.infrastructure.files import VaultLayout
from memory_agent.infrastructure.sqlite import open_state_db
from memory_agent.infrastructure.sqlite.repositories import (
    BatchRepo,
    ConflictRepo,
    MemoryProjectionRepo,
    SourceFileRepo,
)

from memory_agent.adapters.models import (
    SQLiteModelCallRecorder,
    get_default_provider,
)


class DispatchError(ValueError):
    """An error in the dispatch layer (validation, malformed input)."""


def dispatch(
    *,
    operation: str,
    params: dict[str, Any],
    request_id: str | None = None,
    client_id: str = "local_cli",
    idempotency_key: str | None = None,
    vault_root: Path | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request_id = request_id or f"req_{uuid.uuid4().hex[:12]}"
    try:
        envelope = RequestEnvelope(
            api_version=COMMAND_API_VERSION,
            request_id=request_id,
            client_id=client_id,
            idempotency_key=idempotency_key,
            operation=operation,
        )
    except Exception as exc:
        return _err(
            request_id, operation, "INVALID_REQUEST", f"bad envelope: {exc}"
        )

    vault_root = vault_root or Path("vault").resolve()
    config = config or {}
    vault = VaultLayout(vault_root)

    try:
        if envelope.operation == "init":
            return _cmd_init(envelope, vault, params)
        if envelope.operation == "doctor":
            return _cmd_doctor(envelope, vault)
        if envelope.operation == "review":
            return _cmd_review(envelope, vault, params)
        # Below this point we need a database.
        if not vault.state_db.exists():
            return _err(envelope.request_id, envelope.operation, "RECOVERY_REQUIRED",
                         "vault not initialised; run `memory init` first")

        conn = open_state_db(vault.state_db)
        try:
            recover_unfinished_intents(vault=vault, conn=conn)
            if envelope.operation == "scan":
                return _cmd_scan(envelope, vault, conn, params)
            if envelope.operation == "batch.plan":
                return _cmd_plan(envelope, vault, conn, params)
            if envelope.operation == "batch.process":
                return _cmd_process(envelope, vault, conn, params)
            if envelope.operation == "maintain":
                return _cmd_maintain(envelope, vault, conn, params)
            if envelope.operation == "recall":
                return _cmd_recall(envelope, vault, conn, params)
            if envelope.operation == "conflicts":
                return _cmd_conflicts(envelope, vault, conn)
            if envelope.operation == "decide":
                return _cmd_decide(envelope, vault, conn, params)
            if envelope.operation == "source_correction.decide":
                return _cmd_source_correction_decide(
                    envelope, vault, conn, params
                )
            if envelope.operation in {
                "privacy.forget", "privacy.list",
                "privacy.receipt", "privacy.unblock",
            }:
                return _cmd_privacy(envelope, vault, conn, params)
            if envelope.operation in {
                "backup.create", "backup.list", "backup.restore",
            }:
                return _cmd_backup(envelope, vault, conn, params)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return _err(envelope.request_id, envelope.operation, "INVALID_REQUEST",
                    f"unknown operation: {operation}")
    except DispatchError as exc:
        return _err(request_id, operation, "INVALID_REQUEST", str(exc))
    except Exception as exc:  # noqa: BLE001
        return _err(request_id, operation, "INTERNAL", f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# init: create DB schema; optionally baseline configured roots.
# ---------------------------------------------------------------------------


def _cmd_init(
    envelope: RequestEnvelope,
    vault: VaultLayout,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Initialise the vault (idempotent).

    Behaviour:
      * Always creates the SQLite schema if missing.
      * Atomically establishes a zero-version ``privacy/HEAD.json``
        on a genuinely fresh vault (no HEAD, no deletion
        manifests) so the live privacy ledger is durable from the
        first ``init``. The HEAD is the trust root for restore-time
        privacy decisions; without it, restore fails closed. If the
        vault already has deletion manifests but no HEAD (an
        inconsistent torn-write state), ``init`` refuses with
        ``INTERNAL`` so the operator reconciles manually instead of
        silently re-creating a v0 ledger that could mask the
        forgotten content.
      * When called with ``roots`` in params, every file under each
        configured connector root is registered with
        ``complete_offset = current_size`` so no historical content
        is consumed on the next scan. Future appends are then
        picked up incrementally. There is no implicit backfill in
        v0.1; if you need backfill, the documented recipe is to
        delete the project and re-init, or to manually move the
        cursor (P1).
      * Re-running ``init`` with new roots after the fact *baselines
        those new roots at current end-of-file* without touching
        the cursors for roots that were already known.
    """
    already = vault.state_db.exists()
    conn = open_state_db(vault.state_db)
    try:
        roots = dict(params.get("roots") or {})
        baseline_summary = _baseline_roots(conn=conn, vault=vault, roots=roots)
        config_path = vault.config_path
        if not config_path.exists():
            config_path.write_text(
                json.dumps(params.get("config") or {"timezone": "Asia/Shanghai"}, indent=2),
                encoding="utf-8",
            )
    finally:
        try:
            conn.close()
        except Exception:
            pass
    # Establish a v0 privacy HEAD if missing. This is the only
    # code path (apart from the explicit ``memory backup create``
    # operator workflow) that is permitted to write a v0 HEAD into
    # a vault that previously had none. Refuses when deletion
    # manifests exist without a HEAD — the operator must reconcile
    # manually.
    try:
        head_payload = ensure_privacy_head(vault)
    except PrivacySvcError as exc:
        return _err(envelope.request_id, "init", "INTERNAL",
                    f"init: {exc}")
    return _ok(envelope, {
        "vault": str(vault.root),
        "already_initialised": already,
        "schema_version": 1,
        "privacy_head_version": int(head_payload.get("version") or 0),
        "privacy_epoch": head_payload.get("epoch"),
        "baseline": baseline_summary,
    })


def _baseline_roots(*, conn, vault: VaultLayout, roots: dict[str, str]) -> dict[str, Any]:
    """Register files under each root at end-of-file so nothing
    historical is consumed.

    Returns a summary suitable for inclusion in the init response.
    """
    if not roots:
        return {"applied": False, "reason": "no_roots"}
    adapters = default_adapters()
    sf_repo = SourceFileRepo(conn)
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for connector, root in roots.items():
        if not root:
            continue
        reader = adapters.get(connector)
        if reader is None:
            skipped.append({"connector": connector, "reason": "unknown_connector"})
            continue
        root_path = Path(root)
        if not root_path.exists():
            skipped.append({"connector": connector, "reason": "root_missing", "path": root})
            continue
        descriptors = reader.discover(str(root_path))
        for desc in descriptors:
            path = Path(desc["path"])
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            source_file_id = _stable_source_id(connector, path)
            session_id, branch_id, _ = _session_meta(path, connector)
            identity = _file_identity(stat)
            row = sf_repo.get_or_register(
                source_file_id=source_file_id,
                connector=connector,
                session_id=session_id,
                branch_id=branch_id,
                canonical_path=str(path),
                file_identity=identity,
                adapter_version=reader.adapter_version,
                generation=1,
            )
            # Pin the cursor to end-of-file. If the row was newly created,
            # this is the only place we set complete_offset > 0. If the
            # row already existed (e.g. user re-init'd with a known root),
            # we leave the existing cursor alone — no implicit replay.
            if int(row.get("complete_offset") or 0) == 0 and not row.get("size"):
                sf_repo.update_cursor(
                    source_file_id=source_file_id,
                    complete_offset=stat.st_size,
                    size=stat.st_size,
                    mtime=_mtime_iso(stat),
                    last_status="baseline",
                )
                applied.append({
                    "connector": connector,
                    "source_file_id": source_file_id,
                    "path": str(path),
                    "baseline_offset": stat.st_size,
                })
            else:
                skipped.append({
                    "connector": connector,
                    "source_file_id": source_file_id,
                    "reason": "already_baselined_or_have_history",
                })
    return {
        "applied": bool(applied),
        "applied_count": len(applied),
        "skipped_count": len(skipped),
        "applied": applied[:50],
        "skipped": skipped[:50],
        "behaviour": (
            "init records each file at current end-of-file; "
            "no historical content is backfilled. Subsequent appends "
            "are picked up incrementally by `memory maintain`."
        ),
    }


def _stable_source_id(connector: str, path: Path) -> str:
    import hashlib
    h = hashlib.sha256(f"{connector}|{str(path)}".encode("utf-8")).hexdigest()
    return f"{connector}-src-{h[:16]}"


def _session_meta(path: Path, connector: str) -> tuple[str, str, str | None]:
    parts = path.parts
    if connector == "claude-code":
        session_id = path.stem
        project_id = None
        for i, part in enumerate(parts):
            if part == "projects" and i + 2 < len(parts):
                project_id = parts[i + 1]
                break
        return session_id, "main", project_id
    if connector == "codex":
        return path.stem, "main", None
    return path.stem, "main", None


def _file_identity(stat) -> str:
    return f"dev={stat.st_dev}:ino={stat.st_ino}"


def _mtime_iso(stat) -> str:
    return datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def _cmd_doctor(envelope: RequestEnvelope, vault: VaultLayout) -> dict[str, Any]:
    report = run_doctor(vault=vault)
    return _ok(envelope, {
        "ok": report.ok,
        "vault": report.vault_path,
        "schema_version": report.schema_version,
        "head_present": report.head_present,
        "head_commit_id": report.head_commit_id,
        "pending_intents": report.pending_intents,
        "pending_batches": report.pending_batches,
        "active_memories": report.active_memories,
        "diagnostics": report.diagnostics,
    })


def _cmd_review(
    envelope: RequestEnvelope, vault: VaultLayout, params: dict[str, Any],
) -> dict[str, Any]:
    """Sync ``review`` handler.

    Phase C repair-2: this is no longer ``UNSUPPORTED``. The
    worker's ``run_review`` writes a conservative proposal artifact
    and an audit row; the result dict is returned verbatim.
    """
    from memory_agent.application.worker import run_review
    period = (params or {}).get("period")
    if period not in {"week", "month"}:
        return _err(
            envelope.request_id, "review", "INVALID_REQUEST",
            f"review: 'period' must be 'week' or 'month', got {period!r}",
        )
    try:
        review = run_review(
            vault_root=vault.root, period=period,
            worker_id=envelope.client_id,
        )
    except Exception as exc:  # noqa: BLE001
        return _err(envelope.request_id, "review", "INTERNAL",
                    f"{type(exc).__name__}: {exc}")
    return _ok(envelope, review)


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


def _cmd_scan(envelope: RequestEnvelope, vault: VaultLayout, conn, params: dict[str, Any]) -> dict[str, Any]:
    try:
        req = ScanRequest.model_validate(params)
    except Exception as exc:
        return _err(envelope.request_id, "scan", "INVALID_REQUEST", f"scan: {exc}")
    roots = dict(req.roots or {})
    if not roots:
        return _err(
            envelope.request_id, "scan", "INVALID_REQUEST",
            "scan: 'roots' must specify at least one configured root",
        )
    adapters = default_adapters()
    try:
        result = scan_once(
            vault=vault,
            conn=conn,
            adapters=adapters,
            roots=roots,
            max_bytes_per_file=req.max_bytes_per_file,
            mode=req.mode,
            since_iso=req.since_iso,
            until_iso=req.until_iso,
            timezone_name=req.timezone,
        )
    except ValueError as exc:
        return _err(envelope.request_id, "scan", "INVALID_REQUEST", f"scan: {exc}")
    # Phase C repair-2: a scan that observed a rewrite / middle-rewrite
    # / rotation must refresh the source-correction overlay *inline*
    # so a subsequent recall (before the worker pass) flags affected
    # memories as ``pending_correction`` instead of returning them
    # silently. We do this synchronously inside the same conn so the
    # overlay is visible to the next reader.
    overlay_rows_touched = 0
    proposal_summary: dict[str, Any] = {}
    try:
        from memory_agent.application.source_correction import (
            discover_affected_memories,
            refresh_overlay,
        )
        discovery = discover_affected_memories(vault=vault, conn=conn)
        overlay_rows_touched = refresh_overlay(conn=conn)
        proposal_summary = discovery.to_dict()
    except Exception as exc:  # noqa: BLE001
        # A discovery failure must not block the scan result. Record
        # the diagnostic in the response so the caller can see it.
        proposal_summary = {"diagnostics": [
            {"code": "discovery_failed",
             "error": f"{type(exc).__name__}: {exc}"},
        ]}
    return _ok(envelope, {
        "files_seen": result.files_seen,
        "files_changed": result.files_changed,
        "bytes_read": result.bytes_read,
        "new_events": result.new_events,
        "duplicate_events": result.duplicate_events,
        "partial_record_pending": result.partial_record_pending,
        "diagnostics": result.diagnostics,
        "model_calls": 0,
        "mode": req.mode,
        "backfill_id": result.backfill_id,
        "target_events": result.target_events,
        "context_only_events": result.context_only_events,
        "rotations": result.rotations,
        "truncated": result.truncated,
        "middle_rewrites": result.middle_rewrites,
        "superseded_batches": result.superseded_batches,
        "source_correction": {
            **proposal_summary,
            "overlay_rows_touched": overlay_rows_touched,
        },
    })


# ---------------------------------------------------------------------------
# batch.plan / batch.process / maintain
# ---------------------------------------------------------------------------


def _event_from_row_with_text(conn, r) -> ConversationEvent:
    """Build a ConversationEvent from a source_events row, fetching the
    real text and the real byte/ordinal locator from its segment file.

    v0.1 stores event text inside the segment file (which is durable
    and content-addressed); the SQLite row carries locator metadata.
    We rebuild the ConversationEvent honestly: byte_start, byte_end and
    record_ordinal come from the persisted SourceLocator stored inside
    the segment JSON, not from a fabricated ``(0, len(text), 1)``.
    """
    seg_row = conn.execute(
        "SELECT s.path AS JSONPath FROM source_events e "
        "JOIN segments s ON s.segment_id = e.segment_id "
        "WHERE e.event_id = ? LIMIT 1",
        (r["event_id"],),
    ).fetchone()
    text = ""
    byte_start = 0
    byte_end = 0
    # record_ordinal lives on the event's SourceLocator inside the
    # segment JSON; default to 1 only if we can't read it.
    record_ordinal = 1
    parent_id = None
    discovered_at = str(r["discovered_at"])
    source_time = str(r["source_time"] or discovered_at)
    if seg_row is not None:
        path = Path(seg_row["JSONPath"])
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                for ev in payload.get("events", []):
                    if ev.get("event_id") == r["event_id"]:
                        text = ev.get("text", "")
                        src = ev.get("source", {})
                        loc = src.get("locator", {})
                        byte_start = int(loc.get("byte_start", 0))
                        byte_end = int(loc.get("byte_end", byte_start + len(text)))
                        record_ordinal = int(
                            loc.get("record_ordinal", record_ordinal)
                        )
                        source_time = str(src.get("source_time", source_time))
                        discovered_at = str(src.get("discovered_at", discovered_at))
                        parent_id = ev.get("parent_id")
                        break
            except (json.JSONDecodeError, OSError):
                pass
    # If the segment JSON did not carry byte offsets (older segment
    # payloads or unexpected schema), raise instead of silently using
    # (0, len(text), 1). The caller is expected to fail-closed.
    if byte_end == 0 and text:
        raise ValueError(
            f"segment for event {r['event_id']!r} missing byte locator; "
            "cannot build an honest ConversationEvent"
        )
    return ConversationEvent(
        schema_version=1,
        event_id=r["event_id"],
        logical_message_id=r["event_id"],
        revision_hash=r["revision_hash"],
        source=SourceRef(
            connector=_connector_from_source_file_id(r["source_file_id"]),
            adapter_version="reader-v0.1",
            host_id="local-mac",
            source_file_id=r["source_file_id"],
            generation=1,
            session_id=r["session_id"],
            branch_id=r["branch_id"],
            turn_key=r["event_id"],
            project_id=None,
            locator=SourceLocator(
                byte_start=byte_start,
                byte_end=max(byte_start, byte_end),
                record_ordinal=record_ordinal,
            ),
            source_time=source_time,
            discovered_at=discovered_at,
        ),
        role=r["role"],
        kind=r["kind"],
        text=text,
        parent_id=parent_id,
        origin="user_session" if r["role"] == "user" else "assistant_response",
    )


def _connector_from_source_file_id(source_file_id: str) -> str:
    if source_file_id.startswith("claude-code-"):
        return "claude-code"
    if source_file_id.startswith("codex-"):
        return "codex"
    return "claude-code"


def _cmd_plan(envelope: RequestEnvelope, vault: VaultLayout, conn, params: dict[str, Any]) -> dict[str, Any]:
    session_id = params.get("session_id")
    branch_id = params.get("branch_id", "main")
    if not session_id:
        raise DispatchError("batch.plan: 'session_id' is required")
    rows = conn.execute(
        "SELECT * FROM source_events WHERE session_id = ? AND branch_id = ? ORDER BY discovered_at",
        (session_id, branch_id),
    ).fetchall()
    events: list[ConversationEvent] = [_event_from_row_with_text(conn, r) for r in rows]
    policy = BatchPolicyInput(
        max_turns_per_batch=int(params.get("max_turns_per_batch", DEFAULT_MAX_TURNS_PER_BATCH)),
        max_input_tokens=int(params.get("max_input_tokens", DEFAULT_MAX_INPUT_TOKENS)),
    )
    planned = plan_batches_for_session(
        session_id=session_id,
        branch_id=branch_id,
        events=events,
        policy=policy,
        conn=conn,
    )
    return _ok(envelope, {
        "planned_batches": [b["batch"]["batch_id"] for b in planned],
        "count": len(planned),
    })


def _cmd_process(envelope: RequestEnvelope, vault: VaultLayout, conn, params: dict[str, Any]) -> dict[str, Any]:
    batch_ids = params.get("batch_ids") or []
    if not batch_ids:
        batch_ids = [b["batch_id"] for b in BatchRepo(conn).list_pending(limit=100)]
    max_batches = int(params.get("max_batches", 5))
    batch_ids = batch_ids[:max_batches]

    recorder = SQLiteModelCallRecorder(conn=conn)
    provider = get_default_provider(recorder=recorder)
    committer = CommitService(vault=vault, conn=conn)
    reconciler = Reconciler(memory_projection_repo=MemoryProjectionRepo(conn))
    privacy_epoch = _current_privacy_epoch(vault)
    results = []
    for batch_id in batch_ids:
        # Re-read HEAD before each publish so the second batch's
        # commit manifest chains off the first batch's published
        # commit, not off HEAD as it stood when the loop started.
        base_commit = _current_head_commit_id(vault)
        try:
            res = process_batch(
                batch_id=batch_id,
                vault=vault,
                conn=conn,
                provider=provider,
                reconciler=reconciler,
                committer=committer,
                evidence_lookup=lambda eid, rh: _evidence_text_lookup(conn, eid, rh, vault),
                base_commit_id=base_commit or "root",
                privacy_epoch=privacy_epoch or "epoch-0",
                run_id=f"run_{envelope.request_id}",
            )
            results.append({"batch_id": batch_id, "ok": True, **res})
        except Exception as exc:  # noqa: BLE001
            results.append({"batch_id": batch_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return _ok(envelope, {"processed": results})


def _cmd_maintain(envelope: RequestEnvelope, vault: VaultLayout, conn, params: dict[str, Any]) -> dict[str, Any]:
    roots = params.get("roots") or {}
    if not roots:
        raise DispatchError("maintain: 'roots' must specify at least one configured root")
    adapters = default_adapters()
    scan_res = scan_once(vault=vault, conn=conn, adapters=adapters, roots=roots)
    # Plan batches across all sessions.
    policy = BatchPolicyInput(
        max_turns_per_batch=int(params.get("max_turns_per_batch", DEFAULT_MAX_TURNS_PER_BATCH)),
        max_input_tokens=int(params.get("max_input_tokens", DEFAULT_MAX_INPUT_TOKENS)),
    )
    sessions = conn.execute(
        "SELECT DISTINCT session_id, branch_id FROM source_events"
    ).fetchall()
    planned = 0
    for s in sessions:
        sid = s["session_id"]
        bid = s["branch_id"]
        rows = conn.execute(
            "SELECT * FROM source_events WHERE session_id = ? AND branch_id = ? ORDER BY discovered_at",
            (sid, bid),
        ).fetchall()
        events: list[ConversationEvent] = [_event_from_row_with_text(conn, r) for r in rows]
        planned += len(plan_batches_for_session(
            session_id=sid, branch_id=bid, events=events, policy=policy, conn=conn,
        ))

    # Process up to max_batches. Track real model call attempts (read
    # model_calls table counts before/after, NOT a hardcoded 0).
    before = _model_call_count(conn)
    max_batches = int(params.get("max_batches", 5))
    process_res = _cmd_process(
        envelope, vault, conn,
        {"batch_ids": [b["batch_id"] for b in BatchRepo(conn).list_pending(limit=max_batches)],
         "max_batches": max_batches},
    )
    after = _model_call_count(conn)
    process_dict = process_res["result"] or {}
    processed = process_dict.get("processed", [])
    # Detect partial / failed batches.
    has_partial = any(
        (not p.get("ok")) or p.get("status") in {"timeout", "auth_error", "malformed", "provider_unavailable", "failed"}
        for p in processed
    )
    return _ok(envelope, {
        "scan": {
            "files_seen": scan_res.files_seen,
            "files_changed": scan_res.files_changed,
            "new_events": scan_res.new_events,
            "duplicate_events": scan_res.duplicate_events,
        },
        "planned_batches": planned,
        "processed": processed,
        "model_calls": max(0, after - before),
        "partial": bool(has_partial),
    })


def _model_call_count(conn) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM model_calls").fetchone()
    return int(row["c"]) if row else 0


# ---------------------------------------------------------------------------
# recall / conflicts
# ---------------------------------------------------------------------------


def _cmd_recall(envelope: RequestEnvelope, vault: VaultLayout, conn, params: dict[str, Any]) -> dict[str, Any]:
    req = RecallRequest.model_validate(params)
    svc = RecallService(
        vault=vault,
        memory_projection_repo=MemoryProjectionRepo(conn),
        rewrite_provider=get_default_rewrite_provider(),
        audit_conn=conn,
    )
    res = svc.recall(
        query=req.query,
        project_ids=req.project_ids,
        domain_ids=req.domain_ids,
        include_archived=req.include_archived,
        limit=req.limit,
        retrieval=req.retrieval,
        rewrite=req.rewrite,
        expand=req.expand,
        cursor=req.cursor,
        retrieval_limit=req.retrieval_limit,
        audit=req.audit,
    )
    return _ok(envelope, res)


def _cmd_conflicts(envelope: RequestEnvelope, vault: VaultLayout, conn) -> dict[str, Any]:
    repo = ConflictRepo(conn)
    return _ok(envelope, {"open": repo.list_open()})


# ---------------------------------------------------------------------------
# decide: real version-checked accept/reject.
# ---------------------------------------------------------------------------


def _cmd_decide(
    envelope: RequestEnvelope,
    vault: VaultLayout,
    conn,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Apply a user decision to a conflict projection.

    Contract:
      * ``proposal_revision`` must match the current open revision;
        stale revisions are rejected with ``INVALID_REQUEST`` so users
        can't accidentally resolve on top of an out-of-date proposal.
      * ``action`` semantics:
          - ``adopt_new``: write the candidate as a new active memory
            (revision 1) with the candidate's evidence preserved, and
            supersede the conflicting memory.
          - ``keep_old``: leave the active memory in place; mark the
            conflict ``rejected`` (decision: keep_old).
          - ``scope_split``: write the new candidate as a separate active
            memory under a narrower scope (the candidate's existing
            scope is used) and supersede the original.
          - ``defer``: leave the conflict ``open``.
      * Every decision writes a single audit row and an updated
        ``conflict_projection`` entry; memory writes go through the
        ``CommitService`` so the HEAD swap + finalize protocol applies.
    """
    # Strip dispatch-only keys before validating the DecideRequest body.
    body = {k: v for k, v in params.items() if k != "idempotency_key"}
    req = DecideRequest.model_validate(body)
    idem_key = params.get("idempotency_key") or envelope.idempotency_key
    if idem_key is None:
        return _err(envelope.request_id, "decide", "INVALID_REQUEST",
                    "decide requires an idempotency key (--idempotency-key)")
    repo = ConflictRepo(conn)
    open_rows = [r for r in repo.list_open() if r["conflict_id"] == req.conflict_id]
    if not open_rows:
        return _err(envelope.request_id, "decide", "NOT_FOUND",
                    f"conflict {req.conflict_id!r} is not open")
    current = sorted(open_rows, key=lambda r: r["revision"])[-1]
    if int(current["revision"]) != int(req.proposal_revision):
        return _err(envelope.request_id, "decide", "INVALID_REQUEST",
                    f"proposal_revision {req.proposal_revision} is stale; "
                    f"current is {current['revision']}")
    new_memory_ref = current.get("new_memory_ref")
    old_memory_ref = current.get("old_memory_ref")
    candidate_payload = _load_candidate_payload(vault, req.conflict_id)

    base_commit = _current_head_commit_id(vault)
    privacy_epoch = _current_privacy_epoch(vault) or "epoch-0"
    committer = CommitService(vault=vault, conn=conn)

    new_status: str
    new_state: str
    wrote_memory = False
    superseded: list[str] = []

    if req.action == "adopt_new":
        if candidate_payload is None:
            new_state = "rejected"
            new_memory_ref = None
        else:
            mem = _candidate_to_memory(candidate_payload, status="active")
            intent = committer.prepare_commit(
                proposed_memories=[mem],
                pending_proposals=[],
                base_commit_id=base_commit or "root",
                privacy_epoch=privacy_epoch,
                batch_ids=[f"decide:{req.conflict_id}"],
                target_unit_ids=[mem.memory_id],
            )
            committer.publish(intent["commit_id"])
            new_memory_ref = mem.memory_id
            wrote_memory = True
        new_state = "decided"
        if old_memory_ref:
            superseded.append(old_memory_ref)
    elif req.action == "keep_old":
        new_state = "decided_keep_old"
        new_memory_ref = old_memory_ref
    elif req.action == "scope_split":
        if candidate_payload is None:
            new_state = "rejected"
        else:
            mem = _candidate_to_memory(candidate_payload, status="active")
            intent = committer.prepare_commit(
                proposed_memories=[mem],
                pending_proposals=[],
                base_commit_id=base_commit or "root",
                privacy_epoch=privacy_epoch,
                batch_ids=[f"decide:split:{req.conflict_id}"],
                target_unit_ids=[mem.memory_id],
            )
            committer.publish(intent["commit_id"])
            new_memory_ref = mem.memory_id
            wrote_memory = True
        new_state = "decided"
        if old_memory_ref:
            superseded.append(old_memory_ref)
    elif req.action == "defer":
        new_state = "open"
    else:  # pragma: no cover
        return _err(envelope.request_id, "decide", "INVALID_REQUEST",
                    f"unknown action {req.action!r}")

    repo.close(req.conflict_id, int(req.proposal_revision), new_state)
    if superseded:
        # Mark superseded memories as superseded in the projection.
        for mid in superseded:
            conn.execute(
                "UPDATE memory_projection SET status = 'superseded' "
                "WHERE memory_id = ? AND status = 'active'",
                (mid,),
            )
    _audit_decision(conn, req, new_state, wrote_memory)
    return _ok(envelope, {
        "conflict_id": req.conflict_id,
        "status": new_state,
        "wrote_memory": wrote_memory,
        "superseded": superseded,
        "new_memory_id": new_memory_ref,
    })


# ---------------------------------------------------------------------------
# source_correction.decide: versioned adopt_new / scope_split / keep_old
# ---------------------------------------------------------------------------


def _cmd_source_correction_decide(
    envelope: RequestEnvelope,
    vault: VaultLayout,
    conn,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Apply a versioned decision to a source-correction proposal.

    Contract:

    * ``expected_proposal_revision`` MUST match the row's current
      ``proposal_revision``. Stale callers are rejected with
      ``INVALID_REQUEST`` and the row is *not* touched (no file write,
      no audit row, no overlay change).
    * ``keep_old`` / ``defer`` close the proposal in place; no HEAD
      swap, no file write.
    * ``adopt_new`` / ``scope_split`` publish a new memory through
      ``CommitService`` (file -> manifest -> HEAD swap -> finalize)
      and supersede the old memory. If the publish step fails, the
      proposal stays ``pending`` and the overlay stays
      ``pending_correction`` so the user can retry.
    """
    from memory_agent.contracts.commands import SourceCorrectionDecideParams
    try:
        req = SourceCorrectionDecideParams.model_validate(params)
    except Exception as exc:
        return _err(envelope.request_id, "source_correction.decide",
                    "INVALID_REQUEST",
                    f"source_correction.decide: {exc}")
    # Use the pre-validation conn so reads see a consistent view.
    try:
        updated = decide_proposal(
            conn=conn,
            correction_id=req.correction_id,
            action=req.action,
            note=req.note,
            expected_proposal_revision=req.expected_proposal_revision,
            vault=vault,
        )
    except CorrectionCommitError as exc:
        return _err(envelope.request_id, "source_correction.decide",
                    "INVALID_REQUEST", str(exc))
    except ValueError as exc:
        return _err(envelope.request_id, "source_correction.decide",
                    "INVALID_REQUEST", f"source_correction.decide: {exc}")
    if updated is None:
        return _err(envelope.request_id, "source_correction.decide",
                    "NOT_FOUND",
                    f"correction_id {req.correction_id!r} not found")
    return _ok(envelope, {
        "correction_id": updated["correction_id"],
        "status": updated["status"],
        "proposal_revision": updated.get("proposal_revision", 1),
        "superseded_memory_id": updated.get("superseded_memory_id"),
        "superseded_memory_revision": updated.get("superseded_memory_revision"),
        "superseded_head_commit_id": updated.get("superseded_head_commit_id"),
        "action": req.action,
    })


def _load_candidate_payload(vault: VaultLayout, conflict_id: str) -> dict[str, Any] | None:
    """Find the candidate payload associated with a conflict.

    Conflict files are written by CommitService at
    ``<vault>/objects/conflicts/<candidate_id>.json``; the conflict_id
    is the same as the candidate_id by convention.
    """
    path = vault.objects_dir / "conflicts" / f"{conflict_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _candidate_to_memory(payload: dict[str, Any], *, status: MemoryStatus) -> MemoryAtom:
    cand = payload.get("candidate") or {}
    now = datetime.now(timezone.utc).isoformat()
    scope_dict = cand.get("scope") or {"level": "global"}
    scope = Scope(**scope_dict) if isinstance(scope_dict, dict) else Scope(level="global")
    evidence = [
        EvidenceRef(**e) if not isinstance(e, EvidenceRef) else e
        for e in cand.get("evidence", [])
    ]
    return MemoryAtom(
        schema_version=1,
        memory_id="mem_" + (cand.get("candidate_id") or "decide").replace("cand_", ""),
        revision=1,
        status=status,
        kind=cand.get("kind") or "requirement",
        abstraction=cand.get("abstraction") or "no_opinion",
        statement=cand.get("statement") or "",
        subject=(scope.project_ids or ["global"])[0],
        predicate=None,
        value=None,
        modality=cand.get("modality") or "unspecified",
        scope=scope,
        primary_domain=cand.get("primary_domain"),
        domains=scope.domain_ids,
        tags=list(cand.get("tags") or []),
        horizon="unknown",
        valid_from=None,
        valid_until=None,
        asserted_at=now,
        recorded_at=now,
        evidence=evidence,
    )


def _audit_decision(conn, req: DecideRequest, new_state: str, wrote_memory: bool) -> None:
    import hashlib
    audit_id = "aud_" + hashlib.sha1(
        f"decide|{req.conflict_id}|{req.proposal_revision}|{new_state}".encode()
    ).hexdigest()[:12]
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM audit_events"
    ).fetchone()
    next_seq = int(row["next"])
    conn.execute(
        """INSERT INTO audit_events(
            audit_id, seq, occurred_at, actor, action,
            object_type, object_id, correlation_json,
            before_after_json, outcome, reason_code, details_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            audit_id, next_seq, datetime.now(timezone.utc).isoformat(),
            "user", "conflict.decided",
            "conflict", req.conflict_id,
            json.dumps({"idempotency_key": "decide"}),
            json.dumps({"action": req.action, "note": req.note}),
            "success" if new_state != "rejected" else "skipped",
            None if new_state != "rejected" else "no_candidate_payload",
            json.dumps({
                "proposal_revision": req.proposal_revision,
                "new_state": new_state,
                "wrote_memory": wrote_memory,
            }),
        ),
    )


# ---------------------------------------------------------------------------
# evidence_text_lookup: real segment JSON, fail-closed.
# ---------------------------------------------------------------------------


def _evidence_text_lookup(conn, event_id: str, revision_hash: str, vault: VaultLayout) -> str:
    """Look up the original text for an event by scanning segments.

    v0.1 stores event text inside segment files; we walk
    ``source_events`` -> ``segments`` -> JSON, matching both
    ``event_id`` and ``revision_hash``. Returns an empty string when
    the row is missing (the caller decides whether that's a fail).
    """
    row = conn.execute(
        "SELECT segment_id, revision_hash FROM source_events "
        "WHERE event_id = ? AND revision_hash = ?",
        (event_id, revision_hash),
    ).fetchone()
    if row is None:
        return ""
    seg = conn.execute(
        "SELECT path FROM segments WHERE segment_id = ?",
        (row["segment_id"],),
    ).fetchone()
    if seg is None:
        return ""
    path = Path(seg["path"])
    if not path.exists():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    for ev in payload.get("events", []):
        if ev.get("event_id") == event_id and ev.get("revision_hash") == revision_hash:
            return ev.get("text", "")
    return ""


def _current_head_commit_id(vault: VaultLayout) -> str | None:
    if not vault.head_path.exists():
        return None
    try:
        return json.loads(vault.head_path.read_text(encoding="utf-8")).get("commit_id")
    except json.JSONDecodeError:
        return None


def _current_privacy_epoch(vault: VaultLayout) -> str | None:
    if not vault.privacy_head.exists():
        return None
    try:
        return json.loads(vault.privacy_head.read_text(encoding="utf-8")).get("epoch")
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# response helpers
# ---------------------------------------------------------------------------


def _ok(envelope: RequestEnvelope, result: dict[str, Any]) -> dict[str, Any]:
    return CommandResponse(
        api_version=COMMAND_API_VERSION,
        request_id=envelope.request_id,
        operation=envelope.operation,
        status="ok",
        result=result,
    ).model_dump(mode="json")


def _err(request_id: str, operation: str, code: str, msg: str) -> dict[str, Any]:
    return CommandResponse(
        api_version=COMMAND_API_VERSION,
        request_id=request_id,
        operation=operation,
        status="error",
        error=ResponseError(code=code, message=msg),
    ).model_dump(mode="json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Privacy operations: privacy.forget / privacy.list / privacy.receipt /
# privacy.unblock. The dispatcher is the *only* writer of privacy
# receipts; the storage is the file + DB mirror described in
# application/privacy.py.
# ---------------------------------------------------------------------------


def _cmd_privacy(
    envelope: RequestEnvelope, vault: VaultLayout, conn,
    params: dict[str, Any],
) -> dict[str, Any]:
    op = envelope.operation
    if op == "privacy.forget":
        return _cmd_privacy_forget(envelope, vault, conn, params)
    if op == "privacy.list":
        return _cmd_privacy_list(envelope, vault, conn, params)
    if op == "privacy.receipt":
        return _cmd_privacy_receipt(envelope, vault, conn, params)
    if op == "privacy.unblock":
        return _cmd_privacy_unblock(envelope, vault, conn, params)
    return _err(envelope.request_id, op, "UNKNOWN_OPERATION",
                f"unknown privacy operation {op!r}")


def _cmd_privacy_forget(
    envelope: RequestEnvelope, vault: VaultLayout, conn, params: dict[str, Any],
) -> dict[str, Any]:
    from memory_agent.contracts.privacy import PrivacyForgetParams
    try:
        req = PrivacyForgetParams.model_validate(params)
    except Exception as exc:
        return _err(envelope.request_id, "privacy.forget", "INVALID_REQUEST",
                    f"privacy.forget: {exc}")
    try:
        svc = PrivacyService(vault=vault, conn=conn)
        receipt = svc.forget(
            kind=req.kind, targets=list(req.targets),
            actor=envelope.client_id, note=req.note,
        )
    except PrivacySvcError as exc:
        return _err(envelope.request_id, "privacy.forget", "INVALID_REQUEST",
                    f"privacy.forget: {exc}")
    return _ok(envelope, receipt.to_dict())


def _cmd_privacy_list(
    envelope: RequestEnvelope, vault: VaultLayout, conn, params: dict[str, Any],
) -> dict[str, Any]:
    from memory_agent.contracts.privacy import PrivacyListParams
    try:
        req = PrivacyListParams.model_validate(params)
    except Exception as exc:
        return _err(envelope.request_id, "privacy.list", "INVALID_REQUEST",
                    f"privacy.list: {exc}")
    svc = PrivacyService(vault=vault, conn=conn)
    items = [r.to_dict() for r in svc.list_receipts()]
    if req.kind is not None:
        items = [i for i in items if i.get("kind") == req.kind]
    items = items[: int(req.limit)]
    return _ok(envelope, {"receipts": items, "count": len(items)})


def _cmd_privacy_receipt(
    envelope: RequestEnvelope, vault: VaultLayout, conn, params: dict[str, Any],
) -> dict[str, Any]:
    from memory_agent.contracts.privacy import PrivacyReceiptParams
    try:
        req = PrivacyReceiptParams.model_validate(params)
    except Exception as exc:
        return _err(envelope.request_id, "privacy.receipt", "INVALID_REQUEST",
                    f"privacy.receipt: {exc}")
    svc = PrivacyService(vault=vault, conn=conn)
    for receipt in svc.list_receipts():
        if receipt.op_id == req.op_id:
            return _ok(envelope, receipt.to_dict())
    return _err(envelope.request_id, "privacy.receipt", "NOT_FOUND",
                f"privacy.receipt: op_id {req.op_id!r} not found")


def _cmd_privacy_unblock(
    envelope: RequestEnvelope, vault: VaultLayout, conn, params: dict[str, Any],
) -> dict[str, Any]:
    from memory_agent.contracts.privacy import PrivacyUnblockParams
    try:
        req = PrivacyUnblockParams.model_validate(params)
    except Exception as exc:
        return _err(envelope.request_id, "privacy.unblock", "INVALID_REQUEST",
                    f"privacy.unblock: {exc}")
    # Locate the receipt; refuse if it is not a source_block.
    svc = PrivacyService(vault=vault, conn=conn)
    target = None
    for receipt in svc.list_receipts():
        if receipt.op_id == req.op_id:
            target = receipt
            break
    if target is None:
        return _err(envelope.request_id, "privacy.unblock", "NOT_FOUND",
                    f"privacy.unblock: op_id {req.op_id!r} not found")
    if target.kind != "source_block":
        return _err(envelope.request_id, "privacy.unblock",
                    "INVALID_REQUEST",
                    "privacy.unblock: only source_block receipts are reversible")
    # Drop the block row; the cursor stays pinned at its current
    # offset so a subsequent scan resumes from where it stopped —
    # the user can re-run ``scan`` to consume the rest, or wipe the
    # source_files row to start over. The receipt manifest file
    # stays on disk for the audit trail; the row in
    # ``privacy_blocks`` is removed so the scanner no longer refuses
    # to register new events.
    conn.execute(
        "DELETE FROM privacy_blocks WHERE op_id = ?",
        (req.op_id,),
    )
    # Restore the source_files.last_status so the operator can see
    # why the cursor is where it is.
    for sf_id in target.targets:
        conn.execute(
            "UPDATE source_files SET last_status = 'privacy_unblocked' "
            "WHERE source_file_id = ?",
            (sf_id,),
        )
    # Audit row.
    import hashlib as _h
    seq_row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM audit_events"
    ).fetchone()
    next_seq = int(seq_row["next"]) if seq_row else 1
    audit_id = "aud_" + _h.sha1(
        f"privacy.unblocked|{req.op_id}".encode()
    ).hexdigest()[:12]
    conn.execute(
        """INSERT INTO audit_events(
            audit_id, seq, occurred_at, actor, action,
            object_type, object_id, correlation_json,
            before_after_json, outcome, reason_code, details_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            audit_id, next_seq, _now_iso(), envelope.client_id,
            "privacy.unblocked", "privacy_op", req.op_id,
            json.dumps({"targets": list(target.targets)}),
            json.dumps({}), "success", None,
            json.dumps({"kind": target.kind}),
        ),
    )
    return _ok(envelope, {
        "op_id": req.op_id,
        "kind": target.kind,
        "targets": list(target.targets),
        "status": "unblocked",
    })


# ---------------------------------------------------------------------------
# Backup operations: backup.create / backup.list / backup.restore. The
# dispatcher is the only caller of BackupService; legacy CLI uses
# ``memory backup ...`` which routes through the call dispatcher
# below.
# ---------------------------------------------------------------------------


def _cmd_backup(
    envelope: RequestEnvelope, vault: VaultLayout, conn,
    params: dict[str, Any],
) -> dict[str, Any]:
    op = envelope.operation
    if op == "backup.create":
        return _cmd_backup_create(envelope, vault, params)
    if op == "backup.list":
        return _cmd_backup_list(envelope, params)
    if op == "backup.restore":
        return _cmd_backup_restore(envelope, vault, params)
    return _err(envelope.request_id, op, "UNKNOWN_OPERATION",
                f"unknown backup operation {op!r}")


def _cmd_backup_create(
    envelope: RequestEnvelope, vault: VaultLayout, params: dict[str, Any],
) -> dict[str, Any]:
    from memory_agent.contracts.backup import BackupCreateParams
    try:
        req = BackupCreateParams.model_validate(params)
    except Exception as exc:
        return _err(envelope.request_id, "backup.create", "INVALID_REQUEST",
                    f"backup.create: {exc}")
    output = Path(req.output).expanduser().resolve()
    try:
        manifest = BackupService(vault=vault).create(
            output=output, fmt=req.format, label=req.label,
        )
    except BackupError as exc:
        return _err(envelope.request_id, "backup.create", "INTERNAL",
                    f"backup.create: {exc}")
    except Exception as exc:  # noqa: BLE001
        return _err(envelope.request_id, "backup.create", "INTERNAL",
                    f"{type(exc).__name__}: {exc}")
    return _ok(envelope, manifest.to_dict() | {"output": str(output)})


def _cmd_backup_list(
    envelope: RequestEnvelope, params: dict[str, Any],
) -> dict[str, Any]:
    from memory_agent.contracts.backup import BackupListParams
    try:
        req = BackupListParams.model_validate(params)
    except Exception as exc:
        return _err(envelope.request_id, "backup.list", "INVALID_REQUEST",
                    f"backup.list: {exc}")
    bundle = Path(req.bundle).expanduser().resolve()
    try:
        # Use a fresh (empty) vault handle for listing; we don't need
        # a live vault for read-only metadata.
        manifest = BackupService(vault=VaultLayout(bundle.parent / "_noop_")).list_bundle(
            bundle=bundle,
        )
    except BackupError as exc:
        return _err(envelope.request_id, "backup.list", "NOT_FOUND",
                    f"backup.list: {exc}")
    except Exception as exc:  # noqa: BLE001
        return _err(envelope.request_id, "backup.list", "INTERNAL",
                    f"{type(exc).__name__}: {exc}")
    return _ok(envelope, manifest.to_dict() | {"bundle": str(bundle)})


def _cmd_backup_restore(
    envelope: RequestEnvelope, vault: VaultLayout, params: dict[str, Any],
) -> dict[str, Any]:
    """Restore a backup bundle into a clean ``target``.

    Privacy safety (P9): the live vault's *authoritative* privacy
    ledger (``privacy/HEAD.json`` + ``privacy/deletions/``) is the
    trust root. A bundle older than the live ledger, or matching
    the live ledger version but missing any current deletion
    receipt, is quarantined (``BackupError``) and the target is
    left unmounted. The CLI cannot bypass this — there is no
    ``--no-quarantine`` flag, and there is no caller-supplied
    integer that can substitute for the manifest.
    """
    from memory_agent.application.backup import (
        read_live_privacy_ledger,
    )
    from memory_agent.contracts.backup import BackupRestoreParams
    try:
        req = BackupRestoreParams.model_validate(params)
    except Exception as exc:
        return _err(envelope.request_id, "backup.restore", "INVALID_REQUEST",
                    f"backup.restore: {exc}")
    bundle = Path(req.bundle).expanduser().resolve()
    target = Path(req.target).expanduser().resolve()
    # Read the live vault's authoritative privacy ledger. Any
    # failure here is fail-closed: refuse to restore and leave
    # ``target`` untouched. The ``vault`` argument is the
    # operator's live vault (``dispatch(vault_root=...)``),
    # NOT the empty restore target — that was the silent
    # resurrection bug.
    try:
        live_ledger = read_live_privacy_ledger(vault)
    except BackupError as exc:
        return _err(envelope.request_id, "backup.restore",
                    "INTERNAL", f"backup.restore: {exc}")
    try:
        result = BackupService(vault=vault).restore(
            bundle=bundle, target=target,
            live_privacy_ledger=live_ledger,
        )
    except BackupError as exc:
        return _err(envelope.request_id, "backup.restore",
                    "INVALID_REQUEST" if "quarantined" in str(exc)
                    else "INTERNAL",
                    f"backup.restore: {exc}")
    except Exception as exc:  # noqa: BLE001
        return _err(envelope.request_id, "backup.restore", "INTERNAL",
                    f"{type(exc).__name__}: {exc}")
    return _ok(envelope, result)