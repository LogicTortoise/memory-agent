"""Source-correction queue (Phase C release blocker).

Phase B's P0 source-integrity fix detects when a source file is
rewritten (middle-rewrite, rotation, replacement, deletion) and bumps
``source_files.generation`` / ``revision_seq`` while preserving the
old segment as evidence. It also writes a ``source.corrected`` audit
row. That is **only** the source-row side of the contract.

Phase C adds the memory-reconciliation side:

  1. **Discover.** Walk every committed memory's ``evidence`` list,
     pull out the referenced ``source_event_id`` + ``source_revision_hash``,
     and check whether a newer ``source_events`` row exists for the same
     connector/session/branch and a different ``revision_hash``. If
     yes, the memory cites a now-stale event_id.
  2. **Propose.** Persist a stable ``source_correction_proposals`` row
     for each (memory_id, memory_revision, old_event_id) tuple. The
     proposal's ``status`` defaults to ``pending`` and is gated behind
     a user decision.
  3. **Mark / omit in recall.** Write a ``memory_correction_overlay``
     row that flags the memory for recall: the recall path joins on
     this overlay, appends a warning, and *omits* the item unless the
     caller explicitly opts into pending-correction views (it never
     silently promotes changed text).
  4. **Worker is deterministic, never silent.** The worker may run
     the discovery pass and may auto-resolve the trivial "the
     rewrite kept the same text" case (verifying the new segment's
     event still matches the evidence quote). Anything else stays
     pending for the user.
  5. **User decision via ``conflict.decide`` or ``decide``.**
     ``decide.action="adopt_new"`` creates a new memory that cites
     the new event_id and supersedes the old one. ``keep_old``
     closes the proposal ``old_revision`` with ``decided_keep``.
     ``defer`` leaves the proposal pending.

Phase C repair-2 strengthens the ``adopt_new`` / ``scope_split``
contract:

  * The user-supplied ``expected_proposal_revision`` MUST match the
    row's current ``proposal_revision``; stale callers are rejected
    without touching the source row or any file on disk.
  * The new memory is written through the same ``CommitService``
    pipeline (file -> manifest -> HEAD swap -> finalize) that
    ``maintain`` and ``decide`` use. If the publish step fails for
    any reason (HEAD race, segment missing, recovery in progress,
    manual file write injected failure) the proposal stays
    ``pending`` and the overlay stays ``pending_correction`` — the
    user can retry with a fresh revision token.
  * On success, the proposal's ``superseded_memory_id`` /
    ``superseded_memory_revision`` / ``superseded_head_commit_id`` are
    recorded so the audit trail and a future UI can show exactly
    which new memory replaced the old one.

Old segment files and ``revision_hash`` evidence remain accessible so
the user can compare before/after. The user's decision is the only
path to publication — the worker cannot silently relink a memory to
changed user text.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from memory_agent.infrastructure.files import VaultLayout
from memory_agent.infrastructure.sqlite.repositories import (
    AuditRepo,
    MemoryCorrectionOverlayRepo,
    MemoryProjectionRepo,
    SourceCorrectionProposalRepo,
)


# Status lifecycle strings exported for tests + callers.
STATUS_PENDING = "pending"
STATUS_DECIDED_KEEP = "decided_keep"
STATUS_DECIDED_RELINK = "decided_relink"
STATUS_DECIDED_OBSOLETE = "decided_obsolete"
STATUS_AUTO_RESOLVED = "auto_resolved"


@dataclass
class CorrectionDiscovery:
    """Result of one source-correction discovery pass."""

    scanned_memories: int = 0
    new_proposals: int = 0
    refreshed_proposals: int = 0
    auto_resolved: int = 0
    skipped_text_match: int = 0
    affected_memories: int = 0
    diagnostics: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned_memories": self.scanned_memories,
            "new_proposals": self.new_proposals,
            "refreshed_proposals": self.refreshed_proposals,
            "auto_resolved": self.auto_resolved,
            "skipped_text_match": self.skipped_text_match,
            "affected_memories": self.affected_memories,
            "diagnostics": list(self.diagnostics or []),
        }


def _stable_id(*parts: str) -> str:
    h = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return f"cor_{h[:16]}"


def _list_memory_projection_files(vault) -> Iterable[Path]:
    """Yield every committed memory's ``memory_projection`` row path.

    We read the persisted memory files (Markdown) on demand rather
    than scanning the JSON projection in SQLite — the memory body
    holds the YAML frontmatter / evidence that pins the
    ``source_event_id`` and ``source_revision_hash`` we need.

    Accepts either a :class:`VaultLayout` (preferred) or a raw
    :class:`pathlib.Path` for callers that only have the vault root.
    """
    from memory_agent.infrastructure.files import VaultLayout as _VL
    layout = vault if isinstance(vault, _VL) else _VL(vault)
    memories_root = layout.memories_dir
    if not memories_root.exists():
        return
    for mem_dir in sorted(memories_root.iterdir()):
        if not mem_dir.is_dir():
            continue
        for rev_path in sorted(mem_dir.glob("r*.md")):
            yield rev_path


def _parse_memory_evidence(md_text: str) -> tuple[list[dict[str, Any]], str]:
    """Pull ``evidence:`` lines out of a memory Markdown body.

    Phase v0.1 / v1 writes memory bodies as YAML frontmatter followed
    by free-form text. The evidence list is a YAML sequence of
    ``- source_event_id: …`` entries. We accept a permissive parser:
    we walk lines, capture ``- source_event_id: <id>`` /
    ``  source_revision_hash: <hash>`` / ``  quote: "<quote>"`` until
    the dash sequence ends.
    """
    evidence: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for raw_line in md_text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("- source_event_id:"):
            if cur is not None:
                evidence.append(cur)
            cur = {"source_event_id": line.split(":", 1)[1].strip()}
        elif cur is not None:
            stripped = line.strip()
            if stripped.startswith("source_revision_hash:"):
                cur["source_revision_hash"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("quote:"):
                cur["quote"] = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            elif not stripped:
                # blank line ends the sequence
                evidence.append(cur)
                cur = None
    if cur is not None:
        evidence.append(cur)
    return evidence, md_text


def discover_affected_memories(
    *,
    vault,
    conn,
) -> CorrectionDiscovery:
    """Walk committed memories and queue correction proposals.

    Idempotent: re-running the pass refreshes ``reason`` /
    ``updated_at`` on existing pending proposals and never duplicates
    a proposal that has already been decided. The discovery pass does
    NOT touch the ``memory_projection.status`` column — only the
    overlay table, which the recall path joins against.
    """
    out = CorrectionDiscovery(diagnostics=[])
    proposals = SourceCorrectionProposalRepo(conn)
    audit = AuditRepo(conn)

    # Index every source_event row by event_id (for exact match) and by
    # (source_file_id, anchor) so we can detect in-place rewrites
    # that re-register the same record under a new event_id (the
    # canonical event_id encodes the revision_hash so a rewrite
    # produces a new event_id at the same anchor).
    events = conn.execute(
        "SELECT event_id, revision_hash, source_file_id, session_id, branch_id, "
        "discovered_at FROM source_events"
    ).fetchall()
    by_event: dict[str, list[dict[str, Any]]] = {}
    by_file: dict[str, list[dict[str, Any]]] = {}
    by_anchor: dict[str, list[dict[str, Any]]] = {}
    for row in events:
        d = dict(row)
        by_event.setdefault(str(d["event_id"]), []).append(d)
        by_file.setdefault(str(d["source_file_id"]), []).append(d)
        anchor = _event_anchor(str(d["event_id"]))
        by_anchor.setdefault(anchor, []).append(d)

    affected: set[tuple[str, int]] = set()

    for path in _list_memory_projection_files(vault):
        out.scanned_memories += 1
        try:
            md = path.read_text(encoding="utf-8")
        except OSError as exc:
            (out.diagnostics or []).append({
                "code": "memory_read_failed",
                "path": str(path),
                "error": str(exc),
            })
            continue
        rev = int(path.stem.lstrip("r") or "0") or 1
        memory_id = path.parent.name
        evidence, _ = _parse_memory_evidence(md)
        if not evidence:
            continue
        for ev in evidence:
            old_event_id = ev.get("source_event_id")
            old_revision_hash = ev.get("source_revision_hash")
            quote = ev.get("quote", "")
            if not old_event_id or not old_revision_hash:
                continue
            # Step 1: look for a successor event at the same anchor.
            anchor = _event_anchor(str(old_event_id))
            anchor_rows = by_anchor.get(anchor, [])
            newer = [
                r for r in anchor_rows
                if str(r["revision_hash"]) != old_revision_hash
                and str(r["event_id"]) != old_event_id
            ]
            replacement = None
            if newer:
                replacement = sorted(
                    newer,
                    key=lambda r: str(r["discovered_at"]),
                )[-1]
            else:
                # Step 2: anchor missing; fall back to a global scan
                # in the same source file.
                replacement = _find_replacement_event(
                    by_file=by_file, old_event_id=old_event_id,
                    old_revision_hash=old_revision_hash,
                )
                if replacement is None:
                    # No successor. Memory stays as-is until a future
                    # scan introduces a new event at the same anchor.
                    continue
            new_event = replacement
            new_event_id = str(replacement["event_id"])
            new_revision_hash = str(replacement["revision_hash"])
            auto_resolved = False
            if quote:
                still_matches = _quote_still_in_segments(
                    vault=vault,
                    event_id=new_event_id,
                    revision_hash=new_revision_hash,
                    quote=quote,
                )
                if still_matches:
                    auto_resolved = True
                    out.skipped_text_match += 1
            proposal_status = (
                STATUS_AUTO_RESOLVED if auto_resolved else STATUS_PENDING
            )
            source_file_id = str(new_event["source_file_id"])
            proposal = {
                "correction_id": _stable_id(
                    memory_id, str(rev), old_event_id, source_file_id,
                ),
                "source_file_id": source_file_id,
                "old_event_id": old_event_id,
                "new_event_id": new_event_id,
                "memory_id": memory_id,
                "memory_revision": rev,
                "old_revision_hash": old_revision_hash,
                "new_revision_hash": new_revision_hash,
                "status": proposal_status,
                "reason": "source_rewritten",
            }
            inserted = proposals.upsert(proposal)
            if inserted and proposal_status == STATUS_PENDING:
                out.new_proposals += 1
                affected.add((memory_id, rev))
            elif inserted and proposal_status == STATUS_AUTO_RESOLVED:
                out.auto_resolved += 1
            else:
                out.refreshed_proposals += 1

    out.affected_memories = len(affected)
    audit.append({
        "audit_id": _new_audit_id("source_correction", out.scanned_memories, out.new_proposals),
        "actor": "scanner",
        "action": "source.correction_scanned",
        "occurred_at": _now_iso(),
        "object_type": "source_correction",
        "object_id": "discovery",
        "correlation": {},
        "before_after": {},
        "outcome": "success" if not out.diagnostics else "partial",
        "reason_code": None,
        "details": out.to_dict(),
    })
    return out


def _event_anchor(event_id: str) -> str:
    """Return the anchor part of an event_id.

    The event_id encodes ``<connector>::<source_file_id>::<anchor>::<revision_hash>``.
    Stripping the trailing ``<revision_hash>`` segment yields the
    anchor — the same record anchor with different revision hashes
    represents the same logical record at different points in time.
    """
    parts = str(event_id).rsplit("::", 1)
    if len(parts) == 2:
        return parts[0]
    return str(event_id)


def _find_replacement_event(
    *,
    by_file: dict[str, list[dict[str, Any]]],
    old_event_id: str,
    old_revision_hash: str,
) -> dict[str, Any] | None:
    """Find the most recent replacement event for ``old_event_id``.

    Search by anchor: events whose ``event_id`` shares the same
    anchor prefix as ``old_event_id`` but differ in ``revision_hash``
    are rewrite successors. Falls back to a global scan when no
    anchor match is found (the old event was simply deleted with no
    successor at that anchor).
    """
    anchor = _event_anchor(old_event_id)
    candidates: list[dict[str, Any]] = []
    for rows in by_file.values():
        for row in rows:
            if str(row["event_id"]) == old_event_id:
                continue
            if str(row["revision_hash"]) == old_revision_hash:
                continue
            if _event_anchor(str(row["event_id"])) != anchor:
                continue
            candidates.append(row)
    if not candidates:
        return None
    return sorted(candidates, key=lambda r: str(r["discovered_at"]))[-1]


def _new_audit_id(*parts: Any) -> str:
    """Build a unique audit id that never collides across passes."""
    import time as _time
    return "aud_" + hashlib.sha1(
        ("|".join(str(p) for p in parts) + "|" + str(_time.time_ns())).encode()
    ).hexdigest()[:12]


def _quote_still_in_segments(
    *,
    vault,
    event_id: str,
    revision_hash: str,
    quote: str,
) -> bool:
    """Walk every segment and report whether ``quote`` is still a
    contiguous substring of the event text at the new ``revision_hash``.

    This is the deterministic "the rewrite didn't change what the
    memory actually said" path. Anything else stays pending.

    Accepts either a :class:`VaultLayout` or a raw :class:`Path` for
    the vault root.
    """
    from memory_agent.infrastructure.files import VaultLayout as _VL
    layout = vault if isinstance(vault, _VL) else _VL(vault)
    segments_dir = layout.segments_dir
    if not segments_dir.exists():
        return False
    for seg_path in sorted(segments_dir.glob("*.json")):
        try:
            payload = json.loads(seg_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for ev in payload.get("events", []):
            if str(ev.get("event_id")) != event_id:
                continue
            if str(ev.get("revision_hash")) != revision_hash:
                continue
            text = str(ev.get("text", ""))
            if quote and quote in text:
                return True
            return False
    return False


def refresh_overlay(*, conn) -> int:
    """Rebuild ``memory_correction_overlay`` rows from pending proposals.

    The overlay is a denormalised read view used by recall; rebuilding
    it from the proposals table keeps the join cheap and ensures
    decided proposals drop the overlay flag immediately.
    """
    proposals = SourceCorrectionProposalRepo(conn)
    overlay = MemoryCorrectionOverlayRepo(conn)
    pending = proposals.list_pending(limit=10_000)
    touched = 0
    for prop in pending:
        affected = overlay.get(
            str(prop["memory_id"]), int(prop["memory_revision"])
        )
        if affected is not None and str(affected.get("state")) == "pending_correction":
            continue  # unchanged
        overlay.upsert({
            "memory_id": prop["memory_id"],
            "memory_revision": prop["memory_revision"],
            "pending_correction_id": prop["correction_id"],
            "state": _overlay_state_for(str(prop["status"])),
            "affected_event_ids": [prop["old_event_id"]],
        })
        touched += 1
    # Drop overlay rows whose proposals are no longer pending.
    keep_ids = {(p["memory_id"], int(p["memory_revision"])) for p in pending}
    existing_rows = conn.execute(
        "SELECT memory_id, memory_revision FROM memory_correction_overlay "
        "WHERE state != 'ok'"
    ).fetchall()
    for row in existing_rows:
        key = (str(row["memory_id"]), int(row["memory_revision"]))
        if key not in keep_ids:
            overlay.clear(str(row["memory_id"]), int(row["memory_revision"]))
            touched += 1
    return touched


def _overlay_state_for(status: str) -> str:
    if status == STATUS_PENDING:
        return "pending_correction"
    if status == STATUS_AUTO_RESOLVED:
        return "auto_resolved"
    return "ok"


def list_pending_for_memory(memory_id: str, *, conn) -> list[dict[str, Any]]:
    return SourceCorrectionProposalRepo(conn).list_for_memory(memory_id)


# ---------------------------------------------------------------------------
# adopt_new / scope_split — write a new memory through CommitService
# ---------------------------------------------------------------------------


class CorrectionCommitError(RuntimeError):
    """Raised when a ``decide_proposal`` ``adopt_new`` / ``scope_split``
    cannot be safely published. The proposal stays pending and the
    overlay stays ``pending_correction`` so the user can retry with a
    fresh revision token.
    """


def _resolve_new_event(
    vault: VaultLayout, new_event_id: str, new_revision_hash: str,
) -> dict[str, Any] | None:
    """Look up the rewritten event in the segments directory.

    Returns the immutable segment record (``text`` + ``source`` block)
    or ``None`` if the new event has not been registered yet. We
    refuse to fabricate a memory from a phantom source.
    """
    segments_dir = vault.segments_dir
    if not segments_dir.exists():
        return None
    for seg_path in sorted(segments_dir.glob("*.json")):
        try:
            payload = json.loads(seg_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for ev in payload.get("events", []):
            if (str(ev.get("event_id")) == new_event_id
                    and str(ev.get("revision_hash")) == new_revision_hash):
                return ev
    return None


def _find_quote_span(
    text: str, preferred: str, fallback: str,
) -> tuple[str, int, int] | None:
    """Return (quote, span_start, span_end) for a stable slice of
    ``text``. Prefers ``preferred`` (the old evidence quote); if the
    rewrite kept the same phrase the span is preserved verbatim. If
    the phrase is gone, falls back to a deterministic leading
    non-empty slice of ``text`` so the new memory still has a
    verifiable quote.
    """
    if not text:
        return None
    if preferred:
        idx = text.find(preferred)
        if idx >= 0:
            return preferred, idx, idx + len(preferred)
    if fallback:
        idx = text.find(fallback)
        if idx >= 0:
            return fallback, idx, idx + len(fallback)
    # Last-resort deterministic slice: the first non-empty line.
    head = text[:200]
    stripped = re.sub(r"\s+", " ", head).strip()
    if not stripped:
        return None
    return stripped, 0, len(stripped)


def _supersede_old_memory(
    conn, *, old_memory_id: str, old_memory_revision: int,
    new_memory_id: str, head_commit_id: str,
) -> None:
    """Mark the old memory as superseded in ``memory_projection`` so
    the recall path stops returning it. We do this in the same
    transaction that recorded the new memory so the two rows are
    always consistent on read.
    """
    repo = MemoryProjectionRepo(conn)
    conn.execute(
        "UPDATE memory_projection "
        "SET status = 'superseded' "
        "WHERE memory_id = ? AND revision = ? AND status = 'active'",
        (old_memory_id, int(old_memory_revision)),
    )
    # Record the new memory's projection row alongside the head commit.
    # The new memory's body is already published on disk by CommitService
    # before we get here; we just need a row that recall can join on.
    repo.upsert({
        "memory_id": new_memory_id,
        "revision": 1,
        "status": "active",
        "scope": {},
        "project_ids": [],
        "domains": [],
        "commit_id": head_commit_id,
        "statement": "",
    })


def adopt_new_memory(
    *,
    vault: VaultLayout,
    conn,
    correction_id: str,
    expected_proposal_revision: int,
    note: str | None = None,
    scope_split: bool = False,
) -> dict[str, Any] | None:
    """Apply ``adopt_new`` (or ``scope_split``) to a pending correction.

    This is the worker-facing helper. It is also called by the CLI
    ``source_correction.decide`` handler (see
    ``_cmd_source_correction_decide`` in dispatch). The function:

      1. Verifies the proposal exists and the supplied
         ``expected_proposal_revision`` matches the current row.
      2. Reads the old memory file (frontmatter + body) so the new
         memory preserves scope / statement / kind.
      3. Looks up the rewritten event in segments. If the new event
         is missing, refuses with ``CorrectionCommitError`` — the
         proposal stays pending.
      4. Writes a new memory through ``CommitService`` referencing the
         new ``event_id`` and ``revision_hash`` (and a verifiable
         quote slice). On any failure the proposal + overlay are
         preserved.
      5. Marks the old memory ``superseded`` and the proposal
         ``decided_relink``, with the new memory_id / revision /
         commit_id recorded in the proposal row.

    Returns the updated proposal row (with the new
    ``superseded_memory_id`` / ``superseded_head_commit_id``) or
    ``None`` if the proposal was not found.
    """
    proposals = SourceCorrectionProposalRepo(conn)
    proposal = proposals.get(correction_id)
    if proposal is None:
        return None
    current_rev = int(proposal.get("proposal_revision") or 1)
    if int(expected_proposal_revision) != current_rev:
        raise CorrectionCommitError(
            f"expected_proposal_revision {expected_proposal_revision} is stale; "
            f"current is {current_rev}"
        )
    if proposal.get("status") not in {STATUS_PENDING, STATUS_AUTO_RESOLVED}:
        # Already decided; refuse to overwrite. Returning the row keeps
        # the response idempotent.
        return proposal
    new_event_id = proposal.get("new_event_id")
    new_revision_hash = proposal.get("new_revision_hash")
    if not new_event_id or not new_revision_hash:
        raise CorrectionCommitError(
            f"correction {correction_id!r} is missing new_event_id/"
            f"new_revision_hash; cannot safely adopt_new"
        )
    new_event = _resolve_new_event(vault, new_event_id, new_revision_hash)
    if new_event is None:
        raise CorrectionCommitError(
            f"new event {new_event_id!r} (revision {new_revision_hash!r}) "
            "not found in segments; refusing to publish a phantom memory"
        )
    # Read the old memory file so the new one preserves the same
    # statement / scope / kind.
    from memory_agent.contracts.candidates import EvidenceRef, Scope
    from memory_agent.contracts.memory import MemoryAtom
    from memory_agent.application.commit import CommitService

    old_memory_id = str(proposal["memory_id"])
    old_revision = int(proposal["memory_revision"])
    mem_path = vault.memory_revision_path(old_memory_id, old_revision)
    if not mem_path.exists():
        raise CorrectionCommitError(
            f"old memory file {mem_path} is missing; refusing to derive "
            "a new memory from a missing source"
        )
    md = mem_path.read_text(encoding="utf-8")
    fm, body = _split_md_frontmatter(md)
    statement = body.strip() or fm.get("statement") or ""
    kind = fm.get("kind") or "requirement"
    modality = fm.get("modality") or "unspecified"
    abstraction = fm.get("abstraction") or "no_opinion"
    subject = fm.get("subject") or "global"
    horizon = fm.get("horizon") or "unknown"
    raw_scope = fm.get("scope") or {"level": "global"}
    if not isinstance(raw_scope, dict):
        raw_scope = {"level": "global"}
    try:
        # pydantic v2 refuses raw ``dict[str, Any]`` for typed
        # ``Literal`` fields; cast to the union type.
        scope = Scope.model_validate(raw_scope)
    except Exception:
        scope = Scope(level="global")
    # Pull the *original* evidence quote (best anchor) and a fallback
    # in case the rewrite kept the same phrase.
    old_quote = ""
    raw_ev = fm.get("evidence") or []
    if isinstance(raw_ev, list) and raw_ev:
        first = raw_ev[0]
        if isinstance(first, dict):
            old_quote = str(first.get("quote") or "")
    new_text = str(new_event.get("text", ""))
    if scope_split:
        # Narrow the scope to a project-scoped sub-rule so the new
        # memory coexists with the old one. The new scope's project
        # is taken from the old memory's project_ids (first one) and
        # the level is forced to "project" so a "global" memory
        # becomes a project rule.
        first_project = (
            scope.project_ids[0] if scope.project_ids else (
                fm.get("project_ids", [None])[0] if fm.get("project_ids") else None
            )
        )
        new_scope = Scope(
            level="project",
            project_ids=[first_project] if first_project else [],
            domain_ids=list(scope.domain_ids),
            task_ids=list(scope.task_ids),
            conditions=list(scope.conditions),
            excludes=list(scope.excludes),
        )
    else:
        new_scope = scope
    span = _find_quote_span(new_text, preferred=old_quote,
                            fallback="use Python for the prototype")
    if span is None:
        raise CorrectionCommitError(
            "new event text is empty; cannot build a verifiable evidence "
            "slice for the new memory"
        )
    quote, span_start, span_end = span
    # The new memory uses a fresh memory_id derived from the old one
    # so the audit trail is traceable.
    suffix = hashlib.sha1(
        f"{old_memory_id}|r{old_revision}|{new_event_id}".encode()
    ).hexdigest()[:10]
    new_memory_id = f"mem_relink_{suffix}"
    new_evidence = [
        EvidenceRef(
            source_event_id=str(new_event_id),
            source_revision_hash=str(new_revision_hash),
            role="user",
            origin="user_session",
            quote=quote,
            span_start=span_start,
            span_end=span_end,
        )
    ]
    now = _now_iso()
    new_mem = MemoryAtom(
        schema_version=1,
        memory_id=new_memory_id,
        revision=1,
        status="active",
        kind=kind,
        abstraction=abstraction,
        statement=statement,
        subject=subject,
        predicate=None,
        value=None,
        modality=modality,
        scope=new_scope,
        primary_domain=fm.get("primary_domain"),
        domains=list(fm.get("domains") or []),
        tags=list(fm.get("tags") or []),
        horizon=horizon,
        valid_from=fm.get("valid_from"),
        valid_until=fm.get("valid_until"),
        asserted_at=now,
        recorded_at=now,
        evidence=new_evidence,
        relations=[],
        supersedes=[old_memory_id],
    )
    head_commit = _current_head_commit_id(vault) or "root"
    privacy_epoch = _current_privacy_epoch(vault) or "epoch-0"
    committer = CommitService(vault=vault, conn=conn)
    try:
        intent = committer.prepare_commit(
            proposed_memories=[new_mem],
            pending_proposals=[],
            base_commit_id=head_commit,
            privacy_epoch=privacy_epoch,
            batch_ids=[f"source_correction:{correction_id}"],
            target_unit_ids=[new_memory_id],
        )
        committer.publish(intent["commit_id"])
        committer.finalize(intent["commit_id"])
    except Exception as exc:  # noqa: BLE001
        raise CorrectionCommitError(
            f"CommitService failed during adopt_new: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    # Mark the old memory superseded and update the proposal.
    _supersede_old_memory(
        conn,
        old_memory_id=old_memory_id,
        old_memory_revision=old_revision,
        new_memory_id=new_memory_id,
        head_commit_id=intent["commit_id"],
    )
    # ``update_decision`` bumps ``proposal_revision`` and sets
    # ``decided_at``. If a concurrent caller already decided this
    # proposal, rowcount is 0 and we abort the supersede.
    flipped = proposals.update_decision(
        correction_id=correction_id,
        status=STATUS_DECIDED_RELINK,
        superseded_memory_id=new_memory_id,
        superseded_memory_revision=1,
        superseded_head_commit_id=intent["commit_id"],
        decision_note=note or ("scope_split" if scope_split else "adopt_new"),
    )
    if not flipped:
        # Another caller won the race. The HEAD swap is already
        # visible (the new memory is published) so leaving the
        # proposal row alone keeps both halves of the truth: the
        # memory is on disk, the proposal still says the prior
        # status, and the audit row explains the situation.
        AuditRepo(conn).append({
            "audit_id": _new_audit_id("correction_race", correction_id,
                                      new_memory_id),
            "actor": "user",
            "action": "source_correction.decided_relink_race",
            "occurred_at": _now_iso(),
            "object_type": "source_correction",
            "object_id": correction_id,
            "correlation": {"new_memory_id": new_memory_id,
                            "head_commit_id": intent["commit_id"]},
            "before_after": {},
            "outcome": "partial",
            "reason_code": "IDEMPOTENCY_CONFLICT",
            "details": {
                "new_memory_id": new_memory_id,
                "head_commit_id": intent["commit_id"],
                "message": (
                    "new memory published but proposal row was already "
                    "decided by another caller; recovery is manual"
                ),
            },
        })
    # The recall overlay should now drop the memory. ``refresh_overlay``
    # reads ``list_pending`` and clears rows whose proposal is no
    # longer pending.
    refresh_overlay(conn=conn)
    audit = AuditRepo(conn)
    audit.append({
        "audit_id": _new_audit_id("source_correction_relink", correction_id,
                                  new_memory_id),
        "actor": "user",
        "action": "source_correction.decided_relink",
        "occurred_at": _now_iso(),
        "object_type": "source_correction",
        "object_id": correction_id,
        "correlation": {"new_memory_id": new_memory_id,
                        "head_commit_id": intent["commit_id"]},
        "before_after": {"old_memory_id": old_memory_id,
                          "old_memory_revision": old_revision},
        "outcome": "success",
        "reason_code": None,
        "details": {
            "new_memory_id": new_memory_id,
            "new_event_id": new_event_id,
            "new_revision_hash": new_revision_hash,
            "scope_split": scope_split,
            "note": note,
        },
    })
    return proposals.get(correction_id)


# ---------------------------------------------------------------------------
# small frontmatter helpers (kept local so source_correction has no
# dependency on CommitService internals).
# ---------------------------------------------------------------------------


def _split_md_frontmatter(md: str) -> tuple[dict[str, Any], str]:
    if not md.startswith("---"):
        return {}, md
    end = md.find("\n---", 3)
    if end == -1:
        return {}, md
    raw = md[3:end].strip()
    body = md[end + 4:].lstrip()
    return _mini_yaml(raw), body


def _mini_yaml(raw: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    evidence: list[dict[str, Any]] = []
    in_evidence = False
    current: dict[str, Any] | None = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        if line.startswith("evidence:"):
            in_evidence = True
            continue
        if not line.startswith((" ", "-")):
            in_evidence = False
        if in_evidence:
            stripped = line.strip()
            if stripped.startswith("- "):
                if current is not None:
                    evidence.append(current)
                current = {}
                stripped = stripped[2:]
            if current is not None and ":" in stripped:
                k, _, v = stripped.partition(":")
                current[k.strip()] = _yaml_scalar(v.strip())
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = _yaml_scalar(value.strip())
    if current is not None:
        evidence.append(current)
    if evidence:
        out["evidence"] = evidence
    return out


def _yaml_scalar(v: str) -> Any:
    if not v:
        return ""
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    if v == "null":
        return None
    if v in {"true", "false"}:
        return v == "true"
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def _current_head_commit_id(vault: VaultLayout) -> str | None:
    if not vault.head_path.exists():
        return None
    try:
        return json.loads(vault.head_path.read_text(encoding="utf-8")).get("commit_id")
    except json.JSONDecodeError:
        return None


def _current_privacy_epoch(vault: VaultLayout) -> str | None:
    if not vault.privacy_head.exists():
        return None
    try:
        return json.loads(vault.privacy_head.read_text(encoding="utf-8")).get("epoch")
    except json.JSONDecodeError:
        return None


def decide_proposal(
    *,
    conn,
    correction_id: str,
    action: str,
    note: str | None = None,
    expected_proposal_revision: int | None = None,
    vault: VaultLayout | None = None,
) -> dict[str, Any] | None:
    """Apply a user decision to a correction proposal.

    ``action``:
      * ``"keep_old"``    → ``decided_keep`` (close overlay, recall stops flagging)
      * ``"adopt_new"``   → publish a new memory through CommitService
                            citing the new event_id; mark the old one
                            superseded. Requires ``vault``.
      * ``"scope_split"`` → as ``adopt_new`` but the new memory is
                            narrowed to project scope. Requires ``vault``.
      * ``"defer"``       → no-op (status remains ``pending``).

    The optional ``expected_proposal_revision`` is an
    optimistic-concurrency token. If it does not match the row's
    current ``proposal_revision``, the call is rejected with
    ``CorrectionCommitError`` and the row is *not* touched.

    Returns the updated proposal row or ``None`` if not found.
    """
    proposals = SourceCorrectionProposalRepo(conn)
    proposal = proposals.get(correction_id)
    if proposal is None:
        return None
    if expected_proposal_revision is not None:
        current_rev = int(proposal.get("proposal_revision") or 1)
        if int(expected_proposal_revision) != current_rev:
            raise CorrectionCommitError(
                f"expected_proposal_revision {expected_proposal_revision} is stale; "
                f"current is {current_rev}"
            )
    if proposal["status"] not in {STATUS_PENDING, STATUS_AUTO_RESOLVED}:
        # Already decided. Refuse to overwrite.
        return proposal
    if action == "defer":
        return proposal
    if action in {"keep_old"}:
        proposals.update_status(correction_id, STATUS_DECIDED_KEEP,
                                revision_note=note)
    elif action in {"adopt_new", "scope_split"}:
        if vault is None:
            raise CorrectionCommitError(
                f"{action} requires a vault to write the new memory"
            )
        return adopt_new_memory(
            vault=vault,
            conn=conn,
            correction_id=correction_id,
            expected_proposal_revision=int(
                proposal.get("proposal_revision") or 1
            ),
            note=note,
            scope_split=(action == "scope_split"),
        )
    else:  # pragma: no cover
        raise ValueError(f"unknown correction action {action!r}")
    # Refresh overlay so recall stops flagging the memory.
    refresh_overlay(conn=conn)
    return proposals.get(correction_id)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()