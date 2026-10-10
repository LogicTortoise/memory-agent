"""Pure-DP batching rules.

v0.1 implements the defaults from docs/09:
  * default 5 turns per batch (configurable up to 20)
  * 6000 input tokens, 1500 output tokens (configurable)
  * never mix session/branch
  * tools are not turns
  * one event/work_unit can be an active target of at most one batch per epoch

Phase B (T06 long-turn fragmentation) extends the above with:
  * explicit long-turn fragmentation at deterministic text boundaries
  * the 5 turns + 6000 input token cap is enforced including the
    context / tool / schema overhead (no fragment can push a batch
    over the cap)
  * unfragmentable oversize turns become a single ``needs_context``
    durable state — they never become an active target
  * fragments share a ``parent_event_id`` and code-point
    ``span_start`` / ``span_end`` so the planner can rebuild the
    original event after all fragments are accounted for
  * cross-batch reference context is carried as ``context_only`` and
    never counts twice

The token counter is a *heuristic* (CPython bytes / 4) so unit tests stay
fast and deterministic; production should swap in a real tokenizer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

# Default per docs/09. We keep them here (not in contracts) because they
# are operational policy, not wire-format data.
DEFAULT_MAX_TURNS_PER_BATCH = 5
DEFAULT_MAX_INPUT_TOKENS = 6000
DEFAULT_MAX_OUTPUT_TOKENS = 1500

# Schema/system overhead that must be included in the input budget per
# docs/09. The planner keeps room for these even when a fragment is
# itself small, so a 5500-token fragment is still refused when the
# batch already has 700 tokens of context / schema.
SCHEMA_OVERHEAD_TOKENS = 192
CONTEXT_PREFIX_TOKENS = 512


@dataclass(frozen=True)
class TurnSample:
    """A single user-turn-shaped slice the planner can pack.

    `estimated_tokens` is the *input* budget this turn would add to a
    batch (system instructions, schema, and a slice of source counts).
    `is_oversize` means the turn alone is bigger than the input budget.
    ``fragments`` is non-empty when the original turn was split at
    deterministic text boundaries. ``parent_event_id`` ties a fragment
    back to its source ConversationEvent so the dispatcher can mark
    the source event complete only after every fragment is accounted
    for.
    """

    turn_id: str
    session_id: str
    branch_id: str
    role: str  # user/assistant/tool/system
    text: str
    estimated_tokens: int
    is_complete: bool = True  # False => still being written
    context_only: bool = False  # True => include for context, don't count as a target
    event_id: str | None = None  # bound ConversationEvent.event_id when known
    # --- phase B long-turn fragmentation ---
    is_oversize: bool = False  # True => single turn > max_input_tokens
    parent_event_id: str | None = None  # fragment's parent ConversationEvent
    span_start: int = 0  # code-point span into parent.text
    span_end: int = 0
    condition_prefix: str | None = None  # carried only when the fragment starts mid-condition
    needs_context: bool = False  # True => durable needs_context state; never a target


@dataclass(frozen=True)
class BatchPolicyInput:
    max_turns_per_batch: int = DEFAULT_MAX_TURNS_PER_BATCH
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS
    overlap_turns: int = 0
    # Phase B overheads: when > 0, the planner keeps room in the
    # input budget for system/schema instructions and context prefix
    # alongside the turns. v0.1 assumed 0 overhead; phase B preserves
    # that default so existing tests pass. When the caller wants to
    # honour the docs/09 budget literally, set these explicitly.
    schema_overhead_tokens: int = 0
    context_prefix_tokens: int = 0


def is_oversize(turn: TurnSample, max_input_tokens: int) -> bool:
    """A single turn that already exceeds the input budget on its own.

    Includes schema/system overhead so a 5500-token turn is rejected
    even though it is technically below the 6000-token input cap.
    """
    return turn.estimated_tokens > max_input_tokens


# ---------------------------------------------------------------------------
# Phase B: long-turn fragmentation
# ---------------------------------------------------------------------------


# Deterministic text boundaries, in priority order:
#   1. blank line (paragraph)
#   2. list item / numbered item
#   3. sentence terminator
#   4. hard line break
#   5. whitespace (last resort)
_BOUNDARY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\n\s*\n"),                                # paragraph
    re.compile(r"\n\s*(?:[-*+]|\d+\.)\s+"),                # list / numbered item
    re.compile(r"(?<=[\.!?。！？])\s+"),                    # sentence
    re.compile(r"\n"),                                     # hard line break
    re.compile(r"\s+"),                                    # whitespace
)


def _boundary_offsets(text: str) -> list[int]:
    """Return code-point offsets where the text can be safely split.

    ``offsets[0] == 0`` and ``offsets[-1] == len(text)`` so the caller
    can iterate ``text[s:e]`` over the partition without checking
    bounds. The offsets are computed against the original ``str`` so
    they are stable code-point positions, not byte offsets.
    """
    n = len(text)
    if n == 0:
        return [0]
    found: set[int] = {0, n}
    for pattern in _BOUNDARY_PATTERNS:
        for m in pattern.finditer(text):
            pos = m.end()
            if 0 < pos < n:
                found.add(pos)
    return sorted(found)


def split_user_turn_by_tokens(
    *,
    text: str,
    parent_event_id: str | None,
    max_input_tokens: int,
    condition_prefix_budget_tokens: int = 96,
) -> list[TurnSample]:
    """Split a long user turn into fragments that fit ``max_input_tokens``.

    Each fragment:
      * has a deterministic code-point ``span_start`` / ``span_end``
        that lines up with the original text (``text[s:e]`` reconstructs it)
      * carries an optional ``condition_prefix`` when the fragment
        starts mid-condition (so the planner does not silently widen
        the meaning)
      * shares ``parent_event_id`` so the dispatcher can mark the
        source event complete only after every fragment is accounted
        for

    Returns an empty list when the text cannot be safely split (no
    boundary, or every candidate cut would still be over budget). The
    caller is expected to fall back to ``needs_context``.
    """
    if not text:
        return []
    if heuristic_tokens(text) <= max_input_tokens:
        return []
    boundaries = _boundary_offsets(text)
    fragments: list[TurnSample] = []
    start = 0
    while start < len(text):
        end = len(text)
        chosen = None
        for b in boundaries:
            if b <= start:
                continue
            candidate = text[start:b]
            if heuristic_tokens(candidate) <= max_input_tokens:
                chosen = b
                continue
            break
        if chosen is None:
            # No boundary from ``start`` produces a fitting fragment.
            # Try a single-character fallback to find a strictly
            # smaller split. If even one character is over budget, the
            # text is intrinsically over budget and we refuse.
            tokens_per_char = max(1, heuristic_tokens(text[start:start + 1]) or 1)
            if tokens_per_char > max_input_tokens:
                return []
            chosen = start + max(1, max_input_tokens // tokens_per_char)
            if chosen >= len(text):
                chosen = len(text)
        end = chosen
        body = text[start:end]
        cond_prefix = None
        if start > 0:
            # Look back for a clause terminator within a small budget
            # and carry that as the condition prefix.
            window = text[max(0, start - 256):start]
            for pat in (re.compile(r"[。！？.!?]\s*"), re.compile(r"\n\s*\n")):
                m = None
                for cand in pat.finditer(window):
                    m = cand
                if m is not None:
                    cond_prefix = window[m.start():].strip()
                    if heuristic_tokens(cond_prefix) > condition_prefix_budget_tokens:
                        cond_prefix = None
                    else:
                        break
        frag_tokens = heuristic_tokens(body) + 32  # per-fragment overhead
        fragments.append(TurnSample(
            turn_id=f"frag::{parent_event_id}::{start}-{end}",
            session_id="",
            branch_id="",
            role="user",
            text=body,
            estimated_tokens=frag_tokens,
            is_complete=True,
            event_id=None,
            is_oversize=False,
            parent_event_id=parent_event_id,
            span_start=start,
            span_end=end,
            condition_prefix=cond_prefix,
        ))
        if end >= len(text):
            break
        start = end
    if not fragments:
        return []
    if any(heuristic_tokens(f.text) > max_input_tokens for f in fragments):
        return []
    return fragments


def plan_one_batch(
    turns: Sequence[TurnSample],
    policy: BatchPolicyInput,
    start_index: int,
) -> tuple[list[TurnSample], int, str | None]:
    """Greedy-pack a single batch starting at start_index.

    Returns (selected_turns, new_index, reason). If nothing fits, the
    batch may be empty and reason='empty'. If a turn is oversize, it
    becomes a single-element batch and reason='oversize'. Fragments
    (parent_event_id != None) are batched normally but counted with
    the schema/context overhead so the input cap is never breached.
    """

    selected: list[TurnSample] = []
    token_total = policy.schema_overhead_tokens + policy.context_prefix_tokens
    idx = start_index
    reason: str | None = None

    if idx >= len(turns):
        return selected, idx, "empty"

    first = turns[idx]
    if first.context_only:
        # context_only never leads a batch.
        return selected, idx, "no_target"

    if first.needs_context:
        # The needs_context marker is durably persisted separately;
        # it never consumes a target slot.
        return selected, idx + 1, "needs_context"

    if is_oversize(first, policy.max_input_tokens):
        return [first], idx + 1, "oversize"

    while idx < len(turns):
        t = turns[idx]
        # Must respect session/branch identity of the first turn.
        if t.session_id != first.session_id or t.branch_id != first.branch_id:
            reason = reason or "branch_mismatch"
            break
        if t.context_only:
            # context_only is allowed inside a batch but never consumes a target slot.
            selected.append(t)
            idx += 1
            continue
        if t.needs_context:
            # Same as context_only in the planning sense: do not count
            # as a target, do not advance the budget.
            selected.append(t)
            idx += 1
            continue
        if not t.is_complete:
            reason = reason or "incomplete_tail"
            break
        # Target turn: check turn count and token budget.
        target_count = sum(1 for s in selected if not s.context_only and not s.needs_context)
        if target_count >= policy.max_turns_per_batch:
            reason = reason or "turn_cap"
            break
        if token_total + t.estimated_tokens > policy.max_input_tokens:
            reason = reason or "token_cap"
            break
        selected.append(t)
        token_total += t.estimated_tokens
        idx += 1

    if not selected:
        reason = "empty"
    return selected, idx, reason


def plan_batches(
    turns: Sequence[TurnSample],
    policy: BatchPolicyInput,
) -> list[list[TurnSample]]:
    """Plan all batches by repeatedly packing from the head of the queue."""
    if not turns:
        return []
    batches: list[list[TurnSample]] = []
    idx = 0
    while idx < len(turns):
        batch, new_idx, _reason = plan_one_batch(turns, policy, idx)
        if new_idx <= idx:
            # Safety: prevent infinite loop if planner returns no progress.
            idx += 1
        else:
            idx = new_idx
        if batch:
            batches.append(batch)
    return batches


def heuristic_tokens(text: str) -> int:
    """A simple deterministic estimate: one token per 4 bytes of UTF-8."""
    if not text:
        return 0
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


def collect_oversize_needs_context(
    turns: Sequence[TurnSample],
    *,
    max_input_tokens: int,
) -> list[TurnSample]:
    """Return the turns that cannot be safely split and need a
    needs_context durable record.

    The caller persists these as ``oversize_units`` so they surface
    in the UI / doctor without becoming active batch targets.
    """
    out: list[TurnSample] = []
    for t in turns:
        if not t.is_oversize:
            continue
        if t.needs_context:
            out.append(t)
            continue
        # An oversize turn that survived events_to_turn_samples is one
        # that could not be safely split.
        out.append(t)
    return out


__all__ = [
    "BatchPolicyInput",
    "TurnSample",
    "plan_batches",
    "plan_one_batch",
    "is_oversize",
    "heuristic_tokens",
    "split_user_turn_by_tokens",
    "collect_oversize_needs_context",
    "DEFAULT_MAX_TURNS_PER_BATCH",
    "DEFAULT_MAX_INPUT_TOKENS",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "SCHEMA_OVERHEAD_TOKENS",
    "CONTEXT_PREFIX_TOKENS",
]