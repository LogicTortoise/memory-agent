"""Turn assembly and batch planning.

For v0.1, turn assembly is simple: every *user* event starts a new turn,
the following assistant/tool events belong to that turn until the next
user event. We do not split long turns in this slice; oversize goes to
needs_context via the planner.

Phase B (T06 long-turn fragmentation) extends the above:

  * When a user turn's ``estimated_tokens`` is greater than the input
    budget, the planner tries to split it at deterministic text
    boundaries into ``TurnSample`` fragments that share a
    ``parent_event_id``.
  * When a turn cannot be safely split, it is recorded as a
    ``needs_context`` durable state and never becomes an active batch
    target.
  * Assistant/tool/system events become ``context_only`` rows bound to
    the *preceding* user turn when one exists; pre-conversation context
    rows get folded into the first batch as context only.
  * ``batch_id`` is deterministic for the (session, branch, member-set,
    policy_version) tuple, so replanning is idempotent.
  * Each fragment's ``work_units`` row carries the fragment's
    ``span_start`` / ``span_end`` / ``split_policy`` / ``parent_unit_id``
    so a downstream consumer can rebuild the source event from its
    fragments and mark it complete only after every fragment is
    accounted for.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

from memory_agent.contracts.events import ConversationEvent
from memory_agent.domain.batching import (
    BatchPolicyInput,
    TurnSample,
    heuristic_tokens,
    plan_batches,
    split_user_turn_by_tokens,
)
from memory_agent.infrastructure.sqlite.repositories import (
    BatchRepo,
    OversizeUnitRepo,
    TurnRepo,
    WorkUnitFragmentRepo,
)


def events_to_turn_samples(
    events: Iterable[ConversationEvent],
    *,
    policy: BatchPolicyInput | None = None,
) -> list[TurnSample]:
    """Flatten events into user-turn-shaped samples for the planner.

    Non-user events do not become turns on their own. They are folded
    into the preceding user turn as context_only samples.

    A user turn whose ``estimated_tokens`` exceeds the input budget
    is replaced with one or more fragments (when the text has a safe
    boundary) or marked ``needs_context`` (when no safe boundary
    exists). The original ``event_id`` is preserved as
    ``parent_event_id`` on each fragment.
    """
    events = sorted(events, key=lambda e: (e.source.source_time, e.source.locator.record_ordinal))
    policy = policy or BatchPolicyInput()
    samples: list[TurnSample] = []
    pending_user: TurnSample | None = None
    pending_context: list[TurnSample] = []
    for ev in events:
        text = ev.text
        tokens = heuristic_tokens(text) + 32  # +overhead
        if ev.role == "user" and ev.kind == "message":
            if pending_user is not None:
                samples.append(pending_user)
                samples.extend(pending_context)
                pending_context = []
            fragments = split_user_turn_by_tokens(
                text=text,
                parent_event_id=ev.event_id,
                max_input_tokens=policy.max_input_tokens,
            ) if tokens > policy.max_input_tokens else []
            if tokens > policy.max_input_tokens and not fragments:
                # Cannot safely split: durable needs_context.
                pending_user = TurnSample(
                    turn_id=f"oversize::{ev.source.session_id}::{ev.source.branch_id}::{ev.source.locator.record_ordinal}",
                    session_id=ev.source.session_id,
                    branch_id=ev.source.branch_id,
                    role=ev.role,
                    text=text,
                    estimated_tokens=tokens,
                    is_complete=True,
                    event_id=ev.event_id,
                    is_oversize=True,
                    needs_context=True,
                )
            elif fragments:
                # Splittable: keep the fragments as the pending user
                # "slot"; the first fragment becomes the active target
                # candidate and the rest are queued behind it.
                for i, frag in enumerate(fragments):
                    frag_session = ev.source.session_id
                    frag_branch = ev.source.branch_id
                    frag_event = ev.event_id
                    bound = TurnSample(
                        turn_id=frag.turn_id,
                        session_id=frag_session,
                        branch_id=frag_branch,
                        role=ev.role,
                        text=frag.text,
                        estimated_tokens=frag.estimated_tokens,
                        is_complete=True,
                        event_id=frag_event,
                        is_oversize=False,
                        parent_event_id=frag_event,
                        span_start=frag.span_start,
                        span_end=frag.span_end,
                        condition_prefix=frag.condition_prefix,
                    )
                    if i == 0:
                        pending_user = bound
                    else:
                        samples.append(bound)
            else:
                pending_user = TurnSample(
                    turn_id=f"turn::{ev.source.session_id}::{ev.source.branch_id}::{ev.source.locator.record_ordinal}",
                    session_id=ev.source.session_id,
                    branch_id=ev.source.branch_id,
                    role=ev.role,
                    text=text,
                    estimated_tokens=tokens,
                    is_complete=True,
                    event_id=ev.event_id,
                )
        else:
            # Treat assistant/tool/system as context for the prior user turn.
            sample = TurnSample(
                turn_id=f"ctx::{ev.event_id}",
                session_id=ev.source.session_id,
                branch_id=ev.source.branch_id,
                role=ev.role,
                text=text,
                estimated_tokens=tokens,
                is_complete=True,
                context_only=True,
                event_id=ev.event_id,
            )
            if pending_user is not None:
                pending_context.append(sample)
            else:
                # Pre-conversation context; still record as a target with
                # tiny weight so the planner can produce a single batch.
                pending_user = TurnSample(
                    turn_id=f"pre::{ev.event_id}",
                    session_id=ev.source.session_id,
                    branch_id=ev.source.branch_id,
                    role=ev.role,
                    text=text,
                    estimated_tokens=tokens,
                    is_complete=True,
                )
    if pending_user is not None:
        samples.append(pending_user)
        samples.extend(pending_context)
    return samples


def plan_batches_for_session(
    *,
    session_id: str,
    branch_id: str,
    events: list[ConversationEvent],
    policy: BatchPolicyInput,
    conn,
) -> list[dict[str, Any]]:
    """Plan and persist batches for one (session, branch).

    Every user turn in the produced batches is its own ``active_target``
    work unit bound to the source event's ``event_id``. Assistant and
    tool rows are persisted as ``context_only`` members of the same
    batch (or the nearest batch when no user turn is available).

    Oversize user turns that could not be safely split are persisted
    as ``oversize_units`` rows and never become batch members.
    """
    samples = events_to_turn_samples(events, policy=policy)
    # Persist needs_context durable rows up front so they survive
    # replanning and surface in `memory doctor`.
    oversize_repo = OversizeUnitRepo(conn)
    fragment_repo = WorkUnitFragmentRepo(conn)
    for s in samples:
        if s.needs_context and s.event_id:
            oversize_repo.upsert({
                "oversize_id": "over_" + hashlib.sha256(
                    f"{session_id}|{branch_id}|{s.event_id}".encode()
                ).hexdigest()[:12],
                "session_id": session_id,
                "branch_id": branch_id,
                "event_id": s.event_id,
                "estimated_tokens": s.estimated_tokens,
                "reason": "oversize_no_safe_split",
                "state": "needs_context",
                "policy_version": f"v1.{policy.max_turns_per_batch}",
            })
        elif s.parent_event_id and s.event_id is None:
            fragment_repo.record({
                "parent_event_id": s.parent_event_id,
                "span_start": s.span_start,
                "span_end": s.span_end,
                "session_id": session_id,
                "branch_id": branch_id,
                "policy_version": f"v1.{policy.max_turns_per_batch}",
                "condition_prefix": s.condition_prefix,
            })
    # Filter needs_context samples out of the planning horizon; they
    # are persisted separately and never consume target slots.
    plannable = [s for s in samples if not s.needs_context]
    turn_batches = plan_batches(plannable, policy)
    if not turn_batches:
        return []
    batches: list[dict[str, Any]] = []
    epoch = 1
    batch_repo = BatchRepo(conn)
    turn_repo = TurnRepo(conn)
    for turns in turn_batches:
        # Build a stable batch_id from session/branch/member-set so
        # replans are idempotent.
        member_fingerprint = "|".join(t.turn_id for t in turns)
        batch_id = "batch_" + hashlib.sha256(
            f"{session_id}|{branch_id}|{epoch}|{member_fingerprint}|{policy.schema_overhead_tokens}".encode("utf-8")
        ).hexdigest()[:12]

        members: list[dict[str, Any]] = []
        ord_counter = 1
        seen_unit_ids: set[str] = set()
        active_targets: list[dict[str, Any]] = []
        for t in turns:
            is_target = (not t.context_only) and (t.role == "user") and not t.needs_context
            unit_id = "unit_" + hashlib.sha256(
                f"{batch_id}|{t.turn_id}|{epoch}".encode("utf-8")
            ).hexdigest()[:12]
            if unit_id in seen_unit_ids:
                # Defensive: planner should not emit duplicate turn_ids
                # in a single batch. Skip rather than crash.
                continue
            seen_unit_ids.add(unit_id)

            purpose = "target" if is_target else "context_only"
            active = 1 if is_target else 0
            member = {
                "unit_id": unit_id,
                "purpose": purpose,
                "order": ord_counter,
                "epoch": epoch,
                "active_target": active,
                # Binding metadata (consumed by process_batches._build_task).
                "turn_id": t.turn_id,
                "event_id": t.event_id,
                "parent_event_id": t.parent_event_id,
                "span_start": t.span_start,
                "span_end": t.span_end,
                "condition_prefix": t.condition_prefix,
                "text_preview": t.text[:64],
            }
            members.append(member)
            ord_counter += 1

            if is_target and t.event_id:
                # Persist a work_units row keyed to this batch, so
                # process_batches._build_target_lookup can resolve it.
                conn.execute(
                    """INSERT OR IGNORE INTO work_units(
                        unit_id, session_id, branch_id, turn_id, event_id,
                        span_start, span_end, split_policy, parent_unit_id, epoch
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        unit_id, session_id, branch_id, t.turn_id,
                        t.event_id, t.span_start, t.span_end,
                        ("fragment" if t.parent_event_id else None),
                        ("unit_" + hashlib.sha256(
                            f"parent|{t.parent_event_id}".encode()
                        ).hexdigest()[:12]) if t.parent_event_id else None,
                        epoch,
                    ),
                )
                active_targets.append({
                    "unit_id": unit_id,
                    "event_id": t.event_id,
                    "turn_id": t.turn_id,
                    "text": t.text,
                    "parent_event_id": t.parent_event_id,
                    "span_start": t.span_start,
                    "span_end": t.span_end,
                })

        # Update the per-session turn counter for the *first* active
        # target only (turns table is keyed per turn_id, not per batch).
        if active_targets:
            first_target = active_targets[0]
            ordinal = int(batch_id[-4:], 16) if batch_id[-4:].isalnum() else 1
            turn_repo.upsert({
                "turn_id": first_target["turn_id"],
                "session_id": session_id,
                "branch_id": branch_id,
                "ordinal": ordinal,
                "state": "closed",
                "first_event_id": first_target["event_id"],
                "last_event_id": first_target["event_id"],
            })

        input_hash = hashlib.sha256(
            member_fingerprint.encode("utf-8")
        ).hexdigest()
        batch = {
            "batch_id": batch_id,
            "session_id": session_id,
            "branch_id": branch_id,
            "policy_version": f"v1.{policy.max_turns_per_batch}",
            "input_hash": input_hash,
            "status": "ready",
            "active_target_count": len(active_targets),
        }
        batch_repo.create(batch, members)
        batches.append({
            "batch": batch,
            "members": members,
            "active_targets": active_targets,
        })
        epoch += 1
    return batches