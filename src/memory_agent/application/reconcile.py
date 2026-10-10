"""Reconciliation: convert validated candidate sets into
publish/pending/no-memory decisions against existing memory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from memory_agent.contracts.candidates import Candidate, CandidateSet, Scope
from memory_agent.contracts.memory import (
    MemoryAbstraction,
    MemoryAtom,
    MemoryHorizon,
    MemoryKind,
    MemoryStatus,
    Modality,
)
from memory_agent.domain.scope import scopes_intersect


@dataclass
class ReconcilerOutcome:
    to_publish: list[MemoryAtom] = field(default_factory=list)
    pending: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    no_memory: bool = False


class Reconciler:
    """Conservative reconciler.

    Decisions for a single candidate ``c``:
      * ``kind == out_of_scope`` -> rejected.
      * empty statement -> rejected.
      * statement duplicates an existing active memory verbatim ->
        rejected (duplicate).
      * candidate's scope intersects an existing active memory AND
        the candidate's statement is materially different -> pending
        proposal (cross-batch contradiction).
      * otherwise -> publish as a new active memory.

    In addition, candidates *within the same batch* are compared to
    each other. Candidates whose scopes overlap and whose statements
    are materially different form a contradiction group; **every**
    member of such a group goes to ``pending`` with
    ``reason="same_batch_contradiction"`` and *none* of them is
    published. Picking a winner by source order would publish an
    arbitrary side of instructions the user gave in the same breath;
    the whole group needs a human decision.

    This is *not* a full semantic conflict detector. It catches
    obvious cases; anything subtle stays pending.
    """

    def __init__(self, *, memory_projection_repo):
        self.memories = memory_projection_repo

    def reconcile(
        self,
        candidate_set: CandidateSet,
        *,
        evidence_lookup,
    ) -> ReconcilerOutcome:
        if candidate_set.no_memory or not candidate_set.candidates:
            return ReconcilerOutcome(no_memory=True)

        existing = self.memories.list_active()
        outcome = ReconcilerOutcome()

        # First pass: classify each candidate against pre-existing
        # memory (cross-batch contradiction). Rejected/out-of-scope
        # candidates are not admitted to the publish set.
        eligible: list[Candidate] = []
        for cand in candidate_set.candidates:
            if cand.kind == "out_of_scope":
                outcome.rejected.append({
                    "candidate_id": cand.candidate_id,
                    "reason": "out_of_scope",
                })
                continue
            if not cand.statement.strip():
                outcome.rejected.append({
                    "candidate_id": cand.candidate_id,
                    "reason": "empty_statement",
                })
                continue
            intersect_with = self._find_intersecting(cand, existing)
            if intersect_with:
                identical = any(
                    _norm(m["statement"]) == _norm(cand.statement)
                    for m in intersect_with
                )
                if identical:
                    outcome.rejected.append({
                        "candidate_id": cand.candidate_id,
                        "reason": "duplicate_active_memory",
                    })
                    continue
                outcome.pending.append({
                    "candidate_id": cand.candidate_id,
                    "candidate": cand.model_dump(mode="json"),
                    "intersects": [m["memory_id"] for m in intersect_with],
                    "reason": "cross_batch_overlap",
                })
                continue
            eligible.append(cand)

        # Second pass: compare candidates against each other within
        # the same batch. Candidates whose scopes overlap and whose
        # statements are materially different are contradictions. We
        # group them into connected components: **every** member of a
        # component of size > 1 goes to pending. Admitting the
        # first-by-source-order would publish an arbitrary winner for
        # instructions the user gave in the same breath; both sides
        # need a human decision instead.
        components = _contradiction_components(eligible)
        admitted: list[Candidate] = []
        for component in components:
            if len(component) == 1:
                admitted.append(component[0])
                continue
            for cand in component:
                outcome.pending.append({
                    "candidate_id": cand.candidate_id,
                    "candidate": cand.model_dump(mode="json"),
                    "intersects": sorted(
                        o.candidate_id for o in component if o is not cand
                    ),
                    "reason": "same_batch_contradiction",
                })

        for cand in admitted:
            outcome.to_publish.append(self._to_memory(cand))
        return outcome

    def _find_intersecting(
        self, cand: Candidate, existing: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        hits: list[dict[str, Any]] = []
        for m in existing:
            if scopes_intersect(cand.scope, _scope_of(m)):
                hits.append(m)
        return hits

    def _to_memory(self, cand: Candidate) -> MemoryAtom:
        now = datetime.now(timezone.utc).isoformat()
        kind_map = {
            "requirement": "requirement",
            "preference": "preference",
            "self_fact": "self_fact",
        }
        return MemoryAtom(
            schema_version=1,
            memory_id="mem_" + cand.candidate_id.replace("cand_", ""),
            revision=1,
            status="candidate",
            kind=kind_map.get(cand.kind, "requirement"),
            abstraction=cand.abstraction,
            statement=cand.statement,
            subject=cand.scope.project_ids[0] if cand.scope.project_ids else "global",
            predicate=None,
            value=None,
            modality=cand.modality,
            scope=cand.scope,
            primary_domain=cand.primary_domain,
            domains=cand.scope.domain_ids,
            tags=list(cand.tags),
            horizon="unknown",
            valid_from=None,
            valid_until=None,
            asserted_at=now,
            recorded_at=now,
            evidence=list(cand.evidence),
        )


def _scope_of(row: dict[str, Any]) -> Scope:
    """Rebuild the committed ``Scope`` from a ``memory_projection`` row.

    The projection stores the scope as ``scope_json`` plus denormalised
    ``project_ids_json`` / ``domains_json``. Reading those (rather than
    defaulting to ``global``) is what lets cross-batch conflict
    detection see a project-scoped memory as project-scoped instead of
    silently colliding with every other memory.
    """
    scope_obj = _load_json(row.get("scope_json"), default={}) or {}
    level = scope_obj.get("level") or "global"
    project_ids = _load_json(row.get("project_ids_json"), default=[]) or []
    domain_ids = _load_json(row.get("domains_json"), default=[]) or []
    task_ids = list(scope_obj.get("task_ids") or [])
    if level not in {"global", "domain", "project", "task", "session"}:
        level = "global"
    return Scope(
        level=level,  # type: ignore[arg-type]
        project_ids=[str(x) for x in project_ids],
        domain_ids=[str(x) for x in domain_ids],
        task_ids=[str(x) for x in task_ids],
        conditions=list(scope_obj.get("conditions") or []),
        excludes=list(scope_obj.get("excludes") or []),
    )


def _load_json(raw: Any, *, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _contradiction_components(
    eligible: list[Candidate],
) -> list[list[Candidate]]:
    """Group same-batch candidates into contradiction components.

    Two candidates are linked when their scopes intersect and their
    statements are materially different. Groups are the connected
    components of that relation, so a chain of three mutually
    contradictory instructions produces one group of three rather than
    an arbitrary winner plus two demotions.
    """
    parent = list(range(len(eligible)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    for i in range(len(eligible)):
        for j in range(i + 1, len(eligible)):
            if _is_same_batch_contradiction(eligible[i], eligible[j]):
                union(i, j)

    groups: dict[int, list[Candidate]] = {}
    for idx, cand in enumerate(eligible):
        groups.setdefault(find(idx), []).append(cand)
    # Preserve source order of first appearance for stable output.
    return list(groups.values())


def _is_same_batch_contradiction(a: Candidate, b: Candidate) -> bool:
    """Two candidates in the same batch are contradictions when their
    scopes overlap and their statements are materially different.

    Identical statements are duplicates (already filtered upstream).
    Different kinds (e.g. requirement vs preference) can still be
    contradictions if their statements disagree.
    """
    if not scopes_intersect(a.scope, b.scope):
        return False
    return _norm(a.statement) != _norm(b.statement)


def _norm(s: str) -> str:
    return " ".join(s.split()).strip().lower()