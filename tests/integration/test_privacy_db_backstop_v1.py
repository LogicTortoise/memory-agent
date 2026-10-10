"""P9 / T17 — privacy ledger DB backstop.

The privacy HEAD durability work (P9 repair-2) closed the *file-only*
resurrection path: ``ensure_privacy_head`` refuses to mint v0 when
manifests exist, and ``read_live_privacy_ledger`` refuses to
synthesise a v0 ledger when the live vault has no HEAD. This file
closes the *DB-side* resurrection path: the SQLite
``privacy_deletions`` table is also a deletion record, and a vault
whose DB still records privacy deletions but whose on-disk HEAD +
manifests were lost must NOT be silently re-initialised to v0, and
must NOT pass a forged same-version bundle through restore.

Properties under test (all on invented fixtures, no real data /
Telegram / network):

  1. ``ensure_privacy_head`` refuses to mint v0 when the state DB
     records any ``privacy_deletions`` rows, even when both
     ``HEAD.json`` and every ``privacy/deletions/*.json`` receipt
     have been deleted from disk.
  2. ``ensure_privacy_head`` refuses when the state DB exists but
     cannot be safely read.
  3. ``read_live_privacy_ledger`` refuses when a HEAD is present at
     the same version, but a DB deletion receipt has no matching
     on-disk manifest file (a forged same-version bundle would
     otherwise pass the hash check).
  4. ``read_live_privacy_ledger`` refuses when the receipt file is
     altered (op_id / kind / targets / version mismatch with the DB).
  5. ``read_live_privacy_ledger`` allows disk ahead + DB missing
     (the file ledger is authoritative after a crash between HEAD
     swap and DB mirror); the DB-lag window is the only place the
     file ledger priority is preserved.
  6. The current happy paths keep working: fresh init writes v0,
     forget bumps, restore of a post-forget bundle honours the
     deletion, and a CLI subprocess that runs through this path
     still fails closed end-to-end.

The harness is built from sanitised / invented fixtures
(``vault = tmp_path``, ``memory_id = mem_<uuid>``,
``event_id = evt_<uuid>``); no real session / Telegram / deployment
is touched.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from memory_agent.application.backup import (
    BackupError,
    BackupService,
    read_live_privacy_ledger,
)
from memory_agent.application.privacy import (
    PrivacyError,
    PrivacyService,
    ensure_privacy_head,
    read_privacy_head,
)
from memory_agent.infrastructure.files import VaultLayout
from memory_agent.infrastructure.sqlite import open_state_db
from memory_agent.infrastructure.sqlite.repositories import (
    MemoryProjectionRepo,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed_memory(
    tmp_path: Path, *, memory_id: str, statement: str = "seed",
) -> VaultLayout:
    """Build a small live vault with one active memory and a v0 HEAD."""
    vault = VaultLayout(tmp_path)
    conn = open_state_db(vault.state_db)
    mem_path = vault.memory_revision_path(memory_id, 1)
    mem_path.parent.mkdir(parents=True, exist_ok=True)
    mem_path.write_text(
        f"---\nid: {memory_id}\nrevision: 1\nstatus: active\n"
        f"scope:\n  level: global\nstatement: |\n  {statement}\n"
        f"evidence: []\n---\n\n{statement}\n",
        encoding="utf-8",
    )
    MemoryProjectionRepo(conn).upsert({
        "memory_id": memory_id, "revision": 1, "status": "active",
        "scope_json": json.dumps({"level": "global"}),
        "project_ids_json": "[]", "domains_json": "[]",
        "commit_id": "commit_seed",
        "recorded_at": "2026-10-09T00:00:00+00:00",
        "statement": statement,
    })
    vault.head_path.write_text(
        json.dumps({"commit_id": "commit_seed"}), encoding="utf-8",
    )
    ensure_privacy_head(vault)
    conn.commit()
    conn.close()
    return vault


def _forget_one_memory(
    tmp_path: Path, *, memory_id: str, actor: str = "test",
) -> tuple[VaultLayout, str]:
    """Seed + forget a memory; return (vault, op_id)."""
    vault = _seed_memory(tmp_path, memory_id=memory_id)
    conn = open_state_db(vault.state_db)
    receipt = PrivacyService(vault=vault, conn=conn).forget(
        kind="memory", targets=[memory_id], actor=actor,
    )
    conn.commit()
    conn.close()
    return vault, receipt.op_id


def _run_cli(
    args: list[str], vault: Path | VaultLayout, timeout: int = 20,
) -> dict[str, Any]:
    vault_path = vault.root if isinstance(vault, VaultLayout) else vault
    cmd = [sys.executable, "-m", "memory_agent",
           "--vault", str(vault_path), *args]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    stdout = (res.stdout or "").strip()
    stderr = (res.stderr or "").strip()
    payload: dict[str, Any]
    if not stdout:
        payload = {"error": {"code": "INVALID_REQUEST",
                              "message": stderr or "no stdout"}}
    else:
        try:
            payload = json.loads(stdout.splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            payload = {"error": {"code": "PARSE_ERROR",
                                  "message": f"stdout={stdout!r} stderr={stderr!r}"}}
    return {"payload": payload, "exit": res.returncode, "stderr": stderr}


# ---------------------------------------------------------------------------
# ensure_privacy_head — DB backstop
# ---------------------------------------------------------------------------


def test_ensure_privacy_head_refuses_when_db_records_deletion(tmp_path):
    """Scenario 1 — DB backstop.

    ``init`` -> ``backup`` (v0) -> ``forget`` (DB records v1) ->
    delete ``HEAD.json`` + every ``privacy/deletions/*.json`` while
    keeping ``state.sqlite``. ``ensure_privacy_head`` MUST refuse to
    mint v0: a v0 bundle could otherwise be mounted and resurrect
    the forgotten content.
    """
    vault, op_id = _forget_one_memory(
        tmp_path, memory_id="mem_dbback01",
    )
    # Sanity: the receipt exists on disk and the DB has the row.
    head = read_privacy_head(vault)
    assert head is not None and head["version"] == 1
    assert (vault.root / "privacy" / "deletions" / f"{op_id}.json").exists()
    # Take the v0 bundle BEFORE we tamper.
    bundle = tmp_path / "pre_resurrect.tar"
    BackupService(vault=vault).create(output=bundle, fmt="tar")
    # Tamper: drop HEAD + every receipt. Keep state.sqlite so the
    # DB still records v1.
    (vault.root / "privacy" / "HEAD.json").unlink()
    for p in (vault.root / "privacy" / "deletions").glob("*.json"):
        p.unlink()
    assert not (vault.root / "privacy" / "HEAD.json").exists()
    assert not list((vault.root / "privacy" / "deletions").glob("*.json"))
    # ``ensure_privacy_head`` refuses.
    with pytest.raises(PrivacyError) as excinfo:
        ensure_privacy_head(vault)
    msg = str(excinfo.value).lower()
    assert "privacy_deletions" in msg or "deletion" in msg, msg
    assert "synthesize" in msg or "v0" in msg, msg
    # No v0 HEAD was written.
    assert not (vault.root / "privacy" / "HEAD.json").exists()
    # ``memory init`` (which calls ensure_privacy_head) must also
    # refuse end-to-end via the CLI.
    res = _run_cli(["init"], vault)
    assert res["exit"] != 0, res
    err = res["payload"].get("error") or {}
    assert err.get("code") == "INTERNAL", res["payload"]
    assert "deletion" in (err.get("message") or "").lower(), err
    assert not (vault.root / "privacy" / "HEAD.json").exists()
    # ``backup.create`` on the tampered vault also refuses — the
    # service refuses because there is no HEAD to snapshot (and
    # even if there were, init is the only path allowed to mint v0).
    out2 = tmp_path / "tampered_bundle.tar"
    with pytest.raises(BackupError):
        BackupService(vault=vault).create(output=out2, fmt="tar")
    # And the old v0 bundle cannot be restored into a clean target
    # via the dispatcher (which consults read_live_privacy_ledger
    # against the tampered live vault).
    target = tmp_path / "resurrect_target"
    res = _run_cli([
        "backup", "restore",
        "--bundle", str(bundle),
        "--target", str(target),
    ], vault)
    assert res["exit"] != 0, res
    err = res["payload"].get("error") or {}
    # Either INTERNAL (privacy ledger unsafe) or RECOVERY_REQUIRED
    # (no state.sqlite on the new target), or INVALID_REQUEST —
    # the important thing is the target stays unmounted.
    assert err.get("code") in {
        "INVALID_REQUEST", "INTERNAL", "RECOVERY_REQUIRED",
    }, res["payload"]
    assert not (target / "state" / "state.sqlite").exists()
    assert not (target / "privacy" / "HEAD.json").exists()


def test_ensure_privacy_head_refuses_when_db_is_unreadable(tmp_path):
    """Scenario 2 — DB safety.

    A state DB that exists but cannot be read (corrupt / not a SQLite
    file / wrong schema) MUST still block the v0 synthesis. We
    cannot claim "the DB has no privacy_deletions rows" if we
    cannot read the DB — a row could still be there.
    """
    vault = VaultLayout(tmp_path)
    # Write garbage into the state DB path so it exists but cannot
    # be opened as SQLite.
    vault.state_db.parent.mkdir(parents=True, exist_ok=True)
    vault.state_db.write_bytes(b"NOT A SQLITE FILE")
    with pytest.raises(PrivacyError) as excinfo:
        ensure_privacy_head(vault)
    msg = str(excinfo.value).lower()
    assert "state db" in msg or "safely read" in msg, msg
    assert "synthesize" in msg or "v0" in msg, msg
    # No v0 HEAD was written.
    assert not (vault.root / "privacy" / "HEAD.json").exists()


def test_ensure_privacy_head_allows_fresh_init_with_empty_db(tmp_path):
    """Sanity: a genuinely fresh init still works.

    An empty state DB (no tables, no rows) is *not* "unsafe" — the
    migration creates the tables and there are no privacy_deletions
    rows. ``ensure_privacy_head`` must atomically write v0.
    """
    vault = VaultLayout(tmp_path)
    # Touch the DB so migrations run, but no privacy rows are added.
    conn = open_state_db(vault.state_db)
    conn.execute(
        "INSERT INTO schema_migrations(id, applied_at) "
        "SELECT 9999, 'never' WHERE 0"
    )
    conn.commit()
    conn.close()
    head = ensure_privacy_head(vault)
    assert head["version"] == 0
    assert head["epoch"] == "epoch-0000"
    assert (vault.root / "privacy" / "HEAD.json").exists()


def test_ensure_privacy_head_blocked_db_refuses_via_init_cli(tmp_path):
    """End-to-end: ``memory init`` itself fails closed when the DB
    records a privacy_deletions row but the on-disk HEAD + receipts
    are missing. Two layers (init + restore) close the path.
    """
    vault, _ = _forget_one_memory(
        tmp_path, memory_id="mem_init_block01",
    )
    # Tamper: drop HEAD + every receipt.
    (vault.root / "privacy" / "HEAD.json").unlink()
    for p in (vault.root / "privacy" / "deletions").glob("*.json"):
        p.unlink()
    res = _run_cli(["init"], vault)
    assert res["exit"] != 0, res
    err = res["payload"].get("error") or {}
    assert err.get("code") == "INTERNAL", res["payload"]
    assert "deletion" in (err.get("message") or "").lower(), err
    assert not (vault.root / "privacy" / "HEAD.json").exists()


# ---------------------------------------------------------------------------
# read_live_privacy_ledger — DB cross-check
# ---------------------------------------------------------------------------


def test_read_live_privacy_ledger_refuses_missing_receipt_for_db_row(tmp_path):
    """Scenario 3 — DB-ahead path.

    HEAD survives at v1, but a receipt file is missing while the
    DB still records the row. The live ledger hash becomes "empty"
    (sha256 of zero files) and a forged same-version bundle could
    pass the version+hash comparison. ``read_live_privacy_ledger``
    must refuse.
    """
    vault, op_id = _forget_one_memory(
        tmp_path, memory_id="mem_dbled01",
    )
    head = read_privacy_head(vault)
    assert head is not None and head["version"] == 1
    # Take the bundle first (so we have a clean reference).
    bundle = tmp_path / "post_forget.tar"
    BackupService(vault=vault).create(output=bundle, fmt="tar")
    # Tamper: drop the receipt file but keep the HEAD + DB.
    receipt_path = vault.root / "privacy" / "deletions" / f"{op_id}.json"
    assert receipt_path.exists()
    receipt_path.unlink()
    # The DB row still exists.
    conn = open_state_db(vault.state_db)
    row = conn.execute(
        "SELECT op_id FROM privacy_deletions WHERE op_id = ?",
        (op_id,),
    ).fetchone()
    assert row is not None
    conn.close()
    # Reader refuses.
    with pytest.raises(BackupError) as excinfo:
        read_live_privacy_ledger(vault)
    msg = str(excinfo.value).lower()
    assert "privacy_deletions" in msg or "op_id" in msg, msg
    assert op_id in str(excinfo.value) or "receipt" in msg, msg


def test_read_live_privacy_ledger_refuses_altered_receipt(tmp_path):
    """Scenario 4 — DB-vs-disk integrity.

    HEAD survives, DB row survives, the receipt file is on disk
    but its ``targets`` have been altered (an attacker rewrote the
    file to point at a different memory_id). The ledger refuses.
    """
    vault, op_id = _forget_one_memory(
        tmp_path, memory_id="mem_dbled02",
    )
    receipt_path = vault.root / "privacy" / "deletions" / f"{op_id}.json"
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    # Forged targets — pretend the forget was for a different memory.
    payload["targets"] = ["mem_different"]
    receipt_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with pytest.raises(BackupError) as excinfo:
        read_live_privacy_ledger(vault)
    msg = str(excinfo.value).lower()
    assert "targets" in msg or "mismatch" in msg, msg


def test_read_live_privacy_ledger_allows_disk_ahead_db_lag(tmp_path):
    """Scenario 5 — DB-lag window (the only place file-ledger priority
    is preserved).

    If the disk has the receipt but the DB has not yet mirrored it
    (a crash between HEAD swap and DB insert), the ledger must
    succeed: the file manifest is the authoritative source and the
    doctor / recovery path catches the lag.
    """
    vault = _seed_memory(tmp_path, memory_id="mem_dblag01")
    # Simulate the disk-ahead + DB-missing window: write a receipt
    # + bump HEAD to v1, but never mirror into SQLite.
    deletions = vault.root / "privacy" / "deletions"
    op_id = "del_lag01"
    payload = {
        "op_id": op_id,
        "kind": "memory",
        "targets": ["mem_dblag01"],
        "privacy_epoch": "epoch-0001",
        "version": 1,
        "created_at": "2026-10-09T00:00:00+00:00",
        "actor": "test",
        "note": None,
    }
    (deletions / f"{op_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # Bump HEAD to v1 (preserve the original v0 epoch).
    vault.privacy_head.write_text(
        json.dumps({
            "schema_version": 1,
            "epoch": "epoch-0000",
            "version": 1,
            "updated_at": "2026-10-09T00:00:00+00:00",
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # The DB has no privacy_deletions row (the simulated lag).
    conn = open_state_db(vault.state_db)
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM privacy_deletions"
    ).fetchone()
    assert int(row["c"]) == 0
    conn.close()
    # The reader accepts: file ledger is authoritative. The epoch
    # stays at the original v0 epoch because the simulated
    # disk-ahead + DB-lag state preserves the original HEAD.
    ledger = read_live_privacy_ledger(vault)
    assert ledger.version == 1
    assert ledger.epoch == "epoch-0000"


def test_read_live_privacy_ledger_refuses_db_version_above_head(tmp_path):
    """Scenario 6 — DB-ahead + HEAD-lag window.

    If the DB has a ``privacy_deletions`` row whose ``version``
    exceeds the on-disk HEAD, the HEAD swap didn't complete — the
    ledger is in a torn state. Restore must refuse.
    """
    vault = _forget_one_memory(
        tmp_path, memory_id="mem_dbhead01",
    )[0]
    # Tamper: roll the HEAD version back to 0 even though the DB
    # still records v1.
    head_path = vault.privacy_head
    head = json.loads(head_path.read_text(encoding="utf-8"))
    head["version"] = 0
    head["epoch"] = "epoch-0000"
    head_path.write_text(
        json.dumps(head, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with pytest.raises(BackupError) as excinfo:
        read_live_privacy_ledger(vault)
    msg = str(excinfo.value).lower()
    assert "head" in msg or "swap" in msg or "incomplete" in msg, msg


def test_read_live_privacy_ledger_refuses_corrupt_db(tmp_path):
    """Scenario 7 — DB safety (corrupt).

    A DB that exists but cannot be opened (corrupt / not SQLite)
    makes the ledger unsafe. The reader refuses rather than
    trust an empty ledger.
    """
    vault = _seed_memory(tmp_path, memory_id="mem_dbcorrupt01")
    # Take a snapshot first so we have a known-good baseline.
    head = read_privacy_head(vault)
    assert head is not None and head["version"] == 0
    # Corrupt the DB.
    vault.state_db.write_bytes(b"NOT A SQLITE FILE")
    with pytest.raises(BackupError) as excinfo:
        read_live_privacy_ledger(vault)
    msg = str(excinfo.value).lower()
    assert "state db" in msg or "read" in msg, msg


def test_read_live_privacy_ledger_happy_path_still_succeeds(tmp_path):
    """Scenario 8 — happy path regression net.

    A live vault whose state DB records privacy_deletions rows AND
    whose on-disk manifest matches must still produce a
    ``LivePrivacyLedger`` so the normal post-forget restore can
    succeed.
    """
    vault, _ = _forget_one_memory(
        tmp_path, memory_id="mem_happy01",
    )
    ledger = read_live_privacy_ledger(vault)
    assert ledger.version == 1
    assert ledger.epoch == "epoch-0000"
    assert ledger.schema_version == 1
    assert ledger.deletion_manifest_sha256 != ""
    # Live happy path: post-forget restore honours the deletion.
    bundle = tmp_path / "post_forget_happy.tar"
    BackupService(vault=vault).create(output=bundle, fmt="tar")
    target = tmp_path / "happy_target"
    result = BackupService(vault=vault).restore(
        bundle=bundle, target=target,
        live_privacy_ledger=ledger,
    )
    assert result["privacy_head_version"] == 1
    assert (target / "state" / "state.sqlite").exists()
    assert (target / "privacy" / "HEAD.json").exists()


def test_backup_create_refuses_when_db_records_deletion_without_head(tmp_path):
    """Scenario 9 — backup.create path.

    ``backup.create`` snapshots the live vault; if the vault has a
    privacy_deletions row but no HEAD, the snapshot has no privacy
    HEAD to record and the restore would fail closed anyway. The
    service refuses early (the head check already exists; we verify
    the new DB backstop does not break that path).
    """
    vault, op_id = _forget_one_memory(
        tmp_path, memory_id="mem_bkrefuse01",
    )
    # Drop the HEAD only.
    vault.privacy_head.unlink()
    out = tmp_path / "refused.tar"
    with pytest.raises(BackupError):
        BackupService(vault=vault).create(output=out, fmt="tar")
    # Also drop the receipts; the DB still records the row.
    (vault.root / "privacy" / "deletions" / f"{op_id}.json").unlink()
    with pytest.raises(BackupError):
        BackupService(vault=vault).create(output=out, fmt="tar")


def test_backup_restore_cli_rejects_when_db_lacks_receipt(tmp_path):
    """Scenario 10 — full CLI subprocess end-to-end.

    A live vault whose DB still records a privacy_deletions row but
    whose on-disk HEAD + receipts were dropped: ``memory init``
    refuses, and the CLI ``backup restore`` of a pre-tamper bundle
    refuses with INTERNAL.
    """
    vault = _forget_one_memory(
        tmp_path, memory_id="mem_clidbled01",
    )[0]
    bundle = tmp_path / "pre_tamper.tar"
    res = _run_cli(["backup", "create", "--output", str(bundle)], vault)
    assert res["exit"] == 0, res
    # Tamper: drop HEAD + every receipt. The DB still records v1.
    (vault.root / "privacy" / "HEAD.json").unlink()
    for p in (vault.root / "privacy" / "deletions").glob("*.json"):
        p.unlink()
    # ``init`` refuses.
    init_res = _run_cli(["init"], vault)
    assert init_res["exit"] != 0, init_res
    err = init_res["payload"].get("error") or {}
    assert err.get("code") == "INTERNAL", init_res["payload"]
    assert "deletion" in (err.get("message") or "").lower(), err
    # ``backup restore`` of the pre-tamper bundle refuses.
    target = tmp_path / "cli_target"
    res = _run_cli([
        "backup", "restore",
        "--bundle", str(bundle),
        "--target", str(target),
    ], vault)
    assert res["exit"] != 0, res
    err = res["payload"].get("error") or {}
    assert err.get("code") in {
        "INVALID_REQUEST", "INTERNAL", "RECOVERY_REQUIRED",
    }, res["payload"]
    assert not (target / "state" / "state.sqlite").exists()
    assert not (target / "privacy" / "HEAD.json").exists()