"""Minimal Telegram Bot API client (stdlib ``urllib`` only).

The base URL is configurable so tests can point the client at a fake
loopback HTTP server — no test ever contacts api.telegram.org and no
real bot token is read.

The token is passed in by the caller (read from the environment) and is
never logged, never persisted. Error messages deliberately redact it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class TelegramTransportError(RuntimeError):
    """Network / HTTP failure talking to the Bot API."""


class TelegramBotClient:
    def __init__(
        self,
        *,
        token: str,
        base_url: str = "https://api.telegram.org",
        http_timeout: float = 10.0,
        opener: Any | None = None,
    ) -> None:
        self._token = token
        self.base_url = base_url.rstrip("/")
        self.http_timeout = http_timeout
        # A custom opener is injectable for tests; default is urllib's.
        self._opener = opener or urllib.request.build_opener()

    # -- plumbing ---------------------------------------------------------

    def _url(self, method: str) -> str:
        return f"{self.base_url}/bot{self._token}/{method}"

    def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._url(method),
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(req, timeout=self.http_timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise TelegramTransportError(
                f"HTTP {exc.code} from Bot API method {method}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise TelegramTransportError(
                f"transport failure calling Bot API method {method}: "
                f"{type(exc).__name__}") from None
        if not body.get("ok"):
            raise TelegramTransportError(
                f"Bot API method {method} returned ok=false "
                f"({body.get('description', 'no description')})")
        return body.get("result") or {}

    # -- api methods ------------------------------------------------------

    def get_updates(self, *, offset: int | None, timeout: int = 0,
                    limit: int = 20) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": int(timeout), "limit": int(limit)}
        if offset is not None:
            payload["offset"] = int(offset)
        result = self._post("getUpdates", payload)
        if isinstance(result, list):
            return result
        return []

    def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._post("sendMessage", payload)

    @staticmethod
    def encode_callback(token_id: str, action: str) -> str:
        """Compact ``callback_data`` (Telegram caps this at 64 bytes)."""
        return f"d{token_id}:{action}"


def parse_callback(data: str) -> tuple[str, str] | None:
    """Inverse of :meth:`TelegramBotClient.encode_callback`."""
    if not data or not data.startswith("d"):
        return None
    body = data[1:]
    if ":" not in body:
        return None
    token_id, action = body.split(":", 1)
    if not token_id or not action:
        return None
    return token_id, action


def parse_chat_id(chat: dict[str, Any] | None) -> str | None:
    if not chat:
        return None
    raw = chat.get("id")
    return None if raw is None else str(raw)


def parse_user_id(user: dict[str, Any] | None) -> str | None:
    if not user:
        return None
    raw = user.get("id")
    return None if raw is None else str(raw)


def quote(text: str) -> str:
    """Escape a string for safe inclusion in a MarkdownV2 message."""
    special = r"_*[]()~`>#+-=|{}.!"
    return "".join("\\" + c if c in special else c for c in text)


def urlencode(data: dict[str, Any]) -> str:  # pragma: no cover - helper
    return urllib.parse.urlencode(data)
