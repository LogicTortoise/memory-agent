"""Telegram channel configuration.

Project-owned config lives at ``<vault>/config/telegram.json``. It holds
**only references** — the bot token itself is read from the environment
variable named by ``token_env`` (default ``MEMORY_AGENT_TELEGRAM_TOKEN``)
and is never persisted anywhere in the vault.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_TOKEN_ENV = "MEMORY_AGENT_TELEGRAM_TOKEN"
DEFAULT_BASE_URL = "https://api.telegram.org"


@dataclass
class TelegramConfig:
    enabled: bool = False
    token_env: str = DEFAULT_TOKEN_ENV
    base_url: str = DEFAULT_BASE_URL
    allowed_chats: tuple[str, ...] = ()
    allowed_users: tuple[str, ...] = ()
    poll_timeout_seconds: int = 0
    max_updates_per_pass: int = 20
    callback_ttl_seconds: int = 86400
    send_attempts_before_backoff: int = 1
    backoff_seconds: int = 30
    notify_chat_id: str | None = None
    notify_user_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.token_env)


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, int)):
        return (str(value),)
    return tuple(str(v) for v in value)


def config_path(vault_root: Path) -> Path:
    return Path(vault_root) / "config" / "telegram.json"


def load_telegram_config(vault_root: Path) -> TelegramConfig:
    """Load ``config/telegram.json`` if present.

    Missing file → disabled config (no network calls). The token is
    resolved lazily from the environment by the caller via
    ``resolve_token`` so that importing this module never touches a
    secret.
    """
    path = config_path(vault_root)
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except (json.JSONDecodeError, OSError):
            raw = {}
    enabled = bool(raw.get("enabled", True)) and bool(raw)
    cfg = TelegramConfig(
        enabled=enabled,
        token_env=str(raw.get("token_env") or DEFAULT_TOKEN_ENV),
        base_url=str(raw.get("base_url") or DEFAULT_BASE_URL).rstrip("/"),
        allowed_chats=_as_str_tuple(raw.get("allowed_chats")),
        allowed_users=_as_str_tuple(raw.get("allowed_users")),
        poll_timeout_seconds=int(raw.get("poll_timeout_seconds", 0) or 0),
        max_updates_per_pass=int(raw.get("max_updates_per_pass", 20) or 20),
        callback_ttl_seconds=int(raw.get("callback_ttl_seconds", 86400) or 86400),
        send_attempts_before_backoff=int(
            raw.get("send_attempts_before_backoff", 1) or 1),
        backoff_seconds=int(raw.get("backoff_seconds", 30) or 30),
        notify_chat_id=(
            str(raw["notify_chat_id"]) if raw.get("notify_chat_id") else None),
        notify_user_id=(
            str(raw["notify_user_id"]) if raw.get("notify_user_id") else None),
        raw=raw,
    )
    return cfg


def resolve_token(cfg: TelegramConfig) -> str | None:
    """Read the bot token from the environment. Returns None if unset."""
    token = os.environ.get(cfg.token_env)
    return token or None
