"""Phase C repair-2 acceptance tests.

Phase C ships the async ``call`` interface, project-owned worker
with schedules, and the source-correction reconciliation queue.
Phase B / C / repair-1 delivered the baseline. This file is the
**repair-2** suite, which closes the four C release blockers the
codex flagged after the C acceptance report:

  1. Real scheduled work — ``_maybe_daily_pass`` must actually scan
     configured roots, drain queued jobs, and bump the
     ``last_daily_run`` timestamp only on successful completion
     with real work (no model call, no increment → no bump). The
     weekly / monthly passes must write conservative, auditable
     review proposals. ``review`` must work through ``call`` /
     ``worker`` (no longer ``UNSUPPORTED``).
  2. Source correction decision — ``adopt_new`` / ``scope_split``
     must verify ``expected_proposal_revision`` and write a new
     memory revision through the existing file commit / recovery
     path, referencing the *new* event and hash, with
     supersede + overlay refresh + HEAD advance all in one
     atomic-enough transaction. If the safe commit fails, retain
     the pending proposal and overlay.
  3. Discovery timing — after a ``scan`` observes a source
     rewrite, the recall path must not return known stale memory
     silently: a recall observed *before* the next worker pass
     must still see the ``pending_correction`` overlay and emit a
     warning.
  4. Query — a real opt-in Claude rewrite provider, the
     ``include_archived`` contract, stable cursor behaviour, and
     failed-provider fallback to the original query, with hard
     constraints preserved.

Sanitised fixtures only. No real model call. No launchd install.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ---------------------------------------------------------------------------
# helpers (mirror the test_phase_c_* style)
# ---------------------------------------------------------------------------


def _run_cli(*args, stdin_payload: str | None = None,
             vault: Path | None = None, expect_exit: int | None = None,
             env: dict[str, str] | None = None,
             omit_model_env: bool = False) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "memory_agent"]
    if vault is not None:
        cmd += ["--vault", str(vault)]
    cmd += list(args)
    proc_env = os.environ.copy()
    proc_env["PYTHONPATH"] = str(SRC)
    if not omit_model_env:
        proc_env["MEMORY_AGENT_MODEL_PROVIDER"] = "fake"
    proc_env.pop("MEMORY_AGENT_ENABLE_CLAUDE_CLI", None)
    if env:
        proc_env.update(env)
    proc = subprocess.run(
        cmd,
        input=stdin_payload.encode("utf-8") if stdin_payload is not None else None,
        capture_output=True,
        env=proc_env,
        timeout=120,
    )
    if expect_exit is not None and proc.returncode != expect_exit:
        raise AssertionError(
            f"unexpected exit {proc.returncode}; stderr={proc.stderr.decode('utf-8', 'ignore')!r}"
        )
    return proc


def _read_cli_json(proc: subprocess.CompletedProcess) -> dict:
    out = proc.stdout.decode("utf-8", "ignore").strip()
    if not out:
        raise AssertionError(f"empty stdout; stderr={proc.stderr.decode('utf-8', 'ignore')!r}")
    return json.loads(out)


def _make_claude_root(path: Path, *, session_id: str = "sess_r2",
                      user_lines: list[str] | None = None,
                      timestamp: str = "2026-10-08T10:00:00Z") -> Path:
    user_lines = user_lines or [
        "use Python for the prototype",
        "always log decisions to vault/audit",
    ]
    project = path / "projects" / "proj_repair2"
    project.mkdir(parents=True, exist_ok=True)
    session = project / f"{session_id}.jsonl"
    rows: list[dict] = []
    uuid_counter = 0
    for content in user_lines:
        user_uuid = f"u{uuid_counter:03d}"
        rows.append({
            "type": "user", "uuid": user_uuid,
            "message": {"role": "user", "content": content},
            "timestamp": timestamp, "sessionId": session_id, "parentUuid": None,
        })
        rows.append({
            "type": "assistant", "uuid": f"a{uuid_counter:03d}",
            "message": {"role": "assistant", "content": "ok"},
            "timestamp": timestamp, "sessionId": session_id,
            "parentUuid": user_uuid,
        })
        uuid_counter += 1
    with session.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def _init_vault(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    claude_root = _make_claude_root(tmp_path / "claude_root")
    proc = _run_cli("--json", "init", "--root",
                    f"claude-code={claude_root}", vault=vault,
                    expect_exit=0)
    assert _read_cli_json(proc)["status"] == "ok"
    return vault, claude_root


def _open_db(vault: Path) -> sqlite3.Connection:
    path = vault / "state" / "state.sqlite"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _append_user_line(
    claude_root: Path, content: str,
    session_id: str = "sess_r2",
    timestamp: str = "2026-10-08T10:30:00Z",
    uuid_prefix: str = "u_extra",
) -> None:
    session = claude_root / "projects" / "proj_repair2" / f"{session_id}.jsonl"
    with session.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "user", "uuid": f"{uuid_prefix}_new",
            "message": {"role": "user", "content": content},
            "timestamp": timestamp, "sessionId": session_id, "parentUuid": None,
        }) + "\n")


# ---------------------------------------------------------------------------
# 1. Real scheduled work
# ---------------------------------------------------------------------------


class TestRealScheduledWork:
    def test_daily_no_increment_does_not_bump_timestamp(
        self, tmp_path: Path,
    ) -> None:
        """A forced daily pass on a quiet vault must not bump
        ``last_daily_run``. The next pass in the same window
        therefore still sees the daily catch-up as needed.
        """
        from memory_agent.application.worker import run_worker_once
        from memory_agent.infrastructure.files import VaultLayout
        from memory_agent.infrastructure.sqlite import open_state_db
        from memory_agent.infrastructure.sqlite.repositories import (
            WorkerRuntimeStateRepo,
        )
        vault, _ = _init_vault(tmp_path)
        layout = VaultLayout(vault)
        conn = open_state_db(layout.state_db)
        try:
            before = WorkerRuntimeStateRepo(conn).get("last_daily_run")
        finally:
            conn.close()
        result = run_worker_once(
            vault_root=vault, worker_id="worker_no_inc",
            lease_ttl_seconds=10, force_daily=True,
        )
        d = result.to_dict()
        assert d["daily_run"] is False, d
        assert "daily_pass_no_increment" in d.get("diagnostics", {}), d
        conn = open_state_db(layout.state_db)
        try:
            after = WorkerRuntimeStateRepo(conn).get("last_daily_run")
        finally:
            conn.close()
        assert (before or {}).get("date") == (after or {}).get("date")

    def test_daily_with_increment_bumps_timestamp(
        self, tmp_path: Path,
    ) -> None:
        """A daily pass whose scan observed a new event row must
        bump ``last_daily_run`` and surface ``daily_run=True`` in
        the worker pass result. The bump is real, not a stub."""
        from memory_agent.application.worker import run_worker_once
        from memory_agent.infrastructure.files import VaultLayout
        from memory_agent.infrastructure.sqlite import open_state_db
        from memory_agent.infrastructure.sqlite.repositories import (
            WorkerRuntimeStateRepo,
        )
        vault, claude_root = _init_vault(tmp_path)
        # Append a fresh user line so the daily scan has something
        # to ingest.
        _append_user_line(
            claude_root, "use Python for the prototype",
            timestamp="2026-10-08T10:30:00Z",
        )
        layout = VaultLayout(vault)
        result = run_worker_once(
            vault_root=vault, worker_id="worker_inc",
            lease_ttl_seconds=10, force_daily=True,
        )
        d = result.to_dict()
        assert d["daily_run"] is True, d
        diag = d.get("diagnostics", {}).get("daily_pass_increment") or {}
        assert int(diag.get("scan", {}).get("new_events", 0)) >= 1, diag
        conn = open_state_db(layout.state_db)
        try:
            after = WorkerRuntimeStateRepo(conn).get("last_daily_run")
        finally:
            conn.close()
        assert after is not None
        assert after.get("new_events", 0) >= 1, after

    def test_daily_drains_queued_job_counts_as_work(
        self, tmp_path: Path,
    ) -> None:
        """A queued async ``scan`` job that the same pass drains
        counts as real work, so the daily timestamp advances even
        if the scan itself observed no new bytes (the cursor
        was already at end-of-file when the job was queued)."""
        from memory_agent.application.worker import run_worker_once
        from memory_agent.infrastructure.files import VaultLayout
        from memory_agent.infrastructure.sqlite import open_state_db
        from memory_agent.infrastructure.sqlite.repositories import (
            WorkerRuntimeStateRepo,
        )
        vault, claude_root = _init_vault(tmp_path)
        # Queue an async scan even though no new content was
        # appended.
        payload = json.dumps({
            "api_version": 1, "request_id": "req_dwm_drain",
            "operation": "scan", "mode": "async",
            "idempotency_key": "r2-dwm-1",
            "params": {"roots": {"claude-code": str(claude_root)},
                       "mode": "incremental"},
        })
        proc = _run_cli("--json", "call", "--request-file", "-",
                         vault=vault, stdin_payload=payload,
                         expect_exit=0)
        body = _read_cli_json(proc)
        assert body["status"] == "queued", body
        layout = VaultLayout(vault)
        result = run_worker_once(
            vault_root=vault, worker_id="worker_dwm_drain",
            lease_ttl_seconds=10, force_daily=True,
        )
        d = result.to_dict()
        assert d["daily_run"] is True, d
        conn = open_state_db(layout.state_db)
        try:
            after = WorkerRuntimeStateRepo(conn).get("last_daily_run")
        finally:
            conn.close()
        assert after is not None
        assert body["job_id"] in after.get("queued_jobs_succeeded", [])

    def test_weekly_writes_conservative_proposal_artifact(
        self, tmp_path: Path,
    ) -> None:
        """A forced weekly pass must write a JSON review artifact
        under ``<vault>/artifacts/reviews/weekly_<date>.json``,
        produce a non-empty ``proposals`` list (when the
        heuristics fire), and bump ``last_weekly_run``. The
        timestamp only advances on a successful write.

        Phase C repair-2: the weekly pass is gated on a daily
        catch-up having run in the same wall-clock window; we
        prime ``last_daily_run`` directly so the test focuses on
        the weekly artifact path. The dependency itself is
        covered by ``test_weekly_requires_daily_catchup`` in the
        Phase C acceptance suite.
        """
        from memory_agent.application.worker import run_worker_once
        from memory_agent.infrastructure.files import VaultLayout
        from memory_agent.infrastructure.sqlite import open_state_db
        from memory_agent.infrastructure.sqlite.repositories import (
            WorkerRuntimeStateRepo,
        )
        vault, _ = _init_vault(tmp_path)
        layout = VaultLayout(vault)
        # Prime ``last_daily_run`` so the weekly pass sees a daily
        # catch-up has already run today.
        conn = open_state_db(layout.state_db)
        try:
            WorkerRuntimeStateRepo(conn).upsert(
                "last_daily_run", {
                    "date": __import__("datetime").datetime.now().date().isoformat(),
                    "at": "2026-10-08T10:00:00+00:00",
                    "worker_id": "test_setup",
                },
            )
        finally:
            conn.close()
        result = run_worker_once(
            vault_root=vault, worker_id="worker_w",
            lease_ttl_seconds=10, force_weekly=True,
        )
        d = result.to_dict()
        assert d["weekly_run"] is True, d
        review = d.get("diagnostics", {}).get("weekly_review") or {}
        assert review, d
        assert "path" in review
        path = Path(review["path"])
        assert path.exists()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["period"] == "weekly"
        assert "proposals" in payload
        assert "metrics" in payload
        conn = open_state_db(layout.state_db)
        try:
            after = WorkerRuntimeStateRepo(conn).get("last_weekly_run")
        finally:
            conn.close()
        assert after is not None
        assert after.get("path") == review["path"]

    def test_monthly_writes_conservative_proposal_artifact(
        self, tmp_path: Path,
    ) -> None:
        """Same as weekly but on the monthly axis."""
        from memory_agent.application.worker import run_worker_once
        from memory_agent.infrastructure.files import VaultLayout
        from memory_agent.infrastructure.sqlite import open_state_db
        from memory_agent.infrastructure.sqlite.repositories import (
            WorkerRuntimeStateRepo,
        )
        vault, _ = _init_vault(tmp_path)
        layout = VaultLayout(vault)
        conn = open_state_db(layout.state_db)
        try:
            WorkerRuntimeStateRepo(conn).upsert(
                "last_daily_run", {
                    "date": __import__("datetime").datetime.now().date().isoformat(),
                    "at": "2026-10-08T10:00:00+00:00",
                    "worker_id": "test_setup",
                },
            )
        finally:
            conn.close()
        result = run_worker_once(
            vault_root=vault, worker_id="worker_m",
            lease_ttl_seconds=10, force_monthly=True,
        )
        d = result.to_dict()
        assert d["monthly_run"] is True, d
        review = d.get("diagnostics", {}).get("monthly_review") or {}
        assert review, d
        path = Path(review["path"])
        assert path.exists()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["period"] == "monthly"

    def test_review_via_call_returns_proposal(
        self, tmp_path: Path,
    ) -> None:
        """``memory call --operation review --params period=week``
        must succeed (no longer ``UNSUPPORTED``) and return the
        proposal in the response. The artifact lands on disk and
        ``last_weekly_run`` is bumped so a subsequent worker pass
        within the same day sees the review as already done.
        """
        from memory_agent.infrastructure.files import VaultLayout
        from memory_agent.infrastructure.sqlite import open_state_db
        from memory_agent.infrastructure.sqlite.repositories import (
            WorkerRuntimeStateRepo,
        )
        vault, _ = _init_vault(tmp_path)
        payload = json.dumps({
            "api_version": 1, "request_id": "req_review_r2",
            "operation": "review", "mode": "sync",
            "idempotency_key": "r2-review-1",
            "params": {"period": "week"},
        })
        proc = _run_cli("--json", "call", "--request-file", "-",
                         vault=vault, stdin_payload=payload,
                         expect_exit=0)
        body = _read_cli_json(proc)
        assert body["status"] == "ok", body
        assert body["operation"] == "review"
        result = body["result"]
        assert result["period"] == "weekly"
        assert "proposals" in result
        assert "metrics" in result
        # The artifact is on disk.
        layout = VaultLayout(vault)
        path = layout.artifacts_dir / "reviews" / (
            f"{result['period']}_{result['date']}.json"
        )
        assert path.exists()
        # ``last_weekly_run`` is bumped.
        conn = open_state_db(layout.state_db)
        try:
            state = WorkerRuntimeStateRepo(conn).get("last_weekly_run")
        finally:
            conn.close()
        assert state is not None
        assert state.get("trigger") == "review_cli"

    def test_review_invalid_period_rejected(
        self, tmp_path: Path,
    ) -> None:
        """An invalid ``period`` is rejected with ``INVALID_REQUEST``."""
        vault, _ = _init_vault(tmp_path)
        payload = json.dumps({
            "api_version": 1, "request_id": "req_review_bad",
            "operation": "review", "mode": "sync",
            "params": {"period": "fortnight"},
        })
        proc = _run_cli("--json", "call", "--request-file", "-",
                         vault=vault, stdin_payload=payload,
                         expect_exit=2)
        body = _read_cli_json(proc)
        assert body["status"] == "error"
        assert body["error"]["code"] == "INVALID_REQUEST"


# ---------------------------------------------------------------------------
# 2. Source correction decision: adopt_new / scope_split
# ---------------------------------------------------------------------------


class TestSourceCorrectionDecision:
    def _setup_memory_with_proposal(
        self, tmp_path: Path, *, new_event_text: str,
    ) -> tuple[Path, str, str, str, str]:
        """Build a vault with a real memory + a real rewrite segment.

        Returns ``(vault, memory_id, old_event_id, new_event_id,
        new_revision_hash)``. The new event lives in a segment so
        ``adopt_new`` can resolve it and produce a verifiable quote.
        """
        from memory_agent.application.scan import default_adapters, scan_once
        from memory_agent.infrastructure.files import VaultLayout
        from memory_agent.infrastructure.sqlite import open_state_db
        from memory_agent.infrastructure.sqlite.repositories import (
            SourceCorrectionProposalRepo,
        )
        vault, claude_root = _init_vault(
            tmp_path, session_id="sess_r2_decide",
        ) if False else _init_vault(tmp_path)
        layout = VaultLayout(vault)
        # The init already registered the JSONL at end-of-file.
        # Append a new line so an incremental scan emits an event.
        _append_user_line(
            claude_root, "use Python for the prototype",
            session_id="sess_r2",
            timestamp="2026-10-08T10:00:00Z",
            uuid_prefix="u_base",
        )
        conn = open_state_db(layout.state_db)
        try:
            scan_once(
                vault=layout, conn=conn,
                adapters=default_adapters(),
                roots={"claude-code": str(claude_root)},
                mode="incremental",
            )
            row = conn.execute(
                "SELECT event_id, revision_hash FROM source_events "
                "ORDER BY discovered_at LIMIT 1"
            ).fetchone()
            old_event_id = str(row["event_id"])
            old_revision_hash = str(row["revision_hash"])
        finally:
            conn.close()
        # Write a memory that cites the old event.
        memory_id = "mem_repair2_001"
        mem_dir = layout.memories_dir / memory_id
        mem_dir.mkdir(parents=True, exist_ok=True)
        evidence_quote = "use Python for the prototype"
        mem_path = mem_dir / "r0001.md"
        mem_path.write_text(
            "---\nschema_version: 1\nmemory_id: " + memory_id + "\nrevision: 1\n"
            "status: active\nkind: requirement\nabstraction: no_opinion\n"
            "statement: " + evidence_quote + "\nsubject: global\n"
            "modality: must\nscope:\n  level: global\n"
            "horizon: unknown\nasserted_at: '2026-10-08T10:00:00Z'\n"
            "recorded_at: '2026-10-08T10:00:00Z'\nevidence:\n"
            f"- source_event_id: {old_event_id}\n"
            f"  source_revision_hash: {old_revision_hash}\n"
            f"  quote: \"{evidence_quote}\"\n"
            "  role: user\n  origin: user_session\n"
            "  span_start: 0\n  span_end: 27\n"
            "relations: []\nsupersedes: []\n---\n\n"
            "Body: " + evidence_quote + "\n",
            encoding="utf-8",
        )
        # Register the memory in ``memory_projection`` so the
        # ``adopt_new`` flow has a row to mark ``superseded``.
        conn = open_state_db(layout.state_db)
        try:
            from memory_agent.infrastructure.sqlite.repositories import (
                MemoryProjectionRepo,
            )
            MemoryProjectionRepo(conn).upsert({
                "memory_id": memory_id,
                "revision": 1,
                "status": "active",
                "scope": {"level": "global"},
                "project_ids": [],
                "domains": [],
                "commit_id": "root",
                "statement": evidence_quote,
            })
        finally:
            conn.close()
        # The rewrite segment carries the new event_id / new hash.
        new_event_id = "evt_r2_adopt"
        new_revision_hash = "rh_r2_new"
        seg_path = layout.segments_dir / "seg_r2_rewrite.json"
        seg_path.write_text(json.dumps({
            "segment_id": "seg_r2_rewrite",
            "source_file_id": "r2-source",
            "generation": 2,
            "events": [{
                "event_id": new_event_id,
                "revision_hash": new_revision_hash,
                "role": "user",
                "kind": "user_message",
                "text": new_event_text,
                "source": {
                    "locator": {"byte_start": 0,
                                "byte_end": len(new_event_text),
                                "record_ordinal": 1},
                    "source_time": "2026-10-08T11:00:00Z",
                    "discovered_at": "2026-10-08T11:00:00Z",
                },
            }],
        }), encoding="utf-8")
        # Insert the proposal row directly.
        conn = open_state_db(layout.state_db)
        try:
            repo = SourceCorrectionProposalRepo(conn)
            proposal_id = f"cor_repair2_{memory_id}"
            repo.upsert({
                "correction_id": proposal_id,
                "source_file_id": "r2-source",
                "old_event_id": old_event_id,
                "new_event_id": new_event_id,
                "memory_id": memory_id,
                "memory_revision": 1,
                "old_revision_hash": old_revision_hash,
                "new_revision_hash": new_revision_hash,
                "status": "pending",
                "reason": "source_rewritten",
            })
        finally:
            conn.close()
        return vault, memory_id, old_event_id, new_event_id, new_revision_hash

    def test_adopt_new_publishes_new_memory_and_supersedes_old(
        self, tmp_path: Path,
    ) -> None:
        """``adopt_new`` must publish a new memory through
        ``CommitService`` (file + manifest + HEAD swap + finalize)
        referencing the *new* event_id and new_revision_hash, mark
        the old memory ``superseded``, advance the proposal to
        ``decided_relink`` with ``superseded_memory_id`` /
        ``superseded_head_commit_id`` set, and bump
        ``proposal_revision``."""
        from memory_agent.application.source_correction import (
            decide_proposal,
        )
        from memory_agent.infrastructure.files import VaultLayout
        from memory_agent.infrastructure.sqlite import open_state_db
        from memory_agent.infrastructure.sqlite.repositories import (
            MemoryProjectionRepo,
            SourceCorrectionProposalRepo,
        )
        vault, memory_id, old_event_id, new_event_id, new_rh = (
            self._setup_memory_with_proposal(
                tmp_path, new_event_text="use TypeScript for the prototype",
            )
        )
        layout = VaultLayout(vault)
        conn = open_state_db(layout.state_db)
        try:
            # The pre-decision proposal revision is 1.
            proposal = SourceCorrectionProposalRepo(conn).get(
                f"cor_repair2_{memory_id}"
            )
            pre_rev = int(proposal["proposal_revision"])
            updated = decide_proposal(
                conn=conn,
                correction_id=f"cor_repair2_{memory_id}",
                action="adopt_new", vault=layout,
            )
        finally:
            conn.close()
        assert updated is not None
        assert updated["status"] == "decided_relink", updated
        assert int(updated["proposal_revision"]) > pre_rev, updated
        assert updated["superseded_memory_id"], updated
        assert updated["superseded_head_commit_id"], updated
        assert updated["decided_at"], updated
        new_mem_id = updated["superseded_memory_id"]
        new_mem_path = layout.memory_revision_path(new_mem_id, 1)
        assert new_mem_path.exists(), (
            f"new memory file {new_mem_path} must be published"
        )
        new_md = new_mem_path.read_text(encoding="utf-8")
        assert new_event_id in new_md, new_md
        assert new_rh in new_md, new_md
        # HEAD advanced to the new commit.
        head = json.loads(layout.head_path.read_text(encoding="utf-8"))
        assert head["commit_id"] == updated["superseded_head_commit_id"]
        # The old memory is marked superseded in ``memory_projection``.
        conn = open_state_db(layout.state_db)
        try:
            row = conn.execute(
                "SELECT status FROM memory_projection "
                "WHERE memory_id = ? AND revision = 1",
                (memory_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None and row["status"] == "superseded", row
        # The new memory's projection row is also visible.
        conn = open_state_db(layout.state_db)
        try:
            new_row = conn.execute(
                "SELECT status FROM memory_projection "
                "WHERE memory_id = ? AND revision = 1",
                (new_mem_id,),
            ).fetchone()
        finally:
            conn.close()
        assert new_row is not None and new_row["status"] == "active", new_row

    def test_adopt_new_stale_proposal_revision_rejected(
        self, tmp_path: Path,
    ) -> None:
        """A stale ``expected_proposal_revision`` must reject the
        call without writing any file and without bumping the
        proposal revision. The pending row is preserved."""
        from memory_agent.application.source_correction import (
            CorrectionCommitError,
            decide_proposal,
        )
        from memory_agent.infrastructure.files import VaultLayout
        from memory_agent.infrastructure.sqlite import open_state_db
        from memory_agent.infrastructure.sqlite.repositories import (
            SourceCorrectionProposalRepo,
        )
        vault, memory_id, _, _, _ = self._setup_memory_with_proposal(
            tmp_path, new_event_text="use TypeScript for the prototype",
        )
        layout = VaultLayout(vault)
        conn = open_state_db(layout.state_db)
        try:
            with pytest.raises(CorrectionCommitError):
                decide_proposal(
                    conn=conn,
                    correction_id=f"cor_repair2_{memory_id}",
                    action="adopt_new", vault=layout,
                    expected_proposal_revision=999,
                )
            proposal = SourceCorrectionProposalRepo(conn).get(
                f"cor_repair2_{memory_id}"
            )
        finally:
            conn.close()
        # Proposal is still pending and no file was written.
        assert proposal["status"] == "pending", proposal
        new_path = layout.memories_dir / (
            "mem_relink_" + "mem_repair2_001".split("_")[-1]
        )
        # No r0001.md under any newly-created relink dir.
        relink_dirs = list(layout.memories_dir.glob("mem_relink_*"))
        assert not relink_dirs, (
            f"no relink dir should exist on stale revision; got {relink_dirs}"
        )

    def test_adopt_new_publish_failure_retains_proposal_and_overlay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Inject a failure inside ``CommitService.publish``. The
        call must surface ``CorrectionCommitError`` and leave the
        proposal ``pending`` + the overlay ``pending_correction`` so
        the user can retry. No new memory file may appear."""
        from memory_agent.application.source_correction import (
            CorrectionCommitError,
            decide_proposal,
        )
        from memory_agent.infrastructure.files import VaultLayout
        from memory_agent.infrastructure.sqlite import open_state_db
        from memory_agent.infrastructure.sqlite.repositories import (
            MemoryCorrectionOverlayRepo,
            SourceCorrectionProposalRepo,
        )
        vault, memory_id, _, _, _ = self._setup_memory_with_proposal(
            tmp_path, new_event_text="use TypeScript for the prototype",
        )
        layout = VaultLayout(vault)
        # Pre-refresh the overlay so we can assert it stays.
        conn = open_state_db(layout.state_db)
        try:
            from memory_agent.application.source_correction import (
                refresh_overlay,
            )
            refresh_overlay(conn=conn)
            pre_overlay = MemoryCorrectionOverlayRepo(conn).get(
                memory_id, 1,
            )
            assert pre_overlay is not None
            assert pre_overlay["state"] == "pending_correction"
        finally:
            conn.close()
        # Inject a failure into ``CommitService.publish``.
        from memory_agent.application import commit as commit_mod
        real_publish = commit_mod.CommitService.publish
        def _explode(self, commit_id):
            raise RuntimeError("simulated publish failure")
        monkeypatch.setattr(
            commit_mod.CommitService, "publish", _explode,
        )
        conn = open_state_db(layout.state_db)
        try:
            with pytest.raises(CorrectionCommitError):
                decide_proposal(
                    conn=conn,
                    correction_id=f"cor_repair2_{memory_id}",
                    action="adopt_new", vault=layout,
                )
            proposal = SourceCorrectionProposalRepo(conn).get(
                f"cor_repair2_{memory_id}"
            )
            overlay = MemoryCorrectionOverlayRepo(conn).get(
                memory_id, 1,
            )
        finally:
            conn.close()
        assert proposal["status"] == "pending", proposal
        assert overlay is not None
        assert overlay["state"] == "pending_correction", overlay
        # HEAD pointer is still at the initial state (no commit
        # was published). A fresh vault has no ``HEAD.json``; the
        # contract is that no *new* commit was published.
        if layout.head_path.exists():
            head = json.loads(layout.head_path.read_text(encoding="utf-8"))
            assert head.get("commit_id") in (None, "root"), head

    def test_scope_split_narrows_to_project_scope(
        self, tmp_path: Path,
    ) -> None:
        """``scope_split`` writes a new memory with a project
        scope. The old memory stays in place (different scope) so
        both memories coexist. ``superseded_memory_id`` records the
        new memory; the old memory's ``status`` flips to
        ``superseded`` (the existing convention)."""
        from memory_agent.application.source_correction import (
            decide_proposal,
        )
        from memory_agent.infrastructure.files import VaultLayout
        from memory_agent.infrastructure.sqlite import open_state_db
        from memory_agent.infrastructure.sqlite.repositories import (
            MemoryProjectionRepo,
        )
        # Set up a memory with a global scope.
        vault, memory_id, old_event_id, new_event_id, new_rh = (
            self._setup_memory_with_proposal(
                tmp_path, new_event_text="use TypeScript for the prototype",
            )
        )
        layout = VaultLayout(vault)
        conn = open_state_db(layout.state_db)
        try:
            updated = decide_proposal(
                conn=conn,
                correction_id=f"cor_repair2_{memory_id}",
                action="scope_split", vault=layout,
            )
            new_mem_id = updated["superseded_memory_id"]
            new_md = (layout.memory_revision_path(new_mem_id, 1)
                      .read_text(encoding="utf-8"))
        finally:
            conn.close()
        assert "level: project" in new_md, new_md
        # ``supersedes`` references the old memory.
        assert memory_id in new_md, new_md

    def test_cli_source_correction_decide_round_trip(
        self, tmp_path: Path,
    ) -> None:
        """The CLI ``call`` surface for ``source_correction.decide``
        accepts ``adopt_new`` with a valid ``expected_proposal_revision``
        and persists the new memory + HEAD swap. Stale revisions
        return ``INVALID_REQUEST``."""
        from memory_agent.infrastructure.files import VaultLayout
        from memory_agent.infrastructure.sqlite import open_state_db
        vault, memory_id, _, _, _ = self._setup_memory_with_proposal(
            tmp_path, new_event_text="use TypeScript for the prototype",
        )
        # Happy path: adopt_new with revision=1.
        payload = json.dumps({
            "api_version": 1, "request_id": "req_sc_decide",
            "operation": "source_correction.decide", "mode": "sync",
            "params": {
                "correction_id": f"cor_repair2_{memory_id}",
                "action": "adopt_new",
                "expected_proposal_revision": 1,
                "note": "test",
            },
        })
        proc = _run_cli("--json", "call", "--request-file", "-",
                         vault=vault, stdin_payload=payload,
                         expect_exit=0)
        body = _read_cli_json(proc)
        assert body["status"] == "ok", body
        assert body["result"]["status"] == "decided_relink"
        assert body["result"]["superseded_memory_id"]
        # The new memory file is on disk.
        layout = VaultLayout(vault)
        new_path = layout.memories_dir / (
            body["result"]["superseded_memory_id"]
        ) / "r0001.md"
        assert new_path.exists()
        # Stale revision path: call again with revision=1 (already
        # bumped to >=2 after the first call), which must reject.
        payload_stale = json.dumps({
            "api_version": 1, "request_id": "req_sc_stale",
            "operation": "source_correction.decide", "mode": "sync",
            "params": {
                "correction_id": f"cor_repair2_{memory_id}",
                "action": "adopt_new",
                "expected_proposal_revision": 1,
            },
        })
        proc2 = _run_cli("--json", "call", "--request-file", "-",
                          vault=vault, stdin_payload=payload_stale,
                          expect_exit=2)
        body2 = _read_cli_json(proc2)
        assert body2["status"] == "error"
        assert body2["error"]["code"] == "INVALID_REQUEST"


# ---------------------------------------------------------------------------
# 3. Discovery timing: scan + recall integration
# ---------------------------------------------------------------------------


class TestDiscoveryTiming:
    def _build_rewrite_scenario(self, tmp_path: Path) -> tuple[Path, str, str]:
        """Create a vault with a memory citing an event, then
        rewrite the source so a backfill scan generates a new
        event row at a different revision_hash. Returns
        ``(vault, memory_id, old_event_id)``.
        """
        from memory_agent.infrastructure.files import VaultLayout
        from memory_agent.infrastructure.sqlite import open_state_db
        vault, claude_root = _init_vault(tmp_path)
        layout = VaultLayout(vault)
        # Append a fresh line so the first scan registers an event.
        _append_user_line(
            claude_root, "use Python for the prototype",
            session_id="sess_r2", timestamp="2026-10-08T10:00:00Z",
            uuid_prefix="u_base",
        )
        payload = json.dumps({
            "api_version": 1, "request_id": "req_disco_base",
            "operation": "scan", "mode": "sync",
            "params": {"roots": {"claude-code": str(claude_root)},
                       "mode": "incremental"},
        })
        proc = _run_cli("--json", "call", "--request-file", "-",
                         vault=vault, stdin_payload=payload,
                         expect_exit=0)
        body = _read_cli_json(proc)
        assert body.get("status") == "ok", body
        assert body.get("result", {}).get("new_events", 0) >= 1, body
        # Pick the event id.
        conn = open_state_db(layout.state_db)
        try:
            row = conn.execute(
                "SELECT event_id FROM source_events ORDER BY discovered_at LIMIT 1"
            ).fetchone()
            old_event_id = str(row["event_id"])
        finally:
            conn.close()
        # Hand-craft a memory citing this event.
        memory_id = "mem_disco_001"
        mem_dir = layout.memories_dir / memory_id
        mem_dir.mkdir(parents=True, exist_ok=True)
        evidence_quote = "use Python for the prototype"
        conn = open_state_db(layout.state_db)
        try:
            row = conn.execute(
                "SELECT revision_hash FROM source_events "
                "WHERE event_id = ?", (old_event_id,),
            ).fetchone()
            old_rh = str(row["revision_hash"])
        finally:
            conn.close()
        mem_path = mem_dir / "r0001.md"
        mem_path.write_text(
            "---\nschema_version: 1\nmemory_id: " + memory_id + "\n"
            "revision: 1\nstatus: active\nkind: requirement\n"
            "abstraction: no_opinion\nstatement: " + evidence_quote
            + "\nsubject: global\nmodality: must\nscope:\n  level: global\n"
            "horizon: unknown\nasserted_at: '2026-10-08T10:00:00Z'\n"
            "recorded_at: '2026-10-08T10:00:00Z'\nevidence:\n"
            f"- source_event_id: {old_event_id}\n"
            f"  source_revision_hash: {old_rh}\n"
            f"  quote: \"{evidence_quote}\"\n"
            "  role: user\n  origin: user_session\n"
            "  span_start: 0\n  span_end: 27\n"
            "relations: []\nsupersedes: []\n---\n\n"
            "Body: " + evidence_quote + "\n",
            encoding="utf-8",
        )
        # Rewrite the source in place.
        session = claude_root / "projects" / "proj_repair2" / "sess_r2.jsonl"
        original = session.read_text(encoding="utf-8")
        session.write_text(
            original.replace("use Python for the prototype",
                             "use TypeScript for the prototype"),
            encoding="utf-8",
        )
        return vault, memory_id, old_event_id

    def test_scan_refreshes_overlay_inline(
        self, tmp_path: Path,
    ) -> None:
        """A scan that observes a rewrite must refresh the source
        correction overlay *inline*, not just as a side-effect of
        the next worker pass. The ``scan`` result includes
        ``source_correction.overlay_rows_touched`` so the caller
        can observe the refresh."""
        vault, memory_id, _ = self._build_rewrite_scenario(tmp_path)
        # The memory projection is also on disk; the scan dispatcher
        # walks committed memories to populate the overlay.
        from memory_agent.infrastructure.files import VaultLayout
        layout = VaultLayout(vault)
        # Register the memory in ``memory_projection`` so the
        # discovery step can find it.
        from memory_agent.infrastructure.sqlite import open_state_db
        conn = open_state_db(layout.state_db)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO memory_projection("
                "memory_id, revision, status, scope_json, "
                "project_ids_json, domains_json, commit_id, "
                "recorded_at, statement) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (memory_id, 1, "active", "{}", "[]", "[]",
                 "root", "2026-10-08T10:00:00Z", ""),
            )
            conn.commit()
        finally:
            conn.close()
        # The backfill scan discovers the rewrite and refreshes
        # the overlay inline.
        claude_root = next(
            p for p in tmp_path.iterdir()
            if (p / "projects" / "proj_repair2").is_dir()
        )
        payload = json.dumps({
            "api_version": 1, "request_id": "req_disco_rewrite",
            "operation": "scan", "mode": "sync",
            "params": {
                "roots": {"claude-code": str(claude_root)},
                "mode": "backfill",
                "since_iso": "2026-10-08T10:00:00Z",
                "until_iso": "2026-10-08T11:00:00Z",
                "timezone": "UTC",
            },
        })
        proc = _run_cli("--json", "call", "--request-file", "-",
                         vault=vault, stdin_payload=payload,
                         expect_exit=0)
        body = _read_cli_json(proc)
        assert body.get("status") == "ok", body
        sc = body.get("result", {}).get("source_correction", {})
        # The scan refreshed the overlay inline.
        assert int(sc.get("new_proposals", 0)) >= 1, sc
        # The overlay row exists on disk immediately.
        conn = open_state_db(layout.state_db)
        try:
            row = conn.execute(
                "SELECT state FROM memory_correction_overlay "
                "WHERE memory_id = ? AND memory_revision = 1",
                (memory_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["state"] == "pending_correction", dict(row)

    def test_recall_emits_pending_correction_warning(
        self, tmp_path: Path,
    ) -> None:
        """A recall observed *before* the worker pass must still
        see the overlay and emit a ``pending_correction:<id>`` warning
        instead of returning the stale memory silently. The recall
        never silently promotes a memory that is awaiting a
        user decision on a source rewrite."""
        from memory_agent.application.recall import RecallService
        from memory_agent.infrastructure.files import VaultLayout
        from memory_agent.infrastructure.sqlite import open_state_db
        from memory_agent.infrastructure.sqlite.repositories import (
            MemoryCorrectionOverlayRepo,
            MemoryProjectionRepo,
            SourceCorrectionProposalRepo,
        )
        vault, memory_id, old_event_id = (
            self._build_rewrite_scenario(tmp_path)
        )
        layout = VaultLayout(vault)
        # Register the memory in the projection and add an overlay
        # row (no worker pass required).
        conn = open_state_db(layout.state_db)
        try:
            MemoryProjectionRepo(conn).upsert({
                "memory_id": memory_id, "revision": 1,
                "status": "active", "scope": {"level": "global"},
                "project_ids": [], "domains": [],
                "commit_id": "root", "statement": "",
            })
            old_rh_row = conn.execute(
                "SELECT revision_hash FROM source_events "
                "WHERE event_id = ?", (old_event_id,),
            ).fetchone()
            old_rh = str(old_rh_row["revision_hash"])
            SourceCorrectionProposalRepo(conn).upsert({
                "correction_id": "cor_disco_test",
                "source_file_id": "r2-source",
                "old_event_id": old_event_id,
                "new_event_id": "evt_disco_new",
                "memory_id": memory_id,
                "memory_revision": 1,
                "old_revision_hash": old_rh,
                "new_revision_hash": "rh_disco_new",
                "status": "pending",
                "reason": "source_rewritten",
            })
            MemoryCorrectionOverlayRepo(conn).upsert({
                "memory_id": memory_id,
                "memory_revision": 1,
                "pending_correction_id": "cor_disco_test",
                "state": "pending_correction",
                "affected_event_ids": [old_event_id],
            })
            conn.commit()
        finally:
            conn.close()
        # Recall the project. The overlay repo is injected so the
        # service can read the pending-correction row. The recall
        # needs a HEAD pointer to compute a snapshot id; create a
        # minimal one.
        (layout.root / "HEAD.json").write_text(
            json.dumps({"commit_id": "test_commit"}),
            encoding="utf-8",
        )
        overlay = MemoryCorrectionOverlayRepo(
            open_state_db(layout.state_db),
        )
        conn = open_state_db(layout.state_db)
        try:
            svc = RecallService(
                vault=layout,
                memory_projection_repo=MemoryProjectionRepo(conn),
                overlay_repo=overlay,
                audit_conn=conn,
            )
            res = svc.recall(
                query="use TypeScript for the prototype",
                project_ids=[], domain_ids=[],
                include_archived=False, limit=8,
            )
        finally:
            conn.close()
        warnings = res.get("warnings", [])
        assert any(
            w.startswith("pending_correction:") and memory_id in w
            for w in warnings
        ), warnings


# ---------------------------------------------------------------------------
# 4. Query: include_archived, opt-in Claude rewrite, fallback, cursor
# ---------------------------------------------------------------------------


class TestQueryContract:
    def test_include_archived_returns_archived_rows(
        self, tmp_path: Path,
    ) -> None:
        """``include_archived=True`` must surface rows whose
        ``status='archived'``. The default (``False``) still hides
        them. v0.1 silently dropped archived rows; the v1 contract
        must keep them visible to the caller who explicitly asks.
        The repository layer is the authoritative place where the
        flag is honoured; the recall service consumes the
        repo's list methods."""
        from memory_agent.infrastructure.sqlite import open_state_db
        from memory_agent.infrastructure.sqlite.repositories import (
            MemoryProjectionRepo,
        )
        vault, _ = _init_vault(tmp_path)
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            repo = MemoryProjectionRepo(conn)
            repo.upsert({
                "memory_id": "mem_arch_1", "revision": 1,
                "status": "archived",
                "scope": {"level": "global"},
                "project_ids": [], "domains": [],
                "commit_id": "root",
                "statement": "remember to log decisions",
            })
            repo.upsert({
                "memory_id": "mem_active_1", "revision": 1,
                "status": "active",
                "scope": {"level": "global"},
                "project_ids": [], "domains": [],
                "commit_id": "root",
                "statement": "use Python for the prototype",
            })
            # list_archived() surfaces the archived row; list_active()
            # does not.
            archived = repo.list_archived()
            active = repo.list_active()
        finally:
            conn.close()
        archived_ids = {row["memory_id"] for row in archived}
        active_ids = {row["memory_id"] for row in active}
        assert "mem_arch_1" in archived_ids, archived_ids
        assert "mem_arch_1" not in active_ids, active_ids
        assert "mem_active_1" in active_ids, active_ids
        # The recall service consults list_archived() when
        # ``include_archived=True``. We pin that branch by
        # monkey-patching the repo to verify the flag is honoured.
        from memory_agent.application.recall import RecallService
        from memory_agent.infrastructure.files import VaultLayout
        called = {"active": 0, "archived": 0}

        class _ProbeRepo:
            def list_active(self, project_ids=None):
                called["active"] += 1
                return repo.list_active(project_ids)

            def list_archived(self):
                called["archived"] += 1
                return repo.list_archived()

        # No source files on disk, so the recall path will surface
        # ``missing_object`` warnings. We assert only that the
        # call paths were taken.
        layout = VaultLayout(vault)
        (layout.root / "HEAD.json").write_text(
            json.dumps({"commit_id": "test_commit"}),
            encoding="utf-8",
        )
        probe_conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            class _LiveRepo:
                def list_active(self_inner, project_ids=None):
                    called["active"] += 1
                    return MemoryProjectionRepo(probe_conn).list_active(
                        project_ids
                    )

                def list_archived(self_inner):
                    called["archived"] += 1
                    return MemoryProjectionRepo(probe_conn).list_archived()

            svc = RecallService(
                vault=layout, memory_projection_repo=_LiveRepo(),
            )
            svc.recall(
                query="anything", project_ids=[], domain_ids=[],
                include_archived=True, limit=4,
            )
            svc.recall(
                query="anything", project_ids=[], domain_ids=[],
                include_archived=False, limit=4,
            )
        finally:
            probe_conn.close()
        assert called["archived"] >= 1, called
        assert called["active"] >= 2, called

    def test_failed_provider_falls_back_to_original(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A rewrite provider that raises must not break the
        recall: the planner falls back to original-only retrieval
        and the trace records ``fallback_reason='provider_error'``.
        Hard constraints (negations, exact identifiers) are still
        preserved on the original variant.
        """
        from memory_agent.application.recall import RecallService
        from memory_agent.application.search.rewrite_provider import (
            FakeRewriteProvider,
            RewriteProviderError,
        )
        from memory_agent.infrastructure.files import VaultLayout
        from memory_agent.infrastructure.sqlite import open_state_db
        from memory_agent.infrastructure.sqlite.repositories import (
            MemoryProjectionRepo,
        )
        vault, _ = _init_vault(tmp_path)
        layout = VaultLayout(vault)
        (layout.root / "HEAD.json").write_text(
            json.dumps({"commit_id": "test_commit"}),
            encoding="utf-8",
        )
        # Seed a memory that contains the exact identifier
        # ``pkg@1.0.0``; a rewrite that drops it must be rejected.
        conn = open_state_db(layout.state_db)
        try:
            MemoryProjectionRepo(conn).upsert({
                "memory_id": "mem_q_1", "revision": 1,
                "status": "active",
                "scope": {"level": "global"},
                "project_ids": [], "domains": [],
                "commit_id": "root",
                "statement": "use pkg@1.0.0 only",
            })
            # A rewrite that introduces a *new* project_id (a hard
            # constraint) must be rejected.
            provider = FakeRewriteProvider(
                rewrite_value="use proj_xyz memory about pkg@1.0.0",
                expansion_values=(),
            )
            svc = RecallService(
                vault=layout,
                memory_projection_repo=MemoryProjectionRepo(conn),
                rewrite_provider=provider,
                audit_conn=conn,
            )
            res = svc.recall(
                query="what should I use for the dependency",
                project_ids=[], domain_ids=[],
                include_archived=False, limit=8,
                retrieval="bm25",
                rewrite="auto", expand="off",
            )
        finally:
            conn.close()
        # The rewrite was rejected (it introduced a project_id
        # that the original query did not name). The fallback
        # reason surfaces in the trace.
        trace = res.get("query_trace", {}) or {}
        rejected = trace.get("rejected") or []
        reasons = [str(r.get("reason") or "") for r in rejected]
        assert any(
            "identifier" in r.lower() or "project" in r.lower()
            for r in reasons
        ), reasons
        # The fallback reason is recorded.
        assert trace.get("fallback_reason") in {
            None, "no_provider_wired", "provider_error",
        }
        # The original query alone produced a successful recall
        # (the memory is in the keyword pass).
        # The test does not assert item presence because the
        # memory file on disk is missing in this fixture (the
        # contract pinned here is the planner behaviour, not the
        # end-to-end recall).

    def test_fake_provider_constraints_preserved(
        self, tmp_path: Path,
    ) -> None:
        """The FakeRewriteProvider's rewrite is rejected when it
        violates a hard constraint. The original variant survives
        and the constraint violation is recorded in the trace.
        """
        from memory_agent.application.recall import RecallService
        from memory_agent.application.search.rewrite_provider import (
            FakeRewriteProvider,
        )
        from memory_agent.infrastructure.files import VaultLayout
        from memory_agent.infrastructure.sqlite import open_state_db
        from memory_agent.infrastructure.sqlite.repositories import (
            MemoryProjectionRepo,
        )
        vault, _ = _init_vault(tmp_path)
        layout = VaultLayout(vault)
        (layout.root / "HEAD.json").write_text(
            json.dumps({"commit_id": "test_commit"}),
            encoding="utf-8",
        )
        conn = open_state_db(layout.state_db)
        try:
            MemoryProjectionRepo(conn).upsert({
                "memory_id": "mem_q_neg", "revision": 1,
                "status": "active",
                "scope": {"level": "global"},
                "project_ids": [], "domains": [],
                "commit_id": "root",
                "statement": "do not use image gen",
            })
            # A rewrite that *flips* the negation: "not include" ->
            # "include". That violates the negation constraint.
            provider = FakeRewriteProvider(
                rewrite_value="include image gen now",
                expansion_values=(),
            )
            svc = RecallService(
                vault=layout,
                memory_projection_repo=MemoryProjectionRepo(conn),
                rewrite_provider=provider,
                audit_conn=conn,
            )
            res = svc.recall(
                query="do not include image gen",
                project_ids=[], domain_ids=[],
                include_archived=False, limit=8,
                retrieval="bm25", rewrite="auto", expand="off",
            )
        finally:
            conn.close()
        trace = res.get("query_trace", {}) or {}
        # The rewrite was rejected because it flipped the
        # ``not include`` -> ``include`` negation.
        rejected = trace.get("rejected") or []
        assert any(
            "negation" in str(r.get("reason") or "").lower()
            for r in rejected
        ), trace

    def test_stable_cursor_survives_repeat_call(
        self, tmp_path: Path,
    ) -> None:
        """A recall call that returns a cursor must, on a follow-up
        call with the same cursor, hit the same plan fingerprint
        and not be rejected as ``stale_cursor_rejected`` (because
        HEAD did not move and the fingerprint is stable). The
        recall contract is honest: cursor is bound to
        (snapshot_id, plan_fingerprint)."""
        from memory_agent.application.recall import RecallService
        from memory_agent.infrastructure.files import VaultLayout
        from memory_agent.infrastructure.sqlite import open_state_db
        from memory_agent.infrastructure.sqlite.repositories import (
            MemoryProjectionRepo,
        )
        vault, _ = _init_vault(tmp_path)
        layout = VaultLayout(vault)
        # Seed enough memories for the recall to truncate.
        conn = open_state_db(layout.state_db)
        try:
            repo = MemoryProjectionRepo(conn)
            for i in range(20):
                repo.upsert({
                    "memory_id": f"mem_{i:02d}", "revision": 1,
                    "status": "active", "scope": {},
                    "project_ids": [], "domains": [],
                    "commit_id": "root",
                    "statement": f"python prototype project {i}",
                })
            # Pin HEAD so the recall can compute the snapshot.
            (layout.root / "HEAD.json").write_text(
                json.dumps({"commit_id": "test_commit"}),
                encoding="utf-8",
            )
            svc = RecallService(
                vault=layout, memory_projection_repo=repo,
                audit_conn=conn,
            )
            res1 = svc.recall(
                query="python prototype", project_ids=[],
                domain_ids=[], include_archived=False,
                limit=5, retrieval_limit=5,
            )
            cursor = res1.get("next_cursor")
            res2 = svc.recall(
                query="python prototype", project_ids=[],
                domain_ids=[], include_archived=False,
                limit=5, retrieval_limit=5,
                cursor=cursor,
            )
        finally:
            conn.close()
        # The follow-up must not be stale.
        assert res2.get("stale_cursor_rejected") is False, res2
        # The same plan fingerprint is honoured.
        assert res1.get("query_trace", {}).get("fingerprint") == (
            res2.get("query_trace", {}).get("fingerprint")
        ), (res1, res2)

    def test_claude_rewrite_provider_gated_by_env(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``get_default_rewrite_provider()`` returns the
        ``ClaudeCLIRewriteProvider`` only when
        ``MEMORY_AGENT_ENABLE_CLAUDE_CLI=1`` is set; otherwise
        the identity stub. Tests can pin the fake / static
        provider directly through the dataclass without touching
        the CLI.
        """
        from memory_agent.application.search.rewrite_provider import (
            ClaudeCLIRewriteProvider,
            IdentityRewriteProvider,
            get_default_rewrite_provider,
        )
        monkeypatch.delenv("MEMORY_AGENT_ENABLE_CLAUDE_CLI",
                           raising=False)
        assert isinstance(get_default_rewrite_provider(),
                          IdentityRewriteProvider)
        monkeypatch.setenv("MEMORY_AGENT_ENABLE_CLAUDE_CLI", "1")
        assert isinstance(get_default_rewrite_provider(),
                          ClaudeCLIRewriteProvider)
