"""End-to-end CLI subprocess test against an isolated temporary vault."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _run_cli(args: list[str], env: dict[str, str]) -> dict:
    env = {**os.environ, **env, "PYTHONPATH": str(SRC)}
    proc = subprocess.run(
        [sys.executable, "-m", "memory_agent", *args],
        capture_output=True, text=True, env=env, check=False,
    )
    assert proc.returncode == 0, f"CLI failed: {proc.stderr}"
    return json.loads(proc.stdout)


@pytest.fixture
def fake_provider_env() -> dict[str, str]:
    """Test-only env: use the in-process FakeModelProvider instead of
    shelling out to ``claude``. Every CLI subprocess in this module
    inherits this fixture so end-to-end tests do not depend on real
    Claude auth or OAuth."""
    return {"MEMORY_AGENT_MODEL_PROVIDER": "fake"}


@pytest.fixture
def isolated_vault(tmp_path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def claude_root_with_session(tmp_path) -> Path:
    """Build a sanitised Claude Code JSONL session for the e2e test."""
    root = tmp_path / "claude"
    proj = root / "projects" / "proj_e2e"
    proj.mkdir(parents=True)
    session = proj / "sess_e2e_001.jsonl"
    lines = [
        {"type": "user", "uuid": "u01", "message": {"role": "user", "content": "use Python for the prototype"},
         "timestamp": "2026-10-08T10:00:00Z", "sessionId": "sess_e2e_001", "parentUuid": None},
        {"type": "assistant", "uuid": "a01", "message": {"role": "assistant", "content": "OK."},
         "timestamp": "2026-10-08T10:00:01Z", "sessionId": "sess_e2e_001", "parentUuid": "u01"},
    ]
    session.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n",
                       encoding="utf-8")
    return root


class TestCLISubprocessE2E:
    def test_init_then_maintain_then_recall(
        self, isolated_vault, claude_root_with_session, fake_provider_env
    ):
        env = {**fake_provider_env, "MEMORY_VAULT": str(isolated_vault)}

        # 1. init (must baseline the configured root)
        r = _run_cli(
            [
                "--vault", str(isolated_vault),
                "init",
                "--root", f"claude-code={claude_root_with_session}",
            ],
            env,
        )
        assert r["status"] == "ok"
        baseline = r["result"]["baseline"]
        # The session file already existed before init -> baseline
        # pins the cursor at end-of-file. No historical ingestion yet.
        assert baseline["applied_count"] >= 1
        assert baseline["behaviour"].startswith("init records")

        # 2. maintain with the sanitised root.
        r = _run_cli(
            [
                "--vault", str(isolated_vault),
                "maintain",
                "--root", f"claude-code={claude_root_with_session}",
            ],
            env,
        )
        assert r["status"] == "ok"
        result = r["result"]
        # The session file existed before init, so the first maintain
        # sees no new content. Append a new user line and run again.
        sess = (
            claude_root_with_session
            / "projects"
            / "proj_e2e"
            / "sess_e2e_001.jsonl"
        )
        sess.write_text(
            sess.read_text(encoding="utf-8")
            + json.dumps({
                "type": "user", "uuid": "u99",
                "message": {"role": "user",
                            "content": "use Python for the prototype"},
                "timestamp": "2026-10-08T13:00:00Z",
                "sessionId": "sess_e2e_001", "parentUuid": "a01",
            })
            + "\n",
            encoding="utf-8",
        )
        r = _run_cli(
            [
                "--vault", str(isolated_vault),
                "maintain",
                "--root", f"claude-code={claude_root_with_session}",
            ],
            env,
        )
        assert r["status"] == "ok"
        result = r["result"]
        assert result["scan"]["new_events"] >= 1
        assert result["processed"] != []
        assert result["processed"][0]["ok"] is True
        # The fake provider made >=1 call this round.
        assert result["model_calls"] >= 1

        # 3. recall must return the committed memory.
        r = _run_cli(
            [
                "--vault", str(isolated_vault),
                "recall", "--query", "Python prototype",
            ],
            env,
        )
        assert r["status"] == "ok"
        items = r["result"].get("items", [])
        assert items, "recall must return the committed memory"
        item = items[0]
        assert "Python" in item["statement"]
        assert item["quote"]
        assert item["locator"]["byte_start"] >= 0
        assert item["locator"]["byte_end"] > item["locator"]["byte_start"]

        # 4. doctor: should report healthy.
        r = _run_cli(["--vault", str(isolated_vault), "doctor"], env)
        assert r["status"] == "ok"
        assert "schema_version" in r["result"]

    def test_idempotent_repeat_maintain_no_duplicate(
        self, isolated_vault, claude_root_with_session, fake_provider_env
    ):
        env = {**fake_provider_env, "MEMORY_VAULT": str(isolated_vault)}
        _run_cli(
            [
                "--vault", str(isolated_vault),
                "init",
                "--root", f"claude-code={claude_root_with_session}",
            ],
            env,
        )
        # Append a user message before the first maintain.
        sess = (
            claude_root_with_session
            / "projects"
            / "proj_e2e"
            / "sess_e2e_001.jsonl"
        )
        sess.write_text(
            sess.read_text(encoding="utf-8")
            + json.dumps({
                "type": "user", "uuid": "u88",
                "message": {"role": "user",
                            "content": "always log decisions to vault/audit"},
                "timestamp": "2026-10-08T13:30:00Z",
                "sessionId": "sess_e2e_001", "parentUuid": "a01",
            })
            + "\n",
            encoding="utf-8",
        )
        # First maintain
        r1 = _run_cli(
            [
                "--vault", str(isolated_vault),
                "maintain",
                "--root", f"claude-code={claude_root_with_session}",
            ],
            env,
        )
        assert r1["status"] == "ok"
        first_calls = r1["result"]["model_calls"]
        assert first_calls >= 1
        first_events = r1["result"]["scan"]["new_events"]
        assert first_events >= 1

        # Second maintain: nothing new, zero new model calls.
        r2 = _run_cli(
            [
                "--vault", str(isolated_vault),
                "maintain",
                "--root", f"claude-code={claude_root_with_session}",
            ],
            env,
        )
        assert r2["status"] == "ok"
        assert r2["result"]["scan"]["new_events"] == 0
        assert r2["result"]["model_calls"] == 0
        assert r2["result"]["processed"] == []

    def test_init_baseline_then_late_root(self, isolated_vault, tmp_path,
                                            claude_root_with_session,
                                            fake_provider_env):
        """Init without roots must not read anything; baseline applied
        later via `init` again with a root only baselines that file.

        Specifically: a vault created via `init` with no roots must
        reject `maintain` that has no roots (because there is nothing
        to maintain), and `init --root` run later must pin the cursor
        at end-of-file so the first `maintain --root` sees zero new
        events."""
        env = {**fake_provider_env, "MEMORY_VAULT": str(isolated_vault)}
        # 1. init (no roots)
        r = _run_cli(["--vault", str(isolated_vault), "init"], env)
        assert r["status"] == "ok"
        assert r["result"]["baseline"]["applied"] is False

        # 2. maintain with no roots is rejected (no source configured).
        proc = subprocess.run(
            [sys.executable, "-m", "memory_agent",
             "--vault", str(isolated_vault),
             "maintain"],
            capture_output=True, text=True,
            env={**os.environ, **env, "PYTHONPATH": str(SRC)},
            check=False,
        )
        assert proc.returncode != 0
        body = json.loads(proc.stdout)
        assert body["status"] == "error"

        # 3. Re-init with --root baselines the session at end-of-file.
        r = _run_cli(
            [
                "--vault", str(isolated_vault),
                "init",
                "--root", f"claude-code={claude_root_with_session}",
            ],
            env,
        )
        assert r["status"] == "ok"
        assert r["result"]["baseline"]["applied_count"] >= 1

        # 4. First maintain after baseline: nothing historical.
        r = _run_cli(
            [
                "--vault", str(isolated_vault),
                "maintain",
                "--root", f"claude-code={claude_root_with_session}",
            ],
            env,
        )
        assert r["status"] == "ok"
        assert r["result"]["scan"]["new_events"] == 0

    def test_conflicts_pending_when_in_scope_overlap(
        self, isolated_vault, tmp_path, fake_provider_env
    ):
        """Two contradictory same-scope requirements must become a
        pending conflict projection entry."""
        root = tmp_path / "claude"
        proj = root / "projects" / "proj_conflict"
        proj.mkdir(parents=True)
        s1 = proj / "sess_first.jsonl"
        s2 = proj / "sess_second.jsonl"
        s1.write_text(
            json.dumps({
                "type": "user", "uuid": "u01",
                "message": {"role": "user", "content": "use Python for the prototype"},
                "timestamp": "2026-10-08T10:00:00Z",
                "sessionId": "sess_first", "parentUuid": None,
            }) + "\n", encoding="utf-8",
        )
        env = {**fake_provider_env, "MEMORY_VAULT": str(isolated_vault)}
        # Init WITHOUT roots so the first maintain actually ingests
        # historical content; that gives us an active Python memory to
        # conflict against.
        _run_cli(["--vault", str(isolated_vault), "init"], env)
        r1 = _run_cli(
            [
                "--vault", str(isolated_vault),
                "maintain", "--root", f"claude-code={root}",
            ],
            env,
        )
        assert r1["status"] == "ok"
        assert r1["result"]["scan"]["new_events"] >= 1

        # Now add TypeScript (contradictory) in the second session.
        s2.write_text(
            json.dumps({
                "type": "user", "uuid": "u10",
                "message": {"role": "user", "content": "use TypeScript for the dashboard"},
                "timestamp": "2026-10-08T11:00:00Z",
                "sessionId": "sess_second", "parentUuid": None,
            }) + "\n", encoding="utf-8",
        )
        r2 = _run_cli(
            [
                "--vault", str(isolated_vault),
                "maintain", "--root", f"claude-code={root}",
            ],
            env,
        )
        assert r2["status"] == "ok"
        # The second requirement must show up in the open conflicts list.
        cf = _run_cli(
            ["--vault", str(isolated_vault), "conflicts"], env,
        )
        assert cf["status"] == "ok"
        open_conflicts = cf["result"].get("open", [])
        assert open_conflicts, "expected a pending conflict for contradictory requirements"

    def test_malformed_request_rejected(self, isolated_vault, fake_provider_env):
        # No init; scan should return a RECOVERY_REQUIRED error.
        env = {**fake_provider_env, "MEMORY_VAULT": str(isolated_vault)}
        proc = subprocess.run(
            [sys.executable, "-m", "memory_agent",
             "--vault", str(isolated_vault),
             "scan",
             "--root", "claude-code=/tmp"],
            capture_output=True, text=True,
            env={**os.environ, **env, "PYTHONPATH": str(SRC)},
            check=False,
        )
        # Without init, scan should fail with non-zero exit code.
        assert proc.returncode != 0

    def test_decide_adopt_new_writes_memory_and_supersedes_old(
        self, isolated_vault, tmp_path, fake_provider_env
    ):
        """After a conflict is open, `decide adopt_new` writes the new
        memory and supersedes the old; the conflict projection closes."""
        root = tmp_path / "claude"
        proj = root / "projects" / "proj_decide"
        proj.mkdir(parents=True)
        s1 = proj / "sess_first.jsonl"
        s2 = proj / "sess_second.jsonl"
        s1.write_text(json.dumps({
            "type": "user", "uuid": "u01",
            "message": {"role": "user", "content": "use Python for the prototype"},
            "timestamp": "2026-10-08T10:00:00Z",
            "sessionId": "sess_first", "parentUuid": None,
        }) + "\n", encoding="utf-8")
        env = {**fake_provider_env, "MEMORY_VAULT": str(isolated_vault)}
        _run_cli(["--vault", str(isolated_vault), "init"], env)
        _run_cli(["--vault", str(isolated_vault),
                   "maintain", "--root", f"claude-code={root}"], env)
        s2.write_text(json.dumps({
            "type": "user", "uuid": "u10",
            "message": {"role": "user", "content": "use TypeScript for the dashboard"},
            "timestamp": "2026-10-08T11:00:00Z",
            "sessionId": "sess_second", "parentUuid": None,
        }) + "\n", encoding="utf-8")
        _run_cli(["--vault", str(isolated_vault),
                   "maintain", "--root", f"claude-code={root}"], env)
        cf = _run_cli(["--vault", str(isolated_vault), "conflicts"], env)
        open_conflicts = cf["result"].get("open", [])
        assert open_conflicts
        cid = open_conflicts[0]["conflict_id"]
        rev = open_conflicts[0]["revision"]
        # decide adopt_new with an idempotency key.
        r = _run_cli(
            ["--vault", str(isolated_vault), "decide",
             "--conflict-id", cid,
             "--proposal-revision", str(rev),
             "--action", "adopt_new",
             "--idempotency-key", "idem-decide-1"],
            env,
        )
        assert r["status"] == "ok"
        assert r["result"]["wrote_memory"] is True
        # Repeat decide on the now-closed conflict must fail. Run
        # through subprocess directly because the CLI exits non-zero
        # on errors (we want to assert the JSON error envelope, not
        # the exit code).
        proc = subprocess.run(
            [sys.executable, "-m", "memory_agent",
             "--vault", str(isolated_vault), "decide",
             "--conflict-id", cid,
             "--proposal-revision", str(rev),
             "--action", "keep_old",
             "--idempotency-key", "idem-decide-2"],
            capture_output=True, text=True,
            env={**os.environ, **env, "PYTHONPATH": str(SRC)},
            check=False,
        )
        r2 = json.loads(proc.stdout)
        assert r2["status"] == "error"
        assert r2["error"]["code"] in {"NOT_FOUND", "INVALID_REQUEST"}

    def test_decide_requires_idempotency_key(
        self, isolated_vault, tmp_path, fake_provider_env
    ):
        env = {**fake_provider_env, "MEMORY_VAULT": str(isolated_vault)}
        _run_cli(["--vault", str(isolated_vault), "init"], env)
        proc = subprocess.run(
            [sys.executable, "-m", "memory_agent",
             "--vault", str(isolated_vault), "decide",
             "--conflict-id", "cand_x",
             "--proposal-revision", "1",
             "--action", "defer"],
            capture_output=True, text=True,
            env={**os.environ, **env, "PYTHONPATH": str(SRC)},
            check=False,
        )
        assert proc.returncode != 0
        body = json.loads(proc.stdout)
        assert body["status"] == "error"
        assert body["error"]["code"] == "INVALID_REQUEST"

    def test_cross_batch_conflict_pending(
        self, isolated_vault, tmp_path, fake_provider_env
    ):
        """Two contradictory same-scope requirements produced in
        different batches (here: separate sessions) must end up as a
        pending conflict projection entry. Same-batch conflicts are
        limited to one user-target per batch by the planner policy, so
        they cannot actually generate a same-batch collision in v0.1.
        """
        root = tmp_path / "claude"
        proj = root / "projects" / "proj_same"
        proj.mkdir(parents=True)
        s1 = proj / "sess_first.jsonl"
        s2 = proj / "sess_second.jsonl"
        s1.write_text(json.dumps({
            "type": "user", "uuid": "u01",
            "message": {"role": "user", "content": "use Python for the prototype"},
            "timestamp": "2026-10-08T10:00:00Z",
            "sessionId": "sess_first", "parentUuid": None,
        }) + "\n", encoding="utf-8")
        env = {**fake_provider_env, "MEMORY_VAULT": str(isolated_vault)}
        _run_cli(["--vault", str(isolated_vault), "init"], env)
        # First maintain commits Python memory.
        r1 = _run_cli(["--vault", str(isolated_vault),
                        "maintain", "--root", f"claude-code={root}"], env)
        assert r1["status"] == "ok"
        # Add TypeScript and maintain again.
        s2.write_text(json.dumps({
            "type": "user", "uuid": "u10",
            "message": {"role": "user", "content": "use TypeScript for the dashboard"},
            "timestamp": "2026-10-08T11:00:00Z",
            "sessionId": "sess_second", "parentUuid": None,
        }) + "\n", encoding="utf-8")
        r2 = _run_cli(["--vault", str(isolated_vault),
                        "maintain", "--root", f"claude-code={root}"], env)
        assert r2["status"] == "ok"
        # The cross-batch requirement must show up in conflicts.
        cf = _run_cli(["--vault", str(isolated_vault), "conflicts"], env)
        open_conflicts = cf["result"].get("open", [])
        assert open_conflicts, "expected a pending conflict for contradictory cross-batch requirements"

    def test_unsupported_record_stops_cursor(
        self, isolated_vault, tmp_path, fake_provider_env
    ):
        """A bad record on a known-format file must NOT advance the
        cursor past it. Re-running scan must surface the same
        diagnostic and a zero new_events result. Once the bad line is
        replaced with a valid one, the cursor advances and the new
        event lands."""
        root = tmp_path / "claude"
        proj = root / "projects" / "proj_bad"
        proj.mkdir(parents=True)
        s = proj / "sess.jsonl"
        # First line is well-formed, second is unsupported.
        s.write_text(
            json.dumps({"type": "user", "uuid": "u01",
                        "message": {"role": "user", "content": "use Python for the prototype"},
                        "timestamp": "2026-10-08T10:00:00Z",
                        "sessionId": "sess", "parentUuid": None})
            + "\n" + json.dumps({"type": "weird"})
            + "\n", encoding="utf-8",
        )
        env = {**fake_provider_env, "MEMORY_VAULT": str(isolated_vault)}
        _run_cli(["--vault", str(isolated_vault), "init"], env)
        r = _run_cli(["--vault", str(isolated_vault),
                       "scan", "--root", f"claude-code={root}"], env)
        assert r["status"] == "ok"
        # The first good event should have been ingested; the bad one
        # must show up in diagnostics with code="unsupported_record"
        # and the cursor must remain before the bad line.
        diags = r["result"]["diagnostics"]
        assert any(d.get("code") == "unsupported_record" for d in diags)
        # Re-scan must see zero new events (cursor did not advance past bad).
        r2 = _run_cli(["--vault", str(isolated_vault),
                        "scan", "--root", f"claude-code={root}"], env)
        assert r2["status"] == "ok"
        assert r2["result"]["new_events"] == 0
        # Fix the bad line by overwriting it with a valid record.
        s.write_text(
            json.dumps({"type": "user", "uuid": "u02",
                        "message": {"role": "user", "content": "always log decisions"},
                        "timestamp": "2026-10-08T11:00:00Z",
                        "sessionId": "sess", "parentUuid": "u01"})
            + "\n", encoding="utf-8",
        )
        r3 = _run_cli(["--vault", str(isolated_vault),
                        "scan", "--root", f"claude-code={root}"], env)
        assert r3["status"] == "ok"
        assert r3["result"]["new_events"] == 1

    def test_bad_provider_marks_batch_failed_no_fake_no_memory(
        self, isolated_vault, tmp_path
    ):
        """When MEMORY_AGENT_MODEL_PROVIDER is the default (CLI) and
        `claude` is not on PATH (or opt-in is missing), `maintain`
        must NOT mark a batch as completed with no_memory. The batch
        stays retryable / failed."""
        root = tmp_path / "claude"
        proj = root / "projects" / "proj_noprov"
        proj.mkdir(parents=True)
        s = proj / "sess.jsonl"
        s.write_text(json.dumps({
            "type": "user", "uuid": "u01",
            "message": {"role": "user", "content": "use Python for the prototype"},
            "timestamp": "2026-10-08T10:00:00Z",
            "sessionId": "sess", "parentUuid": None,
        }) + "\n", encoding="utf-8")
        env = {"MEMORY_VAULT": str(isolated_vault)}
        # Wipe any provider env override so the default (CLI) wins.
        # Other tests (``test_http_api``, ``test_audit_queries``) call
        # ``os.environ.setdefault("MEMORY_AGENT_MODEL_PROVIDER", "fake")``
        # at module-import time and pollute the parent process; we must
        # strip those keys from ``os.environ`` too, not just from this
        # local ``env`` dict, or the subprocess inherits the fake
        # provider and silently produces ``ok=True`` candidates.
        clean_environ = {
            k: v for k, v in os.environ.items()
            if not k.startswith("MEMORY_AGENT_MODEL_PROVIDER")
            and not k.startswith("MEMORY_AGENT_ENABLE_CLAUDE_CLI")
        }
        env = {k: v for k, v in env.items()
                if not k.startswith("MEMORY_AGENT_MODEL_PROVIDER")
                and not k.startswith("MEMORY_AGENT_ENABLE_CLAUDE_CLI")}
        # Ensure claude is not visible to the subprocess.
        proc = subprocess.run(
            [sys.executable, "-m", "memory_agent",
             "--vault", str(isolated_vault),
             "init"], capture_output=True, text=True,
            env={**clean_environ, **env, "PYTHONPATH": str(SRC),
                 "PATH": "/usr/bin:/bin"}, check=False,
        )
        assert proc.returncode == 0
        proc = subprocess.run(
            [sys.executable, "-m", "memory_agent",
             "--vault", str(isolated_vault),
             "maintain", "--root", f"claude-code={root}"],
            capture_output=True, text=True,
            env={**clean_environ, **env, "PYTHONPATH": str(SRC),
                 "PATH": "/usr/bin:/bin"}, check=False,
        )
        body = json.loads(proc.stdout)
        # Direct assertions against the persisted state: no batch may
        # reach ``completed`` and no model_call row may exist when the
        # provider was refused. The subprocess either fails the whole
        # call (status=error) or marks every batch as failed; either
        # way, the durable state must show no silent success.
        state_db = isolated_vault / "state.sqlite"
        if state_db.exists():
            import sqlite3 as _sqlite3
            conn = _sqlite3.connect(str(state_db))
            try:
                batches_rows = conn.execute(
                    "SELECT batch_id, status FROM batches"
                ).fetchall()
                assert all(r["status"] != "completed" for r in batches_rows), (
                    f"no batch may reach status=completed without a real "
                    f"provider attempt; got {[dict(r) for r in batches_rows]}"
                )
                mc_count = conn.execute(
                    "SELECT COUNT(*) AS c FROM model_calls"
                ).fetchone()
                assert int(mc_count["c"]) == 0, (
                    "no model_call row may exist when the provider was "
                    "refused (the call never happened)"
                )
            finally:
                conn.close()
        # Either: status="error" with INTERNAL/PROVIDER_UNAVAILABLE
        # (provider constructor refused), or status="ok" with at
        # least one failed batch and no silent ``completed`` /
        # ``no_memory`` outcome.
        if body["status"] == "ok":
            processed = body["result"]["processed"]
            assert processed, "scan must have produced a batch"
            assert all(not p.get("ok") for p in processed), \
                "no batch may be marked ok without a real provider attempt"
            # No batch may reach completed / no_memory outcome when
            # the provider was unavailable.
            for p in processed:
                assert p.get("status") not in {"completed", "no_memory"}, (
                    f"batch {p.get('batch_id')!r} reached "
                    f"status={p.get('status')!r} without a real provider "
                    f"attempt"
                )
        else:
            assert body["status"] == "error"
            assert body["error"]["code"] in {"INTERNAL", "PROVIDER_UNAVAILABLE"}

    def test_decide_keep_old_does_not_write_new_memory(
        self, isolated_vault, tmp_path, fake_provider_env
    ):
        root = tmp_path / "claude"
        proj = root / "projects" / "proj_keep"
        proj.mkdir(parents=True)
        s1 = proj / "sess_first.jsonl"
        s2 = proj / "sess_second.jsonl"
        s1.write_text(json.dumps({
            "type": "user", "uuid": "u01",
            "message": {"role": "user", "content": "use Python for the prototype"},
            "timestamp": "2026-10-08T10:00:00Z",
            "sessionId": "sess_first", "parentUuid": None,
        }) + "\n", encoding="utf-8")
        env = {**fake_provider_env, "MEMORY_VAULT": str(isolated_vault)}
        _run_cli(["--vault", str(isolated_vault), "init"], env)
        _run_cli(["--vault", str(isolated_vault),
                   "maintain", "--root", f"claude-code={root}"], env)
        s2.write_text(json.dumps({
            "type": "user", "uuid": "u10",
            "message": {"role": "user", "content": "use TypeScript for the dashboard"},
            "timestamp": "2026-10-08T11:00:00Z",
            "sessionId": "sess_second", "parentUuid": None,
        }) + "\n", encoding="utf-8")
        _run_cli(["--vault", str(isolated_vault),
                   "maintain", "--root", f"claude-code={root}"], env)
        cf = _run_cli(["--vault", str(isolated_vault), "conflicts"], env)
        open_conflicts = cf["result"].get("open", [])
        assert open_conflicts
        cid = open_conflicts[0]["conflict_id"]
        rev = open_conflicts[0]["revision"]
        # Count active memories before decide.
        rec_before = _run_cli(["--vault", str(isolated_vault),
                                "recall", "--query", "TypeScript"], env)
        before_count = len(rec_before["result"].get("items", []))
        r = _run_cli(
            ["--vault", str(isolated_vault), "decide",
             "--conflict-id", cid,
             "--proposal-revision", str(rev),
             "--action", "keep_old",
             "--idempotency-key", "idem-keep-1"],
            env,
        )
        assert r["status"] == "ok"
        assert r["result"]["wrote_memory"] is False
        rec_after = _run_cli(["--vault", str(isolated_vault),
                               "recall", "--query", "TypeScript"], env)
        after_count = len(rec_after["result"].get("items", []))
        # keep_old must NOT add a TypeScript memory.
        assert after_count == before_count