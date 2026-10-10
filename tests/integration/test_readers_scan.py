"""Reader + scanner + planner integration tests against sanitised fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memory_agent.adapters.sessions import ClaudeCodeReader, CodexReader
from memory_agent.application.plan_batches import plan_batches_for_session
from memory_agent.application.scan import default_adapters, scan_once
from memory_agent.contracts.events import (
    ConversationEvent,
    SourceLocator,
    SourceRef,
)
from memory_agent.domain.batching import BatchPolicyInput


def _make_source(session_id: str, branch_id: str, turn_key: str,
                 *, source_time: str, discovered_at: str) -> SourceRef:
    return SourceRef(
        connector="claude-code",
        adapter_version="reader-v0.1",
        host_id="local-mac",
        source_file_id="src_x",
        generation=1,
        session_id=session_id,
        branch_id=branch_id,
        turn_key=turn_key,
        project_id="proj_e2e",
        locator=SourceLocator(byte_start=0, byte_end=10, record_ordinal=1),
        source_time=source_time,
        discovered_at=discovered_at,
    )


class TestReaders:
    def test_claude_code_parses_two_user_messages(self, claude_root):
        r = ClaudeCodeReader()
        descs = r.discover(str(claude_root))
        assert len(descs) == 1
        page = r.read_delta(descs[0], offset=0, max_bytes=10_000)
        assert page["complete"] is True
        user_msgs = [m for m in page["records"] if m.get("type") == "user"]
        assert len(user_msgs) == 2

    def test_codex_parses_user_messages(self, codex_root):
        r = CodexReader()
        descs = r.discover(str(codex_root))
        assert len(descs) == 1
        page = r.read_delta(descs[0], offset=0, max_bytes=10_000)
        user_msgs = [m for m in page["records"] if m.get("role") == "user"]
        assert len(user_msgs) == 2

    def test_unknown_record_rejected(self, tmp_path):
        root = tmp_path / "claude_unknown"
        proj = root / "projects" / "p"
        proj.mkdir(parents=True)
        f = proj / "s.jsonl"
        with f.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "weird"}) + "\n")
        r = ClaudeCodeReader()
        descs = r.discover(str(root))
        page = r.read_delta(descs[0], 0, 10_000)
        # One good line (the bad JSON line becomes None record for normalize).
        assert len(page["records"]) == 1
        # normalize fails on unknown type:
        from memory_agent.adapters.sessions import UnsupportedFormatError
        with pytest.raises(UnsupportedFormatError):
            r.normalize(page["records"][0], host_id="h", source_file_id="s",
                         generation=1, session_id="s", branch_id="main",
                         project_id=None, discovered_at="2026-10-08T10:00:00Z",
                         record_ordinal=0)


class TestScanAndPlan:
    def test_scan_then_plan_uses_claude_code_reader(self, tmp_vault, tmp_conn, claude_root):
        adapters = default_adapters()
        roots = {"claude-code": str(claude_root), "codex": ""}
        scan_once(vault=tmp_vault, conn=tmp_conn, adapters=adapters, roots=roots)
        # Now plan batches for the session we just scanned.
        events = [
            ConversationEvent(
                schema_version=1, event_id="u01", logical_message_id="u01", revision_hash="rh1",
                source=_make_source(
                    session_id="sess_test_001", branch_id="main", turn_key="u01",
                    source_time="2026-10-08T10:00:00Z", discovered_at="2026-10-08T10:00:01Z",
                ),
                role="user", kind="message", text="use Python for the prototype",
                origin="user_session",
            ),
            ConversationEvent(
                schema_version=1, event_id="u02", logical_message_id="u02", revision_hash="rh1",
                source=_make_source(
                    session_id="sess_test_001", branch_id="main", turn_key="u02",
                    source_time="2026-10-08T10:01:00Z", discovered_at="2026-10-08T10:01:01Z",
                ),
                role="user", kind="message", text="always log decisions to vault/audit",
                origin="user_session",
            ),
        ]
        planned = plan_batches_for_session(
            session_id="sess_test_001", branch_id="main", events=events,
            policy=BatchPolicyInput(), conn=tmp_conn,
        )
        assert len(planned) >= 1
        # Each batch must have one target (the user message).
        for b in planned:
            targets = [m for m in b["members"] if int(m["active_target"]) == 1]
            assert len(targets) == 1

    def test_same_text_in_two_sessions_distinct(self, tmp_vault, tmp_conn, claude_root, codex_root):
        adapters = default_adapters()
        roots = {"claude-code": str(claude_root), "codex": str(codex_root)}
        scan_once(vault=tmp_vault, conn=tmp_conn, adapters=adapters, roots=roots)
        # No event_id collisions in source_events.
        rows = tmp_conn.execute(
            "SELECT event_id, COUNT(*) c FROM source_events GROUP BY event_id HAVING c > 1"
        ).fetchall()
        assert rows == []

    def test_scan_zero_model_calls(self, tmp_vault, tmp_conn, claude_root, codex_root):
        # No model_calls rows after a scan.
        scan_once(vault=tmp_vault, conn=tmp_conn,
                   adapters=default_adapters(),
                   roots={"claude-code": str(claude_root), "codex": str(codex_root)})
        n = tmp_conn.execute("SELECT COUNT(*) AS c FROM model_calls").fetchone()["c"]
        assert n == 0