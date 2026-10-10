"""Reader adapter base + Claude Code / Codex adapters.

These adapters are deliberately small and explicitly configured:
they do not scan the home directory, do not pull from any global path,
and will refuse unrecognised schemas.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from memory_agent.contracts.events import ConversationEvent

__all__ = [
    "BaseReader",
    "ClaudeCodeReader",
    "CodexReader",
]


class UnsupportedFormatError(ValueError):
    """Raised when the file's record format is unknown."""


class BaseReader:
    connector: str = ""
    adapter_version: str = "reader-v0"

    def discover(self, root: str) -> list[dict[str, Any]]:
        """List candidate files under the configured root."""
        raise NotImplementedError

    def read_delta(
        self, descriptor: dict[str, Any], offset: int, max_bytes: int
    ) -> dict[str, Any]:
        """Read a chunk of complete records from `descriptor`.

        Must return: {"records": [<parsed JSON object dicts>],
                    "end_offset": N, "complete": bool, "diagnostics": [...]}
        """
        raise NotImplementedError

    def normalize(
        self, raw: dict[str, Any], *, host_id: str, source_file_id: str,
        generation: int, session_id: str, branch_id: str, project_id: str | None,
        discovered_at: str, record_ordinal: int,
    ) -> ConversationEvent:
        raise NotImplementedError


class ClaudeCodeReader(BaseReader):
    """Claude Code JSONL session reader.

    The expected record shape (illustrative, sanitised) is::

        {"type":"user", "uuid":"u42",
         "message": {"role":"user","content":"...text..."},
         "timestamp":"2026-10-08T13:00:00Z",
         "sessionId":"sess_x", "parentUuid":"a41" or null}

    We deliberately accept a permissive subset; the goal is to extract
    user/assistant text records. Tool calls become kind=tool_call.
    """

    connector = "claude-code"
    adapter_version = "reader-v0.1"

    def discover(self, root: str) -> list[dict[str]]:
        root_path = Path(root)
        if not root_path.exists():
            return []
        out: list[dict[str]] = []
        # Convention: <root>/projects/**/<sessionId>.jsonl
        for jsonl in sorted(root_path.rglob("*.jsonl")):
            out.append({"path": str(jsonl), "kind": "jsonl"})
        return out

    def read_delta(
        self, descriptor: dict[str, Any], offset: int, max_bytes: int
    ) -> dict[str, Any]:
        path = Path(descriptor["path"])
        return _read_jsonl_delta(path, offset, max_bytes)

    def normalize(
        self, raw: dict[str, Any], *, host_id: str, source_file_id: str,
        generation: int, session_id: str, branch_id: str, project_id: str | None,
        discovered_at: str, record_ordinal: int,
    ) -> ConversationEvent:
        rec_type = raw.get("type") or raw.get("role") or ""
        # Map type to role/kind.
        if rec_type == "user":
            role = "user"
            kind = "message"
        elif rec_type in {"assistant", "assistant_text"}:
            role = "assistant"
            kind = "message"
        elif rec_type in {"tool_use", "tool_call"}:
            role = "tool"
            kind = "tool_call"
        elif rec_type in {"tool_result"}:
            role = "tool"
            kind = "tool_result"
        elif rec_type in {"summary"}:
            role = "assistant"
            kind = "summary"
        else:
            raise UnsupportedFormatError(
                f"claude-code: unknown record type {rec_type!r} at ordinal {record_ordinal}"
            )

        text = _extract_text(raw)
        if not text:
            # Tool records without text become summaries so they can still
            # be addressed in segments, but they carry no claim weight.
            text = f"<{kind} without text>"

        from memory_agent.contracts.events import SourceLocator, SourceRef
        locator = SourceLocator(
            byte_start=int(raw.get("__byte_start", 0)),
            byte_end=int(raw.get("__byte_end", 0)),
            record_ordinal=max(1, int(record_ordinal)),
        )
        source = SourceRef(
            connector="claude-code",
            adapter_version=self.adapter_version,
            host_id=host_id,
            source_file_id=source_file_id,
            generation=generation,
            session_id=session_id,
            branch_id=branch_id,
            turn_key=str(raw.get("uuid") or raw.get("turn_key") or f"ord{record_ordinal}"),
            project_id=project_id,
            locator=locator,
            source_time=str(raw.get("timestamp") or discovered_at),
            discovered_at=discovered_at,
        )
        logical_message_id = str(raw.get("uuid") or raw.get("messageId") or f"ord{record_ordinal}")
        revision_hash = raw.get("revisionHash") or raw.get("__revision_hash") or logical_message_id
        origin = "user_session" if role == "user" else (
            "assistant_response" if role == "assistant" else "tool_invocation"
        )

        from memory_agent.contracts.events import ConversationEvent
        return ConversationEvent(
            schema_version=1,
            event_id=f"claude-code::{source_file_id}::{logical_message_id}::{revision_hash}",
            logical_message_id=logical_message_id,
            revision_hash=revision_hash,
            source=source,
            role=role,
            kind=kind,
            text=text,
            parent_id=raw.get("parentUuid"),
            origin=origin,
        )


class CodexReader(BaseReader):
    """Codex JSONL session reader.

    Expected sanitised shape::

        {"event":"message", "id":"u42",
         "role":"user", "text":"...",
         "parent":"a41" or null,
         "ts":"2026-10-08T13:00:00Z",
         "session":"sess_x"}
    """

    connector = "codex"
    adapter_version = "reader-v0.1"

    def discover(self, root: str) -> list[dict[str, Any]]:
        root_path = Path(root)
        if not root_path.exists():
            return []
        out: list[dict[str, Any]] = []
        # Convention: <root>/sessions/<id>.jsonl (one file per session).
        for jsonl in sorted((root_path / "sessions").rglob("*.jsonl")):
            out.append({"path": str(jsonl), "kind": "jsonl"})
        return out

    def read_delta(
        self, descriptor: dict[str, Any], offset: int, max_bytes: int
    ) -> dict[str, Any]:
        path = Path(descriptor["path"])
        return _read_jsonl_delta(path, offset, max_bytes)

    def normalize(
        self, raw: dict[str, Any], *, host_id: str, source_file_id: str,
        generation: int, session_id: str, branch_id: str, project_id: str | None,
        discovered_at: str, record_ordinal: int,
    ) -> ConversationEvent:
        role = raw.get("role") or raw.get("event") or ""
        if role == "user":
            mapped_role = "user"
            kind = "message"
        elif role in {"assistant", "model"}:
            mapped_role = "assistant"
            kind = "message"
        elif role == "tool":
            mapped_role = "tool"
            kind = "tool_call" if raw.get("text") is None else "tool_result"
        else:
            raise UnsupportedFormatError(
                f"codex: unknown role {role!r} at ordinal {record_ordinal}"
            )

        text = raw.get("text") or raw.get("content") or ""
        if not text:
            text = f"<{kind} without text>"

        from memory_agent.contracts.events import SourceLocator, SourceRef, ConversationEvent
        locator = SourceLocator(
            byte_start=int(raw.get("__byte_start", 0)),
            byte_end=int(raw.get("__byte_end", 0)),
            record_ordinal=max(1, int(record_ordinal)),
        )
        source = SourceRef(
            connector="codex",
            adapter_version=self.adapter_version,
            host_id=host_id,
            source_file_id=source_file_id,
            generation=generation,
            session_id=session_id,
            branch_id=branch_id,
            turn_key=str(raw.get("id") or raw.get("turn_key") or f"ord{record_ordinal}"),
            project_id=project_id,
            locator=locator,
            source_time=str(raw.get("ts") or discovered_at),
            discovered_at=discovered_at,
        )
        logical_message_id = str(raw.get("id") or f"ord{record_ordinal}")
        revision_hash = raw.get("revisionHash") or raw.get("__revision_hash") or logical_message_id
        origin = "user_session" if mapped_role == "user" else (
            "assistant_response" if mapped_role == "assistant" else "tool_invocation"
        )

        return ConversationEvent(
            schema_version=1,
            event_id=f"codex::{source_file_id}::{logical_message_id}::{revision_hash}",
            logical_message_id=logical_message_id,
            revision_hash=revision_hash,
            source=source,
            role=mapped_role,
            kind=kind,
            text=text,
            parent_id=raw.get("parent"),
            origin=origin,
        )


def _read_jsonl_delta(path: Path, offset: int, max_bytes: int) -> dict:
    """Read a window of complete JSONL records from `path`.

    Returns:
        records: list of {raw dict, byte_start, byte_end, ordinal, revision_hash?}
        end_offset: bytes consumed (may be 0 if a partial line sits at the
                    start; we never advance past a partial final line)
        complete: True iff we encountered a complete trailing newline
        diagnostics: list of {code, ordinal, message}
    """
    records: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    if not path.exists():
        return {
            "records": records,
            "end_offset": offset,
            "complete": True,
            "diagnostics": [{"code": "file_missing", "message": str(path)}],
        }
    size = path.stat().st_size
    if offset >= size:
        return {
            "records": records,
            "end_offset": offset,
            "complete": True,
            "diagnostics": [],
        }
    with path.open("rb") as f:
        f.seek(offset)
        buf = f.read(max_bytes)
    # Split on the *byte* buffer, never the decoded text: a locator is
    # documented as a byte range into the raw file, and character
    # indices silently disagree with byte offsets as soon as a line
    # contains any non-ASCII content.
    cursor = 0
    ordinal = 0
    while True:
        nl = buf.find(b"\n", cursor)
        if nl == -1:
            # Final (possibly partial) line. Do not advance past it.
            break
        line = buf[cursor:nl]
        byte_start = offset + cursor
        byte_end = offset + nl + 1  # include newline
        cursor = nl + 1
        ordinal += 1
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            diagnostics.append({
                "code": "bad_json",
                "ordinal": ordinal,
                "message": f"line {ordinal} failed to parse: {exc}",
            })
            continue
        obj["__byte_start"] = byte_start
        obj["__byte_end"] = byte_end
        # Use a deterministic hash (sha256 truncated to 64 bits) so
        # event_id is stable across Python processes. Python's
        # built-in ``hash()`` is randomised by PYTHONHASHSEED, which
        # would cause repeated runs to emit "duplicate" events for
        # the same content.
        import hashlib
        obj["__revision_hash"] = hashlib.sha256(
            f"{byte_start}|".encode("utf-8") + line
        ).hexdigest()[:16]
        records.append(obj)
    end_offset = offset + cursor
    complete = (end_offset == size) and (buf.endswith(b"\n") or not buf)
    # If we found some content but stopped at a partial line, complete=False.
    if not buf.endswith(b"\n") and buf.strip():
        complete = False
    return {
        "records": records,
        "end_offset": end_offset,
        "complete": complete,
        "diagnostics": diagnostics,
    }


def _extract_text(raw: dict[str, Any]) -> str:
    """Pull a string out of either flat "text"/"content" or {"message": {"content": ...}}."""
    if isinstance(raw.get("text"), str):
        return raw["text"]
    if isinstance(raw.get("content"), str):
        return raw["content"]
    msg = raw.get("message")
    if isinstance(msg, dict):
        c = msg.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            parts = []
            for part in c:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif isinstance(part, str):
                    parts.append(part)
            return "\n".join(parts)
    return ""