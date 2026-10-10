"""Telegram Bot API decision channel (P4 / T12).

The adapter is optional. When it is not configured (``config/telegram.json``
missing or the token environment variable unset) every entry point in this
package becomes a no-op and **no network call is made** — the local CLI
(``memory conflicts`` / ``memory decide``) remains the only decision path.

Guarantees implemented here:

  * Notifications are sent from a durable outbox with retry; the send
    is at-least-once with a stable ``notification_id``.
  * Incoming updates are deduplicated by ``update_id``; the durable
    update offset lives in ``runtime_state``. A decision and its
    update receipt are persisted *before* the offset advances, so a
    crash between the two replays into an idempotent no-op rather than
    a second commit.
  * Identity = allowed ``chat_id`` + allowed ``user_id``; the callback
    also carries a short-lived opaque token bound to
    (conflict_id, proposal_revision, action). Unknown / expired /
    stale-revision callbacks never commit.
  * Decisions go through ``dispatch(operation="decide")`` — literally
    the same application service the CLI uses.
  * Bot-originated updates (bot echo) are recorded as ``ignored`` and
    never become a decision or a source event.
  * The bot token is read from the environment and is never written
    to SQLite, artifacts, logs or audit rows.
"""

from memory_agent.adapters.telegram.config import TelegramConfig, load_telegram_config
from memory_agent.adapters.telegram.client import TelegramBotClient, TelegramTransportError
from memory_agent.adapters.telegram.formatter import (
    format_conflict_message,
    format_decision_ack,
)
from memory_agent.adapters.telegram.service import run_telegram_pass

__all__ = [
    "TelegramConfig",
    "load_telegram_config",
    "TelegramBotClient",
    "TelegramTransportError",
    "format_conflict_message",
    "format_decision_ack",
    "run_telegram_pass",
]
