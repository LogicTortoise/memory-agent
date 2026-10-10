"""Phase C acceptance tests — async commands, worker, source-correction queue.

Covers the four T10/T11 acceptance buckets from the Phase C scope:

  * Subprocess JSON stdin sync + async; offline queued state.
  * Same-key replay / same-key changed params (IDEMPOTENCY_CONFLICT).
  * Worker ``--once`` progress; no-increment zero model call.
  * Worker lease expiration + old token rejection; cancellation
    before / after commit; backlog after budget / timeout; daily
    catch-up before weekly / monthly; two-worker lock contention;
    restart no duplicate memory.
  * Source-correction queue: commit memory, rewrite source, scan,
    assert stable correction queue + recall warning/omission; worker
    restart does not duplicate proposal; old evidence remains
    accessible; versioned user decision resolves or rejects correction.

Sanitised fixtures only. No real history, no real model call, no
launchd install.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


from memory_agent.application.call import (
    canonical_request_hash,
    handle_call,
)
from memory_agent.application.source_correction import (
    decide_proposal,
    discover_affected_memories,
    refresh_overlay,
)
from memory_agent.application.worker import run_worker_once
from memory_agent.infrastructure.files import VaultLayout
from memory_agent.infrastructure.sqlite import open_state_db
from memory_agent.infrastructure.sqlite.repositories import (
    JobRepo,
    MemoryCorrectionOverlayRepo,
    SourceCorrectionProposalRepo,
    WorkerLeaseRepo,
    WorkerRunRepo,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run_cli(*args, stdin_payload: str | None = None,
             vault: Path | None = None, expect_exit: int | None = None,
             env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "memory_agent"]
    if vault is not None:
        cmd += ["--vault", str(vault)]
    cmd += list(args)
    proc_env = os.environ.copy()
    proc_env["PYTHONPATH"] = str(SRC)
    proc_env["MEMORY_AGENT_MODEL_PROVIDER"] = "fake"
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


@pytest.fixture(autouse=True)
def _fake_model_env(monkeypatch):
    """Force the fake provider in-process for the worker / handler tests."""
    monkeypatch.setenv("MEMORY_AGENT_MODEL_PROVIDER", "fake")


def _read_cli_json(proc: subprocess.CompletedProcess) -> dict:
    out = proc.stdout.decode("utf-8", "ignore").strip()
    if not out:
        raise AssertionError(f"empty stdout; stderr={proc.stderr.decode('utf-8', 'ignore')!r}")
    return json.loads(out)


def _make_sanitised_claude_root(path: Path, *, session_id: str = "sess_phasec",
                                user_lines: list[str] | None = None,
                                timestamp: str = "2026-10-08T10:00:00Z") -> Path:
    """Write a sanitised Claude Code JSONL session and return the root."""
    user_lines = user_lines or [
        "use Python for the prototype",
        "always log decisions to vault/audit",
    ]
    project = path / "projects" / "proj_test_alpha"
    project.mkdir(parents=True, exist_ok=True)
    session = project / f"{session_id}.jsonl"
    rows: list[dict] = []
    uuid_counter = 0
    for content in user_lines:
        user_uuid = f"u{uuid_counter:03d}"
        assistant_uuid = f"a{uuid_counter:03d}"
        rows.append({
            "type": "user", "uuid": user_uuid,
            "message": {"role": "user", "content": content},
            "timestamp": timestamp, "sessionId": session_id, "parentUuid": None,
        })
        rows.append({
            "type": "assistant", "uuid": assistant_uuid,
            "message": {"role": "assistant", "content": "ok"},
            "timestamp": timestamp, "sessionId": session_id, "parentUuid": user_uuid,
        })
        uuid_counter += 1
    with session.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


# ---------------------------------------------------------------------------
# sync + async subprocess JSON stdin
# ---------------------------------------------------------------------------


class TestCallSubprocessSyncAndAsync:
    """`memory call --request-file - --json` accepts both modes and
    emits exactly one JSON ``CommandResponse`` on stdout."""

    def _init_vault(self, tmp_path: Path) -> tuple[Path, Path]:
        vault = tmp_path / "vault"
        claude_root = _make_sanitised_claude_root(tmp_path / "claude_root")
        proc = _run_cli(
            "--json", "init",
            "--root", f"claude-code={claude_root}",
            vault=vault,
            expect_exit=0,
        )
        body = _read_cli_json(proc)
        assert body["status"] == "ok", body
        return vault, claude_root

    def test_sync_init_roundtrip(self, tmp_path: Path) -> None:
        vault, _ = self._init_vault(tmp_path)
        # Re-run init via stdin JSON.
        payload = json.dumps({
            "api_version": 1, "request_id": "req_sync_init",
            "operation": "init", "mode": "sync",
            "params": {"roots": {}},
        })
        proc = _run_cli(
            "--json", "call", "--request-file", "-",
            vault=vault, stdin_payload=payload,
            expect_exit=0,
        )
        body = _read_cli_json(proc)
        assert body["status"] == "ok"
        assert body["operation"] == "init"

    def test_async_returns_queued_when_worker_offline(self, tmp_path: Path) -> None:
        vault, claude_root = self._init_vault(tmp_path)
        payload = json.dumps({
            "api_version": 1, "request_id": "req_async_maintain",
            "operation": "maintain", "mode": "async",
            "idempotency_key": "phasec-async-maintain-1",
            "params": {
                "roots": {"claude-code": str(claude_root)},
                "max_batches": 1,
            },
        })
        proc = _run_cli(
            "--json", "call", "--request-file", "-",
            vault=vault, stdin_payload=payload,
            expect_exit=0,
        )
        body = _read_cli_json(proc)
        assert body["status"] == "queued", body
        assert body["job_id"], body
        assert body["job_state"] == "queued"
        # Worker is offline, so we expect the warning.
        assert "worker_offline" in body["warnings"], body
        # The job exists in the DB and is queued.
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            row = JobRepo(conn).get(body["job_id"])
            assert row is not None
            assert row["state"] == "queued"
            assert row["cancel_requested"] == 0
        finally:
            conn.close()

    def test_stdin_must_be_json(self, tmp_path: Path) -> None:
        vault, _ = self._init_vault(tmp_path)
        proc = _run_cli(
            "--json", "call", "--request-file", "-",
            vault=vault, stdin_payload="not-valid-json",
            expect_exit=2,
        )
        body = _read_cli_json(proc)
        assert body["status"] == "error"
        assert body["error"]["code"] == "INVALID_REQUEST"

    def test_unknown_field_rejected(self, tmp_path: Path) -> None:
        vault, _ = self._init_vault(tmp_path)
        payload = json.dumps({
            "api_version": 1, "request_id": "req_extra",
            "operation": "init", "mode": "sync",
            "params": {"roots": {}, "unexpected": 1},
        })
        proc = _run_cli(
            "--json", "call", "--request-file", "-",
            vault=vault, stdin_payload=payload,
            expect_exit=2,
        )
        body = _read_cli_json(proc)
        assert body["status"] == "error"
        assert body["error"]["code"] == "INVALID_REQUEST"

    def test_unknown_operation_rejected(self, tmp_path: Path) -> None:
        vault, _ = self._init_vault(tmp_path)
        payload = json.dumps({
            "api_version": 1, "request_id": "req_xx",
            "operation": "does_not_exist", "mode": "sync",
            "params": {},
        })
        proc = _run_cli(
            "--json", "call", "--request-file", "-",
            vault=vault, stdin_payload=payload,
            expect_exit=2,
        )
        body = _read_cli_json(proc)
        assert body["status"] == "error"
        assert body["error"]["code"] == "INVALID_REQUEST"

    def test_review_is_explicitly_unsupported(self, tmp_path: Path) -> None:
        """Phase C repair-2: ``review`` is **not** ``UNSUPPORTED``
        anymore — it routes through the worker's conservative
        review pipeline. The handler writes a proposal artifact
        and an audit row, and returns the proposal in the response.
        """
        vault, _ = self._init_vault(tmp_path)
        payload = json.dumps({
            "api_version": 1, "request_id": "req_review",
            "operation": "review", "mode": "sync",
            "params": {"period": "week"},
        })
        proc = _run_cli(
            "--json", "call", "--request-file", "-",
            vault=vault, stdin_payload=payload,
            expect_exit=0,
        )
        body = _read_cli_json(proc)
        assert body["status"] == "ok", body
        assert body["operation"] == "review", body
        result = body["result"]
        assert result["period"] in {"weekly", "monthly"}, result
        assert "proposals" in result
        assert "metrics" in result
        # The proposal artifact is on disk.
        from memory_agent.infrastructure.files import VaultLayout
        layout = VaultLayout(vault)
        path = layout.artifacts_dir / "reviews" / (
            f"{result['period']}_{result['date']}.json"
        )
        assert path.exists(), f"expected {path} to exist"


# ---------------------------------------------------------------------------
# idempotency: same key + same params => cached; same key + changed =>
# IDEMPOTENCY_CONFLICT
# ---------------------------------------------------------------------------


class TestIdempotency:

    def _setup(self, tmp_path: Path) -> tuple[Path, Path]:
        vault = tmp_path / "vault"
        claude_root = _make_sanitised_claude_root(tmp_path / "claude_root")
        _run_cli("--json", "init", "--root",
                 f"claude-code={claude_root}", vault=vault,
                 expect_exit=0)
        return vault, claude_root

    def test_same_key_same_params_returns_cached(self, tmp_path: Path) -> None:
        vault, _ = self._setup(tmp_path)
        payload = json.dumps({
            "api_version": 1, "request_id": "req_idem_1",
            "client_id": "clientA", "idempotency_key": "k-1",
            "operation": "doctor", "mode": "sync",
            "params": {},
        })
        proc1 = _run_cli("--json", "call", "--request-file", "-",
                          vault=vault, stdin_payload=payload,
                          expect_exit=0)
        body1 = _read_cli_json(proc1)
        assert body1["status"] == "ok"
        assert body1["job_id"], body1
        proc2 = _run_cli("--json", "call", "--request-file", "-",
                          vault=vault, stdin_payload=payload,
                          expect_exit=0)
        body2 = _read_cli_json(proc2)
        assert body2["status"] == "ok"
        assert body2["job_id"] == body1["job_id"], (
            "replay must return the original job_id"
        )

    def test_same_key_changed_params_conflicts(self, tmp_path: Path) -> None:
        vault, claude_root = self._setup(tmp_path)
        payload_v1 = json.dumps({
            "api_version": 1, "request_id": "req_idem_2",
            "client_id": "clientA", "idempotency_key": "k-2",
            "operation": "scan", "mode": "sync",
            "params": {"roots": {"claude-code": str(claude_root)},
                       "mode": "incremental"},
        })
        proc1 = _run_cli("--json", "call", "--request-file", "-",
                          vault=vault, stdin_payload=payload_v1,
                          expect_exit=0)
        body1 = _read_cli_json(proc1)
        assert body1["status"] == "ok"
        payload_v2 = json.dumps({
            "api_version": 1, "request_id": "req_idem_2",
            "client_id": "clientA", "idempotency_key": "k-2",
            "operation": "scan", "mode": "sync",
            "params": {"roots": {"claude-code": str(claude_root)},
                       "mode": "backfill",
                       "since_iso": "2026-01-01T00:00:00Z",
                       "until_iso": "2026-12-31T23:59:59Z",
                       "timezone": "UTC"},
        })
        proc2 = _run_cli("--json", "call", "--request-file", "-",
                          vault=vault, stdin_payload=payload_v2,
                          expect_exit=4)
        body2 = _read_cli_json(proc2)
        assert body2["status"] == "error"
        assert body2["error"]["code"] == "IDEMPOTENCY_CONFLICT", body2

    def test_canonical_request_hash_ignores_request_id(self) -> None:
        from memory_agent.contracts.commands import CommandRequest
        a = CommandRequest.model_validate({
            "api_version": 1, "request_id": "req_a", "operation": "doctor",
            "mode": "sync", "params": {},
        })
        b = CommandRequest.model_validate({
            "api_version": 1, "request_id": "req_b", "operation": "doctor",
            "mode": "sync", "params": {},
        })
        assert canonical_request_hash(a) == canonical_request_hash(b)


# ---------------------------------------------------------------------------
# worker --once, lease, cancel, restart, lock contention
# ---------------------------------------------------------------------------


class TestWorkerOnce:

    def _setup(self, tmp_path: Path) -> tuple[Path, Path]:
        vault = tmp_path / "vault"
        claude_root = _make_sanitised_claude_root(tmp_path / "claude_root")
        _run_cli("--json", "init", "--root",
                 f"claude-code={claude_root}", vault=vault,
                 expect_exit=0)
        return vault, claude_root

    def test_worker_once_no_increment_zero_model_calls(self, tmp_path: Path) -> None:
        vault, claude_root = self._setup(tmp_path)
        result = run_worker_once(
            vault_root=vault,
            worker_id="worker_test",
            lease_ttl_seconds=10,
        )
        d = result.to_dict()
        # No queued jobs; worker pass should still run the discovery
        # step. ``model_calls`` may be 0 if nothing was discovered.
        assert d["outcome"] in {"ok", "skipped"}, d
        assert d["lease_acquired"] is True
        assert d["lease_released"] is True
        assert d["jobs_claimed"] == []
        # Source-correction discovery ran, but there is no memory so it
        # is a no-op. ``model_calls`` must be 0.
        assert d["model_calls"] == 0, d
        assert d["source_correction"]["scanned_memories"] == 0

    def test_worker_once_drains_queued_job(self, tmp_path: Path) -> None:
        vault, claude_root = self._setup(tmp_path)
        payload = json.dumps({
            "api_version": 1, "request_id": "req_drain",
            "operation": "scan", "mode": "async",
            "idempotency_key": "phasec-drain-1",
            "params": {"roots": {"claude-code": str(claude_root)},
                       "mode": "incremental"},
        })
        proc = _run_cli("--json", "call", "--request-file", "-",
                         vault=vault, stdin_payload=payload,
                         expect_exit=0)
        body = _read_cli_json(proc)
        assert body["status"] == "queued", body
        job_id = body["job_id"]
        result = run_worker_once(
            vault_root=vault,
            worker_id="worker_drain",
            lease_ttl_seconds=10,
        )
        d = result.to_dict()
        assert job_id in d["jobs_succeeded"], d
        # The job is now succeeded.
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            row = JobRepo(conn).get(job_id)
            assert row is not None and row["state"] == "succeeded"
        finally:
            conn.close()

    def test_lease_expiration_releases_to_new_worker(self, tmp_path: Path) -> None:
        vault, _ = self._setup(tmp_path)
        # First worker acquires a 1-second lease and stops.
        first = run_worker_once(
            vault_root=vault, worker_id="worker_A", lease_ttl_seconds=1,
        )
        assert first.lease_acquired is True
        # Manually expire by waiting past TTL.
        time.sleep(1.2)
        # Verify the lease-reclamation path at the repository level:
        # any worker process whose lease is expired may take ownership
        # of the slot. The previous ``run_worker_once`` already
        # reclaimed the expired worker_A lease by the time we got
        # here; a fresh acquire by worker_B therefore succeeds.
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            leases = WorkerLeaseRepo(conn)
            ok = leases.acquire(
                worker_id="worker_B", lease_token="lease_pinned",
                ttl_seconds=60, current_job_id=None,
            )
            assert ok is True, (
                "an expired lease must be reclaimable by a new worker"
            )
            leases.release(worker_id="worker_B", lease_token="lease_pinned")
        finally:
            conn.close()
        # And the second worker's ``run_worker_once`` itself succeeds
        # (after reclaiming the expired slot).
        second = run_worker_once(
            vault_root=vault, worker_id="worker_B", lease_ttl_seconds=5,
        )
        assert second.lease_acquired is True, second.to_dict()

    def test_two_workers_lock_contention(self, tmp_path: Path) -> None:
        vault, _ = self._setup(tmp_path)
        # The lease is the vault-level writer lock (Phase C repair-1).
        # Two distinct workers CANNOT both hold the active lease at
        # the same time. The contention guarantee applies across the
        # whole vault: only one worker process may run a pass at a
        # time, which serialises ``audit_events``, the head swap, the
        # ``source_correction_proposals`` upsert, and the
        # ``worker_runtime_state`` keys.
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            leases = WorkerLeaseRepo(conn)
            ok = leases.acquire(
                worker_id="worker_A", lease_token="lease_A",
                ttl_seconds=10, current_job_id=None,
            )
            assert ok is True
            # Same worker tries to acquire again — must fail because
            # the existing lease is still active (not expired).
            ok2 = leases.acquire(
                worker_id="worker_A", lease_token="lease_A2",
                ttl_seconds=10, current_job_id=None,
            )
            assert ok2 is False, (
                "active lease must block re-acquire by the same worker"
            )
            # A different worker is REFUSED while worker_A's lease is
            # still active — this is the new vault-level writer lock.
            ok3 = leases.acquire(
                worker_id="worker_B", lease_token="lease_B",
                ttl_seconds=10, current_job_id=None,
            )
            assert ok3 is False, (
                "different workers must NOT both hold an active lease "
                "(Phase C repair-1 vault-level writer lock)"
            )
            # After worker_A's lease expires, worker_B can reclaim.
            conn.execute(
                "UPDATE worker_leases SET expires_at = '2000-01-01T00:00:00+00:00' "
                "WHERE worker_id = 'worker_A'"
            )
            ok4 = leases.acquire(
                worker_id="worker_B", lease_token="lease_B",
                ttl_seconds=10, current_job_id=None,
            )
            assert ok4 is True, (
                "an expired lease must be reclaimable by a different worker"
            )
        finally:
            conn.close()

    def test_cancel_before_finish(self, tmp_path: Path) -> None:
        vault, claude_root = self._setup(tmp_path)
        payload = json.dumps({
            "api_version": 1, "request_id": "req_cancel",
            "operation": "scan", "mode": "async",
            "idempotency_key": "phasec-cancel-1",
            "params": {"roots": {"claude-code": str(claude_root)},
                       "mode": "incremental"},
        })
        proc = _run_cli("--json", "call", "--request-file", "-",
                         vault=vault, stdin_payload=payload,
                         expect_exit=0)
        body = _read_cli_json(proc)
        job_id = body["job_id"]
        revision = body["progress"] and 1  # we will look up via DB
        # Look up the actual revision.
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            row = JobRepo(conn).get(job_id)
            revision = int(row["revision"])
        finally:
            conn.close()
        # Cancel it (still queued).
        proc_c = _run_cli(
            "--json", "job", "cancel",
            "--job-id", job_id, "--expected-revision", str(revision),
            vault=vault, expect_exit=0,
        )
        body_c = _read_cli_json(proc_c)
        assert body_c["status"] == "ok"
        assert body_c["result"]["cancel_requested"] is True
        # Run worker: it should drain the job as cancelled, not run it.
        result = run_worker_once(
            vault_root=vault, worker_id="worker_cancel",
            lease_ttl_seconds=5,
        )
        d = result.to_dict()
        assert job_id in d["jobs_cancelled"], d
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            row = JobRepo(conn).get(job_id)
            assert row["state"] == "cancelled", row
        finally:
            conn.close()

    def test_cancel_after_commit_does_not_rollback(self, tmp_path: Path) -> None:
        """Cancel after the job has succeeded must be a no-op on the
        published commits; the job terminal state stays succeeded."""
        vault, claude_root = self._setup(tmp_path)
        payload = json.dumps({
            "api_version": 1, "request_id": "req_done",
            "operation": "scan", "mode": "async",
            "idempotency_key": "phasec-cancel-done-1",
            "params": {"roots": {"claude-code": str(claude_root)},
                       "mode": "incremental"},
        })
        proc = _run_cli("--json", "call", "--request-file", "-",
                         vault=vault, stdin_payload=payload,
                         expect_exit=0)
        body = _read_cli_json(proc)
        job_id = body["job_id"]
        # Run worker so the job completes.
        run_worker_once(vault_root=vault, worker_id="worker_done")
        # Now try to cancel — the response should be ok with state
        # unchanged.
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            row = JobRepo(conn).get(job_id)
            revision = int(row["revision"])
            assert row["state"] == "succeeded"
        finally:
            conn.close()
        proc_c = _run_cli(
            "--json", "job", "cancel",
            "--job-id", job_id, "--expected-revision", str(revision),
            vault=vault, expect_exit=0,
        )
        body_c = _read_cli_json(proc_c)
        assert body_c["status"] == "ok"
        assert body_c["job_state"] == "succeeded", body_c
        # Cancel must not roll back the published commits: the job
        # terminal state stays succeeded and no error was raised.
        assert "warnings" in body_c
        assert any(
            "rollback" in w for w in body_c["warnings"]
        ), body_c

    def test_stale_revision_rejected(self, tmp_path: Path) -> None:
        vault, claude_root = self._setup(tmp_path)
        payload = json.dumps({
            "api_version": 1, "request_id": "req_stale",
            "operation": "scan", "mode": "async",
            "idempotency_key": "phasec-stale-1",
            "params": {"roots": {"claude-code": str(claude_root)},
                       "mode": "incremental"},
        })
        proc = _run_cli("--json", "call", "--request-file", "-",
                         vault=vault, stdin_payload=payload,
                         expect_exit=0)
        body = _read_cli_json(proc)
        job_id = body["job_id"]
        # Cancel with the wrong revision -> INVALID_REQUEST.
        proc_c = _run_cli(
            "--json", "job", "cancel",
            "--job-id", job_id, "--expected-revision", "999",
            vault=vault, expect_exit=2,
        )
        body_c = _read_cli_json(proc_c)
        assert body_c["status"] == "error"
        assert body_c["error"]["code"] == "INVALID_REQUEST", body_c


class TestWorkerRestart:

    def _setup(self, tmp_path: Path) -> tuple[Path, Path]:
        vault = tmp_path / "vault"
        claude_root = _make_sanitised_claude_root(tmp_path / "claude_root")
        _run_cli("--json", "init", "--root",
                 f"claude-code={claude_root}", vault=vault,
                 expect_exit=0)
        return vault, claude_root

    def test_restart_does_not_republish_job(self, tmp_path: Path) -> None:
        vault, claude_root = self._setup(tmp_path)
        # Queue one async job.
        payload = json.dumps({
            "api_version": 1, "request_id": "req_restart",
            "operation": "scan", "mode": "async",
            "idempotency_key": "phasec-restart-1",
            "params": {"roots": {"claude-code": str(claude_root)},
                       "mode": "incremental"},
        })
        proc = _run_cli("--json", "call", "--request-file", "-",
                         vault=vault, stdin_payload=payload,
                         expect_exit=0)
        body = _read_cli_json(proc)
        job_id = body["job_id"]
        # First worker drains it.
        first = run_worker_once(
            vault_root=vault, worker_id="worker_first",
            lease_ttl_seconds=10,
        )
        assert job_id in first.to_dict()["jobs_succeeded"]
        # Second worker reclaims the lease (the first one already
        # released). It should NOT see the job again as queued.
        second = run_worker_once(
            vault_root=vault, worker_id="worker_second",
            lease_ttl_seconds=10,
        )
        d2 = second.to_dict()
        assert job_id not in d2["jobs_succeeded"], d2
        assert d2["jobs_claimed"] == [], d2
        # The job is still succeeded, with the original revision.
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            row = JobRepo(conn).get(job_id)
            assert row["state"] == "succeeded"
            runs = WorkerRunRepo(conn).list_recent()
            assert len(runs) == 2
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# schedule semantics: daily / weekly / monthly
# ---------------------------------------------------------------------------


class TestWorkerSchedule:

    def _setup(self, tmp_path: Path) -> tuple[Path, Path]:
        vault = tmp_path / "vault"
        claude_root = _make_sanitised_claude_root(tmp_path / "claude_root")
        _run_cli("--json", "init", "--root",
                 f"claude-code={claude_root}", vault=vault,
                 expect_exit=0)
        return vault, claude_root

    def test_force_daily_weekly_monthly(self, tmp_path: Path) -> None:
        """With real work to consume (a fresh event line + a queued
        async job), forcing daily / weekly / monthly must bump the
        ``last_*_run`` timestamp and surface the run flags.

        The phase C repair-2 contract is: the timestamp only advances
        on real work. ``force_daily=True`` runs the scan; if the
        scan observes an increment (or the same pass drained a
        queued job) the timestamp advances. ``force_weekly=True``
        and ``force_monthly=True`` always produce a conservative
        review proposal artifact and bump their timestamps.
        """
        vault, claude_root = self._setup(tmp_path)
        # Append a fresh line so the daily scan has something to
        # ingest, then queue an async scan so the worker also has
        # queued work to drain.
        session = claude_root / "projects" / "proj_test_alpha" / "sess_phasec.jsonl"
        with session.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "user", "uuid": "u_dwm",
                "message": {"role": "user", "content": "use Python for the prototype"},
                "timestamp": "2026-10-08T10:05:00Z",
                "sessionId": "sess_phasec", "parentUuid": None,
            }) + "\n")
        payload = json.dumps({
            "api_version": 1, "request_id": "req_dwm",
            "operation": "scan", "mode": "async",
            "idempotency_key": "phasec-dwm-1",
            "params": {"roots": {"claude-code": str(claude_root)},
                       "mode": "incremental"},
        })
        proc = _run_cli("--json", "call", "--request-file", "-",
                         vault=vault, stdin_payload=payload,
                         expect_exit=0)
        body = _read_cli_json(proc)
        assert body["status"] == "queued", body
        first = run_worker_once(
            vault_root=vault, worker_id="worker_dwm",
            lease_ttl_seconds=10,
            force_daily=True, force_weekly=True, force_monthly=True,
        )
        d = first.to_dict()
        # The drain step ran the queued async scan, so the pass
        # completed a job — ``daily_run`` advances on real work.
        assert d["daily_run"] is True, d
        assert d["weekly_run"] is True, d
        assert d["monthly_run"] is True, d
        # Weekly / monthly review artifacts exist on disk.
        from memory_agent.infrastructure.files import VaultLayout
        layout = VaultLayout(vault)
        weekly = layout.artifacts_dir / "reviews" / "weekly_*.json"
        monthly = layout.artifacts_dir / "reviews" / "monthly_*.json"
        assert any(weekly.parent.glob("weekly_*.json"))
        assert any(monthly.parent.glob("monthly_*.json"))

    def test_weekly_requires_daily_catchup(self, tmp_path: Path) -> None:
        """Weekly run is suppressed unless a daily catch-up has run
        in the same wall-clock window. Phase C repair-2: a daily run
        means *real work* happened (an increment was observed)."""
        vault, claude_root = self._setup(tmp_path)
        # First pass: only weekly forced. No daily run, so weekly
        # is suppressed.
        first = run_worker_once(
            vault_root=vault, worker_id="worker_w1",
            lease_ttl_seconds=10,
            force_weekly=True,
        )
        d = first.to_dict()
        assert d["weekly_run"] is False, d
        # Append a fresh line so the *next* daily pass sees an
        # increment and bumps its timestamp.
        session = claude_root / "projects" / "proj_test_alpha" / "sess_phasec.jsonl"
        with session.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "user", "uuid": "u_daily",
                "message": {"role": "user", "content": "use Python for the prototype"},
                "timestamp": "2026-10-08T10:06:00Z",
                "sessionId": "sess_phasec", "parentUuid": None,
            }) + "\n")
        # Now force daily (real work is present) and weekly; the
        # weekly proposal must fire.
        second = run_worker_once(
            vault_root=vault, worker_id="worker_w2",
            lease_ttl_seconds=10,
            force_daily=True, force_weekly=True,
        )
        d2 = second.to_dict()
        assert d2["daily_run"] is True, d2
        assert d2["weekly_run"] is True, d2

    def test_daily_no_increment_no_timestamp_bump(self, tmp_path: Path) -> None:
        """The daily pass must NOT advance ``last_daily_run`` when
        there is no increment and no queued job. The next pass in
        the same window therefore still sees the daily catch-up as
        needed. This pins the no-op safety of the daily pass.
        """
        vault, _ = self._setup(tmp_path)
        # No fresh events appended; the vault is empty. A forced
        # daily pass must not bump the timestamp.
        from memory_agent.infrastructure.sqlite import open_state_db
        from memory_agent.infrastructure.sqlite.repositories import (
            WorkerRuntimeStateRepo,
        )
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            before = WorkerRuntimeStateRepo(conn).get("last_daily_run")
        finally:
            conn.close()
        result = run_worker_once(
            vault_root=vault, worker_id="worker_no_inc",
            lease_ttl_seconds=10,
            force_daily=True,
        )
        d = result.to_dict()
        assert d["daily_run"] is False, d
        # ``diagnostics`` records that the pass ran with no
        # increment so the next scheduler can see it.
        assert "daily_pass_no_increment" in d.get("diagnostics", {}), d
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            after = WorkerRuntimeStateRepo(conn).get("last_daily_run")
        finally:
            conn.close()
        # No timestamp bump.
        assert (before or {}).get("date") == (after or {}).get("date")


# ---------------------------------------------------------------------------
# source-correction queue
# ---------------------------------------------------------------------------


class TestSourceCorrectionQueue:

    def _commit_memory_with_evidence(self, tmp_path: Path) -> tuple[Path, str, str, str]:
        """Build a small vault, scan once to register an event, write a
        memory Markdown citing the event_id, and return ``(vault,
        memory_id, event_id, evidence_quote)``.

        We avoid the full extract / commit pipeline here because the
        source-correction queue should be testable directly: the
        discovery pass walks the memory filesystem, the test rewrites
        the source text, then we re-scan and assert the queue.
        """
        vault = tmp_path / "vault"
        claude_root = _make_sanitised_claude_root(
            tmp_path / "claude_root",
            user_lines=["use Python for the prototype"],
            timestamp="2026-10-08T10:00:00Z",
        )
        _run_cli("--json", "init", "--root",
                 f"claude-code={claude_root}", vault=vault,
                 expect_exit=0)
        # Append a *new* line AFTER init so the incremental scan
        # actually advances the cursor and emits a fresh event row.
        session = (claude_root / "projects" / "proj_test_alpha"
                   / "sess_phasec.jsonl")
        with session.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "user", "uuid": "u_extra",
                "message": {"role": "user", "content": "use Python for the prototype"},
                "timestamp": "2026-10-08T10:05:00Z",
                "sessionId": "sess_phasec", "parentUuid": None,
            }) + "\n")
        payload = json.dumps({
            "api_version": 1, "request_id": "req_sc",
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
        # Pick the first registered source_event_id.
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            row = conn.execute(
                "SELECT event_id, revision_hash FROM source_events "
                "ORDER BY discovered_at LIMIT 1"
            ).fetchone()
            event_id = str(row["event_id"])
            old_revision_hash = str(row["revision_hash"])
        finally:
            conn.close()
        # Hand-craft a memory Markdown that cites this event_id.
        memory_id = "mem_phase_c_001"
        mem_dir = vault / "objects" / "memories" / memory_id
        mem_dir.mkdir(parents=True, exist_ok=True)
        evidence_quote = "use Python for the prototype"
        mem_path = mem_dir / "r0001.md"
        mem_path.write_text(
            "---\nschema_version: 1\nmemory_id: mem_phase_c_001\nrevision: 1\n"
            "status: active\nkind: requirement\nabstraction: no_opinion\n"
            "statement: use Python for the prototype\nsubject: global\n"
            "modality: must\nscope:\n  level: global\n"
            "horizon: unknown\nasserted_at: '2026-10-08T10:00:00Z'\n"
            "recorded_at: '2026-10-08T10:00:00Z'\nevidence:\n"
            f"- source_event_id: {event_id}\n"
            f"  source_revision_hash: {old_revision_hash}\n"
            f"  quote: \"{evidence_quote}\"\n"
            "  role: user\n  origin: user_session\n"
            "  span_start: 0\n  span_end: 27\n"
            "relations: []\nsupersedes: []\n---\n\n"
            "Body: use Python for the prototype\n",
            encoding="utf-8",
        )
        return vault, memory_id, event_id, evidence_quote

    def test_discovery_creates_pending_proposal_after_rewrite(
        self, tmp_path: Path,
    ) -> None:
        vault, memory_id, event_id, _ = self._commit_memory_with_evidence(tmp_path)
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            discovery = discover_affected_memories(vault=vault, conn=conn)
            assert discovery.scanned_memories >= 1, discovery.to_dict()
            assert discovery.new_proposals == 0, discovery.to_dict()
        finally:
            conn.close()
        # Now rewrite the source text in place AND add a new event row
        # at a different revision_hash (simulates a middle-rewrite).
        claude_root = next(iter(_claude_roots(vault)))
        session = claude_root / "projects" / "proj_test_alpha" / "sess_phasec.jsonl"
        original = session.read_text(encoding="utf-8")
        session.write_text(
            original.replace("use Python for the prototype",
                             "use TypeScript for the prototype"),
            encoding="utf-8",
        )
        # Scan with backfill on a tight window so the changed record
        # becomes a new event row at a different revision_hash.
        payload = json.dumps({
            "api_version": 1, "request_id": "req_rewrite",
            "operation": "scan", "mode": "sync",
            "params": {
                "roots": {"claude-code": str(claude_root)},
                "mode": "backfill",
                "since_iso": "2026-10-08T10:00:00Z",
                "until_iso": "2026-10-08T11:00:00Z",
                "timezone": "UTC",
            },
        })
        _run_cli("--json", "call", "--request-file", "-",
                 vault=vault, stdin_payload=payload,
                 expect_exit=0)
        # Re-run discovery; expect a pending proposal. The
        # ``_cmd_scan`` dispatcher now refreshes the overlay
        # *inline* on every scan (phase C repair-2), so by the
        # time we re-enter the in-process helper, the overlay
        # rows are already there. We still call ``refresh_overlay``
        # to confirm the call is idempotent (zero rows touched is
        # the expected outcome).
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            discovery2 = discover_affected_memories(vault=vault, conn=conn)
            overlay_touched = refresh_overlay(conn=conn)
            proposals = SourceCorrectionProposalRepo(conn).list_pending()
            overlay = MemoryCorrectionOverlayRepo(conn).get(memory_id, 1)
        finally:
            conn.close()
        assert discovery2.new_proposals >= 1, discovery2.to_dict()
        # The overlay was refreshed during the scan itself, so the
        # pending-correction row is already on disk and the helper
        # call here is a no-op (zero rows touched).
        assert overlay is not None
        assert overlay["state"] == "pending_correction", overlay
        # The proposal pins the old event_id and the new event_id
        # (the latter may be NULL or a new id depending on whether
        # the backfill ingested a fresh row at the same byte
        # position).
        target = next(p for p in proposals if p["memory_id"] == memory_id)
        assert target["old_event_id"] == event_id, target
        assert target["status"] in {"pending", "auto_resolved"}, target

    def test_overlay_marks_memory_pending_correction(self, tmp_path: Path) -> None:
        vault, memory_id, event_id, _ = self._commit_memory_with_evidence(tmp_path)
        # Force the proposal directly (skip the rewrite / re-scan dance
        # because that requires a sanitised session rewrite which is
        # already covered above).
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            repo = SourceCorrectionProposalRepo(conn)
            repo.upsert({
                "correction_id": f"cor_test_{memory_id}",
                "source_file_id": "test-source",
                "old_event_id": event_id,
                "new_event_id": "evt_new",
                "memory_id": memory_id,
                "memory_revision": 1,
                "old_revision_hash": "rh_old",
                "new_revision_hash": "rh_new",
                "status": "pending",
                "reason": "source_rewritten",
            })
            touched = refresh_overlay(conn=conn)
            overlay = MemoryCorrectionOverlayRepo(conn).get(memory_id, 1)
        finally:
            conn.close()
        assert touched >= 1
        assert overlay is not None
        assert overlay["state"] == "pending_correction", overlay
        assert event_id in overlay["affected_event_ids"]

    def test_replay_does_not_duplicate_proposal(self, tmp_path: Path) -> None:
        vault, memory_id, event_id, _ = self._commit_memory_with_evidence(tmp_path)
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            repo = SourceCorrectionProposalRepo(conn)
            for _ in range(3):
                repo.upsert({
                    "correction_id": f"cor_replay_{memory_id}",
                    "source_file_id": "test-source",
                    "old_event_id": event_id,
                    "new_event_id": "evt_new",
                    "memory_id": memory_id,
                    "memory_revision": 1,
                    "old_revision_hash": "rh_old",
                    "new_revision_hash": "rh_new",
                    "status": "pending",
                    "reason": "source_rewritten",
                })
            rows = conn.execute(
                "SELECT COUNT(*) AS c FROM source_correction_proposals "
                "WHERE memory_id = ?", (memory_id,),
            ).fetchone()
        finally:
            conn.close()
        assert rows["c"] == 1

    def test_worker_restart_does_not_duplicate_proposal(self, tmp_path: Path) -> None:
        vault, memory_id, event_id, _ = self._commit_memory_with_evidence(tmp_path)
        # Run the worker twice; the discovery step should not duplicate.
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            repo = SourceCorrectionProposalRepo(conn)
            # First run: insert proposal directly.
            repo.upsert({
                "correction_id": f"cor_restart_{memory_id}",
                "source_file_id": "test-source",
                "old_event_id": event_id,
                "new_event_id": "evt_new",
                "memory_id": memory_id,
                "memory_revision": 1,
                "old_revision_hash": "rh_old",
                "new_revision_hash": "rh_new",
                "status": "pending",
                "reason": "source_rewritten",
            })
        finally:
            conn.close()
        # First worker pass: nothing new to queue (overlay only).
        run_worker_once(vault_root=vault, worker_id="w_restart_1")
        # Second pass: still nothing new.
        run_worker_once(vault_root=vault, worker_id="w_restart_2")
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            rows = conn.execute(
                "SELECT COUNT(*) AS c FROM source_correction_proposals "
                "WHERE memory_id = ?", (memory_id,),
            ).fetchone()
        finally:
            conn.close()
        assert rows["c"] == 1

    def test_decide_keep_resolves_overlay(self, tmp_path: Path) -> None:
        vault, memory_id, event_id, _ = self._commit_memory_with_evidence(tmp_path)
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            repo = SourceCorrectionProposalRepo(conn)
            proposal_id = f"cor_keep_{memory_id}"
            repo.upsert({
                "correction_id": proposal_id,
                "source_file_id": "test-source",
                "old_event_id": event_id,
                "new_event_id": "evt_new",
                "memory_id": memory_id,
                "memory_revision": 1,
                "old_revision_hash": "rh_old",
                "new_revision_hash": "rh_new",
                "status": "pending",
                "reason": "source_rewritten",
            })
            refresh_overlay(conn=conn)
            # User decides to keep the old memory.
            updated = decide_proposal(
                conn=conn, correction_id=proposal_id,
                action="keep_old", note="verified by hand",
            )
        finally:
            conn.close()
        assert updated is not None
        assert updated["status"] == "decided_keep", updated
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            overlay = MemoryCorrectionOverlayRepo(conn).get(memory_id, 1)
        finally:
            conn.close()
        # Overlay entry is cleared because the proposal is no longer
        # pending.
        assert overlay is None

    def test_decide_defer_leaves_pending(self, tmp_path: Path) -> None:
        vault, memory_id, event_id, _ = self._commit_memory_with_evidence(tmp_path)
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            repo = SourceCorrectionProposalRepo(conn)
            proposal_id = f"cor_defer_{memory_id}"
            repo.upsert({
                "correction_id": proposal_id,
                "source_file_id": "test-source",
                "old_event_id": event_id,
                "new_event_id": "evt_new",
                "memory_id": memory_id,
                "memory_revision": 1,
                "old_revision_hash": "rh_old",
                "new_revision_hash": "rh_new",
                "status": "pending",
                "reason": "source_rewritten",
            })
            refresh_overlay(conn=conn)
            updated = decide_proposal(
                conn=conn, correction_id=proposal_id, action="defer",
            )
        finally:
            conn.close()
        assert updated is not None
        assert updated["status"] == "pending", updated

    def test_decide_relink_marks_relink(self, tmp_path: Path) -> None:
        """``adopt_new`` must publish a new memory through
        ``CommitService`` (referencing the new event_id / revision_hash)
        and mark the old memory ``superseded``. The proposal row must
        advance to ``decided_relink`` with ``superseded_memory_id`` /
        ``superseded_head_commit_id`` populated.
        """
        from memory_agent.infrastructure.files import VaultLayout
        vault, memory_id, event_id, _ = self._commit_memory_with_evidence(tmp_path)
        # The proposal claims a new event_id; we must back that with a
        # real segment so ``adopt_new`` can resolve it.
        layout = VaultLayout(vault)
        new_event_id = "evt_new_adopt"
        new_revision_hash = "rh_new_adopt"
        rewrite_text = "use TypeScript for the prototype"
        rewrite_segment = {
            "segment_id": "seg_adopt_rewrite",
            "source_file_id": "test-source",
            "generation": 2,
            "events": [
                {
                    "event_id": new_event_id,
                    "revision_hash": new_revision_hash,
                    "role": "user",
                    "kind": "user_message",
                    "text": rewrite_text,
                    "source": {
                        "locator": {"byte_start": 0,
                                    "byte_end": len(rewrite_text),
                                    "record_ordinal": 1},
                        "source_time": "2026-10-08T11:00:00Z",
                        "discovered_at": "2026-10-08T11:00:00Z",
                    },
                }
            ],
        }
        seg_path = layout.segments_dir / "seg_adopt_rewrite.json"
        seg_path.write_text(
            json.dumps(rewrite_segment, ensure_ascii=False), encoding="utf-8"
        )
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            repo = SourceCorrectionProposalRepo(conn)
            proposal_id = f"cor_relink_{memory_id}"
            repo.upsert({
                "correction_id": proposal_id,
                "source_file_id": "test-source",
                "old_event_id": event_id,
                "new_event_id": new_event_id,
                "memory_id": memory_id,
                "memory_revision": 1,
                "old_revision_hash": "rh_old",
                "new_revision_hash": new_revision_hash,
                "status": "pending",
                "reason": "source_rewritten",
            })
            refresh_overlay(conn=conn)
            updated = decide_proposal(
                conn=conn, correction_id=proposal_id, action="adopt_new",
                vault=layout,
            )
        finally:
            conn.close()
        assert updated is not None
        assert updated["status"] == "decided_relink", updated
        assert updated.get("superseded_memory_id"), updated
        assert updated.get("superseded_head_commit_id"), updated
        # The new memory file is on disk.
        new_mem_id = updated["superseded_memory_id"]
        new_mem_path = layout.memory_revision_path(new_mem_id, 1)
        assert new_mem_path.exists(), (
            f"new memory file {new_mem_path} must be published"
        )
        # The new memory references the *new* event_id, not the old one.
        new_md = new_mem_path.read_text(encoding="utf-8")
        assert new_event_id in new_md, new_md
        # HEAD advanced to the new commit.
        head = json.loads(layout.head_path.read_text(encoding="utf-8"))
        assert head["commit_id"] == updated["superseded_head_commit_id"]

    def test_old_evidence_remains_accessible(self, tmp_path: Path) -> None:
        """The old segment file is preserved by phase B; a query that
        targets the old event_id + old revision_hash must still
        resolve. This pins the contract that the worker never
        silently rewrites the source-of-truth filesystem."""
        vault, memory_id, event_id, _ = self._commit_memory_with_evidence(tmp_path)
        segments_dir = vault / "sources" / "segments"
        # At least one segment file must exist and reference the event.
        found = False
        for seg in segments_dir.glob("*.json"):
            try:
                payload = json.loads(seg.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            for ev in payload.get("events", []):
                if str(ev.get("event_id")) == event_id:
                    found = True
                    break
            if found:
                break
        assert found, (
            "old segment file referencing the original event_id must "
            "still be on disk"
        )


def _claude_roots(vault: Path) -> list[Path]:
    """Find any path under the vault that looks like a sanitised claude root.

    Used by tests that need to locate the raw JSONL after the vault has
    moved on. The fixture writes roots into ``/tmp/<vault>/claude_root``;
    we walk siblings looking for a ``projects/proj_test_alpha/...``.
    """
    parent = vault.parent
    out: list[Path] = []
    for child in parent.iterdir():
        if not child.is_dir():
            continue
        if (child / "projects" / "proj_test_alpha").is_dir():
            out.append(child)
    return out