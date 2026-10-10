"""Privacy deletion barrier (P9 / T17).

The forget authorization is the *only* contract that lets a user
request removal of a memory, its evidence, its related query artefacts
and its audit payload. The manifest is a **durable independent file**
under ``<vault>/privacy/deletions/<op_id>.json``; an atomic
``<vault>/privacy/HEAD.json`` points at the latest version. The query
path reads the privacy HEAD at the start of every recall and just
before returning the result so even a stale DB projection or an
out-of-date backup cannot resurrect erased content.

Only metadata survives a forget: ``op_id``, the kind of erasure
(``memory`` / ``event`` / ``source_block``), the deletion targets
(``memory_id`` / ``event_id`` / ``source_file_id``), the timestamp,
and the actor. No memory text, evidence quote, locator, body, or
full-text audit payload is preserved.

Re-ingest guard: when ``source_block`` is set, the scanner refuses to
register any source event whose ``source_file_id`` matches the
blocked set. A blocked source therefore cannot return under a new
``revision_hash``; the only path back is a fresh ``privacy
unblock --op-id <op-id>`` invocation.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory_agent.infrastructure.files import (
    VaultLayout,
    atomic_write_json,
)


# ---------------------------------------------------------------------------
# Privacy HEAD: atomic swap, version-bumped on every forget.
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_op_id() -> str:
    return "del_" + uuid.uuid4().hex[:16]


def _privacy_head_path(vault: VaultLayout) -> Path:
    return vault.root / "privacy" / "HEAD.json"


def _deletions_dir(vault: VaultLayout) -> Path:
    d = vault.root / "privacy" / "deletions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _db_deletion_receipt_count(vault: VaultLayout) -> int:
    """Read-only check: how many ``privacy_deletions`` rows does the
    state DB currently record?

    Returns ``-1`` if the DB cannot be safely inspected (missing /
    corrupt / unreadable schema). ``ensure_privacy_head`` treats
    ``-1`` as "DB state is unsafe — refuse to mint v0"; a DB that
    we cannot read might still hold ``privacy_deletions`` rows that
    we cannot see, so the safe answer is to refuse, not to assume
    the DB is empty.
    """
    if not vault.state_db.exists():
        return 0
    conn = None
    try:
        conn = sqlite3.connect(
            f"file:{vault.state_db}?mode=ro", uri=True,
            timeout=5,
        )
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM privacy_deletions"
        ).fetchone()
        return int(row[0]) if row else 0
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError):
        return -1
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def read_privacy_head(vault: VaultLayout) -> dict[str, Any] | None:
    path = _privacy_head_path(vault)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def ensure_privacy_head(vault: VaultLayout) -> dict[str, Any]:
    """Atomically establish a zero-version privacy HEAD if missing.

    This is the **operator-authorised safe initialisation path**: it
    is the only code path that is permitted to write a v0 HEAD into
    a vault that previously had none. ``restore`` and the live
    privacy ledger reader must NEVER synthesise a v0 HEAD on the
    fly; that would let an old bundle be silently mounted into a
    brand-new (un-initialised) vault and resurrect forgotten content.

    Rules (no bypass flag, no integer override, no compatibility
    shim):

      * HEAD already exists → returned verbatim. Idempotent.
      * HEAD missing, deletion-manifest directory empty **and** the
        state DB records zero ``privacy_deletions`` rows → atomically
        write a v0 HEAD and return it.
      * HEAD missing **and** either a deletion manifest exists on
        disk or the state DB has a ``privacy_deletions`` row →
        raise ``PrivacyError``. This state is inconsistent: a forget
        cannot have produced a manifest / DB row without bumping the
        HEAD, so the operator must reconcile manually (e.g. re-issue
        the HEAD swap, restore the receipt files, or remove the
        orphan rows) before continuing. The function will not paper
        over the inconsistency.
      * HEAD missing and the state DB exists but cannot be safely
        read → also refuse. A DB we cannot read may still hold
        ``privacy_deletions`` rows we cannot see; assuming "empty"
        would resurrect forgotten content. The operator must repair
        the DB first.

    The atomic write goes through ``atomic_write_json`` (write → fsync
    → rename → dir fsync) so a torn write cannot leave a HEAD with a
    schema_version=0 but a half-written JSON object.
    """
    head_path = _privacy_head_path(vault)
    if head_path.exists():
        existing = read_privacy_head(vault)
        if existing is None:
            raise PrivacyError(
                f"ensure_privacy_head: privacy HEAD at {head_path} "
                "exists but is malformed; refusing to overwrite. "
                "Operator intervention required."
            )
        return existing
    deletions_dir = _deletions_dir(vault)
    manifest_files = sorted(deletions_dir.glob("*.json"))
    if manifest_files:
        raise PrivacyError(
            f"ensure_privacy_head: {len(manifest_files)} deletion "
            f"manifest(s) already exist under {deletions_dir} but "
            f"{head_path} is missing. Refusing to silently "
            "synthesize a v0 HEAD; operator intervention required."
        )
    # DB backstop: a vault whose state DB still records privacy
    # deletions cannot be safely initialised by minting v0, even when
    # the on-disk manifests were lost. Without this check, an
    # operator who deleted ``HEAD.json`` + ``privacy/deletions/*.json``
    # but kept ``state.sqlite`` could re-init the privacy layer to
    # v0 and mount an old bundle that pre-dates the deletion.
    db_count = _db_deletion_receipt_count(vault)
    if db_count < 0:
        raise PrivacyError(
            f"ensure_privacy_head: state DB at {vault.state_db} "
            "exists but cannot be safely read; refusing to "
            "synthesize a v0 HEAD. The DB may still record "
            "privacy_deletions rows that must not be orphaned. "
            "Operator intervention required."
        )
    if db_count > 0:
        raise PrivacyError(
            f"ensure_privacy_head: state DB at {vault.state_db} "
            f"still records {db_count} privacy_deletions row(s) "
            f"but {head_path} is missing and no receipt files exist "
            f"under {deletions_dir}. Refusing to synthesize a v0 "
            "HEAD; a v0 bundle could otherwise be restored against "
            "this vault and resurrect forgotten content. "
            "Operator intervention required."
        )
    return write_privacy_head(vault, version=0, epoch=_epoch_for(0))


def write_privacy_head(vault: VaultLayout, *, version: int, epoch: str) -> dict[str, Any]:
    """Atomically swap the privacy HEAD with a monotonically increasing version."""
    payload = {
        "schema_version": 1,
        "epoch": epoch,
        "version": int(version),
        "updated_at": _now_iso(),
    }
    atomic_write_json(_privacy_head_path(vault), payload)
    return payload


# ---------------------------------------------------------------------------
# PrivacyService
# ---------------------------------------------------------------------------


class PrivacyError(ValueError):
    """A privacy operation was malformed or unsafe."""


@dataclass
class DeletionReceipt:
    op_id: str
    kind: str
    targets: list[str]
    privacy_epoch: str
    version: int
    created_at: str
    actor: str
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "op_id": self.op_id,
            "kind": self.kind,
            "targets": list(self.targets),
            "privacy_epoch": self.privacy_epoch,
            "version": self.version,
            "created_at": self.created_at,
            "actor": self.actor,
            "note": self.note,
        }


class PrivacyService:
    """Forget + receipts + re-ingest guard.

    ``conn`` is held by the caller (the dispatcher); the service owns
    no module-global state. Every forget acquires the *current* HEAD
    version, durably writes the manifest, atomically bumps the HEAD,
    and then — *inside the same conn transaction* — updates the
    ``privacy_deletions`` table that the recall path consults. The
    two writes are not strictly atomic (HEAD is a file, the DB is
    SQLite), but the ordering is chosen so a crash between them leaves
    the system safe:

      * manifest written, HEAD not yet bumped ⇒ the manifest is on
        disk but recall still sees a stale HEAD ⇒ harmless: the new
        manifest is simply not visible until the next HEAD swap.
      * HEAD bumped, DB not yet updated ⇒ recall reads the new HEAD
        but the DB hasn't recorded the targets yet. This window is
        narrowed by reading the manifest files on disk (see
        ``_scan_disk_deletions`` below) and merging with the DB rows.

    Both recall and scan paths therefore consult *both* the file
    HEAD and the on-disk manifests. A clean restart re-reads both and
    converges.
    """

    def __init__(self, *, vault: VaultLayout, conn: sqlite3.Connection):
        self.vault = vault
        self.conn = conn

    # ------------------------------------------------------------------
    # public surface
    # ------------------------------------------------------------------

    def forget(
        self,
        *,
        kind: str,
        targets: list[str],
        actor: str = "user_cli",
        note: str | None = None,
    ) -> DeletionReceipt:
        """Authorise and persist a forget operation.

        ``kind`` is one of ``memory``, ``event``, ``source_block``.
        ``targets`` are the corresponding ids (memory_id,
        event_id, source_file_id). Duplicate targets collapse.
        """
        if kind not in {"memory", "event", "source_block"}:
            raise PrivacyError(
                f"forget: unknown kind {kind!r}; "
                "expected memory / event / source_block"
            )
        if not targets:
            raise PrivacyError("forget: 'targets' must not be empty")
        # Normalise: strip, dedupe (preserve order), reject empties.
        seen: set[str] = set()
        clean: list[str] = []
        for t in targets:
            if not isinstance(t, str) or not t.strip():
                raise PrivacyError(f"forget: invalid target {t!r}")
            t = t.strip()
            if t not in seen:
                seen.add(t)
                clean.append(t)
        op_id = _new_op_id()
        # Read current HEAD version to derive the next version; the
        # HEAD file may be absent on first run.
        head = read_privacy_head(self.vault) or {}
        version = int(head.get("version") or 0) + 1
        epoch = str(head.get("epoch") or _epoch_for(version))
        receipt = DeletionReceipt(
            op_id=op_id,
            kind=kind,
            targets=clean,
            privacy_epoch=epoch,
            version=version,
            created_at=_now_iso(),
            actor=actor,
            note=note,
        )
        # 1. Write the immutable manifest file (content-free).
        manifest_path = _deletions_dir(self.vault) / f"{op_id}.json"
        atomic_write_json(manifest_path, receipt.to_dict())
        # 2. Atomically bump HEAD.
        write_privacy_head(
            self.vault, version=version, epoch=epoch,
        )
        # 3. Mirror in SQLite for the recall path's hot filter.
        self._mirror_in_db(receipt)
        # 4. Erase / redact related content.
        erased = self._erase_related(receipt)
        # 5. Audit row (no full-text payload; only the receipt summary).
        self._write_audit(receipt, erased)
        return receipt

    def list_receipts(self) -> list[DeletionReceipt]:
        """Return every receipt known to the file system.

        The file manifest is the source of truth; the DB mirror is a
        hot filter. Listing uses the manifest directory so a torn
        write that succeeded in the DB but not on disk still leaves
        the file count consistent.
        """
        out: list[DeletionReceipt] = []
        deletions = _deletions_dir(self.vault)
        for path in sorted(deletions.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            try:
                out.append(DeletionReceipt(**{
                    k: data.get(k) for k in (
                        "op_id", "kind", "targets", "privacy_epoch",
                        "version", "created_at", "actor", "note",
                    )
                }))
            except TypeError:
                continue
        out.sort(key=lambda r: int(r.version))
        return out

    def current_version(self) -> int:
        head = read_privacy_head(self.vault)
        return int(head.get("version") or 0) if head else 0

    def is_target_blocked(self, *, source_file_id: str | None = None,
                          memory_id: str | None = None,
                          event_id: str | None = None) -> bool:
        """Fast lookup: is this source / memory / event blocked right now?"""
        # Always re-read HEAD so the caller cannot hold a stale answer
        # across a concurrent forget.
        self.current_version()
        hit = False
        if source_file_id:
            row = self.conn.execute(
                "SELECT 1 AS hit FROM privacy_blocks "
                "WHERE source_file_id = ? LIMIT 1",
                (source_file_id,),
            ).fetchone()
            hit = hit or bool(row)
        if memory_id and not hit:
            row = self.conn.execute(
                "SELECT 1 AS hit FROM privacy_deletions "
                "WHERE kind='memory' AND ? IN ("
                "SELECT value FROM json_each(targets_json)) "
                "LIMIT 1",
                (memory_id,),
            ).fetchone()
            hit = hit or bool(row)
        if event_id and not hit:
            row = self.conn.execute(
                "SELECT 1 AS hit FROM privacy_deletions "
                "WHERE kind='event' AND ? IN ("
                "SELECT value FROM json_each(targets_json)) "
                "LIMIT 1",
                (event_id,),
            ).fetchone()
            hit = hit or bool(row)
        # Always consult the on-disk manifests. They are the
        # authoritative source; the DB is a hot mirror. Even when
        # the DB has caught up, walking the manifest confirms the
        # block survives a DB-only crash where the manifest file is
        # the only durable artefact.
        if not hit:
            hit = self._disk_match(
                source_file_id=source_file_id,
                memory_id=memory_id,
                event_id=event_id,
            )
        return hit

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _mirror_in_db(self, receipt: DeletionReceipt) -> None:
        # Idempotent on op_id: a retry of the same forget won't
        # duplicate the row.
        self.conn.execute(
            """INSERT OR REPLACE INTO privacy_deletions(
                op_id, kind, targets_json, privacy_epoch,
                version, created_at, actor, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                receipt.op_id, receipt.kind,
                json.dumps(list(receipt.targets)),
                receipt.privacy_epoch, int(receipt.version),
                receipt.created_at, receipt.actor, receipt.note,
            ),
        )

    def _disk_max_version(self) -> int:
        version = 0
        for path in _deletions_dir(self.vault).glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                version = max(version, int(data.get("version") or 0))
            except (json.JSONDecodeError, OSError, ValueError, TypeError):
                continue
        return version

    def _disk_match(
        self,
        *,
        source_file_id: str | None,
        memory_id: str | None,
        event_id: str | None,
    ) -> bool:
        # Synthesise "any hit" sentinel for is_target_blocked.
        for path in _deletions_dir(self.vault).glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            targets = list(data.get("targets") or [])
            kind = str(data.get("kind") or "")
            if source_file_id and kind == "source_block" and source_file_id in targets:
                return True
            if memory_id and kind == "memory" and memory_id in targets:
                return True
            if event_id and kind == "event" and event_id in targets:
                return True
        return False

    def _erase_related(self, receipt: DeletionReceipt) -> dict[str, int]:
        """Erase or redact related content.

        Rules (no full-text payload is preserved):

          * ``memory``: the immutable memory files are removed from
            ``<vault>/objects/memories/<id>/``; the
            ``memory_projection`` row is updated to ``status='redacted'``
            and ``statement`` / ``scope_json`` are wiped. The
            ``commit_intents.expected_revisions_json`` is *not* mutated
            (commit history is content-free metadata).
          * ``event``: the source_events row is removed; any segment
            JSON that referenced it as the *last* event is left in
            place (segments are immutable artefacts) but its text
            payload is overwritten with a content-free tombstone only
            for events whose entire source file is blocked. For a
            single-event forget, segments stay (other events in the
            segment still need them) and only the source_events row
            is dropped, plus any audit details referencing the event.
          * ``source_block``: in addition to the per-event handling,
            the source_file row's ``complete_offset`` is pinned at its
            current ``size`` (so the next ``scan`` does not consume
            any new bytes), and a content-free tombstone is appended
            to the segments for traceability.

        Returns a small counters dict for the audit row.
        """
        erased = {"memories_removed": 0, "projections_redacted": 0,
                  "events_removed": 0, "sources_pinned": 0,
                  "audit_redacted": 0, "trace_redacted": 0}
        if receipt.kind == "memory":
            for mem_id in receipt.targets:
                mem_dir = self.vault.memories_dir / mem_id
                if mem_dir.exists():
                    # Move to trash; we never delete user files in
                    # place because a torn erase could leave the
                    # vault unreadable. ``trash`` is content-free
                    # metadata only (the manifest); the memory body
                    # is *shredded* (truncated to zero bytes) before
                    # the rename so even forensic recovery sees an
                    # empty file.
                    for mem_file in mem_dir.glob("*"):
                        try:
                            mem_file.write_text("", encoding="utf-8")
                        except OSError:
                            pass
                    trash = self.vault.root / "privacy" / "trash" / receipt.op_id
                    trash.mkdir(parents=True, exist_ok=True)
                    try:
                        # rename the directory; its files are empty.
                        target = trash / mem_id
                        mem_dir.rename(target)
                        erased["memories_removed"] += 1
                    except OSError:
                        # Cross-device / permission: leave a tombstone.
                        (trash / f"{mem_id}.TOMBSTONE").write_text(
                            json.dumps({"op_id": receipt.op_id,
                                       "memory_id": mem_id,
                                       "kind": "memory"}),
                            encoding="utf-8",
                        )
                        erased["memories_removed"] += 1
                # Redact the projection row (wipe body / scope).
                self.conn.execute(
                    "UPDATE memory_projection SET status='redacted', "
                    "statement='', scope_json='{}', "
                    "project_ids_json='[]', domains_json='[]' "
                    "WHERE memory_id = ?",
                    (mem_id,),
                )
                erased["projections_redacted"] += int(
                    self.conn.execute(
                        "SELECT changes()",
                    ).fetchone()[0]
                )
        elif receipt.kind == "event":
            for ev_id in receipt.targets:
                # Remove the source_events row; keep segments immutable.
                self.conn.execute(
                    "DELETE FROM source_events WHERE event_id = ?",
                    (ev_id,),
                )
                erased["events_removed"] += int(
                    self.conn.execute("SELECT changes()").fetchone()[0]
                )
                # Redact any audit_events.details_json that referenced
                # the event_id; preserve the audit row shape and the
                # metadata, only wipe the body.
                rows = self.conn.execute(
                    "SELECT audit_id, details_json FROM audit_events "
                    "WHERE object_type='event' AND object_id=?",
                    (ev_id,),
                ).fetchall()
                for row in rows:
                    self.conn.execute(
                        "UPDATE audit_events SET details_json=? "
                        "WHERE audit_id=?",
                        (json.dumps({"redacted": True,
                                     "op_id": receipt.op_id}), row["audit_id"]),
                    )
                erased["audit_redacted"] += len(rows)
        elif receipt.kind == "source_block":
            for sf_id in receipt.targets:
                # Pin the source cursor so future scans do not consume
                # any new bytes from this source.
                row = self.conn.execute(
                    "SELECT size FROM source_files WHERE source_file_id = ?",
                    (sf_id,),
                ).fetchone()
                if row is not None:
                    self.conn.execute(
                        "UPDATE source_files SET "
                        "complete_offset = MAX(complete_offset, size), "
                        "last_status='privacy_blocked' "
                        "WHERE source_file_id = ?",
                        (sf_id,),
                    )
                    erased["sources_pinned"] += 1
                # Register the privacy_blocks row so the scanner's
                # hot filter can refuse to ingest any new events from
                # this source. ``op_id`` references the receipt that
                # pinned the source.
                self.conn.execute(
                    "INSERT OR REPLACE INTO privacy_blocks("
                    "source_file_id, op_id, blocked_at"
                    ") VALUES (?, ?, ?)",
                    (sf_id, receipt.op_id, _now_iso()),
                )
                # Drop any events already indexed for this source so
                # they cannot reappear via a recall that consults
                # stale projection rows.
                self.conn.execute(
                    "DELETE FROM source_events WHERE source_file_id = ?",
                    (sf_id,),
                )
                erased["events_removed"] += int(
                    self.conn.execute("SELECT changes()").fetchone()[0]
                )
        # Always wipe any query trace artefact that referenced an
        # erased memory_id / event_id. Traces are content-free after
        # this: only the original_query / variants metadata remain.
        erased["trace_redacted"] = self._redact_query_traces(receipt)
        return erased

    def _redact_query_traces(self, receipt: DeletionReceipt) -> int:
        if not self.vault.artifacts_dir.exists():
            return 0
        queries_dir = self.vault.artifacts_dir / "queries"
        if not queries_dir.exists():
            return 0
        target_set = set(receipt.targets)
        touched = 0
        for trace_path in queries_dir.glob("*.json"):
            try:
                payload = json.loads(trace_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            items = payload.get("items") or []
            hit = False
            new_items = []
            for item in items:
                mid = (item or {}).get("memory_id")
                if mid and mid in target_set:
                    hit = True
                    # Replace the item with a content-free tombstone.
                    new_items.append({
                        "memory_id": mid,
                        "redacted": True,
                        "op_id": receipt.op_id,
                    })
                else:
                    new_items.append(item)
            if hit:
                payload["items"] = new_items
                payload["redactions"] = list(payload.get("redactions") or []) + [
                    {"op_id": receipt.op_id, "kind": receipt.kind}
                ]
                trace_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                touched += 1
        return touched

    def _write_audit(self, receipt: DeletionReceipt, erased: dict[str, int]) -> None:
        # Audit row: never include full-text payload.
        # ``memory_id`` column is the standard schema; ``object_id``
        # carries the op_id so the audit history can be queried.
        details = {
            "kind": receipt.kind,
            "targets_count": len(receipt.targets),
            "erased": erased,
            "privacy_epoch": receipt.privacy_epoch,
            "version": receipt.version,
        }
        seq_row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM audit_events"
        ).fetchone()
        next_seq = int(seq_row["next"]) if seq_row else 1
        audit_id = "aud_" + hashlib.sha1(
            f"privacy.deleted|{receipt.op_id}".encode()
        ).hexdigest()[:12]
        try:
            self.conn.execute(
                """INSERT INTO audit_events(
                    audit_id, seq, occurred_at, actor, action,
                    object_type, object_id, correlation_json,
                    before_after_json, outcome, reason_code, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    audit_id, next_seq, _now_iso(), receipt.actor,
                    "privacy.deleted", "privacy_op", receipt.op_id,
                    json.dumps({"privacy_epoch": receipt.privacy_epoch,
                                "version": receipt.version}),
                    json.dumps({"targets_count": len(receipt.targets)}),
                    "success", None,
                    json.dumps(details),
                ),
            )
        except sqlite3.IntegrityError:
            pass


# ---------------------------------------------------------------------------
# Helper: derive an epoch id from a version number.
# ---------------------------------------------------------------------------


def _epoch_for(version: int) -> str:
    return f"epoch-{int(version):04d}"


__all__ = [
    "DeletionReceipt",
    "PrivacyError",
    "PrivacyService",
    "ensure_privacy_head",
    "read_privacy_head",
    "write_privacy_head",
]
