"""Recall: keyword baseline + opt-in BM25 over current HEAD snapshot.

v1 recall is the explicit recall path. It:

  1. Builds a ``QueryPlan`` (original query, hard filters, exact
     identifiers, negations, optional rewrite + up to 2 expansions).
  2. Filters memory projections by project / domain *before* the BM25
     corpus is built.
  3. If ``retrieval="bm25"``, scores each (filtered) memory with BM25
     per accepted variant and combines via reciprocal rank fusion where
     the **original** variant carries weight 2 and the sum of every
     rewrite/expansion weight is at most 1. If ``retrieval="keyword"``,
     only the keyword pass runs.
  4. Verifies every surfaced memory's evidence against the immutable
     segment; a memory without verifiable source evidence is omitted
     with a warning.
  5. Persists a query trace artifact under
     ``<vault>/artifacts/queries/<trace_id>.json`` and writes one
     ``query.retrieved`` audit row when ``audit=True`` on the request.
  6. Supports an opaque cursor bound to (fingerprint, snapshot_id);
     a cursor whose fingerprint / snapshot no longer matches the
     current HEAD is rejected with ``stale_cursor_rejected=True``.

BM25 is **never** imported from scan, batch, reconcile, or maintain.
That invariant is enforced by ``tests/unit/test_bm25_isolation.py``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from memory_agent.application.search.bm25 import BM25Index, reciprocal_rank_fusion, tokenize
from memory_agent.application.search.planner import (
    PlannerOutcome,
    fingerprint_for,
    plan_query,
)
from memory_agent.application.search.rewrite_provider import (
    IdentityRewriteProvider,
    RewriteProvider,
)
from memory_agent.contracts.query import (
    QueryTrace,
    QueryVariant,
    RecallItem,
    RecallResult,
    RetrievalKind,
)
from memory_agent.domain.query_policy import (
    RecallConstraintError,
    enforce_recall_constraints,
    extract_exact_identifiers,
    extract_negations,
    validate_plan_filters,
)
from memory_agent.infrastructure.files import VaultLayout
from memory_agent.infrastructure.sqlite.repositories import (
    MemoryCorrectionOverlayRepo,
)
from memory_agent.application.privacy import PrivacyService

# A resolver turns (event_id, revision_hash) into the immutable source
# record. Returning None means "not resolvable" and the item is dropped.
SourceResolver = Callable[[str, str], "dict[str, Any] | None"]


@dataclass
class SegmentSourceResolver:
    """Resolve source events by scanning the vault's immutable segments.

    Segment files are the canonical store for event text and locators,
    so this needs nothing but the vault. Matching on *both*
    ``event_id`` and ``revision_hash`` means a replayed or superseded
    event can never be cited against the wrong text.
    """

    vault: VaultLayout
    _cache: dict[tuple[str, str], dict[str, Any] | None] | None = None

    def __post_init__(self) -> None:
        self._cache = {}

    def __call__(self, event_id: str, revision_hash: str) -> dict[str, Any] | None:
        key = (event_id, revision_hash)
        assert self._cache is not None
        if key in self._cache:
            return self._cache[key]
        found = self._scan(event_id, revision_hash)
        self._cache[key] = found
        return found

    def _scan(self, event_id: str, revision_hash: str) -> dict[str, Any] | None:
        segments_dir = self.vault.segments_dir
        if not segments_dir.exists():
            return None
        for seg_path in sorted(segments_dir.glob("*.json")):
            try:
                payload = json.loads(seg_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            for ev in payload.get("events", []):
                if (ev.get("event_id") == event_id
                        and ev.get("revision_hash") == revision_hash):
                    loc = (ev.get("source", {}) or {}).get("locator", {}) or {}
                    return {
                        "text": ev.get("text", ""),
                        "locator": {
                            "byte_start": int(loc.get("byte_start", 0)),
                            "byte_end": int(loc.get("byte_end", 0)),
                            "record_ordinal": int(loc.get("record_ordinal", 1)),
                        },
                    }
        return None


# Rank-fusion weights: original dominates everything combined.
_WEIGHT_ORIGINAL = 2.0
_WEIGHT_REWRITE_OR_EXPANSION_TOTAL_MAX = 1.0
_WEIGHT_KEYWORD_BONUS = 1.0


def expansion_marker_list(expand: str) -> list[str]:
    """Return a non-empty list when expansions are requested, else [].

    The planner only needs to know *whether* to ask the provider for
    expansions; it ignores the contents. Returning ``["*"]`` keeps the
    type as ``list[str]`` while signalling "yes, please".
    """
    return ["*"] if expand == "auto" else []


@dataclass
class RecallService:
    vault: VaultLayout
    memory_projection_repo: Any
    include_archived: bool = False
    source_resolver: SourceResolver | None = None
    rewrite_provider: RewriteProvider | None = None
    audit_conn: sqlite3.Connection | None = None
    trace_artifact_root: Path | None = None
    # Injected by the caller when the recall path needs to consult
    # the source-correction overlay. Optional: when unset, the
    # service builds a fresh repository from ``audit_conn`` (or
    # returns no warnings if neither is available).
    overlay_repo: MemoryCorrectionOverlayRepo | None = None

    def _overlay_repo(self) -> MemoryCorrectionOverlayRepo | None:
        if self.overlay_repo is not None:
            return self.overlay_repo
        if self.audit_conn is None:
            return None
        return MemoryCorrectionOverlayRepo(self.audit_conn)

    def _collect_correction_warnings(
        self, overlay_repo: MemoryCorrectionOverlayRepo | None,
        rows: list[dict[str, Any]],
    ) -> list[str]:
        if overlay_repo is None:
            return []
        warnings: list[str] = []
        for row in rows:
            mem_id = str(row.get("memory_id") or "")
            rev = int(row.get("revision") or 0)
            if not mem_id or not rev:
                continue
            overlay = overlay_repo.get(mem_id, rev)
            if overlay is None:
                continue
            state = str(overlay.get("state") or "")
            if state == "pending_correction":
                warnings.append(
                    f"pending_correction:{mem_id}@r{rev}"
                )
        return warnings

    def recall(
        self,
        *,
        query: str,
        project_ids,
        domain_ids,
        include_archived: bool,
        limit: int,
        retrieval: RetrievalKind = "keyword",
        rewrite: str = "off",
        expand: str = "off",
        cursor: str | None = None,
        retrieval_limit: int = 30,
        audit: bool = True,
    ) -> dict[str, Any]:
        retrieval = retrieval or "keyword"
        if retrieval not in {"keyword", "bm25"}:
            raise RecallConstraintError(f"unknown retrieval={retrieval!r}")
        if rewrite not in {"off", "auto"}:
            raise RecallConstraintError(f"unknown rewrite={rewrite!r}")
        if expand not in {"off", "auto"}:
            raise RecallConstraintError(f"unknown expand={expand!r}")
        _ = include_archived  # accepted for forward-compat; v0.1 never returned archived

        original_query = query
        try:
            enforce_recall_constraints(
                original_query=original_query,
                original_domains=list(domain_ids),
                original_projects=list(project_ids),
            )
        except RecallConstraintError:
            raise

        head_commit = self._read_head_commit()
        if head_commit is None:
            return self._empty_result(query, retrieval=retrieval, reason="no_active_memory")

        # ------------------------------------------------------------------
        # 1. Stale cursor rejection — must happen before any retrieval.
        # ------------------------------------------------------------------
        stale = False
        cursor_plan_fingerprint: str | None = None
        if cursor:
            parsed = _decode_cursor(cursor)
            if parsed is None:
                stale = True
            else:
                cursor_snapshot, cursor_plan_fingerprint = parsed
                if cursor_snapshot != head_commit:
                    stale = True
                # Plan fingerprint is re-checked after planning below.

        # ------------------------------------------------------------------
        # 2. Build the plan. Filters + provider are *only* applied to the
        #    original query / hard filters — never to rewrite/expansion text.
        # ------------------------------------------------------------------
        hard_filters = {
            "project_ids": sorted(set(str(p) for p in project_ids)),
            "domain_ids": sorted(set(str(d) for d in domain_ids)),
        }
        provider = self.rewrite_provider
        # BM25 still uses the planner so its contract is consistent;
        # for keyword-only requests we can skip rewrite/expansion.
        if retrieval == "keyword" and rewrite == "off" and expand == "off":
            outcome = plan_query(
                original_query=original_query,
                hard_filters=hard_filters,
                rewrite="off",
                expansions=[],
                provider=None,
            )
        else:
            outcome = plan_query(
                original_query=original_query,
                hard_filters=hard_filters,
                rewrite=rewrite,
                expansions=expansion_marker_list(expand),
                provider=provider or IdentityRewriteProvider(),
            )

        # Hard-filter validation on every accepted non-original variant.
        negations = extract_negations(original_query)
        accepted_variants = list(outcome.plan.variants)
        safe_variants: list[QueryVariant] = []
        rejection_log: list[QueryVariant] = list(outcome.rejected)
        for v in accepted_variants:
            if v.origin == "original":
                safe_variants.append(v)
                continue
            try:
                validate_plan_filters(
                    plan_text=v.text,
                    original_query=original_query,
                    original_projects=hard_filters["project_ids"],
                    original_domains=hard_filters["domain_ids"],
                    negations=negations,
                )
                safe_variants.append(v)
            except RecallConstraintError as exc:
                rejection_log.append(QueryVariant(
                    variant_id=v.variant_id,
                    origin=v.origin,
                    text=v.text,
                    accepted=False,
                    reason=str(exc),
                    provider=v.provider,
                    prompt_version=v.prompt_version,
                ))

        plan_fingerprint = fingerprint_for(
            _plan_with_safe_variants(outcome.plan, safe_variants)
        )
        if cursor and not stale and cursor_plan_fingerprint is not None:
            if cursor_plan_fingerprint != plan_fingerprint:
                stale = True
        if stale:
            return _stale_cursor_response(query, retrieval=retrieval)

        # ------------------------------------------------------------------
        # 3. Apply scope/project/domain filters BEFORE building the BM25
        #    corpus. The keyword-only path uses the same pre-filtered rows.
        # ------------------------------------------------------------------
        rows = self.memory_projection_repo.list_active()
        if include_archived:
            rows.extend(self.memory_projection_repo.list_archived())

        # ------------------------------------------------------------------
        # 3a. Privacy deletion barrier (P9 / T17). Read the privacy
        #     HEAD at the *start* of the recall and filter any memory
        #     that is the target of a forget operation. The same HEAD
        #     is re-read just before returning; if a forget landed
        #     between the two reads, the new ``erased_ids`` set is
        #     used to drop the freshly-deleted memory from the
        #     response as well. A memory whose ``memory_id`` is in
        #     the manifest is filtered *even if* the DB projection row
        #     is still live — the manifest is the authority.
        #
        #     The privacy filter runs *before* the scope/project/domain
        #     filter so a forgotten memory is never resurfaced even
        #     when its DB projection is otherwise valid.
        # ------------------------------------------------------------------
        overlay_repo = self._overlay_repo()
        correction_warnings = self._collect_correction_warnings(
            overlay_repo, rows,
        )

        privacy_repo: PrivacyService | None = None
        privacy_applied = False
        if self.audit_conn is not None:
            privacy_repo = PrivacyService(vault=self.vault, conn=self.audit_conn)
        privacy_head_start = (
            privacy_repo.current_version() if privacy_repo else 0
        )
        if privacy_repo is not None:
            rows, privacy_applied = _filter_erased(rows, privacy_repo)
        warnings: list[str] = list(correction_warnings)
        if privacy_applied:
            warnings.append("privacy:recall_filter_applied")

        # ------------------------------------------------------------------
        # 3b. Scope / project / domain filter. Runs *after* the
        #     privacy filter so a forgotten memory does not count as
        #     an excluded scope-filtered memory.
        # ------------------------------------------------------------------
        rows, filter_warnings = _apply_filters(
            rows,
            project_ids=list(hard_filters["project_ids"]),
            domain_ids=list(hard_filters["domain_ids"]),
        )
        warnings.extend(filter_warnings)

        # ------------------------------------------------------------------
        # 3c. Source-correction overlay: a memory with a pending
        #     correction (the source it cites was rewritten and the user
        #     has not yet decided) is flagged with a warning. The
        #     overlay is updated by ``discover_affected_memories`` on
        #     every worker pass; the scan dispatcher also refreshes it
        #     inline so a recall observed *before* the worker pass
        #     still sees the overlay.
        # ------------------------------------------------------------------
        warnings.extend(self._collect_correction_warnings(
            overlay_repo, rows,
        ))

        resolver = self.source_resolver or SegmentSourceResolver(self.vault)

        # Load each memory once: file path + body + statement + scope + evidence.
        loaded: list[dict[str, Any]] = []
        for row in rows:
            mem_id = row["memory_id"]
            rev = int(row["revision"])
            mem_path = self.vault.memory_revision_path(mem_id, rev)
            if not mem_path.exists():
                warnings.append(f"missing_object:{mem_id}")
                continue
            md = mem_path.read_text(encoding="utf-8")
            evidence = _resolve_evidence(md, resolver)
            if evidence is None:
                # No verifiable source evidence: omit rather than
                # invent a quote or a locator.
                warnings.append(f"unverified_source_evidence:{mem_id}")
                continue
            loaded.append({
                "row": row,
                "memory_id": mem_id,
                "revision": rev,
                "md": md,
                "statement": _statement_of(md),
                "evidence": evidence,
            })

        # ------------------------------------------------------------------
        # 4. Score: keyword pass + (optional) BM25 pass.
        # ------------------------------------------------------------------
        keyword_pass: tuple[list[float], set[str]] = self._keyword_pass(loaded, original_query)
        per_item_keyword: list[float] = keyword_pass[0]
        bm25_score_per_variant: dict[str, list[float]] = {}
        bm25_variant_weights: dict[str, float] = {}

        if retrieval == "bm25":
            # Build one BM25 index over the *filtered* corpus.
            index = BM25Index()
            exact_ids = extract_exact_identifiers(original_query)
            for entry in loaded:
                index.add(
                    entry["md"],
                    exact_identifiers=exact_ids,
                )
            bm25_score_per_variant = self._bm25_per_variant(
                index=index,
                variants=safe_variants,
                exact_identifiers=exact_ids,
            )
            # Weights: original=2, each rewrite/expansion=0.5, total ≤ 1.
            n_var = sum(1 for v in safe_variants if v.origin != "original")
            per_var = min(0.5, _WEIGHT_REWRITE_OR_EXPANSION_TOTAL_MAX / max(1, n_var))
            for v in safe_variants:
                bm25_variant_weights[v.variant_id] = (
                    _WEIGHT_ORIGINAL if v.origin == "original" else per_var
                )

        # ------------------------------------------------------------------
        # 5. Combine scores + rank.
        # ------------------------------------------------------------------
        per_item_score: list[float] = []
        per_item_bm25: list[float] = []
        per_item_surfaced_by: list[list[str]] = []
        fused_per_item: list[float] = [0.0] * len(loaded)
        if retrieval == "bm25" and safe_variants:
            fused_per_item = reciprocal_rank_fusion(
                variant_scores={
                    v.variant_id: bm25_score_per_variant.get(v.variant_id, [0.0] * len(loaded))
                    for v in safe_variants
                },
                variant_weights=bm25_variant_weights,
            )
        for i in range(len(loaded)):
            kw = per_item_keyword[i] if i < len(per_item_keyword) else 0.0
            bm25_score = fused_per_item[i] if i < len(fused_per_item) else 0.0
            per_item_bm25.append(bm25_score)
            # keyword bonus ensures keyword hits still surface even if
            # BM25 reorders them lower.
            combined = bm25_score + _WEIGHT_KEYWORD_BONUS * (1.0 if kw > 0 else 0.0)
            per_item_score.append(combined)
            surfaced_by: list[str] = []
            if kw > 0:
                surfaced_by.append("keyword")
            if retrieval == "bm25":
                for v in safe_variants:
                    score_list = bm25_score_per_variant.get(v.variant_id, [])
                    if i < len(score_list) and score_list[i] > 0:
                        surfaced_by.append(v.variant_id)
            per_item_surfaced_by.append(surfaced_by)

        # Sort by combined score, then by memory_id for stable ties.
        order = sorted(
            range(len(loaded)),
            key=lambda i: (-per_item_score[i], loaded[i]["memory_id"]),
        )

        # ------------------------------------------------------------------
        # 6. Apply retrieval_limit before slicing the final ``limit``.
        #    required_constraints_complete is True iff every surviving
        #    item above the retrieval_limit was an explicit "required"
        #    match (keyword hit on original). For v1 we set it to True
        #    iff the caller did not pass a smaller retrieval_limit.
        # ------------------------------------------------------------------
        truncated = len(order) > retrieval_limit
        if truncated:
            kept_order = order[:retrieval_limit]
            extra = order[retrieval_limit:]
            # Required constraints are "satisfied" when at least one
            # required match (keyword on original) is in the kept set,
            # OR when there were none in the corpus.
            required_in_corpus = any(per_item_keyword[i] > 0 for i in range(len(loaded)))
            required_in_kept = any(
                per_item_keyword[i] > 0 for i in kept_order
            )
            required_constraints_complete = (
                (not required_in_corpus) or required_in_kept
            )
        else:
            kept_order = order
            extra = []
            required_constraints_complete = True

        items: list[RecallItem] = []
        for i in kept_order[:limit]:
            entry = loaded[i]
            items.append(RecallItem(
                memory_id=entry["memory_id"],
                revision=entry["revision"],
                statement=entry["statement"],
                score=float(per_item_score[i]),
                quote=entry["evidence"]["quote"],
                locator=entry["evidence"]["locator"],
                scope={"scope": entry["row"].get("scope_json")},
                surfaced_by=per_item_surfaced_by[i],
                keyword_contribution=float(per_item_keyword[i]),
                bm25_contribution=float(per_item_bm25[i]),
            ))

        # ------------------------------------------------------------------
        # 6a. Re-read privacy HEAD just before returning (P9 / T17).
        #     A forget operation that landed between the start-of-
        #     recall read and now must be reflected in the response;
        #     otherwise the caller could observe a freshly-erased
        #     memory in the returned items.
        # ------------------------------------------------------------------
        if privacy_repo is not None:
            privacy_head_end = privacy_repo.current_version()
            if privacy_head_end > privacy_head_start:
                # The HEAD advanced — re-filter and re-drop the items
                # whose memory_id was forgotten in the window.
                items = _drop_erased_items(items, privacy_repo)
                if items:
                    warnings.append("privacy:post_window_filter_applied")

        next_cursor: str | None = None
        if truncated and extra:
            next_cursor = _encode_cursor(
                snapshot_id=head_commit,
                plan_fingerprint=plan_fingerprint,
            )

        trace_id = "trace_" + hashlib.sha1(
            f"{plan_fingerprint}|{head_commit}".encode()
        ).hexdigest()[:12]

        result = RecallResult(
            schema_version=1,
            snapshot_id=head_commit,
            query_trace=QueryTrace(
                trace_id=trace_id,
                fingerprint=plan_fingerprint,
                variants=safe_variants,
                snapshot_id=head_commit,
                rejected=rejection_log,
                fallback_reason=outcome.fallback_reason,
                retrieval=retrieval,
            ),
            items=items,
            truncated=truncated,
            next_cursor=next_cursor,
            required_constraints_complete=required_constraints_complete,
            warnings=warnings,
        )

        # Persist trace artifact + audit row.
        if audit:
            self._persist_trace_artifact(
                trace_id=trace_id,
                snapshot_id=head_commit,
                plan=outcome,
                safe_variants=safe_variants,
                rejection_log=rejection_log,
                retrieval=retrieval,
                retrieval_limit=retrieval_limit,
                limit=limit,
                warnings=warnings,
            )
            self._write_audit(
                trace_id=trace_id,
                snapshot_id=head_commit,
                retrieval=retrieval,
                accepted_variants=[v.variant_id for v in safe_variants],
                rejected_variants=[v.variant_id for v in rejection_log],
                result_count=len(items),
                fallback_reason=outcome.fallback_reason,
            )

        return result.model_dump(mode="json")

    # -----------------------------------------------------------------------
    # helpers
    # -----------------------------------------------------------------------

    def _empty_result(self, query: str, *, retrieval: RetrievalKind, reason: str) -> dict[str, Any]:
        return RecallResult(
            schema_version=1,
            snapshot_id="empty",
            query_trace=QueryTrace(
                trace_id="trace_empty",
                fingerprint="empty",
                variants=[QueryVariant(variant_id="original", origin="original", text=query, accepted=True)],
                snapshot_id="empty",
                retrieval=retrieval,
            ),
            items=[],
            warnings=[reason],
        ).model_dump(mode="json")

    def _read_head_commit(self) -> str | None:
        if not self.vault.head_path.exists():
            return None
        try:
            data = json.loads(self.vault.head_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return data.get("commit_id")

    def _keyword_pass(
        self,
        loaded: list[dict[str, Any]],
        query: str,
    ) -> tuple[list[float], set[str]]:
        """Return (per-item score, set of memory_ids that hit at least one term)."""
        q = (query or "").lower().strip()
        scores: list[float] = []
        hits: set[str] = set()
        if not q:
            return [0.0] * len(loaded), hits
        terms = [t for t in q.split() if t]
        if not terms:
            return [0.0] * len(loaded), hits
        for entry in loaded:
            md_lower = entry["md"].lower()
            hit = sum(1 for t in terms if t in md_lower)
            score = hit / max(1, len(terms))
            scores.append(score)
            if hit > 0:
                hits.add(entry["memory_id"])
        return scores, hits

    def _bm25_per_variant(
        self,
        *,
        index: BM25Index,
        variants: list[QueryVariant],
        exact_identifiers: list[str],
    ) -> dict[str, list[float]]:
        """Score the BM25 corpus for every accepted variant.

        Returns a dict variant_id → per-document score list.
        """
        out: dict[str, list[float]] = {}
        for v in variants:
            tokens = tokenize(v.text, exact_identifiers=exact_identifiers)
            out[v.variant_id] = index.score(tokens)
        return out

    def _persist_trace_artifact(
        self,
        *,
        trace_id: str,
        snapshot_id: str,
        plan: PlannerOutcome,
        safe_variants: list[QueryVariant],
        rejection_log: list[QueryVariant],
        retrieval: str,
        retrieval_limit: int,
        limit: int,
        warnings: list[str],
    ) -> None:
        root = self.trace_artifact_root or (self.vault.artifacts_dir / "queries")
        root.mkdir(parents=True, exist_ok=True)
        payload = {
            "trace_id": trace_id,
            "snapshot_id": snapshot_id,
            "retrieval": retrieval,
            "retrieval_limit": retrieval_limit,
            "limit": limit,
            "original_query": plan.plan.original_query,
            "hard_filters": plan.plan.hard_filters,
            "rewrite": plan.plan.rewrite,
            "expansions": list(plan.plan.expansions),
            "variants": [v.model_dump(mode="json") for v in safe_variants],
            "rejected": [v.model_dump(mode="json") for v in rejection_log],
            "fallback_reason": plan.fallback_reason,
            "provider": plan.provider_name,
            "prompt_version": plan.prompt_version,
            "warnings": list(warnings),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        target = root / f"{trace_id}.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _write_audit(
        self,
        *,
        trace_id: str,
        snapshot_id: str,
        retrieval: str,
        accepted_variants: list[str],
        rejected_variants: list[str],
        result_count: int,
        fallback_reason: str | None,
    ) -> None:
        if self.audit_conn is None:
            return
        audit_id = "aud_" + hashlib.sha1(
            f"query.retrieved|{trace_id}".encode()
        ).hexdigest()[:12]
        row = self.audit_conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM audit_events"
        ).fetchone()
        next_seq = int(row["next"]) if row else 1
        try:
            self.audit_conn.execute(
                """INSERT INTO audit_events(
                    audit_id, seq, occurred_at, actor, action,
                    object_type, object_id, correlation_json,
                    before_after_json, outcome, reason_code, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    audit_id, next_seq,
                    datetime.now(timezone.utc).isoformat(),
                    "user_cli", "query.retrieved",
                    "query", trace_id,
                    json.dumps({"snapshot_id": snapshot_id}),
                    json.dumps({"retrieval": retrieval,
                                "accepted_variants": accepted_variants,
                                "rejected_variants": rejected_variants,
                                "result_count": result_count,
                                "fallback_reason": fallback_reason}),
                    "success" if result_count > 0 or not fallback_reason else "skipped",
                    fallback_reason,
                    json.dumps({"trace_artifact_ref": f"artifacts/queries/{trace_id}.json"}),
                ),
            )
            self.audit_conn.commit()
        except sqlite3.IntegrityError:
            # Idempotent: same trace_id → same audit_id, ignore duplicates.
            pass


# ---------------------------------------------------------------------------
# privacy filtering (P9 / T17)
# ---------------------------------------------------------------------------


def _filter_erased(
    rows: list[dict[str, Any]],
    privacy_repo: PrivacyService,
) -> tuple[list[dict[str, Any]], bool]:
    """Drop rows whose memory_id is in the privacy manifest.

    Reads the privacy HEAD at the moment of the call; consults both
    the DB mirror and the on-disk manifests so a torn write does not
    leak erased content. The caller is responsible for the second
    read just before return.

    Returns ``(filtered_rows, applied)`` where ``applied`` is True
    iff at least one row was dropped.
    """
    if not rows:
        return rows, False
    out: list[dict[str, Any]] = []
    dropped = 0
    for row in rows:
        mem_id = str(row.get("memory_id") or "")
        if mem_id and privacy_repo.is_target_blocked(memory_id=mem_id):
            dropped += 1
            continue
        out.append(row)
    return out, dropped > 0


def _drop_erased_items(
    items: list, privacy_repo: PrivacyService,
) -> list:
    out = []
    for item in items:
        mem_id = getattr(item, "memory_id", None)
        if mem_id and privacy_repo.is_target_blocked(memory_id=str(mem_id)):
            continue
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# cursor codec
# ---------------------------------------------------------------------------


def _encode_cursor(*, snapshot_id: str, plan_fingerprint: str) -> str:
    raw = json.dumps({"snapshot_id": snapshot_id, "fp": plan_fingerprint}, sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str] | None:
    try:
        pad = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + pad).decode()
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            return None
        snap = obj.get("snapshot_id")
        fp = obj.get("fp")
        if not isinstance(snap, str) or not isinstance(fp, str):
            return None
        return snap, fp
    except (ValueError, json.JSONDecodeError):
        return None


def _stale_cursor_response(query: str, *, retrieval: RetrievalKind) -> dict[str, Any]:
    return RecallResult(
        schema_version=1,
        snapshot_id="stale",
        query_trace=QueryTrace(
            trace_id="trace_stale",
            fingerprint="stale",
            variants=[QueryVariant(variant_id="original", origin="original", text=query, accepted=True)],
            snapshot_id="stale",
            retrieval=retrieval,
            fallback_reason="stale_cursor_rejected",
        ),
        items=[],
        warnings=["stale_cursor_rejected"],
        stale_cursor_rejected=True,
    ).model_dump(mode="json")


def _plan_with_safe_variants(plan, safe_variants):
    """Return a new QueryPlan whose variants are the safe ones."""
    from memory_agent.contracts.query import QueryPlan as _QP
    return _QP(
        original_query=plan.original_query,
        hard_filters=plan.hard_filters,
        exact_identifiers=plan.exact_identifiers,
        negations=plan.negations,
        rewrite=plan.rewrite,
        expansions=plan.expansions,
        variants=list(safe_variants),
    )


def recall_keyword(
    *,
    vault: VaultLayout,
    memory_projection_repo,
    query: str,
    project_ids: list[str],
    domain_ids: list[str],
    limit: int = 8,
    source_resolver: SourceResolver | None = None,
) -> dict[str, Any]:
    """v0.1 keyword-only entry point. Kept for backward compatibility."""
    svc = RecallService(
        vault=vault,
        memory_projection_repo=memory_projection_repo,
        source_resolver=source_resolver,
    )
    return svc.recall(
        query=query,
        project_ids=project_ids,
        domain_ids=domain_ids,
        include_archived=False,
        limit=limit,
        retrieval="keyword",
        rewrite="off",
        expand="off",
    )


# ---------------------------------------------------------------------------
# projection filtering
# ---------------------------------------------------------------------------


def _row_scope(row: dict[str, Any]) -> tuple[str | None, set[str], set[str]]:
    """Return (level, project_ids, domains) from a projection row.

    ``level`` is ``None`` when ``scope_json`` is missing or its ``level``
    field is not a recognised value (``global`` / ``domain`` / ``project``
    / ``task`` / ``session``). Callers must not widen ``None`` to
    ``global`` — a corrupt or unparseable projection is excluded with a
    warning, never silently broadened.
    """
    scope_obj = _load_json(row.get("scope_json"), default={}) or {}
    raw_level = scope_obj.get("level")
    project_ids = {
        str(x) for x in (_load_json(row.get("project_ids_json"), default=[]) or [])
    }
    domains = {
        str(x) for x in (_load_json(row.get("domains_json"), default=[]) or [])
    }
    # scope_json is authoritative; the denormalised columns back it up.
    project_ids |= {str(x) for x in (scope_obj.get("project_ids") or [])}
    domains |= {str(x) for x in (scope_obj.get("domain_ids") or [])}
    if not isinstance(raw_level, str) or raw_level not in {
        "global", "domain", "project", "task", "session",
    }:
        return None, project_ids, domains
    return raw_level, project_ids, domains


def _apply_filters(
    rows: list[dict[str, Any]],
    *,
    project_ids: list[str],
    domain_ids: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Filter active rows by project / domain.

    Policy (matches docs/04 §"召回" / docs/14 §"recall filter 语义"):

      * Unscoped query (``project_ids`` and ``domain_ids`` empty):
        default to global + domain rules. Project / task / session scoped
        rows are *hidden* until the caller names a matching facet —
        cross-project / cross-session browsing must be explicit.
      * Global rows survive every filter combination.
      * Both explicit facets are applied to the same memory: a project
        row with an explicit ``domain_ids`` tag is only returned when
        the caller names that project *and* a domain that intersects.
        A row whose explicit facet is not satisfied by the query is
        excluded (conservative: explicit facets are never loosened).
      * Domain scope carries no project meaning: a domain row with no
        project tag is returned when the caller's domain filter
        matches, regardless of the project filter.
      * Task / session rows cannot answer "which project / domain",
        so they are excluded under any project / domain filter.
      * Rows whose ``scope_json.level`` is missing or unknown are
        excluded with a warning. An unknown projection is never
        widened to global.
    """
    wanted_projects = set(project_ids)
    wanted_domains = set(domain_ids)
    kept: list[dict[str, Any]] = []
    excluded: list[str] = []
    invalid: list[str] = []

    for row in rows:
        level, row_projects, row_domains = _row_scope(row)

        if level is None:
            # Unknown / corrupt projection: never widen to global.
            invalid.append(str(row["memory_id"]))
            continue

        # Unscoped query: keep global + domain; hide the rest.
        if not wanted_projects and not wanted_domains:
            if level in ("global", "domain"):
                kept.append(row)
            else:
                excluded.append(str(row["memory_id"]))
            continue

        # Global survives any filter combination.
        if level == "global":
            kept.append(row)
            continue

        ok = True

        # Project facet (only meaningful for project-scoped rows).
        if wanted_projects:
            if level == "project":
                if not (row_projects & wanted_projects):
                    ok = False
            elif level == "domain":
                # Domain scope carries no project meaning — a domain
                # rule applies across all projects, so a project filter
                # does not exclude it.
                pass
            else:
                # task / session cannot answer "which project?".
                ok = False

        # Domain facet: applies to domain-scoped rows and to project
        # rows that carry an explicit domain tag. If a row has an
        # explicit domain tag, the caller's domain filter must
        # intersect it. If the caller named no domain filter but the
        # row has an explicit domain tag, the row's domain constraint
        # is unsatisfied and the row is excluded (explicit facets are
        # never loosened).
        if ok and wanted_domains:
            if level == "domain":
                if not (row_domains & wanted_domains):
                    ok = False
            elif level == "project":
                if not row_domains or not (row_domains & wanted_domains):
                    ok = False
            else:
                # task / session cannot answer "which domain?".
                ok = False
        elif ok and level == "project" and row_domains:
            # Conservative: explicit domain tag on a project row is
            # an unsatisfied facet when the caller did not ask for
            # any domain. Excluded rather than silently broadened.
            ok = False

        if ok:
            kept.append(row)
        else:
            excluded.append(str(row["memory_id"]))

    warnings: list[str] = []
    if excluded:
        warnings.append(
            "scope_filtered_out:" + ",".join(sorted(excluded))
        )
    if invalid:
        warnings.append(
            "invalid_scope_level:" + ",".join(sorted(invalid))
        )
    return kept, warnings


def _load_json(raw: Any, *, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# memory object parsing
# ---------------------------------------------------------------------------


def _split_frontmatter(md: str) -> tuple[dict[str, Any], str]:
    if not md.startswith("---"):
        return {}, md
    end = md.find("\n---", 3)
    if end == -1:
        return {}, md
    raw = md[3:end]
    body = md[end + 4:].lstrip()
    try:
        import yaml  # type: ignore[import-not-found]
        data = yaml.safe_load(raw)
    except Exception:  # noqa: BLE001 — fall back to the body-only read
        data = None
    if not isinstance(data, dict):
        data = _mini_frontmatter(raw)
    return dict(data), body


def _mini_frontmatter(raw: str) -> dict[str, Any]:
    """Parse the ``key: value`` / ``- item`` subset CommitService emits.

    Evidence lists are nested one level under ``evidence:``; we only
    need ``source_event_id``, ``source_revision_hash``, ``quote`` and
    the span bounds, so a flat scan of that block is enough when PyYAML
    is unavailable.
    """
    out: dict[str, Any] = {}
    evidence: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_evidence = False
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
                if current:
                    evidence.append(current)
                current = {}
                stripped = stripped[2:]
            if current is not None and ":" in stripped:
                k, _, v = stripped.partition(":")
                current[k.strip()] = _yaml_scalar_value(v.strip())
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = _yaml_scalar_value(value.strip())
    if current:
        evidence.append(current)
    if evidence:
        out["evidence"] = evidence
    return out


def _yaml_scalar_value(v: str) -> Any:
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


def _statement_of(md: str) -> str:
    fm, body = _split_frontmatter(md)
    _ = fm
    return body.splitlines()[0] if body.strip() else ""


def _evidence_refs(md: str) -> list[dict[str, Any]]:
    fm, body = _split_frontmatter(md)
    _ = body
    raw = fm.get("evidence")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict)]


def _resolve_evidence(
    md: str, resolver: SourceResolver
) -> dict[str, Any] | None:
    """Return the first evidence ref that resolves *and* verifies.

    A ref is only usable when the persisted segment holds the exact
    (event_id, revision_hash) pair and the declared span slices the
    decoded source text to exactly the stored quote. Anything else is
    corrupt evidence, and corrupt evidence yields no item.
    """
    for ref in _evidence_refs(md):
        event_id = ref.get("source_event_id")
        revision_hash = ref.get("source_revision_hash")
        quote = ref.get("quote")
        span_start = ref.get("span_start")
        span_end = ref.get("span_end")
        if not isinstance(event_id, str) or not isinstance(revision_hash, str):
            continue
        if not isinstance(quote, str) or not quote:
            continue
        if not isinstance(span_start, int) or not isinstance(span_end, int):
            continue
        record = resolver(event_id, revision_hash)
        if record is None:
            continue
        text = record.get("text")
        locator = record.get("locator")
        if not isinstance(text, str) or not isinstance(locator, dict):
            continue
        if span_start < 0 or span_end > len(text) or span_start >= span_end:
            continue
        if text[span_start:span_end] != quote:
            continue
        return {"quote": quote, "locator": dict(locator)}
    return None
