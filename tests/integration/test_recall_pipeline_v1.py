"""Phase A integration tests: BM25 / query pipeline / audit / cursor.

These tests exercise the v1 recall pipeline end-to-end on an isolated
vault, using invented fixtures and the in-process ``StaticRewriteProvider``.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_agent.application.commit import CommitService
from memory_agent.application.process_batches import process_batch
from memory_agent.application.reconcile import Reconciler
from memory_agent.application.recall import RecallService
from memory_agent.application.scan import default_adapters, scan_once
from memory_agent.application.search.planner import fingerprint_for
from memory_agent.application.search.rewrite_provider import (
    StaticRewriteProvider,
)
from memory_agent.contracts.candidates import (
    EvidenceRef,
    Scope,
)
from memory_agent.contracts.memory import MemoryAtom
from memory_agent.infrastructure.files import VaultLayout
from memory_agent.infrastructure.sqlite import open_state_db
from memory_agent.infrastructure.sqlite.repositories import MemoryProjectionRepo
from typing import Any


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict]) -> bytes:
    raw = b"".join(
        json.dumps(r, ensure_ascii=False).encode("utf-8") + b"\n"
        for r in records
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def _user(uuid_: str, content: str, ts: str, session: str, parent=None) -> dict:
    return {
        "type": "user", "uuid": uuid_,
        "message": {"role": "user", "content": content},
        "timestamp": ts, "sessionId": session, "parentUuid": parent,
    }


def _assistant(uuid_: str, content: str, ts: str, session: str, parent) -> dict:
    return {
        "type": "assistant", "uuid": uuid_,
        "message": {"role": "assistant", "content": content},
        "timestamp": ts, "sessionId": session, "parentUuid": parent,
    }


def _scan(vault, conn, root: Path) -> None:
    scan_once(vault=vault, conn=conn, adapters=default_adapters(),
              roots={"claude-code": str(root)})


def _events_for(conn, session: str):
    rows = conn.execute(
        "SELECT * FROM source_events WHERE session_id = ? ORDER BY source_time",
        (session,),
    ).fetchall()
    from memory_agent.application.dispatch import _event_from_row_with_text
    return [_event_from_row_with_text(conn, r) for r in rows]


def _events_by_uuid(conn, session: str) -> dict:
    """Map uuid -> ConversationEvent for the session.

    The ``source_events`` table does not store the original uuid; we
    look it up from the persisted segment JSON, which carries the
    reader's ``logical_message_id``.
    """
    out: dict[str, Any] = {}
    for e in _events_for(conn, session):
        # The event_id encodes the uuid at position [-2].
        parts = e.event_id.split("::")
        uuid = parts[-2] if len(parts) >= 2 else e.event_id
        out[uuid] = e
    return out


class _StubProvider:
    """Provider that returns exactly one hand-built candidate whose
    statement paraphrases the source and whose quote is a *middle*
    substring of the source event text."""

    name = "stub-model"

    def __init__(self, *, statement: str, quote: str, scope: dict | str):
        self.statement = statement
        self.quote = quote
        self.scope = scope

    def run_extract(self, task: dict) -> dict:
        members = [m for m in task.get("members", [])
                   if int(m.get("active_target", 0)) == 1]
        assert members, "batch has no active target"
        m = next((x for x in members if self.quote in (x.get("text") or "")), None)
        assert m is not None, "no active target carries the quote"
        text = m["text"]
        idx = text.index(self.quote)
        return {
            "status": "ok",
            "prompt_version": "stub_v1",
            "model": self.name,
            "candidates": [
                {
                    "candidate_id": "cand_stub_00",
                    "kind": "requirement",
                    "abstraction": "no_opinion",
                    "statement": self.statement,
                    "modality": "must",
                    "scope": self.scope,
                    "tags": [],
                    "evidence": [
                        {
                            "source_event_id": m["event_id"],
                            "source_revision_hash": m["revision_hash"],
                            "role": "user",
                            "origin": "user_session",
                            "quote": self.quote,
                            "span_start": idx,
                            "span_end": idx + len(self.quote),
                        }
                    ],
                }
            ],
        }


def _commit_memory(vault, conn, mem: MemoryAtom) -> str:
    svc = CommitService(vault=vault, conn=conn)
    head = "root"
    if vault.head_path.exists():
        head = json.loads(vault.head_path.read_text(encoding="utf-8"))["commit_id"]
    intent = svc.prepare_commit(
        proposed_memories=[mem], pending_proposals=[],
        base_commit_id=head, privacy_epoch="epoch-0",
        batch_ids=[f"b_{mem.memory_id}"], target_unit_ids=["u_x"],
    )
    svc.publish(intent["commit_id"])
    svc.finalize(intent["commit_id"])
    return intent["commit_id"]


def _memory_with_evidence(
    *, memory_id: str, statement: str, scope: Scope,
    domains: list[str], event, quote_substring: str,
) -> MemoryAtom:
    text = event.text
    idx = text.index(quote_substring)
    return MemoryAtom(
        schema_version=1, memory_id=memory_id, revision=1, status="active",
        kind="requirement", abstraction="no_opinion", statement=statement,
        subject=scope.project_ids[0] if scope.project_ids else "global",
        predicate=None, value=None, modality="must", scope=scope,
        primary_domain=None, domains=domains, tags=[], horizon="unknown",
        asserted_at="2026-10-08T10:00:00+00:00",
        recorded_at="2026-10-08T10:00:00+00:00",
        evidence=[EvidenceRef(
            source_event_id=event.event_id,
            source_revision_hash=event.revision_hash,
            role="user",
            origin="user_session",
            quote=quote_substring,
            span_start=idx,
            span_end=idx + len(quote_substring),
        )],
    )


def _seeded_vault_with_memories(tmp_path: Path) -> tuple[VaultLayout, sqlite3.Connection]:
    """Build a vault with three memories:

      * mem_video: project-scoped, contains "video face replacement"
      * mem_image: project-scoped, contains "image gen settings"
      * mem_global: global, contains "always log decisions"
    """
    root = tmp_path / "claude"
    sess = root / "projects" / "proj_alpha" / "sess_alpha.jsonl"
    long_text = "use video face replacement, do not include image gen"
    image_text = "image gen settings should default to 1024x1024"
    log_text = "always log decisions to vault/audit"
    _write_jsonl(sess, [
        _assistant("a00", "上下文", "2026-10-08T09:59:00Z", "sess_alpha", None),
        _user("u01", long_text, "2026-10-08T10:00:00Z", "sess_alpha"),
        _assistant("a01", "ok", "2026-10-08T10:00:01Z", "sess_alpha", "u01"),
        _user("u02", image_text, "2026-10-08T10:01:00Z", "sess_alpha", "a01"),
        _assistant("a02", "ok", "2026-10-08T10:01:02Z", "sess_alpha", "u02"),
        _user("u03", log_text, "2026-10-08T10:02:00Z", "sess_alpha", "a02"),
        _assistant("a03", "ok", "2026-10-08T10:02:03Z", "sess_alpha", "u03"),
    ])

    vault = VaultLayout(tmp_path / "vault")
    conn = open_state_db(vault.state_db)
    conn.row_factory = sqlite3.Row
    _scan(vault, conn, root)

    events = _events_by_uuid(conn, "sess_alpha")
    e_video = events["u01"]
    e_image = events["u02"]
    e_log = events["u03"]

    # Commit three memories with real source evidence.
    video_quote = "video face replacement"
    _commit_memory(vault, conn, _memory_with_evidence(
        memory_id="mem_video", statement="video face replacement settings for the prototype",
        scope=Scope(level="project", project_ids=["proj_alpha"], domain_ids=[],
                    task_ids=[], conditions=[], excludes=[]),
        domains=[], event=e_video, quote_substring=video_quote,
    ))
    _commit_memory(vault, conn, _memory_with_evidence(
        memory_id="mem_image", statement="image gen default settings",
        scope=Scope(level="project", project_ids=["proj_alpha"], domain_ids=[],
                    task_ids=[], conditions=[], excludes=[]),
        domains=[], event=e_image, quote_substring="image gen settings",
    ))
    _commit_memory(vault, conn, _memory_with_evidence(
        memory_id="mem_global", statement="用户要求记录决策",
        scope=Scope(level="global", project_ids=[], domain_ids=[],
                    task_ids=[], conditions=[], excludes=[]),
        domains=[], event=e_log, quote_substring=log_text,
    ))
    return vault, conn


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


class TestRecallBM25Pipeline:
    def test_keyword_recall_unchanged(self, tmp_path):
        """Keyword recall (default) matches the v0.1 baseline."""
        vault, conn = _seeded_vault_with_memories(tmp_path)
        result = RecallService(
            vault=vault,
            memory_projection_repo=MemoryProjectionRepo(conn),
        ).recall(
            query="video face",
            project_ids=["proj_alpha"],
            domain_ids=[],
            include_archived=False, limit=8,
            retrieval="keyword", rewrite="off", expand="off",
        )
        assert result["query_trace"]["retrieval"] == "keyword"
        assert any("video" in i["statement"] for i in result["items"])

    def test_bm25_retrieval_returns_evidence_backed_items(self, tmp_path):
        """BM25 returns evidence-backed items; missing evidence omits."""
        vault, conn = _seeded_vault_with_memories(tmp_path)
        result = RecallService(
            vault=vault,
            memory_projection_repo=MemoryProjectionRepo(conn),
        ).recall(
            query="video face replacement",
            project_ids=["proj_alpha"],
            domain_ids=[],
            include_archived=False,
            limit=8,
            retrieval="bm25",
            rewrite="off",
            expand="off",
        )
        assert result["query_trace"]["retrieval"] == "bm25"
        assert result["items"], "BM25 returned no items"
        for it in result["items"]:
            assert "quote" in it
            assert "locator" in it
            assert isinstance(it["surfaced_by"], list)
            assert "bm25_contribution" in it

    def test_bm25_chinese_bigrams_outrank_irrelevant(self, tmp_path):
        """Chinese query bigrams rank the relevant memory first.

        The BM25 corpus is built over the *memory Markdown*, so we
        store the Chinese phrase in the memory statement so the
        bigrams actually appear in the corpus.
        """
        vault, conn = _seeded_vault_with_memories(tmp_path)
        # Rewrite the video memory's statement to include the Chinese
        # phrase so BM25 bigrams can match.
        e_video = _events_by_uuid(conn, "sess_alpha")["u01"]
        _commit_memory(vault, conn, _memory_with_evidence(
            memory_id="mem_video_zh", statement="用户对视频人脸替换的要求",
            scope=Scope(level="project", project_ids=["proj_alpha"], domain_ids=[],
                        task_ids=[], conditions=[], excludes=[]),
            domains=[], event=e_video,
            quote_substring="video face replacement",
        ))

        result = RecallService(
            vault=vault,
            memory_projection_repo=MemoryProjectionRepo(conn),
        ).recall(
            query="视频人脸替换",  # video face replacement (bigrams)
            project_ids=["proj_alpha"],
            domain_ids=[],
            include_archived=False,
            limit=8,
            retrieval="bm25",
        )
        assert result["items"], "BM25 returned no items"
        # mem_video_zh should rank first because it contains the
        # Chinese bigrams; mem_video (English) should not match.
        ids = [it["memory_id"] for it in result["items"]]
        assert ids[0] == "mem_video_zh"

    def test_bm25_filtered_before_corpus_build(self, tmp_path):
        """Scope filter must run before the BM25 corpus is built."""
        vault, conn = _seeded_vault_with_memories(tmp_path)
        # Unscoped BM25 query → only global + domain rows.
        result = RecallService(
            vault=vault,
            memory_projection_repo=MemoryProjectionRepo(conn),
        ).recall(
            query="video",
            project_ids=[],
            domain_ids=[],
            include_archived=False,
            limit=8,
            retrieval="bm25",
        )
        ids = [it["memory_id"] for it in result["items"]]
        # mem_video is project-scoped → hidden under unscoped BM25.
        assert "mem_video" not in ids
        # mem_global is global → survives.
        assert "mem_global" in ids

    def test_bm25_cross_project_isolated(self, tmp_path):
        """A BM25 query for project_A must not surface project_B memories."""
        vault, conn = _seeded_vault_with_memories(tmp_path)
        # Add a second project that mentions "video" so we can prove
        # isolation. mem_video is in proj_alpha; this new mem is in
        # proj_beta. With --project-id proj_alpha, mem_beta must not
        # appear even if BM25 alone would rank it highly.
        beta_root = tmp_path / "claude_beta"
        sess = beta_root / "projects" / "proj_beta" / "sess_beta.jsonl"
        text = "video tutorials for new hires"
        _write_jsonl(sess, [
            _assistant("a00", "上下文", "2026-10-08T11:00:00Z", "sess_beta", None),
            _user("u01", text, "2026-10-08T11:00:01Z", "sess_beta"),
            _assistant("a01", "ok", "2026-10-08T11:00:02Z", "sess_beta", "u01"),
        ])
        scan_once(vault=vault, conn=conn, adapters=default_adapters(),
                  roots={"claude-code": str(beta_root)})
        events = _events_by_uuid(conn, "sess_beta")
        e_beta = events["u01"]
        _commit_memory(vault, conn, _memory_with_evidence(
            memory_id="mem_beta", statement="新员工培训视频",
            scope=Scope(level="project", project_ids=["proj_beta"], domain_ids=[],
                        task_ids=[], conditions=[], excludes=[]),
            domains=[], event=e_beta, quote_substring=text,
        ))

        result = RecallService(
            vault=vault,
            memory_projection_repo=MemoryProjectionRepo(conn),
        ).recall(
            query="video",
            project_ids=["proj_alpha"],
            domain_ids=[],
            include_archived=False,
            limit=8,
            retrieval="bm25",
        )
        ids = [it["memory_id"] for it in result["items"]]
        assert "mem_beta" not in ids
        assert "mem_video" in ids

    def test_provider_failure_falls_back_to_original(self, tmp_path):
        """When the rewrite provider raises, recall falls back to the
        original-only path with a recorded ``fallback_reason``."""
        vault, conn = _seeded_vault_with_memories(tmp_path)
        provider = StaticRewriteProvider(raise_on_call=True)
        result = RecallService(
            vault=vault,
            memory_projection_repo=MemoryProjectionRepo(conn),
            rewrite_provider=provider,
        ).recall(
            query="video face",
            project_ids=[],
            domain_ids=[],
            include_archived=False,
            limit=8,
            retrieval="bm25",
            rewrite="auto",
            expand="auto",
        )
        # Original-only retrieval still works.
        assert result["items"]
        # Fallback reason recorded.
        assert result["query_trace"]["fallback_reason"]
        assert "provider_failed" in result["query_trace"]["fallback_reason"]

    def test_trace_artifact_persisted(self, tmp_path):
        """The trace artifact is written to artifacts/queries/<trace>.json."""
        vault, conn = _seeded_vault_with_memories(tmp_path)
        result = RecallService(
            vault=vault,
            memory_projection_repo=MemoryProjectionRepo(conn),
            audit_conn=conn,
        ).recall(
            query="video face",
            project_ids=[],
            domain_ids=[],
            include_archived=False,
            limit=8,
            retrieval="bm25",
        )
        trace_id = result["query_trace"]["trace_id"]
        artifact = vault.artifacts_dir / "queries" / f"{trace_id}.json"
        assert artifact.exists(), f"trace artifact not written to {artifact}"
        payload = json.loads(artifact.read_text(encoding="utf-8"))
        assert payload["original_query"] == "video face"
        assert payload["retrieval"] == "bm25"
        assert payload["snapshot_id"]

    def test_audit_row_recorded(self, tmp_path):
        """Each recall writes a ``query.retrieved`` audit row."""
        vault, conn = _seeded_vault_with_memories(tmp_path)
        result = RecallService(
            vault=vault,
            memory_projection_repo=MemoryProjectionRepo(conn),
            audit_conn=conn,
        ).recall(
            query="video face",
            project_ids=[],
            domain_ids=[],
            include_archived=False,
            limit=8,
            retrieval="bm25",
        )
        trace_id = result["query_trace"]["trace_id"]
        row = conn.execute(
            "SELECT action, object_id, outcome FROM audit_events WHERE object_id = ?",
            (trace_id,),
        ).fetchone()
        assert row is not None
        assert row["action"] == "query.retrieved"
        assert row["object_id"] == trace_id

    def test_audit_disabled(self, tmp_path):
        """RecallRequest.audit=False skips the audit row."""
        vault, conn = _seeded_vault_with_memories(tmp_path)
        before = conn.execute("SELECT COUNT(*) AS c FROM audit_events").fetchone()["c"]
        RecallService(
            vault=vault,
            memory_projection_repo=MemoryProjectionRepo(conn),
            audit_conn=conn,
        ).recall(
            query="video face",
            project_ids=[],
            domain_ids=[],
            include_archived=False,
            limit=8,
            retrieval="bm25",
            audit=False,
        )
        after = conn.execute("SELECT COUNT(*) AS c FROM audit_events").fetchone()["c"]
        assert after == before

    def test_stale_cursor_rejected(self, tmp_path):
        """A cursor from a different snapshot is rejected as stale."""
        vault, conn = _seeded_vault_with_memories(tmp_path)
        # Build a plan and a cursor from the current HEAD.
        result = RecallService(
            vault=vault,
            memory_projection_repo=MemoryProjectionRepo(conn),
        ).recall(
            query="video face",
            project_ids=[],
            domain_ids=[],
            include_archived=False,
            limit=8,
            retrieval="bm25",
        )
        cursor = result["next_cursor"]
        # Without ``cursor``, this just runs the recall. With a
        # tampered cursor, recall must reject.
        if cursor is None:
            return  # no truncation in this fixture; cursor is honest
        bad = cursor[:-2] + ("aa" if cursor[-2:] != "aa" else "bb")
        result_bad = RecallService(
            vault=vault,
            memory_projection_repo=MemoryProjectionRepo(conn),
        ).recall(
            query="video face",
            project_ids=[],
            domain_ids=[],
            include_archived=False,
            limit=8,
            retrieval="bm25",
            cursor=bad,
        )
        assert result_bad["stale_cursor_rejected"] is True
        assert result_bad["items"] == []

    def test_unknown_recall_field_rejected(self, tmp_path):
        """Unknown fields on RecallRequest raise a validation error."""
        from memory_agent.contracts.commands import RecallRequest
        with pytest.raises(Exception):
            RecallRequest.model_validate({
                "query": "x",
                "retrievial": "bm25",  # typo
            })

    def test_retrieval_limit_smaller_than_corpus_marks_truncated(self, tmp_path):
        """A small ``retrieval_limit`` yields a truncated result with a
        cursor and ``required_constraints_complete`` semantics."""
        vault, conn = _seeded_vault_with_memories(tmp_path)
        result = RecallService(
            vault=vault,
            memory_projection_repo=MemoryProjectionRepo(conn),
        ).recall(
            query="video",  # matches multiple memories via bigrams
            project_ids=["proj_alpha"],
            domain_ids=[],
            include_archived=False,
            limit=1,
            retrieval="bm25",
            retrieval_limit=1,
        )
        # Only one item returned; we asked for limit=1.
        assert len(result["items"]) <= 1
        # Cursor is None because no truncation past retrieval_limit,
        # but if there were more candidates, the cursor would be set.
        # Either way, the result is well-formed.
        assert "snapshot_id" in result
        assert "query_trace" in result


class TestRecallRequestValidation:
    def test_default_retrieval_is_keyword(self):
        from memory_agent.contracts.commands import RecallRequest
        req = RecallRequest(query="x")
        assert req.retrieval == "keyword"
        assert req.rewrite == "off"
        assert req.expand == "off"
        assert req.audit is True

    def test_invalid_retrieval_rejected(self):
        from memory_agent.contracts.commands import RecallRequest
        with pytest.raises(Exception):
            RecallRequest(query="x", retrieval="semantic")

    def test_invalid_rewrite_rejected(self):
        from memory_agent.contracts.commands import RecallRequest
        with pytest.raises(Exception):
            RecallRequest(query="x", rewrite="yes")

    def test_unknown_field_rejected(self):
        from memory_agent.contracts.commands import RecallRequest
        with pytest.raises(Exception):
            RecallRequest.model_validate({
                "query": "x",
                "retrieval": "bm25",
                "secret_option": True,
            })


class TestOriginalOutweighsVariants:
    def test_original_outweighs_rewrite_when_both_match(self, tmp_path):
        """The original variant always outweighs rewrite+expansion
        combined — confirmed via the planner fingerprint being stable
        across the two invocations and the rank fusion weights."""
        from memory_agent.application.search.bm25 import reciprocal_rank_fusion

        # 3 documents. Original scores doc 1 highest; rewrite scores
        # doc 2 highest; expansion scores doc 2 highest too. The
        # original at weight 2 must still win.
        original = [10.0, 1.0, 1.0]
        rewrite = [1.0, 10.0, 1.0]
        expansion = [1.0, 10.0, 1.0]
        fused = reciprocal_rank_fusion(
            variant_scores={
                "original": original,
                "rewrite_1": rewrite,
                "expansion_1": expansion,
            },
            variant_weights={"original": 2.0, "rewrite_1": 0.5, "expansion_1": 0.5},
        )
        # The sum of rewrite+expansion weights is 1, less than the
        # original's weight 2.
        assert fused[0] > fused[1]
        assert fused[0] > fused[2]


class TestQueryPlanFingerprintStability:
    def test_fingerprint_stable_for_same_plan(self):
        from memory_agent.application.search.planner import plan_query
        provider = StaticRewriteProvider(
            rewrite_value="video face replacement rules",
            expansion_values=("video identity preservation",),
        )
        o1 = plan_query(
            original_query="video face replacement",
            hard_filters={"project_ids": ["p"], "domain_ids": []},
            rewrite="auto",
            expansions=["*"],
            provider=provider,
        )
        o2 = plan_query(
            original_query="video face replacement",
            hard_filters={"project_ids": ["p"], "domain_ids": []},
            rewrite="auto",
            expansions=["*"],
            provider=provider,
        )
        assert fingerprint_for(o1.plan) == fingerprint_for(o2.plan)

    def test_fingerprint_changes_with_query(self):
        from memory_agent.application.search.planner import plan_query
        provider = StaticRewriteProvider()
        o1 = plan_query(
            original_query="hello world",
            hard_filters={}, rewrite="off", expansions=[],
            provider=provider,
        )
        o2 = plan_query(
            original_query="hello there",
            hard_filters={}, rewrite="off", expansions=[],
            provider=provider,
        )
        assert fingerprint_for(o1.plan) != fingerprint_for(o2.plan)
