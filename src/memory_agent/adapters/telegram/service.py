"""Telegram decision-loop service (P4 / T12).

One pass = (1) enqueue missing notifications for open conflicts,
(2) attempt to send due outbox rows with retry/backoff, (3) poll the
Bot API once and process the returned updates.

Ordering / durability rules (docs/05, docs/06, docs/12):

  * The update offset (``runtime_state``) advances **only in the same
    transaction that records the ``telegram_updates`` receipt** (and
    consumes the callback token). The decision itself is committed by
    the shared application service *before* that transaction runs.
  * A replayed update (same ``update_id``) is a no-op. An update that
    replays after a crash between the commit and the receipt is
    recognised by the ``conflict.decided`` audit row and recorded as
    ``already_applied`` — never a second commit.
  * Out-of-order / already-past-offset updates are recorded as
    ``ignored_out_of_order`` without touching the offset backwards.
  * Invalid identity, unknown/expired/mismatched token, stale proposal
    revision, or an already-decided conflict → rejected, no commit.
  * Bot-originated updates are ``ignored`` — an echo never becomes a
    decision or a source event.

Every network call is wrapped: a Telegram outage leaves pending work
in the outbox / offset untouched and the worker pass continues (local
scan / recall are never blocked).
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from memory_agent.adapters.telegram.client import (
    TelegramBotClient,
    TelegramTransportError,
    parse_callback,
    parse_chat_id,
    parse_user_id,
)
from memory_agent.adapters.telegram.config import (
    TelegramConfig,
    load_telegram_config,
    resolve_token,
)
from memory_agent.adapters.telegram.formatter import (
    format_conflict_message,
    format_decision_ack,
)
from memory_agent.infrastructure.files import VaultLayout
from memory_agent.infrastructure.sqlite import transaction
from memory_agent.infrastructure.sqlite.repositories import (
    AuditRepo,
    ConflictRepo,
    NotificationOutboxRepo,
    RuntimeStateRepo,
    TelegramCallbackTokenRepo,
    TelegramUpdateRepo,
)


CURSOR_KEY = "telegram.update_offset"
CHANNEL = "telegram"

ACTIONS = ("adopt_new", "keep_old", "scope_split", "defer")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _now_iso() -> str:
    return _iso(_now())


def _stable_id(prefix: str, *parts: Any) -> str:
    h = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8"))
    return f"{prefix}_{h.hexdigest()[:16]}"


def _telegram_dir(vault: VaultLayout) -> Path:
    path = vault.artifacts_dir / "telegram"
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_telegram_pass(
    *,
    vault: VaultLayout,
    conn,
    config: TelegramConfig | None = None,
    client: TelegramBotClient | None = None,
) -> dict[str, Any]:
    """Run one Telegram pass. Never raises; returns a diagnostics dict."""
    cfg = config if config is not None else load_telegram_config(vault.root)
    result: dict[str, Any] = {
        "enabled": False,
        "reason": None,
        "enqueued": 0,
        "sent": 0,
        "send_errors": 0,
        "updates_seen": 0,
        "decided": 0,
        "rejected": 0,
        "ignored": 0,
        "duplicates": 0,
        "already_applied": 0,
        "poll_error": None,
        "cursor": None,
    }
    if not cfg.enabled:
        result["reason"] = "not_configured"
        return result
    token = resolve_token(cfg)
    if not token:
        # Configured but no token in the environment: stay offline
        # rather than making a doomed network call.
        result["reason"] = "token_missing"
        return result
    result["enabled"] = True
    if client is None:
        client = TelegramBotClient(token=token, base_url=cfg.base_url)

    # 1. Reconcile missing notifications (crash between HEAD publish
    #    and outbox enqueue must not permanently lose a notification).
    try:
        result["enqueued"] = enqueue_conflict_notifications(
            vault=vault, conn=conn, cfg=cfg)
    except Exception as exc:  # noqa: BLE001
        result["enqueue_error"] = f"{type(exc).__name__}: {exc}"

    # 2. Drain due outbox rows (retry with backoff; offline-safe).
    try:
        sent, errors = send_due_notifications(
            vault=vault, conn=conn, cfg=cfg, client=client)
        result["sent"] = sent
        result["send_errors"] = errors
    except TelegramTransportError as exc:
        result["send_errors"] = 1
        result["send_error"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        result["send_errors"] = 1
        result["send_error"] = f"{type(exc).__name__}: {exc}"

    # 3. Poll once and process updates.
    try:
        poll = poll_and_process(vault=vault, conn=conn, cfg=cfg, client=client)
        result.update(poll)
    except TelegramTransportError as exc:
        result["poll_error"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        result["poll_error"] = f"{type(exc).__name__}: {exc}"
    return result


# ---------------------------------------------------------------------------
# outbound: conflict notifications
# ---------------------------------------------------------------------------


def enqueue_conflict_notifications(
    *, vault: VaultLayout, conn, cfg: TelegramConfig,
) -> int:
    """Create one outbox row + callback tokens per open conflict."""
    if not cfg.notify_chat_id:
        return 0
    outbox = NotificationOutboxRepo(conn)
    tokens = TelegramCallbackTokenRepo(conn)
    conflicts = ConflictRepo(conn).list_open()
    created = 0
    for conflict in conflicts:
        cid = str(conflict["conflict_id"])
        rev = int(conflict["revision"])
        notification_id = _stable_id("notif", cid, rev, CHANNEL, "conflict")
        if outbox.get(notification_id) is not None:
            continue
        tokens_by_action: dict[str, str] = {}
        expires_at = _iso(_now() + timedelta(seconds=cfg.callback_ttl_seconds))
        for action in ACTIONS:
            token_id = secrets.token_hex(8)
            tokens.create(
                token_id=token_id, conflict_id=cid, proposal_revision=rev,
                action=action, chat_id=cfg.notify_chat_id,
                user_id=cfg.notify_user_id or "*", expires_at=expires_at,
            )
            tokens_by_action[action] = token_id
        old_statement = _old_statement(conn, conflict.get("old_memory_ref"))
        new_statement = _new_statement(vault, cid)
        text, markup = format_conflict_message(
            conflict=conflict,
            old_statement=old_statement,
            new_statement=new_statement,
            tokens_by_action=tokens_by_action,
        )
        payload = {
            "notification_id": notification_id,
            "kind": "conflict_notice",
            "conflict_id": cid,
            "proposal_revision": rev,
            "chat_id": cfg.notify_chat_id,
            "text": text,
            "reply_markup": markup,
        }
        _write_payload(vault, notification_id, payload)
        if outbox.enqueue(
            notification_id=notification_id,
            conflict_revision=f"{cid}:{rev}",
            channel=CHANNEL,
            payload_ref=_payload_ref(vault, notification_id),
        ):
            created += 1
    return created


def enqueue_decision_ack(
    *,
    vault: VaultLayout, conn, cfg: TelegramConfig,
    conflict_id: str, proposal_revision: int, update_id: int,
    action: str, status: str, note: str | None = None,
) -> str | None:
    """Queue the human-readable receipt for an applied decision."""
    if not cfg.notify_chat_id:
        return None
    notification_id = _stable_id(
        "notif", conflict_id, proposal_revision, CHANNEL, "ack", update_id)
    outbox = NotificationOutboxRepo(conn)
    if outbox.get(notification_id) is not None:
        return notification_id
    text = format_decision_ack(
        conflict_id=conflict_id, action=action, status=status, note=note)
    payload = {
        "notification_id": notification_id,
        "kind": "decision_ack",
        "conflict_id": conflict_id,
        "proposal_revision": proposal_revision,
        "chat_id": cfg.notify_chat_id,
        "text": text,
        "reply_markup": None,
    }
    _write_payload(vault, notification_id, payload)
    outbox.enqueue(
        notification_id=notification_id,
        conflict_revision=f"{conflict_id}:{proposal_revision}:ack:{update_id}",
        channel=CHANNEL,
        payload_ref=_payload_ref(vault, notification_id),
    )
    return notification_id


def send_due_notifications(
    *, vault: VaultLayout, conn, cfg: TelegramConfig,
    client: TelegramBotClient,
) -> tuple[int, int]:
    """Send every due pending outbox row. Returns (sent, errors)."""
    outbox = NotificationOutboxRepo(conn)
    sent = 0
    errors = 0
    for row in outbox.due(channel=CHANNEL):
        ref = row["payload_ref"]
        payload = _read_payload(vault, ref)
        if payload is None:
            errors += 1
            attempt = int(row["attempt"]) + 1
            outbox.mark_retry(
                str(row["notification_id"]), attempt=attempt,
                next_retry=_iso(_now() + timedelta(
                    seconds=cfg.backoff_seconds * min(attempt, 10))),
            )
            continue
        try:
            sent_msg = client.send_message(
                chat_id=str(payload["chat_id"]),
                text=str(payload["text"]),
                reply_markup=payload.get("reply_markup"),
            )
        except TelegramTransportError:
            errors += 1
            attempt = int(row["attempt"]) + 1
            outbox.mark_retry(
                str(row["notification_id"]), attempt=attempt,
                next_retry=_iso(_now() + timedelta(
                    seconds=cfg.backoff_seconds * min(attempt, 10))),
            )
            continue
        with transaction(conn):
            outbox.mark_sent(
                str(row["notification_id"]),
                str(sent_msg.get("message_id") or ""),
            )
        sent += 1
    return sent, errors


# ---------------------------------------------------------------------------
# inbound: update polling + decision processing
# ---------------------------------------------------------------------------


def poll_and_process(
    *, vault: VaultLayout, conn, cfg: TelegramConfig,
    client: TelegramBotClient,
) -> dict[str, Any]:
    """Fetch updates once and process each exactly once."""
    runtime = RuntimeStateRepo(conn)
    offset = runtime.get(CURSOR_KEY)
    updates = client.get_updates(
        offset=offset,
        timeout=cfg.poll_timeout_seconds,
        limit=cfg.max_updates_per_pass,
    )
    stats: dict[str, Any] = {
        "updates_seen": 0,
        "decided": 0,
        "rejected": 0,
        "ignored": 0,
        "duplicates": 0,
        "already_applied": 0,
    }
    for update in sorted(updates, key=lambda u: int(u.get("update_id", 0))):
        stats["updates_seen"] += 1
        process_update(vault=vault, conn=conn, cfg=cfg, update=update,
                       stats=stats)
    stats["cursor"] = runtime.get(CURSOR_KEY)
    return stats


def process_update(
    *, vault: VaultLayout, conn, cfg: TelegramConfig,
    update: dict[str, Any], stats: dict[str, Any],
) -> None:
    update_id = int(update.get("update_id", 0))
    updates_repo = TelegramUpdateRepo(conn)
    runtime = RuntimeStateRepo(conn)
    offset = runtime.get(CURSOR_KEY)

    if updates_repo.exists(update_id):
        # Dedupe: receipt already durable. Never commit again.
        stats["duplicates"] += 1
        with transaction(conn):
            runtime.put(CURSOR_KEY, max(int(offset or 0), update_id + 1))
        return
    if offset is not None and update_id < int(offset):
        # Out-of-order (already past the durable offset).
        stats["ignored"] += 1
        _record(conn, updates_repo, update_id=update_id, status="ignored",
                reason_code="out_of_order")
        return

    cb = update.get("callback_query")
    if cb is not None:
        _process_callback(vault=vault, conn=conn, cfg=cfg, update=update,
                          cb=cb, stats=stats)
        return

    # Non-callback update (plain message, edited message, bot echo...).
    user = update.get("message") or update.get("edited_message") or {}
    reason = "bot_echo" if (user.get("from") or {}).get("is_bot") \
        else "not_a_callback"
    stats["ignored"] += 1
    _record(conn, updates_repo, update_id=update_id, status="ignored",
            reason_code=reason,
            chat_id=parse_chat_id(user.get("chat")),
            user_id=parse_user_id(user.get("from")))


def _process_callback(
    *, vault: VaultLayout, conn, cfg: TelegramConfig,
    update: dict[str, Any], cb: dict[str, Any], stats: dict[str, Any],
) -> None:
    update_id = int(update.get("update_id", 0))
    updates_repo = TelegramUpdateRepo(conn)
    tokens_repo = TelegramCallbackTokenRepo(conn)
    runtime = RuntimeStateRepo(conn)
    audit = AuditRepo(conn)
    sender = cb.get("from") or {}
    chat_id = parse_chat_id((cb.get("message") or {}).get("chat"))
    user_id = parse_user_id(sender)

    def reject(reason: str, conflict_id: str | None = None,
               action: str | None = None) -> None:
        stats["rejected"] += 1
        _record(conn, updates_repo, update_id=update_id, status="rejected",
                reason_code=reason, chat_id=chat_id, user_id=user_id,
                conflict_id=conflict_id, action=action)
        _audit_decision_event(
            audit, update_id=update_id, outcome="error", reason=reason,
            conflict_id=conflict_id, action=action, chat_id=chat_id,
            user_id=user_id)

    # 1. Bot echo is never a source of decisions.
    if sender.get("is_bot"):
        stats["ignored"] += 1
        _record(conn, updates_repo, update_id=update_id, status="ignored",
                reason_code="bot_echo", chat_id=chat_id, user_id=user_id)
        return

    # 2. Identity: allowed chat + allowed user.
    if chat_id is None or user_id is None:
        reject("missing_identity")
        return
    if cfg.allowed_chats and chat_id not in cfg.allowed_chats:
        reject("chat_not_allowed")
        return
    if cfg.allowed_users and user_id not in cfg.allowed_users:
        reject("user_not_allowed")
        return

    # 3. Opaque callback token.
    parsed = parse_callback(str(cb.get("data") or ""))
    if parsed is None:
        reject("unknown_callback")
        return
    token_id, action = parsed
    token = tokens_repo.get(token_id)
    if token is None:
        reject("unknown_token")
        return
    if str(token["expires_at"]) < _now_iso():
        reject("expired_token", str(token["conflict_id"]), action)
        return
    if str(token["chat_id"]) != chat_id:
        reject("chat_mismatch", str(token["conflict_id"]), action)
        return
    if str(token["user_id"]) not in ("*", user_id):
        reject("user_mismatch", str(token["conflict_id"]), action)
        return
    if str(token["action"]) != action:
        reject("action_mismatch", str(token["conflict_id"]), action)
        return
    if token["consumed_at"] is not None:
        reject("token_consumed", str(token["conflict_id"]), action)
        return

    conflict_id = str(token["conflict_id"])
    proposal_revision = int(token["proposal_revision"])

    # 4. Proposal must still be open at exactly this revision.
    open_rows = [r for r in ConflictRepo(conn).list_open()
                 if r["conflict_id"] == conflict_id]
    if not open_rows:
        # Either someone already decided (possibly this same update,
        # replayed after a crash between commit and receipt) or the
        # proposal was closed by the CLI/UI.
        if _has_decision_audit(conn, conflict_id):
            stats["already_applied"] += 1
            _record(conn, updates_repo, update_id=update_id,
                    status="already_applied", chat_id=chat_id,
                    user_id=user_id, conflict_id=conflict_id, action=action)
            tokens_repo.mark_consumed(token_id)
            _advance_cursor(conn, runtime, update_id)
        else:
            reject("conflict_not_open", conflict_id, action)
        return
    current = sorted(open_rows, key=lambda r: int(r["revision"]))[-1]
    if int(current["revision"]) != proposal_revision:
        reject("stale_revision", conflict_id, action)
        return

    # 5. Apply through the SAME application service the CLI uses.
    from memory_agent.application.dispatch import dispatch
    response = dispatch(
        operation="decide",
        params={
            "conflict_id": conflict_id,
            "proposal_revision": proposal_revision,
            "action": action,
            "idempotency_key": f"telegram:{update_id}",
        },
        request_id=f"req_telegram_{update_id}",
        client_id="user_telegram",
        vault_root=vault.root,
    )
    ok = response.get("status") == "ok"
    result = response.get("result") or {}
    new_state = str(result.get("status") or "unknown")
    if not ok:
        reject(str((response.get("error") or {}).get("code") or "decide_failed"),
               conflict_id, action)
        return

    # 6. Receipt + token consumption + cursor advance, one transaction.
    audit_id = _decision_audit_id(conflict_id, action, new_state)
    with transaction(conn):
        updates_repo.record(
            update_id=update_id, status="decided",
            received_at=_now_iso(), chat_id=chat_id, user_id=user_id,
            conflict_id=conflict_id, action=action,
            decision_event_ref=audit_id, processed_at=_now_iso(),
        )
        tokens_repo.mark_consumed(token_id)
        runtime.put(CURSOR_KEY, update_id + 1)
    stats["decided"] += 1

    # 7. Queue the human-readable ack (at-least-once outbox).
    enqueue_decision_ack(
        vault=vault, conn=conn, cfg=cfg, conflict_id=conflict_id,
        proposal_revision=proposal_revision, update_id=update_id,
        action=action, status=new_state,
    )
    _audit_decision_event(
        audit, update_id=update_id, outcome="success",
        reason=None, conflict_id=conflict_id, action=action,
        chat_id=chat_id, user_id=user_id, new_state=new_state)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _advance_cursor(conn, runtime: RuntimeStateRepo, update_id: int) -> None:
    offset = runtime.get(CURSOR_KEY)
    with transaction(conn):
        runtime.put(CURSOR_KEY, max(int(offset or 0), update_id + 1))


def _record(conn, updates_repo: TelegramUpdateRepo, **kwargs: Any) -> None:
    kwargs.setdefault("received_at", _now_iso())
    with transaction(conn):
        updates_repo.record(**kwargs)


def _has_decision_audit(conn, conflict_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM audit_events WHERE action = 'conflict.decided' "
        "AND object_id = ? LIMIT 1", (conflict_id,)
    ).fetchone()
    return row is not None


def _decision_audit_id(conflict_id: str, action: str, state: str) -> str:
    return _stable_id("aud", conflict_id, action, state)


def _audit_decision_event(
    audit: AuditRepo, *, update_id: int, outcome: str, reason: str | None,
    conflict_id: str | None, action: str | None,
    chat_id: str | None, user_id: str | None, new_state: str | None = None,
) -> None:
    """Audit the Telegram decision channel (never stores message text)."""
    audit_id = f"aud_tg_{update_id}_{_stable_id('x', update_id, outcome, reason or '', conflict_id or '')[2:]}"
    if audit_id in _existing_audit_ids(audit, audit_id):
        return
    audit.append({
        "audit_id": audit_id,
        "actor": "user_telegram",
        "action": "telegram.callback_processed",
        "occurred_at": _now_iso(),
        "object_type": "conflict",
        "object_id": conflict_id,
        "correlation": {
            "update_id": update_id, "chat_id": chat_id, "user_id": user_id},
        "before_after": {"action": action, "new_state": new_state},
        "outcome": outcome,
        "reason_code": reason,
        "details": {"channel": CHANNEL},
    })


def _existing_audit_ids(audit: AuditRepo, audit_id: str) -> set[str]:
    # AuditRepo has no get-by-id; query via a tiny select on the same conn.
    row = audit.conn.execute(
        "SELECT 1 FROM audit_events WHERE audit_id = ? LIMIT 1",
        (audit_id,)).fetchone()
    return {audit_id} if row is not None else set()


def _payload_ref(vault: VaultLayout, notification_id: str) -> str:
    return str(_telegram_dir(vault) / f"{notification_id}.json")


def _write_payload(vault: VaultLayout, notification_id: str,
                   payload: dict[str, Any]) -> None:
    path = _telegram_dir(vault) / f"{notification_id}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_payload(vault: VaultLayout, ref: str) -> dict[str, Any] | None:
    path = Path(ref)
    if not path.is_absolute():
        path = vault.root / path
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _old_statement(conn, memory_id: Any) -> str | None:
    if not memory_id:
        return None
    row = conn.execute(
        "SELECT statement FROM memory_projection WHERE memory_id = ? "
        "ORDER BY revision DESC LIMIT 1", (str(memory_id),)
    ).fetchone()
    return str(row["statement"]) if row is not None else None


def _new_statement(vault: VaultLayout, conflict_id: str) -> str | None:
    path = vault.objects_dir / "conflicts" / f"{conflict_id}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return payload.get("statement") or payload.get("text") or None
