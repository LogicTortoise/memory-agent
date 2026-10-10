"""Application-layer ports.

Ports are protocols; concrete adapters live in adapters/ and
infrastructure/. Application code only depends on the protocols here, so
the planner never sees a CLI, the CLI never sees a model, and the model
never writes to a file.
"""

from __future__ import annotations

from typing import Any, Iterable, Protocol, runtime_checkable

from memory_agent.contracts.candidates import CandidateSet  # noqa: F401  (re-exported)
from memory_agent.contracts.events import ConversationEvent
from memory_agent.contracts.memory import MemoryAtom


@runtime_checkable
class EventStorePort(Protocol):
    """Idempotent event registration."""

    def upsert_event(self, event: ConversationEvent, segment_id: str) -> bool: ...


@runtime_checkable
class ScanCursorPort(Protocol):
    def get_or_register_file(
        self,
        *,
        source_file_id: str,
        connector: str,
        session_id: str,
        branch_id: str,
        canonical_path: str,
        adapter_version: str,
        generation: int,
    ) -> dict[str, Any]: ...

    def update_cursor(
        self,
        *,
        source_file_id: str,
        complete_offset: int,
        size: int,
        mtime: str,
        last_status: str,
    ) -> None: ...


@runtime_checkable
class SegmentStorePort(Protocol):
    def write_segment(
        self,
        *,
        source_file_id: str,
        generation: int,
        start_offset: int,
        end_offset: int,
        events: list[ConversationEvent],
    ) -> tuple[str, str]: ...


@runtime_checkable
class BatchRepositoryPort(Protocol):
    def create_batch(self, batch: dict[str, Any]) -> None: ...
    def claim_batch(self, batch_id: str, lease_token: str, lease_until: str) -> bool: ...
    def finalize_batch(self, batch_id: str, status: str, lease_token: str) -> bool: ...
    def get_batch(self, batch_id: str) -> dict[str, Any] | None: ...
    def list_pending_batches(self, limit: int) -> list[dict[str, Any]]: ...


@runtime_checkable
class ModelProviderPort(Protocol):
    """Provider-agnostic model interface."""

    name: str

    def run_extract(self, task: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class MemoryCommitPort(Protocol):
    def prepare_commit(
        self,
        *,
        proposed_memories: list[MemoryAtom],
        pending_proposals: list[dict[str, Any]],
        base_commit_id: str,
        privacy_epoch: str,
        batch_ids: list[str],
        target_unit_ids: list[str],
    ) -> dict[str, Any]: ...

    def publish(self, commit_id: str) -> dict[str, Any]: ...

    def finalize(self, commit_id: str) -> dict[str, Any]: ...

    def abort(self, commit_id: str) -> None: ...

    def recover_unfinished(self) -> dict[str, Any]: ...


@runtime_checkable
class RecallPort(Protocol):
    def recall(
        self,
        *,
        query: str,
        project_ids: Iterable[str],
        domain_ids: Iterable[str],
        include_archived: bool,
        limit: int,
    ) -> dict[str, Any]: ...


@runtime_checkable
class AuditPort(Protocol):
    def append(self, event: dict[str, Any]) -> None: ...


@runtime_checkable
class SourceReaderPort(Protocol):
    """Per-connector Reader protocol. See docs/09 for full semantics."""

    connector: str
    adapter_version: str

    def discover(self, root: str) -> list[dict[str, Any]]: ...

    def read_delta(self, descriptor: dict[str, Any], offset: int, max_bytes: int) -> dict[str, Any]: ...


@runtime_checkable
class TurnRepositoryPort(Protocol):
    def upsert_turn(self, turn: dict[str, Any]) -> None: ...
    def list_open_turns(self, session_id: str, branch_id: str) -> list[dict[str, Any]]: ...


@runtime_checkable
class WorkUnitRepositoryPort(Protocol):
    def create_units(self, units: list[dict[str, Any]]) -> None: ...
    def active_target_assignment(self, source_event_id: str) -> dict[str, Any] | None: ...


__all__ = [
    "EventStorePort",
    "ScanCursorPort",
    "SegmentStorePort",
    "BatchRepositoryPort",
    "ModelProviderPort",
    "MemoryCommitPort",
    "RecallPort",
    "AuditPort",
    "SourceReaderPort",
    "TurnRepositoryPort",
    "WorkUnitRepositoryPort",
    "CandidateSet",
    "ConversationEvent",
    "MemoryAtom",
]