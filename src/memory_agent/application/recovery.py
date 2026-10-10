"""Recovery: at startup, walk intents and reconcile HEAD state with
DB phase. Called automatically by main() before processing.
"""

from __future__ import annotations

from typing import Any

from memory_agent.application.commit import CommitService
from memory_agent.infrastructure.files import VaultLayout


def recover_unfinished_intents(*, vault: VaultLayout, conn) -> dict[str, Any]:
    svc = CommitService(vault=vault, conn=conn)
    return svc.recover_unfinished()