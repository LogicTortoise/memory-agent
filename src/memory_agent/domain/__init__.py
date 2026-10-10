"""Domain-level rules that are pure and deterministic.

These contain no IO and no model dependency. They encode the project's
"non-negotiable" policies: how a candidate's quote is validated, how
turns are batched, how scopes intersect, and how lifecycle transitions
must look.
"""

from memory_agent.domain.batching import (
    BatchPolicyInput,
    plan_batches,
    plan_one_batch,
    is_oversize,
)
from memory_agent.domain.scope import (
    scopes_intersect,
    normalize_scope,
    scope_contains_project,
)
from memory_agent.domain.lifecycle import (
    assert_valid_status_transition,
    StatusTransitionError,
)
from memory_agent.domain.evidence import (
    assert_quote_matches_slice,
    EvidenceValidationError,
    slice_unicode,
)
from memory_agent.domain.query_policy import (
    enforce_recall_constraints,
    RecallConstraintError,
)

__all__ = [
    "BatchPolicyInput",
    "plan_batches",
    "plan_one_batch",
    "is_oversize",
    "scopes_intersect",
    "normalize_scope",
    "scope_contains_project",
    "assert_valid_status_transition",
    "StatusTransitionError",
    "assert_quote_matches_slice",
    "EvidenceValidationError",
    "slice_unicode",
    "enforce_recall_constraints",
    "RecallConstraintError",
]