"""Candidate and evidence contracts produced by extraction."""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

CANDIDATE_SCHEMA_VERSION = 1

CandidateStatus = Literal["validated", "rejected", "pending"]

# Conservative set of "kind" we will auto-commit; anything else becomes
# pending. Matches docs/05 (R02): user requirements/preferences/self-facts.
CandidateKind = Literal["requirement", "preference", "self_fact", "out_of_scope"]
CandidateAbstraction = Literal["principle", "detail", "no_opinion"]
CandidateModality = Literal["must", "prefer", "avoid", "unspecified"]
ScopeLevel = Literal["global", "domain", "project", "task", "session"]


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: ScopeLevel
    project_ids: list[str] = Field(default_factory=list)
    domain_ids: list[str] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    excludes: list[str] = Field(default_factory=list)


class EvidenceRef(BaseModel):
    """A *Unicode code-point* span into the decoded event text.

    The span uses half-open intervals [start, end) and must slice the
    source text exactly when validated.
    """

    model_config = ConfigDict(extra="forbid")

    source_event_id: str
    source_revision_hash: str
    role: Literal["user", "assistant", "tool", "system"]
    origin: Literal["user_session", "assistant_response", "tool_invocation", "replayed_history", "context_only"]
    quote: str
    span_start: int = Field(ge=0)
    span_end: int = Field(ge=0)

    @field_validator("span_end")
    @classmethod
    def _span_end_nonneg(cls, v: int) -> int:
        # Ordering and matching are enforced by the evidence validator.
        return v


class Candidate(BaseModel):
    """A single, conservatively-scoped, source-grounded claim."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    kind: CandidateKind
    abstraction: CandidateAbstraction = "no_opinion"
    statement: str
    modality: CandidateModality = "unspecified"
    scope: Scope
    primary_domain: str | None = None
    tags: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    uncertainty: str | None = None  # free-form model flag


class CandidateSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    candidate_set_id: str
    batch_id: str
    candidates: list[Candidate]
    no_memory: bool = False  # validated empty; explicitly not the same as failure
    prompt_version: str
    model: str
    raw_payload_sha256: str
    status: CandidateStatus = "validated"

    @field_validator("schema_version")
    @classmethod
    def _version_ok(cls, v: int) -> int:
        if v != CANDIDATE_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version={v}; expected {CANDIDATE_SCHEMA_VERSION}")
        return v


def candidate_set_to_dict(cs: CandidateSet) -> dict[str, Any]:
    return cs.model_dump(mode="json")


def candidate_set_from_dict(data: dict[str, Any]) -> CandidateSet:
    return CandidateSet.model_validate(data)