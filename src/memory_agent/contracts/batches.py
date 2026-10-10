"""BatchSpec, BatchMember, BatchPolicy contracts."""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

BATCH_SCHEMA_VERSION = 1

BatchMemberPurpose = Literal["target", "context_only"]
BatchDisposition = Literal[
    "ready", "leased", "extracted", "reconciling", "completed",
    "retry_wait", "failed", "needs_context", "superseded", "cancelled",
]


class BatchPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_turns_per_batch: int = Field(default=5, ge=1, le=20)
    max_input_tokens: int = Field(default=6000, ge=200)
    max_output_tokens: int = Field(default=1500, ge=64)
    overlap_turns: int = Field(default=0, ge=0, le=2)
    policy_version: str = "batch_policy_v1"


class BatchMember(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_id: str
    purpose: BatchMemberPurpose
    order: int = Field(ge=1)
    epoch: int = Field(ge=1)


class BatchSpec(BaseModel):
    """A planner-produced, planner-owned grouping of work units."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    batch_id: str
    session_id: str
    branch_id: str
    project_id: str | None
    connector: Literal["claude-code", "codex"]
    policy: BatchPolicy
    input_hash: str  # sha256 of the (turn_id, text) sequence + policy
    members: list[BatchMember]
    status: BatchDisposition = "ready"
    attempt: int = Field(default=0, ge=0)
    lease_token: str | None = None
    lease_until: str | None = None

    @field_validator("schema_version")
    @classmethod
    def _version_ok(cls, v: int) -> int:
        if v != BATCH_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version={v}; expected {BATCH_SCHEMA_VERSION}")
        return v

    @field_validator("members")
    @classmethod
    def _members_present(cls, v: list[BatchMember]) -> list[BatchMember]:
        if not v:
            raise ValueError("batch must have at least one member")
        return v

    def target_members(self) -> list[BatchMember]:
        return [m for m in self.members if m.purpose == "target"]


def batch_to_dict(batch: BatchSpec) -> dict[str, Any]:
    return batch.model_dump(mode="json")


def batch_from_dict(data: dict[str, Any]) -> BatchSpec:
    return BatchSpec.model_validate(data)