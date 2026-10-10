"""Backup contracts (P9 / T17).

Backup and restore are versioned through the same ``CommandRequest``
envelope as everything else; the on-disk shape lives in
``application/backup.py`` and the SQLite backup API is the snapshot
mechanism. Only invented / sanitised fixtures are exercised in
tests; no real user vault is ever read or written.
"""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class BackupCreateParams(BaseModel):
    """Snapshot the vault into a single ``.tar`` (or directory) blob.

    * ``output`` is a *required* absolute path; the parent directory
      must already exist (the create call never creates deep paths).
    * ``format`` is ``tar`` (default) or ``dir``. ``dir`` writes the
      bundle as a directory tree; useful for ``diff`` /
      ``rsync``-style verification.
    * ``label`` is an optional free-form tag stored in the bundle's
      ``manifest.json``. No secrets, no absolute vault paths.
    """

    model_config = ConfigDict(extra="forbid")

    output: str = Field(min_length=1, max_length=1024)
    format: Literal["tar", "dir"] = "tar"
    label: str | None = Field(default=None, max_length=128)


class BackupListParams(BaseModel):
    """Read a backup bundle's manifest.

    ``bundle`` is the path of a backup produced by
    ``backup.create``. The manifest is content-free metadata
    (filenames, sizes, sha256, privacy HEAD version, last deletion
    version).
    """

    model_config = ConfigDict(extra="forbid")

    bundle: str = Field(min_length=1, max_length=1024)


class BackupRestoreParams(BaseModel):
    """Restore a backup bundle into a clean target vault.

    * ``bundle`` is the backup path.
    * ``target`` is the (existing) vault directory into which the
      backup is materialised. The target must NOT contain a live
      vault — the service refuses to overwrite an existing
      ``state/state.sqlite`` or ``privacy/HEAD.json``.

    Quarantine is always on and is not a caller-controlled flag.
    The dispatcher reads the operator's live vault's authoritative
    privacy ledger (``privacy/HEAD.json`` + ``privacy/deletions/``)
    and refuses to mount a bundle that is older than the live
    ledger *or* missing any current deletion receipt. A caller-
    supplied integer version was the previous (unsafe) interface
    and has been removed; an attacker who controls the CLI can
    no longer bypass the privacy check by claiming a lower live
    version.
    """

    model_config = ConfigDict(extra="forbid")

    bundle: str = Field(min_length=1, max_length=1024)
    target: str = Field(min_length=1, max_length=1024)


__all__ = [
    "BackupCreateParams",
    "BackupListParams",
    "BackupRestoreParams",
]
