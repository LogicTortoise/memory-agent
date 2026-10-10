"""Batch processor: extract, validate, reconcile, publish.

Pipeline per batch:
  1. claim batch (write lease_token)
  2. build the provider task with EVERY active target's text and
     locator. Each target is its own work unit.
  3. call model.run_extract(...); fail closed on non-ok status
  4. validate JSON, evidence, scope, role/origin, span vs source text
     AND verify every evidence cites a target event from this batch.
  5. reconcile against prior memories
  6. commit (publish via MemoryCommitPort) OR mark no_memory OR pending
  7. finalize batch (write status, clear lease)

v0.1 fail-closed behaviour:

  * Empty ``evidence_lookup`` is *not* a free pass — the candidate's
    evidence must reference a real source event row, and that row's
    source text must contain the quoted span verbatim. If the source
    event row is missing or the span doesn't match, the batch is
    retried and, after exhausting attempts, marked ``failed``. We do
    not return ``ok`` for an evidence mismatch.

  * Every member is bound to its own work_unit -> event_id. We never
    share one event's text across multiple target members in a batch.
    The provider receives the right text for each member.

  * Evidence must cite one of the batch's *active target* events
    (every user requirement in the batch is its own target). Evidence
    pointing at a context_only event is rejected; this is the contract
    that prevents a batch from silently losing user requirements.

  * Provider ``status`` other than ``ok`` (timeout, auth_error,
    malformed, provider_unavailable) is treated as retryable until the
    attempt budget is exhausted, then ``failed``. We never lower the
    status to ``completed`` with an empty candidate set as a way to
    paper over a real error.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory_agent.application.ports import MemoryCommitPort, ModelProviderPort
from memory_agent.application.reconcile import Reconciler, ReconcilerOutcome
from memory_agent.contracts.candidates import (
    Candidate,
    CandidateSet,
    EvidenceRef,
    Scope,
)
from memory_agent.domain.evidence import (
    EvidenceValidationError,
    assert_quote_matches_slice,
)
from memory_agent.infrastructure.files import VaultLayout, atomic_write_json
from memory_agent.infrastructure.sqlite.repositories import (
    AuditRepo,
    BatchRepo,
    CandidateSetRepo,
)


class BatchProcessingError(RuntimeError):
    """Raised when a batch terminally fails and the caller should stop."""


def process_batch(
    *,
    batch_id: str,
    vault: VaultLayout,
    conn,
    provider: ModelProviderPort,
    reconciler: Reconciler,
    committer: MemoryCommitPort,
    evidence_lookup,
    base_commit_id: str,
    privacy_epoch: str,
    max_attempts: int = 3,
    run_id: str | None = None,
) -> dict[str, Any]:
    batch = BatchRepo(conn).get(batch_id)
    if batch is None:
        raise BatchProcessingError(f"batch {batch_id} not found")

    lease_token = "lease_" + hashlib.sha1(batch_id.encode()).hexdigest()[:10]
    lease_until = _in_seconds(120)
    claimed = BatchRepo(conn).claim(batch_id, lease_token, lease_until)
    if not claimed:
        raise BatchProcessingError(f"batch {batch_id} not in claimable state")

    last_error: str | None = None
    cs_repo = CandidateSetRepo(conn)
    audit = AuditRepo(conn)
    run_id = run_id or f"run_{batch_id}"

    target_unit_ids = [m["unit_id"] for m in batch["members"]
                     if int(m["active_target"]) == 1]
    target_event_ids = {m["event_id"] for m in batch["members"]
                        if int(m["active_target"]) == 1 and m.get("event_id")}

    for attempt in range(1, max_attempts + 1):
        task = _build_task(dict(batch, _conn=conn), run_id=run_id,
                           batch_id=batch_id)
        result = provider.run_extract(task)
        status = result.get("status", "malformed")
        if status != "ok":
            last_error = status
            audit.append(_audit(
                action="batch.failed", outcome="failure", batch_id=batch_id,
                details={"attempt": attempt, "status": status,
                          "error_code": result.get("error_code")},
            ))
            continue
        candidates_payload = result.get("candidates", [])
        try:
            cs = _candidate_set_from_payload(
                payload=candidates_payload,
                batch_id=batch_id,
                prompt_version=result.get("prompt_version", "extract_v1"),
                model=result.get("model", provider.name),
                raw_payload=json.dumps(result, sort_keys=True),
            )
        except ValueError as exc:
            last_error = f"validation: {exc}"
            audit.append(_audit(
                action="batch.failed", outcome="failure", batch_id=batch_id,
                details={"attempt": attempt, "validation": str(exc)},
            ))
            continue

        # ----- Evidence validation against the *exact* persisted source event.
        try:
            target_lookup = _build_target_lookup(conn, batch)
            for cand in cs.candidates:
                if not cand.evidence:
                    raise EvidenceValidationError(
                        f"candidate {cand.candidate_id!r} has no evidence"
                    )
                for ev in cand.evidence:
                    target = target_lookup.get(ev.source_event_id)
                    if target is None:
                        # Evidence must cite one of THIS batch's
                        # active target events. Context-only or
                        # other-session events are rejected.
                        raise EvidenceValidationError(
                            f"candidate {cand.candidate_id!r} cites event "
                            f"{ev.source_event_id!r} which is not an active "
                            f"target of batch {batch_id!r} "
                            f"(targets: {sorted(target_event_ids)})"
                        )
                    # Enforce role/origin: only role=user with
                    # origin=user_session may become a memory.
                    text = evidence_lookup(ev.source_event_id, ev.source_revision_hash)
                    if not text:
                        raise EvidenceValidationError(
                            f"empty source text for event {ev.source_event_id!r}"
                        )
                    assert_quote_matches_slice(
                        quote=ev.quote,
                        source_text=text,
                        span_start=ev.span_start,
                        span_end=ev.span_end,
                        role=ev.role,
                        origin=ev.origin,
                    )
        except EvidenceValidationError as exc:
            last_error = f"evidence: {exc}"
            audit.append(_audit(
                action="batch.failed", outcome="failure", batch_id=batch_id,
                details={"attempt": attempt, "evidence_error": str(exc)},
            ))
            continue

        # Persist candidate set artifact.
        artifact_path = vault.candidates_dir / f"{cs.candidate_set_id}.json"
        atomic_write_json(artifact_path, cs.model_dump(mode="json"))
        cs_repo.insert({
            "candidate_set_id": cs.candidate_set_id,
            "batch_id": batch_id,
            "artifact_ref": str(artifact_path),
            "sha256": hashlib.sha256(json.dumps(cs.model_dump(mode="json"), sort_keys=True).encode()).hexdigest(),
            "prompt_version": cs.prompt_version,
            "model": cs.model,
            "validation_state": "validated",
        })

        # Reconcile.
        outcome: ReconcilerOutcome = reconciler.reconcile(
            cs, evidence_lookup=evidence_lookup,
        )

        if outcome.to_publish or outcome.pending or outcome.no_memory:
            # Every durable terminal decision — including pending-only
            # and no_memory — must produce a HEAD-advanced commit.
            # The manifest can carry zero memories; pending_proposals
            # carries the open conflict refs; an empty
            # (memories=[], pending_proposals=[]) manifest is the
            # "consumption receipt" for a no_memory batch.
            commit_intent = committer.prepare_commit(
                proposed_memories=outcome.to_publish,
                pending_proposals=outcome.pending,
                base_commit_id=base_commit_id,
                privacy_epoch=privacy_epoch,
                batch_ids=[batch_id],
                target_unit_ids=target_unit_ids,
            )
            committer.publish(commit_intent["commit_id"])
            committer.finalize(commit_intent["commit_id"])
            new_status = "completed"
        else:
            new_status = "completed"

        BatchRepo(conn).finalize(batch_id, new_status, lease_token)
        audit.append(_audit(
            action="batch.extracted", outcome="success", batch_id=batch_id,
            details={
                "candidates": len(cs.candidates),
                "to_publish": len(outcome.to_publish),
                "pending": len(outcome.pending),
                "rejected": len(outcome.rejected),
                "active_targets": len(target_unit_ids),
                "covered_targets": sorted({ev.source_event_id for c in cs.candidates for ev in c.evidence}),
            },
        ))
        return {
            "status": new_status,
            "candidates": len(cs.candidates),
            "to_publish": len(outcome.to_publish),
            "pending": len(outcome.pending),
            "rejected": len(outcome.rejected),
            "active_targets": len(target_unit_ids),
            "covered_targets": sorted({ev.source_event_id for c in cs.candidates for ev in c.evidence}),
            "candidate_set_id": cs.candidate_set_id,
        }

    # exhausted
    BatchRepo(conn).finalize(batch_id, "failed", lease_token)
    audit.append(_audit(
        action="batch.failed", outcome="failure", batch_id=batch_id,
        details={"attempts": max_attempts, "last_error": last_error},
    ))
    raise BatchProcessingError(
        f"batch {batch_id} failed after {max_attempts} attempts: {last_error}"
    )


def _build_target_lookup(conn, batch: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map source_event_id -> persisted event row for this batch's
    active targets. Used to validate that every piece of evidence
    refers to a real target and to fetch the exact byte/ordinal
    locator for each."""
    target_unit_ids = [m["unit_id"] for m in batch["members"] if int(m["active_target"]) == 1]
    if not target_unit_ids:
        return {}
    placeholders = ",".join("?" for _ in target_unit_ids)
    rows = conn.execute(
        f"SELECT w.unit_id, e.event_id, e.revision_hash, e.role, e.kind, "
        f"e.source_file_id, e.segment_id "
        f"FROM work_units w "
        f"JOIN source_events e ON e.event_id = w.event_id "
        f"WHERE w.unit_id IN ({placeholders})",
        target_unit_ids,
    ).fetchall()
    return {r["event_id"]: dict(r) for r in rows}


def _build_task(batch: dict[str, Any], *, run_id: str, batch_id: str) -> dict[str, Any]:
    """Enrich the batch with the source-event text *for each active
    target member individually*. We never reuse one event's text across
    members — every target gets its own row.

    The provider receives only the JSON below: no file or DB access.

    For evidence spans: we preserve any span_start / span_end the
    provider already set on a member (the fake provider computes
    these against the line text). Only when the provider hasn't set
    them do we fall back to a default ``[0, len(text))`` so the member
    payload is self-describing for the model. Crucially, the locator's
    ``byte_end`` is the byte position in the *file*, not a Unicode
    offset in the *event text*, so we never substitute one for the
    other.
    """
    conn = batch.get("_conn")
    members_with_text = []
    if conn is None:
        return {"task_type": "extract", "task_id": batch_id, "run_id": run_id,
                 "batch_id": batch_id, "members": []}

    # Resolve (event_id, role) for every unit_id in this batch in one
    # round-trip so we can build a per-target payload. This is the
    # canonical source of truth: the planner persists unit_id ->
    # event_id in work_units; the batch_members row only carries
    # (unit_id, purpose, ord, epoch, active_target).
    unit_ids = [m["unit_id"] for m in batch["members"]]
    unit_event: dict[str, str] = {}
    if unit_ids:
        placeholders = ",".join("?" for _ in unit_ids)
        rows = conn.execute(
            f"SELECT w.unit_id, w.event_id, e.role FROM work_units w "
            f"JOIN source_events e ON e.event_id = w.event_id "
            f"WHERE w.unit_id IN ({placeholders})",
            unit_ids,
        ).fetchall()
        for r in rows:
            unit_event[r["unit_id"]] = r["event_id"]

    for m in batch["members"]:
        enriched = dict(m)
        event_id = unit_event.get(m["unit_id"]) or m.get("event_id")
        if int(m["active_target"]) == 1 and event_id:
            row = conn.execute(
                """SELECT s.path AS seg_path, e.event_id, e.revision_hash, e.role,
                          e.source_time, e.discovered_at
                FROM source_events e
                JOIN segments s ON s.segment_id = e.segment_id
                WHERE e.event_id = ? LIMIT 1""",
                (event_id,),
            ).fetchone()
            if row is not None:
                text, locator = _read_event_from_segment(row, event_id)
                enriched["text"] = text
                enriched["event_id"] = row["event_id"]
                enriched["revision_hash"] = row["revision_hash"]
                enriched["role"] = row["role"]
                enriched["origin"] = (
                    "user_session" if row["role"] == "user" else "assistant_response"
                )
                # Honor the real file-byte locator so any candidate
                # evidence the provider emits can be cross-checked
                # against the persisted SourceLocator.
                enriched["source_time"] = row["source_time"] or row["discovered_at"]
                enriched["byte_start"] = locator["byte_start"]
                enriched["byte_end"] = locator["byte_end"]
                enriched["record_ordinal"] = locator["record_ordinal"]
                # Span defaults to the full event text *only* if the
                # provider didn't set one.
                if "span_start" not in enriched or enriched.get("span_start") is None:
                    enriched["span_start"] = 0
                if "span_end" not in enriched or enriched.get("span_end") is None:
                    enriched["span_end"] = len(text)
        members_with_text.append(enriched)
    return {
        "task_type": "extract",
        "task_id": batch_id,
        "run_id": run_id,
        "batch_id": batch_id,
        "members": members_with_text,
    }


def _read_event_from_segment(row: dict[str, Any], event_id: str) -> tuple[str, dict[str, Any]]:
    """Look up the exact text and SourceLocator for an event inside
    its persisted segment JSON. Falls back to ('', all zeros) when the
    segment is unreadable — the caller treats that as evidence failure.
    """
    seg_path = Path(row["seg_path"])
    if not seg_path.exists():
        return "", {"byte_start": 0, "byte_end": 0, "record_ordinal": 1}
    try:
        payload = json.loads(seg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "", {"byte_start": 0, "byte_end": 0, "record_ordinal": 1}
    for ev in payload.get("events", []):
        if ev.get("event_id") == event_id:
            text = ev.get("text", "")
            loc = (ev.get("source", {}) or {}).get("locator", {}) or {}
            return text, {
                "byte_start": int(loc.get("byte_start", 0)),
                "byte_end": int(loc.get("byte_end", len(text))),
                "record_ordinal": int(loc.get("record_ordinal", 1)),
            }
    return "", {"byte_start": 0, "byte_end": 0, "record_ordinal": 1}


def _candidate_set_from_payload(
    *,
    payload: list[dict[str, Any]],
    batch_id: str,
    prompt_version: str,
    model: str,
    raw_payload: str,
) -> CandidateSet:
    """Build a ``CandidateSet`` from the model output.

    Scope normalisation rules (v0.1):
      * a scope *object* with a recognised ``level`` is accepted as-is;
      * a scope that is a *bare string* like ``"global"`` is normalised
        to ``{"level": "global"}`` only when the string is an exact
        match for one of the canonical ``ScopeLevel`` values
        (``global``, ``domain``, ``project``, ``task``, ``session``).
        Free-form text is *rejected* — we never invent a project or
        domain scope from a string we cannot recognise, because that
        would silently broaden a memory's reach;
      * anything else (lists, dicts without ``level``, unsupported
        level strings, etc.) is rejected with ``ValueError`` so the
        batch is marked failed rather than auto-promoted.

    All other fields keep their strict validation: ``Statement``
    must be non-empty, ``EvidenceRef`` must carry a real span that
    matches the source slice, ``role=assistant`` evidence is refused.
    """
    cands: list[Candidate] = []
    for i, item in enumerate(payload):
        cand_id = item.get("candidate_id") or f"cand_{batch_id}_{i:02d}"
        scope_obj = _normalise_scope(item.get("scope"))
        cand = Candidate(
            candidate_id=cand_id,
            kind=item.get("kind", "out_of_scope"),
            abstraction=item.get("abstraction", "no_opinion"),
            statement=item.get("statement") or "",
            modality=item.get("modality", "unspecified"),
            scope=scope_obj,
            primary_domain=item.get("primary_domain"),
            tags=list(item.get("tags", [])),
            evidence=[EvidenceRef(**e) for e in item.get("evidence", [])],
            uncertainty=item.get("uncertainty"),
        )
        cands.append(cand)
    no_memory = (len(cands) == 0)
    return CandidateSet(
        schema_version=1,
        candidate_set_id="cs_" + hashlib.sha1(
            f"{batch_id}|{raw_payload}".encode()
        ).hexdigest()[:12],
        batch_id=batch_id,
        candidates=cands,
        no_memory=no_memory,
        prompt_version=prompt_version,
        model=model,
        raw_payload_sha256=hashlib.sha256(raw_payload.encode()).hexdigest(),
        status="validated",
    )


_CANONICAL_SCOPE_LEVELS: frozenset[str] = frozenset(
    {"global", "domain", "project", "task", "session"}
)

# The ID facet each non-global level must populate to be sufficiently
# scoped. A level naming a facet with no IDs is *unknown*, not global:
# treating it as global would silently widen the memory's reach.
_LEVEL_REQUIRED_ID_FIELD: dict[str, str | None] = {
    "global": None,          # global must carry no IDs at all
    "project": "project_ids",
    "domain": "domain_ids",
    "task": "task_ids",
    "session": "task_ids",   # session key rides in task_ids
}


def _normalise_scope(raw: Any) -> Scope:
    """Translate a model-supplied scope into a strict ``Scope``.

    Fail-closed rules (v0.1):

      * ``None`` / absent scope is *rejected*. An unknown scope is not
        promoted to ``global`` — the model's uncertainty must not
        become the widest possible reach.
      * a bare string is accepted only for ``"global"``, which is an
        exact synonym of ``{"level": "global"}``. Every other level
        (``"project"``, ``"domain"``, ``"task"``, ``"session"``) names
        a facet with no ID, so it is rejected as under-scoped.
      * an object scope must name a canonical ``level``, must populate
        the ID facet that level requires, and must not carry IDs
        belonging to another facet.
      * anything else raises ``ValueError`` so the batch fails closed.
    """
    if isinstance(raw, Scope):
        _validate_scope_object(raw.level, raw.model_dump())
        return raw
    if raw is None:
        raise ValueError(
            "scope is missing; a memory-eligible candidate must state its "
            "scope explicitly (use {\"level\": \"global\"} for cross-project "
            "memories). An absent scope is never treated as global."
        )
    if isinstance(raw, str):
        if raw == "global":
            return Scope(level="global")
        if raw in _CANONICAL_SCOPE_LEVELS:
            raise ValueError(
                f"bare scope string {raw!r} is under-scoped: a {raw} scope "
                "must name the ID it applies to (project_ids / domain_ids / "
                "task_ids); it is not equivalent to global"
            )
        raise ValueError(
            f"unsupported scope string {raw!r}; expected 'global' or a scope "
            "object with field 'level'"
        )
    if not isinstance(raw, dict):
        raise ValueError(
            f"scope must be an object with 'level' or the string 'global', "
            f"got {type(raw).__name__}"
        )
    if "level" not in raw:
        raise ValueError(
            "scope object must include a 'level' field; got "
            f"{sorted(raw.keys())}"
        )
    level = raw["level"]
    if not (isinstance(level, str) and level in _CANONICAL_SCOPE_LEVELS):
        raise ValueError(
            f"unsupported scope.level {level!r}; expected one of "
            f"{sorted(_CANONICAL_SCOPE_LEVELS)}"
        )
    _validate_scope_object(level, raw)
    return Scope(level=level, **{
        k: v for k, v in raw.items() if k != "level"
    })  # type: ignore[arg-type]


def _validate_scope_object(level: str, raw: dict[str, Any]) -> None:
    """Reject scopes whose level and ID facets disagree.

    ``_LEVEL_REQUIRED_ID_FIELD`` maps each level to the facet it must
    populate (``None`` for ``global``, which must populate none). IDs
    that belong to a different facet than the declared level are
    inconsistent and rejected — we never guess which facet was meant.
    """
    required = _LEVEL_REQUIRED_ID_FIELD[level]
    facets = ("project_ids", "domain_ids", "task_ids")
    present = {f for f in facets if list(raw.get(f) or [])}
    if required is None:
        if present:
            raise ValueError(
                f"scope.level 'global' is inconsistent with ID facets "
                f"{sorted(present)}; a global scope must name no IDs"
            )
        return
    if required not in present:
        raise ValueError(
            f"scope.level {level!r} requires at least one id in "
            f"{required!r}; got {sorted(present) or 'no ids'} — an "
            "under-scoped memory is never widened to global"
        )
    stray = present - {required}
    if stray:
        raise ValueError(
            f"scope.level {level!r} is inconsistent with ID facets "
            f"{sorted(stray)}; only {required!r} may be populated for this "
            "level"
        )


def _in_seconds(seconds: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _audit(*, action: str, outcome: str, batch_id: str, details: dict[str, Any]) -> dict[str, Any]:
    import time as _time
    return {
        "audit_id": "aud_" + hashlib.sha1(
            f"{action}|{batch_id}|{outcome}|{_time.time_ns()}".encode()
        ).hexdigest()[:12],
        "action": action,
        "actor": "processor",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "object_type": "batch",
        "object_id": batch_id,
        "outcome": outcome,
        "details": details,
    }