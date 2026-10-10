"""Atomic memory contracts and status transitions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

from memory_agent.contracts.candidates import EvidenceRef as _EvidenceRef, Scope as _Scope

MEMORY_SCHEMA_VERSION = 1

# Re-export for downstream imports
EvidenceRef = _EvidenceRef
Scope = _Scope

MemoryStatus = Literal["candidate", "needs_confirmation", "active", "superseded", "expired", "archived", "rejected"]
MemoryKind = Literal["requirement", "preference", "self_fact", "procedure", "task_note"]
MemoryAbstraction = Literal["principle", "detail", "no_opinion"]
MemoryHorizon = Literal["persistent", "temporary", "unknown"]
Modality = Literal["must", "prefer", "avoid", "unspecified"]


class MemoryAtom(BaseModel):
    """A single committed memory object.

    Body text lives in the on-disk Markdown file; the structured fields live
    in this dataclass (which is serialized alongside the body as YAML
    frontmatter and a JSON projection). Memory text never enters SQLite.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    memory_id: str
    revision: int = Field(ge=1)
    status: MemoryStatus
    kind: MemoryKind
    abstraction: MemoryAbstraction
    statement: str
    subject: str
    predicate: str | None = None
    value: str | None = None
    modality: Modality
    scope: Scope
    primary_domain: str | None = None
    domains: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    horizon: MemoryHorizon = "unknown"
    valid_from: str | None = None
    valid_until: str | None = None
    asserted_at: str
    recorded_at: str
    evidence: list[EvidenceRef] = Field(default_factory=list)
    relations: list[str] = Field(default_factory=list)
    supersedes: list[str] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _version_ok(cls, v: int) -> int:
        if v != MEMORY_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version={v}; expected {MEMORY_SCHEMA_VERSION}")
        return v

    @field_validator("recorded_at", "asserted_at")
    @classmethod
    def _iso(cls, v: str) -> str:
        # Validate RFC3339 loosely; rejects obviously bad strings.
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid RFC3339 timestamp: {v}") from exc
        return v

    def is_active_at(self, as_of: datetime | None = None) -> bool:
        if self.status != "active":
            return False
        as_of = as_of or datetime.now(timezone.utc)
        if self.valid_from:
            start = datetime.fromisoformat(self.valid_from.replace("Z", "+00:00"))
            if as_of < start:
                return False
        if self.valid_until:
            end = datetime.fromisoformat(self.valid_until.replace("Z", "+00:00"))
            if as_of >= end:
                return False
        return True


def memory_to_dict(m: MemoryAtom) -> dict[str, Any]:
    return m.model_dump(mode="json")


def memory_from_dict(data: dict[str, Any]) -> MemoryAtom:
    return MemoryAtom.model_validate(data)