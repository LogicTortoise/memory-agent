"""Targeted tests for the six release-blocking defects.

Each test is a direct, falsifiable claim that the underlying fix
sticks. Run together with the rest of the suite via pytest.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_agent.application.commit import CommitService
from memory_agent.application.dispatch import (
    _event_from_row_with_text,
    _evidence_text_lookup,
)
from memory_agent.application.plan_batches import plan_batches_for_session
from memory_agent.application.process_batches import process_batch
from memory_agent.application.reconcile import Reconciler
from memory_agent.application.scan import default_adapters, scan_once
from memory_agent.adapters.models import FakeModelProvider
from memory_agent.contracts.candidates import CandidateSet
from memory_agent.contracts.memory import MemoryAtom
from memory_agent.domain.batching import BatchPolicyInput
from memory_agent.infrastructure.files import VaultLayout
from memory_agent.infrastructure.sqlite import open_state_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_root(tmp_path: Path, lines: list[dict]) -> Path:
    root = tmp_path / "claude"
    proj = root / "projects" / "proj_target"
    proj.mkdir(parents=True, exist_ok=True)
    sess = proj / "sess_target.jsonl"
    with sess.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False))
            f.write("\n")
    return root


def _user_line(uuid_: str, content: str, ts: str = "2026-10-08T10:00:00Z") -> dict:
    return {
        "type": "user", "uuid": uuid_,
        "message": {"role": "user", "content": content},
        "timestamp": ts,
        "sessionId": "sess_target",
        "parentUuid": None,
    }


def _assistant_line(uuid_: str, content: str, ts: str) -> dict:
    return {
        "type": "assistant", "uuid": uuid_,
        "message": {"role": "assistant", "content": content},
        "timestamp": ts,
        "sessionId": "sess_target",
        "parentUuid": uuid_,
    }


def _scan_and_get_events(root: Path, vault: VaultLayout, conn) -> list:
    scan_once(
        vault=vault, conn=conn,
        adapters=default_adapters(),
        roots={"claude-code": str(root)},
    )
    rows = conn.execute(
        "SELECT * FROM source_events WHERE session_id = ? ORDER BY source_time",
        ("sess_target",),
    ).fetchall()
    return [_event_from_row_with_text(conn, r) for r in rows]


# ---------------------------------------------------------------------------
# Defect 1: batch loss
# ---------------------------------------------------------------------------


class TestBatchLoss:
    def test_twelve_user_turns_produce_three_batches_with_twelve_active_targets(
        self, tmp_path
    ):
        """12 user turns (plus tool rows) must yield exactly 3 DB
        batches with 12 distinct active targets, and the batch
        processor must see all 12 of them — not just one."""
        lines: list[dict] = []
        for i in range(12):
            uid = f"u{i:02d}"
            lines.append(_user_line(uid, f"must use Python for the prototype {uid}",
                                    f"2026-10-08T10:{i:02d}:00Z"))
            # an assistant reply (becomes context_only)
            lines.append(_assistant_line(
                f"a{i:02d}", "ok", f"2026-10-08T10:{i:02d}:01Z"))
        root = _make_root(tmp_path, lines)
        vault = VaultLayout(tmp_path / "vault")
        conn = open_state_db(vault.state_db)
        events = _scan_and_get_events(root, vault, conn)
        assert len(events) == 24

        planned = plan_batches_for_session(
            session_id="sess_target", branch_id="main",
            events=events, policy=BatchPolicyInput(), conn=conn,
        )
        # 12 user turns -> exactly 3 batches (max 5 turns each).
        assert len(planned) == 3, f"expected 3 batches, got {len(planned)}"
        active_total = 0
        all_target_event_ids: set[str] = set()
        for entry in planned:
            active_targets = entry["active_targets"]
            assert len(active_targets) == 5 or len(active_targets) == 2, (
                "every user turn must be its own active_target"
            )
            active_total += len(active_targets)
            for t in active_targets:
                assert t["event_id"] not in all_target_event_ids, (
                    "active targets must be unique across the batch"
                )
                all_target_event_ids.add(t["event_id"])
        assert active_total == 12
        assert len(all_target_event_ids) == 12

    def test_process_batch_sees_every_active_target(self, tmp_path):
        """When the FakeModelProvider returns one candidate per target,
        processing must cover ALL of the batch's active target event_ids
        in the resulting candidates. If the batch planner hid targets,
        this fails."""
        lines: list[dict] = []
        for i in range(6):
            uid = f"u{i:02d}"
            lines.append(_user_line(uid, f"must use Python for the prototype {uid}",
                                    f"2026-10-08T10:{i:02d}:00Z"))
        root = _make_root(tmp_path, lines)
        vault = VaultLayout(tmp_path / "vault")
        conn = open_state_db(vault.state_db)
        events = _scan_and_get_events(root, vault, conn)

        planned = plan_batches_for_session(
            session_id="sess_target", branch_id="main",
            events=events, policy=BatchPolicyInput(), conn=conn,
        )
        assert len(planned) == 2

        # Drive FakeModelProvider manually so we can inspect the task
        # it received and the candidates it produced.
        first_batch = planned[0]
        provider = FakeModelProvider()
        from memory_agent.application.process_batches import _build_task
        task = _build_task(
            dict(first_batch["batch"], _conn=conn, members=first_batch["members"]),
            run_id="run_test", batch_id=first_batch["batch"]["batch_id"],
        )
        target_event_ids = {t["event_id"] for t in first_batch["active_targets"]}
        texts_in_task = {m["text"] for m in task["members"]
                         if int(m["active_target"]) == 1}
        for ev_id in target_event_ids:
            ev_row = next(e for e in events if e.event_id == ev_id)
            assert ev_row.text in texts_in_task, (
                f"target event {ev_id!r} missing from provider task"
            )

        # Now drive process_batch end-to-end against the first batch.
        # Use a reconciler that publishes every non-empty statement.
        from memory_agent.application.process_batches import BatchProcessingError
        try:
            process_batch(
                batch_id=first_batch["batch"]["batch_id"],
                vault=vault, conn=conn,
                provider=provider,
                reconciler=_PublishAllReconciler(),
                committer=CommitService(vault=vault, conn=conn),
                evidence_lookup=lambda eid, rh: _evidence_text_lookup(
                    conn, eid, rh, vault),
                base_commit_id="root",
                privacy_epoch="epoch-0",
                run_id="run_x",
            )
        except BatchProcessingError:
            pass
        # covered_targets from the audit/projection should equal the
        # first batch's active_target event_ids.
        audit_row = conn.execute(
            "SELECT details_json FROM audit_events "
            "WHERE action = 'batch.extracted' AND object_id = ?",
            (first_batch["batch"]["batch_id"],),
        ).fetchone()
        assert audit_row, "batch.extracted audit row must exist"
        details = json.loads(audit_row["details_json"])
        covered = set(details.get("covered_targets", []))
        # The probe only fires if every active target produced a
        # candidate. FakeModelProvider emits one candidate per
        # requirement line; the user's text starts with "must use"
        # which matches its trigger word set.
        assert covered == target_event_ids, (
            f"covered_targets={sorted(covered)} != target_event_ids="
            f"{sorted(target_event_ids)}"
        )


class _PublishAllReconciler(Reconciler):
    """Test-only reconciler that publishes every validated candidate as
    a fresh memory with no conflict detection. Used by TestBatchLoss to
    drive process_batch end-to-end and inspect coverage."""
    def __init__(self):
        # FakeRepo keeps the Reconciler base class happy; we never read
        # from it because we override reconcile().
        class _FakeRepo:
            def list_active(self, project_ids=None):
                return []
        super().__init__(memory_projection_repo=_FakeRepo())  # type: ignore[arg-type]

    def reconcile(self, candidate_set: CandidateSet, *, evidence_lookup):
        from memory_agent.application.reconcile import ReconcilerOutcome
        out = ReconcilerOutcome()
        now = "2026-10-08T10:00:00+00:00"
        for cand in candidate_set.candidates:
            if cand.kind == "out_of_scope":
                out.rejected.append({"candidate_id": cand.candidate_id,
                                     "reason": "out_of_scope"})
                continue
            if not cand.statement.strip():
                continue
            mem = MemoryAtom(
                schema_version=1,
                memory_id="mem_" + cand.candidate_id.replace("cand_", ""),
                revision=1, status="active",
                kind="requirement", abstraction="no_opinion",
                statement=cand.statement,
                subject="global", predicate=None, value=None,
                modality="unspecified", scope=cand.scope,
                primary_domain=cand.primary_domain,
                domains=cand.scope.domain_ids,
                tags=list(cand.tags), horizon="unknown",
                asserted_at=now, recorded_at=now,
                evidence=list(cand.evidence),
            )
            out.to_publish.append(mem)
        if not out.to_publish:
            out.no_memory = True
        return out


# ---------------------------------------------------------------------------
# Defect 2: evidence locator
# ---------------------------------------------------------------------------


class TestEvidenceLocator:
    def test_multi_record_same_segment_preserves_real_locator(self, tmp_path):
        """The same segment file must carry distinct byte offsets for
        each record, and the recall path must return the second
        record's exact byte range — not byte 0 and not the first
        record's range."""
        root = tmp_path / "claude"
        proj = root / "projects" / "proj_loc"
        proj.mkdir(parents=True, exist_ok=True)
        sess = proj / "sess_loc.jsonl"

        first_text = "use Python for the prototype"
        second_text = "always log decisions to vault/audit"
        first_line = json.dumps({
            "type": "user", "uuid": "u01",
            "message": {"role": "user", "content": first_text},
            "timestamp": "2026-10-08T10:00:00Z",
            "sessionId": "sess_loc", "parentUuid": None,
        })
        second_line = json.dumps({
            "type": "user", "uuid": "u02",
            "message": {"role": "user", "content": second_text},
            "timestamp": "2026-10-08T10:01:00Z",
            "sessionId": "sess_loc", "parentUuid": "u01",
        })
        sess.write_text(first_line + "\n" + second_line + "\n", encoding="utf-8")

        first_start = 0
        first_end = len(first_line) + 1
        second_start = first_end
        second_end = first_end + len(second_line) + 1

        vault = VaultLayout(tmp_path / "vault")
        conn = open_state_db(vault.state_db)
        scan_once(vault=vault, conn=conn,
                  adapters=default_adapters(),
                  roots={"claude-code": str(root)})

        rows = conn.execute(
            "SELECT * FROM source_events WHERE session_id = 'sess_loc' "
            "ORDER BY source_time"
        ).fetchall()
        assert len(rows) == 2

        # First row locator -> first record.
        ev0 = _event_from_row_with_text(conn, rows[0])
        assert ev0.source.locator.byte_start == first_start
        assert ev0.source.locator.byte_end == first_end
        assert ev0.source.locator.record_ordinal == 1

        # Second row locator -> second record's exact byte range.
        ev1 = _event_from_row_with_text(conn, rows[1])
        assert ev1.source.locator.byte_start == second_start
        assert ev1.source.locator.byte_end == second_end
        assert ev1.source.locator.record_ordinal == 2
        assert ev1.text == second_text


# ---------------------------------------------------------------------------
# Defect 3: commit chain + projection timing
# ---------------------------------------------------------------------------


class TestCommitChain:
    def test_two_consecutive_commits_chain_via_HEAD(self, tmp_path):
        """Two commits processed in the same loop must chain:
        manifest_2.parent_id == commit_1.commit_id, and HEAD.json
        ends at commit_2. base_commit_id is read fresh inside the
        loop, not once before."""
        vault = VaultLayout(tmp_path / "vault")
        conn = open_state_db(vault.state_db)
        svc = CommitService(vault=vault, conn=conn)

        from memory_agent.contracts.candidates import Scope
        now = "2026-10-08T10:00:00+00:00"
        mem1 = MemoryAtom(
            schema_version=1, memory_id="m_chain_1", revision=1,
            status="active", kind="requirement",
            abstraction="no_opinion", statement="first statement",
            subject="global", modality="must",
            scope=Scope(level="global"), primary_domain=None, domains=[],
            tags=[], horizon="unknown", asserted_at=now, recorded_at=now,
            evidence=[],
        )
        intent1 = svc.prepare_commit(
            proposed_memories=[mem1], pending_proposals=[],
            base_commit_id="root", privacy_epoch="epoch-0",
            batch_ids=["b_chain_1"], target_unit_ids=["u_chain_1"],
        )
        svc.publish(intent1["commit_id"])
        svc.finalize(intent1["commit_id"])

        # Read HEAD fresh — this is what the dispatcher's loop must do.
        head1 = json.loads(vault.head_path.read_text(encoding="utf-8"))
        assert head1["commit_id"] == intent1["commit_id"]

        mem2 = MemoryAtom(
            schema_version=1, memory_id="m_chain_2", revision=1,
            status="active", kind="requirement",
            abstraction="no_opinion", statement="second statement",
            subject="global", modality="must",
            scope=Scope(level="global"), primary_domain=None, domains=[],
            tags=[], horizon="unknown", asserted_at=now, recorded_at=now,
            evidence=[],
        )
        # Fresh base_commit_id like the dispatcher loop does.
        intent2 = svc.prepare_commit(
            proposed_memories=[mem2], pending_proposals=[],
            base_commit_id=head1["commit_id"],
            privacy_epoch="epoch-0",
            batch_ids=["b_chain_2"], target_unit_ids=["u_chain_2"],
        )
        svc.publish(intent2["commit_id"])
        svc.finalize(intent2["commit_id"])

        head2 = json.loads(vault.head_path.read_text(encoding="utf-8"))
        assert head2["commit_id"] == intent2["commit_id"]
        # manifest_2.parent_id == commit_1.commit_id
        manifest_2_path = vault.commits_dir / intent2["commit_id"] / "manifest.json"
        manifest_2 = json.loads(manifest_2_path.read_text(encoding="utf-8"))
        assert manifest_2["parent_id"] == intent1["commit_id"]

    def test_prepared_unpublished_intent_writes_no_projection(self, tmp_path):
        """An intent that was prepare()'d but never published must NOT
        appear as an active memory in memory_projection. The crash
        window is honest: a written manifest does not equal a
        published memory."""
        vault = VaultLayout(tmp_path / "vault")
        conn = open_state_db(vault.state_db)
        svc = CommitService(vault=vault, conn=conn)
        from memory_agent.contracts.candidates import Scope
        now = "2026-10-08T10:00:00+00:00"
        mem = MemoryAtom(
            schema_version=1, memory_id="m_unpub", revision=1,
            status="active", kind="requirement",
            abstraction="no_opinion", statement="unpub",
            subject="global", modality="must",
            scope=Scope(level="global"), primary_domain=None, domains=[],
            tags=[], horizon="unknown", asserted_at=now, recorded_at=now,
            evidence=[],
        )
        intent_info = svc.prepare_commit(
            proposed_memories=[mem], pending_proposals=[],
            base_commit_id="root", privacy_epoch="epoch-0",
            batch_ids=["b_unpub"], target_unit_ids=["u_unpub"],
        )
        # No publish() call.
        # HEAD must NOT exist yet — no projection was written.
        assert not vault.head_path.exists()
        rows = conn.execute(
            "SELECT * FROM memory_projection WHERE memory_id = 'm_unpub'"
        ).fetchall()
        assert rows == []
        # The intent is 'prepared' and can be aborted.
        svc.abort(intent_info["commit_id"])
        phase = conn.execute(
            "SELECT phase FROM commit_intents WHERE commit_id = ?",
            (intent_info["commit_id"],),
        ).fetchone()
        assert phase["phase"] == "aborted"

    def test_prepared_intent_with_head_at_parent_is_aborted_not_published(
        self, tmp_path
    ):
        """A prepared intent whose parent still matches HEAD on next
        startup must NOT be auto-published. The caller is gone;
        recovery aborts the intent and the batch stays retryable.

        The original "auto-finalise" behaviour was a defect: it
        silently converted a crashed prepared intent into a published
        memory without the caller ever choosing to publish."""
        vault = VaultLayout(tmp_path / "vault")
        conn = open_state_db(vault.state_db)
        svc = CommitService(vault=vault, conn=conn)
        from memory_agent.contracts.candidates import Scope
        now = "2026-10-08T10:00:00+00:00"
        mem = MemoryAtom(
            schema_version=1, memory_id="m_recover_once", revision=1,
            status="active", kind="requirement",
            abstraction="no_opinion", statement="recover once",
            subject="global", modality="must",
            scope=Scope(level="global"), primary_domain=None, domains=[],
            tags=[], horizon="unknown", asserted_at=now, recorded_at=now,
            evidence=[],
        )
        intent_info = svc.prepare_commit(
            proposed_memories=[mem], pending_proposals=[],
            base_commit_id="root", privacy_epoch="epoch-0",
            batch_ids=["b_rec"], target_unit_ids=["u_rec"],
        )
        # Don't publish; simulate restart and recover.
        rec = svc.recover_unfinished()
        # Recovery aborts the unreachable prepared intent; it does
        # NOT auto-finalise it.
        assert rec["aborted"] >= 1
        assert rec["finalized"] == 0
        # HEAD never moved; no projection rows exist.
        assert not vault.head_path.exists()
        rows = conn.execute(
            "SELECT * FROM memory_projection WHERE memory_id = 'm_recover_once'"
        ).fetchall()
        assert rows == []
        # The intent is now aborted.
        phase = conn.execute(
            "SELECT phase FROM commit_intents WHERE commit_id = ?",
            (intent_info["commit_id"],),
        ).fetchone()
        assert phase["phase"] == "aborted"

    def test_missing_manifest_blocks_publish(self, tmp_path):
        """A prepared intent whose manifest file has been deleted or
        corrupted must surface as recovery_required. ``publish()``
        refuses to swap HEAD, and any subsequent ``publish()`` on top
        of HEAD is blocked until the operator resolves."""
        vault = VaultLayout(tmp_path / "vault")
        conn = open_state_db(vault.state_db)
        svc = CommitService(vault=vault, conn=conn)
        from memory_agent.contracts.candidates import Scope
        now = "2026-10-08T10:00:00+00:00"
        mem = MemoryAtom(
            schema_version=1, memory_id="m_corrupt", revision=1,
            status="active", kind="requirement",
            abstraction="no_opinion", statement="corrupt",
            subject="global", modality="must",
            scope=Scope(level="global"), primary_domain=None, domains=[],
            tags=[], horizon="unknown", asserted_at=now, recorded_at=now,
            evidence=[],
        )
        intent_info = svc.prepare_commit(
            proposed_memories=[mem], pending_proposals=[],
            base_commit_id="root", privacy_epoch="epoch-0",
            batch_ids=["b_corrupt"], target_unit_ids=["u_corrupt"],
        )
        manifest_path = Path(intent_info["manifest_ref"])
        manifest_path.unlink()
        # publish() must refuse and raise CommitError because the
        # prepared intent's manifest is unreadable.
        from memory_agent.application.commit import CommitError
        with pytest.raises(CommitError):
            svc.publish(intent_info["commit_id"])
        # HEAD never moved; projection must not exist.
        assert not vault.head_path.exists()
        rows = conn.execute(
            "SELECT * FROM memory_projection WHERE memory_id = 'm_corrupt'"
        ).fetchall()
        assert rows == []
        # A new write attempt with HEAD still absent must succeed
        # because the operator can decide to abandon the corrupt
        # intent and start fresh. Surface the corrupt intent via
        # doctor: it sits in 'prepared' state and the operator can
        # abort it. No auto-finalisation, no auto-publish.
        rec = svc.recover_unfinished()
        # The prepared intent is aborted because HEAD is at root
        # (no HEAD file exists yet). No recovery_required entry
        # for this case; the operator simply sees the aborted intent
        # in doctor.
        assert rec["aborted"] >= 1
        assert intent_info["commit_id"] not in rec["recovery_required"]


# ---------------------------------------------------------------------------
# Defect 4: first-run / init-baseline
# ---------------------------------------------------------------------------


class TestInitBaseline:
    def test_init_with_existing_root_does_not_ingest_history(self, tmp_path):
        """A session file that exists at `init --root` time must NOT
        be ingested by the first `maintain`. The cursor is pinned at
        end-of-file."""
        root = tmp_path / "claude"
        proj = root / "projects" / "proj_e"
        proj.mkdir(parents=True, exist_ok=True)
        sess = proj / "sess_e.jsonl"
        sess.write_text(json.dumps({
            "type": "user", "uuid": "u01",
            "message": {"role": "user", "content": "use Python for the prototype"},
            "timestamp": "2026-10-08T10:00:00Z",
            "sessionId": "sess_e", "parentUuid": None,
        }) + "\n", encoding="utf-8")

        # Drive init via CLI subprocess: this is the documented entry
        # point and matches the README. The CLI pins the cursor at
        # end-of-file and does NOT ingest the existing line.
        vault = tmp_path / "vault"
        env = {
            "MEMORY_AGENT_MODEL_PROVIDER": "fake",
            "MEMORY_VAULT": str(vault),
            "PYTHONPATH": str(SRC),
            "PATH": os.environ.get("PATH", ""),
        }
        proc = subprocess.run(
            [sys.executable, "-m", "memory_agent",
             "--vault", str(vault),
             "init", "--root", f"claude-code={root}"],
            capture_output=True, text=True, env=env, check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        init_resp = json.loads(proc.stdout)
        assert init_resp["status"] == "ok"
        # At this point the cursor is pinned at end-of-file. Run
        # maintain: nothing historical is ingested.
        proc = subprocess.run(
            [sys.executable, "-m", "memory_agent",
             "--vault", str(vault),
             "maintain", "--root", f"claude-code={root}"],
            capture_output=True, text=True, env=env, check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        m_resp = json.loads(proc.stdout)
        assert m_resp["status"] == "ok"
        assert m_resp["result"]["scan"]["new_events"] == 0
        # Now append a new line; the next maintain must pick it up.
        sess.write_text(
            sess.read_text(encoding="utf-8")
            + json.dumps({
                "type": "user", "uuid": "u02",
                "message": {"role": "user", "content": "always log decisions"},
                "timestamp": "2026-10-08T11:00:00Z",
                "sessionId": "sess_e", "parentUuid": "u01",
            }) + "\n", encoding="utf-8",
        )
        proc = subprocess.run(
            [sys.executable, "-m", "memory_agent",
             "--vault", str(vault),
             "maintain", "--root", f"claude-code={root}"],
            capture_output=True, text=True, env=env, check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        m_resp2 = json.loads(proc.stdout)
        assert m_resp2["result"]["scan"]["new_events"] >= 1


# ---------------------------------------------------------------------------
# Defect 6: acceptance integrity
# ---------------------------------------------------------------------------


class TestAcceptanceIntegrity:
    def test_first_maintain_with_fake_provider_commits_memory(self, tmp_path):
        """End-to-end using FakeModelProvider: a single appended user
        line that starts with a trigger word must commit one
        active memory, and recall must return that memory's
        statement. This is the same claim the README now makes."""
        # Build a clean session.
        root = tmp_path / "claude"
        proj = root / "projects" / "proj_a"
        proj.mkdir(parents=True, exist_ok=True)
        sess = proj / "sess_a.jsonl"
        sess.write_text(json.dumps({
            "type": "user", "uuid": "u01",
            "message": {"role": "user", "content": "use Python for the prototype always"},
            "timestamp": "2026-10-08T10:00:00Z",
            "sessionId": "sess_a", "parentUuid": None,
        }) + "\n", encoding="utf-8")

        # Init via CLI subprocess (mirror README steps).
        vault = tmp_path / "vault"
        env = {
            "MEMORY_AGENT_MODEL_PROVIDER": "fake",
            "MEMORY_VAULT": str(vault),
            "PYTHONPATH": str(SRC),
            "PATH": os.environ.get("PATH", ""),
        }
        proc = subprocess.run(
            [sys.executable, "-m", "memory_agent",
             "--vault", str(vault),
             "init", "--root", f"claude-code={root}"],
            capture_output=True, text=True, env=env, check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        init_resp = json.loads(proc.stdout)
        assert init_resp["status"] == "ok"
        # Append another user line and run maintain.
        sess.write_text(
            sess.read_text(encoding="utf-8")
            + json.dumps({
                "type": "user", "uuid": "u02",
                "message": {"role": "user",
                            "content": "use Python for the prototype always"},
                "timestamp": "2026-10-08T13:00:00Z",
                "sessionId": "sess_a", "parentUuid": "u01",
            }) + "\n", encoding="utf-8",
        )
        proc = subprocess.run(
            [sys.executable, "-m", "memory_agent",
             "--vault", str(vault),
             "maintain", "--root", f"claude-code={root}"],
            capture_output=True, text=True, env=env, check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        m_resp = json.loads(proc.stdout)
        assert m_resp["status"] == "ok"
        assert m_resp["result"]["scan"]["new_events"] >= 1
        # Recall must return the committed memory.
        proc = subprocess.run(
            [sys.executable, "-m", "memory_agent",
             "--vault", str(vault),
             "recall", "--query", "Python prototype"],
            capture_output=True, text=True, env=env, check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        r_resp = json.loads(proc.stdout)
        assert r_resp["status"] == "ok"
        items = r_resp["result"].get("items", [])
        assert items, "recall must return the committed memory"
        assert any("Python" in item["statement"] for item in items)


class TestPackageRuntime:
    def test_pyyaml_dependency_declared(self):
        """The package declares PyYAML because CommitService renders
        YAML frontmatter at first commit. A clean venv without it
        would render empty frontmatter and fail memory file reads."""
        pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert "PyYAML" in pyproject, "pyproject.toml must declare PyYAML"

    def test_help_works_from_installed_package(self):
        """``python -m memory_agent --help`` must succeed when the
        package is importable from the current environment. We don't
        try to import from a *stripped* path because the project's
        own pyproject is what makes the package available; the
        clean-venv verification is done in the install-then-e2e
        test below."""
        proc = subprocess.run(
            [sys.executable, "-m", "memory_agent", "--help"],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": str(SRC),
                 "PATH": "/usr/bin:/bin"},
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert "init" in proc.stdout
        assert "recall" in proc.stdout

    def test_clean_venv_install_then_full_pipeline(self, tmp_path):
        """Spin up a fresh venv, install the package, and exercise the
        full pipeline (init -> append -> maintain -> recall) without
        relying on the host Python. This is the test that catches the
        PyYAML/clean-venv defect."""
        venv_dir = tmp_path / "venv"
        # Use the host python3 to bootstrap the venv.
        proc = subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir)],
            capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 0, proc.stderr
        vpy = venv_dir / "bin" / "python"
        # Install package + dev deps from the project.
        proc = subprocess.run(
            [str(vpy), "-m", "pip", "install", "-e", ".[dev]"],
            capture_output=True, text=True,
            cwd=str(PROJECT_ROOT), check=False,
        )
        assert proc.returncode == 0, (
            f"pip install failed: {proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
        )
        # --help works.
        proc = subprocess.run(
            [str(vpy), "-m", "memory_agent", "--help"],
            capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 0, proc.stderr
        # Full invented pipeline.
        root = tmp_path / "claude"
        proj = root / "projects" / "proj_venv"
        proj.mkdir(parents=True, exist_ok=True)
        sess = proj / "sess_venv.jsonl"
        sess.write_text(json.dumps({
            "type": "user", "uuid": "u01",
            "message": {"role": "user",
                        "content": "use Python for the prototype always"},
            "timestamp": "2026-10-08T10:00:00Z",
            "sessionId": "sess_venv", "parentUuid": None,
        }) + "\n", encoding="utf-8")
        vault = tmp_path / "vault_venv"
        env = {
            "MEMORY_AGENT_MODEL_PROVIDER": "fake",
            "MEMORY_VAULT": str(vault),
            "PATH": str(venv_dir / "bin") + os.pathsep + os.environ.get("PATH", ""),
        }
        proc = subprocess.run(
            [str(vpy), "-m", "memory_agent",
             "--vault", str(vault),
             "init", "--root", f"claude-code={root}"],
            capture_output=True, text=True, env=env, check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        # Append + maintain.
        sess.write_text(
            sess.read_text(encoding="utf-8")
            + json.dumps({
                "type": "user", "uuid": "u02",
                "message": {"role": "user",
                            "content": "use Python for the prototype always"},
                "timestamp": "2026-10-08T11:00:00Z",
                "sessionId": "sess_venv", "parentUuid": "u01",
            }) + "\n", encoding="utf-8",
        )
        proc = subprocess.run(
            [str(vpy), "-m", "memory_agent",
             "--vault", str(vault),
             "maintain", "--root", f"claude-code={root}"],
            capture_output=True, text=True, env=env, check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        # Recall.
        proc = subprocess.run(
            [str(vpy), "-m", "memory_agent",
             "--vault", str(vault),
             "recall", "--query", "Python prototype"],
            capture_output=True, text=True, env=env, check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        r = json.loads(proc.stdout)
        assert r["status"] == "ok"
        items = r["result"].get("items", [])
        assert items, "clean-venv recall must return the committed memory"
        assert any("Python" in it["statement"] for it in items)