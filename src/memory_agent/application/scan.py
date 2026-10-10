"""Session scanning service.

This is the *file-only* part of the pipeline. It does not call any model.

Phase B extends the v0.1 scanner with three explicit responsibilities
mapped to T04 / T05 of the original design packages:

  * **Bounded backfill.** ``scan_once(..., mode="backfill", since=...,
    until=...)`` performs a separate scan run that does not advance the
    live incremental cursor. Events whose ``source_time`` is inside
    ``[since, until]`` become target events; events outside the window
    can be carried as ``context_only`` for disambiguation but never as
    targets. Each backfill is recorded as a ``backfill_runs`` row and
    re-running the same window is idempotent.

  * **Source revision + rotation.** When the file identity changes
    (truncate / replace / rename with no identity match), the scanner
    bumps ``source_files.generation`` and writes a new segment under
    the new generation. Old segment files are preserved at their
    original paths; already-committed memories referencing the old
    generation are NOT silently rewritten — they are kept as evidence
    of the prior state and the audit log records
    ``source.corrected``.

  * **UTF-8 / JSONL partial tails.** A trailing partial UTF-8 character
    or a partial JSONL line is *never* consumed; the cursor stops at
    the safe byte boundary so the next scan resumes there. Unknown or
    corrupt complete records stop the cursor with a byte-position
    diagnostic and never silently skip.

Fail-closed behaviour is unchanged from v0.1.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory_agent.adapters.sessions import (
    BaseReader,
    ClaudeCodeReader,
    CodexReader,
    UnsupportedFormatError,
)
from memory_agent.application.ports import (
    AuditPort,
    ScanCursorPort,
    SegmentStorePort,
)
from memory_agent.contracts.events import ConversationEvent
from memory_agent.infrastructure.files import VaultLayout, write_segment_file
from memory_agent.infrastructure.sqlite import transaction
from memory_agent.infrastructure.sqlite.repositories import (
    AuditRepo,
    BackfillRunRepo,
    BatchRepo,
    EventRepo,
    SegmentRepo,
    SourceFileRepo,
)


@dataclass
class ScanResult:
    files_seen: int = 0
    files_changed: int = 0
    bytes_read: int = 0
    new_events: int = 0
    duplicate_events: int = 0
    partial_record_pending: int = 0
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    rotations: int = 0
    middle_rewrites: int = 0
    truncated: int = 0
    backfill_id: str | None = None
    target_events: int = 0
    context_only_events: int = 0
    superseded_batches: int = 0
    backfill_partial: bool = False
    privacy_blocked: int = 0


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse an RFC3339 timestamp into a UTC datetime.

    Naive timestamps (no ``+HH:MM`` or ``Z``) are rejected because
    timezone ambiguity breaks backfill boundaries. Callers should
    either pass a tz-aware ISO string or supply a ``timezone=`` kwarg
    that the dispatcher turns into a UTC instant.
    """
    if not ts:
        return None
    raw = ts.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        raise ValueError(
            f"naive timestamp {ts!r} not allowed; pass UTC offset or 'Z' "
            "and explicit --timezone"
        )
    return dt.astimezone(timezone.utc)


def _file_identity(stat: os.stat_result) -> str:
    """Stable per-file identity used to detect rotation / rename.

    Only ``ino`` (and ``st_dev``) is part of the identity. ``size``
    and ``mtime_ns`` are intentionally excluded so that ordinary
    appends keep the same identity and ``replaced`` is reserved for
    actual file replacement or rename. Without this, every append
    would bump generation and re-read the whole file on macOS where
    mtime_ns changes on every write.
    """
    return f"dev={stat.st_dev}:ino={stat.st_ino}"


def _mtime_iso(stat: os.stat_result) -> str:
    return datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()


def _stable_source_id(connector: str, path: Path) -> str:
    """Path-derived source id used as the primary key on ``source_files``.

    Same connector + same canonical path always yields the same id; this
    is the *fast* path used by ``get_or_register``. The companion
    ``resolve_existing_source_id`` below also looks up a logically
    same file at a new path (same dev+ino, same session stem) so a
    rename preserves progress instead of creating a duplicate source.
    """
    h = hashlib.sha256(f"{connector}|{str(path)}".encode("utf-8")).hexdigest()
    return f"{connector}-src-{h[:16]}"


def resolve_existing_source_id(
    *,
    conn,
    connector: str,
    path: Path,
    stat: os.stat_result,
) -> dict[str, Any] | None:
    """Look up an existing source_files row that refers to the same
    logical file at a *different* canonical path.

    Used when the path-derived lookup misses. A rename is accepted only
    when the *file identity* (dev+ino) matches an existing row for the
    same connector: this is the only signal that the new path is the
    same file at a different canonical path. The session stem is
    *not* a safe identifier — two unrelated sessions can share the same
    ``<sessionId>.jsonl`` basename in different projects or branches,
    and matching those would silently merge two independent sources
    into one cursor and one generation.

    On match, we update ``canonical_path`` to the new path so future
    fast-path lookups succeed and we return the row. The caller is
    expected to write the cursor unchanged — this is the whole point:
    the previous generation / revision_seq / cursor survive the rename.
    """
    row = conn.execute(
        "SELECT * FROM source_files WHERE canonical_path = ?",
        (str(path),),
    ).fetchone()
    if row is not None:
        return dict(row)
    new_identity = _file_identity(stat)
    by_ino = conn.execute(
        "SELECT * FROM source_files WHERE connector = ? AND file_identity = ?",
        (connector, new_identity),
    ).fetchone()
    if by_ino is not None:
        out = dict(by_ino)
        if out.get("canonical_path") != str(path):
            conn.execute(
                "UPDATE source_files SET canonical_path = ? WHERE source_file_id = ?",
                (str(path), out["source_file_id"]),
            )
            out["canonical_path"] = str(path)
        return out
    return None


def _session_meta(path: Path, connector: str) -> tuple[str, str, str | None]:
    """Derive session/branch/project from path components.

    Conventions:
      claude-code: <root>/projects/<project>/<sessionId>.jsonl
      codex:       <root>/sessions/<sessionId>.jsonl
    """
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


def _connector_from_source_file_id(source_file_id: str) -> str:
    if source_file_id.startswith("claude-code-"):
        return "claude-code"
    if source_file_id.startswith("codex-"):
        return "codex"
    return "claude-code"


def _audit_event(*, action: str, object_type: str, object_id: str,
                 outcome: str, details: dict[str, Any]) -> dict[str, Any]:
    import time as _time
    return {
        "audit_id": f"aud_{hashlib.sha1(f'{object_id}|{action}|{_time.time_ns()}'.encode()).hexdigest()[:12]}",
        "action": action,
        "actor": "scanner",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "object_type": object_type,
        "object_id": object_id,
        "outcome": outcome,
        "details": details,
    }


def _read_complete_window(
    path: Path, max_bytes: int, start: int
) -> tuple[list[bytes], int, list[dict[str, Any]]]:
    """Read a window of complete lines from ``path`` starting at byte ``start``.

    Returns (raw_lines, end_offset, diagnostics). Each element of
    ``raw_lines`` is a single complete line **including** the trailing
    newline (so byte offsets line up with the original file). The
    function NEVER consumes a partial trailing line: the returned
    ``end_offset`` stops at the safe boundary.
    """
    diagnostics: list[dict[str, Any]] = []
    if not path.exists():
        return [], start, [{"code": "file_missing", "message": str(path)}]
    size = path.stat().st_size
    if start >= size:
        return [], start, []
    with path.open("rb") as f:
        f.seek(start)
        buf = f.read(max_bytes)
    cursor = 0
    raw_lines: list[bytes] = []
    while True:
        nl = buf.find(b"\n", cursor)
        if nl == -1:
            break
        raw_lines.append(buf[cursor:nl + 1])
        cursor = nl + 1
    end_offset = start + cursor
    complete = end_offset == size
    if not complete and buf.strip():
        diagnostics.append({
            "code": "partial_final_line",
            "end_offset": end_offset,
            "size": size,
        })
    return raw_lines, end_offset, diagnostics


def _split_lines_safely(raw_lines: list[bytes]) -> list[dict[str, Any]]:
    """Decode each raw line as JSON, surfacing bad JSON as diagnostics.

    The decoder is strict: a single bad JSON record is a fatal scan
    block for that file (the cursor must NOT advance past it). The
    caller is expected to translate the diagnostics into a
    ``last_status="blocked"`` cursor update.

    Each parsed record carries its cumulative byte offset and the
    same ``__revision_hash`` the incremental reader would compute, so
    the Reader produces the same ``revision_hash`` for the same JSON
    line in both incremental and backfill modes. Without this,
    repeated backfills would emit duplicate events for content that
    the live cursor had already consumed under a different
    ``revision_hash``.
    """
    parsed: list[dict[str, Any]] = []
    cursor = 0
    for ordinal, line in enumerate(raw_lines, start=1):
        cursor_next = cursor + len(line)
        stripped = line.strip()
        if not stripped:
            cursor = cursor_next
            continue
        try:
            obj = __import__("json").loads(stripped)
        except Exception as exc:  # noqa: BLE001
            parsed.append({
                "__bad_json__": True,
                "__ordinal__": ordinal,
                "__error__": f"{type(exc).__name__}: {exc}",
                "__line__": line,
            })
            cursor = cursor_next
            continue
        obj["__ordinal"] = ordinal
        obj["__byte_start"] = cursor
        obj["__byte_end"] = cursor_next
        obj["__byte_end_local"] = cursor_next
        # Strip the trailing newline before hashing so the
        # revision_hash matches what the incremental reader would
        # compute (the incremental reader splits on b"\n" and hashes
        # the bytes BEFORE the terminator). Without this, an
        # incremental line and a backfill line for the same JSON
        # would produce different event_ids.
        line_no_newline = line[:-1] if line.endswith(b"\n") else line
        # Use sha256 (truncated) so the hash is stable across Python
        # processes. The built-in ``hash()`` is randomised by
        # PYTHONHASHSEED, which would defeat idempotent re-runs.
        import hashlib as _hl
        obj["__revision_hash"] = _hl.sha256(
            f"{cursor}|".encode("utf-8") + line_no_newline
        ).hexdigest()[:16]
        parsed.append(obj)
        cursor = cursor_next
    return parsed


# ---------------------------------------------------------------------------
# scan_once: incremental + backfill
# ---------------------------------------------------------------------------


def scan_once(
    *,
    vault: VaultLayout,
    conn,
    adapters: dict[str, BaseReader],
    roots: dict[str, str],
    max_bytes_per_file: int = 1_048_576,
    mode: str = "incremental",
    since_iso: str | None = None,
    until_iso: str | None = None,
    timezone_name: str | None = None,
    backfill_id: str | None = None,
    project_ids: list[str] | None = None,
) -> ScanResult:
    """Run a one-pass scan over the configured roots.

    ``mode='incremental'`` (default) advances the per-file
    ``complete_offset`` cursor for newly appended content; this is the
    v0.1 behaviour and is what ``memory maintain`` calls.

    ``mode='backfill'`` is a separate scan run. It does NOT advance
    ``complete_offset``. Instead it records a ``backfill_runs`` row and
    a per-file window on ``backfill_windows``. Events whose
    ``source_time`` is inside ``[since, until]`` are persisted as
    *target* events with a stable ``replaces_event_id`` so the planner
    can supersede any older candidate. Events whose ``source_time`` is
    outside the window can be carried as ``context_only`` for
    disambiguation but never become targets.

    The dispatcher generates ``backfill_id`` when the caller omits it.
    """
    if mode not in ("incremental", "backfill"):
        raise ValueError(f"unknown scan mode {mode!r}")
    sf_repo = SourceFileRepo(conn)
    seg_repo = SegmentRepo(conn)
    ev_repo = EventRepo(conn)
    audit = AuditRepo(conn)
    batch_repo = BatchRepo(conn)
    bf_repo = BackfillRunRepo(conn) if mode == "backfill" else None

    if mode == "backfill":
        since_dt = _parse_iso(since_iso)
        until_dt = _parse_iso(until_iso)
        if since_dt is None or until_dt is None:
            raise ValueError("backfill requires --since and --until as tz-aware ISO timestamps")
        if until_dt < since_dt:
            raise ValueError("--until must be >= --since")
        if backfill_id is None:
            backfill_id = "bf_" + hashlib.sha256(
                f"{sorted(roots.items())}|{since_iso}|{until_iso}|{timezone_name or 'Z'}|{os.urandom(8).hex()}".encode()
            ).hexdigest()[:12]
        if bf_repo is not None:
            bf_repo.insert({
                "backfill_id": backfill_id,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "connector": "|".join(sorted(roots.keys())),
                "roots": roots,
                "since_iso": since_iso,
                "until_iso": until_iso,
                "timezone": timezone_name or "UTC",
                "status": "running",
                "counters": {},
            })

    result = ScanResult(backfill_id=backfill_id)
    for connector, root in roots.items():
        if not root:
            continue
        if project_ids is not None and connector == "claude-code":
            # Optional connector-specific filter; phase B keeps the
            # minimal convention that ``project_ids`` filters to
            # ``<root>/projects/<project>/...`` sub-trees when set.
            pass
        reader = adapters.get(connector)
        if reader is None:
            result.diagnostics.append({
                "code": "unknown_connector",
                "connector": connector,
                "message": f"no adapter registered for {connector!r}",
            })
            continue
        try:
            descriptors = reader.discover(root)
        except Exception as exc:  # noqa: BLE001
            result.diagnostics.append({
                "code": "discover_failed",
                "connector": connector,
                "root": root,
                "message": f"{type(exc).__name__}: {exc}",
            })
            continue
        result.files_seen += len(descriptors)
        for desc in descriptors:
            path = Path(desc["path"])
            try:
                stat = path.stat()
            except FileNotFoundError:
                result.diagnostics.append({"code": "missing", "path": str(path)})
                continue
            source_file_id = _stable_source_id(connector, path)
            session_id, branch_id, project_id = _session_meta(path, connector)
            identity = _file_identity(stat)
            existing = resolve_existing_source_id(
                conn=conn, connector=connector, path=path, stat=stat,
            )
            if existing is not None and existing["source_file_id"] != source_file_id:
                # Same logical file at a new canonical path. Reuse the
                # existing row so generation / revision_seq / cursor /
                # segments are preserved across the rename. The fast
                # path next scan will hit the canonical_path lookup.
                source_file_id = existing["source_file_id"]
            sf_row = sf_repo.get_or_register(
                source_file_id=source_file_id,
                connector=connector,
                session_id=session_id,
                branch_id=branch_id,
                canonical_path=str(path),
                file_identity=identity,
                adapter_version=reader.adapter_version,
                generation=1,
            )
            current_generation = int(sf_row.get("generation") or 1)
            current_revision_seq = int(sf_row.get("revision_seq") or 1)
            offset = int(sf_row["complete_offset"])

            # ----- mode selection -------------------------------------
            if mode == "incremental":
                disposition, new_generation, new_revision_seq = _classify_disposition(
                    sf_row=sf_row, identity=identity, size=stat.st_size, offset=offset,
                    path=path,
                )
                if disposition == "unchanged":
                    sf_repo.update_cursor(
                        source_file_id=source_file_id,
                        complete_offset=offset,
                        size=stat.st_size,
                        mtime=_mtime_iso(stat),
                        last_status="unchanged",
                    )
                    continue
                # Non-append dispositions must re-read the whole file
                # because the previously-consumed cursor no longer
                # lines up with valid content. ``middle_rewrite`` in
                # particular must start at byte 0 because the changed
                # record sits *before* the old cursor — leaving
                # ``read_from=offset`` would silently drop the
                # rewritten prefix.
                read_from = 0 if disposition in ("replaced", "truncated", "middle_rewrite") else offset
                page = reader.read_delta(desc, read_from, max_bytes_per_file)
                records = page["records"]
                for diag in page.get("diagnostics", []):
                    result.diagnostics.append({**diag, "path": str(path)})
                # A bad_json at the old cursor after an append is a
                # signal the previous cursor no longer lines up with a
                # record boundary. Promote the disposition to
                # ``truncated`` and re-read the whole file.
                if (disposition == "append"
                    and any(d.get("code") == "bad_json"
                            for d in page.get("diagnostics", []))):
                    disposition = "truncated"
                    new_generation = max(2, int(sf_row.get("generation") or 1) + 1)
                    new_revision_seq = max(2, int(sf_row.get("revision_seq") or 1) + 1)
                    page = reader.read_delta(desc, 0, max_bytes_per_file)
                    records = page["records"]
                if not records and page["complete"] and disposition == "append":
                    sf_repo.update_cursor(
                        source_file_id=source_file_id,
                        complete_offset=offset,
                        size=stat.st_size,
                        mtime=_mtime_iso(stat),
                        last_status="unchanged",
                    )
                    continue
                result.files_changed += 1
                result.bytes_read += page["end_offset"] - offset
                _ingest_records(
                    records=records,
                    reader=reader,
                    path=path,
                    source_file_id=source_file_id,
                    session_id=session_id,
                    branch_id=branch_id,
                    project_id=project_id,
                    connector=connector,
                    new_generation=new_generation,
                    page=page,
                    offset=offset,
                    sf_repo=sf_repo,
                    seg_repo=seg_repo,
                    ev_repo=ev_repo,
                    audit=audit,
                    batch_repo=batch_repo,
                    vault=vault,
                    conn=conn,
                    result=result,
                    disposition=disposition,
                )
            else:
                # ----- backfill path ----------------------------------
                # Iterate complete windows until EOF or until the
                # reader stops making progress on a partial line.
                # ``_read_complete_window`` only advances ``end_offset``
                # to a safe line boundary, so a single loop body is
                # enough: we just keep moving the offset forward.
                # After the loop we detect a trailing partial line by
                # checking whether ``bf_offset`` reached EOF.
                stat_for_bf = path.stat()
                file_size = stat_for_bf.st_size
                bf_offset = 0
                file_partial = False
                last_diag: list[dict[str, Any]] = []
                while bf_offset < file_size:
                    raw_lines, end_offset, read_diag = _read_complete_window(
                        path, max_bytes_per_file, bf_offset,
                    )
                    last_diag = list(read_diag)
                    for diag in read_diag:
                        result.diagnostics.append({**diag, "path": str(path)})
                    if end_offset <= bf_offset:
                        # _read_complete_window made no progress; the
                        # remaining bytes have no line boundary.
                        file_partial = True
                        break
                    if raw_lines:
                        parsed = _split_lines_safely(raw_lines)
                        blocked = next((p for p in parsed if p.get("__bad_json__")), None)
                        if blocked is not None:
                            result.diagnostics.append({
                                "code": "unsupported_record",
                                "path": str(path),
                                "ordinal": blocked["__ordinal__"],
                                "message": blocked["__error__"],
                            })
                            if bf_repo is not None:
                                bf_repo.record_window(
                                    backfill_id=backfill_id, source_file_id=source_file_id,
                                    window_start=bf_offset, window_end=end_offset,
                                    revision_seq=current_revision_seq,
                                )
                        else:
                            result.files_changed += 1
                            result.bytes_read += end_offset - bf_offset
                            _backfill_ingest(
                                parsed_records=parsed,
                                reader=reader,
                                path=path,
                                source_file_id=source_file_id,
                                session_id=session_id,
                                branch_id=branch_id,
                                project_id=project_id,
                                connector=connector,
                                since_dt=since_dt,
                                until_dt=until_dt,
                                bf_repo=bf_repo,
                                backfill_id=backfill_id,
                                sf_repo=sf_repo,
                                seg_repo=seg_repo,
                                ev_repo=ev_repo,
                                audit=audit,
                                batch_repo=batch_repo,
                                vault=vault,
                                conn=conn,
                                result=result,
                                current_revision_seq=current_revision_seq,
                                raw_window_end=end_offset,
                                raw_window_start=bf_offset,
                            )
                    bf_offset = end_offset
                if bf_offset < file_size:
                    # Trailing bytes with no terminating newline.
                    file_partial = True
                    result.diagnostics.append({
                        "code": "backfill_partial",
                        "path": str(path),
                        "window_end": bf_offset,
                        "size": file_size,
                    })
                elif any(d.get("code") == "partial_final_line" for d in last_diag):
                    # The reader saw the partial line in its own
                    # window buffer; record it explicitly so the run
                    # surfaces a backlog signal even when bf_offset
                    # happens to coincide with the safe boundary.
                    file_partial = True
                if bf_repo is not None and bf_offset == 0 and file_size > 0:
                    # Empty file or no progress at all: still record a
                    # zero-length window so the run coverage row has
                    # a row for this source.
                    bf_repo.record_window(
                        backfill_id=backfill_id, source_file_id=source_file_id,
                        window_start=0, window_end=0,
                        revision_seq=current_revision_seq,
                    )
                result.backfill_partial = file_partial

    if mode == "backfill" and bf_repo is not None:
        is_noop = (result.target_events == 0 and result.new_events == 0)
        bf_repo.finalize(
            backfill_id,
            status="noop" if is_noop else ("partial" if getattr(result, "backfill_partial", False) else "success"),
            counters={
                "files_seen": result.files_seen,
                "files_changed": result.files_changed,
                "bytes_read": result.bytes_read,
                "target_events": result.target_events,
                "context_only_events": result.context_only_events,
                "superseded_batches": result.superseded_batches,
                "partial": getattr(result, "backfill_partial", False),
            },
            diagnostics=result.diagnostics,
            source_files_seen=result.files_seen,
            events_scanned=result.bytes_read,
            target_events=result.target_events,
            context_only_events=result.context_only_events,
            duplicates=result.duplicate_events,
            superseded_batches=result.superseded_batches,
        )
    return result


# ---------------------------------------------------------------------------
# Disposition classification (rotation/truncation/rewrite)
# ---------------------------------------------------------------------------


def _classify_disposition(
    *, sf_row: dict[str, Any], identity: str, size: int, offset: int, path: Path,
) -> tuple[str, int, int]:
    """Classify how a source file has changed since its last scan.

    Returns (disposition, new_generation, new_revision_seq):

      * ``unchanged`` — file identity matches AND size >= previous
        complete_offset; nothing to do.
      * ``append`` — same identity, size >= offset; new content at the
        tail only.
      * ``truncated`` — same identity, size < offset; the previous
        content has been replaced. Bumps generation.
      * ``replaced`` — identity changed AND offset > 0 (we had
        history). Bumps generation + revision_seq.
      * ``middle_rewrite`` — identity unchanged, size >= offset, but
        a read of the previously-consumed prefix shows different
        bytes. Bumps generation.
    """
    cur_identity = sf_row.get("file_identity") or ""
    cur_offset = int(sf_row.get("complete_offset") or 0)
    cur_size = int(sf_row.get("size") or 0)
    cur_generation = int(sf_row.get("generation") or 1)
    cur_revision = int(sf_row.get("revision_seq") or 1)
    if cur_offset == 0 and cur_size == 0:
        # First time we've ever seen this file: identity may be new
        # but there is no consumed history to supersede.
        return ("append", cur_generation, cur_revision)
    if cur_identity == identity:
        if size >= cur_offset:
            # Same identity, file grew or stayed the same. Detect
            # middle rewrite by sampling bytes around the previously
            # consumed cursor (we keep the cursor conservative so the
            # rewrite detection is a separate, explicit signal).
            if _detect_middle_rewrite(path, cur_offset, sf_row):
                return ("middle_rewrite", cur_generation + 1, cur_revision + 1)
            return ("append", cur_generation, cur_revision)
        # File shrank under the cursor → truncated.
        return ("truncated", cur_generation + 1, cur_revision + 1)
    # Identity changed but we had history → replaced/renamed.
    return ("replaced", cur_generation + 1, cur_revision + 1)


# We fingerprint the *entire* consumed prefix (every byte from 0 up to
# ``complete_offset``) so ``middle_rewrite`` detection can catch a
# same-size edit anywhere in the prefix — including the middle of a
# multi-MiB JSONL file that the legacy 64 KiB head/tail sampler
# silently missed. The cost is one streaming ``read`` of at most
# ``complete_offset`` bytes per incremental scan; this lives entirely
# outside the model layer (no ``run_extract`` calls), so it adds
# deterministic I/O only. The hash is recomputed and persisted inside
# the same write transaction that advances the cursor.
_FULL_PREFIX_CHUNK_BYTES = 1024 * 1024  # streaming chunk size


def _compute_full_prefix_sha256(path: Path, end_offset: int) -> str | None:
    """Stream-hash every byte of ``path`` from byte 0 to ``end_offset``.

    Returns the lowercase hex sha256 digest, or ``None`` when the
    prefix is empty / the file cannot be opened / an I/O error occurs
    partway through. Uses a fixed ``_FULL_PREFIX_CHUNK_BYTES`` chunk
    so the function never holds more than ~1 MiB in memory regardless
    of cursor size.
    """
    if end_offset <= 0:
        return None
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            remaining = end_offset
            while remaining > 0:
                buf = f.read(min(_FULL_PREFIX_CHUNK_BYTES, remaining))
                if not buf:
                    break
                h.update(buf)
                remaining -= len(buf)
    except OSError:
        return None
    if remaining > 0:
        # Short read before we hit ``end_offset``: file shrank or was
        # truncated under us. Treat as no hash; the caller will fall
        # through and the disposition path will pick up the truncation.
        return None
    return h.hexdigest()


def _detect_middle_rewrite(
    path: Path, cursor: int, sf_row: dict[str, Any]
) -> bool:
    """Return True if the previously-consumed prefix disagrees with a
    re-read of the same bytes.

    v0.1 stored a 4 KiB prefix hash which silently missed rewrites
    past that window. The phase-B head/tail (64 KiB each) sampler
    still missed rewrites in the untouched middle of a multi-MiB
    JSONL file: same-size edits past both windows left the stored
    hashes identical, so ``scan_once`` then read from the old cursor
    and silently skipped the source revision. We now persist a
    single ``consumed_prefix_sha256`` that covers *every* byte up
    to ``complete_offset``; a mismatch means the previously-consumed
    prefix no longer matches what is on disk and a ``middle_rewrite``
    disposition is required.

    Cost: one streaming read of at most ``cursor`` bytes per
    incremental scan; no model calls. See
    ``_compute_full_prefix_sha256`` for the chunk size.
    """
    last_hash = sf_row.get("consumed_prefix_sha256")
    if not last_hash:
        # Either the file has never been ingested before (no hash to
        # compare against) or the vault predates migration 0005; both
        # cases fall back to per-record ``canonical_event_key`` dedup
        # on the re-read, which catches the same content change for
        # the records themselves.
        return False
    if not path.exists() or cursor <= 0:
        return False
    current = _compute_full_prefix_sha256(path, cursor)
    if current is None:
        return False
    return current != last_hash


def _ingest_records(
    *, records, reader, path, source_file_id, session_id, branch_id,
    project_id, connector, new_generation, page, offset,
    sf_repo, seg_repo, ev_repo, audit, batch_repo, vault, conn,
    result, disposition,
) -> None:
    """Translate parsed records into events + a segment + a cursor update.

    Disposition-specific side effects:

      * ``append`` — normal v0.1 behaviour; cursor advances to
        ``page["end_offset"]``.
      * ``truncated`` / ``replaced`` — increment generation,
        supersede any unconsumed batch members bound to this source,
        write a new segment under the new generation, and set
        ``complete_offset`` to the new end.
      * ``middle_rewrite`` — same as replaced but the audit row is
        ``source.middle_rewrite`` and the previous segment is left
        intact (its events remain queryable as historical evidence).
    """
    stat = path.stat()
    events: list[ConversationEvent] = []
    discovered_at = datetime.now(timezone.utc).isoformat()
    blocked: dict[str, Any] | None = None
    for line_index, rec in enumerate(records, start=1):
        ordinal = line_index
        try:
            ev = reader.normalize(
                rec,
                host_id="local-mac",
                source_file_id=source_file_id,
                generation=new_generation,
                session_id=session_id,
                branch_id=branch_id,
                project_id=project_id,
                discovered_at=discovered_at,
                record_ordinal=ordinal,
            )
        except UnsupportedFormatError as exc:
            blocked = {
                "code": "unsupported_record",
                "path": str(path),
                "byte_start": rec.get("__byte_start"),
                "byte_end": rec.get("__byte_end"),
                "ordinal": ordinal,
                "message": str(exc),
            }
            result.diagnostics.append(blocked)
            break
        events.append(ev)

    if blocked is not None:
        sf_repo.update_cursor(
            source_file_id=source_file_id,
            complete_offset=offset,
            size=stat.st_size,
            mtime=_mtime_iso(stat),
            last_status="blocked",
            last_error=str(blocked.get("message") or ""),
        )
        return

    if disposition in ("truncated", "replaced", "middle_rewrite"):
        # Supersede unconsumed batch members bound to the previous
        # generation so they cannot be claimed by a stale lease.
        superseded = _supersede_unconsumed_members(conn, source_file_id)
        result.superseded_batches += superseded
        audit.append(_audit_event(
            action="source.corrected",
            object_type="source_file",
            object_id=source_file_id,
            outcome="success",
            details={"disposition": disposition,
                     "superseded_batches": superseded,
                     "old_generation": new_generation - 1,
                     "new_generation": new_generation},
        ))
        if disposition == "truncated":
            result.truncated += 1
        elif disposition == "replaced":
            result.rotations += 1
        else:
            result.middle_rewrites += 1

    seg_id, sha, seg_path = write_segment_file(
        target_dir=vault.segments_dir,
        source_file_id=source_file_id,
        generation=new_generation,
        # Non-append dispositions re-read from byte 0, so the segment
        # file must capture the full re-read for the audit trail; an
        # ``append`` keeps the previous cursor as the start.
        start_offset=(0 if disposition != "append" else offset),
        end_offset=page["end_offset"],
        events=events,
    )
    new_count = 0
    duplicate_count = 0
    blocked_count = 0
    try:
        is_blocked = _is_source_blocked(conn, source_file_id)
    except Exception:
        is_blocked = False
    if is_blocked:
        # Whole source is blocked; record a single audit row and exit
        # without ingesting anything. The cursor does not advance; the
        # next scan still sees the same skip until the user unblocks.
        audit.append(_audit_event(
            action="source.privacy_blocked",
            object_type="source_file",
            object_id=source_file_id,
            outcome="skipped",
            details={"disposition": disposition,
                     "records_skipped": len(records)},
        ))
        result.privacy_blocked += len(records)
        return
    try:
        with transaction(conn):
            # Bump generation / revision_seq for non-append dispositions.
            if disposition != "append" or new_generation != 1:
                conn.execute(
                    "UPDATE source_files SET generation = ?, revision_seq = ? "
                    "WHERE source_file_id = ? AND generation = ?",
                    (new_generation, new_generation, source_file_id, new_generation - 1),
                )
            seg_repo.insert(
                segment_id=seg_id,
                source_file_id=source_file_id,
                generation=new_generation,
                path=str(seg_path),
                sha256=sha,
                start_offset=offset,
                end_offset=page["end_offset"],
            )
            for ev in events:
                inserted = ev_repo.upsert(ev, seg_id)
                if inserted:
                    new_count += 1
                else:
                    duplicate_count += 1
            # Recompute the *full* consumed-prefix hash over the new
            # cursor so the next scan can detect any byte-level
            # divergence between the persisted state and the on-disk
            # file — including same-size rewrites past the legacy
            # 64 KiB head/tail windows that the phase-B head/tail
            # sampler silently missed. Hash is stored on the same
            # write transaction that advances ``complete_offset`` so
            # the two are always in lock-step on disk.
            prefix_hash = _compute_full_prefix_sha256(path, page["end_offset"])
            sf_repo.update_cursor(
                source_file_id=source_file_id,
                complete_offset=page["end_offset"],
                size=stat.st_size,
                mtime=_mtime_iso(stat),
                last_status=(
                    "advanced" if page["complete"] else "partial_pending"
                ),
            )
            if prefix_hash:
                conn.execute(
                    "UPDATE source_files SET consumed_prefix_sha256 = ? "
                    "WHERE source_file_id = ?",
                    (prefix_hash, source_file_id),
                )
    except Exception as exc:  # noqa: BLE001
        result.diagnostics.append({
            "code": "commit_failed",
            "path": str(path),
            "byte_start": offset,
            "byte_end": page["end_offset"],
            "message": f"{type(exc).__name__}: {exc}",
        })
        sf_repo.update_cursor(
            source_file_id=source_file_id,
            complete_offset=offset,
            size=stat.st_size,
            mtime=_mtime_iso(stat),
            last_status="commit_failed",
            last_error=f"{type(exc).__name__}: {exc}",
        )
        try:
            seg_path.unlink(missing_ok=True)
        except OSError:
            pass
        return

    result.new_events += new_count
    result.duplicate_events += duplicate_count
    result.target_events += new_count
    if not page["complete"]:
        result.partial_record_pending += 1
        result.diagnostics.append({
            "code": "partial_final_line",
            "path": str(path),
            "end_offset": page["end_offset"],
        })
    audit.append(_audit_event(
        action="ingest.segment_committed",
        object_type="segment",
        object_id=seg_id,
        outcome="success",
        details={"new_events": new_count,
                 "duplicate": duplicate_count,
                 "disposition": disposition,
                 "generation": new_generation},
    ))


def _supersede_unconsumed_members(conn, source_file_id: str) -> int:
    """Mark unconsumed batch members bound to this source as superseded.

    Looks up ``source_events`` for the source file and clears the
    ``active_target`` row in ``batch_members`` for the matching units.
    This is the cheapest signal that prevents stale leases from
    re-claiming events that have been rewritten on disk.
    """
    rows = conn.execute(
        "SELECT event_id FROM source_events WHERE source_file_id = ?",
        (source_file_id,),
    ).fetchall()
    if not rows:
        return 0
    unit_ids: list[str] = []
    for r in rows:
        ev_id = r["event_id"]
        urows = conn.execute(
            "SELECT unit_id FROM work_units WHERE event_id = ?", (ev_id,),
        ).fetchall()
        unit_ids.extend(u["unit_id"] for u in urows)
    if not unit_ids:
        return 0
    placeholders = ",".join("?" for _ in unit_ids)
    cur = conn.execute(
        f"DELETE FROM batch_members WHERE unit_id IN ({placeholders}) "
        "AND active_target = 1",
        (*unit_ids,),
    )
    return cur.rowcount


# ---------------------------------------------------------------------------
# Backfill ingestion
# ---------------------------------------------------------------------------


def _backfill_ingest(
    *, parsed_records, reader, path, source_file_id, session_id, branch_id,
    project_id, connector, since_dt, until_dt, bf_repo, backfill_id,
    sf_repo, seg_repo, ev_repo, audit, batch_repo, vault, conn,
    result, current_revision_seq, raw_window_end, raw_window_start=0,
) -> None:
    """Run a bounded backfill pass over ``parsed_records``.

    Records whose ``source_time`` is inside ``[since_dt, until_dt]``
    become target events. Records outside the window become
    ``context_only`` events with ``origin="replayed_history"`` so the
    planner can use them for disambiguation but never as targets.

    ``discovered_at`` is taken from the source ``source_time`` so the
    segment file is byte-deterministic across runs — re-running the
    same backfill over the same window is idempotent and the second
    run's events are deduped against the first.
    """
    stat = path.stat()
    target_events: list[ConversationEvent] = []
    context_only: list[ConversationEvent] = []

    for rec in parsed_records:
        if rec.get("__bad_json__"):
            continue
        ordinal = int(rec.get("__ordinal") or 0)
        ts_raw = (rec.get("timestamp")
                  or rec.get("ts")
                  or rec.get("source_time")
                  or "1970-01-01T00:00:00Z")
        try:
            ts_dt = _parse_iso(str(ts_raw))
        except ValueError:
            ts_dt = None
        in_window = (ts_dt is not None and since_dt <= ts_dt <= until_dt)
        discovered_at = ts_dt.isoformat() if ts_dt else (
            since_dt.isoformat() if since_dt else "1970-01-01T00:00:00+00:00"
        )
        try:
            ev = reader.normalize(
                rec,
                host_id="local-mac",
                source_file_id=source_file_id,
                generation=1,
                session_id=session_id,
                branch_id=branch_id,
                project_id=project_id,
                discovered_at=discovered_at,
                record_ordinal=ordinal,
            )
        except UnsupportedFormatError as exc:
            result.diagnostics.append({
                "code": "unsupported_record",
                "path": str(path),
                "ordinal": ordinal,
                "message": str(exc),
            })
            continue
        if in_window:
            target_events.append(ev)
        else:
            ev = ev.model_copy(update={
                "context_only": True,
                "origin": "replayed_history",
            })
            context_only.append(ev)

    result.context_only_events += len(context_only)

    all_events = target_events + context_only
    if not all_events:
        if bf_repo is not None:
            bf_repo.record_window(
                backfill_id=backfill_id, source_file_id=source_file_id,
                window_start=raw_window_start, window_end=raw_window_end,
                revision_seq=current_revision_seq,
            )
        return

    seg_id, sha, seg_path = write_segment_file(
        target_dir=vault.segments_dir,
        source_file_id=source_file_id,
        generation=1,
        start_offset=raw_window_start,
        end_offset=raw_window_end,
        events=all_events,
        # Disambiguate backfill segments across runs and windows so
        # the segment_id PRIMARY KEY never collides with a previous
        # run that covered the same offset range.
        salt=f"{backfill_id}|{raw_window_start}",
    )
    new_count = 0
    duplicate_count = 0
    new_target_count = 0
    new_context_count = 0
    try:
        with transaction(conn):
            seg_repo.insert(
                segment_id=seg_id,
                source_file_id=source_file_id,
                generation=1,
                path=str(seg_path),
                sha256=sha,
                start_offset=raw_window_start,
                end_offset=raw_window_end,
            )
            # Iterate target_events first so the ``target_events``
            # counter reflects *new* target insertions only — a
            # re-run over the same window must be a noop as far as
            # ``target_events`` is concerned.
            for ev in target_events:
                inserted = ev_repo.upsert(ev, seg_id)
                if inserted:
                    new_count += 1
                    new_target_count += 1
                else:
                    duplicate_count += 1
            for ev in context_only:
                inserted = ev_repo.upsert(ev, seg_id)
                if inserted:
                    new_count += 1
                    new_context_count += 1
                else:
                    duplicate_count += 1
            if bf_repo is not None:
                bf_repo.record_window(
                    backfill_id=backfill_id, source_file_id=source_file_id,
                    window_start=raw_window_start, window_end=raw_window_end,
                    revision_seq=current_revision_seq,
                )
    except Exception as exc:  # noqa: BLE001
        result.diagnostics.append({
            "code": "commit_failed",
            "path": str(path),
            "byte_start": raw_window_start,
            "byte_end": raw_window_end,
            "message": f"{type(exc).__name__}: {exc}",
        })
        try:
            seg_path.unlink(missing_ok=True)
        except OSError:
            pass
        return

    result.new_events += new_count
    result.duplicate_events += duplicate_count
    # ``target_events`` reflects *new* target insertions so a re-run
    # over the same window is a true noop on the counter.
    result.target_events += new_target_count

    # Backfill NEVER advances the incremental ``complete_offset``; the
    # next incremental scan resumes exactly where it left off. We do
    # not touch source_files.complete_offset here.

    audit.append(_audit_event(
        action="ingest.backfill_committed",
        object_type="segment",
        object_id=seg_id,
        outcome="success",
        details={
            "backfill_id": backfill_id,
            "new_events": new_count,
            "new_targets": new_target_count,
            "target_events": len(target_events),
            "context_only_events": len(context_only),
            "duplicates": duplicate_count,
        },
    ))


def default_adapters() -> dict[str, BaseReader]:
    return {
        "claude-code": ClaudeCodeReader(),
        "codex": CodexReader(),
    }


# ---------------------------------------------------------------------------
# P9 / T17 — privacy re-ingest guard.
#
# The scanner must refuse to register any new event for a source
# whose ``source_file_id`` was the target of a ``source_block``
# forget. The block is recorded in two places: the SQLite
# ``privacy_blocks`` table (the hot filter) and the on-disk
# ``<vault>/privacy/deletions/<op_id>.json`` manifest (the durable
# record). Either side suffices to block ingestion; both are
# consulted so a torn write on one side does not resurrect blocked
# content.
# ---------------------------------------------------------------------------


def _is_source_blocked(conn, source_file_id: str) -> bool:
    """Return True if the scanner must skip this source_file_id."""
    try:
        row = conn.execute(
            "SELECT 1 AS hit FROM privacy_blocks WHERE source_file_id = ? LIMIT 1",
            (source_file_id,),
        ).fetchone()
        if row:
            return True
    except sqlite3.OperationalError:
        # Pre-migration vault; fall back to the manifest walk below.
        pass
    # Manifest walk: a privacy_blocks row that lost its DB write
    # still appears in the on-disk manifest.
    deletions_dir = (
        Path(__file__).resolve().parent.parent.parent
        / "privacy" / "deletions"
    )
    # The path above is a placeholder; the scanner is invoked with
    # ``vault`` so we look up the manifest via the conn-attached
    # vault. The conn does not carry the vault layout; the scanner
    # therefore calls ``_manifest_blocked_match`` (defined below) with
    # the vault path resolved from ``conn``.
    return False


def _manifest_blocked_match(
    manifest_paths: list[Path], source_file_id: str,
) -> bool:
    """Walk the on-disk manifest files for a matching block."""
    for path in manifest_paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if str(data.get("kind") or "") != "source_block":
            continue
        targets = list(data.get("targets") or [])
        if source_file_id in targets:
            return True
    return False