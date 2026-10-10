"""SQLite state tests covering migrations, idempotency, and recovery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memory_agent.application.commit import CommitService
from memory_agent.application.recovery import recover_unfinished_intents
from memory_agent.application.scan import default_adapters, scan_once
from memory_agent.contracts.events import (
    CONVERSATION_EVENT_SCHEMA_VERSION,
    ConversationEvent,
    SourceLocator,
    SourceRef,
)
from memory_agent.infrastructure.sqlite import open_state_db
from memory_agent.infrastructure.sqlite.repositories import (
    BatchRepo,
    CommitIntentRepo,
    EventRepo,
    MemoryProjectionRepo,
    SegmentRepo,
    SourceFileRepo,
)


def _mk_event(*, eid="evt_1", role="user", text="hello", source_file_id="src1",
              session_id="s1", branch_id="main", turn_key="u01", lmid=None):
    return ConversationEvent(
        schema_version=CONVERSATION_EVENT_SCHEMA_VERSION,
        event_id=eid,
        logical_message_id=lmid or eid,
        revision_hash="rh1",
        source=SourceRef(
            connector="claude-code",
            adapter_version="reader-v0.1",
            host_id="local-mac",
            source_file_id=source_file_id,
            generation=1,
            session_id=session_id,
            branch_id=branch_id,
            turn_key=turn_key,
            project_id=None,
            locator=SourceLocator(byte_start=0, byte_end=10, record_ordinal=1),
            source_time="2026-10-08T10:00:00Z",
            discovered_at="2026-10-08T10:00:01Z",
        ),
        role=role,
        kind="message",
        text=text,
        origin="user_session" if role == "user" else "assistant_response",
    )


class TestSQLiteState:
    def test_migrations_apply_once(self, tmp_vault, tmp_conn):
        # tmp_conn fixture already applied migrations; verify the table exists.
        row = tmp_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='source_files'"
        ).fetchone()
        assert row is not None
        # Idempotent migration: opening a second connection must not error.
        conn2 = open_state_db(tmp_vault.state_db)
        row2 = conn2.execute(
            "SELECT MAX(id) AS v FROM schema_migrations"
        ).fetchone()
        assert row2["v"] >= 1
        conn2.close()

    def test_event_idempotency(self, tmp_conn):
        ev = _mk_event()
        # Source file + segment must exist (FK chain).
        SourceFileRepo(tmp_conn).get_or_register(
            source_file_id=ev.source.source_file_id,
            connector=ev.source.connector,
            session_id=ev.source.session_id,
            branch_id=ev.source.branch_id,
            canonical_path="/tmp/sess_x.jsonl",
            file_identity="test",
            adapter_version=ev.source.adapter_version,
            generation=ev.source.generation,
        )
        seg_id = "seg_a"
        SegmentRepo(tmp_conn).insert(
            segment_id=seg_id, source_file_id=ev.source.source_file_id,
            generation=1, path="/tmp/seg.json", sha256="x",
            start_offset=0, end_offset=10,
        )
        EventRepo(tmp_conn).upsert(ev, seg_id)
        EventRepo(tmp_conn).upsert(ev, seg_id)  # second insert must be no-op
        rows = tmp_conn.execute(
            "SELECT COUNT(*) AS c FROM source_events WHERE event_id = ?",
            (ev.event_id,),
        ).fetchone()
        assert rows["c"] == 1

    def test_canonical_event_key_unique(self, tmp_conn):
        ev = _mk_event()
        SourceFileRepo(tmp_conn).get_or_register(
            source_file_id=ev.source.source_file_id,
            connector=ev.source.connector,
            session_id=ev.source.session_id,
            branch_id=ev.source.branch_id,
            canonical_path="/tmp/sess_x.jsonl",
            file_identity="test",
            adapter_version=ev.source.adapter_version,
            generation=ev.source.generation,
        )
        SegmentRepo(tmp_conn).insert(
            segment_id="seg_b", source_file_id=ev.source.source_file_id,
            generation=1, path="/tmp/seg.json", sha256="x",
            start_offset=0, end_offset=10,
        )
        first = EventRepo(tmp_conn).upsert(ev, "seg_b")
        # Different event_id but same canonical_event_key -> second returns False.
        ev2 = _mk_event(eid="evt_other", lmid="evt_1")
        second = EventRepo(tmp_conn).upsert(ev2, "seg_b")
        assert first is True
        assert second is False
        # And we still have only one row.
        rows = tmp_conn.execute(
            "SELECT COUNT(*) AS c FROM source_events"
        ).fetchone()
        assert rows["c"] == 1

    def test_active_target_unique_per_epoch(self, tmp_conn):
        b1 = {"batch_id": "b1", "session_id": "s", "branch_id": "main",
              "policy_version": "v1", "input_hash": "h", "status": "ready"}
        b2 = {"batch_id": "b2", "session_id": "s", "branch_id": "main",
              "policy_version": "v1", "input_hash": "h", "status": "ready"}
        m1 = {"unit_id": "u1", "purpose": "target", "order": 1, "epoch": 1, "active_target": 1}
        BatchRepo(tmp_conn).create(b1, [m1])
        with pytest.raises(Exception):
            # Same unit_id as active target on a different batch in same epoch.
            BatchRepo(tmp_conn).create(b2, [dict(m1, batch_id=None)])

    def test_scan_writes_segments_idempotently(self, tmp_vault, tmp_conn, claude_root, codex_root):
        adapters = default_adapters()
        roots = {"claude-code": str(claude_root), "codex": str(codex_root)}
        # First scan
        r1 = scan_once(vault=tmp_vault, conn=tmp_conn, adapters=adapters, roots=roots)
        first_events = r1.new_events
        # Second scan = no new content
        r2 = scan_once(vault=tmp_vault, conn=tmp_conn, adapters=adapters, roots=roots)
        assert r2.new_events == 0
        # Third scan still 0
        r3 = scan_once(vault=tmp_vault, conn=tmp_conn, adapters=adapters, roots=roots)
        assert r3.new_events == 0
        assert first_events > 0
        # Append one new record, scan again -> 1 new event
        with (claude_root / "projects" / "proj_test_alpha" / "sess_test_001.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "user", "uuid": "u99",
                "message": {"role": "user", "content": "appended record"},
                "timestamp": "2026-10-08T12:00:00Z",
                "sessionId": "sess_test_001", "parentUuid": None,
            }) + "\n")
        r4 = scan_once(vault=tmp_vault, conn=tmp_conn, adapters=adapters, roots=roots)
        assert r4.new_events == 1
        # Partial final line: append half a JSON line and ensure cursor
        # does not advance past it.
        with (claude_root / "projects" / "proj_test_alpha" / "sess_test_001.jsonl").open("a", encoding="utf-8") as f:
            f.write('{"type":"user","uuid":"u100","message":{"role":')
        r5 = scan_once(vault=tmp_vault, conn=tmp_conn, adapters=adapters, roots=roots)
        assert r5.new_events == 0
        assert r5.partial_record_pending >= 1

    def test_recovery_finalizes_published_intent(self, tmp_vault, tmp_conn):
        svc = CommitService(vault=tmp_vault, conn=tmp_conn)
        from memory_agent.contracts.memory import MemoryAtom
        from memory_agent.contracts.candidates import Scope, EvidenceRef
        now = "2026-10-08T10:00:00+00:00"
        mem = MemoryAtom(
            schema_version=1, memory_id="m_recover", revision=1,
            status="active", kind="requirement",
            abstraction="principle", statement="recovered statement",
            subject="proj_recover", modality="must",
            scope=Scope(level="global"), primary_domain=None, domains=[],
            tags=[], horizon="unknown", asserted_at=now, recorded_at=now,
            evidence=[],
        )
        intent = svc.prepare_commit(
            proposed_memories=[mem], pending_proposals=[],
            base_commit_id="root", privacy_epoch="epoch-0",
            batch_ids=["b_test"], target_unit_ids=["u_test"],
        )
        svc.publish(intent["commit_id"])
        # Don't call finalize. Force a recovery.
        rec = recover_unfinished_intents(vault=tmp_vault, conn=tmp_conn)
        assert rec["finalized"] >= 1
        rows = tmp_conn.execute(
            "SELECT * FROM memory_projection WHERE memory_id = 'm_recover'"
        ).fetchall()
        assert len(rows) == 1

    def test_invalid_lease_token_cannot_finalize(self, tmp_conn):
        repo = BatchRepo(tmp_conn)
        b = {"batch_id": "b_lease", "session_id": "s", "branch_id": "main",
             "policy_version": "v1", "input_hash": "h", "status": "ready"}
        m = {"unit_id": "u1", "purpose": "target", "order": 1, "epoch": 1, "active_target": 1}
        repo.create(b, [m])
        assert repo.claim("b_lease", "good_lease", "2099-01-01T00:00:00Z")
        # Wrong token cannot finalize.
        assert not repo.finalize("b_lease", "completed", "wrong_lease")
        # Right token can.
        assert repo.finalize("b_lease", "completed", "good_lease")