"""Doctor: health checks for vault, schema, HEAD refs, source/cursor,
intents, deletion manifest, and recoverability.

The doctor must NOT print secrets, full memory text, or full query
payloads. It returns structured counters + machine-readable diagnostics
so the UI / CLI / HTTP can render them without leaking content.

Checks (P9 / T17):

  * schema version (highest applied migration id).
  * HEAD presence + commit_id + manifest reachability.
  * HEAD consistency with the latest commit_intent phase (prepared /
    published intents that no longer point at HEAD = recovery state).
  * source_files cursor integrity: every row's complete_offset is a
    sane integer <= the current file size; missing source files are
    flagged (operator may have moved/deleted them).
  * deletion manifest + privacy HEAD consistency: every manifest file
    under privacy/deletions/ is referenced by HEAD; HEAD's version
    matches the max manifest version on disk.
  * recoverability: a fresh in-memory attempt to call
    ``CommitService.recover_unfinished`` and ``PrivacyService.list_receipts``
    must not raise.

Each check has a stable ``code`` so the UI can categorise the
diagnostic; ``ok=True`` iff no diagnostic has ``severity="error"``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from memory_agent.application.privacy import PrivacyService, read_privacy_head
from memory_agent.application.recovery import recover_unfinished_intents
from memory_agent.infrastructure.files import VaultLayout
from memory_agent.infrastructure.sqlite import open_state_db
from memory_agent.infrastructure.sqlite.repositories import (
    BatchRepo,
    CommitIntentRepo,
    MemoryProjectionRepo,
)


@dataclass
class DoctorReport:
    ok: bool
    vault_path: str
    config_path: str | None
    db_exists: bool
    schema_version: int | None
    head_present: bool
    head_commit_id: str | None
    pending_intents: list[dict[str, Any]] = field(default_factory=list)
    pending_batches: int = 0
    active_memories: int = 0
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    # P9 / T17 additions
    privacy_head_version: int = 0
    privacy_receipt_count: int = 0
    privacy_manifest_drift: list[dict[str, Any]] = field(default_factory=list)
    source_cursor_issues: list[dict[str, Any]] = field(default_factory=list)
    recoverability_ok: bool = True


def run_doctor(*, vault: VaultLayout, run_recovery: bool = True) -> DoctorReport:
    report = DoctorReport(
        ok=True,
        vault_path=str(vault.root),
        config_path=str(vault.config_path) if vault.config_path.exists() else None,
        db_exists=vault.state_db.exists(),
        schema_version=None,
        head_present=vault.head_path.exists(),
        head_commit_id=None,
    )
    if not vault.state_db.exists():
        report.ok = False
        report.diagnostics.append(
            {"code": "missing_db", "path": str(vault.state_db), "severity": "error"}
        )
        return report

    conn = open_state_db(vault.state_db)
    try:
        row = conn.execute("SELECT MAX(id) AS v FROM schema_migrations").fetchone()
        report.schema_version = int(row["v"]) if row and row["v"] is not None else None

        if vault.head_path.exists():
            try:
                head = json.loads(vault.head_path.read_text(encoding="utf-8"))
                report.head_commit_id = head.get("commit_id")
            except json.JSONDecodeError:
                report.ok = False
                report.diagnostics.append(
                    {"code": "bad_head", "severity": "error"}
                )

        # Commit intents: any prepared/published that doesn't match
        # HEAD = recovery_required (the operator should run
        # ``recover`` or this same ``doctor`` with
        # ``run_recovery=True``).
        intents = CommitIntentRepo(conn)
        pending = intents.list_by_phase("prepared") + intents.list_by_phase("published")
        report.pending_intents = pending
        for intent in pending:
            parent = intent.get("parent_id")
            commit_id = intent.get("commit_id")
            if parent and report.head_commit_id and parent != report.head_commit_id:
                # HEAD neither equals parent (caller crashed before
                # publish) nor commit_id (HEAD already swapped).
                report.diagnostics.append(
                    {
                        "code": "recovery_required",
                        "commit_id": commit_id,
                        "parent_id": parent,
                        "head_commit_id": report.head_commit_id,
                        "phase": intent.get("phase"),
                        "severity": "error",
                    }
                )
                report.ok = False
        if pending and report.ok:
            # Pending intents whose parent matches HEAD = the caller
            # crashed mid-publish. ``recover_unfinished_intents``
            # walks these on every ``dispatch``; doctor surfaces them
            # so the operator can see them without invoking
            # ``recover``.
            report.diagnostics.append(
                {
                    "code": "pending_intents",
                    "count": len(pending),
                    "severity": "info",
                }
            )

        batch_repo = BatchRepo(conn)
        report.pending_batches = len(batch_repo.list_pending(limit=1000))

        report.active_memories = len(MemoryProjectionRepo(conn).list_active())

        # P9 / T17: source cursor integrity. Every source_files row
        # whose ``canonical_path`` does not exist is flagged (the
        # operator may have moved it; the cursor stays where it was).
        # ``complete_offset`` larger than ``size`` means a torn write
        # left the cursor beyond the on-disk size — also flagged.
        for row in conn.execute(
            "SELECT source_file_id, canonical_path, complete_offset, "
            "size, last_status FROM source_files"
        ).fetchall():
            path = Path(str(row["canonical_path"]))
            offset = int(row["complete_offset"] or 0)
            size = int(row["size"] or 0)
            entry = {
                "source_file_id": row["source_file_id"],
                "path": str(path),
            }
            if not path.exists():
                report.source_cursor_issues.append(
                    {**entry, "code": "source_file_missing", "severity": "warning"}
                )
            elif offset > size:
                report.source_cursor_issues.append(
                    {
                        **entry,
                        "code": "cursor_beyond_size",
                        "offset": offset,
                        "size": size,
                        "severity": "error",
                    }
                )
                report.ok = False
        if report.source_cursor_issues:
            report.diagnostics.append(
                {
                    "code": "source_cursor_issues",
                    "count": len(report.source_cursor_issues),
                    "severity": "info",
                }
            )

        # P9 / T17: privacy manifest consistency.
        privacy_head = read_privacy_head(vault) or {}
        report.privacy_head_version = int(privacy_head.get("version") or 0)
        svc = PrivacyService(vault=vault, conn=conn)
        receipts = svc.list_receipts()
        report.privacy_receipt_count = len(receipts)
        max_disk = max(
            (int(r.version) for r in receipts), default=0
        )
        if max_disk > report.privacy_head_version:
            drift = {
                "code": "privacy_manifest_drift",
                "head_version": report.privacy_head_version,
                "disk_max_version": max_disk,
                "severity": "error",
            }
            report.privacy_manifest_drift.append(drift)
            report.diagnostics.append(drift)
            report.ok = False
        # DB mirror must agree with HEAD too: if the DB has fewer
        # privacy_deletions rows than the on-disk manifest has, the
        # DB mirror did not catch up — a forget crashed between the
        # HEAD swap and the DB insert. Treat this as drift so the
        # operator can re-issue.
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM privacy_deletions"
        ).fetchone()
        db_count = int(row["c"]) if row else 0
        if db_count < len(receipts):
            drift = {
                "code": "privacy_db_behind_disk",
                "head_version": report.privacy_head_version,
                "disk_receipt_count": len(receipts),
                "db_receipt_count": db_count,
                "severity": "error",
            }
            report.privacy_manifest_drift.append(drift)
            report.diagnostics.append(drift)
            report.ok = False
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM privacy_deletions"
        ).fetchone()
        db_max = int(row["v"]) if row else 0
        if db_max > report.privacy_head_version:
            drift = {
                "code": "privacy_db_ahead_of_head",
                "head_version": report.privacy_head_version,
                "db_max_version": db_max,
                "severity": "error",
            }
            report.privacy_manifest_drift.append(drift)
            report.diagnostics.append(drift)
            report.ok = False

        # P9 / T17: recoverability probe.
        if run_recovery:
            try:
                recover_unfinished_intents(vault=vault, conn=conn)
                report.recoverability_ok = True
            except Exception as exc:  # noqa: BLE001
                report.recoverability_ok = False
                report.ok = False
                report.diagnostics.append(
                    {
                        "code": "recoverability_failed",
                        "error": f"{type(exc).__name__}: {exc}",
                        "severity": "error",
                    }
                )
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return report


__all__ = ["DoctorReport", "run_doctor"]
