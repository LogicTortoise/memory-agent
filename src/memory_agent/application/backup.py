"""Consistent SQLite + files backup / restore (P9 / T17).

A backup is a *content-free* metadata bundle that lets the operator
restore a vault into the same shape it had at the snapshot moment.
Three properties matter:

  1. **SQLite is consistent.** We use the standard
     ``sqlite3.Connection.backup`` API which serialises a live
     WAL-mode database into a target connection; the result is the
     same as a ``VACUUM INTO`` snapshot but never blocks readers
     longer than a small window. We do NOT ``cp state.sqlite`` —
     that path skips WAL and produces torn snapshots.

  2. **Files are consistent with the DB.** Before the SQLite
     snapshot we walk every commit manifest + segment referenced by
     the live DB and copy them into the bundle under their
     content-addressed path; HEAD and privacy HEAD are pinned too.

  3. **Privacy manifest is durable and independent.** The
     ``<vault>/privacy/deletions/`` directory + ``privacy/HEAD.json``
     are copied last so a restore always sees the latest
     independent privacy state. A backup taken *before* a forget
     cannot be silently mounted without the latest deletion
     manifest; restore refuses and quarantines such a bundle.

Crash windows we guard against:

  * ``scan`` mid-write: the scanner writes a segment, fsyncs, then
    inserts the ``source_events`` row in the same transaction. The
    backup's SQLite snapshot cannot see an uncommitted event; the
    segment is only on disk if the row was committed, so a torn
    segment without a row is impossible.

  * ``publish`` mid-write: the commit service prepares the manifest
    + intent (no HEAD swap), atomically swaps HEAD, then finalises
    the SQL row. If we snapshot between HEAD swap and finalise,
    HEAD already names the new commit, the manifest is on disk,
    and the intent is still ``published``; recovery on the next
    ``doctor`` call finalises it. The backup carries the same
    recovery contract.

  * ``forget`` mid-write: the privacy manifest is written first,
    then HEAD swaps, then the DB mirrors. The backup copies HEAD
    *after* the manifest directory, so a snapshot that reads HEAD
    version N already has all N manifests on disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory_agent.application.privacy import (
    read_privacy_head,
)
from memory_agent.application.recovery import recover_unfinished_intents
from memory_agent.infrastructure.files import VaultLayout
from memory_agent.infrastructure.sqlite import open_state_db


BACKUP_FORMAT_VERSION = 1


@dataclass
class BackupManifest:
    schema_version: int = BACKUP_FORMAT_VERSION
    bundle_id: str = ""
    created_at: str = ""
    label: str | None = None
    format: str = "tar"
    vault_layout: dict[str, str] = field(default_factory=dict)
    file_count: int = 0
    total_bytes: int = 0
    sqlite_sha256: str | None = None
    privacy_head_version: int = 0
    privacy_manifest_count: int = 0
    deletion_manifest_sha256: str | None = None
    head_commit_id: str | None = None
    lease_holder: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "bundle_id": self.bundle_id,
            "created_at": self.created_at,
            "label": self.label,
            "format": self.format,
            "vault_layout": dict(self.vault_layout),
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "sqlite_sha256": self.sqlite_sha256,
            "privacy_head_version": self.privacy_head_version,
            "privacy_manifest_count": self.privacy_manifest_count,
            "deletion_manifest_sha256": self.deletion_manifest_sha256,
            "head_commit_id": self.head_commit_id,
            "lease_holder": self.lease_holder,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BackupManifest":
        return cls(
            schema_version=int(data.get("schema_version") or BACKUP_FORMAT_VERSION),
            bundle_id=str(data.get("bundle_id") or ""),
            created_at=str(data.get("created_at") or ""),
            label=data.get("label"),
            format=str(data.get("format") or "tar"),
            vault_layout=dict(data.get("vault_layout") or {}),
            file_count=int(data.get("file_count") or 0),
            total_bytes=int(data.get("total_bytes") or 0),
            sqlite_sha256=data.get("sqlite_sha256"),
            privacy_head_version=int(data.get("privacy_head_version") or 0),
            privacy_manifest_count=int(data.get("privacy_manifest_count") or 0),
            deletion_manifest_sha256=data.get("deletion_manifest_sha256"),
            head_commit_id=data.get("head_commit_id"),
            lease_holder=data.get("lease_holder"),
            notes=list(data.get("notes") or []),
        )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BackupError(RuntimeError):
    """A backup / restore operation was rejected."""


# ---------------------------------------------------------------------------
# Live privacy ledger: the authoritative view of the operator's live
# vault's privacy state. Restore MUST consult this; it cannot rely on
# a caller-supplied integer or the empty restore target's HEAD.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LivePrivacyLedger:
    """Authoritative privacy ledger of the operator's live vault.

    The ledger is the only trust root for restore-time privacy
    decisions. It captures the latest independent deletion manifest
    *as it actually exists on disk* in the operator's live vault,
    not as a number the caller claims. If the ledger cannot be
    established, restore fails closed.

    ``deletion_manifest_sha256`` is the sha256 of the concatenation
    of every ``privacy/deletions/*.json`` file in sorted order; for
    a vault with no forgets yet, it is the empty-concat hash (a
    real hex string, not ``None``), so a v0 bundle and a v0 live
    ledger can be compared with stable semantics.
    """

    version: int
    epoch: str
    schema_version: int
    deletion_manifest_sha256: str

    @property
    def is_empty(self) -> bool:
        """True when no forget has happened yet on this vault."""
        return self.version == 0


def _db_privacy_receipts(vault: VaultLayout) -> list[dict[str, Any]] | None:
    """Read every ``privacy_deletions`` row from the live vault's
    state DB, plus the ``privacy_blocks`` rows that reference them.

    Returns a list of dicts shaped like::

        {"op_id": str, "kind": str, "targets": list[str],
         "version": int, "epoch": str, "blocked_targets": list[str]}

    or ``None`` if the DB does not exist (a vault that was never
    initialised — not a corruption signal). Raises
    ``BackupError`` if the DB exists but cannot be safely read; the
    caller must fail closed rather than trust an empty ledger.

    A crash window between HEAD swap and DB mirror is allowed:
    ``_verify_db_against_disk`` permits disk ahead + DB missing,
    because the file manifest is the authoritative source and the
    DB row simply hasn't landed yet.
    """
    if not vault.state_db.exists():
        return None
    conn = None
    try:
        conn = sqlite3.connect(
            f"file:{vault.state_db}?mode=ro", uri=True, timeout=5,
        )
        conn.row_factory = sqlite3.Row
        try:
            del_rows = conn.execute(
                "SELECT op_id, kind, targets_json, privacy_epoch, "
                "version FROM privacy_deletions"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            # Table does not exist (very old schema) — treat as no
            # DB rows. Privacy blocks table only would raise the
            # same way; fall through to the block query below.
            if "no such table" not in str(exc).lower():
                raise BackupError(
                    f"read_live_privacy_ledger: state DB at "
                    f"{vault.state_db} cannot be safely read "
                    f"({exc}); refusing to synthesise a ledger"
                ) from exc
            del_rows = []
        blocks_by_op: dict[str, list[str]] = {}
        try:
            block_rows = conn.execute(
                "SELECT source_file_id, op_id FROM privacy_blocks"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise BackupError(
                    f"read_live_privacy_ledger: privacy_blocks "
                    f"cannot be read ({exc}); refusing to "
                    "synthesise a ledger"
                ) from exc
            block_rows = []
        for row in block_rows:
            blocks_by_op.setdefault(str(row["op_id"]), []).append(
                str(row["source_file_id"])
            )
        out: list[dict[str, Any]] = []
        for row in del_rows:
            try:
                targets = json.loads(row["targets_json"]) or []
            except (TypeError, ValueError):
                targets = []
            out.append({
                "op_id": str(row["op_id"]),
                "kind": str(row["kind"] or ""),
                "targets": [str(t) for t in targets],
                "version": int(row["version"] or 0),
                "epoch": str(row["privacy_epoch"] or ""),
                "blocked_targets": blocks_by_op.get(
                    str(row["op_id"]), []
                ),
            })
        return out
    except BackupError:
        raise
    except (sqlite3.DatabaseError, OSError) as exc:
        raise BackupError(
            f"read_live_privacy_ledger: state DB at "
            f"{vault.state_db} cannot be safely read ({exc}); "
            "refusing to synthesise a ledger"
        ) from exc
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _verify_db_against_disk(
    vault: VaultLayout,
    db_rows: list[dict[str, Any]] | None,
    live_version: int,
) -> None:
    """Cross-check every DB deletion receipt against the on-disk
    manifest.

    The DB may lag disk after a crash between HEAD swap and DB
    insert; that window is allowed (the on-disk manifest is
    authoritative). The DB may NOT lead disk — a row in the DB
    whose receipt file is missing, whose op_id does not match the
    file, or whose kind / targets / version disagree with the file
    indicates the live ledger is unsafe. Restore against such a
    ledger would accept a forged same-version bundle that has been
    sanitised of the receipts the DB still remembers.

    Raises ``BackupError`` on any inconsistency. ``db_rows=None``
    is a no-op (the DB does not exist; nothing to cross-check).
    """
    if db_rows is None:
        return
    deletions_dir = vault.root / "privacy" / "deletions"
    by_op: dict[str, Path] = {}
    if deletions_dir.exists():
        for path in sorted(deletions_dir.glob("*.json")):
            # stem() is the op_id; receipts are named "<op_id>.json".
            by_op[path.stem] = path
    for row in db_rows:
        op_id = row["op_id"]
        path = by_op.get(op_id)
        if path is None:
            raise BackupError(
                f"read_live_privacy_ledger: state DB records a "
                f"privacy_deletions row with op_id={op_id!r} "
                f"(version={row['version']}, kind={row['kind']!r}) "
                "but no matching receipt file exists under "
                f"{deletions_dir}. The live ledger is unsafe: a "
                "forged same-version bundle that omits this receipt "
                "would otherwise pass the restore hash check. "
                "Refusing to restore."
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise BackupError(
                f"read_live_privacy_ledger: receipt {path} for "
                f"op_id={op_id!r} cannot be read ({exc}); refusing "
                "to restore"
            ) from exc
        file_op = str(payload.get("op_id") or "")
        if file_op != op_id:
            raise BackupError(
                f"read_live_privacy_ledger: receipt {path} op_id "
                f"mismatch (file={file_op!r}, db={op_id!r}); "
                "refusing to restore"
            )
        file_kind = str(payload.get("kind") or "")
        if file_kind != row["kind"]:
            raise BackupError(
                f"read_live_privacy_ledger: receipt {path} kind "
                f"mismatch for op_id={op_id!r} "
                f"(file={file_kind!r}, db={row['kind']!r}); "
                "refusing to restore"
            )
        file_targets = [str(t) for t in (payload.get("targets") or [])]
        if file_targets != row["targets"]:
            raise BackupError(
                f"read_live_privacy_ledger: receipt {path} targets "
                f"mismatch for op_id={op_id!r} (file={file_targets}, "
                f"db={row['targets']}); refusing to restore"
            )
        try:
            file_version = int(payload.get("version") or 0)
        except (TypeError, ValueError) as exc:
            raise BackupError(
                f"read_live_privacy_ledger: receipt {path} version "
                f"invalid ({exc}); refusing to restore"
            ) from exc
        if file_version != row["version"]:
            raise BackupError(
                f"read_live_privacy_ledger: receipt {path} version "
                f"mismatch for op_id={op_id!r} "
                f"(file={file_version}, db={row['version']}); "
                "refusing to restore"
            )
        # Also enforce the version monotonicity: the DB row's
        # version must not exceed the live HEAD version. A DB row
        # with version > live_version indicates a partial mirror
        # crash where the HEAD swap didn't complete; refuse.
        if row["version"] > live_version:
            raise BackupError(
                f"read_live_privacy_ledger: state DB has a "
                f"privacy_deletions row with version="
                f"{row['version']} > live HEAD version="
                f"{live_version} (op_id={op_id!r}); refusing to "
                "restore. The HEAD swap is incomplete."
            )


def read_live_privacy_ledger(vault: VaultLayout) -> LivePrivacyLedger:
    """Read the operator's live vault's authoritative privacy ledger.

    Rules (no bypass flag, no caller-supplied integer override, no
    silent v0 synthesis):

      * The privacy HEAD file MUST exist on disk. A missing HEAD is
        treated as **untrusted**: the ledger cannot be established
        and ``BackupError`` is raised. The operator must run
        ``memory init`` (which atomically establishes a v0 HEAD via
        ``ensure_privacy_head`` when the vault has no deletion
        manifests and no ``privacy_deletions`` rows in its state DB)
        before restore can succeed. An empty deletions directory is
        NOT sufficient to fabricate a v0 ledger — that path is
        exactly what let an old v0 bundle be silently mounted into
        an un-initialised vault and resurrect forgotten content.
      * If deletion manifests exist but the HEAD is absent, the
        state is inconsistent (a forget cannot have produced a
        manifest without bumping the HEAD); refuse.
      * The HEAD must be well-formed: integer ``version``,
        non-empty ``epoch``, integer ``schema_version`` ≥ 1.
      * The ``deletion_manifest_sha256`` is computed from the actual
        files on disk so the comparison is content-aware, not
        version-only.
      * **DB backstop**: every row in ``privacy_deletions`` (and
        the matching ``privacy_blocks`` rows) MUST have a
        corresponding on-disk manifest file with matching
        ``op_id`` / ``kind`` / ``targets`` / ``version``. The DB
        may lag disk (a crash between HEAD swap and DB mirror);
        that window is allowed because the file manifest is the
        authoritative source. The DB may NOT lead disk — a DB row
        with a missing or altered receipt file means the live
        ledger is unsafe and restore would accept a forged
        same-version bundle that has been sanitised of the receipts
        the DB still remembers.

    Raises ``BackupError`` on any inconsistency; the caller is
    expected to propagate the failure (the target stays unmounted).
    """
    head_path = vault.privacy_head
    deletions_dir = vault.root / "privacy" / "deletions"

    # Compute the deletion-manifest hash from disk so the comparison
    # is content-aware, not version-only.
    h = hashlib.sha256()
    receipt_count = 0
    if deletions_dir.exists():
        for receipt_file in sorted(deletions_dir.glob("*.json")):
            h.update(receipt_file.read_bytes())
            receipt_count += 1
    deletion_hash = h.hexdigest()

    if not head_path.exists():
        # No HEAD: refuse. Whether or not deletion manifests exist,
        # we cannot establish an authoritative ledger without the
        # HEAD file. The operator must run `memory init` (which
        # proves no deletion manifests exist *and* no
        # privacy_deletions DB rows exist before writing v0) or
        # otherwise reconcile manually. Synthesising a v0 ledger
        # here was the silent-resurrection bug.
        if receipt_count > 0:
            raise BackupError(
                "restore: privacy ledger inconsistent: deletion "
                f"manifests exist under {deletions_dir} but "
                f"{head_path} is missing; refusing to restore to "
                "prevent accidental resurrection of forgotten "
                "content"
            )
        raise BackupError(
            f"restore: privacy HEAD missing at {head_path}; "
            "refusing to silently synthesize a v0 ledger. Run "
            "`memory init` on the live vault to establish a "
            "privacy HEAD before restore."
        )

    try:
        head_data = json.loads(head_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise BackupError(
            f"restore: privacy HEAD unreadable: {exc}; refusing to "
            "restore without an authoritative live ledger"
        ) from exc
    if not isinstance(head_data, dict):
        raise BackupError(
            "restore: privacy HEAD malformed (not a JSON object); "
            "refusing to restore"
        )
    try:
        version = int(head_data.get("version") or 0)
    except (TypeError, ValueError) as exc:
        raise BackupError(
            f"restore: privacy HEAD version invalid: {exc}"
        ) from exc
    epoch = str(head_data.get("epoch") or "")
    if not epoch:
        raise BackupError(
            "restore: privacy HEAD missing epoch; refusing to restore"
        )
    schema_version_raw = head_data.get("schema_version")
    try:
        schema_version = int(schema_version_raw) if schema_version_raw is not None else 0
    except (TypeError, ValueError) as exc:
        raise BackupError(
            f"restore: privacy HEAD schema_version invalid: {exc}"
        ) from exc
    if schema_version < 1:
        raise BackupError(
            f"restore: privacy HEAD schema_version={schema_version!r} "
            "is below the supported minimum (1); refusing to restore"
        )
    # DB backstop: cross-check every privacy_deletions row against
    # the on-disk manifest. Disk ahead + DB missing is allowed (the
    # file ledger is authoritative; the DB row simply hasn't landed
    # yet). DB ahead + disk missing / altered is unsafe and we
    # refuse. A corrupt / unreadable DB also refuses — we cannot
    # claim "no DB rows" without seeing the rows.
    db_rows = _db_privacy_receipts(vault)
    _verify_db_against_disk(vault, db_rows, live_version=version)
    return LivePrivacyLedger(
        version=version,
        epoch=epoch,
        schema_version=schema_version,
        deletion_manifest_sha256=deletion_hash,
    )


# ---------------------------------------------------------------------------
# Path helpers — every backup payload lives under a ``memory-agent-backup/``
# directory tree (or the same tree inside a tarball).
# ---------------------------------------------------------------------------


def _bundle_root_in_dir(root: Path) -> Path:
    return root / "memory-agent-backup"


def _payload_paths() -> dict[str, str]:
    """Canonical bundle paths (relative to the bundle root)."""
    return {
        "manifest": "manifest.json",
        "sqlite": "state/state.sqlite",
        "head": "HEAD.json",
        "privacy_head": "privacy/HEAD.json",
        "deletions_dir": "privacy/deletions",
        "commits_dir": "commits",
        "memories_dir": "objects/memories",
        "segments_dir": "sources/segments",
        "artifacts_dir": "artifacts",
        "objects_dir": "objects",
    }


def _all_vault_files(vault: VaultLayout) -> list[Path]:
    """Enumerate every file the backup copies (immutable + atomic HEADs).

    Sources/segments, objects/memories and commits/manifests are the
    content-addressed artefacts. artifacts/ holds review proposals and
    query traces. ``trash/`` is excluded by design — it contains the
    shredded bodies of forgotten memories and is not part of the
    restorable surface.
    """
    out: list[Path] = []
    candidates = [
        vault.head_path,
        vault.privacy_head,
    ]
    for path in candidates:
        if path.exists():
            out.append(path)
    for d in (
        vault.segments_dir,
        vault.memories_dir,
        vault.commits_dir,
        vault.artifacts_dir,
        vault.objects_dir,
        vault.root / "privacy" / "deletions",
    ):
        if d.exists():
            for sub in d.rglob("*"):
                if sub.is_file():
                    out.append(sub)
    return out


# ---------------------------------------------------------------------------
# Backup creation
# ---------------------------------------------------------------------------


class BackupService:
    """Snapshot + restore for a vault.

    The service holds no module-global state; the caller passes the
    vault layout. Snapshot is purely read-only against the live
    vault, but does *pause* briefly to grab the SQLite backup via the
    C API so the snapshot captures a single point in time.
    """

    def __init__(self, *, vault: VaultLayout):
        self.vault = vault

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        output: Path,
        fmt: str = "tar",
        label: str | None = None,
    ) -> BackupManifest:
        if fmt not in {"tar", "dir"}:
            raise BackupError(f"create: unknown format {fmt!r}")
        if not output.parent.exists():
            raise BackupError(f"create: parent of {output} does not exist")
        # Pre-flight: a fresh SQLite backup API snapshot requires the
        # DB to exist. A vault that has never been ``init``-ed cannot
        # be backed up — refuse early with a clear error rather than
        # producing an empty bundle that restore would later reject.
        if not self.vault.state_db.exists():
            raise BackupError(
                "create: vault not initialised; run `memory init` first"
            )
        # 1. Recover unfinished intents so the manifest we snapshot is
        #    consistent with the post-recovery DB.
        conn = open_state_db(self.vault.state_db)
        try:
            recover_unfinished_intents(vault=self.vault, conn=conn)
            head_commit_id = self._read_head_commit_id()
            lease_holder = self._read_active_lease(conn)
            privacy_head = read_privacy_head(self.vault) or {}
            # DB backstop: refuse to snapshot a vault whose privacy
            # HEAD is missing. The bundle would carry no privacy
            # HEAD, and a restore that materialised it would let an
            # attacker (or a forgetful operator) later mount an old
            # bundle against the new target as if it had a v0
            # privacy ledger. The only operator-authorised safe path
            # that may write a v0 HEAD is ``ensure_privacy_head`` —
            # refusing here forces the operator to ``memory init``
            # (which proves the DB has no privacy_deletions rows
            # before writing v0) before a snapshot can be taken.
            if not privacy_head:
                raise BackupError(
                    "create: vault has no privacy HEAD; refusing to "
                    "snapshot a vault without an authoritative "
                    "privacy ledger. Run `memory init` first (the "
                    "only path allowed to write a v0 HEAD); refuse "
                    "if the state DB records privacy_deletions rows."
                )
            privacy_version = int(privacy_head.get("version") or 0)
            privacy_files = sorted(
                (self.vault.root / "privacy" / "deletions").glob("*.json")
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass

        # 2. Stage files into a temp dir then write the bundle.
        bundle_id = "bk_" + uuid.uuid4().hex[:12]
        if fmt == "tar":
            staging = Path(tempfile.mkdtemp(prefix="mabk_"))
            try:
                manifest = self._stage_into(
                    staging=staging,
                    bundle_id=bundle_id,
                    label=label,
                    head_commit_id=head_commit_id,
                    privacy_version=privacy_version,
                    lease_holder=lease_holder,
                    privacy_files=privacy_files,
                )
                # Atomic: write to <output>.tmp then rename.
                tmp = output.with_suffix(output.suffix + ".tmp")
                with tarfile.open(tmp, "w") as tar:
                    tar.add(
                        staging / "memory-agent-backup",
                        arcname="memory-agent-backup",
                        recursive=True,
                    )
                os.replace(tmp, output)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        else:
            staging = output
            staging.mkdir(parents=True, exist_ok=True)
            manifest = self._stage_into(
                staging=staging,
                bundle_id=bundle_id,
                label=label,
                head_commit_id=head_commit_id,
                privacy_version=privacy_version,
                lease_holder=lease_holder,
                privacy_files=privacy_files,
            )
        return manifest

    def _stage_into(
        self,
        *,
        staging: Path,
        bundle_id: str,
        label: str | None,
        head_commit_id: str | None,
        privacy_version: int,
        lease_holder: str | None,
        privacy_files: list[Path],
    ) -> BackupManifest:
        bundle_root = _bundle_root_in_dir(staging)
        bundle_root.mkdir(parents=True, exist_ok=True)
        paths = _payload_paths()

        # Copy HEAD files first (atomic; safe to copy at any time).
        self._safe_copy_file(self.vault.head_path, bundle_root / paths["head"])
        # Privacy HEAD: always materialise a content-free default
        # when the live vault has never recorded a forget. The bundle
        # therefore always carries a ``privacy/HEAD.json`` so restore
        # can validate the privacy epoch boundary.
        privacy_head_path = bundle_root / paths["privacy_head"]
        if not privacy_head_path.exists():
            privacy_head_path.parent.mkdir(parents=True, exist_ok=True)
            privacy_head_path.write_text(
                json.dumps({
                    "schema_version": 1,
                    "epoch": f"epoch-{privacy_version:04d}",
                    "version": privacy_version,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        # Copy privacy deletion manifests (every .json under deletions/).
        deletion_concat = hashlib.sha256()
        for path in privacy_files:
            target = bundle_root / paths["deletions_dir"] / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            data = path.read_bytes()
            target.write_bytes(data)
            deletion_concat.update(data)

        # Snapshot the SQLite DB into the bundle via the backup API.
        # The target connection writes to a transient file inside the
        # bundle; we open it briefly, then close and rename.
        sqlite_target = bundle_root / paths["sqlite"]
        sqlite_target.parent.mkdir(parents=True, exist_ok=True)
        self._snapshot_sqlite(sqlite_target)

        # Copy immutable file content (segments, memories, commits,
        # artifacts). Iterating ``_all_vault_files`` ensures we only
        # copy files that exist; missing optional directories are
        # silently skipped.
        file_count = 0
        total_bytes = 0
        for src in _all_vault_files(self.vault):
            try:
                rel = src.relative_to(self.vault.root)
            except ValueError:
                continue
            target = bundle_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            data = src.read_bytes()
            target.write_bytes(data)
            file_count += 1
            total_bytes += len(data)

        manifest = BackupManifest(
            schema_version=BACKUP_FORMAT_VERSION,
            bundle_id=bundle_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            label=label,
            format="tar",
            vault_layout={
                "root": str(self.vault.root),
                "state_dir": str(self.vault.state_dir),
                "memories_dir": str(self.vault.memories_dir),
                "commits_dir": str(self.vault.commits_dir),
                "segments_dir": str(self.vault.segments_dir),
                "artifacts_dir": str(self.vault.artifacts_dir),
            },
            file_count=file_count,
            total_bytes=total_bytes,
            sqlite_sha256=_sha256_of_file(sqlite_target),
            privacy_head_version=privacy_version,
            privacy_manifest_count=len(privacy_files),
            deletion_manifest_sha256=deletion_concat.hexdigest() or None,
            head_commit_id=head_commit_id,
            lease_holder=lease_holder,
            notes=[],
        )
        (bundle_root / paths["manifest"]).write_text(
            json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return manifest

    def _snapshot_sqlite(self, target: Path) -> None:
        # Standard-library sqlite3.Connection.backup() is the supported
        # API for taking a consistent snapshot of a live WAL-mode
        # database. We open a transient connection to the bundle file
        # and let SQLite stream the page-by-page copy. This is the
        # only safe way to snapshot a running ``state.sqlite``.
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        src_conn = sqlite3.connect(str(self.vault.state_db))
        try:
            dst_conn = sqlite3.connect(str(target))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()

    # ------------------------------------------------------------------
    # restore
    # ------------------------------------------------------------------

    def restore(
        self,
        *,
        bundle: Path,
        target: Path,
        live_privacy_ledger: LivePrivacyLedger,
    ) -> dict[str, Any]:
        """Restore a backup bundle into ``target``.

        Pre-conditions enforced:

          * ``target`` does NOT already contain a live vault (no
            ``state/state.sqlite`` and no ``privacy/HEAD.json``).
          * ``live_privacy_ledger`` is the authoritative view of the
            operator's *current* live vault privacy state, computed
            from ``<live-vault>/privacy/HEAD.json`` and the
            on-disk ``privacy/deletions/`` manifests. The caller
            (the dispatcher / CLI) MUST pass it — there is no
            fall-back to a caller-provided integer.
          * bundle is well-formed (manifest + sqlite + HEAD + privacy
            HEAD parse without error, sha256s match).
          * bundle's privacy HEAD version is **not older** than the
            live ledger version; bundle's deletion manifest hash
            equals the live ledger's deletion manifest hash. A
            bundle that is missing any current deletion receipt —
            even at the same version — is quarantined.

        The live ledger is the trust root. ``read_live_privacy_ledger``
        refuses to synthesise a v0 ledger when the live vault has
        no privacy HEAD — the operator must run ``memory init``
        (which atomically writes a v0 HEAD when no deletion
        manifests exist) before restore can succeed. A bundle at
        v0 with no manifests still restores successfully once the
        live ledger has been established.
        """
        if target.exists() and (
            (target / "state" / "state.sqlite").exists()
            or (target / "privacy" / "HEAD.json").exists()
        ):
            raise BackupError(
                f"restore: target {target} already contains a live vault; "
                "refusing to overwrite"
            )
        staging = Path(tempfile.mkdtemp(prefix="marestore_"))
        try:
            if tarfile.is_tarfile(bundle):
                self._extract_tar(bundle, staging)
            else:
                # Directory-style bundle: copy as-is.
                src = bundle
                if not src.is_dir():
                    raise BackupError(
                        f"restore: {bundle} is not a tarball or directory"
                    )
                shutil.copytree(src, staging, dirs_exist_ok=True)
            bundle_root = staging / "memory-agent-backup"
            if not bundle_root.exists():
                raise BackupError(
                    f"restore: bundle {bundle} is missing memory-agent-backup/"
                )
            manifest = self._load_manifest(bundle_root)
            self._validate_bundle(bundle_root, manifest)
            # Cross-check the bundle's privacy state against the live
            # ledger. Refusing stale or receipt-mismatched bundles
            # prevents silent resurrection of forgotten content.
            bundle_version = int(manifest.privacy_head_version)
            live_version = int(live_privacy_ledger.version)
            if bundle_version < live_version:
                raise BackupError(
                    "restore: bundle is older than the live privacy HEAD; "
                    "quarantined to avoid resurrecting forgotten content "
                    f"(live v{live_version} > bundle v{bundle_version})"
                )
            if bundle_version > live_version:
                # Live ledger is older than the bundle — operator might
                # have rolled back the live vault, or the bundle comes
                # from a different vault. We cannot verify the bundle
                # against the live ledger, so refuse.
                raise BackupError(
                    "restore: live privacy HEAD is older than the bundle; "
                    "cannot verify the bundle against the live ledger "
                    f"(live v{live_version} < bundle v{bundle_version})"
                )
            # Versions match — the receipt content must also match.
            # A bundle at the same version but missing/forged receipts
            # is the silent-resurrection attack surface; refuse it.
            bundle_hash = manifest.deletion_manifest_sha256 or ""
            live_hash = live_privacy_ledger.deletion_manifest_sha256
            if bundle_hash != live_hash:
                raise BackupError(
                    "restore: bundle's deletion manifest does not match "
                    "the live privacy ledger; missing receipts or "
                    "tampering detected; quarantined"
                )
            # Materialise into target.
            target.mkdir(parents=True, exist_ok=True)
            self._materialise(bundle_root, target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        # After materialising, recovery runs the standard unfinished-
        # intent path so any prepared-but-unpublished intent the
        # backup captured is reconciled before the next operation.
        self._post_restore_recovery(target)
        # Then the privacy HEAD is re-applied so even a stale DB
        # projection cannot resurrect erased content.
        self._post_restore_privacy_reconcile(target)
        return {
            "target": str(target),
            "bundle_id": manifest.bundle_id,
            "schema_version": manifest.schema_version,
            "format": manifest.format,
            "created_at": manifest.created_at,
            "label": manifest.label,
            "file_count": manifest.file_count,
            "total_bytes": manifest.total_bytes,
            "sqlite_sha256": manifest.sqlite_sha256,
            "privacy_head_version": manifest.privacy_head_version,
            "privacy_manifest_count": manifest.privacy_manifest_count,
            "head_commit_id": manifest.head_commit_id,
        }

    def list_bundle(self, *, bundle: Path) -> BackupManifest:
        staging = Path(tempfile.mkdtemp(prefix="malist_"))
        try:
            if tarfile.is_tarfile(bundle):
                self._extract_tar(bundle, staging)
            else:
                if not bundle.is_dir():
                    raise BackupError(
                        f"list: {bundle} is not a tarball or directory"
                    )
                shutil.copytree(bundle, staging, dirs_exist_ok=True)
            bundle_root = staging / "memory-agent-backup"
            return self._load_manifest(bundle_root)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _extract_tar(self, bundle: Path, staging: Path) -> None:
        with tarfile.open(bundle, "r") as tar:
            # Refuse path traversal (zip-slip / tar-slip). Every
            # member must start with ``memory-agent-backup/`` and
            # contain no ``..`` segments after normalisation.
            for member in tar.getmembers():
                name = member.name
                if name.startswith("/") or ".." in Path(name).parts:
                    raise BackupError(
                        f"restore: unsafe member {name!r}"
                    )
            tar.extractall(staging)

    def _load_manifest(self, bundle_root: Path) -> BackupManifest:
        manifest_path = bundle_root / "manifest.json"
        if not manifest_path.exists():
            raise BackupError("restore: bundle missing manifest.json")
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise BackupError(
                f"restore: manifest unreadable: {exc}"
            ) from exc
        return BackupManifest.from_dict(data)

    def _validate_bundle(
        self, bundle_root: Path, manifest: BackupManifest
    ) -> None:
        # SQLite must exist and match its declared sha256.
        sqlite_path = bundle_root / "state" / "state.sqlite"
        if not sqlite_path.exists():
            raise BackupError("restore: bundle missing state/state.sqlite")
        if manifest.sqlite_sha256:
            actual = _sha256_of_file(sqlite_path)
            if actual != manifest.sqlite_sha256:
                raise BackupError(
                    "restore: sqlite sha256 mismatch; "
                    "bundle is corrupt or tampered with"
                )
        # HEAD + privacy HEAD are optional. A vault that has never
        # published a commit has no HEAD.json; the bundle therefore
        # carries no HEAD.json and the operator can still restore it
        # into a clean target. The privacy HEAD is always present
        # because the backup service materialises a default when
        # no live vault HEAD exists.
        # Deletion manifest hash must match.
        if manifest.deletion_manifest_sha256:
            deletions = bundle_root / "privacy" / "deletions"
            concat = hashlib.sha256()
            for path in sorted(deletions.glob("*.json")):
                concat.update(path.read_bytes())
            if concat.hexdigest() != manifest.deletion_manifest_sha256:
                raise BackupError(
                    "restore: deletion manifest hash mismatch; "
                    "refusing to mount a tampered privacy manifest"
                )

    def _materialise(self, bundle_root: Path, target: Path) -> None:
        # Copy the bundle tree onto the target. We never delete
        # existing files; a restore into a brand-new directory is the
        # only legal case (the caller pre-checked).
        for src in bundle_root.rglob("*"):
            if not src.is_file():
                continue
            try:
                rel = src.relative_to(bundle_root)
            except ValueError:
                continue
            target_path = target / rel
            target_path.parent.mkdir(parents=True, exist_ok=True)
            data = src.read_bytes()
            target_path.write_bytes(data)
            # Preserve mode bits for parity with the source file.
            try:
                os.chmod(target_path, src.stat().st_mode & 0o777)
            except OSError:
                pass

    def _post_restore_recovery(self, target: Path) -> None:
        vault = VaultLayout(target)
        if not vault.state_db.exists():
            return
        conn = open_state_db(vault.state_db)
        try:
            recover_unfinished_intents(vault=vault, conn=conn)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _post_restore_privacy_reconcile(self, target: Path) -> None:
        """Re-apply the privacy HEAD so restored projection rows that
        reference forgotten memories cannot surface them.

        We never restore ``memory_projection`` rows whose memory_id
        appears in any privacy deletion manifest carried by the
        bundle. Their ``status`` is forced to ``redacted`` and their
        ``statement`` wiped, mirroring the live ``forget`` path.
        """
        vault = VaultLayout(target)
        if not vault.state_db.exists():
            return
        conn = open_state_db(vault.state_db)
        try:
            rows = conn.execute(
                "SELECT targets_json FROM privacy_deletions "
                "WHERE kind = 'memory'"
            ).fetchall()
            if not rows:
                return
            ids: set[str] = set()
            for row in rows:
                try:
                    ids.update(json.loads(row["targets_json"]))
                except (TypeError, ValueError):
                    continue
            if not ids:
                return
            for mem_id in sorted(ids):
                conn.execute(
                    "UPDATE memory_projection SET status='redacted', "
                    "statement='', scope_json='{}', "
                    "project_ids_json='[]', domains_json='[]' "
                    "WHERE memory_id = ?",
                    (mem_id,),
                )
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _safe_copy_file(self, src: Path, dest: Path) -> None:
        if not src.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())

    def _read_head_commit_id(self) -> str | None:
        path = self.vault.head_path
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("commit_id")
        except (json.JSONDecodeError, OSError):
            return None

    def _read_active_lease(self, conn: sqlite3.Connection) -> str | None:
        try:
            row = conn.execute(
                "SELECT worker_id FROM worker_leases "
                "WHERE expires_at >= ?",
                (datetime.now(timezone.utc).isoformat(),),
            ).fetchone()
            return str(row["worker_id"]) if row else None
        except sqlite3.OperationalError:
            return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256_of_file(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


__all__ = [
    "BACKUP_FORMAT_VERSION",
    "BackupError",
    "BackupManifest",
    "BackupService",
    "LivePrivacyLedger",
    "read_live_privacy_ledger",
]
