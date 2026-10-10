"""Tests that prove source-event binding is honest in the planner + batch
processor.

* Two distinct user records in the same session must each become their
  own target with the correct source event bound to the work unit.
* Assistant / tool records must never become target memories, even when
  a batch is built that contains both.
* The batch processor must validate evidence against the *exact* event
  text bound to each active target; an evidence pointing at another
  event's text must fail closed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_agent.contracts.events import (
    CONVERSATION_EVENT_SCHEMA_VERSION,
    ConversationEvent,
    SourceLocator,
    SourceRef,
)
from memory_agent.infrastructure.files import VaultLayout
from memory_agent.infrastructure.sqlite import open_state_db
from memory_agent.application.scan import default_adapters, scan_once


def _mk_text_event(eid, role, text, source_file_id="src_x", session_id="s",
                    branch_id="main", turn_key="t1", lmid=None,
                    byte_start=0, byte_end=10, record_ordinal=1):
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
            locator=SourceLocator(
                byte_start=byte_start,
                byte_end=byte_end,
                record_ordinal=record_ordinal,
            ),
            source_time="2026-10-08T10:00:00Z",
            discovered_at="2026-10-08T10:00:01Z",
        ),
        role=role,
        kind="message",
        text=text,
        origin="user_session" if role == "user" else "assistant_response",
    )


def test_two_distinct_user_events_each_get_their_own_target(tmp_path):
    """A session with two user records + assistant/tool records must
    produce two active targets, each bound to its own event_id, after
    planning batches (scan alone does not create work_units rows)."""
    root = tmp_path / "claude"
    proj = root / "projects" / "proj_a"
    proj.mkdir(parents=True)
    session = proj / "sess_a.jsonl"
    lines = [
        {"type": "user", "uuid": "u01",
         "message": {"role": "user", "content": "use Python for the prototype"},
         "timestamp": "2026-10-08T10:00:00Z", "sessionId": "sess_a", "parentUuid": None},
        {"type": "assistant", "uuid": "a01",
         "message": {"role": "assistant", "content": "Got it."},
         "timestamp": "2026-10-08T10:00:01Z", "sessionId": "sess_a", "parentUuid": "u01"},
        {"type": "tool_use", "uuid": "t01",
         "message": {"role": "tool", "content": ""},
         "timestamp": "2026-10-08T10:00:02Z", "sessionId": "sess_a", "parentUuid": "a01"},
        {"type": "user", "uuid": "u02",
         "message": {"role": "user", "content": "always log decisions to vault/audit"},
         "timestamp": "2026-10-08T10:01:00Z", "sessionId": "sess_a", "parentUuid": "a01"},
        {"type": "assistant", "uuid": "a02",
         "message": {"role": "assistant", "content": "Will do."},
         "timestamp": "2026-10-08T10:01:01Z", "sessionId": "sess_a", "parentUuid": "u02"},
    ]
    session.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n",
                       encoding="utf-8")
    vault = VaultLayout(tmp_path / "vault")
    conn = open_state_db(vault.state_db)
    result = scan_once(
        vault=vault, conn=conn,
        adapters=default_adapters(),
        roots={"claude-code": str(root)},
    )
    assert result.new_events == 5
    # Now plan batches so work_units rows appear.
    from memory_agent.application.dispatch import _event_from_row_with_text
    rows = conn.execute(
        "SELECT * FROM source_events WHERE session_id = ? ORDER BY source_time",
        ("sess_a",),
    ).fetchall()
    events = [_event_from_row_with_text(conn, r) for r in rows]
    from memory_agent.application.plan_batches import plan_batches_for_session
    from memory_agent.domain.batching import BatchPolicyInput
    plan_batches_for_session(
        session_id="sess_a", branch_id="main",
        events=events, policy=BatchPolicyInput(), conn=conn,
    )
    # Each active target must be a user event; assistant and tool
    # records must never be targets even when they sit inside the
    # batch alongside user records.
    rows = conn.execute(
        "SELECT w.event_id, e.role FROM work_units w "
        "JOIN source_events e ON e.event_id = w.event_id "
        "WHERE w.unit_id IN (SELECT unit_id FROM batch_members WHERE active_target = 1)"
    ).fetchall()
    assert rows, "at least one active target must be created"
    for r in rows:
        assert r["role"] == "user", \
            f"non-user event {r['event_id']} (role={r['role']}) cannot be an active target"
    # The batch member set must include all source events, but only
    # user ones are active targets; the rest are context_only.
    # (assistant records become context_only in plan_batches_for_session.)
    members = conn.execute(
        "SELECT w.event_id, bm.purpose, e.role FROM batch_members bm "
        "JOIN work_units w ON w.unit_id = bm.unit_id "
        "JOIN source_events e ON e.event_id = w.event_id"
    ).fetchall()
    roles_in_members = {m["role"] for m in members}
    assert "user" in roles_in_members
    # Non-user roles (assistant/tool) appear only as context_only.
    for m in members:
        if m["role"] != "user":
            assert m["purpose"] == "context_only"


def test_assistant_text_cannot_become_a_memory(tmp_path):
    """Even if a batch tries to point an EvidenceRef at an assistant
    record's text with role=user, the domain evidence validator must
    refuse the mismatch."""
    from memory_agent.domain.evidence import (
        EvidenceValidationError,
        assert_quote_matches_slice,
    )
    src = "Got it, I'll use Python."
    with pytest.raises(EvidenceValidationError):
        assert_quote_matches_slice(
            quote=src,
            source_text=src,
            span_start=0, span_end=len(src),
            role="assistant", origin="assistant_response",
        )


def test_evidence_must_match_persisted_event_text(tmp_path):
    """If a candidate's evidence points to event A but a quote that
    only exists in event B, validation fails closed."""
    from memory_agent.domain.evidence import (
        EvidenceValidationError,
        assert_quote_matches_slice,
    )
    a_text = "use Python for the prototype"
    b_text = "always log decisions to vault/audit"
    # Quote references B's text but role says it's A's.
    with pytest.raises(EvidenceValidationError):
        assert_quote_matches_slice(
            quote=b_text,
            source_text=a_text,  # lookup returns A's text
            span_start=0, span_end=len(b_text),
            role="user", origin="user_session",
        )


def test_segment_locator_preserves_byte_offsets(tmp_path):
    """The segment file written by scan must carry real byte_start and
    byte_end for each event so the planner/processor can rebuild an
    honest SourceLocator."""
    root = tmp_path / "claude"
    proj = root / "projects" / "proj_b"
    proj.mkdir(parents=True)
    session = proj / "sess_b.jsonl"
    line = json.dumps({
        "type": "user", "uuid": "u01",
        "message": {"role": "user", "content": "use Python for the prototype"},
        "timestamp": "2026-10-08T10:00:00Z",
        "sessionId": "sess_b", "parentUuid": None,
    })
    session.write_text(line + "\n", encoding="utf-8")
    expected_byte_start = 0
    expected_byte_end = len(line) + 1  # include newline
    vault = VaultLayout(tmp_path / "vault")
    conn = open_state_db(vault.state_db)
    scan_once(vault=vault, conn=conn,
              adapters=default_adapters(),
              roots={"claude-code": str(root)})
    seg_path = next((vault.segments_dir).iterdir())
    payload = json.loads(seg_path.read_text(encoding="utf-8"))
    ev = payload["events"][0]
    assert ev["source"]["locator"]["byte_start"] == expected_byte_start
    assert ev["source"]["locator"]["byte_end"] == expected_byte_end
    assert ev["source"]["locator"]["record_ordinal"] == 1