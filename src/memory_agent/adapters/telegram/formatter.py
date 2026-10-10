"""Notification text + inline-keyboard formatting.

Plain text (no Markdown parse mode) so no user content can break the
message. Callback payloads carry only an opaque token id + action — no
memory text, no conflict body.
"""

from __future__ import annotations

from typing import Any

from memory_agent.adapters.telegram.client import TelegramBotClient


ACTION_LABELS = {
    "adopt_new": "采用新要求",
    "keep_old": "保持旧要求",
    "scope_split": "按范围并存",
    "defer": "稍后处理",
}


def format_conflict_message(
    *,
    conflict: dict[str, Any],
    new_statement: str | None,
    old_statement: str | None,
    tokens_by_action: dict[str, str],
) -> tuple[str, dict[str, Any] | None]:
    revision = conflict.get("revision")
    lines = [
        "Memory Agent 待确认",
        f"冲突 ID: {conflict.get('conflict_id')}",
        f"提案版本: {revision}",
    ]
    if old_statement:
        lines.append(f"旧要求: {_trim(old_statement)}")
    if new_statement:
        lines.append(f"新要求: {_trim(new_statement)}")
    text = "\n".join(lines)
    buttons = []
    for action in ("adopt_new", "keep_old", "scope_split", "defer"):
        token_id = tokens_by_action.get(action)
        if not token_id:
            continue
        buttons.append({
            "text": ACTION_LABELS[action],
            "callback_data": TelegramBotClient.encode_callback(token_id, action),
        })
    markup = None
    if buttons:
        # Two buttons per row.
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        markup = {"inline_keyboard": rows}
    return text, markup


def format_decision_ack(
    *,
    conflict_id: str,
    action: str,
    status: str,
    note: str | None = None,
) -> str:
    lines = [
        "已记录你的决定",
        f"冲突 ID: {conflict_id}",
        f"操作: {ACTION_LABELS.get(action, action)}",
        f"结果: {status}",
    ]
    if note:
        lines.append(f"备注: {_trim(note)}")
    return "\n".join(lines)


def format_rejection(
    *,
    reason: str,
    conflict_id: str | None = None,
) -> str:
    lines = ["决定未生效", f"原因: {reason}"]
    if conflict_id:
        lines.append(f"冲突 ID: {conflict_id}")
    return "\n".join(lines)


def _trim(text: str, limit: int = 200) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
