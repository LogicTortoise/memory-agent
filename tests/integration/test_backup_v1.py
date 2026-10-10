"""P9 / T17 — consistent SQLite + files backup / restore.

The backup service uses the standard-library ``sqlite3.Connection.backup``
API to take a consistent snapshot of a live WAL-mode ``state.sqlite``;
HEAD, the privacy manifest, segments, memories, commits and review
artefacts are copied in the same run. Restore validates the bundle's
``manifest.json`` + sqlite sha256 + deletion manifest hash and refuses
to mount an old bundle that lacks the latest independent privacy
manifest — the bundle is quarantined rather than silently resurrected.

Tests use invented fixtures only (no real user vault).
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path
from typing import Any

import pytest

from memory_agent.application.backup import (
    BACKUP_FORMAT_VERSION,
    BackupError,
    BackupService,
    LivePrivacyLedger,
    read_live_privacy_ledger,
)
from memory_agent.application.privacy import (
    PrivacyService,
    ensure_privacy_head,
)
from memory_agent.infrastructure.files import VaultLayout
from memory_agent.infrastructure.sqlite import open_state_db
from memory_agent.infrastructure.sqlite.repositories import (
    MemoryProjectionRepo,
)


def _seed_vault(tmp_path: Path, *, memory_id: str = "mem_seed",
                source_root: Path | None = None) -> VaultLayout:
    """Build a small live vault: one memory + one source file.

    Atomically establishes a v0 privacy HEAD via ``ensure_privacy_head``
    so the helper matches the contract ``memory init`` enforces: any
    vault that has been initialised (by us or by ``init``) carries a
    durable v0 HEAD. Without this step,
    ``read_live_privacy_ledger`` would raise ``BackupError`` and every
    restore test below would fail before it ran.
    """
    vault = VaultLayout(tmp_path)
    conn = open_state_db(vault.state_db)
    mem_path = vault.memory_revision_path(memory_id, 1)
    mem_path.parent.mkdir(parents=True, exist_ok=True)
    mem_path.write_text(
        f"---\nid: {memory_id}\nrevision: 1\nstatus: active\n"
        "scope:\n  level: global\nstatement: |\n  backup seed\n"
        "evidence: []\n---\n\nbackup seed\n",
        encoding="utf-8",
    )
    MemoryProjectionRepo(conn).upsert({
        "memory_id": memory_id, "revision": 1, "status": "active",
        "scope_json": json.dumps({"level": "global"}),
        "project_ids_json": "[]", "domains_json": "[]",
        "commit_id": "commit_seed",
        "recorded_at": "2026-10-09T00:00:00+00:00",
        "statement": "backup seed",
    })
    vault.head_path.write_text(
        json.dumps({"commit_id": "commit_seed"}),
        encoding="utf-8",
    )
    ensure_privacy_head(vault)
    if source_root is not None:
        # Register one source file row so source_cursor_issues and
        # privacy_blocks behave realistically.
        src = source_root / "sess.jsonl"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(
            json.dumps({
                "type": "user", "uuid": "u01",
                "message": {"role": "user", "content": "remember this"},
                "timestamp": "2026-10-09T00:00:00Z",
                "sessionId": "sess_test_001", "parentUuid": None,
            }) + "\n",
            encoding="utf-8",
        )
        conn.execute(
            """INSERT INTO source_files(
                source_file_id, connector, session_id, branch_id,
                canonical_path, file_identity, adapter_version,
                generation, complete_offset, size
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "claude-code-src-bk01", "claude-code", "sess_test_001",
                "main", str(src), "dev=1:ino=1", "reader-v1", 1,
                0, src.stat().st_size,
            ),
        )
    conn.commit()
    conn.close()
    return vault


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_backup_create_writes_tar_with_manifest(tmp_path):
    src_root = tmp_path / "src"
    vault = _seed_vault(tmp_path, source_root=src_root)
    out = tmp_path / "bundle.tar"
    manifest = BackupService(vault=vault).create(output=out, fmt="tar")
    assert out.exists()
    assert manifest.bundle_id.startswith("bk_")
    assert manifest.schema_version == BACKUP_FORMAT_VERSION
    assert manifest.format == "tar"
    assert manifest.file_count >= 3  # HEAD + privacy HEAD + state.sqlite + mem
    assert manifest.total_bytes > 0
    assert manifest.head_commit_id == "commit_seed"
    # Tar structure: a top-level memory-agent-backup/ directory.
    with tarfile.open(out, "r") as tar:
        names = tar.getnames()
        assert any(n.startswith("memory-agent-backup/") for n in names)
        assert "memory-agent-backup/manifest.json" in names
        assert "memory-agent-backup/state/state.sqlite" in names
        assert "memory-agent-backup/HEAD.json" in names


def test_backup_create_dir_format(tmp_path):
    vault = _seed_vault(tmp_path)
    out = tmp_path / "bundle_dir"
    manifest = BackupService(vault=vault).create(output=out, fmt="dir")
    assert (out / "memory-agent-backup" / "manifest.json").exists()
    assert (out / "memory-agent-backup" / "HEAD.json").exists()
    assert (out / "memory-agent-backup" / "state" / "state.sqlite").exists()
    assert manifest.bundle_id.startswith("bk_")


def test_backup_create_without_init_fails(tmp_path):
    vault = VaultLayout(tmp_path)  # no DB created yet
    out = tmp_path / "bundle.tar"
    with pytest.raises(BackupError):
        BackupService(vault=vault).create(output=out)


def test_backup_manifest_includes_deletion_metadata(tmp_path):
    vault = _seed_vault(tmp_path)
    conn = open_state_db(vault.state_db)
    svc = PrivacyService(vault=vault, conn=conn)
    svc.forget(kind="memory", targets=["mem_seed"])
    conn.close()
    out = tmp_path / "bundle.tar"
    manifest = BackupService(vault=vault).create(output=out, fmt="tar")
    assert manifest.privacy_head_version == 1
    assert manifest.privacy_manifest_count == 1
    assert manifest.deletion_manifest_sha256 is not None
    # The deletion manifest hash should match the manifest inside the
    # bundle (the concatenation of every receipt's bytes).
    with tarfile.open(out, "r") as tar:
        receipts = sorted(
            n for n in tar.getnames()
            if n.startswith("memory-agent-backup/privacy/deletions/")
            and n.endswith(".json")
        )
        assert receipts, "no deletion manifests inside bundle"
        # Recompute the same concatenation the service used.
        import hashlib
        concat = hashlib.sha256()
        for r in receipts:
            f = tar.extractfile(r)
            assert f is not None
            concat.update(f.read())
        assert concat.hexdigest() == manifest.deletion_manifest_sha256


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------


def test_backup_restore_into_clean_vault_works(tmp_path):
    vault = _seed_vault(tmp_path)
    out = tmp_path / "bundle.tar"
    BackupService(vault=vault).create(output=out, fmt="tar")
    # Restore into a fresh, empty target. The live vault has no
    # privacy HEAD (no forgets yet), so the live ledger is the
    # empty version=0 ledger and the bundle (also v0) restores OK.
    target = tmp_path / "restored"
    result = BackupService(vault=vault).restore(
        bundle=out, target=target,
        live_privacy_ledger=read_live_privacy_ledger(vault),
    )
    assert result["bundle_id"].startswith("bk_")
    assert result["head_commit_id"] == "commit_seed"
    # The restored vault has the same memory file.
    assert (target / "objects" / "memories" / "mem_seed" / "r0001.md").exists()
    # The state.sqlite is restored.
    assert (target / "state" / "state.sqlite").exists()
    # The privacy HEAD is restored.
    assert (target / "privacy" / "HEAD.json").exists()
    # Doctor works against the restored vault.
    from memory_agent.application.doctor import run_doctor
    restored = VaultLayout(target)
    report = run_doctor(vault=restored, run_recovery=False)
    assert report.db_exists is True
    assert report.head_present is True
    assert report.privacy_head_version >= 0
    # memory_projection row is queryable after restore.
    conn = open_state_db(restored.state_db)
    proj = MemoryProjectionRepo(conn).list_active()
    assert any(p.get("memory_id") == "mem_seed" for p in proj)
    conn.close()


def test_backup_restore_into_existing_vault_is_rejected(tmp_path):
    vault = _seed_vault(tmp_path)
    out = tmp_path / "bundle.tar"
    BackupService(vault=vault).create(output=out, fmt="tar")
    target = tmp_path / "already_live"
    # Seed the target with a live vault (HEAD + state.sqlite).
    other = _seed_vault(target, memory_id="mem_other")
    assert (target / "state" / "state.sqlite").exists()
    with pytest.raises(BackupError):
        BackupService(vault=other).restore(
            bundle=out, target=target,
            live_privacy_ledger=read_live_privacy_ledger(other),
        )


def test_backup_restore_quarantines_old_bundle(tmp_path):
    """An old bundle that lacks the latest deletion manifest must
    be quarantined, not silently mounted.

    The semantics: when the *live* vault carries a *newer* privacy
    HEAD than the bundle (because the operator has forgotten
    something since the bundle was taken), restore must refuse so
    a forgotten memory cannot be resurrected via an old bundle.
    The live vault is read directly — there is no caller-supplied
    integer version that can substitute for the authoritative
    privacy ledger.
    """
    live = _seed_vault(tmp_path, memory_id="mem_live")
    # Snapshot v0: no forgets yet.
    old_out = tmp_path / "old.tar"
    BackupService(vault=live).create(output=old_out, fmt="tar")
    # A NEWER snapshot taken on a vault where ``mem_live`` has been
    # forgotten. The operator runs ``memory forget`` against the
    # live vault between snapshots.
    conn = open_state_db(live.state_db)
    PrivacyService(vault=live, conn=conn).forget(
        kind="memory", targets=["mem_live"],
    )
    conn.close()
    # Now try to restore the OLD (v0) bundle into a fresh target.
    # The live vault's privacy HEAD is v1; the bundle claims v0.
    # Restore must refuse — target must NOT have state.sqlite.
    target = tmp_path / "would_resurrect"
    with pytest.raises(BackupError):
        BackupService(vault=live).restore(
            bundle=old_out, target=target,
            live_privacy_ledger=read_live_privacy_ledger(live),
        )
    # Quarantined target is NOT mounted (state.sqlite absent).
    assert not (target / "state" / "state.sqlite").exists()
    assert not (target / "privacy" / "HEAD.json").exists()


def test_backup_restore_quarantines_bundle_missing_receipts(tmp_path):
    """A bundle whose privacy HEAD version matches the live vault
    but whose deletion-manifest contents do NOT match the live
    privacy ledger must also be quarantined. Version comparison
    alone is insufficient; the receipt content hash must match.
    """
    live = _seed_vault(tmp_path, memory_id="mem_forge")
    # Forget once so the live vault has v1 with one receipt.
    conn = open_state_db(live.state_db)
    PrivacyService(vault=live, conn=conn).forget(
        kind="source_block",
        targets=["claude-code-src-bk01"],
        note="forge-test source_block",
    )
    conn.close()
    # Take a legitimate bundle *now* (it carries v1 + the receipt).
    full_out = tmp_path / "full.tar"
    BackupService(vault=live).create(output=full_out, fmt="tar")
    # Forge a bundle: open the full tar, drop the deletion receipt
    # file, rewrite the manifest to claim no deletion manifests
    # (privacy_head_version=1, deletion_manifest_sha256=empty). The
# forged bundle and the live vault therefore have matching
# versions but different deletion-manifest hashes. Version
# comparison alone would let it through.
    import io
    forged_out = tmp_path / "forged.tar"
    import hashlib as _hashlib
    empty_concat = _hashlib.sha256(b"").hexdigest()
    with tarfile.open(full_out, "r") as src_tar, \
            tarfile.open(forged_out, "w") as dst_tar:
        for m in src_tar.getmembers():
            # Drop the receipt file from the forged bundle.
            if m.name.startswith(
                "memory-agent-backup/privacy/deletions/"
            ):
                continue
            if m.name == "memory-agent-backup/manifest.json":
                src_buf = src_tar.extractfile(m)
                assert src_buf is not None
                payload = json.loads(src_buf.read().decode("utf-8"))
                payload["privacy_head_version"] = 1
                payload["privacy_manifest_count"] = 0
                payload["deletion_manifest_sha256"] = empty_concat
                buf = json.dumps(payload).encode("utf-8")
                m.size = len(buf)
                dst_tar.addfile(m, io.BytesIO(buf))
            else:
                dst_tar.addfile(m, src_tar.extractfile(m))
    target = tmp_path / "forged_target"
    # Restore must refuse — version matches but receipt hash does not.
    with pytest.raises(BackupError):
        BackupService(vault=live).restore(
            bundle=forged_out, target=target,
            live_privacy_ledger=read_live_privacy_ledger(live),
        )
    assert not (target / "state" / "state.sqlite").exists()


def test_backup_restore_after_forget_does_not_resurrect(tmp_path):
    """A bundle taken *after* the forget must still drop the
    forgotten memory on restore (the manifest is part of the
    bundle)."""
    vault = _seed_vault(tmp_path, memory_id="mem_resurrect")
    conn = open_state_db(vault.state_db)
    PrivacyService(vault=vault, conn=conn).forget(
        kind="memory", targets=["mem_resurrect"],
    )
    conn.close()
    out = tmp_path / "post_forget.tar"
    BackupService(vault=vault).create(output=out, fmt="tar")
    target = tmp_path / "restored"
    BackupService(vault=vault).restore(
        bundle=out, target=target,
        live_privacy_ledger=read_live_privacy_ledger(vault),
    )
    # Restore a forgotten memory's projection must remain redacted.
    restored = VaultLayout(target)
    row = MemoryProjectionRepo(open_state_db(restored.state_db))
    items = row.list_active()
    assert all(i.get("memory_id") != "mem_resurrect" for i in items)


def test_backup_restore_detects_corrupt_sqlite(tmp_path):
    vault = _seed_vault(tmp_path)
    out = tmp_path / "bundle.tar"
    BackupService(vault=vault).create(output=out, fmt="tar")
    # Corrupt the sqlite inside the tar by overwriting the
    # manifest.json's sha256 with garbage.
    with tarfile.open(out, "r") as tar:
        manifest_file = tar.extractfile(
            "memory-agent-backup/manifest.json"
        )
        assert manifest_file is not None
        manifest_bytes = manifest_file.read()
    payload = json.loads(manifest_bytes.decode("utf-8"))
    payload["sqlite_sha256"] = "0" * 64
    # Re-pack the tar with the bad manifest.
    tmp_bad = tmp_path / "bad.tar"
    with tarfile.open(out, "r") as src_tar:
        with tarfile.open(tmp_bad, "w") as dst_tar:
            for m in src_tar.getmembers():
                if m.name == "memory-agent-backup/manifest.json":
                    buf = json.dumps(payload).encode("utf-8")
                    m.size = len(buf)
                    dst_tar.addfile(m, io_buf(buf))
                else:
                    src_buf = src_tar.extractfile(m)
                    if src_buf is None:
                        continue
                    dst_tar.addfile(m, src_buf)
    target = tmp_path / "restored_bad"
    with pytest.raises(BackupError):
        BackupService(vault=vault).restore(
            bundle=tmp_bad, target=target,
            live_privacy_ledger=read_live_privacy_ledger(vault),
        )


def io_buf(data: bytes):
    import io
    return io.BytesIO(data)


def test_backup_list_bundle(tmp_path):
    vault = _seed_vault(tmp_path)
    out = tmp_path / "bundle.tar"
    BackupService(vault=vault).create(output=out, fmt="tar")
    manifest = BackupService(vault=vault).list_bundle(bundle=out)
    assert manifest.bundle_id.startswith("bk_")
    assert manifest.head_commit_id == "commit_seed"


def test_backup_restore_rejects_unsafe_tar_member(tmp_path):
    """Tar-slip / path-traversal guard: an attacker who controls
    the bundle cannot write outside the bundle root."""
    # Build a tar with a malicious member name.
    bad = tmp_path / "evil.tar"
    with tarfile.open(bad, "w") as tar:
        info = tarfile.TarInfo(name="../escape.txt")
        info.size = 2
        tar.addfile(info, io_buf(b"hi"))
    target = tmp_path / "restored"
    vault = VaultLayout(tmp_path / "_noop_")
    with pytest.raises(BackupError):
        BackupService(vault=vault).restore(
            bundle=bad, target=target,
            live_privacy_ledger=read_live_privacy_ledger(vault),
        )


# ---------------------------------------------------------------------------
# Privacy safety (V1 release blocker): restore MUST consult the
# authoritative live vault privacy ledger; a forgotten memory must
# never be resurrectable via an old bundle.
# ---------------------------------------------------------------------------


def _populate_evidence(vault: VaultLayout, *, memory_id: str,
                        statement: str = "backup evidence") -> None:
    """Seed an evidence/recall chain so the post-restore state is
    non-trivial and we can prove the bundle's contents (or its
    absence) actually surface through recall.
    """
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
    conn.commit()
    conn.close()


def test_backup_restore_requires_live_privacy_ledger(tmp_path):
    """The service refuses to restore without an authoritative live
    privacy ledger — no caller-supplied integer can substitute for
    a manifest.
    """
    vault = _seed_vault(tmp_path)
    out = tmp_path / "bundle.tar"
    BackupService(vault=vault).create(output=out, fmt="tar")
    target = tmp_path / "restored"
    # Missing the required keyword argument is a programmer error.
    with pytest.raises(TypeError):
        BackupService(vault=vault).restore(bundle=out, target=target)
    assert not (target / "state" / "state.sqlite").exists()


def test_backup_restore_fails_closed_when_ledger_is_inconsistent(tmp_path):
    """A live vault with deletion manifests but no privacy HEAD
    cannot have its state established; restore must fail closed
    instead of restoring on the assumption of an empty ledger.
    """
    vault = _seed_vault(tmp_path, memory_id="mem_unhead")
    conn = open_state_db(vault.state_db)
    PrivacyService(vault=vault, conn=conn).forget(
        kind="memory", targets=["mem_unhead"],
    )
    conn.close()
    # Take the v1 bundle BEFORE tampering (backup.create refuses
    # on a vault without a privacy HEAD — the new DB-backstop
    # behaviour closes the silent v0 synthesis path).
    out = tmp_path / "bundle.tar"
    BackupService(vault=vault).create(output=out, fmt="tar")
    # Tamper: drop the privacy HEAD. The deletion manifest files
    # still exist on disk, so the ledger is inconsistent.
    vault.privacy_head.unlink()
    # ``read_live_privacy_ledger`` itself must refuse.
    with pytest.raises(BackupError):
        read_live_privacy_ledger(vault)
    # Even with a hand-crafted empty ledger (version=0), restore
    # would not match — but the dispatcher-level guard fires first.
    target = tmp_path / "no_ledger_target"
    # Build the empty-concat hash so the type matches and we test
    # the dispatcher-level guard (not the type system).
    import hashlib as _hl
    empty_hash = _hl.sha256(b"").hexdigest()
    with pytest.raises(BackupError):
        BackupService(vault=vault).restore(
            bundle=out, target=target,
            live_privacy_ledger=LivePrivacyLedger(
                version=0, epoch="epoch-0000",
                schema_version=1, deletion_manifest_sha256=empty_hash,
            ),
        )
    assert not (target / "state" / "state.sqlite").exists()


def test_backup_restore_after_forget_succeeds_with_ledger_match(tmp_path):
    """A bundle taken *after* a forget, restored into a clean
    target, must succeed AND honour the forget (recall does not
    return the forgotten memory; doctor reports the redacted
    status; privacy HEAD is preserved).
    """
    vault = _seed_vault(tmp_path, memory_id="mem_post_forget")
    _populate_evidence(vault, memory_id="mem_post_forget",
                       statement="post-forget evidence")
    conn = open_state_db(vault.state_db)
    PrivacyService(vault=vault, conn=conn).forget(
        kind="memory", targets=["mem_post_forget"],
    )
    conn.close()
    out = tmp_path / "post_forget.tar"
    BackupService(vault=vault).create(output=out, fmt="tar")
    target = tmp_path / "restored_post"
    result = BackupService(vault=vault).restore(
        bundle=out, target=target,
        live_privacy_ledger=read_live_privacy_ledger(vault),
    )
    assert result["privacy_head_version"] == 1
    # The redacted status survives the round-trip — recall cannot
    # resurrect it.
    restored = VaultLayout(target)
    proj_conn = open_state_db(restored.state_db)
    items = MemoryProjectionRepo(proj_conn).list_active()
    assert all(i.get("memory_id") != "mem_post_forget" for i in items)
    # Doctor reports the privacy HEAD and the redacted projection.
    from memory_agent.application.doctor import run_doctor
    report = run_doctor(vault=restored, run_recovery=False)
    assert report.db_exists is True
    assert report.privacy_head_version == 1
    proj_conn.close()


def test_backup_restore_service_direct_quarantine_path(tmp_path):
    """Direct service test for the quarantine path: an old bundle
    is restored while the live vault has a newer privacy HEAD,
    and the service refuses without going through the CLI.
    """
    live = _seed_vault(tmp_path, memory_id="mem_direct")
    _populate_evidence(live, memory_id="mem_direct")
    # Take v0 bundle.
    old_out = tmp_path / "old_direct.tar"
    BackupService(vault=live).create(output=old_out, fmt="tar")
    # Forget on live -> v1.
    conn = open_state_db(live.state_db)
    PrivacyService(vault=live, conn=conn).forget(
        kind="memory", targets=["mem_direct"],
    )
    conn.close()
    # Restore the v0 bundle via the service directly. Must fail.
    target = tmp_path / "direct_target"
    with pytest.raises(BackupError, match=r"older than the live"):
        BackupService(vault=live).restore(
            bundle=old_out, target=target,
            live_privacy_ledger=read_live_privacy_ledger(live),
        )
    assert not (target / "state" / "state.sqlite").exists()


def test_backup_restore_service_direct_crash_recovery_path(tmp_path):
    """Service-level crash-recovery: even if a previous run left a
    half-written bundle on disk, restore can be invoked again
    because it uses a fresh staging directory each time.
    """
    vault = _seed_vault(tmp_path, memory_id="mem_crash")
    _populate_evidence(vault, memory_id="mem_crash")
    out = tmp_path / "crash.tar"
    BackupService(vault=vault).create(output=out, fmt="tar")
    # Plant a stale staging dir at the prefix the service uses; the
    # service must still succeed because it allocates a fresh
    # mkdtemp prefix each call.
    stale = tmp_path / "marestore_stale"
    stale.mkdir()
    target = tmp_path / "restored"
    BackupService(vault=vault).restore(
        bundle=out, target=target,
        live_privacy_ledger=read_live_privacy_ledger(vault),
    )
    assert (target / "state" / "state.sqlite").exists()
    # And we can re-restore into another target — the staging
    # isolation is per-call.
    target2 = tmp_path / "restored_again"
    BackupService(vault=vault).restore(
        bundle=out, target=target2,
        live_privacy_ledger=read_live_privacy_ledger(vault),
    )
    assert (target2 / "state" / "state.sqlite").exists()


# ---------------------------------------------------------------------------
# Direct CLI subprocess tests for the privacy safety bug. These
# drive ``python -m memory_agent --vault <live> backup restore …``
# as a real subprocess so we exercise the full argv path, not the
# service in isolation. No live session / Telegram / network is
# touched — fixtures only.
# ---------------------------------------------------------------------------


def _run_cli(args: list[str], vault: Path | VaultLayout, timeout: int = 20) -> dict[str, Any]:
    import subprocess
    import sys
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


def test_backup_restore_cli_quarantines_old_bundle(tmp_path: Path):
    """The V1 release-blocker test, run through the real CLI.

    Setup: invented vault with one active memory; take an *old*
    bundle (no forgets yet); then forget the memory on the live
    vault. Attempt to restore the old bundle into a clean target
    via ``memory backup restore --bundle old --target clean``
    *without* any extra flags. The CLI must fail, the target must
    remain unmounted, and the forgotten memory must NOT be
    recallable from the (unwritten) target.
    """
    vault = _seed_vault(tmp_path, memory_id="mem_cli_old")
    _populate_evidence(vault, memory_id="mem_cli_old",
                       statement="forgotten CLI memory")
    old_bundle = tmp_path / "old.tar"
    res = _run_cli(["backup", "create", "--output", str(old_bundle)], vault)
    assert res["exit"] == 0, res
    assert res["payload"].get("status") == "ok"
    # Forget on the live vault.
    forget_res = _run_cli([
        "privacy", "forget",
        "--kind", "memory",
        "--target", "mem_cli_old",
        "--idempotency-key", "idem_cli_old_001",
    ], vault)
    assert forget_res["exit"] == 0, forget_res
    assert forget_res["payload"].get("status") == "ok"
    # Attempt to restore the OLD bundle into a clean target,
    # *without* any extra flags. The CLI must refuse.
    target = tmp_path / "clean_target"
    res = _run_cli([
        "backup", "restore",
        "--bundle", str(old_bundle),
        "--target", str(target),
    ], vault)
    # Exit code is INTERNAL or INVALID_REQUEST (CLI groups both
    # into non-zero), and the payload is an error.
    assert res["exit"] != 0, res
    err = (res["payload"].get("error") or {})
    assert err.get("code") in {"INVALID_REQUEST", "INTERNAL"}, res
    assert "quarantined" in (err.get("message") or "").lower() or \
           "older than" in (err.get("message") or "").lower(), err
    # Target must remain unmounted — no state.sqlite, no HEAD.json.
    assert not (target / "state" / "state.sqlite").exists(), \
        f"target was mounted despite quarantine: {list(target.rglob('*'))}"
    assert not (target / "privacy" / "HEAD.json").exists(), \
        f"target privacy HEAD leaked: {list(target.rglob('*'))}"
    # And the forgotten memory must NOT be recallable from the
    # unmounted target (no state.sqlite means there's nothing to
    # recall — but make sure the live vault also still honours the
    # forget).
    proj_conn = open_state_db(vault.state_db)
    items = MemoryProjectionRepo(proj_conn).list_active()
    assert all(i.get("memory_id") != "mem_cli_old" for i in items)
    proj_conn.close()


def test_backup_restore_cli_rejects_no_quarantine_flag(tmp_path: Path):
    """The unsafe ``--no-quarantine`` bypass flag has been removed
    entirely. argparse must reject it as an unknown flag so an
    attacker (or a forgetful operator) cannot opt out of the
    privacy check from the CLI.
    """
    vault = _seed_vault(tmp_path, memory_id="mem_cli_noq")
    out = tmp_path / "any.tar"
    res = _run_cli(["backup", "create", "--output", str(out)], vault)
    assert res["exit"] == 0, res
    target = tmp_path / "noq_target"
    res = _run_cli([
        "backup", "restore",
        "--bundle", str(out),
        "--target", str(target),
        "--no-quarantine",
    ], vault)
    # argparse exits with code 2 on unknown flag; no JSON response.
    assert res["exit"] != 0, res
    assert res["exit"] == 2, res
    # No state.sqlite on the target because the CLI never reached
    # the restore call.
    assert not (target / "state" / "state.sqlite").exists()


def test_backup_restore_cli_rejects_live_privacy_head_version_flag(tmp_path: Path):
    """The unsafe ``--live-privacy-head-version`` flag has been
    removed. A caller-supplied integer could claim a lower live
    version than reality and bypass the quarantine check. argparse
    must reject it as unknown.
    """
    vault = _seed_vault(tmp_path, memory_id="mem_cli_int")
    out = tmp_path / "any_int.tar"
    res = _run_cli(["backup", "create", "--output", str(out)], vault)
    assert res["exit"] == 0, res
    target = tmp_path / "int_target"
    res = _run_cli([
        "backup", "restore",
        "--bundle", str(out),
        "--target", str(target),
        "--live-privacy-head-version", "0",
    ], vault)
    assert res["exit"] == 2, res
    assert not (target / "state" / "state.sqlite").exists()


def test_backup_restore_cli_current_bundle_after_forget_succeeds(tmp_path: Path):
    """The happy-path corollary: a bundle taken *after* the forget
    (so its privacy HEAD equals the live vault's) restores
    successfully through the real CLI, doctor reports ok, and
    recall cannot surface the forgotten memory.
    """
    vault = _seed_vault(tmp_path, memory_id="mem_cli_post")
    _populate_evidence(vault, memory_id="mem_cli_post",
                       statement="post-forget CLI memory")
    forget_res = _run_cli([
        "privacy", "forget",
        "--kind", "memory",
        "--target", "mem_cli_post",
        "--idempotency-key", "idem_cli_post_001",
    ], vault)
    assert forget_res["exit"] == 0, forget_res
    # Bundle taken AFTER the forget: v1 with the receipt.
    bundle = tmp_path / "post.tar"
    res = _run_cli(["backup", "create", "--output", str(bundle)], vault)
    assert res["exit"] == 0, res
    # Restore into a clean target.
    target = tmp_path / "post_target"
    res = _run_cli([
        "backup", "restore",
        "--bundle", str(bundle),
        "--target", str(target),
    ], vault)
    assert res["exit"] == 0, res
    assert res["payload"].get("status") == "ok"
    assert (target / "state" / "state.sqlite").exists()
    assert (target / "privacy" / "HEAD.json").exists()
    # Doctor works against the restored vault and reports ok.
    doctor_res = _run_cli(["doctor"], target)
    assert doctor_res["exit"] == 0, doctor_res
    assert doctor_res["payload"].get("status") == "ok"
    assert (doctor_res["payload"].get("result") or {}).get("ok") is True
    # The forgotten memory is not recallable from the restored vault.
    restored = VaultLayout(target)
    proj_conn = open_state_db(restored.state_db)
    items = MemoryProjectionRepo(proj_conn).list_active()
    assert all(i.get("memory_id") != "mem_cli_post" for i in items)
    proj_conn.close()


# ---------------------------------------------------------------------------
# Privacy HEAD durability (P9 repair-2 — fail-closed restore).
#
# The previous ``read_live_privacy_ledger`` silently synthesised a
# v0 ``LivePrivacyLedger`` when the live vault had no HEAD and no
# deletion manifests; that let an old v0 bundle be restored into an
# un-initialised vault (``--vault /new/uninitialized``) and resurrect
# forgotten content. The fix has two parts:
#
#   1. ``init`` (and any operator-authorised safe path) atomically
#      establishes a zero-version ``privacy/HEAD.json`` when no
#      deletion manifests exist.
#   2. ``read_live_privacy_ledger`` refuses to synthesise v0; a
#      missing HEAD always raises ``BackupError`` so restore cannot
#      mount a bundle against an un-initialised live vault.
#
# The tests below cover the unit invariant, the init contract, the
# backup-create contract, and the CLI subprocess scenarios that prove
# the un-initialised-vault restore is refused end-to-end while the
# normal post-forget restore succeeds and respects deletion.
# ---------------------------------------------------------------------------


def test_read_live_privacy_ledger_raises_on_missing_path(tmp_path):
    """Unit test: a missing privacy HEAD is NOT a trusted empty
    ledger. ``read_live_privacy_ledger`` MUST raise ``BackupError``
    so restore fails closed even when no privacy files exist at all
    under the live vault. This is the regression net for the silent
    v0 synthesis bug.
    """
    # An invented, never-initialised vault: no DB, no privacy HEAD,
    # no deletions.
    layout = VaultLayout(tmp_path / "missing_vault")
    assert not layout.privacy_head.exists()
    assert not (layout.root / "privacy" / "deletions").exists()
    with pytest.raises(BackupError) as excinfo:
        read_live_privacy_ledger(layout)
    # The message points the operator at the explicit safe path
    # (memory init), not at a numeric override.
    msg = str(excinfo.value).lower()
    assert "privacy head" in msg
    assert "synthesize" in msg or "synthesi" in msg
    assert "init" in msg


def test_read_live_privacy_ledger_raises_when_deletions_exist_without_head(
    tmp_path,
):
    """Unit test: a vault that has deletion manifests but no privacy
    HEAD is inconsistent (a forget cannot have produced a manifest
    without bumping the HEAD). The reader refuses even though the
    previous synthetic-v0 path would have happily emitted a
    ``version=0`` ledger and mounted an old bundle.
    """
    layout = VaultLayout(tmp_path / "torn_vault")
    # Plant an orphan deletion manifest.
    deletions = layout.root / "privacy" / "deletions"
    deletions.mkdir(parents=True, exist_ok=True)
    (deletions / "del_orphan.json").write_text(
        json.dumps({"op_id": "del_orphan", "kind": "memory",
                    "targets": ["mem_x"], "version": 1,
                    "privacy_epoch": "epoch-0001",
                    "created_at": "2026-10-09T00:00:00+00:00",
                    "actor": "user_cli"}),
        encoding="utf-8",
    )
    assert not layout.privacy_head.exists()
    with pytest.raises(BackupError):
        read_live_privacy_ledger(layout)


def test_init_cli_creates_v0_privacy_head(tmp_path: Path):
    """End-to-end: ``memory init`` atomically establishes a v0
    ``privacy/HEAD.json`` so the live privacy ledger is durable from
    the first init. Without this, ``read_live_privacy_ledger``
    would refuse every restore against a freshly-initialised vault
    and the operator would have to call a second command before
    restore could succeed.
    """
    vault_root = tmp_path / "fresh"
    res = _run_cli(["init"], vault_root)
    assert res["exit"] == 0, res
    assert res["payload"].get("status") == "ok"
    # The CLI surfaces the established version so the operator can
    # see the contract was honoured.
    result = res["payload"].get("result") or {}
    assert result.get("privacy_head_version") == 0
    assert result.get("privacy_epoch") == "epoch-0000"
    # The HEAD file is on disk, parseable, schema_version=1.
    head_path = vault_root / "privacy" / "HEAD.json"
    assert head_path.exists()
    head = json.loads(head_path.read_text(encoding="utf-8"))
    assert head["version"] == 0
    assert head["epoch"] == "epoch-0000"
    assert head["schema_version"] == 1
    # And the ledger reader succeeds against the new vault.
    layout = VaultLayout(vault_root)
    ledger = read_live_privacy_ledger(layout)
    assert ledger.version == 0
    assert ledger.epoch == "epoch-0000"
    assert ledger.schema_version == 1


def test_init_cli_is_idempotent_on_existing_privacy_head(tmp_path: Path):
    """Re-running ``init`` against an already-initialised vault
    does NOT bump or rewrite the v0 HEAD — the contract is "HEAD
    already exists → return verbatim". A subsequent ``forget``
    still bumps it from the same v0 base.
    """
    vault_root = tmp_path / "reinit"
    res = _run_cli(["init"], vault_root)
    assert res["exit"] == 0, res
    # Forget bumps to v1.
    forget_res = _run_cli([
        "privacy", "forget", "--kind", "memory",
        "--target", "mem_reinit", "--idempotency-key", "reinit_001",
    ], vault_root)
    assert forget_res["exit"] == 0, forget_res
    # Re-init must leave the HEAD at v1 (not silently rewrite to v0).
    reinit = _run_cli(["init"], vault_root)
    assert reinit["exit"] == 0, reinit
    head_path = vault_root / "privacy" / "HEAD.json"
    head = json.loads(head_path.read_text(encoding="utf-8"))
    assert head["version"] == 1


def test_init_cli_refuses_when_deletions_exist_without_head(tmp_path: Path):
    """``memory init`` is the operator-authorised safe path, so it
    MUST refuse (instead of silently synthesising v0) when the vault
    has orphan deletion manifests but no HEAD. This state can only
    appear after a torn write; papering over it would re-create the
    silent-resurrection bug.
    """
    vault_root = tmp_path / "torn"
    # Plant an orphan manifest (no HEAD).
    deletions = vault_root / "privacy" / "deletions"
    deletions.mkdir(parents=True, exist_ok=True)
    (deletions / "del_torn.json").write_text(
        json.dumps({"op_id": "del_torn", "kind": "memory",
                    "targets": ["mem_torn"], "version": 1,
                    "privacy_epoch": "epoch-0001",
                    "created_at": "2026-10-09T00:00:00+00:00",
                    "actor": "user_cli"}),
        encoding="utf-8",
    )
    res = _run_cli(["init"], vault_root)
    # The dispatcher refuses with INTERNAL — the operator must
    # reconcile manually instead of having init silently cover up
    # the inconsistency.
    assert res["exit"] != 0, res
    err = res["payload"].get("error") or {}
    assert err.get("code") == "INTERNAL", res["payload"]
    assert "manifest" in (err.get("message") or "").lower()
    # No v0 HEAD was written.
    head_path = vault_root / "privacy" / "HEAD.json"
    assert not head_path.exists()


def test_backup_create_cli_maintains_v0_privacy_head(tmp_path: Path):
    """``memory backup create`` on a freshly-initialised vault
    (v0 HEAD, no deletion manifests) produces a bundle whose
    privacy HEAD is also v0 with the empty-concat hash. The HEAD
    is maintained, not silently bumped or fabricated.
    """
    vault_root = tmp_path / "fresh_v0"
    init_res = _run_cli(["init"], vault_root)
    assert init_res["exit"] == 0, init_res
    bundle = tmp_path / "fresh.tar"
    create_res = _run_cli([
        "backup", "create", "--output", str(bundle), "--label", "fresh",
    ], vault_root)
    assert create_res["exit"] == 0, create_res
    result = create_res["payload"].get("result") or {}
    assert result.get("privacy_head_version") == 0
    assert result.get("privacy_manifest_count") == 0
    # The bundle carries a v0 HEAD with the empty-concat hash
    # (sha256 of zero bytes), matching the live ledger.
    import hashlib as _hl
    empty_hash = _hl.sha256(b"").hexdigest()
    assert result.get("deletion_manifest_sha256") == empty_hash
    with tarfile.open(bundle, "r") as tar:
        names = tar.getnames()
        assert "memory-agent-backup/privacy/HEAD.json" in names
        head_file = tar.extractfile(
            "memory-agent-backup/privacy/HEAD.json"
        )
        assert head_file is not None
        head = json.loads(head_file.read().decode("utf-8"))
        assert head["version"] == 0
        assert head["epoch"] == "epoch-0000"
        assert head["schema_version"] == 1


def test_backup_restore_cli_uninitialized_target_quarantines(
    tmp_path: Path,
):
    """The exact acceptance scenario from the P9 fail-closed repair.

    Sequence:
      1. ``init`` on the live vault → live privacy HEAD v0.
      2. ``backup create`` on the live vault → bundle at v0.
      3. ``forget`` on the live vault → live privacy HEAD bumps to v1.
      4. ``backup restore`` with the OLD v0 bundle, against a
         brand-new, un-initialised ``--vault /new/uninitialized``,
         into ``--target /empty``.

    Step 4 MUST fail and leave the target unmounted, even though
    no privacy files exist under ``/new/uninitialized``. The
    previous ``read_live_privacy_ledger`` would have synthesised a
    v0 ledger from the empty deletions directory and silently
    mounted the bundle. The new strict reader refuses; restore
    fails closed.
    """
    live = tmp_path / "live"
    res = _run_cli(["init"], live)
    assert res["exit"] == 0, res
    # Old v0 bundle.
    old_bundle = tmp_path / "old.tar"
    create_res = _run_cli([
        "backup", "create", "--output", str(old_bundle), "--label", "old",
    ], live)
    assert create_res["exit"] == 0, create_res
    assert (create_res["payload"].get("result") or {}).get(
        "privacy_head_version"
    ) == 0
    # Forget on the live vault → live HEAD bumps to v1.
    forget_res = _run_cli([
        "privacy", "forget",
        "--kind", "memory",
        "--target", "mem_resurrect",
        "--idempotency-key", "idem_resurrect_001",
    ], live)
    assert forget_res["exit"] == 0, forget_res
    # Attempt to restore the OLD v0 bundle against an
    # UN-INITIALISED live vault into a clean target.
    new_uninit = tmp_path / "new_uninitialized"
    # The new vault must not have been touched before this command.
    assert not (new_uninit / "privacy" / "HEAD.json").exists()
    empty_target = tmp_path / "empty_target"
    res = _run_cli([
        "backup", "restore",
        "--bundle", str(old_bundle),
        "--target", str(empty_target),
    ], new_uninit)
    # The CLI must fail; the target must NOT be mounted. Two
    # layers of defence protect this path: the dispatcher refuses
    # an un-initialised vault with ``RECOVERY_REQUIRED``, and the
    # privacy HEAD reader refuses a vault without a privacy HEAD
    # with ``INTERNAL`` (even if state.sqlite somehow appeared).
    # Either error code is acceptable; the target stays unmounted
    # and the un-initialised vault is not silently bootstrapped.
    assert res["exit"] != 0, res
    err = res["payload"].get("error") or {}
    assert err.get("code") in {
        "INVALID_REQUEST", "INTERNAL", "RECOVERY_REQUIRED",
    }, res["payload"]
    msg = (err.get("message") or "").lower()
    # The message MUST point the operator at `memory init`.
    assert "init" in msg, err
    # Target must remain unmounted — no state.sqlite, no HEAD.json.
    assert not (empty_target / "state" / "state.sqlite").exists(), (
        f"target was mounted despite quarantine: "
        f"{list(empty_target.rglob('*'))}"
    )
    assert not (empty_target / "privacy" / "HEAD.json").exists(), (
        f"target privacy HEAD leaked: "
        f"{list(empty_target.rglob('*'))}"
    )
    # The un-initialised vault was also not silently bootstrapped.
    assert not (new_uninit / "privacy" / "HEAD.json").exists(), (
        "init was implicitly run on the un-initialised vault; "
        "the only path allowed to do that is the explicit operator "
        "`memory init`"
    )


def test_backup_restore_cli_init_but_no_privacy_head_quarantines(
    tmp_path: Path,
):
    """Defence-in-depth: even when the live vault has ``state.sqlite``
    (so the dispatcher's RECOVERY_REQUIRED gate does NOT fire), a
    missing privacy HEAD must still refuse the restore. This is the
    exact layer that the previous synthetic-v0 ledger bypassed.

    We construct a live vault by ``init`` (which writes both
    state.sqlite AND a v0 privacy HEAD), then drop the privacy
    HEAD so the ledger reader's strict mode fires.
    """
    live = tmp_path / "live_drop"
    res = _run_cli(["init"], live)
    assert res["exit"] == 0, res
    old_bundle = tmp_path / "old_drop.tar"
    res = _run_cli(["backup", "create", "--output", str(old_bundle)], live)
    assert res["exit"] == 0, res
    # Forget on the live vault → live HEAD v1.
    res = _run_cli([
        "privacy", "forget",
        "--kind", "memory",
        "--target", "mem_drop",
        "--idempotency-key", "idem_drop_001",
    ], live)
    assert res["exit"] == 0, res
    # Now SIMULATE an un-initialised privacy layer on a vault
    # that DOES have state.sqlite: drop the privacy HEAD. The
    # deletions directory still contains the receipt, so the
    # privacy reader sees a torn state and refuses.
    (live / "privacy" / "HEAD.json").unlink()
    target = tmp_path / "drop_target"
    res = _run_cli([
        "backup", "restore",
        "--bundle", str(old_bundle),
        "--target", str(target),
    ], live)
    assert res["exit"] != 0, res
    err = res["payload"].get("error") or {}
    assert err.get("code") in {"INVALID_REQUEST", "INTERNAL"}, \
        res["payload"]
    msg = (err.get("message") or "").lower()
    # Privacy HEAD error specifically (not the generic RECOVERY_REQUIRED).
    assert "privacy head" in msg or "manifest" in msg, err
    assert not (target / "state" / "state.sqlite").exists()


def test_backup_restore_cli_uninitialized_target_quarantines_no_deletions(
    tmp_path: Path,
):
    """Companion to the quarantine scenario: even when the *live*
    un-initialised vault has no privacy files at all (no HEAD, no
    deletions, not even a privacy directory), the restore MUST
    fail closed. This is the precise pre-fix bug — the previous
    synthetic v0 ledger would have happily mounted the bundle.
    """
    live = tmp_path / "live_min"
    res = _run_cli(["init"], live)
    assert res["exit"] == 0, res
    bundle = tmp_path / "min.tar"
    res = _run_cli(["backup", "create", "--output", str(bundle)], live)
    assert res["exit"] == 0, res
    # Drop the live HEAD so it is "empty" again (this models a
    # brand-new vault that was never initialised at all).
    (live / "privacy" / "HEAD.json").unlink()
    # Sanity: the live vault has no privacy files now.
    assert not (live / "privacy" / "HEAD.json").exists()
    # Use a fully un-touched vault as the live ledger context.
    new_uninit = tmp_path / "completely_new"
    target = tmp_path / "completely_new_target"
    res = _run_cli([
        "backup", "restore",
        "--bundle", str(bundle),
        "--target", str(target),
    ], new_uninit)
    assert res["exit"] != 0, res
    assert not (target / "state" / "state.sqlite").exists()
    assert not (target / "privacy" / "HEAD.json").exists()


def test_backup_restore_cli_normal_after_forget_succeeds_and_respects_deletion(
    tmp_path: Path,
):
    """The corollary acceptance scenario: the happy path must still
    work end-to-end through the real CLI.

      1. ``init`` on the live vault → live HEAD v0.
      2. ``forget`` on the live vault → live HEAD v1, one manifest.
      3. ``backup create`` → bundle carries v1 + the manifest.
      4. ``backup restore --vault live --target /empty`` → restores
         successfully (live ledger matches the bundle), and the
         forget is honoured in the restored vault.
    """
    live = tmp_path / "live_post"
    res = _run_cli(["init"], live)
    assert res["exit"] == 0, res
    # Forget on the live vault.
    forget_res = _run_cli([
        "privacy", "forget",
        "--kind", "memory",
        "--target", "mem_post_v1",
        "--idempotency-key", "idem_post_v1_001",
    ], live)
    assert forget_res["exit"] == 0, forget_res
    # Bundle taken AFTER the forget: v1 with the receipt.
    bundle = tmp_path / "post_v1.tar"
    create_res = _run_cli([
        "backup", "create", "--output", str(bundle), "--label", "post",
    ], live)
    assert create_res["exit"] == 0, create_res
    assert (create_res["payload"].get("result") or {}).get(
        "privacy_head_version"
    ) == 1
    # Restore into a clean target using the live vault as the
    # authoritative ledger source.
    target = tmp_path / "post_target"
    restore_res = _run_cli([
        "backup", "restore",
        "--bundle", str(bundle),
        "--target", str(target),
    ], live)
    assert restore_res["exit"] == 0, restore_res
    assert restore_res["payload"].get("status") == "ok"
    result = restore_res["payload"].get("result") or {}
    assert result.get("privacy_head_version") == 1
    # Target is mounted; doctor runs cleanly.
    assert (target / "state" / "state.sqlite").exists()
    assert (target / "privacy" / "HEAD.json").exists()
    doctor_res = _run_cli(["doctor"], target)
    assert doctor_res["exit"] == 0, doctor_res
    # And the forget is honoured: the forgotten memory's projection
    # is not active in the restored vault.
    proj_conn = open_state_db(VaultLayout(target).state_db)
    items = MemoryProjectionRepo(proj_conn).list_active()
    assert all(i.get("memory_id") != "mem_post_v1" for i in items)
    proj_conn.close()
