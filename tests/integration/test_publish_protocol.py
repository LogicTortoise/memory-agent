"""Targeted tests for the v0.1 publish-protocol and same-batch
reconciliation invariants.

These tests pin down each fix called out in the task brief. They are
deliberately pointed at the *production* classes
(``CommitService``, ``process_batch``, ``Reconciler``) and assert the
exact post-conditions the spec requires:

  * ``prepare_commit`` writes immutable files + a DB intent and nothing
    else — no memory_projection rows, no conflict_projection rows, and
    no batch finalisation.
  * ``publish`` checks HEAD still points at the intent's parent before
    swapping; a stale HEAD aborts the intent.
  * ``finalize`` runs only after HEAD contains the published commit, and
    writes projection rows + the batch terminal decision in one short
    SQL transaction.
  * Pending-only and no_memory outcomes produce a durable commit (a
    ``commit_…`` with zero memories and zero pending proposals for the
    no_memory case, or one-or-more pending proposals for the pending
    case); HEAD advances exactly once.
  * Same-batch contradictions are detected against candidates *and*
    pre-existing memory, and never produce two contradictory active
    memories.
  * Recovery never auto-publishes a prepared intent whose HEAD is at
    parent; missing or corrupt manifests surface as recovery_required
    and block new writes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_agent.application.commit import CommitError, CommitService
from memory_agent.application.process_batches import process_batch
from memory_agent.application.reconcile import Reconciler
from memory_agent.contracts.candidates import (
    Candidate,
    CandidateSet,
    EvidenceRef,
    Scope,
)
from memory_agent.contracts.memory import MemoryAtom
from memory_agent.infrastructure.files import VaultLayout
from memory_agent.infrastructure.sqlite import open_state_db
from memory_agent.infrastructure.sqlite.repositories import (
    BatchRepo,
    MemoryProjectionRepo,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _scope(level: str = "global") -> Scope:
    return Scope(level=level)  # type: ignore[arg-type]


def _ev(eid: str, quote: str, *, rev: str = "rh") -> EvidenceRef:
    return EvidenceRef(
        source_event_id=eid,
        source_revision_hash=rev,
        role="user",
        origin="user_session",
        quote=quote,
        span_start=0,
        span_end=len(quote),
    )


def _mem(memory_id: str, statement: str) -> MemoryAtom:
    return MemoryAtom(
        schema_version=1,
        memory_id=memory_id, revision=1, status="active",
        kind="requirement", abstraction="no_opinion",
        statement=statement, subject="global",
        predicate=None, value=None, modality="must",
        scope=_scope(), primary_domain=None, domains=[],
        tags=[], horizon="unknown",
        asserted_at="2026-10-08T10:00:00+00:00",
        recorded_at="2026-10-08T10:00:00+00:00",
        evidence=[],
    )


def _candidate(
    cid: str, statement: str, *,
    eid: str = "evt_target", scope: Scope | None = None,
) -> Candidate:
    return Candidate(
        candidate_id=cid, kind="requirement", abstraction="no_opinion",
        statement=statement, modality="unspecified",
        scope=scope or _scope(), primary_domain=None, tags=[],
        evidence=[_ev(eid, statement)],
    )


def _cs(*cands: Candidate, batch_id: str = "b_test") -> CandidateSet:
    return CandidateSet(
        schema_version=1,
        candidate_set_id=f"cs_{batch_id}",
        batch_id=batch_id,
        candidates=list(cands),
        no_memory=(len(cands) == 0),
        prompt_version="fake_extract_v1",
        model="fake-model",
        raw_payload_sha256="deadbeef",
        status="validated",
    )


def _make_batch(
    vault: VaultLayout, conn, members_count: int,
    *, event_ids: list[str] | None = None,
) -> str:
    """Insert a batch with `members_count` active targets so we can
    drive ``process_batch`` end-to-end. Returns batch_id.

    If ``event_ids`` is given, the i-th source event uses that ID;
    otherwise we synthesise ``evt_<batch_id>_<i>``.
    """
    import hashlib
    batch_id = "b_" + hashlib.sha1(str(vault.root).encode()).hexdigest()[:10]
    conn.execute(
        """INSERT INTO batches(
            batch_id, session_id, branch_id, policy_version,
            input_hash, status, attempt, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
        (batch_id, "sess_test", "main", "v1", "ih", "ready",
         "2026-10-08T10:00:00+00:00"),
    )
    # Source events + work_units so evidence validation passes.
    for i in range(members_count):
        eid = (event_ids[i] if event_ids is not None
               else f"evt_{batch_id}_{i}")
        unit_id = f"u_{batch_id}_{i}"
        conn.execute(
            """INSERT INTO source_events(
                event_id, canonical_event_key, session_id, branch_id,
                source_file_id, segment_id, role, kind, revision_hash,
                source_time, discovered_at, replaces_event_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
            (eid, f"ck_{eid}", "sess_test", "main", "src_test",
             "seg_test", "user", "user_turn", "rh", "2026-10-08T10:00:00Z",
             "2026-10-08T10:00:00Z"),
        )
        conn.execute(
            """INSERT INTO work_units(
                unit_id, session_id, branch_id, turn_id, event_id,
                span_start, span_end, split_policy, parent_unit_id, epoch
            ) VALUES (?, ?, ?, ?, ?, 0, 0, NULL, NULL, 1)""",
            (unit_id, "sess_test", "main", "turn_test", eid),
        )
        conn.execute(
            """INSERT INTO batch_members(
                batch_id, unit_id, purpose, ord, epoch, active_target
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (batch_id, unit_id, "active_target", i, 1, 1),
        )
    return batch_id


def _seed_segment(conn, text_by_eid: dict[str, str]) -> None:
    """Persist a segment JSON the evidence lookup can read."""
    seg_path = Path("/tmp") / "seg_test.json"
    payload = {
        "segment_version": 1,
        "source_file_id": "src_test",
        "generation": 1,
        "start_offset": 0,
        "end_offset": 0,
        "events": [
            {"event_id": eid, "revision_hash": "rh",
             "text": text, "source": {"locator": {
                 "byte_start": 0, "byte_end": len(text), "record_ordinal": 1,
             }}}
            for eid, text in text_by_eid.items()
        ],
    }
    seg_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    # Register segment row so evidence_lookup can resolve it.
    conn.execute("DELETE FROM segments WHERE segment_id = 'seg_test'")
    conn.execute("DELETE FROM source_files WHERE source_file_id = 'src_test'")
    conn.execute(
        """INSERT INTO source_files(
            source_file_id, connector, session_id, branch_id,
            canonical_path, file_identity, adapter_version,
            generation, complete_offset, size, mtime, last_scan_at,
            last_status, last_error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, NULL, NULL, 'baseline', NULL)""",
        ("src_test", "claude-code", "sess_test", "main",
         str(seg_path), "fidentity", "reader-v0.1", 1),
    )
    conn.execute(
        """INSERT INTO segments(
            segment_id, source_file_id, generation, path, sha256,
            start_offset, end_offset, created_at
        ) VALUES (?, ?, 1, ?, 'sh', 0, ?, '2026-10-08T10:00:00Z')""",
        ("seg_test", "src_test", str(seg_path), 1024),
    )


# ---------------------------------------------------------------------------
# Invariant 1: prepare_commit writes nothing visible
# ---------------------------------------------------------------------------


class TestPrepareCommitIsSideEffectFree:
    def test_prepare_writes_no_memory_projection(self, tmp_path):
        """prepare_commit must not write any memory_projection rows.
        Projection appears only after finalize()."""
        vault = VaultLayout(tmp_path / "vault")
        conn = open_state_db(vault.state_db)
        svc = CommitService(vault=vault, conn=conn)
        intent = svc.prepare_commit(
            proposed_memories=[_mem("m_pre", "pre")],
            pending_proposals=[],
            base_commit_id="root", privacy_epoch="epoch-0",
            batch_ids=["b_pre"], target_unit_ids=["u_pre"],
        )
        rows = conn.execute(
            "SELECT * FROM memory_projection WHERE memory_id = 'm_pre'"
        ).fetchall()
        assert rows == [], (
            f"prepare_commit leaked {len(rows)} memory_projection rows"
        )
        # HEAD must not exist.
        assert not vault.head_path.exists()
        # Intent is recorded as 'prepared'.
        phase = conn.execute(
            "SELECT phase FROM commit_intents WHERE commit_id = ?",
            (intent["commit_id"],),
        ).fetchone()
        assert phase["phase"] == "prepared"

    def test_prepare_writes_no_conflict_projection(self, tmp_path):
        """prepare_commit must not write conflict_projection rows.
        Conflict projections appear only after finalize() succeeds on
        a HEAD-advanced commit."""
        vault = VaultLayout(tmp_path / "vault")
        conn = open_state_db(vault.state_db)
        svc = CommitService(vault=vault, conn=conn)
        prop = {
            "candidate_id": "cand_pre_pending",
            "candidate": {
                "candidate_id": "cand_pre_pending",
                "kind": "requirement",
                "abstraction": "no_opinion",
                "statement": "pending candidate",
                "modality": "unspecified",
                "scope": {"level": "global"},
                "primary_domain": None, "tags": [],
                "evidence": [],
            },
            "intersects": ["mem_other"],
        }
        svc.prepare_commit(
            proposed_memories=[],
            pending_proposals=[prop],
            base_commit_id="root", privacy_epoch="epoch-0",
            batch_ids=["b_prep"], target_unit_ids=["u_prep"],
        )
        rows = conn.execute(
            "SELECT * FROM conflict_projection WHERE conflict_id = ?",
            ("cand_pre_pending",),
        ).fetchall()
        assert rows == [], (
            f"prepare_commit leaked {len(rows)} conflict_projection rows"
        )
        # HEAD must not exist; the projection is HEAD-driven, not intent-driven.
        assert not vault.head_path.exists()


# ---------------------------------------------------------------------------
# Invariant 2: publish checks HEAD == parent
# ---------------------------------------------------------------------------


class TestPublishHeadCheck:
    def test_publish_aborts_when_head_already_advanced(self, tmp_path):
        """If HEAD has moved past the intent's parent between prepare()
        and publish(), publish() must NOT swap HEAD. It must raise
        CommitError so the caller can surface recovery_required."""
        vault = VaultLayout(tmp_path / "vault")
        conn = open_state_db(vault.state_db)
        svc = CommitService(vault=vault, conn=conn)
        # First commit: base_commit_id=root, publish, finalize -> HEAD at c1.
        c1 = svc.prepare_commit(
            proposed_memories=[_mem("m_one", "one")],
            pending_proposals=[],
            base_commit_id="root", privacy_epoch="epoch-0",
            batch_ids=["b_one"], target_unit_ids=["u_one"],
        )
        svc.publish(c1["commit_id"])
        svc.finalize(c1["commit_id"])
        # Second commit: base_commit_id=root (stale).
        c2 = svc.prepare_commit(
            proposed_memories=[_mem("m_two", "two")],
            pending_proposals=[],
            base_commit_id="root", privacy_epoch="epoch-0",
            batch_ids=["b_two"], target_unit_ids=["u_two"],
        )
        with pytest.raises(CommitError):
            svc.publish(c2["commit_id"])
        # HEAD must still be at c1.
        head = json.loads(vault.head_path.read_text(encoding="utf-8"))
        assert head["commit_id"] == c1["commit_id"]
        # The stale intent stays 'prepared' (not aborted by publish).
        phase = conn.execute(
            "SELECT phase FROM commit_intents WHERE commit_id = ?",
            (c2["commit_id"],),
        ).fetchone()
        assert phase["phase"] == "prepared"


# ---------------------------------------------------------------------------
# Invariant 3: finalize materialises both projections in one tx
# ---------------------------------------------------------------------------


class TestFinalizeTransactional:
    def test_finalize_writes_memory_and_conflict_projections_atomically(
        self, tmp_path
    ):
        """finalize() must write memory_projection AND conflict_projection
        rows together. After HEAD advances, both are visible; before HEAD
        moves, neither is visible."""
        vault = VaultLayout(tmp_path / "vault")
        conn = open_state_db(vault.state_db)
        svc = CommitService(vault=vault, conn=conn)
        prop = {
            "candidate_id": "cand_fin",
            "candidate": {
                "candidate_id": "cand_fin",
                "kind": "requirement",
                "abstraction": "no_opinion",
                "statement": "pending",
                "modality": "unspecified",
                "scope": {"level": "global"},
                "primary_domain": None, "tags": [],
                "evidence": [],
            },
            "intersects": ["mem_old"],
        }
        intent = svc.prepare_commit(
            proposed_memories=[_mem("m_fin", "fin")],
            pending_proposals=[prop],
            base_commit_id="root", privacy_epoch="epoch-0",
            batch_ids=["b_fin"], target_unit_ids=["u_fin"],
        )
        # Pre-publish: nothing visible.
        assert conn.execute(
            "SELECT 1 FROM memory_projection WHERE memory_id = 'm_fin'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM conflict_projection WHERE conflict_id = 'cand_fin'"
        ).fetchone() is None
        svc.publish(intent["commit_id"])
        # Post-publish, pre-finalize: still nothing visible.
        assert conn.execute(
            "SELECT 1 FROM memory_projection WHERE memory_id = 'm_fin'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM conflict_projection WHERE conflict_id = 'cand_fin'"
        ).fetchone() is None
        svc.finalize(intent["commit_id"])
        # Now both are visible.
        assert conn.execute(
            "SELECT 1 FROM memory_projection WHERE memory_id = 'm_fin'"
        ).fetchone() is not None
        assert conn.execute(
            "SELECT 1 FROM conflict_projection WHERE conflict_id = 'cand_fin'"
        ).fetchone() is not None


# ---------------------------------------------------------------------------
# Invariant 4: recovery never auto-publishes prepared intent
# ---------------------------------------------------------------------------


class TestRecoveryNoAutoPublish:
    def test_recovery_aborts_prepared_intent_with_head_at_parent(
        self, tmp_path
    ):
        """A prepared intent whose parent still matches HEAD must be
        ABORTED by recovery, NOT auto-published. The caller crashed
        between prepare and publish; we cannot recover intent."""
        vault = VaultLayout(tmp_path / "vault")
        conn = open_state_db(vault.state_db)
        svc = CommitService(vault=vault, conn=conn)
        intent = svc.prepare_commit(
            proposed_memories=[_mem("m_rec", "rec")],
            pending_proposals=[],
            base_commit_id="root", privacy_epoch="epoch-0",
            batch_ids=["b_rec"], target_unit_ids=["u_rec"],
        )
        # Simulate restart; HEAD still at root (intent never published).
        rec = svc.recover_unfinished()
        # No projection rows.
        rows = conn.execute(
            "SELECT 1 FROM memory_projection WHERE memory_id = 'm_rec'"
        ).fetchall()
        assert rows == []
        # HEAD must not exist.
        assert not vault.head_path.exists()
        # The intent is aborted, not finalised.
        assert rec["aborted"] >= 1
        assert rec["finalized"] == 0
        phase = conn.execute(
            "SELECT phase FROM commit_intents WHERE commit_id = ?",
            (intent["commit_id"],),
        ).fetchone()
        assert phase["phase"] == "aborted"

    def test_recovery_finalizes_published_intent_exactly_once(
        self, tmp_path
    ):
        """A published intent (HEAD already advanced) must be finalised
        by recovery exactly once. The second recovery call is a no-op."""
        vault = VaultLayout(tmp_path / "vault")
        conn = open_state_db(vault.state_db)
        svc = CommitService(vault=vault, conn=conn)
        intent = svc.prepare_commit(
            proposed_memories=[_mem("m_p", "p")],
            pending_proposals=[],
            base_commit_id="root", privacy_epoch="epoch-0",
            batch_ids=["b_p"], target_unit_ids=["u_p"],
        )
        svc.publish(intent["commit_id"])
        # Crash before finalize.
        rec1 = svc.recover_unfinished()
        assert rec1["finalized"] == 1
        # Projection visible.
        rows = conn.execute(
            "SELECT * FROM memory_projection WHERE memory_id = 'm_p'"
        ).fetchall()
        assert len(rows) == 1
        # Second pass is idempotent.
        rec2 = svc.recover_unfinished()
        assert rec2["finalized"] == 0
        rows = conn.execute(
            "SELECT * FROM memory_projection WHERE memory_id = 'm_p'"
        ).fetchall()
        assert len(rows) == 1

    def test_recovery_missing_manifest_returns_recovery_required(
        self, tmp_path
    ):
        """A published intent whose manifest vanished must surface as
        recovery_required and block new writes."""
        vault = VaultLayout(tmp_path / "vault")
        conn = open_state_db(vault.state_db)
        svc = CommitService(vault=vault, conn=conn)
        intent = svc.prepare_commit(
            proposed_memories=[_mem("m_corrupt", "c")],
            pending_proposals=[],
            base_commit_id="root", privacy_epoch="epoch-0",
            batch_ids=["b_c"], target_unit_ids=["u_c"],
        )
        svc.publish(intent["commit_id"])
        Path(intent["manifest_ref"]).unlink()
        rec = svc.recover_unfinished()
        assert intent["commit_id"] in rec["recovery_required"]
        # No projection rows.
        rows = conn.execute(
            "SELECT 1 FROM memory_projection WHERE memory_id = 'm_corrupt'"
        ).fetchall()
        assert rows == []
        # New writes must be blocked: a new commit attempt must surface
        # recovery_required (HEAD doesn't include the lost intent).
        next_intent = svc.prepare_commit(
            proposed_memories=[_mem("m_after_corrupt", "x")],
            pending_proposals=[],
            base_commit_id=intent["commit_id"],
            privacy_epoch="epoch-0",
            batch_ids=["b_after"], target_unit_ids=["u_after"],
        )
        # Trying to publish/finalize on top of a HEAD whose manifest is
        # missing must refuse: HEAD doesn't actually contain the
        # commit because no finalize happened.
        with pytest.raises(CommitError):
            svc.publish(next_intent["commit_id"])


# ---------------------------------------------------------------------------
# Invariant 5: process_batch terminal decisions
# ---------------------------------------------------------------------------


class TestProcessBatchTerminalDecisions:
    def test_pending_only_batch_publishes_a_durable_commit(
        self, tmp_path
    ):
        """A batch whose outcome is pending-only must still produce a
        durable commit (zero memories, N pending proposals), HEAD
        advances exactly once, and only AFTER HEAD moves does the
        conflict_projection row appear."""
        vault = VaultLayout(tmp_path / "vault")
        conn = open_state_db(vault.state_db)
        _seed_segment(conn, {"evt_target": "use TypeScript for the dashboard"})
        batch_id = _make_batch(
            vault, conn, members_count=1, event_ids=["evt_target"],
        )
        # Pre-existing active memory so the candidate is pending.
        MemoryProjectionRepo(conn).upsert({
            "memory_id": "mem_existing", "revision": 1, "status": "active",
            "scope": {"level": "global"}, "project_ids": [],
            "domains": [], "commit_id": "root",
            "statement": "use Python for the prototype",
        })

        class _PendingOnlyReconciler(Reconciler):
            def __init__(self):
                class _Repo:
                    def list_active(self, project_ids=None):
                        return [{
                            "memory_id": "mem_existing",
                            "revision": 1,
                            "status": "active",
                            "scope": {"level": "global"},
                            "project_ids": [], "domains": [],
                            "statement": "use Python for the prototype",
                            "commit_id": "root",
                        }]
                super().__init__(memory_projection_repo=_Repo())  # type: ignore[arg-type]

            def reconcile(self, candidate_set, *, evidence_lookup):
                from memory_agent.application.reconcile import ReconcilerOutcome
                return ReconcilerOutcome(
                    pending=[{
                        "candidate_id": "cand_pending",
                        "candidate": candidate_set.candidates[0].model_dump(mode="json"),
                        "intersects": ["mem_existing"],
                    }],
                )

        from memory_agent.adapters.models import FakeModelProvider
        # Inject one contradictory candidate.
        provider = FakeModelProvider()
        provider.set_response({
            "status": "ok",
            "candidates": [{
                "candidate_id": "cand_pending",
                "kind": "requirement",
                "abstraction": "no_opinion",
                "statement": "use TypeScript for the dashboard",
                "modality": "unspecified",
                "scope": {"level": "global"},
                "tags": [],
                "evidence": [{
                    "source_event_id": "evt_target",
                    "source_revision_hash": "rh",
                    "role": "user",
                    "origin": "user_session",
                    "quote": "use TypeScript for the dashboard",
                    "span_start": 0,
                    "span_end": len("use TypeScript for the dashboard"),
                }],
            }],
        })
        # Lookup function that reads from the segment.
        from memory_agent.application.dispatch import _evidence_text_lookup
        evidence_lookup = lambda eid, rh: _evidence_text_lookup(conn, eid, rh, vault)

        res = process_batch(
            batch_id=batch_id,
            vault=vault, conn=conn,
            provider=provider,
            reconciler=_PendingOnlyReconciler(),
            committer=CommitService(vault=vault, conn=conn),
            evidence_lookup=evidence_lookup,
            base_commit_id="root",
            privacy_epoch="epoch-0",
        )
        assert res["status"] == "completed"
        # HEAD must have advanced to a new commit.
        head = json.loads(vault.head_path.read_text(encoding="utf-8"))
        assert head["commit_id"] not in (None, "root")
        # The new commit's manifest carries zero memories and one
        # pending proposal.
        manifest = json.loads(
            (vault.commits_dir / head["commit_id"] / "manifest.json")
            .read_text(encoding="utf-8")
        )
        assert manifest["memories"] == []
        assert len(manifest["pending_proposals"]) == 1
        # And ONLY now does the conflict_projection row appear.
        rows = conn.execute(
            "SELECT * FROM conflict_projection WHERE conflict_id = 'cand_pending'"
        ).fetchall()
        assert len(rows) == 1
        # Batch is marked completed.
        batch_row = BatchRepo(conn).get(batch_id)
        assert batch_row is not None
        assert batch_row["status"] == "completed"

    def test_no_memory_batch_publishes_a_consumption_receipt(
        self, tmp_path
    ):
        """A no_memory batch must produce a durable HEAD commit
        (zero memories, zero pending proposals) — a 'consumption
        receipt' — and the batch must be marked completed."""
        vault = VaultLayout(tmp_path / "vault")
        conn = open_state_db(vault.state_db)
        _seed_segment(conn, {"evt_target": "just chit-chat, no requirements"})
        batch_id = _make_batch(
            vault, conn, members_count=1, event_ids=["evt_target"],
        )

        from memory_agent.adapters.models import FakeModelProvider
        provider = FakeModelProvider()
        provider.set_response({"status": "ok", "candidates": []})

        from memory_agent.application.reconcile import Reconciler
        from memory_agent.application.dispatch import _evidence_text_lookup
        res = process_batch(
            batch_id=batch_id,
            vault=vault, conn=conn,
            provider=provider,
            reconciler=Reconciler(memory_projection_repo=_FakeRepo()),
            committer=CommitService(vault=vault, conn=conn),
            evidence_lookup=lambda eid, rh: _evidence_text_lookup(conn, eid, rh, vault),
            base_commit_id="root",
            privacy_epoch="epoch-0",
        )
        assert res["status"] == "completed"
        head = json.loads(vault.head_path.read_text(encoding="utf-8"))
        assert head["commit_id"] not in (None, "root")
        manifest = json.loads(
            (vault.commits_dir / head["commit_id"] / "manifest.json")
            .read_text(encoding="utf-8")
        )
        assert manifest["memories"] == []
        assert manifest["pending_proposals"] == []
        # Batch marked completed, not pending.
        batch_row = BatchRepo(conn).get(batch_id)
        assert batch_row is not None
        assert batch_row["status"] == "completed"


class _FakeRepo:
    def list_active(self, project_ids=None):
        return []


# ---------------------------------------------------------------------------
# Invariant 6: same-batch contradiction
# ---------------------------------------------------------------------------


class TestSameBatchContradiction:
    def test_two_contradictory_candidates_in_same_batch_go_pending(
        self, tmp_path
    ):
        """Two contradictory candidates produced by the same batch must
        end up as pending (or, if both are ambiguous, both pending) —
        never both as two contradictory active memories."""
        vault = VaultLayout(tmp_path / "vault")
        conn = open_state_db(vault.state_db)
        _seed_segment(conn, {
            "evt_a": "use Python for the prototype",
            "evt_b": "use TypeScript for the prototype",
        })
        batch_id = _make_batch(
            vault, conn, members_count=2, event_ids=["evt_a", "evt_b"],
        )

        from memory_agent.adapters.models import FakeModelProvider
        provider = FakeModelProvider()
        provider.set_response({
            "status": "ok",
            "candidates": [
                {
                    "candidate_id": "cand_a",
                    "kind": "requirement",
                    "abstraction": "no_opinion",
                    "statement": "use Python for the prototype",
                    "modality": "unspecified",
                    "scope": {"level": "global"},
                    "tags": [],
                    "evidence": [{
                        "source_event_id": "evt_a",
                        "source_revision_hash": "rh",
                        "role": "user",
                        "origin": "user_session",
                        "quote": "use Python for the prototype",
                        "span_start": 0,
                        "span_end": len("use Python for the prototype"),
                    }],
                },
                {
                    "candidate_id": "cand_b",
                    "kind": "requirement",
                    "abstraction": "no_opinion",
                    "statement": "use TypeScript for the prototype",
                    "modality": "unspecified",
                    "scope": {"level": "global"},
                    "tags": [],
                    "evidence": [{
                        "source_event_id": "evt_b",
                        "source_revision_hash": "rh",
                        "role": "user",
                        "origin": "user_session",
                        "quote": "use TypeScript for the prototype",
                        "span_start": 0,
                        "span_end": len("use TypeScript for the prototype"),
                    }],
                },
            ],
        })
        from memory_agent.application.reconcile import Reconciler
        from memory_agent.application.dispatch import _evidence_text_lookup

        # Use the PRODUCTION Reconciler.
        res = process_batch(
            batch_id=batch_id,
            vault=vault, conn=conn,
            provider=provider,
            reconciler=Reconciler(memory_projection_repo=_FakeRepo()),
            committer=CommitService(vault=vault, conn=conn),
            evidence_lookup=lambda eid, rh: _evidence_text_lookup(conn, eid, rh, vault),
            base_commit_id="root",
            privacy_epoch="epoch-0",
        )
        # The result must not yield two contradictory active memories.
        # We assert by reading the finalised HEAD manifest + the
        # memory_projection rows visible after the batch completes.
        head = json.loads(vault.head_path.read_text(encoding="utf-8"))
        manifest = json.loads(
            (vault.commits_dir / head["commit_id"] / "manifest.json")
            .read_text(encoding="utf-8")
        )
        # The number of published memories must be <= 1: at most one of
        # the contradictory candidates becomes active; the other goes
        # pending. (Or both go pending if the reconciler can't pick a
        # winner.)
        assert len(manifest["memories"]) <= 1
        # And the conflict_projection row is visible.
        proj_rows = conn.execute(
            "SELECT * FROM conflict_projection"
        ).fetchall()
        assert len(proj_rows) >= 1
        # Exactly one or two pending proposals; never zero if both
        # candidates were contradictory.
        assert len(manifest["pending_proposals"]) >= 1
        # Sanity: there must NOT be two memory_projection rows holding
        # the two contradictory statements.
        active = conn.execute(
            "SELECT statement FROM memory_projection WHERE status = 'active'"
        ).fetchall()
        stmts = [r["statement"] for r in active]
        assert not (
            "use Python for the prototype" in stmts
            and "use TypeScript for the prototype" in stmts
        ), (
            f"two contradictory active memories were both committed: {stmts}"
        )


# ---------------------------------------------------------------------------
# Invariant 7: duplicate abort removed; recover_unfinished idempotent
# ---------------------------------------------------------------------------


class TestRecoverUnfinishedCounters:
    def test_recovery_aborts_prepared_intent_exactly_once(self, tmp_path):
        """A prepared intent whose parent still matches HEAD must be
        ABORTED exactly once. The previous implementation called
        ``self.abort(...)`` and incremented ``aborted_count`` twice in
        the same branch — which inflated the counter and double-counted
        the work. This test pins the contract.
        """
        vault = VaultLayout(tmp_path / "vault")
        conn = open_state_db(vault.state_db)
        svc = CommitService(vault=vault, conn=conn)
        intent = svc.prepare_commit(
            proposed_memories=[_mem("m_dup", "dup")],
            pending_proposals=[],
            base_commit_id="root", privacy_epoch="epoch-0",
            batch_ids=["b_dup"], target_unit_ids=["u_dup"],
        )
        rec = svc.recover_unfinished()
        # One intent -> one abort (counter == 1, not 2).
        assert rec["aborted"] == 1, (
            f"recovery double-counted abort; aborted={rec['aborted']}"
        )
        # And the intent row reflects the single abort.
        phase = conn.execute(
            "SELECT phase FROM commit_intents WHERE commit_id = ?",
            (intent["commit_id"],),
        ).fetchone()
        assert phase["phase"] == "aborted"

    def test_recovery_idempotent_for_head_before_db_window(
        self, tmp_path
    ):
        """The HEAD-before-DB crash window: ``publish()`` swapped HEAD
        atomically, the caller crashed before the DB phase bump
        landed. The intent stays ``prepared`` while HEAD names its
        commit_id. Recovery must advance it to ``finalized`` exactly
        once — running ``recover_unfinished()`` a second time is a
        no-op (no duplicate projection, no duplicate audit row).
        """
        vault = VaultLayout(tmp_path / "vault")
        conn = open_state_db(vault.state_db)
        svc = CommitService(vault=vault, conn=conn)
        intent = svc.prepare_commit(
            proposed_memories=[_mem("m_hbdb", "head before db")],
            pending_proposals=[],
            base_commit_id="root", privacy_epoch="epoch-0",
            batch_ids=["b_hbdb"], target_unit_ids=["u_hbdb"],
        )
        # Simulate the crash: HEAD.json has been swapped to this
        # intent's commit_id, but the DB phase update was lost. The
        # intent row is still 'prepared'.
        head_tmp = vault.root / "HEAD.json.tmp"
        from memory_agent.infrastructure.files import atomic_write_json
        atomic_write_json(
            head_tmp,
            {"commit_id": intent["commit_id"], "parent_id": "root"},
        )
        head_tmp.replace(vault.head_path)
        # Confirm the simulated state.
        phase = conn.execute(
            "SELECT phase FROM commit_intents WHERE commit_id = ?",
            (intent["commit_id"],),
        ).fetchone()
        assert phase["phase"] == "prepared"
        head = json.loads(vault.head_path.read_text(encoding="utf-8"))
        assert head["commit_id"] == intent["commit_id"]

        # First recovery call: HEAD-before-DB window advanced to
        # finalized exactly once.
        rec1 = svc.recover_unfinished()
        assert rec1["advanced_to_published"] == 1, (
            f"recovery did not advance HEAD-before-DB intent to "
            f"published: {rec1}"
        )
        assert rec1["finalized"] == 1
        assert rec1["recovery_required"] == []
        # Phase is now 'finalized' in the DB.
        phase = conn.execute(
            "SELECT phase FROM commit_intents WHERE commit_id = ?",
            (intent["commit_id"],),
        ).fetchone()
        assert phase["phase"] == "finalized"
        # Projection row exists exactly once.
        proj = conn.execute(
            "SELECT * FROM memory_projection WHERE memory_id = 'm_hbdb'"
        ).fetchall()
        assert len(proj) == 1

        # Second recovery call: idempotent — no new advance, no new
        # finalize, no new audit/projection rows.
        audit_before = conn.execute(
            "SELECT COUNT(*) c FROM audit_events"
        ).fetchone()["c"]
        rec2 = svc.recover_unfinished()
        assert rec2["advanced_to_published"] == 0
        assert rec2["finalized"] == 0
        assert rec2["aborted"] == 0
        audit_after = conn.execute(
            "SELECT COUNT(*) c FROM audit_events"
        ).fetchone()["c"]
        assert audit_after == audit_before, (
            f"recovery wrote audit rows on the second pass: "
            f"{audit_before} -> {audit_after}"
        )
        proj = conn.execute(
            "SELECT * FROM memory_projection WHERE memory_id = 'm_hbdb'"
        ).fetchall()
        assert len(proj) == 1, (
            f"recovery duplicated projection rows on the second pass: "
            f"{len(proj)}"
        )

    def test_recovery_three_cases_separated(self, tmp_path):
        """Three prepared intents, three HEAD states. Each must take
        its own recovery branch. Because HEAD has exactly one value
        at recovery time, we run three independent recovery passes in
        three sub-vaults and assert HEAD, DB phase, projection row,
        and (where relevant) batch row for each case.

          * intent_a: HEAD == parent    -> aborted, batch retryable
          * intent_b: HEAD == commit    -> advanced to published, then finalized
          * intent_c: HEAD neither      -> recovery_required
        """
        from memory_agent.infrastructure.files import atomic_write_json

        # ----- case A: HEAD == parent (intent must be aborted) ----
        vault_a = VaultLayout(tmp_path / "vault_a")
        conn_a = open_state_db(vault_a.state_db)
        svc_a = CommitService(vault=vault_a, conn=conn_a)
        anchor_a = svc_a.prepare_commit(
            proposed_memories=[_mem("m_anchor_a", "anchor a")],
            pending_proposals=[],
            base_commit_id="root", privacy_epoch="epoch-0",
            batch_ids=["b_anchor_a"], target_unit_ids=["u_anchor_a"],
        )
        svc_a.publish(anchor_a["commit_id"])
        svc_a.finalize(anchor_a["commit_id"])
        intent_a = svc_a.prepare_commit(
            proposed_memories=[_mem("m_a", "branch a")],
            pending_proposals=[],
            base_commit_id=anchor_a["commit_id"],
            privacy_epoch="epoch-0",
            batch_ids=["b_a"], target_unit_ids=["u_a"],
        )
        # HEAD still names anchor_a (== intent_a.parent).
        rec_a = svc_a.recover_unfinished()
        assert rec_a["aborted"] == 1
        assert rec_a["advanced_to_published"] == 0
        assert rec_a["finalized"] == 0
        assert rec_a["recovery_required"] == []
        phase_a = conn_a.execute(
            "SELECT phase FROM commit_intents WHERE commit_id = ?",
            (intent_a["commit_id"],),
        ).fetchone()
        assert phase_a["phase"] == "aborted"
        proj_a = conn_a.execute(
            "SELECT * FROM memory_projection WHERE memory_id = 'm_a'"
        ).fetchall()
        assert proj_a == [], "intent_a must NOT publish a projection"
        head_a = json.loads(vault_a.head_path.read_text(encoding="utf-8"))
        assert head_a["commit_id"] == anchor_a["commit_id"]

        # ----- case B: HEAD == commit (HEAD-before-DB window) ------
        vault_b = VaultLayout(tmp_path / "vault_b")
        conn_b = open_state_db(vault_b.state_db)
        svc_b = CommitService(vault=vault_b, conn=conn_b)
        anchor_b = svc_b.prepare_commit(
            proposed_memories=[_mem("m_anchor_b", "anchor b")],
            pending_proposals=[],
            base_commit_id="root", privacy_epoch="epoch-0",
            batch_ids=["b_anchor_b"], target_unit_ids=["u_anchor_b"],
        )
        svc_b.publish(anchor_b["commit_id"])
        svc_b.finalize(anchor_b["commit_id"])
        intent_b = svc_b.prepare_commit(
            proposed_memories=[_mem("m_b", "branch b")],
            pending_proposals=[],
            base_commit_id=anchor_b["commit_id"],
            privacy_epoch="epoch-0",
            batch_ids=["b_b"], target_unit_ids=["u_b"],
        )
        # Simulate the crash: HEAD swapped to intent_b.commit_id,
        # DB phase update lost.
        head_tmp = vault_b.root / "HEAD.json.tmp"
        atomic_write_json(
            head_tmp,
            {"commit_id": intent_b["commit_id"],
             "parent_id": anchor_b["commit_id"]},
        )
        head_tmp.replace(vault_b.head_path)
        rec_b = svc_b.recover_unfinished()
        assert rec_b["advanced_to_published"] == 1
        assert rec_b["finalized"] == 1
        assert rec_b["aborted"] == 0
        assert rec_b["recovery_required"] == []
        phase_b = conn_b.execute(
            "SELECT phase FROM commit_intents WHERE commit_id = ?",
            (intent_b["commit_id"],),
        ).fetchone()
        assert phase_b["phase"] == "finalized"
        proj_b = conn_b.execute(
            "SELECT * FROM memory_projection WHERE memory_id = 'm_b'"
        ).fetchall()
        assert len(proj_b) == 1
        head_b = json.loads(vault_b.head_path.read_text(encoding="utf-8"))
        assert head_b["commit_id"] == intent_b["commit_id"]

        # ----- case C: HEAD neither (competing commit path) --------
        vault_c = VaultLayout(tmp_path / "vault_c")
        conn_c = open_state_db(vault_c.state_db)
        svc_c = CommitService(vault=vault_c, conn=conn_c)
        anchor_c = svc_c.prepare_commit(
            proposed_memories=[_mem("m_anchor_c", "anchor c")],
            pending_proposals=[],
            base_commit_id="root", privacy_epoch="epoch-0",
            batch_ids=["b_anchor_c"], target_unit_ids=["u_anchor_c"],
        )
        svc_c.publish(anchor_c["commit_id"])
        svc_c.finalize(anchor_c["commit_id"])
        intent_c = svc_c.prepare_commit(
            proposed_memories=[_mem("m_c", "branch c")],
            pending_proposals=[],
            base_commit_id=anchor_c["commit_id"],
            privacy_epoch="epoch-0",
            batch_ids=["b_c"], target_unit_ids=["u_c"],
        )
        # Simulate a competing commit: HEAD names some other commit,
        # not intent_c.parent or intent_c.commit_id.
        competing_id = "commit_competing_xxxxxxxxxx"
        head_tmp = vault_c.root / "HEAD.json.tmp"
        atomic_write_json(
            head_tmp,
            {"commit_id": competing_id,
             "parent_id": anchor_c["commit_id"]},
        )
        head_tmp.replace(vault_c.head_path)
        rec_c = svc_c.recover_unfinished()
        assert intent_c["commit_id"] in rec_c["recovery_required"]
        assert rec_c["aborted"] == 0
        assert rec_c["finalized"] == 0
        assert rec_c["advanced_to_published"] == 0
        phase_c = conn_c.execute(
            "SELECT phase FROM commit_intents WHERE commit_id = ?",
            (intent_c["commit_id"],),
        ).fetchone()
        # Recovery-required cases keep phase='prepared' so the
        # doctor surface can show them.
        assert phase_c["phase"] == "prepared"
        proj_c = conn_c.execute(
            "SELECT * FROM memory_projection WHERE memory_id = 'm_c'"
        ).fetchall()
        assert proj_c == [], "intent_c must NOT publish a projection"
        head_c = json.loads(vault_c.head_path.read_text(encoding="utf-8"))
        assert head_c["commit_id"] == competing_id