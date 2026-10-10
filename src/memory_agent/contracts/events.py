"""ConversationEvent and source-locator contracts."""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

CONVERSATION_EVENT_SCHEMA_VERSION = 1

Role = Literal["user", "assistant", "tool", "system"]
EventKind = Literal["message", "tool_call", "tool_result", "summary"]
Origin = Literal["user_session", "assistant_response", "tool_invocation", "replayed_history", "context_only"]
PrivacyClass = Literal["standard", "sensitive"]


class SourceLocator(BaseModel):
    """Stable byte/record locator into the *raw* source file.

    Byte offsets are bytes into the file as it existed at read time.
    Record ordinals are 1-based line indices into the same file.
    """

    model_config = ConfigDict(extra="forbid")

    byte_start: int = Field(ge=0)
    byte_end: int = Field(ge=0)
    record_ordinal: int = Field(ge=1)


class SourceRef(BaseModel):
    """Where an event was discovered and how to get back to the raw line."""

    model_config = ConfigDict(extra="forbid")

    connector: Literal["claude-code", "codex"]
    adapter_version: str
    host_id: str
    source_file_id: str
    generation: int = Field(ge=1)
    session_id: str
    branch_id: str
    turn_key: str
    project_id: str | None = None
    locator: SourceLocator
    source_time: str  # RFC3339
    discovered_at: str  # RFC3339

    @field_validator("session_id", "branch_id", "turn_key", "source_file_id")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("must be non-empty")
        return v


class ConversationEvent(BaseModel):
    """A single normalized record produced by a Reader.

    The event_id is a stable key used for idempotent ingestion and de-dup;
    it must be derived from (connector, source_file_id, generation,
    session_id, branch_id, turn_key, logical_message_id, revision_hash).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    event_id: str
    logical_message_id: str
    revision_hash: str
    source: SourceRef
    role: Role
    kind: EventKind
    text: str
    parent_id: str | None = None
    replaces_event_id: str | None = None
    origin: Origin
    privacy: PrivacyClass = "standard"
    context_only: bool = False

    @field_validator("schema_version")
    @classmethod
    def _version_ok(cls, v: int) -> int:
        if v != CONVERSATION_EVENT_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version={v}; expected {CONVERSATION_EVENT_SCHEMA_VERSION}")
        return v

    @field_validator("text")
    @classmethod
    def _text_not_empty(cls, v: str) -> str:
        if v == "":
            raise ValueError("text must be non-empty (use kind=summary if you need a placeholder)")
        return v

    def canonical_event_key(self) -> str:
        """Stable, content-derived key used as the SQLite unique key."""
        parts = [
            self.source.connector,
            self.source.source_file_id,
            str(self.source.generation),
            self.source.session_id,
            self.source.branch_id,
            self.source.turn_key,
            self.logical_message_id,
            self.revision_hash,
        ]
        return "|".join(parts)


def event_to_dict(event: ConversationEvent) -> dict[str, Any]:
    return event.model_dump(mode="json")


def event_from_dict(data: dict[str, Any]) -> ConversationEvent:
    return ConversationEvent.model_validate(data)