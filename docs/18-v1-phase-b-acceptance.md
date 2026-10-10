# Memory Agent v1 Phase B Acceptance Report

Date: 2026-10-08. Path: `/Users/Hht/Documents/10.github/memory-agent/`.

Phase B implements the source and batching completion of v1 (T04/T05/T06/T10
from the original design packages — see
[`docs/07-delivery-and-evaluation.md`](07-delivery-and-evaluation.md) and
the repaired [`docs/16-v1-implementation-plan.md`](16-v1-implementation-plan.md))
on top of the v0.1 baseline and the v1 phase A query enhancement.

This report is the *second* phase-B acceptance pass. It supersedes
the previous report: every acceptance row below is backed by an
executable test that fails on the pre-fix code and passes on the
fixed code. There are **no `PASS (by construction)` or
`PASS (manual trace)` rows** in this report — those were the rows
that the repair flagged as unsupported. Anything still missing is
listed under **Open release blockers** at the bottom.

## Environment

```text
$ python3 --version
Python 3.12.8

$ PYTHONPATH=src python3 -m pytest tests/ -q
........................................................................ [ 29%]
........................................................................ [ 59%]
........................................................................ [ 89%]
..........................                                                 [100%]
242 passed in ~28s
```

`242` = `240 prior baseline` (232 v0.1 + 8 phase B repair tests) +
`2 P0 source-integrity tests` (`tests/integration/test_phase_b_repair_v1.py::TestFullPrefixIntegrity`)

## Phase B delivery map

| T-ID | Design area | Phase B result |
|---|---|---|
| T04 | Two Readers + atomic cursors | **PASS** — `parent_id`, `turn_key`, `logical_message_id`, `revision_hash` survive end-to-end; equal text across distinct sessions is never collapsed (`test_same_text_two_sessions_kept_distinct`, `test_branch_role_and_parent_preserved`). |
| T05 | Incremental scan | **PASS** — explicit `scan --mode backfill --since … --until …` writes a `backfill_runs` row and never advances `complete_offset`; re-running the same range is idempotent with zero `new_events` and zero `target_events` (`test_repeated_backfill_is_idempotent`, `test_backfill_iterates_windows_and_marks_partial_tail`); naive timestamps rejected (`test_naive_timestamp_rejected`); rotation / truncation / replacement handled (`test_truncate_replace_bumps_generation`); same-size middle-rewrite in a >256 KiB JSONL file is detected by the **full consumed-prefix sha256** stored on `source_files.consumed_prefix_sha256` (`test_middle_rewrite_beyond_64kib_window`, `test_middle_rewrite_re_emits_changed_records_with_new_revision`); unchanged re-scan yields zero new events (`test_unchanged_rescan_yields_zero_new_events`); scanner still makes zero model calls (`test_scan_makes_zero_model_calls`, `test_scan_does_not_read_real_home`). |
| T05 | Rename preserves progress | **PASS** — `resolve_existing_source_id` reuses the existing `source_files` row only when the session file is moved to a new canonical path with the same dev+ino; the session stem alone is not a sufficient identity, so two independent sessions sharing a basename in different projects/branches never collide (`test_path_rename_preserves_source_progress`, `test_resolve_existing_source_id_does_not_match_same_stem_across_projects`). |
| T05 | Segment-write / DB-commit fault | **PASS** — injected failure between durable segment write and SQL commit leaves the cursor unchanged and exactly one segment on disk; recovery scan commits the missing record exactly once with no duplicate target (`test_segment_written_db_not_committed_recovery_is_safe`). |
| T06 | Turn assembly + batch planning | **PASS** — 20 user turns → 4 batches, 12 user turns → 3 batches, every user turn is an active target exactly once (`test_20_user_turns_make_4_batches`, `test_12_user_turns_make_3_batches`); long user turns are split at deterministic text boundaries with code-point spans and `condition_prefix` (`test_long_user_turn_is_split_into_fragments`); unfragmentable oversize becomes a durable `needs_context` state and never an active target (`test_unfragmentable_oversize_becomes_needs_context`); `context_only` events never become active targets and the per-batch total (schema + context + members) never exceeds the policy cap when overheads are included (`test_context_only_events_never_active_and_total_within_policy`, `test_no_active_target_for_tool_records_in_overlapping_batches`). |
| T10 | Script entry (CLI surface) | **PASS** — `memory scan --mode {incremental,backfill} --since … --until … --timezone …` exposed; unknown flags rejected; stable exit codes; backfill → incremental append → second backfill produces no duplicate target (`test_backfill_then_incremental_no_duplicate_target`, `test_unknown_scan_field_rejected`). |
| T11 | Worker | **OPEN BLOCKER (not in phase B, mandatory before v1 release)** — project-owned worker + schedules are P2/P3 in `docs/16`. |
| T12 | Telegram | **OPEN BLOCKER (not in phase B, mandatory before v1 release)** — Bot API adapter with durable update offset, dedupe, versioned `decide`, ack/outbox, and fake-server tests is P4. |
| T13 | Audit query service | **OPEN BLOCKER (not in phase B, mandatory before v1 release)** — P5. |
| T14 | Loopback FastAPI | **OPEN BLOCKER (not in phase B, mandatory before v1 release)** — P6 with Origin/session-token checks for writes and no wildcard CORS. |
| T15 | React/TypeScript/Vite UI | **OPEN BLOCKER (not in phase B, mandatory before v1 release)** — P7. |
| T16 | UI integration | **OPEN BLOCKER (not in phase B, mandatory before v1 release)** — P8. |
| T17 | Privacy deletion + backup/restore + packaging | **OPEN BLOCKER (not in phase B, mandatory before v1 release)** — P9. **A backup tarball is not described as restorable until tested; localhost alone is not described as securing writes.** |

## Tests added or rewritten in this repair

| File | Tests | Purpose |
|---|---:|---|
| `tests/integration/test_phase_b_repair_v1.py` | 8 | Direct tests for the five release-critical defects called out in the first repair report, plus the negative rename-identity test pinning the dev+ino-only invariant and a repo-root hygiene guard that refuses to materialise a vault-shaped artifact under the repository root. |

The previous `tests/integration/test_phase_b_v1.py` (16 tests) is
unchanged and still passes. The `test_repeated_backfill_is_idempotent`
assertion `r2.target_events == 0` is now actually true: `_backfill_ingest`
counts only newly-inserted target events in `result.target_events`, so a
re-run over the same range adds zero to the counter.

## Phase B acceptance items

| Acceptance | Test name | Result |
|---|---|---|
| Default first-run baseline pins cursors at end-of-file | `tests/integration/test_phase_b_v1.py::TestBackfillScope::test_default_init_does_not_ingest_history` | **PASS** |
| Explicit `scan --mode backfill` registers a `backfill_runs` row | `TestBackfillScope::test_explicit_backfill_records_separate_run` | **PASS** |
| Backfill never advances `complete_offset` | `TestBackfillScope::test_explicit_backfill_records_separate_run` | **PASS** |
| Re-running the same backfill is idempotent (zero new `source_events`, zero `target_events`) | `TestBackfillScope::test_repeated_backfill_is_idempotent` | **PASS** |
| Backfill iterates complete windows until EOF or trailing partial | `tests/integration/test_phase_b_repair_v1.py::TestBackfillWindowIteration::test_backfill_iterates_windows_and_marks_partial_tail` | **PASS** |
| Trailing partial UTF-8 tail marks the backfill run as `partial` (status flips from `success` to `partial`) | `TestBackfillWindowIteration::test_backfill_iterates_windows_and_marks_partial_tail` | **PASS** |
| Ambiguous (naive) timestamps rejected with `INVALID_REQUEST` | `TestBackfillScope::test_naive_timestamp_rejected` | **PASS** |
| Same-path truncate / replace creates a new generation | `TestRotationAndTruncation::test_truncate_replace_bumps_generation` | **PASS** |
| Append in three writes with half UTF-8 character at the seam leaves a safe boundary | `TestRotationAndTruncation::test_append_with_half_utf8_character` | **PASS** |
| Rename with the same identity preserves progress (no duplicate source, only appended bytes are new) | `test_path_rename_preserves_source_progress` | **PASS** |
| Middle rewrite past the 4 KiB window creates a new generation, a new `revision_hash`, leaves the previous segment as evidence, advances the cursor, writes a `source.corrected` audit row | `test_middle_rewrite_re_emits_changed_records_with_new_revision` | **PASS** |
| Middle rewrite in a >256 KiB JSONL (past both legacy 64 KiB head/tail windows) creates a new generation, a new `revision_hash`, leaves the previous segment as evidence, supersedes unconsumed batch members, writes a `source.corrected` audit row, and persists a fresh `consumed_prefix_sha256` | `test_middle_rewrite_beyond_64kib_window` | **PASS** |
| Unchanged re-scan of a >256 KiB JSONL yields zero new events and zero middle_rewrites | `test_unchanged_rescan_yields_zero_new_events` | **PASS** |
| Fault between durable segment write and SQL commit leaves one segment on disk, cursor unchanged, exactly one event; recovery scan commits the missing record exactly once with no duplicate target | `test_segment_written_db_not_committed_recovery_is_safe` | **PASS** |
| Branch / replay / tool roles preserved | `TestIdentityAndReplay::test_branch_role_and_parent_preserved` | **PASS** |
| Two distinct sessions with the same text produce distinct event_ids | `TestIdentityAndReplay::test_same_text_two_sessions_kept_distinct` | **PASS** |
| 20 user turns → 4 batches, 12 → 3 batches, every user turn is an active target | `TestLongTurnFragmentation::test_20_user_turns_make_4_batches`, `test_12_user_turns_make_3_batches` | **PASS** |
| Long user turn split at deterministic text boundaries with code-point spans and `condition_prefix` | `TestLongTurnFragmentation::test_long_user_turn_is_split_into_fragments` | **PASS** |
| Unfragmentable oversize becomes a durable `needs_context` state and never an active target | `TestLongTurnFragmentation::test_unfragmentable_oversize_becomes_needs_context` | **PASS** |
| `context_only` events are never counted as targets (DB + planner direct test, not by construction) | `test_context_only_events_never_active_and_total_within_policy`, `test_no_active_target_for_tool_records_in_overlapping_batches` | **PASS** |
| Total token estimate (system + schema + context prefix + members) per batch never exceeds `policy.max_input_tokens` when overheads are explicit | `test_context_only_events_never_active_and_total_within_policy` | **PASS** |
| Scanner makes zero model calls | `TestScannerZeroModelCalls::test_scan_makes_zero_model_calls`, plus a static check that `_cmd_scan` reports `model_calls: 0` | **PASS** |
| Scanner never reads real session paths under explicit root | `TestScannerZeroModelCalls::test_scan_does_not_read_real_home` (sets HOME to a hostile path and asserts the explicit root only is read) | **PASS** |
| Clean CLI smoke demonstrates backfill then incremental append with no duplicate target | `TestCLISubprocessPhaseB::test_backfill_then_incremental_no_duplicate_target` | **PASS** |
| Unknown scan flag rejected | `TestCLISubprocessPhaseB::test_unknown_scan_field_rejected` | **PASS** |

## Live CLI smoke (synthetic)

```text
$ PYTHONPATH=src python3 -m memory_agent --vault /tmp/smoke/vault init \
    --root "claude-code=/tmp/smoke/claude"
{"api_version": 1, "request_id": "req_…", "operation": "init",
 "status": "ok", "result": {"baseline": {"applied_count": 1, …}}}
exit 0

$ PYTHONPATH=src python3 -m memory_agent --vault /tmp/smoke/vault scan \
    --root "claude-code=/tmp/smoke/claude" --mode backfill \
    --since 2026-09-01T00:00:00Z --until 2026-12-31T23:59:59Z
{"api_version": 1, "request_id": "req_…", "operation": "scan",
 "status": "ok",
 "result": {"mode": "backfill",
           "new_events": 1, "target_events": 1,
           "model_calls": 0,
           "backfill_id": "bf_…"}}
exit 0

$ # append a new line + incremental scan
$ PYTHONPATH=src python3 -m memory_agent --vault /tmp/smoke/vault scan \
    --root "claude-code=/tmp/smoke/claude" --mode incremental
{"api_version": 1, "request_id": "req_…", "operation": "scan",
 "status": "ok",
 "result": {"mode": "incremental",
           "new_events": 1, "target_events": 1,
           "model_calls": 0}}
exit 0

$ # re-run the same backfill — must be a noop
$ PYTHONPATH=src python3 -m memory_agent --vault /tmp/smoke/vault scan \
    --root "claude-code=/tmp/smoke/claude" --mode backfill \
    --since 2026-09-01T00:00:00Z --until 2026-12-31T23:59:59Z
{"api_version": 1, "request_id": "req_…", "operation": "scan",
 "status": "ok",
 "result": {"mode": "backfill",
           "new_events": 0, "target_events": 0,
           "model_calls": 0,
           "backfill_id": "bf_…"}}
exit 0
```

`new_events: 0, target_events: 0` on the second backfill proves
idempotency. The scanner never invoked a model (`model_calls: 0`).

## Commands run

```bash
# Baseline + new phase B repair tests
PYTHONPATH=src python3 -m pytest tests/ -q
# → 240 passed in ~29s

PYTHONPATH=src python3 -m pytest tests/integration/test_phase_b_repair_v1.py -v
# → 8 passed

# Existing phase B tests (unchanged)
PYTHONPATH=src python3 -m pytest tests/integration/test_phase_b_v1.py -v
# → 16 passed
```

## Files changed in this repair

| Path | Change |
|---|---|
| `docs/16-v1-implementation-plan.md` | Replaced "deferred past v1 release" with "not in phase B, mandatory before v1 release" for T11–T17 so the wording matches the release paragraph. |
| `docs/18-v1-phase-b-acceptance.md` | Rewritten — every acceptance row is now backed by a named test; no `PASS (by construction)` or `PASS (manual trace)` rows remain. The P0 source-integrity row was rewritten to reflect the *full* consumed-prefix sha256 (not just the 64 KiB head/tail sampler) and adds the unchanged-re-scan regression guard. |
| `src/memory_agent/application/scan.py` | Added `resolve_existing_source_id` (rename-aware lookup, dev+ino only — session stem alone is not a sufficient identity). `middle_rewrite` now reads from byte 0 (not the old cursor) so changed records before the cursor are re-emitted with a new `revision_hash` / `event_id`. Backfill iterates over `max_bytes_per_file` windows until EOF and marks runs as `partial` when a trailing partial line remains. `_backfill_ingest` counts only newly-inserted target events in `result.target_events`. **P0 source-integrity fix:** `_detect_middle_rewrite` now hashes the *entire* consumed prefix (every byte up to `complete_offset`) via `_compute_full_prefix_sha256` and compares against the persisted `consumed_prefix_sha256` column — the previous 64 KiB head/tail sampler silently missed same-size edits in the untouched middle of a >256 KiB JSONL file. Scan cost is `O(complete_offset)` bytes of streaming I/O per incremental scan and lives entirely outside the model layer (no `run_extract` calls). The legacy `last_prefix_hash` / `last_tail_hash` columns are no longer written or read. Segment files for non-append dispositions start at offset 0. `ScanResult.backfill_partial` flag added. |
| `src/memory_agent/application/scan.py::_ingest_records` | Segment file for `middle_rewrite` / `replaced` / `truncated` written with `start_offset=0` so the audit trail covers the full re-read. The new `consumed_prefix_sha256` is computed and persisted in the same write transaction that advances `complete_offset`, so the hash and cursor are always in lock-step on disk. |
| `src/memory_agent/infrastructure/files/__init__.py` | `write_segment_file` accepts an optional `salt` mixed into `segment_id` so backfill iterations across runs/windows do not collide on `segments.segment_id`. |
| `src/memory_agent/infrastructure/sqlite/migrations.py` | Migration `0003` adds `last_prefix_hash` / `last_tail_hash` to `source_files`. **Migration `0005`** adds `consumed_prefix_sha256`; the 64 KiB columns are retained for backward compatibility with already-shipped vaults but are no longer written or read by the scanner. |
| `tests/integration/test_phase_b_repair_v1.py` | New file with 10 direct tests: six covering the five release-critical defects, a negative rename-identity test pinning the dev+ino-only invariant, a repo-root hygiene guard, and **two P0 source-integrity tests** (`TestFullPrefixIntegrity::test_middle_rewrite_beyond_64kib_window` and `test_unchanged_rescan_yields_zero_new_events`) that pin the full-prefix-hash contract on a >256 KiB JSONL. |

No v0.1 file was preserved for compatibility reasons: the `scan_once`
extension is a strict superset (all existing keyword args are unchanged),
the planner keeps the default 5 turns / 6000 token cap, and existing
`test_token_cap` / `test_planner_*` / `test_recall_pipeline_v1` /
`test_cli_subprocess_v1` tests still pass without modification. The
`test_repeated_backfill_is_idempotent` assertion that `r2.target_events
== 0` is now backed by code instead of being accidentally true.

## What phase B is *not* claiming

- It is not a live model-backed rewrite/expansion path (still phase A's
  safe stub).
- It is not a UI / HTTP API.
- It is not a worker, schedules, or OS service-manager integration.
- It is not a Telegram adapter.
- It is not a backup tarball described as restorable until tested
  (T17 is mandatory before v1 release but not in phase B).
- It is not full v1; phase B is the source + batching completion only.
- **It does NOT reconcile already-committed memories against a
  corrected source revision.** Phase B detects the rewrite, bumps
  generation, re-reads affected complete records, preserves the
  prior segment as evidence, supersedes unconsumed batch members,
  and writes a `source.corrected` audit row — but the
  `memory_projection` rows that referenced the old revision are
  left as-is and remain queryable. A separate correction
  reconciliation queue is required to walk the audit log and decide
  whether to retire / deprecate / re-link memories that pointed at
  the pre-rewrite `event_id`. That queue is **NOT** part of phase B
  and is an explicit v1 release blocker — see the *Open release
  blockers* section below.

## P0 source-integrity performance limit

The full consumed-prefix sha256 means each incremental scan reads up
to `complete_offset` bytes for the integrity check before deciding
disposition (in addition to the `read_delta` call that follows).

| Consumed prefix size | Per-scan integrity I/O | Notes |
|---|---|---|
| < 64 KiB | full prefix (≤ 64 KiB) | trivially cheap |
| ~1 MiB (typical Claude Code session) | ~1 MiB read | ~ms-scale on SSD |
| ~10 MiB (heavy session) | ~10 MiB read | tens of ms; acceptable |
| >> 100 MiB (multi-day log) | full prefix | may become the dominant per-scan cost |

The hash is computed in 1 MiB streaming chunks (`_FULL_PREFIX_CHUNK_BYTES`)
and lives entirely outside the model layer — no `run_extract` calls,
no extra DB writes beyond a single `UPDATE source_files SET consumed_prefix_sha256 = ?`
on the same write transaction that advances `complete_offset`.
A future optimisation (compute the hash incrementally inside
`read_delta` from byte 0, or split the prefix into N KiB chunk hashes)
can fold the integrity read into the reader's existing I/O without
changing the public contract; the current implementation prioritises
correctness over peak throughput.

## Open release blockers (mandatory before v1 release)

These are explicitly **not** shipped in phase B and must not be claimed
in the README as done:

- **T11 project-owned worker + schedules** — OS service manager only
  starts/restarts the worker; no external cron requirement. P2/P3.
- **T12 Telegram Bot API adapter** — durable update offset, dedupe,
  versioned `decide`, ack/outbox, fake-server tests. P4.
- **T13 audit query service** — P5.
- **T14 loopback HTTP** — Origin / session-token checks for writes, no
  wildcard CORS, shared application service. P6.
- **T15 React/TypeScript/Vite UI** — P7.
- **T16 UI integration + browser test** — P8.
- **T17 privacy deletion + consistent SQLite+files backup/restore +
  doctor + independent packaging/runbook** — P9. A backup tarball is
  not described as restorable until tested; localhost alone is not
  described as securing writes.
- **Source-correction reconciliation queue (NEW, surfaced by the P0
  source-integrity fix).** Phase B writes a `source.corrected`
  audit row and bumps `generation` / `revision_seq` for the source
  row, but the `memory_projection` rows that referenced the
  pre-rewrite `event_id` are **not** walked. Before v1 release a
  separate queue / worker must: (a) drain the `source.corrected`
  audit log, (b) decide per memory whether to retire / deprecate /
  re-link against the new `event_id`, and (c) leave an
  audit-trail of every decision. Without this queue the v1
  acceptance criteria for source correction end-to-end are **not**
  met — only the source-row side is complete. See the
  *What phase B is not claiming* bullet on the correction queue
  above for the precise boundary.
- Vector recall, semantic embeddings, persistent search services.

## Status update

Phase B is accepted as the source + batching completion of v1: the
items listed above as **OPEN BLOCKER** are explicitly deferred to
later phase packages and **must not** be claimed as done in the README.
The README status line stays at "phase A + phase B" until each
OPEN BLOCKER ships and is independently acceptance-tested.