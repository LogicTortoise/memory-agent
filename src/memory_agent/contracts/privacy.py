"""Privacy deletion contracts (P9 / T17).

The CLI surface accepts a versioned JSON ``CommandRequest`` with
``operation="privacy.forget"`` / ``"privacy.list"`` /
``"privacy.receipt"`` / ``"privacy.unblock"``. The schema is small
on purpose — the forget authorization is the *only* operation that
removes user content, and the rest are read-only metadata endpoints.
"""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


ForgetKind = Literal["memory", "event", "source_block"]


class PrivacyForgetParams(BaseModel):
    """Authorise a forget operation.

    * ``kind`` is ``memory`` / ``event`` / ``source_block``.
    * ``targets`` is the corresponding id list (memory_id,
      event_id, source_file_id). Duplicate targets collapse.
    * ``idempotency_key`` is **required** so a retry of the same
      forget cannot be silently re-issued; the service refuses
      without it (``INVALID_REQUEST``).
    * ``note`` is an optional short reason stored on the receipt.
    """

    model_config = ConfigDict(extra="forbid")

    kind: ForgetKind
    targets: list[str] = Field(default_factory=list, min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=512)


class PrivacyListParams(BaseModel):
    """List every privacy receipt known to the vault.

    Empty params — listing is content-free metadata only.
    """

    model_config = ConfigDict(extra="forbid")

    kind: ForgetKind | None = None
    limit: int = Field(default=100, ge=1, le=1000)


class PrivacyReceiptParams(BaseModel):
    """Read one privacy receipt by ``op_id``.

    ``op_id`` must match a manifest file in
    ``<vault>/privacy/deletions/``. Unknown op_ids return
    ``NOT_FOUND``.
    """

    model_config = ConfigDict(extra="forbid")

    op_id: str = Field(min_length=1, max_length=64)


class PrivacyUnblockParams(BaseModel):
    """Lift a ``source_block`` re-ingest guard.

    ``op_id`` must reference a ``source_block`` receipt; unblock
    revokes that receipt's effect (the manifest file stays on disk
    but the source is unpinned and the row is removed from
    ``privacy_blocks``). Memory / event forgets are not reversible
    by this endpoint — content has already been erased.
    """

    model_config = ConfigDict(extra="forbid")

    op_id: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=128)


__all__ = [
    "PrivacyForgetParams",
    "PrivacyListParams",
    "PrivacyReceiptParams",
    "PrivacyUnblockParams",
    "ForgetKind",
]
