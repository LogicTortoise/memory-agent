"""Seed an invented vault for the audit-UI Playwright suite.

The seeder runs entirely against a temp vault with the fake model
provider. It exercises:

  * init + maintain (twice) + recall, so the audit API has
    sources, batches, memories, an open conflict, and a query trace.
  * The second maintain run appends a session with a contradictory
    requirement (TypeScript vs Python), so the reconciler opens a
    ``conflict_projection`` row the UI can drive.

Returns a ``Fixture`` dataclass with the URL the FastAPI app is bound
on (loopback only) plus the session token the UI must echo.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

os.environ.setdefault("MEMORY_AGENT_MODEL_PROVIDER", "fake")
os.environ.setdefault("MEMORY_AGENT_HTTP_TOKEN", "playwright-test-token")


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@dataclass
class Fixture:
    vault_root: Path
    base_url: str
    session_token: str
    conflict_id: str
    proposal_revision: int
    memory_id: str
    batch_ids: list[str]
    trace_id: str
    server: Any | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False))
        f.write("\n")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def seed_invented_vault(vault_root: Path) -> dict[str, Any]:
    """Drive an end-to-end fake pipeline producing memories + conflict."""

    from memory_agent.application.dispatch import (
        dispatch as legacy_dispatch,
    )
    from memory_agent.application.recall import RecallService
    from memory_agent.infrastructure.files import VaultLayout
    from memory_agent.infrastructure.sqlite import open_state_db
    from memory_agent.infrastructure.sqlite.repositories import MemoryProjectionRepo

    vault = VaultLayout(vault_root)
    claude_root = vault_root / "fixtures" / "claude-code"
    project = claude_root / "projects" / "proj_pw"
    project.mkdir(parents=True, exist_ok=True)
    sess_a = project / "sess_pw_a.jsonl"
    sess_b = project / "sess_pw_b.jsonl"

    # First session — pinned at init, but **not** yet a target because
    # init --root pins everything at EOF. The maintain step that
    # follows then sees the new line as a target.
    _append_jsonl(
        sess_a,
        {
            "type": "user",
            "uuid": "pw_a_u01",
            "message": {"role": "user", "content": "use Python for the prototype"},
            "timestamp": "2026-10-09T00:00:00Z",
            "sessionId": "sess_pw_a",
            "parentUuid": None,
        },
    )
    _append_jsonl(
        sess_a,
        {
            "type": "assistant",
            "uuid": "pw_a_a01",
            "message": {"role": "assistant", "content": "OK Python."},
            "timestamp": "2026-10-09T00:00:01Z",
            "sessionId": "sess_pw_a",
            "parentUuid": "pw_a_u01",
        },
    )

    # init pins existing files at EOF; backfill is opt-in only.
    init_resp = legacy_dispatch(
        operation="init",
        params={"roots": {"claude-code": str(claude_root)}},
        request_id="pw_init",
        vault_root=vault_root,
    )
    assert init_resp["status"] == "ok", init_resp

    # Append a new user turn AFTER init so maintain picks it up.
    _append_jsonl(
        sess_a,
        {
            "type": "user",
            "uuid": "pw_a_u02",
            "message": {"role": "user", "content": "always use Python for prototypes"},
            "timestamp": "2026-10-09T00:00:02Z",
            "sessionId": "sess_pw_a",
            "parentUuid": "pw_a_a01",
        },
    )

    r1 = legacy_dispatch(
        operation="maintain",
        params={"max_batches": 5, "roots": {"claude-code": str(claude_root)}},
        request_id="pw_maintain_1",
        vault_root=vault_root,
    )
    assert r1["status"] == "ok", r1

    # Second session with a contradictory requirement.
    _append_jsonl(
        sess_b,
        {
            "type": "user",
            "uuid": "pw_b_u01",
            "message": {"role": "user", "content": "use TypeScript for prototypes"},
            "timestamp": "2026-10-09T00:01:00Z",
            "sessionId": "sess_pw_b",
            "parentUuid": None,
        },
    )
    _append_jsonl(
        sess_b,
        {
            "type": "assistant",
            "uuid": "pw_b_a01",
            "message": {"role": "assistant", "content": "Will do."},
            "timestamp": "2026-10-09T00:01:01Z",
            "sessionId": "sess_pw_b",
            "parentUuid": "pw_b_u01",
        },
    )

    r2 = legacy_dispatch(
        operation="maintain",
        params={"max_batches": 5, "roots": {"claude-code": str(claude_root)}},
        request_id="pw_maintain_2",
        vault_root=vault_root,
    )
    assert r2["status"] == "ok", r2

    conn = open_state_db(vault.state_db)
    try:
        # Force one recall so the queries page has a trace.
        rs = RecallService(
            vault=vault,
            memory_projection_repo=MemoryProjectionRepo(conn),
            audit_conn=conn,
        )
        rs.recall(
            query="Python prototype",
            project_ids=[],
            domain_ids=[],
            include_archived=False,
            limit=8,
            retrieval="keyword",
            rewrite="off",
            expand="off",
            cursor=None,
            audit=True,
        )
        conn.commit()

        conflicts = conn.execute(
            "SELECT conflict_id, revision, status FROM conflict_projection "
            "ORDER BY opened_at"
        ).fetchall()
        if not conflicts:
            raise RuntimeError(
                "fixture seeder failed: no conflicts produced — fake provider may "
                "have treated the two statements as duplicates; tweak the session text."
            )
        first = conflicts[0]
        trace_row = conn.execute(
            "SELECT object_id FROM audit_events WHERE action = 'query.retrieved' "
            "ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        memory_row = conn.execute(
            "SELECT memory_id FROM memory_projection WHERE status = 'active' "
            "ORDER BY revision LIMIT 1"
        ).fetchone()
        batch_rows = conn.execute(
            "SELECT batch_id FROM batches ORDER BY created_at"
        ).fetchall()
        return {
            "conflict_id": first["conflict_id"],
            "proposal_revision": first["revision"],
            "memory_id": memory_row["memory_id"] if memory_row else "",
            "batch_ids": [r["batch_id"] for r in batch_rows],
            "trace_id": trace_row["object_id"] if trace_row else "",
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


def start_loopback_server(
    vault_root: Path,
    port: int,
    *,
    ui_dist: Path | None = None,
) -> Any:
    import uvicorn
    from memory_agent.interfaces.http.app import create_app

    http_app = create_app(
        vault_root=vault_root,
        bind_host="127.0.0.1",
        port=port,
        ui_dist=ui_dist,
    )
    http_app.auth.allowed_origins.append(f"http://127.0.0.1:{port}")
    http_app.auth.session_token = os.environ["MEMORY_AGENT_HTTP_TOKEN"]
    http_app.app.state.auth.session_token = http_app.auth.session_token

    config = uvicorn.Config(http_app.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, name="pw-fixture-server", daemon=True)
    t.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        if server.started:
            break
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("uvicorn server failed to start")
    return server


def build_fixture(tmp_path: Path) -> Fixture:
    vault_root = tmp_path / "vault"
    ids = seed_invented_vault(vault_root)
    port = _free_port()
    server = start_loopback_server(vault_root, port)
    return Fixture(
        vault_root=vault_root,
        base_url=f"http://127.0.0.1:{port}",
        session_token=os.environ["MEMORY_AGENT_HTTP_TOKEN"],
        conflict_id=ids["conflict_id"],
        proposal_revision=ids["proposal_revision"],
        memory_id=ids["memory_id"],
        batch_ids=ids["batch_ids"],
        trace_id=ids["trace_id"],
        server=server,
    )


def main() -> None:  # pragma: no cover - manual entry point
    import argparse
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build a fresh fixture and print JSON")
    b.add_argument("--vault-root", required=True, help="vault directory to seed")
    b.add_argument("--keep-running", action="store_true",
                    help="keep the loopback server alive (for development)")
    b.add_argument("--ui-dist", default=None,
                    help="path to a built Vite dist to mount at /")
    c = sub.add_parser("bump-conflict", help="bump conflict.revision (test only)")
    c.add_argument("--vault-root", required=True)
    c.add_argument("--conflict-id", required=True)
    args = p.parse_args()

    if args.cmd == "build":
        ids = seed_invented_vault(Path(args.vault_root))
        port = _free_port()
        server = start_loopback_server(
            Path(args.vault_root), port,
            ui_dist=Path(args.ui_dist) if args.ui_dist else None,
        )
        _ = server  # retained for the lifetime of this process
        out = {
            "base_url": f"http://127.0.0.1:{port}",
            "session_token": os.environ["MEMORY_AGENT_HTTP_TOKEN"],
            "conflict_id": ids["conflict_id"],
            "proposal_revision": ids["proposal_revision"],
            "memory_id": ids["memory_id"],
            "batch_ids": ids["batch_ids"],
            "trace_id": ids["trace_id"],
        }
        print(json.dumps(out, ensure_ascii=False))
        sys.stdout.flush()
        if args.keep_running:
            try:
                while True:
                    time.sleep(60)
            except KeyboardInterrupt:
                pass

    if args.cmd == "bump-conflict":
        # Bump the conflict revision directly. Used by the Playwright
        # "stale revision" test to make the UI's cached revision stale
        # without going through the API (which would resolve or refuse
        # the decide itself).
        import sqlite3
        db = Path(args.vault_root) / "state" / "state.sqlite"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "UPDATE conflict_projection SET revision = revision + 1 "
            "WHERE conflict_id = ?",
            (args.conflict_id,),
        )
        conn.commit()
        conn.close()
        print(json.dumps({"ok": True}))


if __name__ == "__main__":
    main()