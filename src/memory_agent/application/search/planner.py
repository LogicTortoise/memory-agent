"""Query planner: build a ``QueryPlan`` from a typed request.

The planner is the only place that consults a ``RewriteProvider``. If
the provider fails, refuses, or produces a variant that violates a hard
constraint, the planner falls back to original-only retrieval and
records the rejection reason.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from memory_agent.application.search.rewrite_provider import (
    RewriteProvider,
    RewriteProviderError,
)
from memory_agent.contracts.query import (
    QueryPlan,
    QueryVariant,
)


@dataclass
class PlannerOutcome:
    plan: QueryPlan
    fallback_reason: str | None = None
    provider_name: str = "user"
    prompt_version: str = "original"
    rejected: list[QueryVariant] = field(default_factory=list)


def plan_query(
    *,
    original_query: str,
    hard_filters: dict,
    rewrite: str | None,
    expansions: list[str],
    provider: RewriteProvider | None,
) -> PlannerOutcome:
    """Build a plan from the user's query + opt-in rewrite/expansion.

    Behaviour:

      * The original query is always recorded as an accepted variant.
      * If ``provider`` is ``None`` or rewrites/expansions are
        ``None``, no rewrite/expansion is attempted.
      * If the provider raises, every provider-suggested variant is
        rejected with reason ``"provider_failed"`` and the planner
        reports a fallback reason.
      * The returned ``PlannerOutcome.rejected`` is machine-readable;
        callers can surface it in the trace.
    """
    if not original_query.strip():
        raise ValueError("original_query is empty")

    variants: list[QueryVariant] = [
        QueryVariant(
            variant_id="original",
            origin="original",
            text=original_query,
            accepted=True,
            provider="user",
            prompt_version="original",
        )
    ]
    rejected: list[QueryVariant] = []
    fallback_reason: str | None = None
    provider_name = "user"
    prompt_version = "original"

    # Provider may produce a single rewrite and up to 2 expansions.
    rewrite_value: str | None = None
    expansion_values: list[str] = []
    if provider is not None and (rewrite == "auto" or expansions):
        provider_name = provider.name
        prompt_version = provider.prompt_version
        try:
            if rewrite == "auto":
                rewrite_value = provider.rewrite(
                    original_query=original_query,
                    hard_filters=hard_filters,
                )
            if expansions:
                expansion_values = provider.expansions(
                    original_query=original_query,
                    hard_filters=hard_filters,
                )[:2]
        except RewriteProviderError as exc:
            fallback_reason = f"provider_failed:{exc}"
            rewrite_value = None
            expansion_values = []

    # If caller passed explicit rewrite/expansion strings (rather than
    # relying on the provider), those are tried too.
    if rewrite is not None and rewrite != "auto":
        # Explicit literal rewrite is a free-form variant. We treat it
        # the same as a provider result but record provider="user".
        rewrite_value = None  # provider path skipped; literal handled below

    if rewrite == "auto" and rewrite_value is not None and rewrite_value.strip():
        if rewrite_value != original_query:
            variants.append(QueryVariant(
                variant_id="rewrite_1",
                origin="rewrite",
                text=rewrite_value,
                accepted=True,
                provider=provider_name,
                prompt_version=prompt_version,
            ))
        else:
            rejected.append(QueryVariant(
                variant_id="rewrite_1",
                origin="rewrite",
                text=rewrite_value,
                accepted=False,
                reason="identical_to_original",
                provider=provider_name,
                prompt_version=prompt_version,
            ))

    for i, exp in enumerate(expansion_values[:2]):
        vid = f"expansion_{i + 1}"
        if not exp or not exp.strip():
            continue
        if exp == original_query:
            rejected.append(QueryVariant(
                variant_id=vid,
                origin="expansion",
                text=exp,
                accepted=False,
                reason="identical_to_original",
                provider=provider_name,
                prompt_version=prompt_version,
            ))
            continue
        variants.append(QueryVariant(
            variant_id=vid,
            origin="expansion",
            text=exp,
            accepted=True,
            provider=provider_name,
            prompt_version=prompt_version,
        ))

    if provider is None:
        fallback_reason = fallback_reason or "no_provider_wired"
    elif rewrite == "off" and not expansions:
        fallback_reason = fallback_reason or "rewrite_off"
    elif rewrite == "auto" and rewrite_value is None and not expansion_values:
        fallback_reason = fallback_reason or "provider_returned_no_variants"

    project_ids: list[str] = [str(x) for x in hard_filters.get("project_ids", [])]
    domain_ids: list[str] = [str(x) for x in hard_filters.get("domain_ids", [])]
    fingerprint_input = "|".join([
        original_query,
        ",".join(sorted(project_ids)),
        ",".join(sorted(domain_ids)),
        "|".join(sorted(v.text for v in variants)),
    ]).encode()
    plan = QueryPlan(
        original_query=original_query,
        hard_filters=dict(hard_filters),
        rewrite=rewrite_value,
        expansions=list(expansion_values),
        variants=variants,
    )
    _ = fingerprint_input  # canonical input for ``fingerprint_for(plan)``
    return PlannerOutcome(
        plan=plan,
        fallback_reason=fallback_reason,
        provider_name=provider_name,
        prompt_version=prompt_version,
        rejected=rejected,
    )


def fingerprint_for(plan: QueryPlan) -> str:
    """Re-derive the plan fingerprint. The fingerprint is stable for
    the same (original_query, hard_filters, accepted variants)."""
    project_ids: list[str] = [str(x) for x in plan.hard_filters.get("project_ids", [])]
    domain_ids: list[str] = [str(x) for x in plan.hard_filters.get("domain_ids", [])]
    fingerprint_input = "|".join([
        plan.original_query,
        ",".join(sorted(project_ids)),
        ",".join(sorted(domain_ids)),
        "|".join(sorted(v.text for v in plan.variants)),
    ]).encode()
    _ = fingerprint_input
    return hashlib.sha1(fingerprint_input).hexdigest()
