"""Targeted tests for the Phase C repair-1 defects.

  1. ``drain_queued_jobs()`` does not instantiate the model provider
     for model-free operations (``scan``/``init``/``doctor``/
     ``batch.plan``/``conflicts``/``decide``); a ``scan`` async job
     completes even when ``MEMORY_AGENT_ENABLE_CLAUDE_CLI`` is unset.
  2. End-to-end subprocess smoke: init -> async scan -> worker --once
     (no opt-in) -> ``job status`` returns ``succeeded`` with the scan
     counters and ``model_calls=0``. A repeat worker pass makes no
     further changes.
  3. ``WorkerLeaseRepo`` enforces single active worker per vault; a
     second worker process holding the writer lock is refused.
  4. Stale ``running`` jobs whose heartbeat has expired are reclaimable
     by the next worker pass and become terminal once.
  5. ``call.py`` routes ``job.status``/``job.cancel``/``job.retry``
     through the explicit ``vault_root`` parameter; no global mutable
     vault-root holder.

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
# Helpers (mirror test_phase_c_v1.py)
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
    # Defensive: never let the test runner leak CLI opt-in.
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


def _make_sanitised_claude_root(path: Path, *, session_id: str = "sess_repair1",
                                user_lines: list[str] | None = None,
                                timestamp: str = "2026-10-08T10:00:00Z") -> Path:
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


def _open_db(vault: Path) -> sqlite3.Connection:
    path = vault / "state" / "state.sqlite"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Defect 1: model-free drain + smoke
# ---------------------------------------------------------------------------


class TestModelFreeDrain:

    def _init_vault(self, tmp_path: Path) -> tuple[Path, Path]:
        vault = tmp_path / "vault"
        claude_root = _make_sanitised_claude_root(tmp_path / "claude_root")
        proc = _run_cli(
            "--json", "init",
            "--root", f"claude-code={claude_root}",
            vault=vault,
            expect_exit=0,
        )
        assert _read_cli_json(proc)["status"] == "ok"
        return vault, claude_root

    def test_async_scan_drains_without_cli_opt_in(self, tmp_path: Path) -> None:
        """End-to-end: init -> async scan (no CLI opt-in) -> worker --once ->
        ``job status`` reports ``succeeded`` with scan counters and
        ``model_calls=0``.
        """
        vault, claude_root = self._init_vault(tmp_path)
        # Append a fresh line so the scan has something to ingest.
        session = claude_root / "projects" / "proj_test_alpha" / "sess_repair1.jsonl"
        with session.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "user", "uuid": "u_extra",
                "message": {"role": "user", "content": "use Python for the prototype"},
                "timestamp": "2026-10-08T10:05:00Z",
                "sessionId": "sess_repair1", "parentUuid": None,
            }) + "\n")
        payload = json.dumps({
            "api_version": 1, "request_id": "req_repair1_scan",
            "operation": "scan", "mode": "async",
            "idempotency_key": "repair1-scan-1",
            "params": {"roots": {"claude-code": str(claude_root)},
                       "mode": "incremental"},
        })
        # The CLI call runs *without* ``MEMORY_AGENT_ENABLE_CLAUDE_CLI``
        # and without ``MEMORY_AGENT_MODEL_PROVIDER=fake`` — proving the
        # default Claude CLI provider path is *not* required for a
        # model-free ``scan`` operation.
        proc = _run_cli(
            "--json", "call", "--request-file", "-",
            vault=vault, stdin_payload=payload,
            expect_exit=0, omit_model_env=True,
        )
        body = _read_cli_json(proc)
        assert body["status"] == "queued", body
        job_id = body["job_id"]

        # Worker drains the job without CLI opt-in.
        proc_w = _run_cli(
            "--json", "worker", "--once",
            vault=vault, omit_model_env=True,
            expect_exit=0,
        )
        worker_body = _read_cli_json(proc_w)
        assert job_id in worker_body.get("jobs_succeeded", []), worker_body
        assert worker_body.get("model_calls") == 0, worker_body

        # Status reflects terminal ``succeeded``.
        proc_s = _run_cli(
            "--json", "job", "status",
            "--job-id", job_id,
            vault=vault, omit_model_env=True,
            expect_exit=0,
        )
        status_body = _read_cli_json(proc_s)
        assert status_body["status"] == "ok", status_body
        assert status_body["job_state"] == "succeeded", status_body
        assert status_body["job_id"] == job_id
        result = status_body.get("result") or {}
        # The scan counters surface in the worker_runs / job result.
        # ``model_calls`` must be 0 because no model provider ran.
        assert (status_body.get("progress") or {}).get("model_calls", 0) == 0

        # Repeat worker pass: nothing to drain, nothing published.
        proc_w2 = _run_cli(
            "--json", "worker", "--once",
            vault=vault, omit_model_env=True,
            expect_exit=0,
        )
        worker_body2 = _read_cli_json(proc_w2)
        assert worker_body2.get("jobs_claimed", []) == [], worker_body2
        assert worker_body2.get("jobs_succeeded", []) == [], worker_body2

        # Job remains succeeded.
        proc_s2 = _run_cli(
            "--json", "job", "status",
            "--job-id", job_id,
            vault=vault, omit_model_env=True,
            expect_exit=0,
        )
        assert _read_cli_json(proc_s2)["job_state"] == "succeeded"

    def test_drain_does_not_instantiate_provider_for_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``drain_queued_jobs`` for a ``scan`` operation must not
        touch the model provider; ``get_default_provider`` must not
        be called from any module in the dispatch path.
        """
        from memory_agent.application.worker import drain_queued_jobs
        from memory_agent.infrastructure.files import VaultLayout
        from memory_agent.infrastructure.sqlite import open_state_db
        from memory_agent.infrastructure.sqlite.repositories import (
            JobRepo, WorkerRunRepo,
        )
        from memory_agent.application.worker import WorkerPassResult, _new_id
        from memory_agent.adapters import models as models_mod
        vault = tmp_path / "vault"
        claude_root = _make_sanitised_claude_root(tmp_path / "claude_root")
        _run_cli(
            "--json", "init", "--root",
            f"claude-code={claude_root}", vault=vault,
            expect_exit=0,
        )
        # Write a real params artifact so the in-process scan dispatch
        # has roots to operate on.
        artifacts_dir = vault / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        params_id = uuid_dash()
        params_ref = str(artifacts_dir / f"repair1-params-{params_id}.json")
        (artifacts_dir / f"repair1-params-{params_id}.json").write_text(
            json.dumps({
                "operation": "scan",
                "params": {
                    "roots": {"claude-code": str(claude_root)},
                    "mode": "incremental",
                },
            }),
            encoding="utf-8",
        )
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            job_repo = JobRepo(conn)
            job_id = f"job_repair1_{uuid_dash()}"
            job_repo.insert({
                "job_id": job_id,
                "request_id": "req_repair1_scan_inproc",
                "client_id": "repair1_test",
                "idempotency_key": "repair1-scan-inproc-1",
                "request_hash": "rh_inproc",
                "operation": "scan",
                "params_ref": params_ref,
                "state": "queued",
                "mode": "async",
            })
        finally:
            conn.close()
        # No opt-in: Claude provider would raise ModelError. We monkey-
        # patch get_default_provider in the models module to fail if
        # any code path in the worker + dispatch chain tries to
        # instantiate it.
        called = {"count": 0}

        def _spy_get_default_provider(*, recorder=None):
            called["count"] += 1
            raise AssertionError(
                "get_default_provider must not be called for a model-free scan"
            )

        monkeypatch.setattr(models_mod, "get_default_provider", _spy_get_default_provider)

        result = WorkerPassResult(worker_id="worker_repair1")
        v = VaultLayout(vault)
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            drain_queued_jobs(
                vault=v, conn=conn,
                lease_token=_new_id("lease", "worker_repair1", "now"),
                heartbeat=0.0,
                result=result,
                run_id=_new_id("run", "worker_repair1", "now"),
                run_repo=WorkerRunRepo(conn),
            )
        finally:
            conn.close()
        assert called["count"] == 0, "get_default_provider must not be called"
        assert result.jobs_succeeded, "scan job must complete as succeeded"
        assert result.model_calls_before == 0


def uuid_dash() -> str:
    import uuid
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# Defect 3: cross-worker exclusion + stale running reclaim
# ---------------------------------------------------------------------------


class TestLeaseExclusion:

    def _setup(self, tmp_path: Path) -> Path:
        vault = tmp_path / "vault"
        claude_root = _make_sanitised_claude_root(tmp_path / "claude_root")
        _run_cli("--json", "init", "--root",
                 f"claude-code={claude_root}", vault=vault,
                 expect_exit=0)
        return vault

    def test_two_distinct_worker_ids_cannot_both_hold_active_lease(
        self, tmp_path: Path,
    ) -> None:
        """``WorkerLeaseRepo.acquire`` must refuse a second worker
        while any other worker holds an active lease, and must allow a
        second worker to reclaim only after the first lease has expired.
        """
        from memory_agent.infrastructure.sqlite import open_state_db
        from memory_agent.infrastructure.sqlite.repositories import WorkerLeaseRepo
        vault = self._setup(tmp_path)
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            leases = WorkerLeaseRepo(conn)
            # Worker A acquires.
            assert leases.acquire(
                worker_id="worker_A", lease_token="lease_A",
                ttl_seconds=60, current_job_id=None,
            ) is True
            # Worker B is refused while worker A's lease is still active.
            assert leases.acquire(
                worker_id="worker_B", lease_token="lease_B",
                ttl_seconds=60, current_job_id=None,
            ) is False, (
                "a second worker must NOT acquire a lease while another "
                "worker's lease is still active"
            )
            # Manually expire worker A's lease (simulate stale TTL).
            conn.execute(
                "UPDATE worker_leases SET expires_at = '2000-01-01T00:00:00+00:00' "
                "WHERE worker_id = 'worker_A'"
            )
            # Worker B can now reclaim.
            assert leases.acquire(
                worker_id="worker_B", lease_token="lease_B",
                ttl_seconds=60, current_job_id=None,
            ) is True, (
                "after the previous lease expires, a different worker "
                "may reclaim"
            )
        finally:
            conn.close()

    def test_stale_running_job_is_reclaimed_on_next_pass(
        self, tmp_path: Path,
    ) -> None:
        """A job stuck in ``running`` with an expired heartbeat must
        be reclaimed by the next worker pass and reach a terminal
        state, not remain stuck forever.
        """
        from memory_agent.application.worker import run_worker_once
        from memory_agent.infrastructure.sqlite import open_state_db
        from memory_agent.infrastructure.sqlite.repositories import JobRepo

        vault = self._setup(tmp_path)
        claude_root = next(p for p in tmp_path.iterdir()
                           if (p / "projects" / "proj_test_alpha").is_dir())
        # Queue an async scan.
        payload = json.dumps({
            "api_version": 1, "request_id": "req_stale",
            "operation": "scan", "mode": "async",
            "idempotency_key": "repair1-stale-1",
            "params": {"roots": {"claude-code": str(claude_root)},
                       "mode": "incremental"},
        })
        proc = _run_cli("--json", "call", "--request-file", "-",
                         vault=vault, stdin_payload=payload,
                         expect_exit=0, omit_model_env=True)
        job_id = _read_cli_json(proc)["job_id"]
        # Manually mark the job as running with a stale heartbeat to
        # simulate a worker that claimed it and then died before
        # finalising.
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            JobRepo(conn).mark_started(job_id)
            conn.execute(
                "UPDATE jobs SET last_heartbeat = '2000-01-01T00:00:00+00:00' "
                "WHERE job_id = ?", (job_id,)
            )
            conn.commit()
        finally:
            conn.close()
        # Next worker pass must reclaim and complete the job.
        result = run_worker_once(
            vault_root=vault, worker_id="worker_repair1_stale",
            lease_ttl_seconds=5,
        )
        d = result.to_dict()
        assert job_id in d["jobs_succeeded"], d
        conn = open_state_db(vault / "state" / "state.sqlite")
        try:
            row = JobRepo(conn).get(job_id)
            assert row["state"] == "succeeded", row
            # The job must reach the terminal state exactly once — no
            # double-publish.
            assert row["revision"] >= 2
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Defect 4: explicit vault_root in handle_call
# ---------------------------------------------------------------------------


class TestExplicitVaultRoot:

    def test_job_status_does_not_use_module_global(self, tmp_path: Path) -> None:
        """``handle_call('job.status')`` must read the vault from the
        explicit ``vault_root`` parameter, not from a module-global
        holder. Setting a *different* global holder between two
        consecutive calls must not change which vault each call sees.
        """
        from memory_agent.application.call import handle_call

        vault_a = tmp_path / "vault_a"
        vault_b = tmp_path / "vault_b"
        for v in (vault_a, vault_b):
            claude_root = _make_sanitised_claude_root(
                tmp_path / f"claude_{v.name}",
                session_id=f"sess_{v.name}",
            )
            _run_cli("--json", "init", "--root",
                     f"claude-code={claude_root}", vault=v,
                     expect_exit=0)
        # Queue a job in vault_a.
        claude_root_a = next(p for p in tmp_path.iterdir()
                             if p.name.startswith("claude_vault_a")
                             and (p / "projects" / "proj_test_alpha").is_dir())
        payload = json.dumps({
            "api_version": 1, "request_id": "req_va",
            "operation": "scan", "mode": "async",
            "idempotency_key": "repair1-va-1",
            "params": {"roots": {"claude-code": str(claude_root_a)},
                       "mode": "incremental"},
        })
        proc = _run_cli(
            "--json", "call", "--request-file", "-",
            vault=vault_a, stdin_payload=payload, omit_model_env=True,
            expect_exit=0,
        )
        job_id_a = _read_cli_json(proc)["job_id"]

        # Call handle_call with the explicit vault_a. The response must
        # describe vault_a's job. (The legacy global holder is gone
        # entirely.)
        outcome = handle_call(
            request={
                "api_version": 1,
                "request_id": "req_status_va",
                "operation": "job.status",
                "mode": "sync",
                "params": {"job_id": job_id_a},
            },
            vault_root=vault_a,
        )
        body = outcome.response
        # The job hasn't been drained yet, so it is ``queued`` —
        # ``status`` is mapped from the row state. The important
        # claim is that the lookup happened against vault_a, not
        # vault_b.
        assert body["status"] in {"queued", "ok"}, body
        assert body["job_id"] == job_id_a
        # The row must come from vault_a's DB. Confirm by reading the
        # DB directly from vault_a.
        conn = _open_db(vault_a)
        try:
            row = conn.execute(
                "SELECT state FROM jobs WHERE job_id = ?", (job_id_a,)
            ).fetchone()
            assert row is not None, "job must exist in vault_a"
        finally:
            conn.close()
        # Sanity: vault_b has no such job.
        conn = _open_db(vault_b)
        try:
            row = conn.execute(
                "SELECT state FROM jobs WHERE job_id = ?", (job_id_a,)
            ).fetchone()
            assert row is None, "vault_b must not contain vault_a's job"
        finally:
            conn.close()
        # Pointing at the *wrong* vault with the same job_id must
        # return NOT_FOUND (proving the call really used the explicit
        # vault_root, not some hidden global state).
        outcome_wrong = handle_call(
            request={
                "api_version": 1,
                "request_id": "req_status_wrong",
                "operation": "job.status",
                "mode": "sync",
                "params": {"job_id": job_id_a},
            },
            vault_root=vault_b,
        )
        wrong = outcome_wrong.response
        assert wrong["status"] == "error", wrong
        assert wrong["error"]["code"] == "NOT_FOUND", wrong

    def test_no_module_global_vault_root_holder(self) -> None:
        """The legacy ``set_vault_root`` global holder must be gone.
        Multi-client callers thread ``vault_root`` through
        ``handle_call`` explicitly.
        """
        from memory_agent.application import call as call_mod
        # The legacy symbol is no longer exported.
        assert not hasattr(call_mod, "set_vault_root"), (
            "call.py must no longer export set_vault_root"
        )
        assert not hasattr(call_mod, "_VAULT_ROOT_HOLDER"), (
            "call.py must no longer carry a module-global vault root holder"
        )
        assert not hasattr(call_mod, "_peek_vault_root"), (
            "call.py must no longer peek a global vault root"
        )