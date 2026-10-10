"""Pytest configuration & shared fixtures.

v0.1 fixtures are *constructed* (sanitised Claude Code / Codex JSONL
shapes) — never copied from real user sessions.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure src/ is importable without installing.
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_agent.infrastructure.files import VaultLayout
from memory_agent.infrastructure.sqlite import open_state_db


@pytest.fixture
def tmp_vault(tmp_path) -> VaultLayout:
    vault = VaultLayout(tmp_path)
    return vault


@pytest.fixture
def tmp_conn(tmp_vault):
    conn = open_state_db(tmp_vault.state_db)
    yield conn
    conn.close()


@pytest.fixture
def claude_root(tmp_path: Path) -> Path:
    """Build a sanitised project tree for testing.

    <root>/projects/<project>/<sessionId>.jsonl with two user lines
    interleaved with assistant and tool records. No real user data.
    """
    root = tmp_path / "claude"
    project = root / "projects" / "proj_test_alpha"
    project.mkdir(parents=True)
    session = project / "sess_test_001.jsonl"
    lines = [
        {"type": "user", "uuid": "u01", "message": {"role": "user", "content": "use Python for the prototype"},
         "timestamp": "2026-10-08T10:00:00Z", "sessionId": "sess_test_001", "parentUuid": None},
        {"type": "assistant", "uuid": "a01", "message": {"role": "assistant", "content": "Got it, I'll use Python."},
         "timestamp": "2026-10-08T10:00:01Z", "sessionId": "sess_test_001", "parentUuid": "u01"},
        {"type": "tool_use", "uuid": "t01", "message": {"role": "tool", "content": ""},
         "timestamp": "2026-10-08T10:00:02Z", "sessionId": "sess_test_001", "parentUuid": "a01"},
        {"type": "user", "uuid": "u02", "message": {"role": "user", "content": "always log decisions to vault/audit"},
         "timestamp": "2026-10-08T10:01:00Z", "sessionId": "sess_test_001", "parentUuid": "a01"},
        {"type": "assistant", "uuid": "a02", "message": {"role": "assistant", "content": "Will do."},
         "timestamp": "2026-10-08T10:01:01Z", "sessionId": "sess_test_001", "parentUuid": "u02"},
    ]
    with session.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False))
            f.write("\n")
    return root


@pytest.fixture
def codex_root(tmp_path: Path) -> Path:
    root = tmp_path / "codex"
    sessions = root / "sessions"
    sessions.mkdir(parents=True)
    session = sessions / "sess_codex_001.jsonl"
    lines = [
        {"event": "message", "id": "cu01", "role": "user", "text": "默认用中文回答",
         "parent": None, "ts": "2026-10-08T11:00:00Z", "session": "sess_codex_001"},
        {"event": "message", "id": "ca01", "role": "assistant", "text": "好的。",
         "parent": "cu01", "ts": "2026-10-08T11:00:01Z", "session": "sess_codex_001"},
        {"event": "message", "id": "cu02", "role": "user", "text": "也用 TypeScript for the dashboard.",
         "parent": "ca01", "ts": "2026-10-08T11:01:00Z", "session": "sess_codex_001"},
    ]
    with session.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False))
            f.write("\n")
    return root