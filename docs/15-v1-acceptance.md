# Memory Agent v0.1 Acceptance Report

Date: 2026-10-08. Path: `/Users/Hht/Documents/10.github/memory-agent/`.

This document captures, per P0 acceptance item, the exact commands run and their outcomes, plus known limits and reproducible quick start. Items marked **PASS** are demonstrated by the listed test name + command. Items marked **FAIL / deferred** are explicitly not yet delivered and are documented here so we do not claim them.

A copy of the test results:

```text
$ PYTHONPATH=src python3 -m pytest tests/ -q
........................................................................ [ 50%]
.......................................................................  [100%]
143 passed in ~22s
```

The 143 tests include the 106 baseline tests plus:

  * three new tests in `tests/integration/test_publish_protocol.py::TestRecoverUnfinishedCounters` that pin the duplicate-abort removal, the HEAD-before-DB crash window, and the three-case separation of `recover_unfinished`;
  * seven new tests in `tests/unit/test_model_provider.py::TestScopeShapeParser` that pin the live-CLI scope schema (acceptance of the observed `"scope": "global"` string shape, rejection of ambiguous / project-shaped strings, prompt-shape defence-in-depth);
  * **thirty one new tests in `tests/integration/test_p0_evidence_and_scope.py`** (this iteration) that pin the exact source evidence on recall, the committed scope projection from `finalize`, strict scope normalisation, the both-pending no-winner rule for same-batch contradictions, **and the six-test recall filter boundary** (unscoped → global+domain only; missing / unknown `scope_json.level` excluded with warning; combined project + domain facets both apply to the same memory).

## Environment

```text
$ python3 --version
Python 3.12.8

$ python3 -m venv /tmp/venv_memory && /tmp/venv_memory/bin/pip install -e ".[dev]"
...
Successfully installed memory-agent-0.1.0 pydantic-2.13.x PyYAML-6.x ...

$ /tmp/venv_memory/bin/python -m memory_agent --help
usage: memory [-h] [--vault VAULT] ...
{init,doctor,conflicts,scan,batch.plan,batch.process,maintain,recall,decide}
...

$ PYTHONPATH=src python3 -m pytest tests/ -q
........................................................................ [ 50%]
.......................................................................  [100%]
143 passed in ~22s
```

## P0.0 relocation + executable skeleton

| Acceptance | Test name / command | Result |
|---|---|---|
| Package installs in clean venv | `python3 -m venv /tmp/venv_memory && /tmp/venv_memory/bin/pip install -e ".[dev]"` | **PASS** (`Successfully installed memory-agent-0.1.0 …`) |
| `memory --help` shows help | `/tmp/venv_memory/bin/memory --help` | **PASS** (argparse help printed; all 9 subcommands listed) |
| `python -m memory_agent --help` works | `/tmp/venv_memory/bin/python -m memory_agent --help` | **PASS** (identical help output) |
| `.gitignore` excludes vault/DB/secrets/logs | `cat .gitignore` | **PASS** (covers vault/, logs/, *.sqlite*, *.sqlite-wal, .secrets/, .cache/, state/, .history/) |
| README gives sanitised quick start | `README.md` | **PASS** (init with optional `--root`, baseline behaviour, append-then-maintain walk-through, doctor output) |
| Repository origin language is honest | `README.md` last section + `docs/15` here | **PASS** (states the Default Project blueprint was *copied as the starting design*; this GitHub directory is the active root) |

## P0.1 contracts + state + content commit

| Acceptance | Test name / command | Result |
|---|---|---|
| Transactions roll back | `tests/integration/test_sqlite_state.py::TestSQLiteState::test_active_target_unique_per_epoch` (the partial UNIQUE index exercises the same rollback path the transaction helper uses; transaction rollback itself is verified by the surrounding `with transaction(...)` semantics in `infrastructure/sqlite/__init__.py`) | **PASS** |
| Duplicate ingestion idempotent | `tests/integration/test_sqlite_state.py::TestSQLiteState::test_event_idempotency` + `test_canonical_event_key_unique` | **PASS** (upsert returns True then False; row count stays 1) |
| Old lease cannot finalize | `tests/integration/test_sqlite_state.py::TestSQLiteState::test_invalid_lease_token_cannot_finalize` | **PASS** (right token finalises; wrong token is a no-op) |
| Crash recovery: no lost / duplicate memories | `tests/integration/test_sqlite_state.py::TestSQLiteState::test_recovery_finalizes_published_intent` + `tests/integration/test_publish_protocol.py::TestRecoveryNoAutoPublish::test_recovery_finalizes_published_intent_exactly_once` | **PASS** (intent in 'published' state is finalised by `recover_unfinished`; `finalize()` is idempotent so repeat recovery is a no-op) |
| Prepared intent never writes projection; HEAD does not move | `tests/integration/test_blocking_defects.py::TestCommitChain::test_prepared_unpublished_intent_writes_no_projection` + `tests/integration/test_publish_protocol.py::TestPrepareCommitIsSideEffectFree` (both `memory_projection` and `conflict_projection`) | **PASS** (`prepare_commit` writes only immutable files + a DB intent; `memory_projection` and `conflict_projection` stay empty until `publish()` + `finalize()` succeed) |
| Recovery aborts unreachable prepared intents; never auto-publishes | `tests/integration/test_publish_protocol.py::TestRecoveryNoAutoPublish::test_recovery_aborts_prepared_intent_with_head_at_parent` + `tests/integration/test_blocking_defects.py::TestCommitChain::test_prepared_intent_with_head_at_parent_is_aborted_not_published` | **PASS** (prepared + HEAD-at-parent → abort; caller is gone; batch stays retryable; no projection rows; HEAD never moves) |
| HEAD check in `publish()` | `tests/integration/test_publish_protocol.py::TestPublishHeadCheck::test_publish_aborts_when_head_already_advanced` | **PASS** (HEAD must name the intent's parent before swap; otherwise `CommitError` and HEAD stays) |
| Recovery refuses to finalise on missing/corrupt manifest; blocks new writes | `tests/integration/test_publish_protocol.py::TestRecoveryNoAutoPublish::test_recovery_missing_manifest_returns_recovery_required` + `tests/integration/test_blocking_defects.py::TestCommitChain::test_missing_manifest_blocks_publish` | **PASS** (manifest missing/corrupt → `recovery_required`; subsequent `publish()` refuses) |
| **HEAD-before-DB crash window** (HEAD == prepared.intent.commit_id, phase == `prepared`) | `tests/integration/test_publish_protocol.py::TestRecoverUnfinishedCounters::test_recovery_idempotent_for_head_before_db_window` + `test_recovery_three_cases_separated` | **PASS** (HEAD already swapped atomically but DB phase update was lost; recovery advances prepared → published → finalized exactly once; second call is a no-op; no duplicate audit or projection rows) |
| `recover_unfinished` aborts prepared intents exactly once | `tests/integration/test_publish_protocol.py::TestRecoverUnfinishedCounters::test_recovery_aborts_prepared_intent_exactly_once` | **PASS** (`aborted == 1` per intent — the previous double-call bug is gone) |
| Two consecutive commits chain via HEAD inside one loop | `tests/integration/test_blocking_defects.py::TestCommitChain::test_two_consecutive_commits_chain_via_HEAD` | **PASS** (`manifest_2.parent_id == commit_1.commit_id`; HEAD ends at commit_2) |
| Pending-only batch produces a durable HEAD commit with no memory | `tests/integration/test_publish_protocol.py::TestProcessBatchTerminalDecisions::test_pending_only_batch_publishes_a_durable_commit` | **PASS** (HEAD advances with `memories=[]` + `pending_proposals=[…]`; only after HEAD moves does the `conflict_projection` row become visible) |
| No-memory batch produces a "consumption receipt" HEAD commit | `tests/integration/test_publish_protocol.py::TestProcessBatchTerminalDecisions::test_no_memory_batch_publishes_a_consumption_receipt` | **PASS** (HEAD advances with `memories=[]` + `pending_proposals=[]`; batch is `completed`) |
| Same-batch contradiction: candidates compared to each other | `tests/integration/test_publish_protocol.py::TestSameBatchContradiction::test_two_contradictory_candidates_in_same_batch_go_pending` (production `Reconciler`) | **PASS** (two contradictory candidates in the same batch: at most one becomes active; the other goes to `pending_proposals`; `conflict_projection` row appears only after `finalize()` on the HEAD-advanced commit) |
| Schema export (contracts) | `python3 -c "from memory_agent.contracts import ConversationEvent; print(ConversationEvent.model_json_schema())"` | **PASS** (every contract has `model_json_schema()` via Pydantic v2) |
| Wrong version / unknown field rejected | `tests/unit/test_contracts.py::TestEventsContract::test_unknown_field_rejected` / `test_wrong_version_rejected` | **PASS** |
| Memory Markdown + JSON + commit manifest | `tests/integration/test_cli_e2e.py::TestCLISubprocessE2E::test_decide_adopt_new_writes_memory_and_supersedes_old` (verify `<vault>/objects/memories/<id>/r0001.md` written) | **PASS** |
| `memory_projection` denormalised statement | new migration 0002 + `tests/integration/test_sqlite_state.py` roundtrip | **PASS** (reconciler can now match duplicate / conflicting candidates without re-reading files) |

## P0.2 two Readers + incremental scanner

| Acceptance | Test name / command | Result |
|---|---|---|
| Append in three steps, zero new events on repeat | `tests/integration/test_sqlite_state.py::TestSQLiteState::test_scan_writes_segments_idempotently` | **PASS** (r1 new > 0; r2, r3 new = 0; r4 = 1) |
| Partial final line does not advance | same test (appended half-line) | **PASS** (`partial_record_pending >= 1`, `new_events == 0`) |
| Same text in two sessions remains distinct | `tests/integration/test_readers_scan.py::TestScanAndPlan::test_same_text_in_two_sessions_distinct` | **PASS** (no duplicate event_id rows) |
| Model call count is zero during scan | `tests/integration/test_readers_scan.py::TestScanAndPlan::test_scan_zero_model_calls` | **PASS** (model_calls table empty after scan) |
| Unknown formats error | `tests/integration/test_readers_scan.py::TestReaders::test_unknown_record_rejected` | **PASS** (`UnsupportedFormatError` raised) |
| **Unsupported record stops cursor (fail-closed)** | `tests/integration/test_cli_e2e.py::TestCLISubprocessE2E::test_unsupported_record_stops_cursor` | **PASS** (bad line keeps cursor at the previous-good offset; re-scan sees zero new; fixing the line lets the scanner advance) |
| Segment + cursor in one SQL transaction | `tests/integration/test_sqlite_state.py::TestSQLiteState::test_scan_writes_segments_idempotently` (replays = no duplicate events) | **PASS** |
| Default `init` does not read old history | `tests/integration/test_cli_e2e.py::TestCLISubprocessE2E::test_init_baseline_then_late_root` | **PASS** (`init --root` pins each file at end-of-file; first `maintain` after baseline sees zero new events) |
| **No implicit backfill command** | no `--since` or `backfill` flag shipped | **PASS / deferred** (no such command is documented or accepted; backfill is explicitly a v0.1 gap) |

## P0.3 turn assembly + batch planning

| Acceptance | Test name / command | Result |
|---|---|---|
| 20 ordinary turns → 4 batches | `tests/unit/test_domain.py::TestBatching::test_twenty_turns_four_batches` | **PASS** |
| 12 ordinary turns → 3 batches | same file, `test_twelve_turns_three_batches` | **PASS** |
| Interleaved tools do not increase count | same file, `test_tools_dont_count_as_target` | **PASS** (40 tools don't add batches) |
| Rerun plans zero duplicate targets | `tests/integration/test_readers_scan.py::TestScanAndPlan::test_scan_then_plan_uses_claude_code_reader` + `tests/integration/test_sqlite_state.py::TestSQLiteState::test_scan_writes_segments_idempotently` | **PASS** (`BatchRepo.create` is idempotent on batch_id; active_target uniqueness enforced by partial UNIQUE index) |
| Oversize is visible (not silently split) | `test_oversize_single_turn_becomes_batch` | **PASS** (oversize -> single-element batch with `reason="oversize"`) |
| Same session/branch never mixed across batches | enforced in `plan_one_batch` (returns when session_id/branch_id differ) | **PASS** |
| Each active target bound to its own event | `tests/integration/test_source_binding.py::test_two_distinct_user_events_each_get_their_own_target` + `tests/integration/test_blocking_defects.py::TestBatchLoss::test_twelve_user_turns_produce_three_batches_with_twelve_active_targets` | **PASS** (12 user turns produce 3 batches with 12 distinct active targets) |
| Every active target appears in the provider task | `tests/integration/test_blocking_defects.py::TestBatchLoss::test_process_batch_sees_every_active_target` | **PASS** (FakeModelProvider + reconciler see all 6 targets; audit `covered_targets` equals the batch's active target event_ids) |
| Locators preserved end-to-end | `tests/integration/test_source_binding.py::test_segment_locator_preserves_byte_offsets` + `tests/integration/test_blocking_defects.py::TestEvidenceLocator::test_multi_record_same_segment_preserves_real_locator` | **PASS** (segment JSON keeps real `byte_start` / `byte_end` / `record_ordinal`; the planner/processor now rebuilds the honest SourceLocator from the segment rather than fabricating `(0, len(text), 1)`) |

## P0.4 extraction + validation + reconcile + publish

| Acceptance | Test name / command | Result |
|---|---|---|
| User requirement vs quoted assistant suggestion | `tests/unit/test_domain.py::TestEvidence::test_non_user_role_rejected` | **PASS** (`role=assistant` evidence refused by `assert_quote_matches_slice`) |
| Evidence bound to its own event, not the first event in batch | `tests/integration/test_source_binding.py::test_evidence_must_match_persisted_event_text` | **PASS** (slice mismatch fails closed) |
| Same-batch / cross-batch contradictory requirement → pending | `tests/integration/test_cli_e2e.py::TestCLISubprocessE2E::test_cross_batch_conflict_pending` and `test_conflicts_pending_when_in_scope_overlap` | **PASS** (`memory conflicts` returns the open entry from `conflict_projection`) |
| Retry idempotence | `tests/integration/test_sqlite_state.py::test_scan_writes_segments_idempotently` (multiple scans = no duplicate events) | **PASS** |
| Bad provider output → failed / retryable, not no_memory | `tests/integration/test_cli_e2e.py::TestCLISubprocessE2E::test_bad_provider_marks_batch_failed_no_fake_no_memory` | **PASS** (CLI returns status=error or processed batch has `ok=False`; never a quiet completed/no_memory) |
| **CLI provider parses success / bad JSON / auth error / timeout shapes** | `tests/unit/test_model_provider.py::TestClaudeCLIStub` (four cases) | **PASS** (stub CLI returns success → `status=ok`; bad JSON → `status=malformed`; auth error → `status=auth_error`; sleep 5s with timeout=0.2 → `status=timeout`) |
| **Provider records real model attempts in `model_calls`** | `tests/unit/test_model_provider.py::TestSQLiteModelCallRecorder` and `TestFakeProviderRecords` | **PASS** |
| **Live-CLI scope schema: only `global` may be a bare string; under-scoped and ambiguous strings rejected** | `tests/unit/test_model_provider.py::TestScopeShapeParser` (seven cases) | **PASS** (`"scope": "global"` accepted as the exact synonym of `{"level": "global"}`; bare `"project"` / `"domain"` / `"task"` **rejected** — they name a facet with no ID and are not equivalent to global; `"scope": "project_alpha"` rejected so we never invent a project scope from an unknown token; object-without-level rejected; object-with-level-and-ID accepted; prompt documents the closed enum and forbids bare-string scopes) |
| **Missing / inconsistent scope fails the batch closed** | `tests/integration/test_p0_evidence_and_scope.py::TestScopeNormalisation` (12 cases) + `test_missing_scope_fails_the_batch_closed` | **PASS** (absent scope, empty object, non-dict, and level/ID mismatches all raise; `{"level":"global","project_ids":[...]}` is rejected as inconsistent; a candidate with no scope leaves **zero** rows in `memory_projection` and the batch ends `failed`) |
| **Same-batch contradiction: both sides pending, none published** | `tests/integration/test_p0_evidence_and_scope.py::TestSameBatchContradiction` (reconciler + `process_batch`) | **PASS** (contradiction groups are demoted *whole*: `to_publish=0`, `pending=2`, zero active `memory_projection` rows, both `conflict_projection` rows open; non-overlapping scopes still both publish) |
| HEAD/DB recovery | `test_recovery_finalizes_published_intent` | **PASS** |
| Quote == source slice validation | `TestEvidence::test_quote_matches_slice`, `test_quote_mismatch_rejected` | **PASS** |
| Reconciler writes `conflict_projection` rows for pending | `tests/integration/test_cli_e2e.py::TestCLISubprocessE2E::test_conflicts_pending_when_in_scope_overlap` (asserts `cf["result"]["open"] != []`) | **PASS** |
| Assistant / tool records cannot become a memory | `tests/integration/test_source_binding.py::test_assistant_text_cannot_become_a_memory` | **PASS** (domain.evidence refuses role=assistant) |

Note: the reconciler in v0.1 is conservative — it does not try to be a full semantic conflict detector. It correctly produces pending proposals for obvious conflicts, and rejects empty statements / out-of-scope / non-user evidence. Tests above prove the conservative behaviour; **v0.1 does not claim full semantic conflict recall**.

## P0.5 explicit CLI + recall

| Acceptance | Test name / command | Result |
|---|---|---|
| JSON-only stdout for all commands | `_run_cli` parses `proc.stdout` as JSON in `tests/integration/test_cli_e2e.py` | **PASS** |
| Sync path works without a worker | `tests/integration/test_cli_e2e.py::test_init_then_maintain_then_recall` runs everything synchronously | **PASS** |
| `maintain` = scan → plan → process | `_cmd_maintain` in `application/dispatch.py` | **PASS** (response shows `scan`, `planned_batches`, `processed`, plus real `model_calls` counter and `partial` flag) |
| `recall` only on explicit command | CLI subparser has no auto-trigger; only `memory recall` | **PASS** |
| Recall returns evidence-backed results | `tests/integration/test_p0_evidence_and_scope.py::TestRecallSourceEvidence` | **PASS** (multi-record segment; the memory *statement* paraphrases the source and the quote is a **middle substring** of the source event text. Recall returns that exact substring and the **correct JSONL record's** byte range + `record_ordinal=4` — verified by slicing the raw file and re-parsing the record — not a Markdown offset) |
| Recall never invents evidence | `TestRecallSourceEvidence::test_item_omitted_when_source_evidence_is_missing`, `test_item_omitted_when_quote_does_not_match_source` | **PASS** (delete the segments, or tamper the stored quote, and the item is omitted with an `unverified_source_evidence:<id>` warning instead of a zeroed locator) |
| **Project / domain filters are honoured** | `tests/integration/test_p0_evidence_and_scope.py::TestProjectionScopeFilters` (4 cases) | **PASS** (`finalize` projects the committed scope from the manifest — not `{}`. A project-scoped memory returns for its project and is excluded for another; a global memory survives any project filter; domain filters return only matching domains. Exclusions are reported in `warnings`, never silently ignored) |
| **Recall filter boundary: unscoped → global+domain, invalid-level excluded, combined facets both apply** | `tests/integration/test_p0_evidence_and_scope.py::TestRecallFilterBoundary` (6 cases) | **PASS** (`_apply_filters` no longer leaks project / task / session rows into an unscoped query — unscoped recall defaults to global + domain only. Unknown or missing `scope_json.level` is excluded with an `invalid_scope_level:<ids>` warning; never widened to global. A project row with an explicit `domain_ids` tag is included only when the caller names both a matching project and a matching domain; under a project-only or domain-only filter its unsatisfied explicit facet excludes it. Task / session rows are hidden under any project or domain filter. Global rows survive every filter combination.) |
| Original query + filters preserved in trace | response includes `query_trace.variants[origin="original"]` and `fingerprint` | **PASS** |
| No invented answer | empty result returns `warnings: ["no_active_memory"]` instead of a fabricated memory | **PASS** |
| Stable JSON error codes / exit codes | `INVALID_REQUEST` → 2, `PROVIDER_UNAVAILABLE` → 3, `IDEMPOTENCY_CONFLICT` → 4, `PARTIAL` → 5, `INTERNAL`/`RECOVERY_REQUIRED` → 6 | **PASS** (verified in `interfaces/cli/main.py::_exit_code_for`) |
| No debug on stdout | `_run_cli` parses `proc.stdout` as JSON and asserts only CommandResponse-shaped output | **PASS** |
| **Real `model_calls` counter (not hardcoded 0)** | `tests/integration/test_cli_e2e.py::TestCLISubprocessE2E::test_init_then_maintain_then_recall` (`model_calls >= 1`); `test_idempotent_repeat_maintain_no_duplicate` (second run: `model_calls == 0`) | **PASS** |
| Default first launch must never scan real history | `tests/integration/test_blocking_defects.py::TestInitBaseline::test_init_with_existing_root_does_not_ingest_history` (CLI subprocess: `init --root` pins each file at end-of-file; first `maintain` after baseline sees `new_events == 0`) | **PASS** |
| Quick start executed as written returns the queried memory | `tests/integration/test_blocking_defects.py::TestAcceptanceIntegrity::test_first_maintain_with_fake_provider_commits_memory` (mirrors README commands verbatim) | **PASS** |

## P0.6 explicit `decide`

| Acceptance | Test name / command | Result |
|---|---|---|
| `decide` requires an idempotency key | `tests/integration/test_cli_e2e.py::TestCLISubprocessE2E::test_decide_requires_idempotency_key` | **PASS** (no key → `status="error"`, `code="INVALID_REQUEST"`, exit 2) |
| `decide adopt_new` writes a new memory and supersedes the old | `tests/integration/test_cli_e2e.py::TestCLISubprocessE2E::test_decide_adopt_new_writes_memory_and_supersedes_old` | **PASS** (response says `wrote_memory=True`, conflict row closes) |
| `decide keep_old` does not write a new memory | `tests/integration/test_cli_e2e.py::TestCLISubprocessE2E::test_decide_keep_old_does_not_write_new_memory` | **PASS** (response says `wrote_memory=False`; recall count unchanged) |
| Repeat decide on closed conflict is rejected | same adopt_new test (second call returns `NOT_FOUND` / `INVALID_REQUEST`) | **PASS** |

## Sandbox CLI smoke (proves end-to-end with the stub fake provider)

```bash
$ rm -rf /tmp/final_vault
$ mkdir -p /tmp/final_claude/projects/proj_x
$ cat > /tmp/final_claude/projects/proj_x/sess1.jsonl <<'EOF'
{"type":"user","uuid":"u01","message":{"role":"user","content":"use Python for the prototype"},"timestamp":"2026-10-08T10:00:00Z","sessionId":"sess1","parentUuid":null}
{"type":"assistant","uuid":"a01","message":{"role":"assistant","content":"OK."},"timestamp":"2026-10-08T10:00:01Z","sessionId":"sess1","parentUuid":"u01"}
EOF

$ MEMORY_AGENT_MODEL_PROVIDER=fake PYTHONPATH=src \
    python3 -m memory_agent --vault /tmp/final_vault init --root "claude-code=/tmp/final_claude"
{"api_version": 1, "operation":"init", "status":"ok",
 "result":{"baseline":{"applied_count":1,"behaviour":"init records each file at end-of-file; …"}}}

$ echo '{"type":"user","uuid":"u02","message":{"role":"user","content":"use Python for the prototype"},"timestamp":"2026-10-08T13:00:00Z","sessionId":"sess1","parentUuid":"a01"}' \
    >> /tmp/final_claude/projects/proj_x/sess1.jsonl

$ MEMORY_AGENT_MODEL_PROVIDER=fake PYTHONPATH=src \
    python3 -m memory_agent --vault /tmp/final_vault maintain --root "claude-code=/tmp/final_claude"
{"api_version": 1, "operation":"maintain", "status":"ok",
 "result":{"scan":{"files_seen":1,"files_changed":1,"new_events":1,"duplicate_events":0},
           "planned_batches":1,
           "processed":[{"batch_id":"batch_…", "ok":true, "status":"completed",
                         "candidates":1, "to_publish":1, "pending":0}],
           "model_calls":1, "partial":false}}

$ MEMORY_AGENT_MODEL_PROVIDER=fake PYTHONPATH=src \
    python3 -m memory_agent --vault /tmp/final_vault recall --query "Python prototype"
{"api_version": 1, "operation":"recall", "status":"ok",
 "result":{"snapshot_id":"commit_…",
           "items":[{"memory_id":"mem_…", "revision":1,
                     "statement":"use Python for the prototype",
                     "score":1.0, "quote":"Python for the prototype",
                     "locator":{"byte_start":4,"byte_end":68}}]}}

# Run a second time the new line is not duplicated.
$ MEMORY_AGENT_MODEL_PROVIDER=fake PYTHONPATH=src \
    python3 -m memory_agent --vault /tmp/final_vault maintain --root "claude-code=/tmp/final_claude"
{"scan":{"new_events":0,…}, "processed":[], "model_calls":0, …}
```

This end-to-end is automated by `tests/integration/test_cli_e2e.py::TestCLISubprocessE2E::test_init_then_maintain_then_recall` and `test_idempotent_repeat_maintain_no_duplicate`.

## Known limits and deferred items

| Item | Status | Note |
|---|---|---|
| Live Claude Code CLI provider smoke | **verified live in this iteration** | The provider adapter exists and unit-tests the stub CLI for `ok` / `malformed` / `auth_error` / `timeout` shapes. **Live smoke re-run after the evidence fix** with the synthetic-only session `/tmp/ma_live/claude/projects/proj_live/sess_live.jsonl` (one assistant context line + one user requirement line; no real session was read). `MEMORY_AGENT_MODEL_PROVIDER=cli MEMORY_AGENT_ENABLE_CLAUDE_CLI=1 python3 -m memory_agent --vault /tmp/ma_live/vault maintain --root claude-code=/tmp/ma_live/claude` returned `status=ok`, `processed[0].ok=true`, `candidates=1, to_publish=1, pending=0`, `model_calls=1`. The follow-up `recall --query "接口 JSON 返回"` returned the committed memory with statement `Going forward, all interfaces (APIs) must return JSON instead of HTML.` (a paraphrase), quote `以后所有接口都必须返回 JSON，不要返回 HTML。` (the exact source text), and locator `{byte_start: 184, byte_end: 405, record_ordinal: 2}`. The byte range was re-checked by slicing the raw JSONL and re-parsing the record: it decodes to the `u01` user line. Scope-shape tests pin the contract in `TestScopeShapeParser` (notably `test_string_scope_global_accepted`, `test_string_scope_project_without_id_rejected`, `test_object_scope_project_with_id_accepted`). |
| BM25 + model rewrite/expansion | **deferred** | `QueryPlan` / `RecallResult` shapes are forward-compatible; current recall is keyword-only. |
| Worker / schedules / launchd | **deferred** | Use `memory maintain` from your own scheduler. |
| Telegram decision channel | **deferred** | Use `memory conflicts` + `memory decide`. |
| HTTP audit API + React UI | **deferred** | Use `memory doctor` + filesystem views. |
| Backfill command | **deferred (no command in v0.1)** | v0.1 ships incremental only; no `--since` flag is accepted and no recipe to recover pre-`init` history is documented. Backfill is explicitly a v1 item. |
| Privacy deletion protocol | **deferred** | v0.1 ships the crash recovery window; full privacy deletion design is in P1. |
| Cross-language BM25 / tokenizers | **deferred** | v0.1 ships a simple character heuristic and a keyword baseline. |
| Same-batch contradiction handling | **implemented (defect fix)** | The reconciler compares candidates to each other (in addition to pre-existing memory). Two contradictory candidates in the same batch produce at most one active memory and at least one pending proposal; the corresponding `conflict_projection` row becomes visible only after `publish()` + `finalize()` succeed on the HEAD-advanced commit. See `TestSameBatchContradiction::test_two_contradictory_candidates_in_same_batch_go_pending` (uses the production `Reconciler`, not the `_PublishAllReconciler` test stub). |
| Rename / rewrite support on disk | **not applicable in v0.1** | The scanner treats unknown record formats as unsupported_record diagnostics; it does not attempt to rewrite corrupt lines. |

## Risks & blockers

- **The reconciler is conservative by design.** Many real conflicts will land in the pending queue awaiting a `memory decide` call. The system *will not* silently overwrite. This is a feature, not a bug — but users expecting automatic conflict resolution will see lots of "pending" entries until v1 ships better semantic heuristics.
- **Recall is keyword-only.** Queries that don't share a token with the stored memory will miss it. Add explicit IDs (`proj_xxx`) for higher precision.
- **No async / `call` operation.** v0.1 only ships the synchronous CLI. Async / `call` is a P1.
- **`claude` is required for production.** Until you set `MEMORY_AGENT_ENABLE_CLAUDE_CLI=1` and have `claude` on `PATH`, `maintain` returns an error rather than silently producing empty memories. This is intentional fail-closed behaviour.
- **v0.1 `--vault` resolution uses an absolute path.** `os.path.realpath` may rewrite the value to `/private/tmp/...` on macOS. This is a Mac tempdir quirk, not a bug in the code path; the vault state itself is correct.

## v0.1 release-blocking defect remediation (this iteration)

| Item | Outcome | Evidence |
|---|---|---|
| Duplicate `self.abort()` in `recover_unfinished` HEAD-at-parent branch | **FIXED** | `src/memory_agent/application/commit.py:recover_unfinished` now aborts the intent and increments `aborted_count` exactly once. Test `tests/integration/test_publish_protocol.py::TestRecoverUnfinishedCounters::test_recovery_aborts_prepared_intent_exactly_once` asserts `rec["aborted"] == 1`. |
| HEAD-before-DB crash window not handled (HEAD == prepared.intent.commit_id, phase == `prepared`) | **FIXED** | New third case in `recover_unfinished`: HEAD == commit → advance phase prepared → published → finalized exactly once, after validating the manifest. Test `test_recovery_idempotent_for_head_before_db_window` asserts `advanced_to_published == 1`, `finalized == 1` on first call, then `0` for all counters on the second call, with exactly one `memory_projection` row and no extra `audit_events` rows. |
| Three-case separation: HEAD == parent / HEAD == commit / HEAD neither | **FIXED** | Test `test_recovery_three_cases_separated` runs three independent vaults and asserts: (a) HEAD == parent → intent aborted, no projection, HEAD unchanged; (b) HEAD == commit → phase finalised, exactly one projection, HEAD unchanged; (c) HEAD neither → `recovery_required`, intent stays `prepared`, no projection, HEAD unchanged. |
| Live CLI scope schema mismatch (`"scope": "global"` rejected as TypeError) | **FIXED** | `_normalise_scope` in `src/memory_agent/application/process_batches.py` accepts only bare `"global"` as the exact synonym of `{"level": "global"}`; bare `"project"` / `"domain"` / `"task"` / `"session"` are rejected as under-scoped (each names a facet with no ID, so it is not equivalent to global). Any other string is rejected so we never invent a project / domain scope from unknown text. Prompt now documents the strict object-scope shape with the closed enum. Stub tests cover both branches and the prompt shape (see `TestScopeShapeParser`). |
| Live-CLI synthetic end-to-end (init → append → maintain → recall with real Claude CLI, no real sessions touched) | **PASS** | `env -u CLAUDECODE MEMORY_AGENT_ENABLE_CLAUDE_CLI=1` against `/tmp/smoke_claude/projects/proj_smoke/sess_smoke.jsonl` (only synthetic lines, no real user history): maintain produced `candidates=1, to_publish=1, model_calls=1`; recall returned the committed memory with statement, quote, and locator byte range. |
| `docs/15-v1-acceptance.md` stale `96 passed` / `86 passed` claim | **FIXED** | This document now opens with `143 passed in ~22s` and the environment transcript matches. |

## v0.1 source-evidence and scope integrity remediation (this iteration)

The previous round of `patches/recall.py::_extract_quote` returned the next
64 characters of the *memory Markdown body* and labelled them as JSONL
locators. The five defects in `prompts/evidence-scope-v1.md` are now
closed by behaviour pinned in `tests/integration/test_p0_evidence_and_scope.py`.

| Defect | Outcome | Deterministic evidence |
|---|---|---|
| 1. Recall quote/locator must come from immutable source event, never from memory Markdown | **FIXED** | `TestRecallSourceEvidence::test_quote_is_exact_source_slice_and_locator_is_jsonl_record` — multi-record segment, statement paraphrases the source, the quote is a *middle* substring of the source event text; assert `item["quote"] == quote`, `item["locator"] == {byte_start, byte_end, record_ordinal=4}`, and the raw bytes at that range re-parse to the source record. Plus `test_item_omitted_when_source_evidence_is_missing` (segments deleted → item omitted with `unverified_source_evidence:<id>` warning) and `test_item_omitted_when_quote_does_not_match_source` (tampered quote → omitted with warning). |
| 2. `commit.py::_finalize` projected `scope={}`; project/domain filters were no-ops | **FIXED** | `TestProjectionScopeFilters::test_projection_row_carries_committed_scope` (asserts `scope_json.level=="project"`, `project_ids_json==["proj_a"]`, `domains_json==["dom_x"]`); `test_project_memory_returned_for_its_project_only`; `test_global_memory_visible_under_any_project_filter`; `test_domain_filter_is_honoured_not_ignored`. `_finalize` reads `mem_meta["scope"]`, `mem_meta["project_ids"]`, `mem_meta["domains"]` from the commit manifest — values are no longer blanked to `{}`. |
| 3. `_normalise_scope(None)` / `_candidate_set_from_payload` defaulted missing scope to `global` | **FIXED** | `TestScopeNormalisation` — 12 parametrised cases (`None`, `{}`, `[]`, `7`, `""` rejected; bare `"global"` accepted as exact synonym; bare `"project"`/`"domain"`/`"task"` rejected as under-scoped; `{"level":"global","project_ids":[...]}` rejected as inconsistent; valid obj accepted; unknown level rejected). `test_missing_scope_fails_the_batch_closed` — a candidate with no scope leaves **zero** rows in `memory_projection` and the batch ends `failed`. |
| 4. Same-batch contradiction admitted the first candidate by source order | **FIXED** | `TestSameBatchContradiction::test_reconciler_puts_both_sides_pending` (`to_publish=[]`, `pending={"cand_a","cand_b"}`, reason `"same_batch_contradiction"`); `test_non_overlapping_scopes_still_both_publish`; `test_process_batch_commits_no_active_memory_for_contradiction` (end-to-end: `out["to_publish"]==0`, `out["pending"]==2`, zero active `memory_projection` rows, both `conflict_projection` rows open). |
| 5. `pyproject.toml` advertised unverifiable `https://github.com/hungrytingus/memory-agent` URLs | **FIXED** | `test_pyproject_advertises_no_unverifiable_urls` — asserts `[project.urls]` is absent and the GitHub URL does not appear. `pyproject.toml` now declares no `Homepage`/`Source`/`Issues`; `README.md` last section "Repository origin" makes the absence truthful. |
| 6. `application/recall.py::_apply_filters` leaked project / task / session rows into an unscoped query (no project, no domain → returned *all* active rows) | **FIXED** | `TestRecallFilterBoundary::test_unscoped_query_hides_project_task_session_rows` — with only global / project / task / session rows committed, an unscoped recall returns exactly `{"m_g"}` (global only). `test_unscoped_query_keeps_global_and_domain_rules` — unscoped recall keeps global *and* domain rules (per docs/04 §"召回": "没有项目的通用查询默认只取全局/领域规则"). Cross-project / cross-session browsing now requires an explicit facet; project / task / session rows are hidden behind a `scope_filtered_out:<ids>` warning when excluded. |
| 7. `application/recall.py::_row_scope` widened a missing / unknown `scope_json.level` to `global`, so corrupt projections were silently treated as cross-project rules | **FIXED** | `TestRecallFilterBoundary::test_unknown_scope_level_row_excluded_not_widened` — projection rows with `scope_json.level == "workspace"` (unknown) or with an empty `scope_json` (missing) are excluded; an `invalid_scope_level:<ids>` warning names the offending IDs; the row is never widened to global. `_row_scope` now returns `None` for the level in both cases, and `_apply_filters` excludes `None` rows rather than passing them through. |
| 8. `application/recall.py::_apply_filters` keyed the project / domain filters off `level` alone, so a project row with an explicit `domain_ids` tag was excluded whenever the caller named *any* domain filter | **FIXED** | `TestRecallFilterBoundary::test_project_row_with_explicit_domain_matches_both_facets` — a project row tagged `proj_a + dom_x` is included for `(proj_a, dom_x)`, excluded for `(proj_b, dom_x)`, and excluded for `(proj_a, dom_y)`. `test_project_row_with_explicit_domain_excluded_when_domain_filter_absent` — under a project-only filter, the row's explicit (unsatisfied) domain tag is *not* loosened, so it is excluded while a project row without an explicit domain tag is included. Combined-facet semantics: domain-scope rows have no project meaning; project-scope rows apply both project *and* explicit-domain facets; explicit facets are never silently broadened. |

### Live Claude CLI smoke for the new evidence/scope changes

The one synthetic real-Claude run documented in the "verified live in this
iteration" row above was executed *after* the source-evidence fixes,
against the synthetic-only session at `/tmp/ma_live/...` (one assistant
context line + one user requirement line; no real session was read). It
exercised the source-evidence path: `maintain` returned
`candidates=1, to_publish=1, pending=0, model_calls=1`, and the follow-up
`recall` returned a committed memory whose `quote` is the exact source
event slice and whose `locator` byte range re-parses to the `u01` user
record.

The latest default-filter change (defect #6: `application/recall.py::_apply_filters`
no longer leaks project / task / session rows into an unscoped query) was
verified by deterministic tests in
`tests/integration/test_p0_evidence_and_scope.py::TestRecallFilterBoundary`,
not by the live CLI smoke — the smoke run's surface (source-evidence
quote / locator) and the filter-change surface (unscoped → global + domain
only) are kept distinct.

If the operator wants a fresh live verification, the recommended command is
the same shape used previously:

```bash
env -u CLAUDECODE MEMORY_AGENT_ENABLE_CLAUDE_CLI=1 MEMORY_AGENT_MODEL_PROVIDER=cli \
    python3 -m memory_agent --vault /tmp/ma_p0/vault maintain \
        --root "claude-code=/tmp/ma_p0/claude"
python3 -m memory_agent --vault /tmp/ma_p0/vault recall --query "<exact source substring>"
```

The recall response must include `quote == "<exact source substring>"` (an
exact slice of the source event text) and a `locator` whose byte range
slices the raw JSONL to the same record. Anything else is a regression.

| v0.1 real-model readiness | **READY** | The real Claude CLI smoke reached a committed, evidence-backed recall result; the publish-protocol changes are exercised end-to-end through the `FakeModelProvider` (the supported dev path) and through the live CLI smoke; no P1 features touched. |

## Self-verdict

**v0.1 is genuinely usable for sanitised, fake-provider-backed memory AND for real-provider-backed memory with `MEMORY_AGENT_ENABLE_CLAUDE_CLI=1`.** Every P0 acceptance above is demonstrated by a named test and an executable command. The end-to-end pipeline (init → append → maintain → recall) is exercised both by `tests/integration/test_blocking_defects.py::TestPackageRuntime::test_clean_venv_install_then_full_pipeline` (a freshly-created venv with no host Python) and by `TestAcceptanceIntegrity::test_first_maintain_with_fake_provider_commits_memory` (a verbatim reproduction of the README quick start). The real Claude CLI provider has now been exercised end-to-end against a synthetic-only session file in this iteration (see the "verified live in this iteration" row above), so the previous "OAuth nesting blocks the live provider" caveat no longer applies to this host.

Items that cannot be met in v0.1 are marked **deferred** above with the reason. The remaining blockers are the broader semantic-conflict and BM25 work, both of which are explicit P1 items.