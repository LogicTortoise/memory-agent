"""P9 / T17 — doctor extensions: privacy manifest consistency,
source cursor integrity, intent recoverability.

The doctor must never reveal secrets or full memory text. The
report is a small dataclass with stable diagnostic codes; the
test harness only inspects the codes and counters.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from memory_agent.application.doctor import DoctorReport, run_doctor
from memory_agent.application.privacy import PrivacyService, write_privacy_head
from memory_agent.infrastructure.files import VaultLayout
from memory_agent.infrastructure.sqlite import open_state_db
from memory_agent.infrastructure.sqlite.repositories import CommitIntentRepo


def _fresh_vault(tmp_path: Path) -> VaultLayout:
    vault = VaultLayout(tmp_path)
    open_state_db(vault.state_db).close()
    return vault


def test_doctor_reports_clean_vault(tmp_path):
    vault = _fresh_vault(tmp_path)
    report = run_doctor(vault=vault, run_recovery=False)
    assert isinstance(report, DoctorReport)
    assert report.db_exists is True
    assert report.schema_version is not None
    assert report.head_present is False
    assert report.ok is True
    codes = [d.get("code") for d in report.diagnostics]
    # No pending intents, no privacy manifest drift, no source cursor issues.
    assert "recovery_required" not in codes
    assert "privacy_manifest_drift" not in codes


def test_doctor_reports_pending_intent_when_head_advanced_to_other_commit(tmp_path):
    """A prepared intent whose parent is no longer HEAD triggers a
    recovery_required diagnostic (operator should run ``doctor`` /
    ``recover``)."""
    vault = _fresh_vault(tmp_path)
    conn = open_state_db(vault.state_db)
    # Inject a HEAD that names a *different* commit than the intent's parent.
    vault.head_path.write_text(
        json.dumps({"commit_id": "commit_other"}),
        encoding="utf-8",
    )
    intent_repo = CommitIntentRepo(conn)
    intent_repo.insert({
        "commit_id": "commit_dangling",
        "parent_id": "commit_seed",
        "manifest_ref": str(vault.commits_dir / "commit_dangling" / "manifest.json"),
        "batch_ids": ["batch_x"],
        "target_unit_ids": ["unit_x"],
        "expected_revisions": ["unit_x"],
        "privacy_epoch": "epoch-0",
        "phase": "prepared",
    })
    conn.commit()
    conn.close()
    report = run_doctor(vault=vault, run_recovery=False)
    codes = [d.get("code") for d in report.diagnostics]
    assert "recovery_required" in codes
    assert report.ok is False


def test_doctor_reports_source_cursor_issues(tmp_path):
    """A source_files row whose complete_offset exceeds size is a
    corrupted cursor — operator action required."""
    vault = _fresh_vault(tmp_path)
    conn = open_state_db(vault.state_db)
    real = tmp_path / "broken_cursor.jsonl"
    real.write_text("{}\n", encoding="utf-8")
    conn.execute(
        """INSERT INTO source_files(
            source_file_id, connector, session_id, branch_id,
            canonical_path, file_identity, adapter_version,
            generation, complete_offset, size
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "src_broken", "claude-code", "sess_x", "main",
            str(real), "dev=1:ino=1",
            "reader-v1", 1, 9999, 100,
        ),
    )
    conn.commit()
    conn.close()
    report = run_doctor(vault=vault, run_recovery=False)
    codes = [i.get("code") for i in report.source_cursor_issues]
    assert "cursor_beyond_size" in codes
    assert report.ok is False


def test_doctor_reports_missing_source_file(tmp_path):
    vault = _fresh_vault(tmp_path)
    conn = open_state_db(vault.state_db)
    real = tmp_path / "real.jsonl"
    real.write_text("[]\n", encoding="utf-8")
    conn.execute(
        """INSERT INTO source_files(
            source_file_id, connector, session_id, branch_id,
            canonical_path, file_identity, adapter_version,
            generation, complete_offset, size
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "src_real", "claude-code", "sess_y", "main",
            str(real), "dev=1:ino=1", "reader-v1", 1,
            0, real.stat().st_size,
        ),
    )
    conn.execute(
        """INSERT INTO source_files(
            source_file_id, connector, session_id, branch_id,
            canonical_path, file_identity, adapter_version,
            generation, complete_offset, size
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "src_missing", "claude-code", "sess_z", "main",
            "/tmp/definitely/missing/sess_z.jsonl", "dev=1:ino=1",
            "reader-v1", 1, 0, 0,
        ),
    )
    conn.commit()
    conn.close()
    report = run_doctor(vault=vault, run_recovery=False)
    missing = [
        i for i in report.source_cursor_issues
        if i.get("code") == "source_file_missing"
    ]
    assert any(i.get("source_file_id") == "src_missing" for i in missing)


def test_doctor_reports_privacy_manifest_drift(tmp_path):
    vault = _fresh_vault(tmp_path)
    conn = open_state_db(vault.state_db)
    PrivacyService(vault=vault, conn=conn).forget(
        kind="memory", targets=["mem_drift"],
    )
    conn.close()
    # Reset the privacy HEAD to v0 (simulating a torn write).
    write_privacy_head(vault, version=0, epoch="epoch-0000")
    conn = open_state_db(vault.state_db)
    try:
        report = run_doctor(vault=vault, run_recovery=False)
        codes = [d.get("code") for d in report.diagnostics]
        assert "privacy_manifest_drift" in codes
        assert report.privacy_head_version == 0
        assert report.privacy_receipt_count == 1
        assert report.ok is False
    finally:
        conn.close()


def test_doctor_runs_recoverability_probe(tmp_path):
    vault = _fresh_vault(tmp_path)
    report = run_doctor(vault=vault, run_recovery=True)
    assert report.recoverability_ok is True


def test_doctor_reports_db_lag_after_backup_restore(tmp_path):
    """The privacy HEAD on disk vs the DB mirror must agree. If
    the DB has no privacy_deletions rows but the manifest directory
    is non-empty, ``doctor`` reports the drift.
    """
    vault = _fresh_vault(tmp_path)
    conn = open_state_db(vault.state_db)
    # Forget once, then delete the DB row to simulate the crash window.
    svc = PrivacyService(vault=vault, conn=conn)
    original = PrivacyService._mirror_in_db  # unbound function, not bound method
    def _break(self, receipt):
        self.conn.execute("DELETE FROM privacy_deletions")
        raise RuntimeError("simulated DB crash")
    PrivacyService._mirror_in_db = _break
    try:
        with pytest.raises(RuntimeError):
            svc.forget(kind="memory", targets=["mem_drift"])
    finally:
        PrivacyService._mirror_in_db = original
    conn.close()
    conn2 = open_state_db(vault.state_db)
    try:
        report = run_doctor(vault=vault, run_recovery=False)
        codes = [d.get("code") for d in report.diagnostics]
        assert any(c in ("privacy_db_behind_disk", "privacy_manifest_drift")
                   for c in codes)
        assert report.ok is False
    finally:
        conn2.close()
