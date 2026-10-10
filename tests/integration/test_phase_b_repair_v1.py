"""Direct tests for phase B source-correction defects.

Each test in this file targets one of the five release-critical
defects called out by the first phase-B repair report. The tests are
deliberately written so they fail against the broken code and pass
once the corresponding fix lands.

  1. ``test_middle_rewrite_re_emits_changed_records_with_new_revision`` —
     a same-size edit to a record near the *middle* of a > 8 KiB file
     must create a new generation, a new ``revision_hash`` on the
     changed record, leave the previous segment intact as evidence,
     supersede unconsumed batches and advance the cursor.
  2. ``test_path_rename_preserves_source_progress`` — a same-session
     rename to a new canonical path must reuse the existing
     source_files row (no duplicate target, same generation/cursor).
     ``test_resolve_existing_source_id_does_not_match_same_stem_across_projects``
     pins the negative: two ``s1.jsonl`` basenames in different
     projects must stay independent (no inode match → no source row
     reuse, even though the session stem is identical).
  3. ``test_segment_written_db_not_committed_recovery_is_safe`` —
     injecting a fault between durable segment write and SQL commit
     must leave the cursor, segment count, and event count consistent
     on reopen + rescan (exactly one event/segment, no duplicate
     target).
  4. ``test_backfill_iterates_windows_and_marks_partial_tail`` —
     backfill over a file larger than two ``max_bytes_per_file``
     windows must walk the whole file; the final partial page is
     marked ``backfill_partial`` and the run status flips to
     ``partial``; a re-run over the same range is idempotent.
  5. ``test_context_only_events_never_active_and_total_within_policy`` —
     context rows never become active targets, every user/fragment is
     active at most once, and the planner's total budget never
     exceeds ``policy.max_input_tokens`` when schema/context overhead
     is included.

The file also pins two non-negotiable regression guards:

  * ``test_phase_b_repair_tests_do_not_create_vault_in_repo_root`` —
    every test in this file must finish without leaving a vault /
    state.sqlite / config.toml in the repository root. A past version
    passed a ``VaultLayout`` instance where a ``Path`` was expected
    and the resulting ``str(vault)`` (the default Python repr)
    materialised as a literal directory name under CWD. The
    repository root must never carry a ``<memory_agent.infrastructure
    .files.VaultLayout object at 0x…>/`` directory, the contents it
    would have written, or any other stray vault.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_agent.application.plan_batches import plan_batches_for_session
from memory_agent.application.scan import (
    default_adapters,
    resolve_existing_source_id,
    scan_once,
    _stable_source_id,
    _file_identity,
)
from memory_agent.contracts.events import (
    ConversationEvent,
    SourceLocator,
    SourceRef,
)
from memory_agent.domain.batching import (
    BatchPolicyInput,
    CONTEXT_PREFIX_TOKENS,
    SCHEMA_OVERHEAD_TOKENS,
    heuristic_tokens,
)
from memory_agent.infrastructure.files import VaultLayout
from memory_agent.infrastructure.sqlite import open_state_db
from memory_agent.infrastructure.sqlite.repositories import (
    BatchRepo,
    BackfillRunRepo,
    SourceFileRepo,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(*, eid: str, role: str, text: str, session: str = "sess_b",
                branch: str = "main", ordinal: int = 1,
                parent: str | None = None,
                source_time: str = "2026-10-08T10:00:00Z",
                source_file_id: str = "claude-code-src-test000000000") -> ConversationEvent:
    return ConversationEvent(
        schema_version=1,
        event_id=eid,
        logical_message_id=eid,
        revision_hash=f"rh-{eid}",
        source=SourceRef(
            connector="claude-code",
            adapter_version="reader-v0.1",
            host_id="local-mac",
            source_file_id=source_file_id,
            generation=1,
            session_id=session,
            branch_id=branch,
            turn_key=eid,
            project_id=None,
            locator=SourceLocator(
                byte_start=0,
                byte_end=len(text.encode("utf-8")),
                record_ordinal=ordinal,
            ),
            source_time=source_time,
            discovered_at="2026-10-08T10:00:01Z",
        ),
        role=role,
        kind="message",
        text=text,
        parent_id=parent,
        origin=("user_session" if role == "user" else "assistant_response"),
    )


def _write_claude_session(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ln in lines:
            f.write(json.dumps(ln, ensure_ascii=False))
            f.write("\n")


def _build_long_session(path: Path) -> list[dict]:
    """Write a Claude Code JSONL with several records.

    Records are sized so the file is > 8 KiB, the *middle* record
    starts well past byte 4096 (so ``last_prefix_hash`` alone cannot
    see it), and every line has identical length so a same-size edit
    keeps byte offsets stable.
    """
    filler = "x" * 2000  # ~2200 bytes per record (5 records ~ 11 KiB)
    lines = [
        {"type": "user", "uuid": "u01",
         "message": {"role": "user", "content": filler},
         "timestamp": "2026-10-08T10:00:00Z",
         "sessionId": "sess_long", "parentUuid": None},
        {"type": "assistant", "uuid": "a01",
         "message": {"role": "assistant", "content": filler},
         "timestamp": "2026-10-08T10:00:01Z",
         "sessionId": "sess_long", "parentUuid": "u01"},
        {"type": "user", "uuid": "u02",
         "message": {"role": "user", "content": filler},
         "timestamp": "2026-10-08T10:00:02Z",
         "sessionId": "sess_long", "parentUuid": "a01"},
        {"type": "user", "uuid": "u03",
         "message": {"role": "user", "content": filler},
         "timestamp": "2026-10-08T10:00:03Z",
         "sessionId": "sess_long", "parentUuid": "u02"},
        {"type": "user", "uuid": "u04",
         "message": {"role": "user", "content": filler},
         "timestamp": "2026-10-08T10:00:04Z",
         "sessionId": "sess_long", "parentUuid": "u03"},
    ]
    _write_claude_session(path, lines)
    return lines


# ---------------------------------------------------------------------------
# 1. middle_rewrite must re-emit changed records with new revisions
# ---------------------------------------------------------------------------


class TestMiddleRewrite:

    def test_middle_rewrite_re_emits_changed_records_with_new_revision(self, tmp_path):
        """A same-size edit to a record near the middle of a > 8 KiB
        file must be re-ingested with a new ``revision_hash`` and a
        new ``event_id``; the previous event stays queryable as
        evidence; generation is bumped; unconsumed batches are
        superseded; cursor advances; audit row is recorded.
        """
        claude = tmp_path / "claude"
        proj = claude / "projects" / "p1"
        proj.mkdir(parents=True)
        sess = proj / "sess_long.jsonl"
        lines = _build_long_session(sess)

        vault_root = tmp_path / "vault"
        vault = VaultLayout(vault_root)
        conn = open_state_db(vault.state_db)

        # First incremental scan consumes everything.
        scan_once(
            vault=vault, conn=conn,
            adapters=default_adapters(),
            roots={"claude-code": str(claude)},
        )
        sf_row = SourceFileRepo(conn).get(
            _stable_source_id("claude-code", sess)
        )
        assert sf_row is not None
        gen1 = int(sf_row["generation"])
        offset_after_first = int(sf_row["complete_offset"])
        assert offset_after_first > 8000  # confirm > 8 KiB
        first_event_ids = {
            r["event_id"].split("::")[2]
            for r in conn.execute(
                "SELECT event_id FROM source_events"
            ).fetchall()
        }
        assert len(first_event_ids) == 5

        # Same-size rewrite of the *middle* record (u02). The text
        # keeps the same byte length but changes content, so the
        # ``__revision_hash`` (which hashes the raw line) must change.
        new_lines = list(lines)
        new_lines[2] = {
            "type": "user", "uuid": "u02",
            "message": {"role": "user", "content": ("y" * 2000)},
            "timestamp": "2026-10-08T10:00:02Z",
            "sessionId": "sess_long", "parentUuid": "a01",
        }
        with sess.open("w", encoding="utf-8") as f:
            for ln in new_lines:
                f.write(json.dumps(ln, ensure_ascii=False))
                f.write("\n")
        # The file size and byte offsets of record u02 stay the same
        # so only ``__revision_hash`` changes — exactly the prefix-hash
        # invariant middle_rewrite is supposed to catch.
        size_after_rewrite = sess.stat().st_size
        assert size_after_rewrite == offset_after_first

        r2 = scan_once(
            vault=vault, conn=conn,
            adapters=default_adapters(),
            roots={"claude-code": str(claude)},
        )
        assert r2.middle_rewrites == 1
        sf_row2 = SourceFileRepo(conn).get(
            _stable_source_id("claude-code", sess)
        )
        assert int(sf_row2["generation"]) == gen1 + 1

        # u02 must now have a fresh event_id and revision_hash.
        u02_rows = conn.execute(
            "SELECT event_id, revision_hash FROM source_events "
            "WHERE session_id = ? AND branch_id = ?",
            ("sess_long", "main"),
        ).fetchall()
        u02_event_ids = [
            r["event_id"] for r in u02_rows
            if "::u02::" in r["event_id"]
        ]
        assert len(u02_event_ids) == 2, (
            "the rewritten u02 must produce a new event_id while the "
            "previous one is preserved as evidence"
        )
        new_u02, old_u02 = sorted(u02_event_ids)
        rh_old = conn.execute(
            "SELECT revision_hash FROM source_events WHERE event_id = ?",
            (old_u02,),
        ).fetchone()["revision_hash"]
        rh_new = conn.execute(
            "SELECT revision_hash FROM source_events WHERE event_id = ?",
            (new_u02,),
        ).fetchone()["revision_hash"]
        assert rh_old != rh_new
        # Old u02 must still carry the same canonical_event_key parts
        # as the original scan (logical_message_id == u02, same path)
        # so it remains queryable as evidence.
        assert "::u02::" in old_u02

        # Cursor must advance past the rewritten tail.
        assert int(sf_row2["complete_offset"]) == size_after_rewrite

        # Audit row for source.corrected / middle_rewrite.
        audit_actions = [
            r["action"]
            for r in conn.execute(
                "SELECT action FROM audit_events ORDER BY seq"
            ).fetchall()
        ]
        assert "source.corrected" in audit_actions

        # The new event must have a fresh ``segment_id`` under the new
        # generation while the old segment is preserved.
        seg_rows = conn.execute(
            "SELECT segment_id, generation FROM segments "
            "WHERE source_file_id = ? ORDER BY generation",
            (sf_row2["source_file_id"],),
        ).fetchall()
        generations = sorted({int(r["generation"]) for r in seg_rows})
        assert generations == [1, 2]


# ---------------------------------------------------------------------------
# 1b. Full consumed-prefix hash must cover every byte (P0 source integrity)
# ---------------------------------------------------------------------------


def _build_large_session(path: Path, *, target_bytes: int = 320 * 1024,
                         uuid_prefix: str = "u") -> list[dict]:
    """Write a Claude Code JSONL whose total size is at least
    ``target_bytes``. Every record has the same ``filler`` byte length
    so a same-size edit keeps byte offsets stable.

    Each line is ~ ``len(json.dumps(record)) + 1`` bytes; with a
    ~8200-byte filler, a single record is roughly 8.5 KiB and 40
    records give ~340 KiB — comfortably past the legacy 64 KiB
    head/tail sampler windows.
    """
    filler = "x" * 8200  # ~8500-byte records
    parent_chain: list[str | None] = [None]
    lines: list[dict] = []
    idx = 0
    while True:
        idx += 1
        uuid = f"{uuid_prefix}{idx:03d}"
        parent = parent_chain[-1]
        rec = {
            "type": "user", "uuid": uuid,
            "message": {"role": "user", "content": filler},
            "timestamp": f"2026-10-08T10:00:{idx % 60:02d}Z",
            "sessionId": "sess_huge", "parentUuid": parent,
        }
        size_before = path.stat().st_size if path.exists() else 0
        line_bytes = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
        if size_before + len(line_bytes) > target_bytes and lines:
            break
        with path.open("ab") as f:
            f.write(line_bytes)
        lines.append(rec)
        parent_chain.append(uuid)
    return lines


def _assert_first_and_last_64kib_unchanged(before: bytes, after: bytes) -> None:
    """Sanity guard: in the >256 KiB middle-rewrite test the first and
    last 64 KiB of the file must be byte-identical between scans so the
    legacy head/tail sampler would miss the rewrite. We assert it
    directly so a future test refactor can't accidentally relax this
    invariant and silently make the test pass for the wrong reason.
    """
    assert len(before) == len(after), (
        "file size changed between scans; this test is only meaningful "
        "for same-size middle edits"
    )
    assert len(before) > 256 * 1024, (
        "test premise violated — file is not past the 64 KiB head/tail "
        "windows; the head/tail sampler would still catch the rewrite"
    )
    head = 64 * 1024
    assert before[:head] == after[:head], (
        "first 64 KiB must be unchanged for the rewrite to sit past the "
        "legacy head/tail sampler windows"
    )
    assert before[-head:] == after[-head:], (
        "last 64 KiB must be unchanged for the rewrite to sit past the "
        "legacy head/tail sampler windows"
    )


class TestFullPrefixIntegrity:
    """P0 source-integrity boundary: ``_detect_middle_rewrite`` must
    cover *every* byte up to ``complete_offset``, not just the first
    and last 64 KiB.

    The previous (phase-B) implementation sampled only the first and
    last 64 KiB of the consumed prefix, so a same-size edit in the
    untouched middle of a >256 KiB JSONL file left the stored hashes
    identical. ``scan_once`` then read from the old cursor and
    silently missed the source revision.

    These tests pin the new contract end-to-end:

      * a same-size middle edit in a >256 KiB JSONL triggers
        ``middle_rewrite``, generation bumps, the rewritten record
        gets a new ``event_id`` / ``revision_hash``, the previous
        segment is preserved as evidence, unconsumed batch members
        are superseded, and a ``source.corrected`` audit row names
        the correction;
      * a no-change re-scan yields zero new events and zero
        middle_rewrites.
    """

    def test_middle_rewrite_beyond_64kib_window(self, tmp_path):
        """A same-size edit to a record sitting between the legacy
        64 KiB head and 64 KiB tail windows of a >256 KiB JSONL file
        must be detected by ``_detect_middle_rewrite`` and the full
        prefix-integrity contract must be honoured.
        """
        claude = tmp_path / "claude"
        proj = claude / "projects" / "p1"
        proj.mkdir(parents=True)
        sess = proj / "sess_huge.jsonl"

        # Build a >256 KiB JSONL where every record has the same
        # byte length so a same-size edit does not shift offsets.
        lines = _build_large_session(sess, target_bytes=320 * 1024)
        assert len(lines) >= 32, "test premise: file must hold many records"
        file_size_initial = sess.stat().st_size
        assert file_size_initial > 256 * 1024, (
            "test premise: file must exceed 256 KiB so the middle sits "
            "past both 64 KiB legacy windows"
        )

        vault_root = tmp_path / "vault"
        vault = VaultLayout(vault_root)
        conn = open_state_db(vault.state_db)

        r1 = scan_once(
            vault=vault, conn=conn,
            adapters=default_adapters(),
            roots={"claude-code": str(claude)},
        )
        assert r1.files_changed == 1
        assert r1.middle_rewrites == 0
        assert r1.new_events == len(lines)

        sf_row = SourceFileRepo(conn).get(
            _stable_source_id("claude-code", sess)
        )
        assert sf_row is not None
        gen1 = int(sf_row["generation"])
        revision1 = int(sf_row["revision_seq"])
        cursor1 = int(sf_row["complete_offset"])
        assert cursor1 == file_size_initial

        # The full-prefix hash must be persisted on the first scan so
        # the next incremental scan can compare against it.
        sf_row_hash = sf_row["consumed_prefix_sha256"]
        assert sf_row_hash and len(sf_row_hash) == 64, (
            "first scan must persist consumed_prefix_sha256"
        )

        # Plant unconsumed batch members bound to a few source events
        # so we can verify they get superseded by the rewrite. We
        # create ``work_units`` rows referencing real ``source_events``
        # for this source file and ``batch_members`` rows with
        # ``active_target=1`` so ``_supersede_unconsumed_members``
        # will pick them up on the rewrite.
        events_before = conn.execute(
            "SELECT event_id FROM source_events WHERE source_file_id = ? "
            "ORDER BY discovered_at, event_id LIMIT 3",
            (sf_row["source_file_id"],),
        ).fetchall()
        assert len(events_before) >= 3, (
            "test premise: source_events must hold at least 3 rows"
        )
        batch_repo = BatchRepo(conn)
        for i, r in enumerate(events_before):
            unit_id = f"wu_test_{i:03d}"
            batch_id = f"b_test_{i:03d}"
            conn.execute(
                "INSERT INTO work_units("
                " unit_id, session_id, branch_id, turn_id, event_id,"
                " span_start, span_end, split_policy, parent_unit_id, epoch"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (unit_id, "sess_huge", "main", f"turn_{i}",
                 r["event_id"], 0, 0, None, None, 1),
            )
            batch_repo.create(
                {
                    "batch_id": batch_id,
                    "session_id": "sess_huge",
                    "branch_id": "main",
                    "policy_version": "test-v1",
                    "input_hash": f"ih_{i:03d}",
                    "status": "ready",
                },
                [{
                    "unit_id": unit_id,
                    "purpose": "target",
                    "order": 0,
                    "epoch": 1,
                    "active_target": 1,
                }],
            )
        unconsumed_before = conn.execute(
            "SELECT count(*) c FROM batch_members WHERE active_target = 1"
        ).fetchone()["c"]
        assert unconsumed_before == 3, (
            "test premise: three unconsumed batch members must be "
            "planted before the rewrite so we can verify supersede"
        )

        # ---- the rewrite: same-size edit in the *middle* of the file ----
        # Record index ``len(lines) // 2`` sits between the legacy 64 KiB
        # head and 64 KiB tail windows because the file is >256 KiB and
        # each record is ~8.5 KiB; verify that before rewriting.
        target_idx = len(lines) // 2
        target_uuid = lines[target_idx]["uuid"]
        # Sanity-check the invariant: the rewritten record sits past
        # both 64 KiB windows.
        assert target_idx > 7 and target_idx < len(lines) - 7, (
            "target record must sit between the head and tail 64 KiB "
            "windows, not inside either"
        )

        # Capture the file bytes BEFORE the rewrite so the
        # post-rewrite invariant assertion below is meaningful.
        before_bytes = sess.read_bytes()

        # Replace the target record's user content with the same byte
        # length so the file size and byte offsets are unchanged. The
        # raw line bytes change (different characters), so the
        # ``__revision_hash`` of the parsed record must change.
        new_lines = list(lines)
        replacement = dict(lines[target_idx])
        # Same byte length: filler of identical length, different chars.
        replacement["message"] = {"role": "user",
                                  "content": ("y" * 8200)}
        new_lines[target_idx] = replacement
        with sess.open("w", encoding="utf-8") as f:
            for ln in new_lines:
                f.write(json.dumps(ln, ensure_ascii=False))
                f.write("\n")
        after_bytes = sess.read_bytes()
        # Same-size rewrite invariant: the file is exactly the same
        # length so a content-preserving scanner reading from the
        # old cursor would silently miss the rewrite.
        assert len(after_bytes) == len(before_bytes)
        assert sess.stat().st_size == cursor1
        # The legacy 64 KiB head/tail windows are byte-identical, so
        # the phase-B head/tail sampler would have missed the rewrite.
        _assert_first_and_last_64kib_unchanged(before_bytes, after_bytes)
        # And the middle byte range actually contains different bytes.
        assert before_bytes != after_bytes

        r2 = scan_once(
            vault=vault, conn=conn,
            adapters=default_adapters(),
            roots={"claude-code": str(claude)},
        )

        # The full-prefix hash MUST have detected the rewrite.
        assert r2.middle_rewrites == 1, (
            "rewrite past the 64 KiB head/tail windows must be detected "
            "by the full consumed-prefix hash; got middle_rewrites="
            f"{r2.middle_rewrites}"
        )

        sf_row2 = SourceFileRepo(conn).get(
            _stable_source_id("claude-code", sess)
        )
        assert sf_row2 is not None
        # Generation bumped (previous segment preserved as evidence).
        assert int(sf_row2["generation"]) == gen1 + 1
        assert int(sf_row2["revision_seq"]) == revision1 + 1
        # Cursor advanced past the new EOF (same here, but the
        # transaction that wrote the new hash also advanced cursor).
        assert int(sf_row2["complete_offset"]) == cursor1
        # The new full-prefix hash must differ from the previous one.
        assert sf_row2["consumed_prefix_sha256"] != sf_row_hash

        # The rewritten logical message (target_uuid) must now have
        # *two* event_ids: the original (preserved as evidence) and a
        # new one with a fresh revision_hash. Other records must keep
        # exactly one event_id.
        rows = conn.execute(
            "SELECT se.event_id, se.revision_hash, se.source_file_id, "
            "       seg.generation "
            "FROM source_events se "
            "JOIN segments seg ON seg.segment_id = se.segment_id "
            "WHERE se.session_id = ? AND se.branch_id = ?",
            ("sess_huge", "main"),
        ).fetchall()
        events_for_target = [
            r for r in rows if f"::{target_uuid}::" in r["event_id"]
        ]
        assert len(events_for_target) == 2, (
            "the rewritten logical message must produce a new event_id "
            "while the previous one is preserved as evidence"
        )
        rh_set = {r["revision_hash"] for r in events_for_target}
        assert len(rh_set) == 2, (
            "old and new revision_hash must differ for the rewritten record"
        )
        generations_present = sorted({int(r["generation"]) for r in rows})
        assert generations_present == [1, 2], (
            f"both old (gen=1) and new (gen=2) events must coexist; got "
            f"{generations_present}"
        )

        # Old segment (gen=1) must still be present; a new segment
        # (gen=2) must have been written for the re-read.
        seg_rows = conn.execute(
            "SELECT segment_id, generation FROM segments "
            "WHERE source_file_id = ? ORDER BY generation",
            (sf_row2["source_file_id"],),
        ).fetchall()
        assert [int(r["generation"]) for r in seg_rows] == [1, 2], (
            "old segment must be preserved as evidence and a new "
            "segment must exist under the bumped generation"
        )

        # Unconsumed batch members from the previous generation must
        # have been superseded by the rewrite.
        unconsumed_after = conn.execute(
            "SELECT count(*) c FROM batch_members WHERE active_target = 1"
        ).fetchone()["c"]
        assert unconsumed_after == 0, (
            "all unconsumed batch members from the pre-rewrite plan "
            "must be superseded so stale leases cannot re-claim them"
        )

        # Audit log must name the correction.
        audit_rows = conn.execute(
            "SELECT action, details_json FROM audit_events "
            "WHERE object_type = 'source_file' AND object_id = ? "
            "ORDER BY seq",
            (sf_row2["source_file_id"],),
        ).fetchall()
        correction_actions = [
            r for r in audit_rows if r["action"] == "source.corrected"
        ]
        assert correction_actions, (
            "audit log must record a source.corrected action naming "
            "the middle_rewrite disposition"
        )
        # The most recent correction must reference the new generation
        # and the middle_rewrite disposition.
        latest = correction_actions[-1]
        details = json.loads(latest["details_json"])
        assert details.get("disposition") == "middle_rewrite", (
            f"audit details must name the disposition; got {details!r}"
        )
        assert details.get("new_generation") == gen1 + 1
        assert details.get("old_generation") == gen1

    def test_unchanged_rescan_yields_zero_new_events(self, tmp_path):
        """Re-scanning a JSONL file with no changes after the first
        ingest must report zero new events, zero duplicates counted
        as new, and zero middle_rewrites. This pins that the
        full-prefix hash matches itself on an unchanged file and the
        detector does not falsely flag no-op rescans as rewrites.
        """
        claude = tmp_path / "claude"
        proj = claude / "projects" / "p1"
        proj.mkdir(parents=True)
        sess = proj / "sess_idem.jsonl"
        lines = _build_large_session(sess, target_bytes=280 * 1024)
        assert len(lines) >= 30

        vault_root = tmp_path / "vault"
        vault = VaultLayout(vault_root)
        conn = open_state_db(vault.state_db)

        r1 = scan_once(
            vault=vault, conn=conn,
            adapters=default_adapters(),
            roots={"claude-code": str(claude)},
        )
        assert r1.new_events == len(lines)
        first_total = conn.execute(
            "SELECT count(*) c FROM source_events"
        ).fetchone()["c"]

        # Second scan with no file changes.
        r2 = scan_once(
            vault=vault, conn=conn,
            adapters=default_adapters(),
            roots={"claude-code": str(claude)},
        )
        assert r2.middle_rewrites == 0, (
            "an unchanged file must not be flagged as a rewrite"
        )
        assert r2.new_events == 0, (
            f"an unchanged re-scan must produce zero new events; got {r2.new_events}"
        )
        second_total = conn.execute(
            "SELECT count(*) c FROM source_events"
        ).fetchone()["c"]
        assert second_total == first_total, (
            "no-op re-scan must not change the source_events total"
        )

        # Generation and revision_seq must stay the same.
        sf_row = SourceFileRepo(conn).get(
            _stable_source_id("claude-code", sess)
        )
        assert int(sf_row["generation"]) == 1
        assert int(sf_row["revision_seq"]) == 1


# ---------------------------------------------------------------------------
# 2. Path rename must preserve source progress
# ---------------------------------------------------------------------------


class TestRenamePreservesProgress:

    def test_path_rename_preserves_source_progress(self, tmp_path):
        """Renaming a session file to a new sibling path with the same
        session stem must reuse the existing source row so generation,
        cursor, and revision_seq are preserved. The next scan after
        the rename must consume *only* the appended message and must
        not duplicate the previous content.
        """
        claude = tmp_path / "claude"
        proj = claude / "projects" / "p1"
        proj.mkdir(parents=True)
        old_path = proj / "sess_rename.jsonl"
        with old_path.open("w", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "user", "uuid": "u01",
                "message": {"role": "user", "content": "before rename"},
                "timestamp": "2026-10-08T10:00:00Z",
                "sessionId": "sess_rename", "parentUuid": None,
            }) + "\n")
            f.write(json.dumps({
                "type": "user", "uuid": "u02",
                "message": {"role": "user", "content": "second"},
                "timestamp": "2026-10-08T10:00:01Z",
                "sessionId": "sess_rename", "parentUuid": "u01",
            }) + "\n")

        vault_root = tmp_path / "vault"
        vault = VaultLayout(vault_root)
        conn = open_state_db(vault.state_db)
        scan_once(
            vault=vault, conn=conn,
            adapters=default_adapters(),
            roots={"claude-code": str(claude)},
        )
        old_id = _stable_source_id("claude-code", old_path)
        sf_before = SourceFileRepo(conn).get(old_id)
        gen_before = int(sf_before["generation"])
        cursor_before = int(sf_before["complete_offset"])
        events_before = conn.execute(
            "SELECT count(*) c FROM source_events"
        ).fetchone()["c"]
        assert events_before == 2

        # Rename to a new sibling path with the same stem; on the same
        # filesystem dev+ino survives, so we deliberately move the
        # file rather than rewrite it.
        new_path = proj / "sess_rename_v2.jsonl"
        old_path.rename(new_path)
        assert new_path.exists()
        assert not old_path.exists()

        # Append one message after the rename.
        with new_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "user", "uuid": "u03",
                "message": {"role": "user", "content": "after rename"},
                "timestamp": "2026-10-08T10:00:02Z",
                "sessionId": "sess_rename", "parentUuid": "u02",
            }) + "\n")

        r2 = scan_once(
            vault=vault, conn=conn,
            adapters=default_adapters(),
            roots={"claude-code": str(claude)},
        )

        # Exactly one source_files row for this session.
        sf_rows = conn.execute(
            "SELECT * FROM source_files WHERE session_id = ?",
            ("sess_rename",),
        ).fetchall()
        assert len(sf_rows) == 1, (
            "rename must reuse the existing source_files row, not "
            "create a duplicate"
        )
        sf_after = sf_rows[0]
        # Path has been updated to the new canonical path.
        assert sf_after["canonical_path"] == str(new_path)
        # Generation is preserved (no rotation); cursor advanced only
        # by the appended bytes.
        assert int(sf_after["generation"]) == gen_before
        assert int(sf_after["complete_offset"]) > cursor_before
        new_bytes = int(sf_after["complete_offset"]) - cursor_before
        # The appended record is one JSONL line of roughly 200 bytes;
        # assert it is small (not the whole original file).
        assert new_bytes < 400, (
            f"cursor must advance only by the appended bytes "
            f"(got {new_bytes})"
        )

        # No duplicate target — exactly one new event was committed.
        assert r2.new_events == 1
        rows = conn.execute(
            "SELECT event_id FROM source_events"
        ).fetchall()
        assert len(rows) == events_before + 1
        assert r2.rotations == 0
        assert r2.truncated == 0
        assert r2.middle_rewrites == 0

    def test_resolve_existing_source_id_does_not_match_same_stem_across_projects(
        self, tmp_path,
    ):
        """Two ``s1.jsonl`` basenames in *different* projects must stay
        independent: ``resolve_existing_source_id`` must NOT match a
        row whose ``file_identity`` (dev+ino) differs even when the
        session stem, branch, and connector are all equal.

        This is the negative companion to the rename test above and is
        the exact failure mode that motivated removing the
        ``by_stem`` fallback in ``resolve_existing_source_id``: two
        unrelated projects that happen to call their session file
        ``s1.jsonl`` would otherwise be silently merged into one
        source_files row, sharing a cursor and a generation across
        logically independent sessions.
        """
        claude = tmp_path / "claude"
        proj_a = claude / "projects" / "p_alpha"
        proj_b = claude / "projects" / "p_beta"
        proj_a.mkdir(parents=True)
        proj_b.mkdir(parents=True)
        path_a = proj_a / "s1.jsonl"
        path_b = proj_b / "s1.jsonl"
        for p in (path_a, path_b):
            with p.open("w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "type": "user", "uuid": f"u-{p.parent.name}",
                    "message": {"role": "user", "content": f"hello from {p.parent.name}"},
                    "timestamp": "2026-10-08T10:00:00Z",
                    "sessionId": "s1", "parentUuid": None,
                }) + "\n")

        vault_root = tmp_path / "vault"
        vault = VaultLayout(vault_root)
        conn = open_state_db(vault.state_db)

        # Scan ONLY project A. Project A's s1.jsonl is registered as
        # its own source; project B's file is on disk but never
        # scanned, so ``resolve_existing_source_id`` for ``path_b``
        # has no canonical_path row to find and must rely on the
        # dev+ino lookup. The broken ``by_stem`` fallback would
        # silently return project A's row here because the session
        # stem ``s1`` and branch ``main`` are identical.
        scan_once(
            vault=vault, conn=conn,
            adapters=default_adapters(),
            roots={"claude-code": str(proj_a)},
        )
        sf_rows = conn.execute(
            "SELECT * FROM source_files ORDER BY canonical_path"
        ).fetchall()
        assert len(sf_rows) == 1
        registered = sf_rows[0]
        assert registered["canonical_path"] == str(path_a)
        assert registered["session_id"] == "s1"
        assert registered["branch_id"] == "main"
        assert registered["file_identity"] != _file_identity(path_b.stat())

        # This is the precise behaviour the by_stem fallback used to
        # break: a different inode, same stem, same branch, same
        # connector — must NOT be merged into the existing source row.
        resolved = resolve_existing_source_id(
            conn=conn, connector="claude-code", path=path_b, stat=path_b.stat(),
        )
        assert resolved is None, (
            "different inode + same stem across projects must NOT be "
            "merged: resolve_existing_source_id must require a "
            "dev+ino match, not a session_stem match"
        )

        # And the canonical_path fast-path must still work for the
        # happy case (the existing path).
        resolved_a = resolve_existing_source_id(
            conn=conn, connector="claude-code", path=path_a, stat=path_a.stat(),
        )
        assert resolved_a is not None
        assert resolved_a["source_file_id"] == registered["source_file_id"]
        assert resolved_a["canonical_path"] == str(path_a)

        # A full scan of both projects must still register two
        # independent source rows.
        scan_once(
            vault=vault, conn=conn,
            adapters=default_adapters(),
            roots={"claude-code": str(claude)},
        )
        sf_rows_both = conn.execute(
            "SELECT * FROM source_files ORDER BY canonical_path"
        ).fetchall()
        assert len(sf_rows_both) == 2, (
            "two projects with identical session stems must produce "
            "two independent source_files rows"
        )
        ids = {r["source_file_id"] for r in sf_rows_both}
        assert len(ids) == 2, (
            "each project must own its own source_file_id, not a "
            "shared cursor"
        )
        identities = {r["file_identity"] for r in sf_rows_both}
        assert len(identities) == 2, (
            "each project file has a different inode; the rows must "
            "carry distinct file_identity values"
        )


# ---------------------------------------------------------------------------
# 3. Segment-written/DB-not-committed window must be safe
# ---------------------------------------------------------------------------


class TestSegmentCrashRecovery:

    def test_segment_written_db_not_committed_recovery_is_safe(self, tmp_path, monkeypatch):
        """Inject a failure inside ``scan_once`` between the durable
        segment write and the SQL commit. Reopen the vault and rescan.

        Contract:
          * the segment file written before the crash must NOT be left
            orphaned on disk (the cleanup path unlinks it);
          * the next scan must NOT advance the cursor past the
            uncommitted records;
          * the rescan must commit the same records exactly once, with
            no duplicate target.
        """
        from memory_agent.application import scan as scan_mod
        from memory_agent.infrastructure.sqlite.repositories import SegmentRepo

        claude = tmp_path / "claude"
        proj = claude / "projects" / "p1"
        proj.mkdir(parents=True)
        sess = proj / "sess_crash.jsonl"
        _write_claude_session(sess, [
            {"type": "user", "uuid": "u01",
             "message": {"role": "user", "content": "before crash"},
             "timestamp": "2026-10-08T10:00:00Z",
             "sessionId": "sess_crash", "parentUuid": None},
        ])

        vault_root = tmp_path / "vault"
        vault = VaultLayout(vault_root)
        conn = open_state_db(vault.state_db)

        # First scan is clean and consumes the record.
        scan_once(
            vault=vault, conn=conn,
            adapters=default_adapters(),
            roots={"claude-code": str(claude)},
        )
        cursor_after_clean = int(conn.execute(
            "SELECT complete_offset FROM source_files LIMIT 1"
        ).fetchone()["complete_offset"])
        events_after_clean = conn.execute(
            "SELECT count(*) c FROM source_events"
        ).fetchone()["c"]
        assert events_after_clean == 1

        # Append a new record so the next scan has something to fail on.
        with sess.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "user", "uuid": "u02",
                "message": {"role": "user", "content": "will crash"},
                "timestamp": "2026-10-08T10:00:01Z",
                "sessionId": "sess_crash", "parentUuid": "u01",
            }) + "\n")

        # Inject a fault the first time SegmentRepo.insert is called;
        # the segment file has already been written at this point so we
        # are inside the durable-write/DB-commit window.
        orig_insert = SegmentRepo.insert
        crash_seen = {"n": 0}

        def boom(self, **_kwargs):
            crash_seen["n"] += 1
            raise RuntimeError("simulated segment-row insert failure")

        monkeypatch.setattr(SegmentRepo, "insert", boom)
        # We also need to make sure transaction() does not swallow the
        # exception; the existing wrapper re-raises after rollback.
        scan_once(
            vault=vault, conn=conn,
            adapters=default_adapters(),
            roots={"claude-code": str(claude)},
        )
        assert crash_seen["n"] >= 1

        # The orphan segment file from the failed run must be cleaned
        # up by the failure branch — never two segments pointing at the
        # same generation. Re-open the connection (simulating a real
        # crash recovery) to confirm durable state is consistent.
        conn.close()
        seg_files = list(vault.segments_dir.glob("*.json"))
        assert len(seg_files) == 1, (
            f"expected one segment on disk after failed commit; got {seg_files}"
        )

        conn2 = open_state_db(vault.state_db)
        cursor_after_crash = int(conn2.execute(
            "SELECT complete_offset FROM source_files LIMIT 1"
        ).fetchone()["complete_offset"])
        assert cursor_after_crash == cursor_after_clean, (
            "cursor must not advance when the SQL commit failed"
        )
        events_after_crash = conn2.execute(
            "SELECT count(*) c FROM source_events"
        ).fetchone()["c"]
        assert events_after_crash == events_after_clean
        segments_after_crash = conn2.execute(
            "SELECT count(*) c FROM segments"
        ).fetchone()["c"]
        assert segments_after_crash == 1

        # Recovery scan (no fault injected) commits the missing record
        # exactly once. Restore the real SegmentRepo.insert.
        monkeypatch.setattr(SegmentRepo, "insert", orig_insert)
        r_recover = scan_once(
            vault=vault, conn=conn2,
            adapters=default_adapters(),
            roots={"claude-code": str(claude)},
        )
        assert r_recover.new_events == 1
        final_events = conn2.execute(
            "SELECT count(*) c FROM source_events"
        ).fetchone()["c"]
        assert final_events == events_after_clean + 1
        # Exactly two segments on disk: the original and the recovery
        # append. No orphans, no duplicates.
        seg_files = sorted(vault.segments_dir.glob("*.json"))
        assert len(seg_files) == 2
        final_cursor = int(conn2.execute(
            "SELECT complete_offset FROM source_files LIMIT 1"
        ).fetchone()["complete_offset"])
        assert final_cursor > cursor_after_clean


# ---------------------------------------------------------------------------
# 4. Backfill must iterate complete windows
# ---------------------------------------------------------------------------


class TestBackfillWindowIteration:

    def test_backfill_iterates_windows_and_marks_partial_tail(self, tmp_path):
        """Build a file larger than two ``max_bytes_per_file`` windows
        and put a target event in the *final* page. The backfill must
        walk past the first window and produce exactly that target
        event. A trailing partial UTF-8 sequence must mark the run as
        ``partial``. Re-running the same range must be idempotent.
        """
        claude = tmp_path / "claude"
        proj = claude / "projects" / "p1"
        proj.mkdir(parents=True)
        sess = proj / "sess_backfill.jsonl"
        # Build a line of ~250 bytes so the file is just over two
        # 1000-byte windows.
        payload = "X" * 220
        lines: list[dict] = []
        # 12 lines => > 3000 bytes, plenty for two 1000-byte windows.
        for i in range(12):
            lines.append({
                "type": "user", "uuid": f"u{i:02d}",
                "message": {"role": "user", "content": f"{payload}_{i}"},
                "timestamp": f"2026-10-08T10:{i:02d}:00Z",
                "sessionId": "sess_backfill",
                "parentUuid": (None if i == 0 else f"u{i-1:02d}"),
            })
        _write_claude_session(sess, lines)
        assert sess.stat().st_size > 2000

        vault_root = tmp_path / "vault"
        vault = VaultLayout(vault_root)
        conn = open_state_db(vault.state_db)

        max_bytes = 1000
        r1 = scan_once(
            vault=vault, conn=conn,
            adapters=default_adapters(),
            roots={"claude-code": str(claude)},
            mode="backfill",
            since_iso="2026-10-08T00:00:00Z",
            until_iso="2026-10-08T23:59:59Z",
            timezone_name="UTC",
            max_bytes_per_file=max_bytes,
        )
        # Every complete record is in-window.
        assert r1.target_events == 12
        # All 12 in-window records made it into source_events.
        n = conn.execute(
            "SELECT count(*) c FROM source_events"
        ).fetchone()["c"]
        assert n == 12
        # The backfill record reflects multi-window coverage.
        run = BackfillRunRepo(conn).get(r1.backfill_id)
        assert run["counters"]["files_seen"] == 1
        assert run["counters"]["target_events"] == 12
        assert run["status"] in ("success", "partial")

        # Append a complete in-window record AFTER the first scan so
        # the next scan has more to ingest. Then add a trailing partial
        # UTF-8 character so the run is marked partial. We append the
        # complete line in text mode then switch to binary mode for
        # the trailing partial bytes so the file ends with a clean
        # newline followed by 2 bytes of an invalid UTF-8 sequence.
        with sess.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "user", "uuid": "u13",
                "message": {"role": "user", "content": "tail_event"},
                "timestamp": "2026-10-08T11:00:00Z",
                "sessionId": "sess_backfill", "parentUuid": "u11",
            }) + "\n")
            f.flush()
        with sess.open("ab") as f:
            # Half UTF-8 character: 0xe4 0xb8 (start of "中").
            f.write(b"\xe4\xb8")
        assert sess.stat().st_size > 4700

        r2 = scan_once(
            vault=vault, conn=conn,
            adapters=default_adapters(),
            roots={"claude-code": str(claude)},
            mode="backfill",
            since_iso="2026-10-08T00:00:00Z",
            until_iso="2026-10-08T23:59:59Z",
            timezone_name="UTC",
            max_bytes_per_file=max_bytes,
        )
        # The complete tail event is committed.
        assert r2.new_events >= 1
        assert r2.backfill_partial is True
        run2 = BackfillRunRepo(conn).get(r2.backfill_id)
        assert run2["status"] == "partial"
        # No duplicate target: the 12 original events stay counted
        # exactly once, the tail event shows up as +1.
        n_after_tail = conn.execute(
            "SELECT count(*) c FROM source_events"
        ).fetchone()["c"]
        assert n_after_tail == 13

        # Re-run over the same range is idempotent: no new events.
        r3 = scan_once(
            vault=vault, conn=conn,
            adapters=default_adapters(),
            roots={"claude-code": str(claude)},
            mode="backfill",
            since_iso="2026-10-08T00:00:00Z",
            until_iso="2026-10-08T23:59:59Z",
            timezone_name="UTC",
            max_bytes_per_file=max_bytes,
        )
        assert r3.new_events == 0
        assert r3.target_events == 0
        n_after_idem = conn.execute(
            "SELECT count(*) c FROM source_events"
        ).fetchone()["c"]
        assert n_after_idem == 13


# ---------------------------------------------------------------------------
# 5. context_only single-assignment + total token bound
# ---------------------------------------------------------------------------


class TestPlannerContextOnlyAndBudget:

    def test_context_only_events_never_active_and_total_within_policy(self, tmp_path):
        """Three classes of guarantee:

          * context_only rows are never active targets;
          * every user turn / fragment is active at most once across
            the whole plan;
          * the total of (schema overhead + context prefix + context
            rows + targets) never exceeds ``policy.max_input_tokens``.
        """
        vault_root = tmp_path / "vault"
        vault = VaultLayout(vault_root)
        conn = open_state_db(vault.state_db)

        # Mix of user / assistant / tool events; one tool event is
        # interleaved so it must NOT become an active target.
        events: list[ConversationEvent] = []
        for i in range(7):
            events.append(_make_event(
                eid=f"u{i:02d}", role="user",
                text=f"turn {i} " + "filler " * 30, ordinal=i * 3 + 1,
                source_time=f"2026-10-08T10:0{i}:00Z",
            ))
            events.append(_make_event(
                eid=f"a{i:02d}", role="assistant",
                text=f"reply {i}", ordinal=i * 3 + 2, parent=f"u{i:02d}",
                source_time=f"2026-10-08T10:0{i}:01Z",
            ))
            events.append(_make_event(
                eid=f"t{i:02d}", role="tool",
                text=f"tool out {i}", ordinal=i * 3 + 3, parent=f"a{i:02d}",
                source_time=f"2026-10-08T10:0{i}:02Z",
            ))

        policy = BatchPolicyInput(
            max_turns_per_batch=5,
            max_input_tokens=6000,
            schema_overhead_tokens=SCHEMA_OVERHEAD_TOKENS,
            context_prefix_tokens=CONTEXT_PREFIX_TOKENS,
        )
        planned = plan_batches_for_session(
            session_id="sess_b", branch_id="main",
            events=events, policy=policy, conn=conn,
        )
        assert planned, "planner must produce at least one batch"

        # --- (1) context_only never becomes an active target --------
        for batch in planned:
            for member in batch["members"]:
                if member["purpose"] == "context_only":
                    assert member["active_target"] == 0, (
                        f"context_only member {member['turn_id']} "
                        f"must not be marked active"
                    )

        # Tool / assistant event_ids must never appear in
        # ``active_targets``.
        active_event_ids = {
            t["event_id"]
            for b in planned for t in b["active_targets"]
        }
        for ev in events:
            if ev.role != "user":
                assert ev.event_id not in active_event_ids, (
                    f"{ev.role} event {ev.event_id} must not be active"
                )

        # --- (2) every user turn / fragment is active at most once --
        assert len(active_event_ids) == len(set(active_event_ids))
        # The 7 user turns should all be active exactly once.
        assert len(active_event_ids) == 7

        # --- (3) total budget never exceeds the policy cap ----------
        cap = policy.max_input_tokens
        for batch in planned:
            members = batch["members"]
            overhead = policy.schema_overhead_tokens + policy.context_prefix_tokens
            token_total = overhead
            for m in members:
                # Reconstruct the per-member token estimate from the
                # preview bytes (matches events_to_turn_samples).
                preview = m.get("text_preview") or ""
                est = heuristic_tokens(preview) + 32
                token_total += est
            assert token_total <= cap, (
                f"batch {batch['batch']['batch_id']} exceeded budget: "
                f"{token_total} > {cap}"
            )

    def test_no_active_target_for_tool_records_in_overlapping_batches(self, tmp_path):
        """Even when a session has alternating user + tool records,
        every active_target event_id is a user event and the unique
        index on ``batch_members(unit_id, epoch) WHERE active_target=1``
        is never violated (no double-active target per unit).
        """
        vault_root = tmp_path / "vault"
        vault = VaultLayout(vault_root)
        conn = open_state_db(vault.state_db)

        events: list[ConversationEvent] = []
        for i in range(15):
            events.append(_make_event(
                eid=f"u{i:02d}", role="user",
                text=f"turn {i}", ordinal=i * 2 + 1,
            ))
            events.append(_make_event(
                eid=f"t{i:02d}", role="tool",
                text=f"tool out {i}", ordinal=i * 2 + 2, parent=f"u{i:02d}",
            ))

        planned = plan_batches_for_session(
            session_id="sess_b", branch_id="main",
            events=events,
            policy=BatchPolicyInput(
                max_turns_per_batch=5,
                max_input_tokens=6000,
                schema_overhead_tokens=SCHEMA_OVERHEAD_TOKENS,
                context_prefix_tokens=CONTEXT_PREFIX_TOKENS,
            ),
            conn=conn,
        )
        # 15 user turns -> 3 batches (5 / 5 / 5).
        assert len(planned) == 3
        target_event_ids: list[str] = []
        for b in planned:
            for t in b["active_targets"]:
                target_event_ids.append(t["event_id"])
        assert len(target_event_ids) == 15
        assert len(set(target_event_ids)) == 15
        # All active targets are user events.
        for ev in events:
            if ev.role == "tool":
                assert ev.event_id not in set(target_event_ids)


# ---------------------------------------------------------------------------
# Repo-root hygiene: phase B repair tests must not materialise a vault
# in the repository root.
# ---------------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parents[2]
VAULT_REPR_PREFIX = "<memory_agent.infrastructure.files.VaultLayout object at "


class TestRepoRootHygiene:
    """Phase B repair tests run from the repository root. A past
    version of one of them passed a ``VaultLayout`` instance where a
    ``Path`` was expected and the implicit ``str(vault)`` turned into
    ``<memory_agent.infrastructure.files.VaultLayout object at
    0x…>`` — which the OS accepted as a literal directory name, so the
    VaultLayout constructor cheerfully wrote ``state/state.sqlite`` and
    ``config.toml`` inside it under CWD. The two resulting directories
    ended up checked in next to ``src/`` and ``tests/``.

    The guard below walks the repository root before and after this
    file's tests run and refuses to leave a vault there. Both the
    directory-name symptom (the literal ``VaultLayout object at``
    prefix) and any new ``state.sqlite`` / ``config.toml`` / ``vault``
    entry directly under the repo root are caught.
    """

    def test_phase_b_repair_tests_do_not_create_vault_in_repo_root(self, tmp_path):
        """A vault-shaped artifact under the repo root is a bug. The
        test builds a real vault under ``tmp_path`` (so the suite has
        somewhere to write), invokes the helpers used elsewhere in
        this file, and then asserts the repo root is clean.
        """
        repo_root = REPO_ROOT
        # Sanity: the repo root really is the parent of ``tests``.
        assert (repo_root / "tests").is_dir()
        assert (repo_root / "src").is_dir()

        # Build a real vault off-tree; this is the only place a vault
        # should appear while this test runs.
        claude = tmp_path / "claude"
        proj = claude / "projects" / "p_hyg"
        proj.mkdir(parents=True)
        sess = proj / "sess_hyg.jsonl"
        with sess.open("w", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "user", "uuid": "u01",
                "message": {"role": "user", "content": "hygiene check"},
                "timestamp": "2026-10-08T10:00:00Z",
                "sessionId": "sess_hyg", "parentUuid": None,
            }) + "\n")
        vault_root = tmp_path / "vault"
        vault = VaultLayout(vault_root)
        conn = open_state_db(vault.state_db)
        scan_once(
            vault=vault, conn=conn,
            adapters=default_adapters(),
            roots={"claude-code": str(claude)},
        )

        # The repo root must contain no vault-shaped entries, neither
        # the symptom directory (literal VaultLayout repr) nor any
        # generic stray ``state.sqlite`` / ``config.toml`` / ``vault``
        # that this file would have created.
        bad_entries: list[str] = []
        for entry in repo_root.iterdir():
            name = entry.name
            if name.startswith(VAULT_REPR_PREFIX):
                bad_entries.append(f"{name}/  (VaultLayout repr leaked to disk)")
                continue
            # Only flag a same-name ``state.sqlite`` / ``config.toml``
            # / ``vault`` directly under the repo root, never under
            # legitimate sub-trees like ``tests/`` or ``.pytest_cache``.
            if name in {"state.sqlite", "config.toml", "vault"}:
                bad_entries.append(f"{name}  ({'file' if entry.is_file() else 'dir'})")
        assert not bad_entries, (
            "phase B repair tests must not materialise vault artifacts "
            f"under {repo_root}; found: {bad_entries}"
        )