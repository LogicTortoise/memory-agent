"""P9 / T17 — privacy deletion barrier.

The privacy deletion barrier is the *only* operation that lets a user
remove memory content. Each test exercises one property of the
contract; the harness is built from sanitised / invented fixtures
(``vault = tmp_path``, ``memory_id = mem_<uuid>``, ``event_id =
evt_<uuid>``), no real session / Telegram / deployment is touched.

Properties under test:

  * The forget operation persists an immutable manifest file under
    ``<vault>/privacy/deletions/``, bumps the atomic
    ``<vault>/privacy/HEAD.json``, mirrors the receipt into the
    ``privacy_deletions`` SQLite table, and emits an audit row —
    *without* preserving any memory text or full-text audit payload.
  * A subsequent recall against the deleted memory_id returns zero
    items and never surfaces the forgotten statement or evidence.
  * A source_block pins the source_files cursor and the scanner
    refuses to re-ingest any new events under that source_file_id.
  * Even after a crash window between HEAD swap and DB mirror, a
    fresh ``is_target_blocked`` consults both the DB and the on-disk
    manifest so the memory does not reappear.
  * An ``unblock`` lifts the source_block re-ingest guard but does
    NOT restore memory content (memory forgets are irreversible).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from memory_agent.application.backup import BackupService
from memory_agent.application.doctor import run_doctor
from memory_agent.application.privacy import (
    PrivacyError,
    PrivacyService,
    read_privacy_head,
)
from memory_agent.application.recall import RecallService
from memory_agent.application.scan import scan_once, default_adapters
from memory_agent.application.source_correction import (
    discover_affected_memories,
)
from memory_agent.contracts.memory import MemoryAtom, MemoryStatus
from memory_agent.contracts.query import RetrievalKind
from memory_agent.domain.scope import Scope
from memory_agent.infrastructure.files import VaultLayout
from memory_agent.infrastructure.sqlite import open_state_db
from memory_agent.infrastructure.sqlite.repositories import (
    AuditRepo,
    MemoryProjectionRepo,
)


def _seed_vault_with_memory(
    tmp_path: Path, *, memory_id: str, statement: str,
) -> tuple[VaultLayout, sqlite3.Connection, Path]:
    """Build a minimal vault containing a single active memory."""
    vault = VaultLayout(tmp_path)
    conn = open_state_db(vault.state_db)
    # Write the immutable memory file (the recall service reads this).
    mem_path = vault.memory_revision_path(memory_id, 1)
    mem_path.parent.mkdir(parents=True, exist_ok=True)
    mem_path.write_text(
        f"---\nid: {memory_id}\nrevision: 1\nstatus: active\n"
        f"scope:\n  level: global\nstatement: |\n  {statement}\n"
        f"evidence: []\n---\n\n{statement}\n",
        encoding="utf-8",
    )
    # Register the projection row.
    MemoryProjectionRepo(conn).upsert({
        "memory_id": memory_id,
        "revision": 1,
        "status": "active",
        "scope_json": json.dumps({"level": "global"}),
        "project_ids_json": "[]",
        "domains_json": "[]",
        "commit_id": "commit_seed",
        "recorded_at": "2026-10-09T00:00:00+00:00",
        "statement": statement,
    })
    # The recall service reads HEAD.json to anchor the snapshot.
    vault.head_path.write_text(
        json.dumps({"commit_id": "commit_seed"}),
        encoding="utf-8",
    )
    return vault, conn, mem_path


def _seed_vault_with_source(tmp_path: Path) -> tuple[VaultLayout, sqlite3.Connection, Path]:
    """Build a sanitised source root + a registered source_files row."""
    vault = VaultLayout(tmp_path)
    conn = open_state_db(vault.state_db)
    root = tmp_path / "src"
    root.mkdir()
    src = root / "sess.jsonl"
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
            canonical_path, file_identity, adapter_version, generation,
            complete_offset, size
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "claude-code-src-test01",
            "claude-code", "sess_test_001", "main",
            str(src), "dev=1:ino=1", "reader-v1", 1, 0, src.stat().st_size,
        ),
    )
    conn.commit()
    return vault, conn, root


# ---------------------------------------------------------------------------
# forget(memory)
# ---------------------------------------------------------------------------


def test_forget_memory_writes_manifest_head_db_audit(tmp_path):
    vault, conn, mem_path = _seed_vault_with_memory(
        tmp_path, memory_id="mem_demo01", statement="Use Python for prototypes",
    )
    svc = PrivacyService(vault=vault, conn=conn)
    receipt = svc.forget(
        kind="memory", targets=["mem_demo01"], actor="test",
    )
    # 1. Manifest file exists.
    manifest_path = (
        vault.root / "privacy" / "deletions" / f"{receipt.op_id}.json"
    )
    assert manifest_path.exists()
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["kind"] == "memory"
    assert data["targets"] == ["mem_demo01"]
    # Content-free: no statement, no evidence, no source text.
    assert "statement" not in data
    assert "evidence" not in data
    assert "text" not in data
    # 2. Privacy HEAD points at version 1.
    head = read_privacy_head(vault)
    assert head["version"] == 1
    # 3. DB mirror exists.
    row = conn.execute(
        "SELECT kind, targets_json, version FROM privacy_deletions "
        "WHERE op_id = ?",
        (receipt.op_id,),
    ).fetchone()
    assert row["kind"] == "memory"
    assert json.loads(row["targets_json"]) == ["mem_demo01"]
    assert int(row["version"]) == 1
    # 4. Audit row appended (without full-text payload).
    audit_row = conn.execute(
        "SELECT actor, action, object_id, outcome FROM audit_events "
        "WHERE action = 'privacy.deleted' ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    assert audit_row["actor"] == "test"
    assert audit_row["object_id"] == receipt.op_id
    assert audit_row["outcome"] == "success"
    # 5. The memory file is shredded / moved to trash (content-free).
    assert not mem_path.exists() or mem_path.stat().st_size == 0
    # 6. The projection row is redacted.
    proj = conn.execute(
        "SELECT status, statement, scope_json FROM memory_projection "
        "WHERE memory_id = ?",
        ("mem_demo01",),
    ).fetchone()
    assert proj["status"] == "redacted"
    assert proj["statement"] == ""
    assert proj["scope_json"] == "{}"
    conn.close()


def test_recall_filters_erased_memory_even_with_live_projection(tmp_path):
    """Even before the DB projection is updated, the recall path
    must filter a memory whose ``memory_id`` is in the manifest."""
    vault, conn, _ = _seed_vault_with_memory(
        tmp_path, memory_id="mem_demo02", statement="Use Python for prototypes",
    )
    # Mark the projection as active so the recall would surface it
    # without the privacy filter.
    MemoryProjectionRepo(conn).upsert({
        "memory_id": "mem_demo02", "revision": 1,
        "status": "active",
        "scope_json": json.dumps({"level": "global"}),
        "project_ids_json": "[]", "domains_json": "[]",
        "commit_id": "commit_seed",
        "recorded_at": "2026-10-09T00:00:00+00:00",
        "statement": "Use Python for prototypes",
    })
    conn.commit()
    # Forget the memory.
    svc = PrivacyService(vault=vault, conn=conn)
    svc.forget(kind="memory", targets=["mem_demo02"])
    # Reset the projection status to 'active' to simulate the case
    # where a stale DB projection still lists the memory as active
    # (e.g. a backup was restored without the privacy HEAD being
    # updated). The privacy filter must still drop it.
    conn.execute(
        "UPDATE memory_projection SET status='active' "
        "WHERE memory_id = ?",
        ("mem_demo02",),
    )
    conn.commit()
    # Run recall with keyword + bm25 to ensure both paths are filtered.
    repo = MemoryProjectionRepo(conn)
    for retrieval in ("keyword", "bm25"):
        svc2 = RecallService(
            vault=vault, memory_projection_repo=repo,
            audit_conn=conn,
        )
        result = svc2.recall(
            query="Python prototype",
            project_ids=[], domain_ids=[], include_archived=False,
            limit=8, retrieval=retrieval,
        )
        items = result.get("items") or []
        assert items == [], (
            f"recall[{retrieval}] returned erased memory: {items}"
        )
        # The privacy filter warns when it actually drops something.
        assert any("privacy" in w for w in (result.get("warnings") or []))
    conn.close()


def test_source_block_pins_cursor_and_blocks_reingest(tmp_path):
    vault, conn, root = _seed_vault_with_source(tmp_path)
    svc = PrivacyService(vault=vault, conn=conn)
    receipt = svc.forget(
        kind="source_block",
        targets=["claude-code-src-test01"],
        actor="test",
    )
    # 1. The privacy_blocks row exists.
    row = conn.execute(
        "SELECT op_id FROM privacy_blocks WHERE source_file_id = ?",
        ("claude-code-src-test01",),
    ).fetchone()
    assert row is not None and row["op_id"] == receipt.op_id
    # 2. source_files cursor is pinned at size, last_status reflects
    #    the block.
    src_row = conn.execute(
        "SELECT complete_offset, size, last_status FROM source_files "
        "WHERE source_file_id = ?",
        ("claude-code-src-test01",),
    ).fetchone()
    assert int(src_row["complete_offset"]) == int(src_row["size"])
    assert src_row["last_status"] == "privacy_blocked"
    # 3. The scanner refuses to ingest any events from this source.
    result = scan_once(
        vault=vault, conn=conn,
        adapters=default_adapters(),
        roots={"claude-code": str(root)},
    )
    assert result.privacy_blocked > 0
    assert result.new_events == 0
    # 4. No source_events row for this source.
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM source_events WHERE source_file_id = ?",
        ("claude-code-src-test01",),
    ).fetchone()["c"]
    assert int(count) == 0
    conn.close()


def test_unblock_lifts_re_ingest_guard_but_does_not_restore_memory(tmp_path):
    vault, conn, root = _seed_vault_with_source(tmp_path)
    svc = PrivacyService(vault=vault, conn=conn)
    receipt = svc.forget(
        kind="source_block",
        targets=["claude-code-src-test01"],
    )
    # Unblock via dispatcher command.
    from memory_agent.application.dispatch import dispatch as _dispatch
    response = _dispatch(
        operation="privacy.unblock",
        params={
            "op_id": receipt.op_id,
            "idempotency_key": "idem_unblock_test_001",
        },
        vault_root=vault.root,
    )
    assert response["status"] == "ok"
    # privacy_blocks row removed.
    row = conn.execute(
        "SELECT 1 AS hit FROM privacy_blocks WHERE source_file_id = ?",
        ("claude-code-src-test01",),
    ).fetchone()
    assert row is None
    # Manifest file still on disk (audit trail), but no blocking row.
    manifest_path = (
        vault.root / "privacy" / "deletions" / f"{receipt.op_id}.json"
    )
    assert manifest_path.exists()
    # Now scan again — new events ARE ingested.
    result = scan_once(
        vault=vault, conn=conn,
        adapters=default_adapters(),
        roots={"claude-code": str(root)},
    )
    assert result.privacy_blocked == 0
    assert result.new_events >= 1
    conn.close()


def test_forget_unknown_kind_is_rejected(tmp_path):
    vault = VaultLayout(tmp_path)
    conn = open_state_db(vault.state_db)
    try:
        svc = PrivacyService(vault=vault, conn=conn)
        with pytest.raises(PrivacyError):
            svc.forget(kind="not_a_kind", targets=["mem_demo"])
    finally:
        conn.close()


def test_forget_empty_targets_is_rejected(tmp_path):
    vault = VaultLayout(tmp_path)
    conn = open_state_db(vault.state_db)
    try:
        svc = PrivacyService(vault=vault, conn=conn)
        with pytest.raises(PrivacyError):
            svc.forget(kind="memory", targets=[])
    finally:
        conn.close()


def test_crash_window_after_head_before_db_mirror(tmp_path):
    """Forget: manifest file + HEAD swap first, then DB mirror.
    A crash between the HEAD swap and the DB insert must NOT resurrect
    the erased content. ``is_target_blocked`` consults the on-disk
    manifest so the answer is correct without the DB row.
    """
    vault, conn, _ = _seed_vault_with_memory(
        tmp_path, memory_id="mem_crashtest", statement="banana",
    )
    # Monkey-patch _mirror_in_db to raise AFTER the manifest + HEAD
    # were already written, simulating a torn write.
    original = PrivacyService._mirror_in_db
    def _broken_mirror(self, receipt):  # noqa: D401
        # Force the DB to forget the row even if it was just inserted.
        self.conn.execute("DELETE FROM privacy_deletions")
        raise RuntimeError("simulated crash")
    PrivacyService._mirror_in_db = _broken_mirror
    try:
        svc = PrivacyService(vault=vault, conn=conn)
        with pytest.raises(RuntimeError):
            svc.forget(kind="memory", targets=["mem_crashtest"])
    finally:
        PrivacyService._mirror_in_db = original
    # DB row may be absent (the simulated crash). But the manifest file
    # is on disk, the HEAD version is 1, and a fresh DB open + doctor
    # / query must still see the memory as blocked.
    # Open a *new* connection (simulating a fresh restart).
    conn2 = open_state_db(vault.state_db)
    svc2 = PrivacyService(vault=vault, conn=conn2)
    assert svc2.current_version() == 1
    assert svc2.is_target_blocked(memory_id="mem_crashtest") is True
    # list_receipts walks the manifest directory and finds the op.
    receipts = svc2.list_receipts()
    assert any(r.op_id for r in receipts)
    assert any(r.targets == ["mem_crashtest"] for r in receipts)
    conn2.close()


def test_recovery_after_db_mirror_failure_via_doctor(tmp_path):
    """``run_doctor`` must report the privacy manifest drift when
    the disk has receipts the DB has not yet mirrored.
    """
    vault, conn, _ = _seed_vault_with_memory(
        tmp_path, memory_id="mem_drift01", statement="cherry",
    )
    original_mirror = PrivacyService._mirror_in_db
    def _break(self, receipt):
        # Drop the DB row to simulate the mirror crash.
        self.conn.execute("DELETE FROM privacy_deletions")
        raise RuntimeError("simulated DB crash")
    PrivacyService._mirror_in_db = _break
    try:
        with pytest.raises(RuntimeError):
            PrivacyService(vault=vault, conn=conn).forget(
                kind="memory", targets=["mem_drift01"],
            )
    finally:
        PrivacyService._mirror_in_db = original_mirror
    conn.close()
    conn2 = open_state_db(vault.state_db)
    try:
        report = run_doctor(vault=vault, run_recovery=False)
        # The manifest exists; the DB has not mirrored; HEAD is
        # bumped. doctor reports the drift as an error.
        codes = [d.get("code") for d in report.diagnostics]
        assert any(c in ("privacy_manifest_drift", "privacy_db_behind_disk")
                   for c in codes)
        assert report.privacy_head_version == 1
        assert report.ok is False
    finally:
        conn2.close()


def test_forget_redacts_query_trace_artefacts(tmp_path):
    """A forget must redact related query traces so the user's query
    text doesn't outlive the memory it referenced."""
    vault, conn, _ = _seed_vault_with_memory(
        tmp_path, memory_id="mem_trace01", statement="lemon",
    )
    # Plant a query trace artefact referencing the memory.
    queries = vault.artifacts_dir / "queries"
    queries.mkdir(parents=True, exist_ok=True)
    trace_id = "trace_abcdef1234"
    (queries / f"{trace_id}.json").write_text(
        json.dumps({
            "trace_id": trace_id,
            "items": [
                {"memory_id": "mem_trace01", "statement": "lemon"},
                {"memory_id": "mem_other", "statement": "orange"},
            ],
        }),
        encoding="utf-8",
    )
    svc = PrivacyService(vault=vault, conn=conn)
    svc.forget(kind="memory", targets=["mem_trace01"])
    # The trace now carries a tombstone for the erased memory; the
    # unrelated memory stays intact.
    payload = json.loads(
        (queries / f"{trace_id}.json").read_text(encoding="utf-8")
    )
    items = payload.get("items") or []
    assert any(
        i.get("redacted") and i.get("memory_id") == "mem_trace01"
        for i in items
    )
    assert any(i.get("memory_id") == "mem_other" for i in items)
    conn.close()
