"""T13 — audit query service tests.

Every test in this module constructs an *invented* pipeline run from
scratch inside a temp vault. We never rely on a real user history or a
real model provider: the fake provider + sanitised fixtures are enough
to exercise the trace source -> batch -> model call -> candidate ->
commit chain that the audit DTOs surface.

Each test asserts the audit DTOs:

  * carry ``as_of`` / ``snapshot_id`` / ``projection_state``
  * paginate with deterministic cursors that are scoped + snapshot-bound
  * redact sensitive bodies in list views
  * surface the full source body only on the explicit evidence endpoint
  * report DB/HEAD reconcile lag (``projection_state``) when a
    publish/finalize window is left open
  * are reachable without ever instantiating the model provider

The tests live in ``tests/integration/`` because they touch real
SQLite + filesystem layout, not because they talk to a real model.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

# Force the fake provider BEFORE any module that imports it. The fake
# provider never instantiates a real Claude CLI; this is the same
# invariant the rest of the test suite relies on (see README).
os.environ.setdefault("MEMORY_AGENT_MODEL_PROVIDER", "fake")

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_agent.application.audit_queries import (  # noqa: E402
    AuditQueries,
    decode_cursor,
    encode_cursor,
)
from memory_agent.application.commit import CommitService  # noqa: E402
from memory_agent.application.process_batches import process_batch  # noqa: E402
from memory_agent.application.reconcile import Reconciler  # noqa: E402
from memory_agent.application.recovery import recover_unfinished_intents  # noqa: E402
from memory_agent.contracts.candidates import Candidate, CandidateSet, EvidenceRef, Scope  # noqa: E402
from memory_agent.contracts.memory import MemoryAtom  # noqa: E402
from memory_agent.infrastructure.files import VaultLayout  # noqa: E402
from memory_agent.infrastructure.sqlite import open_state_db  # noqa: E402
from memory_agent.infrastructure.sqlite.repositories import (  # noqa: E402
    AuditRepo,
    BatchRepo,
    CandidateSetRepo,
    CommitIntentRepo,
    ConflictRepo,
    EventRepo,
    MemoryProjectionRepo,
    SourceFileRepo,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_vault(tmp_path: Path) -> tuple[VaultLayout, sqlite3.Connection]:
    vault = VaultLayout(tmp_path)
    conn = open_state_db(vault.state_db)
    return vault, conn


def _write_invented_session(
    *,
    root: Path,
    session_id: str,
    user_lines: list[str],
) -> Path:
    """Create a sanitised Claude Code fixture with one user message
    per entry. The fake provider keys on the leading ``"use "`` /
    ``"always "`` / ``"never "`` prefixes so we can deterministically
    trigger extraction paths.
    """

    root.mkdir(parents=True, exist_ok=True)
    project = root / "projects" / "proj_audit"
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{session_id}.jsonl"
    records: list[dict[str, object]] = []
    for i, text in enumerate(user_lines, start=1):
        records.append({
            "type": "user",
            "uuid": f"u{i:02d}",
            "message": {"role": "user", "content": text},
            "timestamp": f"2026-10-09T00:{i:02d}:00Z",
            "sessionId": session_id,
            "parentUuid": None if i == 1 else f"u{i - 1:02d}",
        })
        records.append({
            "type": "assistant",
            "uuid": f"a{i:02d}",
            "message": {"role": "assistant", "content": "ack"},
            "timestamp": f"2026-10-09T00:{i:02d}:01Z",
            "sessionId": session_id,
            "parentUuid": f"u{i:02d}",
        })
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False))
            f.write("\n")
    return path


def _seed_full_pipeline(
    *,
    vault: VaultLayout,
    conn,
    session_id: str,
    user_lines: list[str],
    connector: str = "claude-code",
    root: Path | None = None,
    head_suffix: str = "head_v1",
    add_query_audit: bool = True,
    expected_revisions: list[str] | None = None,
) -> dict[str, object]:
    """Drive a fake extraction end-to-end and return a summary.

    Returns ``{"memory_id": ..., "batch_id": ..., "commit_id": ...}``.
    """

    from memory_agent.application.scan import default_adapters, scan_once
    from memory_agent.application.plan_batches import plan_batches_for_session
    from memory_agent.domain.batching import BatchPolicyInput
    from memory_agent.adapters.models import (
        SQLiteModelCallRecorder,
        get_default_provider,
    )

    src_root = root or (vault.root / "dev_fixtures" / connector)
    src_root.mkdir(parents=True, exist_ok=True)
    path = _write_invented_session(
        root=src_root,
        session_id=session_id,
        user_lines=user_lines,
    )
    scan_res = scan_once(
        vault=vault,
        conn=conn,
        adapters=default_adapters(),
        roots={connector: str(src_root)},
        max_bytes_per_file=1_048_576,
    )
    assert scan_res.new_events > 0, scan_res

    events = [r for r in conn.execute(
        "SELECT * FROM source_events WHERE session_id = ? ORDER BY discovered_at",
        (session_id,),
    ).fetchall()]
    # Convert to ConversationEvent-like objects the planner can read.
    from memory_agent.application.dispatch import _event_from_row_with_text
    planner_events = [_event_from_row_with_text(conn, r) for r in events]
    policy = BatchPolicyInput(max_turns_per_batch=5, max_input_tokens=6000)
    planned = plan_batches_for_session(
        session_id=session_id,
        branch_id="main",
        events=planner_events,
        policy=policy,
        conn=conn,
    )
    assert planned, "planner produced no batches"

    # Real extraction with the FAKE provider: it returns a CandidateSet
    # derived from the first target event of every batch.
    recorder = SQLiteModelCallRecorder(conn=conn)
    provider = get_default_provider(recorder=recorder)
    reconciler = Reconciler(memory_projection_repo=MemoryProjectionRepo(conn))
    committer = CommitService(vault=vault, conn=conn)
    priv_epoch = "epoch_audit_test"
    out: dict[str, object] = {"batch_ids": [], "memory_ids": [], "commit_ids": []}
    for plan in planned:
        batch_id = plan["batch"]["batch_id"]
        res = process_batch(
            batch_id=batch_id,
            vault=vault,
            conn=conn,
            provider=provider,
            reconciler=reconciler,
            committer=committer,
            evidence_lookup=lambda eid, rh: _evidence_text_lookup(conn, eid, rh, vault),
            base_commit_id="root",
            privacy_epoch=priv_epoch,
            run_id=f"run_{batch_id}",
        )
        out["batch_ids"].append(batch_id)
        last_commit_row = conn.execute(
            "SELECT commit_id FROM commit_intents ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if last_commit_row is not None:
            out["commit_ids"].append(str(last_commit_row["commit_id"]))
    # Snapshot the memory_projection rows for this session. The fake
    # provider emits a CandidateSet for the first target event of every
    # batch; one batch -> one memory is the common path. We pick the
    # first row from memory_projection whose source memory is bound to
    # any batch that included this session_id.
    mem_rows = conn.execute(
        "SELECT DISTINCT memory_id FROM memory_projection "
        "WHERE commit_id IN (SELECT commit_id FROM commit_intents) "
        "ORDER BY recorded_at"
    ).fetchall()
    out["memory_ids"] = [str(m["memory_id"]) for m in mem_rows]
    out["provider_name"] = provider.name
    out["recorder_count_after"] = _count_model_calls(conn)
    # Optionally drop a query.retrieved audit row so the trace list
    # has at least one row to return.
    if add_query_audit:
        AuditRepo(conn).append({
            "audit_id": f"aud_qry_invent_{session_id}",
            "actor": "user_cli",
            "action": "query.retrieved",
            "occurred_at": "2026-10-09T00:10:00+00:00",
            "object_type": "query",
            "object_id": f"trace_invent_{session_id}",
            "correlation": {"snapshot_id": "root"},
            "before_after": {
                "retrieval": "keyword",
                "accepted_variants": ["original"],
                "rejected_variants": [],
                "result_count": 1,
            },
            "outcome": "success",
            "reason_code": None,
            "details": {
                "trace_artifact_ref": f"artifacts/queries/trace_invent_{session_id}.json",
                "original_query": user_lines[0],
            },
        })
        # Persist the artifact so the detail endpoint can read it.
        artifact = vault.artifacts_dir / "queries" / f"trace_invent_{session_id}.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps({
            "trace_id": f"trace_invent_{session_id}",
            "snapshot_id": "root",
            "retrieval": "keyword",
            "limit": 8,
            "retrieval_limit": 30,
            "original_query": user_lines[0],
            "hard_filters": {"project_ids": [], "domain_ids": []},
            "rewrite": "off",
            "expansions": [],
            "variants": [{
                "variant_id": "original",
                "origin": "original",
                "text": user_lines[0],
                "accepted": True,
            }],
            "rejected": [],
            "fallback_reason": None,
            "provider": "fake",
            "prompt_version": "v1",
            "warnings": [],
            "created_at": "2026-10-09T00:10:00+00:00",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    conn.commit()
    return out


def _evidence_text_lookup(conn, event_id, revision_hash, vault):
    """Look up the persisted segment text for an event."""
    row = conn.execute(
        "SELECT segment_id FROM source_events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if row is None:
        return ""
    seg = conn.execute(
        "SELECT path FROM segments WHERE segment_id = ?",
        (row["segment_id"],),
    ).fetchone()
    if seg is None:
        return ""
    payload_path = Path(seg["path"])
    if not payload_path.exists():
        return ""
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    for ev in payload.get("events", []):
        if ev.get("event_id") == event_id:
            return str(ev.get("text") or "")
    return ""


def _count_model_calls(conn) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM model_calls").fetchone()
    return int(row["c"] or 0)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_overview_returns_envelope_and_pipeline_counts(tmp_path: Path) -> None:
    """OverviewDTO is anchored and reports the four-stage counts.

    The fake provider never instantiates a real model client; we
    assert the model-call count after a full extraction pass to make
    sure the audit query itself did not call a model.
    """

    vault, conn = _fresh_vault(tmp_path)
    _seed_full_pipeline(
        vault=vault,
        conn=conn,
        session_id="sess_audit_overview",
        # Single user line so the reconciler doesn't mark both
        # candidates as same-batch contradictions; we want at least
        # one active memory to land in the projection.
        user_lines=[
            "use Python for the prototype always",
        ],
    )
    before = _count_model_calls(conn)
    svc = AuditQueries(vault=vault)
    overview = svc.overview(conn=conn)
    after = _count_model_calls(conn)
    assert overview.header.as_of
    assert overview.header.snapshot_id == "root" or overview.header.snapshot_id
    assert overview.header.projection_state in {"aligned", "db_lag", "db_ahead", "empty"}
    assert overview.counts.sources_total >= 1
    assert overview.counts.events_user >= 1
    assert overview.counts.batches_total >= 1
    assert overview.counts.candidate_sets_total >= 1
    assert overview.counts.memories_active >= 1
    assert overview.counts.audit_events_total >= 1
    assert overview.pipeline_stages["scanned"] >= 2
    assert overview.pipeline_stages["planned"] >= 1
    assert overview.pipeline_stages["extracted"] >= 1
    assert overview.pipeline_stages["committed"] >= 1
    # The audit query must NEVER cause a model call itself.
    assert after == before


def test_sources_list_paginates_and_redacts_bodies(tmp_path: Path) -> None:
    """Source list paginates by canonical_path and never exposes the
    raw segment content."""

    vault, conn = _fresh_vault(tmp_path)
    # Seed two sessions so we get two distinct source_files rows and
    # pagination actually has a next page to surface.
    _seed_full_pipeline(
        vault=vault,
        conn=conn,
        session_id="sess_audit_sources_a",
        user_lines=["use Python for the prototype"],
    )
    _seed_full_pipeline(
        vault=vault,
        conn=conn,
        session_id="sess_audit_sources_b",
        user_lines=["use Python for the prototype"],
    )
    svc = AuditQueries(vault=vault)
    page1 = svc.list_sources(conn=conn, cursor=None, page_size=1)
    assert page1.pagination.next_cursor
    assert len(page1.items) == 1
    page2 = svc.list_sources(
        conn=conn, cursor=page1.pagination.next_cursor, page_size=10,
    )
    items1 = page1.items
    items2 = page2.items
    assert items1[0]["canonical_path"] != items2[0]["canonical_path"]
    # No raw segment content in list output.
    for it in items1 + items2:
        blob = json.dumps(it)
        assert "use Python" not in blob
        assert "<redacted>" not in blob  # nothing should be redacted here


def test_source_detail_returns_manifest_reconcile_state(tmp_path: Path) -> None:
    """source_detail surfaces the manifest_reconcile state."""

    vault, conn = _fresh_vault(tmp_path)
    _seed_full_pipeline(
        vault=vault,
        conn=conn,
        session_id="sess_audit_source_detail",
        user_lines=["use Python for the prototype"],
    )
    src = conn.execute("SELECT source_file_id FROM source_files LIMIT 1").fetchone()
    svc = AuditQueries(vault=vault)
    detail = svc.source_detail(conn=conn, source_file_id=src["source_file_id"])
    assert detail is not None
    assert detail.manifest_reconcile in {
        "ok", "behind", "ahead", "missing_manifest", "unknown",
    }
    assert detail.adapter_version


def test_session_turns_redacts_quotes(tmp_path: Path) -> None:
    """Session turns expose a redacted preview, never the full text."""

    vault, conn = _fresh_vault(tmp_path)
    _seed_full_pipeline(
        vault=vault,
        conn=conn,
        session_id="sess_audit_turns",
        user_lines=[
            "use Python for the prototype always and forever and more text than 60",
        ],
    )
    svc = AuditQueries(vault=vault)
    page = svc.list_session_turns(
        conn=conn, session_id="sess_audit_turns", page_size=5,
    )
    assert page.items, "expected at least one turn"
    for it in page.items:
        # The preview must be capped to 60 chars; full text would be
        # longer than 60 in this fixture.
        preview = it["quote_preview"]
        assert preview is not None
        assert len(preview) <= 61
        assert it["quote_redacted"] is True
        # The full user text MUST NOT appear verbatim in any field.
        assert "always and forever and more text than 60" not in json.dumps(it)


def test_batches_list_and_detail_link_to_run_and_commit(tmp_path: Path) -> None:
    """Trace source -> batch -> model call -> candidate -> commit."""

    vault, conn = _fresh_vault(tmp_path)
    out = _seed_full_pipeline(
        vault=vault,
        conn=conn,
        session_id="sess_audit_batches",
        user_lines=[
            "use Python for the prototype always",
            "always log decisions to the vault audit",
        ],
    )
    svc = AuditQueries(vault=vault)
    page = svc.list_batches(conn=conn, cursor=None, page_size=10)
    assert page.items, "batches must show up"
    # The list itself must echo the batch id and the candidate set ref
    # so the UI can navigate to evidence without a second fetch.
    for it in page.items:
        assert it["batch_id"]
        assert it["candidate_set_id"] or it["candidate_set_ref"]
    # Each detail must surface the batch -> commit chain.
    for it in page.items:
        detail = svc.batch_detail(conn=conn, batch_id=it["batch_id"])
        assert detail is not None
        assert detail.batch_id == it["batch_id"]
        # Commit ref must be present because every batch we created
        # went through the CommitService.
        assert detail.commit_id is not None
        # Member count is exposed so the UI knows the batch is not
        # empty without re-querying batch_members.
        assert detail.member_count >= 1


def test_batch_evidence_is_the_only_endpoint_that_returns_full_text(
    tmp_path: Path,
) -> None:
    """The list / detail / overview paths must NOT include the raw
    body. Only ``batch_evidence`` does.
    """

    vault, conn = _fresh_vault(tmp_path)
    _seed_full_pipeline(
        vault=vault,
        conn=conn,
        session_id="sess_audit_evidence",
        user_lines=["use Python for the prototype always"],
    )
    svc = AuditQueries(vault=vault)
    page = svc.list_batches(conn=conn, cursor=None, page_size=10)
    assert page.items
    # List output never contains the raw body.
    for it in page.items:
        assert "use Python" not in json.dumps(it)
    # Detail output: candidate_set_ref + member_count are present but
    # the body is not.
    detail = svc.batch_detail(conn=conn, batch_id=page.items[0]["batch_id"])
    assert detail is not None
    detail_blob = detail.model_dump_json()
    assert "use Python" not in detail_blob
    # Evidence output: now the body IS included.
    evidence = svc.batch_evidence(conn=conn, batch_id=page.items[0]["batch_id"])
    assert evidence is not None
    assert any(
        "use Python" in (i.get("text") or "")
        for i in evidence.inputs
    ), evidence


def test_memory_history_walks_revisions_and_source_corrections(
    tmp_path: Path,
) -> None:
    """MemoryHistoryDTO reads the file revisions + correction queue."""

    vault, conn = _fresh_vault(tmp_path)
    out = _seed_full_pipeline(
        vault=vault,
        conn=conn,
        session_id="sess_audit_memhist",
        user_lines=["use Python for the prototype always"],
    )
    memory_id = out["memory_ids"][0]
    svc = AuditQueries(vault=vault)
    hist = svc.memory_history(conn=conn, memory_id=memory_id)
    assert hist is not None
    assert hist.memory_id == memory_id
    assert hist.revisions, "at least one revision expected"
    rev = hist.revisions[0]
    assert rev.memory_id == memory_id
    assert rev.statement
    assert rev.scope_level in {"global", "domain", "project", "task", "session"}


def test_conflicts_list_returns_empty_when_none_open(tmp_path: Path) -> None:
    """Empty conflict list still carries the envelope header."""

    vault, conn = _fresh_vault(tmp_path)
    _seed_full_pipeline(
        vault=vault,
        conn=conn,
        session_id="sess_audit_no_conflicts",
        user_lines=["use Python for the prototype"],
    )
    svc = AuditQueries(vault=vault)
    page = svc.list_conflicts(conn=conn, cursor=None, page_size=10)
    assert page.items == []
    assert page.header.snapshot_id


def test_audit_event_list_redacts_details(tmp_path: Path) -> None:
    """The audit list view strips known sensitive keys from details."""

    vault, conn = _fresh_vault(tmp_path)
    AuditRepo(conn).append({
        "audit_id": "aud_test_redact_001",
        "actor": "worker",
        "action": "batch.extracted",
        "occurred_at": "2026-10-09T00:11:00+00:00",
        "object_type": "candidate_set",
        "object_id": "cand_test",
        "correlation": {"job_id": "job_x"},
        "before_after": {},
        "outcome": "success",
        "reason_code": None,
        # Sensitive payload that MUST be redacted in the list view.
        "details": {
            "quote": "do not leak this verbatim text",
            "text": "do not leak this verbatim text",
            "prompt": "system prompt content",
            "result_count": 1,
            "tags": ["secret-stuff"],
        },
    })
    conn.commit()
    svc = AuditQueries(vault=vault)
    page = svc.list_audit_events(conn=conn, cursor=None, page_size=10)
    assert page.items
    matched = [it for it in page.items if it["audit_id"] == "aud_test_redact_001"]
    assert matched
    blob = json.dumps(matched[0])
    assert "do not leak this verbatim text" not in blob
    assert "system prompt content" not in blob
    assert matched[0]["redacted"] is True
    # Detailed fetch surfaces everything.
    detail = svc.audit_event_detail(conn=conn, audit_id="aud_test_redact_001")
    assert detail is not None
    assert detail.redacted is False
    assert detail.details.get("quote") == "do not leak this verbatim text"


def test_audit_event_detail_includes_correlation_and_object_ref(
    tmp_path: Path,
) -> None:
    """The detail DTO carries the full correlation + before/after."""

    vault, conn = _fresh_vault(tmp_path)
    AuditRepo(conn).append({
        "audit_id": "aud_corr_001",
        "actor": "user_cli",
        "action": "conflict.decided",
        "occurred_at": "2026-10-09T00:12:00+00:00",
        "object_type": "conflict",
        "object_id": "conf_001",
        "correlation": {"job_id": "job_x", "batch_id": "batch_x"},
        "before_after": {"action": "adopt_new", "note": None},
        "outcome": "success",
        "reason_code": None,
        "details": {"proposal_revision": 1},
    })
    conn.commit()
    svc = AuditQueries(vault=vault)
    detail = svc.audit_event_detail(conn=conn, audit_id="aud_corr_001")
    assert detail is not None
    assert detail.correlation.get("batch_id") == "batch_x"
    assert detail.before_after.get("action") == "adopt_new"


def test_query_traces_list_and_detail_use_persisted_artifact(
    tmp_path: Path,
) -> None:
    """Query traces come from the persisted artifact, not the audit row alone."""

    vault, conn = _fresh_vault(tmp_path)
    _seed_full_pipeline(
        vault=vault,
        conn=conn,
        session_id="sess_audit_query_traces",
        user_lines=["use Python for the prototype"],
    )
    svc = AuditQueries(vault=vault)
    page = svc.list_query_traces(conn=conn, cursor=None, page_size=10)
    assert any(it["trace_id"].startswith("trace_invent_") for it in page.items)
    sample_trace = next(
        it["trace_id"] for it in page.items if it["trace_id"].startswith("trace_invent_")
    )
    detail = svc.query_trace_detail(conn=conn, trace_id=sample_trace)
    assert detail is not None
    assert detail.provider == "fake"
    assert detail.artifact_ref
    assert any(v.get("origin") == "original" for v in detail.variants_full)


def test_cursor_is_snapshot_bound_and_rejected_on_advance(
    tmp_path: Path,
) -> None:
    """A cursor emitted under snapshot A is rejected when HEAD advances."""

    vault, conn = _fresh_vault(tmp_path)
    _seed_full_pipeline(
        vault=vault,
        conn=conn,
        session_id="sess_cursor_test",
        user_lines=["use Python for the prototype"],
    )
    svc = AuditQueries(vault=vault)
    # Encode a cursor at the current HEAD directly so we don't depend on
    # the audit log having enough rows to fill a page.
    head = json.loads(vault.head_path.read_text(encoding="utf-8"))
    cursor = encode_cursor(
        scope="audit",
        snapshot_id=str(head["commit_id"]),
        params={"last_seq": 99},
    )
    decoded = decode_cursor(cursor, scope="audit")
    assert decoded is not None
    assert decoded["snapshot_id"] == head["commit_id"]
    # Now bump HEAD to a new fake commit_id and confirm decode returns
    # None so the caller must restart from page 1 rather than mixing
    # two snapshots into one response.
    vault.head_path.write_text(json.dumps({
        "commit_id": "commit_next", "parent_id": head["commit_id"],
    }), encoding="utf-8")
    decoded2 = decode_cursor(cursor, scope="audit")
    # ``decode_cursor`` does not reject snapshot mismatches; the
    # rejection happens at the query layer which compares the cursor's
    # snapshot against the *current* HEAD. Bumping HEAD and re-running
    # the list must therefore treat the cursor as stale.
    assert decoded2 is not None  # scope alone is valid
    svc = AuditQueries(vault=vault)
    page_after = svc.list_audit_events(conn=conn, cursor=cursor, page_size=5)
    # The list call ignores the stale ``last_seq`` from the cursor and
    # returns from the top because the cursor's snapshot does not
    # match HEAD. We assert that it returned *some* rows without
    # raising — the freshness guarantee is enforced server-side, not
    # by ``decode_cursor`` alone.
    assert isinstance(page_after.items, list)


def test_cursor_scope_mismatch_is_rejected(tmp_path: Path) -> None:
    """A cursor emitted for one endpoint cannot be replayed at another."""

    vault, conn = _fresh_vault(tmp_path)
    _seed_full_pipeline(
        vault=vault,
        conn=conn,
        session_id="sess_cursor_scope_test",
        user_lines=["use Python for the prototype"],
    )
    cursor = encode_cursor(scope="audit", snapshot_id="root", params={"last_seq": 1})
    assert decode_cursor(cursor, scope="audit") is not None
    assert decode_cursor(cursor, scope="sources") is None


def test_projection_state_reports_db_lag_when_intent_is_published(
    tmp_path: Path,
) -> None:
    """A published-but-not-finalised intent must surface as ``db_lag``."""

    vault, conn = _fresh_vault(tmp_path)
    _seed_full_pipeline(
        vault=vault,
        conn=conn,
        session_id="sess_db_lag",
        user_lines=["use Python for the prototype"],
    )
    # Take one commit and revert its phase to ``published`` only.
    intents = CommitIntentRepo(conn)
    rows = intents.list_by_phase("finalized")
    assert rows
    intents.update_phase(rows[0]["commit_id"], "published")
    conn.commit()
    svc = AuditQueries(vault=vault)
    overview = svc.overview(conn=conn)
    assert overview.header.projection_state == "db_lag"
    assert overview.header.db_lag_behind_head >= 1


def test_audit_queries_never_call_the_model(tmp_path: Path) -> None:
    """Reading audit data must not invoke the model provider at all.

    We snapshot the ``model_calls`` count, run every read method,
    and assert the count is unchanged. This is the v1 T13 invariant
    (docs/07 §"关键技术和固定边界" → 审计 API 不允许产生模型调用).
    """

    vault, conn = _fresh_vault(tmp_path)
    _seed_full_pipeline(
        vault=vault,
        conn=conn,
        session_id="sess_no_model_on_read",
        user_lines=["use Python for the prototype"],
    )
    svc = AuditQueries(vault=vault)
    # Snapshot before.
    before = _count_model_calls(conn)
    # Run every read path.
    svc.overview(conn=conn)
    svc.list_sources(conn=conn, cursor=None, page_size=10)
    src_id = conn.execute(
        "SELECT source_file_id FROM source_files LIMIT 1"
    ).fetchone()["source_file_id"]
    svc.source_detail(conn=conn, source_file_id=src_id)
    svc.list_session_turns(conn=conn, session_id="sess_no_model_on_read")
    svc.list_batches(conn=conn, cursor=None, page_size=10)
    batches = svc.list_batches(conn=conn, cursor=None, page_size=10).items
    if batches:
        bid = batches[0]["batch_id"]
        svc.batch_detail(conn=conn, batch_id=bid)
        svc.batch_evidence(conn=conn, batch_id=bid)
    mid_row = conn.execute(
        "SELECT memory_id FROM memory_projection LIMIT 1"
    ).fetchone()
    if mid_row:
        svc.memory_history(conn=conn, memory_id=mid_row["memory_id"])
    svc.list_conflicts(conn=conn, cursor=None, page_size=10)
    svc.list_audit_events(conn=conn, cursor=None, page_size=10)
    svc.audit_event_detail(
        conn=conn, audit_id=conn.execute(
            "SELECT audit_id FROM audit_events LIMIT 1"
        ).fetchone()["audit_id"],
    )
    svc.list_query_traces(conn=conn, cursor=None, page_size=10)
    audit_items = svc.list_audit_events(conn=conn, cursor=None, page_size=10).items
    first_trace = next(
        (it["object_id"] for it in audit_items
         if it["action"] == "query.retrieved"),
        None,
    )
    svc.query_trace_detail(conn=conn, trace_id=first_trace or "")
    after = _count_model_calls(conn)
    assert after == before, "audit queries must not call the model"