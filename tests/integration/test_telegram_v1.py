"""P4 / T12 — Telegram Bot API decision loop (fake-server acceptance).

Everything here is invented: a fake loopback Bot API server, a fake
token in the environment, invented conflict fixtures. No real bot, no
real token, no real chat, no network beyond 127.0.0.1.

Scenario coverage (docs/16 P4 acceptance):

  * conflict created → notification sent → valid callback → versioned
    decide → new HEAD/projection → ack
  * replayed / duplicated / out-of-order update → no second commit
  * invalid user / chat / token / stale revision → rejected, no commit
  * crash between decision and offset advance → exactly one commit
  * offline send / update → pending work preserved, retry succeeds
  * bot echo never becomes a decision
  * no token in SQLite / artifacts / logs
  * no Telegram configured → local CLI works, zero network calls
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from memory_agent.adapters.telegram.service import (
    CURSOR_KEY,
    run_telegram_pass,
)
from memory_agent.application.commit import CommitService
from memory_agent.application.dispatch import dispatch
from memory_agent.application.worker import run_worker_once
from memory_agent.contracts.candidates import EvidenceRef, Scope
from memory_agent.contracts.memory import MemoryAtom
from memory_agent.infrastructure.files import VaultLayout
from memory_agent.infrastructure.sqlite import open_state_db
from memory_agent.infrastructure.sqlite.repositories import (
    NotificationOutboxRepo,
    RuntimeStateRepo,
    TelegramCallbackTokenRepo,
    TelegramUpdateRepo,
)


FAKE_TOKEN = "123456789:FAKE-token-for-loopback-tests-only"
CHAT_ID = "-1002223334444"
USER_ID = "555000111"


# ---------------------------------------------------------------------------
# fake Bot API server
# ---------------------------------------------------------------------------


class FakeBotAPI:
    """In-process fake of the Telegram Bot API (getUpdates/sendMessage)."""

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.offline = False
        # Real Telegram honours the offset; tests can disable that to
        # model an at-least-once server re-delivering old updates.
        self.honor_offset = True
        self.get_updates_calls = 0
        self.send_calls = 0
        self.expected_token = FAKE_TOKEN
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def push_update(self, update: dict[str, Any]) -> None:
        with self._lock:
            self.updates.append(update)

    def _make_handler(self):
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):  # noqa: D401 - silence test server
                pass

            def _reply(self, code: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802 - stdlib naming
                raw_path = self.path
                try:
                    token, method = raw_path.strip("/").split("/", 1)
                except ValueError:
                    self._reply(404, {"ok": False, "description": "bad path"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                if token.replace("bot", "") != state.expected_token:
                    self._reply(401, {"ok": False, "description": "Unauthorized"})
                    return
                if state.offline:
                    self._reply(500, {"ok": False, "description": "offline"})
                    return
                if method == "getUpdates":
                    state.get_updates_calls += 1
                    offset = payload.get("offset")
                    with state._lock:
                        result = [
                            u for u in state.updates
                            if not state.honor_offset
                            or offset is None
                            or int(u["update_id"]) >= int(offset)
                        ]
                    self._reply(200, {"ok": True, "result": result})
                    return
                if method == "sendMessage":
                    state.send_calls += 1
                    with state._lock:
                        state.sent.append(payload)
                        message_id = 1000 + len(state.sent)
                    self._reply(200, {"ok": True, "result": {
                        "message_id": message_id,
                        "chat": {"id": payload.get("chat_id")},
                        "text": payload.get("text"),
                    }})
                    return
                self._reply(404, {"ok": False, "description": "unknown method"})

        return Handler


@pytest.fixture
def bot_api():
    api = FakeBotAPI()
    try:
        yield api
    finally:
        api.stop()


# ---------------------------------------------------------------------------
# vault fixtures (invented data only)
# ---------------------------------------------------------------------------


def _write_config(vault: Path, bot_api: FakeBotAPI) -> None:
    cfg_dir = vault / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "telegram.json").write_text(json.dumps({
        "enabled": True,
        "token_env": "MEMORY_AGENT_TELEGRAM_TEST_TOKEN",
        "base_url": bot_api.base_url,
        "allowed_chats": [CHAT_ID],
        "allowed_users": [USER_ID],
        "notify_chat_id": CHAT_ID,
        "notify_user_id": USER_ID,
        "callback_ttl_seconds": 3600,
        "backoff_seconds": 1,
    }), encoding="utf-8")


@pytest.fixture
def telegram_vault(tmp_path: Path, bot_api: FakeBotAPI,
                   monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MEMORY_AGENT_TELEGRAM_TEST_TOKEN", FAKE_TOKEN)
    vault_root = tmp_path / "vault"
    VaultLayout(vault_root)
    _write_config(vault_root, bot_api)
    return vault_root


def _commit_old_memory(vault: VaultLayout, conn) -> str:
    mem = MemoryAtom(
        schema_version=1, memory_id="mem_python_proto", revision=1,
        status="active", kind="requirement", abstraction="no_opinion",
        statement="use Python for the prototype",
        subject="proj_test_alpha", predicate=None, value=None,
        modality="must", scope=Scope(level="project",
                                     project_ids=["proj_test_alpha"]),
        primary_domain=None, domains=[], tags=[], horizon="unknown",
        valid_from=None, valid_until=None,
        asserted_at="2026-10-08T10:00:00+00:00",
        recorded_at="2026-10-08T10:00:00+00:00", evidence=[],
    )
    svc = CommitService(vault=vault, conn=conn)
    intent = svc.prepare_commit(
        proposed_memories=[mem], pending_proposals=[], base_commit_id="root",
        privacy_epoch="epoch-0", batch_ids=["b_seed"],
        target_unit_ids=["u_seed"],
    )
    svc.publish(intent["commit_id"])
    svc.finalize(intent["commit_id"])
    return intent["commit_id"]


def _seed_conflict(vault: VaultLayout, conn) -> str:
    """Commit a pending proposal that conflicts with the seed memory."""
    _commit_old_memory(vault, conn)
    candidate = {
        "candidate_id": "cand_ts_proto",
        "kind": "requirement",
        "abstraction": "no_opinion",
        "statement": "use TypeScript for the prototype",
        "modality": "must",
        "scope": {"level": "project", "project_ids": ["proj_test_alpha"]},
        "primary_domain": None,
        "tags": [],
        "evidence": [],
    }
    proposal = {
        "candidate_id": "cand_ts_proto",
        "candidate": candidate,
        "intersects": ["mem_python_proto"],
        "reason": "cross_batch_overlap",
    }
    head = json.loads(vault.head_path.read_text(encoding="utf-8"))["commit_id"]
    svc = CommitService(vault=vault, conn=conn)
    intent = svc.prepare_commit(
        proposed_memories=[], pending_proposals=[proposal],
        base_commit_id=head, privacy_epoch="epoch-0",
        batch_ids=["b_conflict"], target_unit_ids=["u_conflict"],
    )
    svc.publish(intent["commit_id"])
    svc.finalize(intent["commit_id"])
    return "cand_ts_proto"


def _callback_update(update_id: int, data: str, *, user_id: str = USER_ID,
                     chat_id: str = CHAT_ID, is_bot: bool = False
                     ) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cbq_{update_id}",
            "from": {"id": int(user_id), "is_bot": is_bot},
            "chat_instance": "ci_x",
            "data": data,
            "message": {
                "message_id": 7,
                "chat": {"id": int(chat_id), "type": "supergroup"},
                "text": "conflict notice",
            },
        },
    }


def _first_sent_notice(bot_api: FakeBotAPI) -> dict[str, Any]:
    for msg in bot_api.sent:
        if (msg.get("reply_markup") or {}).get("inline_keyboard"):
            return msg
    raise AssertionError(f"no conflict notice was sent: {bot_api.sent}")


def _button_data(notice: dict[str, Any], action: str) -> str:
    for row in notice["reply_markup"]["inline_keyboard"]:
        for btn in row:
            if btn["callback_data"].endswith(f":{action}"):
                return btn["callback_data"]
    raise AssertionError(f"no button for {action}: {notice}")


def _run_pass(vault_root: Path) -> dict[str, Any]:
    vault = VaultLayout(vault_root)
    conn = open_state_db(vault.state_db)
    try:
        return run_telegram_pass(vault=vault, conn=conn)
    finally:
        conn.close()


def _head_commit_id(vault_root: Path) -> str | None:
    path = vault_root / "HEAD.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))["commit_id"]


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


class TestTelegramDecisionLoop:
    def test_full_loop_notification_decide_ack(
        self, telegram_vault: Path, bot_api: FakeBotAPI,
    ) -> None:
        """conflict → notification → valid callback → decide → ack."""
        vault = VaultLayout(telegram_vault)
        conn = open_state_db(vault.state_db)
        try:
            conflict_id = _seed_conflict(vault, conn)
            head_before = _head_commit_id(telegram_vault)
        finally:
            conn.close()

        # --- pass 1: worker enqueues + sends the notification -----------
        worker_result = run_worker_once(vault_root=telegram_vault)
        assert worker_result.telegram["enabled"] is True
        assert worker_result.telegram["enqueued"] == 1
        assert worker_result.telegram["sent"] == 1
        notice = _first_sent_notice(bot_api)
        assert conflict_id in notice["text"]

        data = _button_data(notice, "adopt_new")
        bot_api.push_update(_callback_update(1001, data))

        # --- pass 2: receive the callback, decide, queue the ack -------
        pass2 = _run_pass(telegram_vault)
        assert pass2["decided"] == 1, pass2
        assert pass2["rejected"] == 0, pass2

        head_after = _head_commit_id(telegram_vault)
        assert head_after and head_after != head_before

        # The new memory object is on disk at the new HEAD…
        manifest = json.loads(
            (telegram_vault / "commits" / str(head_after) / "manifest.json")
            .read_text(encoding="utf-8"))
        assert any(m["memory_id"] == "mem_ts_proto"
                   for m in manifest["memories"]), manifest

        # …and the projection converges on the next dispatch (the same
        # recovery the CLI relies on after any publish-without-finalize).
        assert dispatch(operation="conflicts", params={},
                        vault_root=telegram_vault)["status"] == "ok"
        conn = open_state_db(vault.state_db)
        try:
            rows = conn.execute(
                "SELECT * FROM memory_projection WHERE status='active'"
            ).fetchall()
            assert any("ts_proto" in r["memory_id"] for r in rows), \
                [dict(r) for r in rows]
            conflict = conn.execute(
                "SELECT status FROM conflict_projection WHERE conflict_id = ?",
                (conflict_id,)).fetchone()
            assert conflict["status"] == "decided"
            receipt = TelegramUpdateRepo(conn).get(1001)
            assert receipt is not None and receipt["status"] == "decided"
            assert receipt["action"] == "adopt_new"
            cursor = RuntimeStateRepo(conn).get(CURSOR_KEY)
            assert cursor == 1002, cursor
        finally:
            conn.close()

        # --- pass 3: the ack is delivered -------------------------------
        pass3 = _run_pass(telegram_vault)
        assert pass3["sent"] == 1
        acks = [m for m in bot_api.sent if "已记录你的决定" in m["text"]]
        assert len(acks) == 1
        assert conflict_id in acks[0]["text"]

    def test_replayed_and_out_of_order_updates_never_commit_twice(
        self, telegram_vault: Path, bot_api: FakeBotAPI,
    ) -> None:
        vault = VaultLayout(telegram_vault)
        conn = open_state_db(vault.state_db)
        try:
            _seed_conflict(vault, conn)
        finally:
            conn.close()
        _run_pass(telegram_vault)
        data = _button_data(_first_sent_notice(bot_api), "adopt_new")

        bot_api.push_update(_callback_update(2001, data))
        assert _run_pass(telegram_vault)["decided"] == 1

        conn = open_state_db(vault.state_db)
        try:
            commits_before = conn.execute(
                "SELECT COUNT(*) c FROM commit_intents").fetchone()["c"]
            head_before = _head_commit_id(telegram_vault)
        finally:
            conn.close()

        # exact replay of the same update (at-least-once delivery)
        bot_api.honor_offset = False
        bot_api.push_update(_callback_update(2001, data))
        replay = _run_pass(telegram_vault)
        assert replay["duplicates"] >= 1, replay
        assert replay["decided"] == 0

        # out-of-order: an *older* update id that was never seen
        bot_api.push_update(_callback_update(1500, data))
        stale = _run_pass(telegram_vault)
        assert stale["ignored"] == 1, stale

        conn = open_state_db(vault.state_db)
        try:
            commits_after = conn.execute(
                "SELECT COUNT(*) c FROM commit_intents").fetchone()["c"]
            assert commits_after == commits_before
            assert _head_commit_id(telegram_vault) == head_before
            assert TelegramUpdateRepo(conn).get(2001)["status"] == "decided"
            assert TelegramUpdateRepo(conn).get(1500)["status"] == "ignored"
        finally:
            conn.close()

    def test_invalid_callbacks_are_rejected_without_commit(
        self, telegram_vault: Path, bot_api: FakeBotAPI,
    ) -> None:
        vault = VaultLayout(telegram_vault)
        conn = open_state_db(vault.state_db)
        try:
            conflict_id = _seed_conflict(vault, conn)
        finally:
            conn.close()
        _run_pass(telegram_vault)
        data = _button_data(_first_sent_notice(bot_api), "adopt_new")

        cases = [
            ("not_allowed_user", _callback_update(
                3001, data, user_id="999888777")),
            ("not_allowed_chat", _callback_update(
                3002, data, chat_id="-100999888777")),
            ("unknown_token", _callback_update(3003, "ddeadbeefdeadbeef:adopt_new")),
            ("unknown_callback", _callback_update(3004, "garbage")),
        ]
        # expired token
        with open_state_db(vault.state_db) as c2:
            token_id = data[1:].split(":", 1)[0]
            c2.execute(
                "UPDATE telegram_callback_tokens SET expires_at = '2000-01-01T00:00:00+00:00' "
                "WHERE token_id = ?", (token_id,))
        cases.append(("expired", _callback_update(3005, data)))
        # action mismatch: token issued for adopt_new, replayed as keep_old
        cases.append(("action_mismatch", _callback_update(
            3006, f"{data.rsplit(':', 1)[0]}:keep_old")))
        # bot echo
        cases.append(("bot_echo", _callback_update(
            3007, data, is_bot=True)))
        # stale revision: bump the conflict revision after the token exists
        with open_state_db(vault.state_db) as c3:
            c3.execute(
                "UPDATE conflict_projection SET revision = 2 WHERE conflict_id = ?",
                (conflict_id,))
        cases.append(("stale", _callback_update(3008, data)))

        for name, update in cases:
            bot_api.push_update(update)
        result = _run_pass(telegram_vault)
        assert result["decided"] == 0, result
        assert result["rejected"] + result["ignored"] == len(cases), result

        conn = open_state_db(vault.state_db)
        try:
            for _, update in cases:
                row = TelegramUpdateRepo(conn).get(update["update_id"])
                assert row is not None, update["update_id"]
                assert row["status"] in ("rejected", "ignored"), row
            open_conflicts = conn.execute(
                "SELECT COUNT(*) c FROM conflict_projection WHERE status='pending'"
            ).fetchone()["c"]
            assert open_conflicts == 1
        finally:
            conn.close()

    def test_crash_between_decision_and_offset_advances_once(
        self, telegram_vault: Path, bot_api: FakeBotAPI,
    ) -> None:
        """Commit first, then replay the update as if we crashed before
        persisting the receipt / advancing the offset."""
        vault = VaultLayout(telegram_vault)
        conn = open_state_db(vault.state_db)
        try:
            conflict_id = _seed_conflict(vault, conn)
        finally:
            conn.close()
        _run_pass(telegram_vault)
        data = _button_data(_first_sent_notice(bot_api), "adopt_new")

        # Simulate the crash: the application service commits the
        # decision, but no telegram_updates row / cursor write happens.
        resp = dispatch(
            operation="decide",
            params={"conflict_id": conflict_id, "proposal_revision": 1,
                    "action": "adopt_new",
                    "idempotency_key": "telegram:4001"},
            request_id="req_telegram_4001", client_id="user_telegram",
            vault_root=telegram_vault,
        )
        assert resp["status"] == "ok", resp

        conn = open_state_db(vault.state_db)
        try:
            commits_before = conn.execute(
                "SELECT COUNT(*) c FROM commit_intents").fetchone()["c"]
            assert TelegramUpdateRepo(conn).get(4001) is None
            assert RuntimeStateRepo(conn).get(CURSOR_KEY) is None
        finally:
            conn.close()

        # The replayed update must be recognised, not re-committed.
        bot_api.push_update(_callback_update(4001, data))
        result = _run_pass(telegram_vault)
        assert result["decided"] == 0, result
        assert result["already_applied"] == 1, result

        conn = open_state_db(vault.state_db)
        try:
            commits_after = conn.execute(
                "SELECT COUNT(*) c FROM commit_intents").fetchone()["c"]
            assert commits_after == commits_before
            receipt = TelegramUpdateRepo(conn).get(4001)
            assert receipt["status"] == "already_applied"
            assert RuntimeStateRepo(conn).get(CURSOR_KEY) == 4002
        finally:
            conn.close()

    def test_offline_send_and_poll_preserve_pending_work(
        self, telegram_vault: Path, bot_api: FakeBotAPI,
    ) -> None:
        vault = VaultLayout(telegram_vault)
        conn = open_state_db(vault.state_db)
        try:
            _seed_conflict(vault, conn)
        finally:
            conn.close()

        bot_api.offline = True
        offline = _run_pass(telegram_vault)
        assert offline["enabled"] is True
        assert offline["sent"] == 0
        assert offline["poll_error"], offline
        assert bot_api.sent == []

        conn = open_state_db(vault.state_db)
        try:
            pending = [dict(r) for r in conn.execute(
                "SELECT * FROM notification_outbox WHERE channel='telegram' "
                "AND status='pending'")]
            assert len(pending) == 1, pending
            assert int(pending[0]["attempt"]) >= 1
            assert pending[0]["next_retry"]  # backoff scheduled
        finally:
            conn.close()

        # Local recall must still work while Telegram is down.
        recall = dispatch(operation="recall",
                          params={"query": "prototype", "limit": 3},
                          vault_root=telegram_vault)
        assert recall["status"] == "ok", recall

        # Back online: the outbox retries, the cursor is untouched.
        bot_api.offline = False
        # Backoff has elapsed (backoff_seconds=1): the row is due again.
        import time as _time
        _time.sleep(1.1)
        online = _run_pass(telegram_vault)
        assert online["sent"] == 1, online
        conn = open_state_db(vault.state_db)
        try:
            row = NotificationOutboxRepo(conn).get(pending[0]["notification_id"])
            assert row["status"] == "sent"
            assert row["remote_id"]
        finally:
            conn.close()

    def test_no_telegram_configured_makes_no_network_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("MEMORY_AGENT_TELEGRAM_TEST_TOKEN", raising=False)
        vault_root = tmp_path / "vault"
        vault = VaultLayout(vault_root)
        conn = open_state_db(vault.state_db)
        try:
            _seed_conflict(vault, conn)
            result = run_telegram_pass(vault=vault, conn=conn)
        finally:
            conn.close()
        assert result["enabled"] is False
        assert result["reason"] == "not_configured"
        assert result["sent"] == 0

        # ... and the local CLI decision path still works.
        resp = dispatch(
            operation="decide",
            params={"conflict_id": "cand_ts_proto", "proposal_revision": 1,
                    "action": "keep_old", "idempotency_key": "cli-1"},
            vault_root=vault_root,
        )
        assert resp["status"] == "ok", resp

    def test_bot_echo_message_is_ignored(
        self, telegram_vault: Path, bot_api: FakeBotAPI,
    ) -> None:
        vault = VaultLayout(telegram_vault)
        conn = open_state_db(vault.state_db)
        try:
            _seed_conflict(vault, conn)
        finally:
            conn.close()
        _run_pass(telegram_vault)

        bot_api.push_update({
            "update_id": 5001,
            "message": {
                "message_id": 8,
                "chat": {"id": int(CHAT_ID), "type": "supergroup"},
                "from": {"id": 777, "is_bot": True},
                "text": "use TypeScript for the prototype",
            },
        })
        result = _run_pass(telegram_vault)
        assert result["ignored"] == 1 and result["decided"] == 0

        conn = open_state_db(vault.state_db)
        try:
            row = TelegramUpdateRepo(conn).get(5001)
            assert row["status"] == "ignored"
            assert row["reason_code"] == "bot_echo"
            # No source event / memory was created from the echo.
            assert conn.execute(
                "SELECT COUNT(*) c FROM source_events").fetchone()["c"] == 0
        finally:
            conn.close()

    def test_token_never_appears_in_db_or_artifacts(
        self, telegram_vault: Path, bot_api: FakeBotAPI,
    ) -> None:
        vault = VaultLayout(telegram_vault)
        conn = open_state_db(vault.state_db)
        try:
            _seed_conflict(vault, conn)
        finally:
            conn.close()
        _run_pass(telegram_vault)
        data = _button_data(_first_sent_notice(bot_api), "adopt_new")
        bot_api.push_update(_callback_update(6001, data))
        _run_pass(telegram_vault)

        for path in telegram_vault.rglob("*"):
            if path.is_file():
                blob = path.read_bytes()
                assert FAKE_TOKEN.encode() not in blob, path
        conn = open_state_db(vault.state_db)
        try:
            rows = conn.execute(
                "SELECT audit_id, details_json, correlation_json FROM audit_events"
            ).fetchall()
            assert rows
            for row in rows:
                assert FAKE_TOKEN not in json.dumps(dict(row))
        finally:
            conn.close()

    def test_callback_token_is_bound_to_revision_and_action(
        self, telegram_vault: Path, bot_api: FakeBotAPI,
    ) -> None:
        vault = VaultLayout(telegram_vault)
        conn = open_state_db(vault.state_db)
        try:
            _seed_conflict(vault, conn)
        finally:
            conn.close()
        _run_pass(telegram_vault)
        data = _button_data(_first_sent_notice(bot_api), "keep_old")
        token_id = data[1:].split(":", 1)[0]

        conn = open_state_db(vault.state_db)
        try:
            token = TelegramCallbackTokenRepo(conn).get(token_id)
            assert token["conflict_id"] == "cand_ts_proto"
            assert int(token["proposal_revision"]) == 1
            assert token["action"] == "keep_old"
            assert token["chat_id"] == CHAT_ID
            # The token carries no memory text.
            assert "TypeScript" not in json.dumps(dict(token))
        finally:
            conn.close()
