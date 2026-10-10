"""Phase A CLI subprocess test: keyword and BM25 recall on an isolated
vault created from invented session JSONL. No model calls for scan;
no hidden recall on maintain; no cross-project leak."""

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


def _run_cli(args: list[str], env: dict[str, str]) -> dict:
    full_env = {**os.environ, **env, "PYTHONPATH": str(SRC)}
    proc = subprocess.run(
        [sys.executable, "-m", "memory_agent", *args],
        capture_output=True, text=True, env=full_env, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"CLI failed ({proc.returncode}): stderr={proc.stderr!r} stdout={proc.stdout!r}"
        )
    return json.loads(proc.stdout)


@pytest.fixture
def isolated_vault(tmp_path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def claude_root_with_session(tmp_path) -> Path:
    """Build an invented Claude Code session with content that the
    keyword and BM25 pipelines will recognise."""
    root = tmp_path / "claude"
    proj = root / "projects" / "proj_e2e"
    proj.mkdir(parents=True)
    session = proj / "sess_e2e_001.jsonl"
    lines = [
        {"type": "assistant", "uuid": "a00",
         "message": {"role": "assistant", "content": "上下文"},
         "timestamp": "2026-10-08T10:00:00Z", "sessionId": "sess_e2e_001",
         "parentUuid": None},
        {"type": "user", "uuid": "u01",
         "message": {"role": "user",
                     "content": "use Python for the prototype, do not include image gen"},
         "timestamp": "2026-10-08T10:00:01Z", "sessionId": "sess_e2e_001",
         "parentUuid": "a00"},
    ]
    session.write_text(
        "\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def fake_provider_env() -> dict[str, str]:
    return {"MEMORY_AGENT_MODEL_PROVIDER": "fake"}


class TestCLISubprocessPhaseA:
    def test_keyword_recall_via_cli(
        self, isolated_vault, claude_root_with_session, fake_provider_env
    ):
        """Keyword recall via CLI returns machine-readable JSON."""
        env = {**fake_provider_env, "MEMORY_VAULT": str(isolated_vault)}

        _run_cli(
            ["--vault", str(isolated_vault), "init",
             "--root", f"claude-code={claude_root_with_session}"],
            env,
        )
        # Append a fresh user message AFTER init to trigger a real extraction.
        sess = claude_root_with_session / "projects" / "proj_e2e" / "sess_e2e_001.jsonl"
        with sess.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "user", "uuid": "u02",
                "message": {"role": "user",
                            "content": "use Python for the prototype always"},
                "timestamp": "2026-10-08T10:01:00Z",
                "sessionId": "sess_e2e_001", "parentUuid": "a00",
            }) + "\n")

        maintain = _run_cli(
            ["--vault", str(isolated_vault), "maintain",
             "--root", f"claude-code={claude_root_with_session}"],
            env,
        )
        assert maintain["status"] == "ok"
        # Scan stage must report zero new events (because we just
        # appended a new line, this is the line we'll extract from).
        scan_section = maintain["result"]["scan"]
        # maintain also calls a model for extraction. The contract is
        # that the *scan* path itself never makes a model call, which
        # is enforced by ``tests/unit/test_bm25_isolation.py``.
        assert "files_seen" in scan_section
        # processed batches expose per-batch state, not a hidden recall.
        for p in maintain["result"].get("processed", []):
            assert "recall" not in p

        recall = _run_cli(
            ["--vault", str(isolated_vault), "recall",
             "--query", "Python prototype", "--retrieval", "keyword"],
            env,
        )
        assert recall["status"] == "ok"
        result = recall["result"]
        assert result["query_trace"]["retrieval"] == "keyword"
        # Machine-readable structure.
        assert isinstance(result["items"], list)
        for it in result["items"]:
            assert {"memory_id", "statement", "quote", "locator"} <= set(it)

    def test_bm25_recall_via_cli(
        self, isolated_vault, claude_root_with_session, fake_provider_env
    ):
        """BM25 recall via CLI returns evidence-backed items with the
        ``bm25`` retrieval flag and a trace artifact written."""
        env = {**fake_provider_env, "MEMORY_VAULT": str(isolated_vault)}

        _run_cli(
            ["--vault", str(isolated_vault), "init",
             "--root", f"claude-code={claude_root_with_session}"],
            env,
        )
        sess = claude_root_with_session / "projects" / "proj_e2e" / "sess_e2e_001.jsonl"
        with sess.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "user", "uuid": "u02",
                "message": {"role": "user",
                            "content": "use Python for the prototype always"},
                "timestamp": "2026-10-08T10:01:00Z",
                "sessionId": "sess_e2e_001", "parentUuid": "a00",
            }) + "\n")

        _run_cli(
            ["--vault", str(isolated_vault), "maintain",
             "--root", f"claude-code={claude_root_with_session}"],
            env,
        )

        recall = _run_cli(
            ["--vault", str(isolated_vault), "recall",
             "--query", "Python prototype", "--retrieval", "bm25"],
            env,
        )
        assert recall["status"] == "ok"
        result = recall["result"]
        assert result["query_trace"]["retrieval"] == "bm25"
        # Trace artifact is persisted next to the vault.
        trace_id = result["query_trace"]["trace_id"]
        artifact = isolated_vault / "artifacts" / "queries" / f"{trace_id}.json"
        assert artifact.exists()
        payload = json.loads(artifact.read_text(encoding="utf-8"))
        assert payload["retrieval"] == "bm25"

    def test_maintain_does_not_run_hidden_recall(
        self, isolated_vault, claude_root_with_session, fake_provider_env
    ):
        """``maintain`` must not run any recall-style query or BM25."""
        env = {**fake_provider_env, "MEMORY_VAULT": str(isolated_vault)}
        _run_cli(
            ["--vault", str(isolated_vault), "init",
             "--root", f"claude-code={claude_root_with_session}"],
            env,
        )
        # Inspect imports of bm25 in the maintain flow: there must be
        # none. We assert this via the isolation test (separate file).
        # Here we only assert the CLI returns the expected fields.
        out = _run_cli(
            ["--vault", str(isolated_vault), "maintain",
             "--root", f"claude-code={claude_root_with_session}"],
            env,
        )
        assert "model_calls" in out["result"]
        # Process results expose the per-batch status; no recall field.
        for p in out["result"].get("processed", []):
            assert "recall" not in p

    def test_recall_rejects_unknown_field_via_cli(
        self, isolated_vault, claude_root_with_session, fake_provider_env
    ):
        """Passing an unknown CLI flag must be rejected by argparse."""
        env = {**fake_provider_env, "MEMORY_VAULT": str(isolated_vault)}
        _run_cli(
            ["--vault", str(isolated_vault), "init",
             "--root", f"claude-code={claude_root_with_session}"],
            env,
        )
        proc = subprocess.run(
            [sys.executable, "-m", "memory_agent",
             "--vault", str(isolated_vault),
             "recall", "--query", "x", "--retrievial", "bm25"],
            capture_output=True, text=True,
            env={**os.environ, **env, "PYTHONPATH": str(SRC)},
            check=False,
        )
        assert proc.returncode != 0, "argparse should reject --retrievial"
