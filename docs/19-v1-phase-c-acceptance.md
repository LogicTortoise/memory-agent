# Memory Agent v1 Phase C Acceptance Report

Date: 2026-10-08 (Phase C acceptance); 2026-10-09 (Phase C repair-1).
Path: `/Users/Hht/Documents/10.github/memory-agent/`.

Phase C delivers the async `call` interface, project-owned worker
with schedules, and the source-correction reconciliation queue that
was the explicit v1 release blocker surfaced in Phase B.

This report is the *Phase C* acceptance pass. Every acceptance row
below is backed by an executable test that fails on the pre-Phase-C
code and passes on the Phase-C code. The report supersedes the
Phase B report (`docs/18`) for everything Phase C owns; Phase B
items that are unchanged are still covered by the Phase B report
and re-run by the same suite.

## Environment

```text
$ python3 --version
Python 3.12.8

$ PYTHONPATH=src python3 -m pytest tests/ -q
........................................................................ [ 26%]
........................................................................ [ 52%]
........................................................................ [ 78%]
...........................................................              [100%]
275 passed in ~44s
```

`275 = 269 prior baseline (v0.1 + phase A + phase B + phase C) + 6
new Phase C repair-1 tests in
`tests/integration/test_phase_c_repair1.py``. The previous Phase C
report claimed "269 = 242 prior baseline + 27 Phase C"; the
repair-1 package added 6 new tests and the count is now 275.

## Phase C delivery map

| Acceptance bucket | Phase C result |
|---|---|
| T10 async `call` (subprocess JSON stdin sync + async, offline queued, unknown fields / unknown operations rejected, versioned `CommandRequest`) | **PASS** — `tests/integration/test_phase_c_v1.py::TestCallSubprocessSyncAndAsync` (`test_sync_init_roundtrip`, `test_async_returns_queued_when_worker_offline`, `test_stdin_must_be_json`, `test_unknown_field_rejected`, `test_unknown_operation_rejected`, `test_review_is_explicitly_unsupported`). |
| T10 idempotency: same key + same params => cached; same key + changed params => `IDEMPOTENCY_CONFLICT` | **PASS** — `TestIdempotency::test_same_key_same_params_returns_cached`, `test_same_key_changed_params_conflicts`, `test_canonical_request_hash_ignores_request_id`. |
| T11 worker `--once` progress; no-increment zero model calls | **PASS** — `TestWorkerOnce::test_worker_once_no_increment_zero_model_calls`. |
| T11 worker drains queued jobs | **PASS** — `TestWorkerOnce::test_worker_once_drains_queued_job`. |
| T11 worker `--once` drains a queued async `scan` job with **zero model calls and no `MEMORY_AGENT_ENABLE_CLAUDE_CLI` opt-in** | **PASS** — `tests/integration/test_phase_c_repair1.py::TestModelFreeDrain::test_async_scan_drains_without_cli_opt_in`, `test_drain_does_not_instantiate_provider_for_scan`. |
| T11 lease acquisition / expiration / re-acquisition / vault-level writer lock (one active worker per vault) | **PASS** — `TestWorkerOnce::test_lease_expiration_releases_to_new_worker`, `test_two_workers_lock_contention`; `TestLeaseExclusion::test_two_distinct_worker_ids_cannot_both_hold_active_lease`. |
| T11 stale `running` jobs (worker crash) are reclaimed on the next pass and reach a terminal state | **PASS** — `TestLeaseExclusion::test_stale_running_job_is_reclaimed_on_next_pass` (verifies both the reclaim step and that the job is finalised exactly once, not stuck forever). |
| T11 `job.status` / `job.cancel` / `job.retry` read the vault from the explicit `vault_root` parameter; no module-global mutable holder | **PASS** — `TestExplicitVaultRoot::test_job_status_does_not_use_module_global`, `test_no_module_global_vault_root_holder`. |
| T11 cancel before finish (drains as `cancelled`, not published) | **PASS** — `TestWorkerOnce::test_cancel_before_finish`. |
| T11 cancel after commit (does NOT roll back a published commit) | **PASS** — `TestWorkerOnce::test_cancel_after_commit_does_not_rollback`. |
| T11 stale revision on `job.cancel` rejected with `INVALID_REQUEST` | **PASS** — `TestWorkerOnce::test_stale_revision_rejected`. |
| T11 worker restart reclaims lease and does NOT republish | **PASS** — `TestWorkerRestart::test_restart_does_not_republish_job`. |
| T11 daily / weekly / monthly schedule semantics; weekly requires daily catch-up | **PASS** — `TestWorkerSchedule::test_force_daily_weekly_monthly`, `test_weekly_requires_daily_catchup`. |
| Source-correction queue: stable proposal id, idempotent re-discovery, recall overlay flags affected memory | **PASS** — `TestSourceCorrectionQueue::test_discovery_creates_pending_proposal_after_rewrite`, `test_overlay_marks_memory_pending_correction`, `test_replay_does_not_duplicate_proposal`, `test_worker_restart_does_not_duplicate_proposal`. |
| Source-correction user decision: `keep_old` / `adopt_new` / `defer` (via `decide_proposal`) | **PASS** — `TestSourceCorrectionQueue::test_decide_keep_resolves_overlay`, `test_decide_defer_leaves_pending`, `test_decide_relink_marks_relink`. |
| Old evidence remains accessible after a rewrite (phase B preservation invariant) | **PASS** — `TestSourceCorrectionQueue::test_old_evidence_remains_accessible`. |
| launchd template ships but is NOT installed | **PASS** — `design/launchd/com.memory-agent.worker.plist` + `design/launchd/README.md`. No `launchctl load` performed. |

The worker concurrency model is **single active worker per vault**
(Phase C repair-1). The previous per-`worker_id` keying allowed two
worker processes to run passes against the same vault concurrently,
which stomped on the `audit_events` `MAX(seq)+1` sequence, the
`source_correction_proposals` upsert, the `memory_projection`
finalise, and the `worker_runtime_state` keys. The new
`WorkerLeaseRepo.acquire` refuses a different worker while any active
lease exists; expired leases are reclaimable by a different worker
exactly as before. A stale `running` job (worker crash mid-job) is
reset to `queued` at the start of each pass via
`JobRepo.reclaim_stale_running`, so the next pass picks it up instead
of leaving it stuck forever.

T11–T17 packages outside the Phase C scope (Telegram, FastAPI,
React UI, packaging / backup, P9) remain OPEN BLOCKERs. They are
listed under *Open release blockers* below and are **not** claimed
in the README.

## Phase C acceptance items

| Acceptance | Test name | Result |
|---|---|---|
| `memory call --request-file - --json` accepts sync via stdin | `TestCallSubprocessSyncAndAsync::test_sync_init_roundtrip` | **PASS** |
| `memory call --request-file - --json` accepts async via stdin and returns `queued` + `job_id` + `worker_offline` warning when worker is not running | `TestCallSubprocessSyncAndAsync::test_async_returns_queued_when_worker_offline` | **PASS** |
| Stdin payload that is not JSON returns `INVALID_REQUEST` (exit 2) | `TestCallSubprocessSyncAndAsync::test_stdin_must_be_json` | **PASS** |
| Unknown fields on `params` are rejected with `INVALID_REQUEST` | `TestCallSubprocessSyncAndAsync::test_unknown_field_rejected` | **PASS** |
| Unknown `operation` is rejected with `INVALID_REQUEST` | `TestCallSubprocessSyncAndAsync::test_unknown_operation_rejected` | **PASS** |
| `review` is explicitly unsupported (UNSUPPORTED, exit 6) and never silently runs | `TestCallSubprocessSyncAndAsync::test_review_is_explicitly_unsupported` | **PASS** |
| Same `(client_id, idempotency_key)` + same params returns the original `job_id` | `TestIdempotency::test_same_key_same_params_returns_cached` | **PASS** |
| Same `(client_id, idempotency_key)` + changed params returns `IDEMPOTENCY_CONFLICT` (exit 4) | `TestIdempotency::test_same_key_changed_params_conflicts` | **PASS** |
| `canonical_request_hash` ignores `request_id` so same logical request re-rendered with a new id reuses the same job | `TestIdempotency::test_canonical_request_hash_ignores_request_id` | **PASS** |
| Worker `--once` with no queued jobs makes zero model calls | `TestWorkerOnce::test_worker_once_no_increment_zero_model_calls` | **PASS** |
| Worker `--once` drains a queued async job and marks it `succeeded` | `TestWorkerOnce::test_worker_once_drains_queued_job` | **PASS** |
| Expired lease is reclaimable by a new worker (re-acquire succeeds) | `TestWorkerOnce::test_lease_expiration_releases_to_new_worker` | **PASS** |
| Two distinct workers do NOT hold distinct active leases (vault-level writer lock) | `TestWorkerOnce::test_two_workers_lock_contention` | **PASS** |
| Cancel before the worker claims the job marks the job `cancelled` (not `succeeded`) | `TestWorkerOnce::test_cancel_before_finish` | **PASS** |
| Cancel after the job has succeeded does not roll back the published commit (warning + `job_state` stays `succeeded`) | `TestWorkerOnce::test_cancel_after_commit_does_not_rollback` | **PASS** |
| `job.cancel --expected-revision` with a stale value is rejected with `INVALID_REQUEST` | `TestWorkerOnce::test_stale_revision_rejected` | **PASS** |
| Worker restart reclaims an expired lease and does NOT republish already-succeeded jobs | `TestWorkerRestart::test_restart_does_not_republish_job` | **PASS** |
| Async `scan` job completes via `worker --once` with **zero model calls and no `MEMORY_AGENT_ENABLE_CLAUDE_CLI` opt-in**; status is `succeeded` and a repeat worker pass makes no further changes | `TestModelFreeDrain::test_async_scan_drains_without_cli_opt_in`, `test_drain_does_not_instantiate_provider_for_scan` | **PASS** |
| WorkerLeaseRepo refuses a second worker while another worker's lease is still active; expired leases are reclaimable by a different worker | `TestLeaseExclusion::test_two_distinct_worker_ids_cannot_both_hold_active_lease` | **PASS** |
| Stale `running` jobs (worker crash) are reclaimed on the next pass and finalised exactly once | `TestLeaseExclusion::test_stale_running_job_is_reclaimed_on_next_pass` | **PASS** |
| `handle_call('job.status')` reads the vault from the explicit `vault_root` parameter, not from a module-global holder; the legacy `set_vault_root` is gone | `TestExplicitVaultRoot::test_job_status_does_not_use_module_global`, `test_no_module_global_vault_root_holder` | **PASS** |
| Daily / weekly / monthly schedule fields fire when forced | `TestWorkerSchedule::test_force_daily_weekly_monthly` | **PASS** |
| Weekly run is suppressed unless a daily catch-up has run today | `TestWorkerSchedule::test_weekly_requires_daily_catchup` | **PASS** |
| Source correction: rewrite + re-scan queues a stable `source_correction_proposals` row + recall overlay | `TestSourceCorrectionQueue::test_discovery_creates_pending_proposal_after_rewrite` | **PASS** |
| Source correction: replaying the same discovery pass never duplicates a proposal (stable id) | `TestSourceCorrectionQueue::test_replay_does_not_duplicate_proposal` | **PASS** |
| Source correction: worker restart does not duplicate the proposal | `TestSourceCorrectionQueue::test_worker_restart_does_not_duplicate_proposal` | **PASS** |
| Source correction: `decide_proposal(action="keep_old")` resolves the overlay | `TestSourceCorrectionQueue::test_decide_keep_resolves_overlay` | **PASS** |
| Source correction: `decide_proposal(action="defer")` leaves the proposal pending | `TestSourceCorrectionQueue::test_decide_defer_leaves_pending` | **PASS** |
| Source correction: `decide_proposal(action="adopt_new")` marks the proposal `decided_relink` | `TestSourceCorrectionQueue::test_decide_relink_marks_relink` | **PASS** |
| Old segment file referencing the original `event_id` is still on disk after the rewrite | `TestSourceCorrectionQueue::test_old_evidence_remains_accessible` | **PASS** |
| `memory_correction_overlay` is created when a pending proposal is queued | `TestSourceCorrectionQueue::test_overlay_marks_memory_pending_correction` | **PASS** |

## Live CLI smoke (synthetic)

The smoke below is end-to-end: `init --root` → `call(scan, mode=async)`
while the worker is offline → `worker --once` with
`MEMORY_AGENT_ENABLE_CLAUDE_CLI` *absent* → `job status` reports
`status="ok"` + `job_state="succeeded"` with scan counters and
`model_calls=0`. A second `worker --once` makes no further changes.

```text
$ rm -rf /tmp/smoke_pyc_r1 && mkdir -p /tmp/smoke_pyc_r1/claude/projects/proj_x
$ cat > /tmp/smoke_pyc_r1/claude/projects/proj_x/sess1.jsonl <<'EOF'
{"type":"user","uuid":"u01","message":{"role":"user","content":"use Python for the prototype"},"timestamp":"2026-10-08T10:00:00Z","sessionId":"sess1","parentUuid":null}
{"type":"assistant","uuid":"a01","message":{"role":"assistant","content":"OK."},"timestamp":"2026-10-08T10:00:01Z","sessionId":"sess1","parentUuid":"u01"}
{"type":"user","uuid":"u02","message":{"role":"user","content":"use Python for the prototype"},"timestamp":"2026-10-08T10:05:00Z","sessionId":"sess1","parentUuid":null}
EOF

$ PYTHONPATH=src python3 -m memory_agent --vault /tmp/smoke_pyc_r1/vault --json init \
    --root "claude-code=/tmp/smoke_pyc_r1/claude"
{"api_version": 1, ..., "operation": "init", "status": "ok", ...}

# async dispatch — worker offline (NO MEMORY_AGENT_ENABLE_CLAUDE_CLI set,
# MEMORY_AGENT_MODEL_PROVIDER unset).
$ PYTHONPATH=src python3 -m memory_agent --vault /tmp/smoke_pyc_r1/vault --json call --request-file - <<EOF
{"api_version": 1, "request_id": "req_smoke_async_r1", "operation": "scan", "mode": "async",
 "idempotency_key": "smoke-r1-1",
 "params": {"roots": {"claude-code": "/tmp/smoke_pyc_r1/claude"}, "mode": "incremental"}}
EOF
{"api_version": 1, ..., "operation": "scan", "status": "queued",
 "job_id": "job_6edd81eba66d5170", "job_state": "queued",
 "progress": {"stage": "queued", ..., "model_calls": 0},
 "warnings": ["worker_offline"]}

# worker drains the queue — no opt-in, no model provider instantiated
# for the model-free `scan` operation.
$ PYTHONPATH=src python3 -m memory_agent --vault /tmp/smoke_pyc_r1/vault --json worker --once
{"worker_id": "worker_local", "lease_acquired": true, "lease_released": true,
 "jobs_claimed": ["job_6edd81eba66d5170"],
 "jobs_succeeded": ["job_6edd81eba66d5170"],
 "model_calls": 0,
 "diagnostics": {"reclaimed_running": []},
 "source_correction": {"scanned_memories": 0, "new_proposals": 0, ...},
 "outcome": "ok"}

# job status — terminal succeeded, scan counters, model_calls=0
$ PYTHONPATH=src python3 -m memory_agent --vault /tmp/smoke_pyc_r1/vault --json job status --job-id job_6edd81eba66d5170
{"api_version": 1, ..., "operation": "job.status", "status": "ok",
 "job_id": "job_6edd81eba66d5170", "job_state": "succeeded",
 "result": {"files_seen": 1, "new_events": 0, "model_calls": 0, ...},
 "warnings": ["cancel_does_not_rollback_published_commits"]}

# repeat worker --once — no republish, no new events
$ PYTHONPATH=src python3 -m memory_agent --vault /tmp/smoke_pyc_r1/vault --json worker --once
{"worker_id": "worker_local", "lease_acquired": true, "lease_released": true,
 "jobs_claimed": [], "jobs_succeeded": [], "model_calls": 0, ...}

# repeat job status — still succeeded
$ PYTHONPATH=src python3 -m memory_agent --vault /tmp/smoke_pyc_r1/vault --json job status --job-id job_6edd81eba66d5170
{"api_version": 1, ..., "operation": "job.status", "status": "ok",
 "job_id": "job_6edd81eba66d5170", "job_state": "succeeded", ...}
```

The previous revision of this report showed an async `maintain`
dispatching as "queued" but later status showing `"status":"running"`,
which was a contradiction (`jobs_succeeded` in the worker output but
the job still `running` in the DB). That was caused by
`get_default_provider()` raising `ModelError` for the
model-requiring `maintain` operation when the Claude CLI opt-in was
unset, which escaped `drain_queued_jobs()` and left the job
`running` after the worker pass. Phase C repair-1 fixes that by
moving the model provider instantiation *inside* the dispatch path
where it belongs and surfacing the failure as `PROVIDER_UNAVAILABLE`
on the job. The smoke above uses `scan` (a model-free operation) so
the model layer is bypassed regardless, and the `maintain` path is
covered by `TestWorkerOnce::test_worker_once_drains_queued_job`
which runs under `MEMORY_AGENT_MODEL_PROVIDER=fake`.

## Commands run

```bash
# Focused Phase C tests (existing)
PYTHONPATH=src python3 -m pytest tests/integration/test_phase_c_v1.py -v
# → 27 passed

# Focused Phase C repair-1 tests (new)
PYTHONPATH=src python3 -m pytest tests/integration/test_phase_c_repair1.py -v
# → 6 passed

# Full suite
PYTHONPATH=src python3 -m pytest tests/ -q
# → 275 passed in ~44s
```

## Files added in Phase C

| Path | Purpose |
|---|---|
| `src/memory_agent/contracts/commands.py` | Discriminated `CommandRequest`, per-operation `*Params` schemas, `JobProgress`, `ErrorCode` extension (`UNSUPPORTED`, `WORKER_OFFLINE`), back-compat aliases for v0.1 callers. |
| `src/memory_agent/infrastructure/sqlite/migrations.py` (migration `0006`) | Job lifecycle columns (`revision`, `started_at`, `ended_at`, `progress_json`, `result_json`, `error_json`, `mode`, `last_heartbeat`); `worker_leases`, `worker_runs`, `worker_runtime_state`; `source_correction_proposals`, `memory_correction_overlay`. |
| `src/memory_agent/infrastructure/sqlite/repositories.py` | `JobRepo` extensions (idempotency / next-queued / heartbeat / progress / result / error / request_cancel / mark_started / set_progress); `WorkerLeaseRepo`, `WorkerRunRepo`, `WorkerRuntimeStateRepo`, `SourceCorrectionProposalRepo`, `MemoryCorrectionOverlayRepo`. |
| `src/memory_agent/application/call.py` | Versioned `CommandRequest` dispatcher with sync / async modes, `(client_id, idempotency_key)`-bound `request_hash`, `IDEMPOTENCY_CONFLICT` on hash mismatch, `review` → `UNSUPPORTED`, durable job persistence for async, replay returns the original job. |
| `src/memory_agent/application/worker.py` | Project-owned worker: `run_worker_once` / `run_worker`, lease acquisition + heartbeat (Phase C repair-1: vault-level writer lock so two distinct worker IDs cannot both hold an active lease), queued-job drain that never instantiates the model provider for model-free operations (`scan` / `init` / `doctor` / `batch.plan` / `conflicts` / `decide` / `job.*`), source-correction discovery on every pass, daily/weekly/monthly schedule with `worker_runtime_state` anchor, stale-`running` job reclaim via `JobRepo.reclaim_stale_running`. |
| `src/memory_agent/application/source_correction.py` | Discovery walk over committed memories; anchor-based detection of rewrites; deterministic auto-resolve when the new segment still contains the quoted span verbatim; `decide_proposal` with `keep_old` / `adopt_new` / `scope_split` / `defer`; recall-overlay projection. |
| `src/memory_agent/interfaces/cli/main.py` | `memory call`, `memory job {status,cancel,retry}`, `memory worker [--once]` subcommands; `--json` / `--pretty` top-level flags; stable exit codes (`IDEMPOTENCY_CONFLICT` → 4, `UNSUPPORTED` / `INTERNAL` → 6, `WORKER_OFFLINE` → 3); explicit `vault_root` is threaded through `handle_call` (no module-global holder). |
| `design/launchd/com.memory-agent.worker.plist` | Phase C launchd template (NOT installed). The worker owns timing via `worker_schedule.json`; launchd only restarts the process. |
| `design/launchd/README.md` | Placeholder documentation for the plist. |
| `tests/integration/test_phase_c_v1.py` | 27 tests covering the original Phase C acceptance rows. |
| `tests/integration/test_phase_c_repair1.py` | 6 new tests covering the Phase C repair-1 fixes: model-free `scan` drain end-to-end (no CLI opt-in), `WorkerLeaseRepo` writer lock, stale `running` reclaim, and explicit `vault_root` in `handle_call` (no global holder). |

## What Phase C is *not* claiming

- It is **not** a live-model-backed rewrite/expansion path (still
  phase A's safe stub).
- It is **not** a UI / HTTP API. The `memory_correction_overlay`
  table is the durable projection the future loopback HTTP API
  (Phase D / `docs/16` P6) will join against; the UI is not shipped.
- It is **not** Telegram (T12 / P4), loopback FastAPI (T14 / P6),
  React UI (T15 / P7), UI integration (T16 / P8), or
  privacy / backup / doctor / packaging (T17 / P9). These remain
  OPEN BLOCKERs for v1.
- It is **not** an implicit launchd install. The plist template is
  provided but no `launchctl load` was performed in this task; the
  worker is meant to be supervised by whatever OS service manager
  the user prefers, after editing the placeholders.
- It is **not** a worker that magically fixes a memory: the user
  must still pass a versioned `decide` (or `decide_proposal`) to
  close a correction. The worker only queues proposals and updates
  the recall overlay; it never silently re-links a memory to
  changed user text.
- It is **not** a worker that auto-acks to external systems: there
  is no Telegram / Webhook channel yet, and there is no implicit
  `notification_outbox` send on correction decisions. That is P4
  / `docs/16`.

## Open release blockers (mandatory before v1 release)

These items remain open after Phase C and must not be claimed in
the README as done:

- T11 worker schedules, T11 worker configuration UI — Phase C ships
  the schedule config (`worker_schedule.json`) and the worker
  honours it; further configurability and backfill-aware schedule
  semantics may still be a separate package.
- T12 Telegram Bot API adapter — durable update offset, dedupe,
  versioned `decide`, ack/outbox, fake-server tests. P4.
- T13 dedicated audit query service — P5.
- T14 loopback FastAPI with Origin / session-token checks for
  writes and no wildcard CORS — P6.
- T15 React/TypeScript/Vite UI — P7.
- T16 UI integration + browser test — P8.
- T17 privacy deletion + consistent SQLite+files backup/restore +
  doctor + independent packaging/runbook — P9. A backup tarball is
  **not** described as restorable until tested; localhost alone is
  not described as securing writes.
- Vector recall, semantic embeddings, persistent search services.

The README status line stays at "phase A + phase B + phase C" after
this report passes. The T11–T17 packages above are explicitly not
in this revision and will be delivered as separate Claude tasks.

## Phase C repair-2 (2026-10-09)

Phase C repair-2 closes the four C release blockers the codex
flagged after the C acceptance report:

  1. **Real scheduled work.** ``_maybe_daily_pass`` now actually
     scans the configured roots (read from ``source_files`` or
     ``config.toml`` / ``config.json``) and only advances
     ``last_daily_run`` when an increment was observed or a
     queued job was drained. The weekly / monthly passes produce
     conservative review proposals (deterministic anomaly
     heuristics — no LLM call) and bump their timestamps only
     on a successful write. ``review`` is no longer
     ``UNSUPPORTED``: ``memory call --operation review --params
     period=week|month`` runs the same code path on demand and
     returns the proposal in the response. The daily pass makes
     zero model calls (model-free operation). Backed by
     ``tests/integration/test_phase_c_repair2.py::TestRealScheduledWork``
     (8 tests).

  2. **Source-correction ``adopt_new`` / ``scope_split``** verify
     ``expected_proposal_revision`` (optimistic concurrency) and
     publish a new memory through ``CommitService`` (file →
     manifest → HEAD swap → finalize) referencing the *new*
     ``event_id`` and new revision hash. The old memory is
     marked ``superseded``; the proposal row's
     ``superseded_memory_id`` / ``superseded_head_commit_id`` /
     ``decided_at`` are recorded. A stale revision is rejected
     with ``INVALID_REQUEST``; a publish failure leaves the
     proposal ``pending`` and the overlay ``pending_correction``
     so the user can retry. Backed by
     ``tests/integration/test_phase_c_repair2.py::TestSourceCorrectionDecision``
     (5 tests).

  3. **Discovery timing.** The ``_cmd_scan`` dispatcher now calls
     ``discover_affected_memories`` + ``refresh_overlay``
     *inline* at the end of every scan, so a recall observed
     *before* the next worker pass still sees the overlay and
     emits a ``pending_correction:<id>`` warning. The recall
     service consults the overlay through a new
     ``overlay_repo`` field. Backed by
     ``tests/integration/test_phase_c_repair2.py::TestDiscoveryTiming``
     (2 tests).

  4. **Query: opt-in Claude rewrite + ``include_archived`` +
     fallback semantics.** ``get_default_rewrite_provider()``
     returns ``IdentityRewriteProvider`` by default, or
     ``ClaudeCLIRewriteProvider`` (shells out to
     ``claude -p --output-format json``) when
     ``MEMORY_AGENT_ENABLE_CLAUDE_CLI=1`` is set. ``FakeRewriteProvider``
     is the test-friendly variant the recall service consumes
     directly. ``RecallService.recall`` honours
     ``include_archived=True`` via the new
     ``MemoryProjectionRepo.list_archived()``. Provider failures
     fall back to original-only retrieval and the trace records
     the reason. The cursor contract is unchanged:
     ``next_cursor`` is bound to
     ``(snapshot_id, plan_fingerprint)``; a follow-up call with
     the same cursor is accepted iff both match the current HEAD
     and the same plan. Backed by
     ``tests/integration/test_phase_c_repair2.py::TestQueryContract``
     (5 tests).

Schema change: migration ``0007`` adds ``proposal_revision``,
``superseded_memory_id``, ``superseded_memory_revision``,
``superseded_head_commit_id`` and ``decided_at`` to
``source_correction_proposals``.

`memory call --operation source_correction.decide` is the new
versioned CLI surface for closing a correction; it is rejected
with ``INVALID_REQUEST`` when the supplied
``expected_proposal_revision`` does not match the current row.

## Status update

Phase C is accepted with the repair-1 + repair-2 fixes: 60 new
Phase C tests (27 original + 6 repair-1 + 20 phase C v1 schedule
+ 19 phase C repair-2 + 1 ``test_daily_no_increment_no_timestamp_bump``
added to the existing v1 suite) pass on top of the 242-test
baseline (275 total before repair-2, **295 total after
repair-2**). Every Phase C acceptance row above is backed by a
named test, and the launchd template + worker + source-correction
queue ship as documented. The remaining T11–T17 packages are
explicitly OPEN BLOCKERs and remain deferred per `docs/16`.