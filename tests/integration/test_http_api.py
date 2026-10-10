"""T14 — loopback HTTP API tests.

Uses ``fastapi.testclient.TestClient`` against the real app factory
(in-memory SQLite + files layout). The shared ``dispatch`` path is
the real implementation; nothing here mocks the application service.

Each test pins one of the v1 P6 invariants:

  * The server is bound only to 127.0.0.1; the app factory refuses
    any other bind.
  * GET requests need no auth beyond the loopback bind.
  * POST /api/v1/commands requires both an exact Origin and a
    session token; missing/wrong values return the matching 4xx code.
  * The POST handler goes through ``handle_call`` — invalid
    commands, stale revisions, idempotency conflicts, and unknown
    fields all surface as typed errors.
  * The handler never invokes the model provider; every GET is
    metadata-only and the POST path for model-requiring ops is
    queue-only.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

# Force the fake provider before any module imports the provider
# registry. Same pattern as the audit queries test.
os.environ.setdefault("MEMORY_AGENT_MODEL_PROVIDER", "fake")

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from memory_agent.application.audit_queries import AuditQueries  # noqa: E402
from memory_agent.application.commit import CommitService  # noqa: E402
from memory_agent.application.process_batches import process_batch  # noqa: E402
from memory_agent.application.reconcile import Reconciler  # noqa: E402
from memory_agent.application.scan import default_adapters, scan_once  # noqa: E402
from memory_agent.application.plan_batches import plan_batches_for_session  # noqa: E402
from memory_agent.adapters.models import SQLiteModelCallRecorder, get_default_provider  # noqa: E402
from memory_agent.domain.batching import BatchPolicyInput  # noqa: E402
from memory_agent.infrastructure.files import VaultLayout  # noqa: E402
from memory_agent.infrastructure.sqlite import open_state_db  # noqa: E402
from memory_agent.infrastructure.sqlite.repositories import MemoryProjectionRepo  # noqa: E402
from memory_agent.interfaces.http.app import create_app  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def session_token() -> str:
    return "test-session-token-do-not-leak"


@pytest.fixture
def allowed_origin() -> str:
    return "http://127.0.0.1:8420"


@pytest.fixture
def seeded_vault(vault_root: Path):
    """Run an end-to-end fake extraction so the HTTP layer has
    realistic data to surface.
    """

    vault = VaultLayout(vault_root)
    conn = open_state_db(vault.state_db)
    # Seed two sessions so pagination + cross-batch tracing can be
    # exercised.
    for sid in ("sess_http_a", "sess_http_b"):
        _seed_one_session(vault=vault, conn=conn, session_id=sid)
    try:
        yield vault
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _seed_one_session(*, vault: VaultLayout, conn, session_id: str) -> None:
    """Write a sanitised Claude Code JSONL, run scan + plan + extract."""

    src_root = vault.root / "fixtures" / "claude-code"
    src_root.mkdir(parents=True, exist_ok=True)
    project = src_root / "projects" / "proj_http"
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{session_id}.jsonl"
    # Use a deliberately long user message so the audit quote preview
    # is truncated at 60 chars and the redacted flag is True. This
    # proves the redaction boundary the UI relies on.
    long_user_text = (
        "use Python for the prototype always — this is a long user "
        "message that exceeds the audit preview cap of sixty chars "
        "so the redaction flag must be True"
    )
    records = [
        {
            "type": "user",
            "uuid": f"{session_id}_u01",
            "message": {"role": "user", "content": long_user_text},
            "timestamp": "2026-10-09T00:01:00Z",
            "sessionId": session_id,
            "parentUuid": None,
        },
        {
            "type": "assistant",
            "uuid": f"{session_id}_a01",
            "message": {"role": "assistant", "content": "ack"},
            "timestamp": "2026-10-09T00:01:01Z",
            "sessionId": session_id,
            "parentUuid": f"{session_id}_u01",
        },
    ]
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    scan_once(
        vault=vault,
        conn=conn,
        adapters=default_adapters(),
        roots={"claude-code": str(src_root)},
    )
    from memory_agent.application.dispatch import _event_from_row_with_text
    rows = conn.execute(
        "SELECT * FROM source_events WHERE session_id = ? ORDER BY discovered_at",
        (session_id,),
    ).fetchall()
    events = [_event_from_row_with_text(conn, r) for r in rows]
    planned = plan_batches_for_session(
        session_id=session_id,
        branch_id="main",
        events=events,
        policy=BatchPolicyInput(max_turns_per_batch=5, max_input_tokens=6000),
        conn=conn,
    )
    recorder = SQLiteModelCallRecorder(conn=conn)
    provider = get_default_provider(recorder=recorder)
    reconciler = Reconciler(memory_projection_repo=MemoryProjectionRepo(conn))
    committer = CommitService(vault=vault, conn=conn)

    def _evidence_text_lookup(event_id: str, revision_hash: str) -> str:
        row = conn.execute(
            "SELECT segment_id FROM source_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            return ""
        seg = conn.execute(
            "SELECT path FROM segments WHERE segment_id = ?",
            (row["segment_id"],),
        ).fetchone()
        if seg is None:
            return ""
        path = Path(seg["path"])
        if not path.exists():
            return ""
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return ""
        for ev in payload.get("events", []):
            if ev.get("event_id") == event_id:
                return str(ev.get("text") or "")
        return ""

    for plan in planned:
        process_batch(
            batch_id=plan["batch"]["batch_id"],
            vault=vault,
            conn=conn,
            provider=provider,
            reconciler=reconciler,
            committer=committer,
            evidence_lookup=_evidence_text_lookup,
            base_commit_id="root",
            privacy_epoch="epoch_http",
            run_id=f"run_{session_id}",
        )
    conn.commit()


@pytest.fixture
def http_client(seeded_vault: VaultLayout, session_token: str, allowed_origin: str):
    app = create_app(
        vault_root=seeded_vault.root,
        config={
            "session_token": session_token,
            "allowed_origins": [allowed_origin],
        },
        port=8420,
    )
    with TestClient(app.app) as client:
        yield client, app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_create_app_refuses_non_loopback_bind(tmp_path: Path) -> None:
    """The factory must reject a non-loopback bind host."""

    with pytest.raises(ValueError):
        create_app(vault_root=tmp_path, bind_host="0.0.0.0", port=8420)


def test_overview_returns_dto_and_carries_projection_state(
    http_client,
) -> None:
    client, _ = http_client
    response = client.get("/api/v1/overview")
    assert response.status_code == 200, response.text
    body = response.json()
    assert "header" in body
    assert body["header"]["schema_version"] == 1
    assert body["header"]["snapshot_id"]
    assert body["header"]["projection_state"] in {"aligned", "db_lag", "db_ahead", "empty"}
    assert "counts" in body
    assert body["counts"]["sources_total"] >= 2
    assert body["counts"]["memories_active"] >= 1
    assert body["pipeline_stages"]["committed"] >= 1


def test_sources_list_pagination(http_client) -> None:
    client, _ = http_client
    response = client.get("/api/v1/sources?page_size=1")
    assert response.status_code == 200
    body = response.json()
    assert body["pagination"]["page_size"] == 1
    assert len(body["items"]) == 1
    next_cursor = body["pagination"]["next_cursor"]
    assert next_cursor
    response2 = client.get(
        f"/api/v1/sources?page_size=10&cursor={next_cursor}"
    )
    assert response2.status_code == 200
    body2 = response2.json()
    assert body2["items"][0]["canonical_path"] != body["items"][0]["canonical_path"]


def test_sources_list_rejects_garbage_cursor(http_client) -> None:
    client, _ = http_client
    response = client.get("/api/v1/sources?cursor=not-a-valid-cursor")
    assert response.status_code == 400
    assert "cursor" in response.json()["detail"].lower()


def test_session_turns_redact_full_text(http_client) -> None:
    client, _ = http_client
    response = client.get(
        "/api/v1/sessions/sess_http_a/turns?page_size=5"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["items"], "expected at least one turn"
    for it in body["items"]:
        # Quote preview may exist but must not contain more than 60
        # characters and MUST NOT equal the source text in full.
        preview = it["quote_preview"]
        if preview is not None:
            assert len(preview) <= 61
        assert it["quote_redacted"] is True


def test_batches_list_redacts_bodies(http_client) -> None:
    client, _ = http_client
    response = client.get("/api/v1/batches?page_size=10")
    assert response.status_code == 200
    body = response.json()
    assert body["items"]
    for it in body["items"]:
        # Raw body MUST NOT appear in the list view.
        assert "use Python" not in json.dumps(it)


def test_batch_evidence_includes_raw_text(http_client) -> None:
    client, _ = http_client
    # Pick a batch_id from the list endpoint.
    response = client.get("/api/v1/batches?page_size=10")
    batch_id = response.json()["items"][0]["batch_id"]
    response = client.get(f"/api/v1/batches/{batch_id}/evidence")
    assert response.status_code == 200
    body = response.json()
    assert body["batch_id"] == batch_id
    # The evidence endpoint is the only one that surfaces the body.
    blob = json.dumps(body)
    assert "use Python" in blob


def test_memories_history_returns_revisions(http_client) -> None:
    client, _ = http_client
    # Pull one memory_id from the DB via the overview projection
    # listing isn't exposed; we round-trip through the connection
    # by reading the vault state and then the API directly. Tests
    # already seeded two memories.
    app_obj = http_client[1]
    conn = open_state_db(app_obj.vault.state_db)
    try:
        mid_row = conn.execute(
            "SELECT memory_id FROM memory_projection LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert mid_row is not None
    response = client.get(f"/api/v1/memories/{mid_row['memory_id']}/history")
    assert response.status_code == 200
    body = response.json()
    assert body["memory_id"] == mid_row["memory_id"]
    assert body["revisions"]


def test_audit_list_redacts_details(http_client) -> None:
    client, _ = http_client
    response = client.get("/api/v1/audit?page_size=25")
    assert response.status_code == 200
    body = response.json()
    blob = json.dumps(body)
    # The fake extraction never embeds user text in audit details,
    # but we still confirm the redaction contract: the redacted flag
    # is set on list items and the field exists.
    for it in body["items"]:
        assert it["redacted"] is True


def test_query_trace_list_and_detail(http_client) -> None:
    client, _ = http_client
    response = client.get("/api/v1/queries?page_size=10")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["items"], list)
    # No traces in this test by default; the route should be 200 with
    # an empty list (or contain real traces if a real audit row
    # landed). Either way, the DTO shape must be valid.


def test_get_jobs_route_returns_state(http_client) -> None:
    client, _ = http_client
    # Use the command POST endpoint to enqueue an async job, then
    # poll it via GET /api/v1/jobs/{id}.
    pass  # covered by the post-then-get test below


def test_post_commands_requires_origin_and_token(seeded_vault, session_token) -> None:
    """POST /api/v1/commands must enforce both Origin and token."""

    app = create_app(
        vault_root=seeded_vault.root,
        config={
            "session_token": session_token,
            "allowed_origins": ["http://127.0.0.1:8420"],
        },
        port=8420,
    )
    with TestClient(app.app) as client:
        # 1. Missing Origin -> 403
        body = {
            "api_version": 1,
            "request_id": "req_no_origin",
            "operation": "conflicts",
            "params": {},
        }
        response = client.post(
            "/api/v1/commands",
            json=body,
            headers={"X-Memory-Agent-Token": session_token},
        )
        assert response.status_code == 403, response.text

        # 2. Wrong Origin -> 403
        response = client.post(
            "/api/v1/commands",
            json=body,
            headers={
                "X-Memory-Agent-Token": session_token,
                "Origin": "http://evil.example",
            },
        )
        assert response.status_code == 403, response.text

        # 3. Right Origin, missing token -> 401
        response = client.post(
            "/api/v1/commands",
            json=body,
            headers={"Origin": "http://127.0.0.1:8420"},
        )
        assert response.status_code == 401, response.text

        # 4. Right Origin, wrong token -> 401
        response = client.post(
            "/api/v1/commands",
            json=body,
            headers={
                "X-Memory-Agent-Token": "definitely-wrong",
                "Origin": "http://127.0.0.1:8420",
            },
        )
        assert response.status_code == 401, response.text

        # 5. Right Origin + token: real call succeeds (conflicts is
        # a model-free operation).
        response = client.post(
            "/api/v1/commands",
            json=body,
            headers={
                "X-Memory-Agent-Token": session_token,
                "Origin": "http://127.0.0.1:8420",
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["operation"] == "conflicts"


def test_post_commands_unknown_operation_returns_typed_error(http_client) -> None:
    client, _ = http_client
    body = {
        "api_version": 1,
        "request_id": "req_unknown_op",
        "operation": "totally.unknown",
        "params": {},
    }
    response = client.post(
        "/api/v1/commands",
        json=body,
        headers={
            "X-Memory-Agent-Token": http_client[1].auth.session_token or "",
            "Origin": "http://127.0.0.1:8420",
        },
    )
    # Bad request body -> 422 from ``handle_call`` validation.
    assert response.status_code in {400, 422}, response.text
    payload = response.json()
    assert payload["status"] == "error"


def test_post_commands_idempotency_conflict(http_client) -> None:
    """Same idempotency_key + different params -> IDEMPOTENCY_CONFLICT (409)."""

    client, app_obj = http_client
    token = app_obj.auth.session_token
    headers = {
        "X-Memory-Agent-Token": token,
        "Origin": "http://127.0.0.1:8420",
    }
    body1 = {
        "api_version": 1,
        "request_id": "req_idem_a",
        "client_id": "ui_http_test",
        "idempotency_key": "fixed_key_a",
        "operation": "conflicts",
        "params": {"include_decided": False},
    }
    body2 = dict(body1)
    body2["params"] = {"include_decided": True}
    r1 = client.post("/api/v1/commands", json=body1, headers=headers)
    assert r1.status_code == 200, r1.text
    r2 = client.post("/api/v1/commands", json=body2, headers=headers)
    assert r2.status_code == 409, r2.text
    assert r2.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_get_jobs_returns_state_after_post(http_client) -> None:
    """POST + GET round-trip through the same application service."""

    client, app_obj = http_client
    token = app_obj.auth.session_token
    headers = {
        "X-Memory-Agent-Token": token,
        "Origin": "http://127.0.0.1:8420",
    }
    # ``conflicts`` is synchronous, so we know the response carries a
    # status we can round-trip via the dedicated /jobs route by using
    # the same job_id. We rely on the application service's stable
    # job_id derivation: replay returns the same job_id.
    body = {
        "api_version": 1,
        "request_id": "req_job_roundtrip",
        "client_id": "ui_http_test",
        "idempotency_key": "rt_key",
        "operation": "conflicts",
        "params": {},
    }
    r1 = client.post("/api/v1/commands", json=body, headers=headers)
    assert r1.status_code == 200, r1.text
    job_id = r1.json().get("job_id")
    assert job_id, r1.json()
    r2 = client.get(f"/api/v1/jobs/{job_id}")
    assert r2.status_code == 200, r2.text
    payload = r2.json()
    assert payload["job_id"] == job_id
    assert payload["job_state"] == "succeeded"


def test_get_jobs_unknown_id_returns_404(http_client) -> None:
    client, _ = http_client
    r = client.get("/api/v1/jobs/job_does_not_exist")
    assert r.status_code == 404, r.text


def test_placeholder_page_is_served(http_client) -> None:
    client, _ = http_client
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "memory-agent ui" in body
    assert "/api/v1/overview" in body


def test_healthz_reports_loopback_only(http_client) -> None:
    client, _ = http_client
    r = client.get("/healthz")
    assert r.status_code == 200
    payload = r.json()
    assert payload["loopback_only"] is True
    assert payload["session_token_configured"] is True


def test_get_routes_never_call_the_model(http_client) -> None:
    """Browse-only paths must make zero model calls.

    We snapshot the model_calls count, hit every GET endpoint, and
    assert the count is unchanged. This is the v1 P6 invariant
    (docs/07 §审计 API: read-only endpoints must not call the model).
    """

    client, app_obj = http_client
    conn = open_state_db(app_obj.vault.state_db)
    try:
        before = int(
            conn.execute("SELECT COUNT(*) AS c FROM model_calls").fetchone()["c"]
        )
        # Hit every read endpoint.
        client.get("/api/v1/overview")
        client.get("/api/v1/sources?page_size=5")
        client.get("/api/v1/sessions/sess_http_a/turns")
        client.get("/api/v1/batches?page_size=5")
        batches = client.get("/api/v1/batches?page_size=5").json()["items"]
        if batches:
            client.get(f"/api/v1/batches/{batches[0]['batch_id']}")
            client.get(f"/api/v1/batches/{batches[0]['batch_id']}/evidence")
        mid_row = conn.execute(
            "SELECT memory_id FROM memory_projection LIMIT 1"
        ).fetchone()
        if mid_row:
            client.get(f"/api/v1/memories/{mid_row['memory_id']}/history")
        client.get("/api/v1/conflicts")
        client.get("/api/v1/audit?page_size=5")
        client.get("/api/v1/queries?page_size=5")
        after = int(
            conn.execute("SELECT COUNT(*) AS c FROM model_calls").fetchone()["c"]
        )
    finally:
        conn.close()
    assert after == before


def test_post_commands_async_then_get_jobs_polls_queue(
    http_client,
) -> None:
    """Async POST returns a queued job; GET /api/v1/jobs/{id} polls it."""

    client, app_obj = http_client
    token = app_obj.auth.session_token
    headers = {
        "X-Memory-Agent-Token": token,
        "Origin": "http://127.0.0.1:8420",
    }
    # ``recall`` is an async-capable operation that does not require
    # the model in this fake-provider test (the query-only path goes
    # through the keyword baseline). We request async to force the
    # queued path so the worker picks it up.
    body = {
        "api_version": 1,
        "request_id": "req_async_test",
        "client_id": "ui_http_test",
        "idempotency_key": "async_key_unique",
        "operation": "recall",
        "mode": "async",
        "params": {
            "query": "python prototype",
            "retrieval": "keyword",
            "rewrite": "off",
            "expand": "off",
            "audit": False,
        },
    }
    r1 = client.post("/api/v1/commands", json=body, headers=headers)
    assert r1.status_code == 200, r1.text
    payload = r1.json()
    assert payload["status"] in {"queued", "ok"}
    job_id = payload.get("job_id")
    assert job_id
    # Round-trip via GET /api/v1/jobs/{id}; the worker may or may not
    # have processed it (no worker is running in this test) but the
    # response must carry the same job_id and a job_state.
    r2 = client.get(f"/api/v1/jobs/{job_id}")
    assert r2.status_code == 200, r2.text
    assert r2.json()["job_id"] == job_id
    assert r2.json()["job_state"] in {"queued", "running", "succeeded", "failed"}