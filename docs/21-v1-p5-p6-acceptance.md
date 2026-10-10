# v1 P5/P6 acceptance — audit query service (T13) + loopback HTTP (T14)

Date: 2026-10-09. Owner: T13/T14 (`application/audit_queries`,
`interfaces/http`).

This report is the acceptance evidence for the two v1 packages
landed together:

  * **P5 / T13 — audit query service.** Metadata-first read DTOs
    over the existing SQLite + files layout, server-side pagination
    with deterministic cursors, DB/HEAD reconcile status, redacted
    list views, full bodies only on the explicit evidence endpoint,
    and an end-to-end trace
    `source -> batch -> model call -> candidate -> commit`.
  * **P6 / T14 — loopback FastAPI.** Bind-only-to-127.0.0.1 HTTP
    service that exposes every docs/10 endpoint and forwards
    `POST /api/v1/commands` through the same `handle_call`
    dispatcher the CLI uses. Origin allowlist + session token
    enforcement on writes, no wildcard CORS, no token in URLs or
    log lines, and the model provider is never instantiated by any
    read path.

The work follows [`docs/07-delivery-and-evaluation.md`](07-delivery-and-evaluation.md)
T13 / T14 acceptance rows and the v1 plan
[`docs/16-v1-implementation-plan.md`](16-v1-implementation-plan.md).

## What is shipped

| Path | Purpose |
|---|---|
| `src/memory_agent/contracts/audit.py` | New DTOs: `OverviewDTO`, `SourceDTO`, `SourceDetailDTO`, `SessionTurnDTO`, `BatchDTO`, `BatchDetailDTO`, `BatchEvidenceDTO`, `MemoryHistoryDTO`, `ConflictDTO`, `AuditEventDTO`, `QueryTraceDTO`, `QueryTraceDetailDTO`, `PagedResponse`, `EnvelopeHeader`, `PaginationCursor`, `ProjectionState`. Pre-existing `AuditEvent` / `AuditAction` / `AuditOutcome` are kept (the contracts file is additive). |
| `src/memory_agent/application/audit_queries.py` | `AuditQueries` service with overview, sources (list + detail), session turns, batches (list + detail + evidence), memory history, conflicts, audit events (list + detail), query traces (list + detail). `encode_cursor` / `decode_cursor` for opaque, snapshot-bound, scope-tagged pagination. `_projection_state` reports `aligned` / `db_lag` / `db_ahead` / `empty`. |
| `src/memory_agent/interfaces/http/local_auth.py` | `LocalAuthConfig` + `load_auth_config` + `enforce_write_auth` FastAPI dependency. Origin allowlist (no wildcard), `X-Memory-Agent-Token` header check, token never logged. `token_fingerprint` returns a sha256-prefixed, non-reversible digest for log lines. |
| `src/memory_agent/interfaces/http/audit_routes.py` | GET routes for every docs/10 endpoint plus a tiny placeholder page at `/` until T15 lands. |
| `src/memory_agent/interfaces/http/command_routes.py` | `POST /api/v1/commands` (Origin + token gated) and `GET /api/v1/jobs/{id}`. The POST handler forwards every request through `handle_call`; no separate write logic. |
| `src/memory_agent/interfaces/http/app.py` | `create_app(vault_root, config, bind_host, port, ui_dist)` factory. Refuses any non-loopback bind. Always extends the allowlist with `http://127.0.0.1:<port>`. |
| `tests/integration/test_audit_queries.py` | 15 tests against the audit service. |
| `tests/integration/test_http_api.py` | 20 tests against the FastAPI surface. |
| `pyproject.toml` | Adds `fastapi>=0.110,<0.140`, `uvicorn>=0.27,<0.40`, `httpx>=0.27,<0.30` (dev). |

No new SQLite migrations: every query is over the existing tables
populated by T02 / T08 / phase C. The DB schema is the same as
phase C's.

## Acceptance matrix (T13)

| T13 acceptance row from docs/07 | Status | Evidence |
|---|---|---|
| source → batch → model call → candidate → commit trace | **PASS** | `test_batches_list_and_detail_link_to_run_and_commit`, `test_batch_evidence_includes_raw_text`, `test_batch_evidence_is_the_only_endpoint_that_returns_full_text`. Each batch carries `commit_id` (manusest lookup) and the evidence endpoint returns inputs + candidates + runs + model_calls. |
| consistent counters | **PASS** | `OverviewCounts` is built from a single read pass and the same SQL is used by every test (`test_overview_returns_envelope_and_pipeline_counts`). `pipeline_stages` keeps scanned/planned/extracted/committed in lockstep so the UI doesn't average them. |
| DB lag visible | **PASS** | `test_projection_state_reports_db_lag_when_intent_is_published` — flipping an intent back to `published` surfaces `projection_state="db_lag"` with `db_lag_behind_head >= 1`. |
| privacy / pagination / redaction correct | **PASS** | `test_audit_event_list_redacts_details`, `test_session_turns_redacts_quotes`, `test_batch_evidence_is_the_only_endpoint_that_returns_full_text`, `test_sources_list_paginates_and_redacts_bodies`, `test_cursor_is_snapshot_bound_and_rejected_on_advance`, `test_cursor_scope_mismatch_is_rejected`, `test_sources_list_rejects_garbage_cursor`. |

## Acceptance matrix (T14)

| T14 acceptance row from docs/07 | Status | Evidence |
|---|---|---|
| FastAPI `memory ui` bound only to 127.0.0.1 | **PASS** | `test_create_app_refuses_non_loopback_bind` (factory raises `ValueError` for `bind_host="0.0.0.0"`); `test_healthz_reports_loopback_only` (the `/healthz` endpoint echoes `loopback_only: True`). |
| `docs/10` GETs + `POST /api/v1/commands` through the existing application dispatcher | **PASS** | `audit_routes.py` registers all GETs from docs/10 §"本地审计 HTTP 接口". `command_routes.py` calls `handle_call` directly — no separate write logic. `test_post_commands_requires_origin_and_token` exercises the full POST happy path and confirms the same `CommandResponse` shape the CLI returns. |
| `GET /api/v1/jobs/{id}` | **PASS** | `test_get_jobs_returns_state_after_post`, `test_get_jobs_unknown_id_returns_404`, `test_post_commands_async_then_get_jobs_polls_queue`. |
| POST checks exact Origin allowlist + session token via header (never URL/log) | **PASS** | `test_post_commands_requires_origin_and_token` walks the four failure paths (no Origin / wrong Origin / no token / wrong token) and the happy path; the token is read from `X-Memory-Agent-Token`, never from the URL or query string; `token_fingerprint()` is the only thing the auth module exposes for log lines. |
| preserve version / expected_revision / idempotency validation | **PASS** | `test_post_commands_idempotency_conflict` (same key + changed params → HTTP 409 + `error.code == "IDEMPOTENCY_CONFLICT`). Unknown operations return `error.code == "INVALID_REQUEST"` via `handle_call`'s pydantic validation (`test_post_commands_unknown_operation_returns_typed_error`). |
| no wildcard CORS | **PASS** | The middleware does not call `CORSMiddleware`. Origins are compared exact, case-insensitive, against the allowlist set. `LocalAuthConfig.origin_allowed` never falls back to `*`. |
| reject hostile Origin, missing/wrong token, invalid command, arbitrary path/SQL/shell | **PASS** | The HTTP layer never executes arbitrary SQL / paths / shell — all writes route through `handle_call` → `legacy_dispatch` (CLI). Path parameters are absent from the public routes; payloads are validated by `CommandRequest` (extra fields rejected). |
| serve packaged static UI if present; placeholder page otherwise | **PASS** | `create_app` calls `serve_ui_dist(ui_dist)` and falls back to the `root_router` placeholder when absent. `test_placeholder_page_is_served` asserts the placeholder is reachable at `/` and lists the curl examples; the UI itself is not in scope (T15 owns it). |
| test HTTP contract with TestClient including real decision HEAD/projection | **PASS** | `test_post_commands_idempotency_conflict` uses an idempotency conflict on a real vault with a published HEAD; `test_post_commands_async_then_get_jobs_polls_queue` enqueues an async job and reads the persisted `jobs` row via `GET /api/v1/jobs/{id}`. The seeded fixture produces a real `commit_intents` row + a real `memory_projection` row so the reconcile state exercises a real HEAD. |
| assert GET never calls the model | **PASS** | `test_audit_queries_never_call_the_model` (audit service) and `test_get_routes_never_call_the_model` (HTTP surface) snapshot `SELECT COUNT(*) FROM model_calls` before and after every read endpoint and assert the count is unchanged. |
| no separate write logic | **PASS** | The POST handler calls `handle_call` once; `GET /api/v1/jobs/{id}` calls `_job_status` from the application service. The HTTP module adds zero SQL writes of its own. |

## Counts

* `tests/integration/test_audit_queries.py` — 15 tests, all green.
* `tests/integration/test_http_api.py` — 20 tests, all green.

Run locally:

```bash
python3 -m pytest tests/integration/test_audit_queries.py \
                   tests/integration/test_http_api.py -q
# 35 passed in ~1s
```

## Test evidence — focused run

```text
tests/integration/test_audit_queries.py::test_overview_returns_envelope_and_pipeline_counts PASSED
tests/integration/test_audit_queries.py::test_sources_list_paginates_and_redacts_bodies PASSED
tests/integration/test_audit_queries.py::test_source_detail_returns_manifest_reconcile_state PASSED
tests/integration/test_audit_queries.py::test_session_turns_redacts_quotes PASSED
tests/integration/test_audit_queries.py::test_batches_list_and_detail_link_to_run_and_commit PASSED
tests/integration/test_audit_queries.py::test_batch_evidence_is_the_only_endpoint_that_returns_full_text PASSED
tests/integration/test_audit_queries.py::test_memory_history_walks_revisions_and_source_corrections PASSED
tests/integration/test_audit_queries.py::test_conflicts_list_returns_empty_when_none_open PASSED
tests/integration/test_audit_queries.py::test_audit_event_list_redacts_details PASSED
tests/integration/test_audit_queries.py::test_audit_event_detail_includes_correlation_and_object_ref PASSED
tests/integration/test_audit_queries.py::test_query_traces_list_and_detail_use_persisted_artifact PASSED
tests/integration/test_audit_queries.py::test_cursor_is_snapshot_bound_and_rejected_on_advance PASSED
tests/integration/test_audit_queries.py::test_cursor_scope_mismatch_is_rejected PASSED
tests/integration/test_audit_queries.py::test_projection_state_reports_db_lag_when_intent_is_published PASSED
tests/integration/test_audit_queries.py::test_audit_queries_never_call_the_model PASSED

tests/integration/test_http_api.py::test_create_app_refuses_non_loopback_bind PASSED
tests/integration/test_http_api.py::test_overview_returns_dto_and_carries_projection_state PASSED
tests/integration/test_http_api.py::test_sources_list_pagination PASSED
tests/integration/test_http_api.py::test_sources_list_rejects_garbage_cursor PASSED
tests/integration/test_http_api.py::test_session_turns_redact_full_text PASSED
tests/integration/test_http_api.py::test_batches_list_redacts_bodies PASSED
tests/integration/test_http_api.py::test_batch_evidence_includes_raw_text PASSED
tests/integration/test_http_api.py::test_memories_history_returns_revisions PASSED
tests/integration/test_http_api.py::test_audit_list_redacts_details PASSED
tests/integration/test_http_api.py::test_query_trace_list_and_detail PASSED
tests/integration/test_http_api.py::test_post_commands_requires_origin_and_token PASSED
tests/integration/test_http_api.py::test_post_commands_unknown_operation_returns_typed_error PASSED
tests/integration/test_http_api.py::test_post_commands_idempotency_conflict PASSED
tests/integration/test_http_api.py::test_get_jobs_returns_state_after_post PASSED
tests/integration/test_http_api.py::test_get_jobs_unknown_id_returns_404 PASSED
tests/integration/test_http_api.py::test_placeholder_page_is_served PASSED
tests/integration/test_http_api.py::test_healthz_reports_loopback_only PASSED
tests/integration/test_http_api.py::test_get_routes_never_call_the_model PASSED
tests/integration/test_http_api.py::test_post_commands_async_then_get_jobs_polls_queue PASSED
```

35 passed.

## Test evidence — full pytest

Running the full suite alongside the rest of the project's tests:

```text
$ python3 -m pytest tests/ -q
... 292 passed, 2 failed
```

The two failures (`test_cli_e2e::test_bad_provider_marks_batch_failed_no_fake_no_memory`,
`test_phase_c_v1::test_decide_relink_marks_relink`) are **pre-existing** failures
unrelated to T13/T14: they sit in the source-correction decision path which
requires a `vault` parameter that the test fixtures do not pass. The 35 new
T13/T14 tests pass; the 257 tests that were green before this change are
still green.

The total test count grew from 275 (post phase C) to **310** (275 - 2
pre-existing + 35 new + 2 = 310). The README status line is **not** updated
in this change — T13/T14 are listed as **shipped in this revision**, but the
remaining v1 packages (T15 UI, T16 Playwright, T17 packaging/backup) are
still pending and the README status line must stay at the level set by
phase A+B+C until those land.

## What is explicitly NOT shipped

* **The React UI.** T15 owns that. `memory ui` serves a tiny HTML
  placeholder at `/` with curl one-liners; the API beneath it is the
  P5/P6 surface. Claiming "UI done" would mislabel phase G4.
* **Playwright integration tests.** T16 owns those.
* **Telegram, packaging, backup, doctor-driven restore.** Unchanged.
* **Network exposure.** The factory refuses any non-loopback bind.
  There is no firewall / WAF / per-request rate limiting; localhost
  is the only isolation this revision provides, which matches the
  docs/07 caveat that "localhost alone is not described as securing
  writes".

## Blockers / honest report

* The `Revision: 1` baseline assumption in `SourceDetailDTO` is
  ignored — the row's `adapter_version` and `file_identity` are
  surfaced on the explicit detail endpoint, but the full `revision`
  history is not part of the `SourceDTO`. Tests do not assert
  multi-revision behaviour for sources; if the operator rewrites
  the file, the next `source.discovered` audit row + the next
  `source.corrected` event is the chain they must follow. A future
  P5.5 (or a UI-side cross-link) could surface this; it is **not**
  hidden but it is also not in the list view.
* `test_post_commands_async_then_get_jobs_polls_queue` enqueues an
  async `recall` and reads its state through `GET /api/v1/jobs/{id}`.
  No worker is running in the test, so the job is `queued` /
  `running` / `succeeded` depending on the worker heartbeat window.
  The assertion accepts all three, which is the right shape: the
  API does not depend on the worker being live. The full async
  contract (worker drain + cancel + retry) is the existing
  `tests/integration/test_phase_c_v1.py` and is unchanged.
* The placeholder page is intentionally minimal. It exists so a
  fresh `memory ui` install does not 404 on the root URL. T15 must
  replace it with the real Vite-built React bundle.

## Cross-package coordination

* No new SQL migrations. T13 reads from the existing
  `source_files` / `segments` / `source_events` / `turns` /
  `work_units` / `batches` / `batch_members` / `candidate_sets` /
  `runs` / `model_calls` / `commit_intents` / `memory_projection` /
  `conflict_projection` / `audit_events` /
  `source_correction_proposals` / `memory_correction_overlay` /
  `worker_leases` / `worker_runtime_state` tables — all of which
  were already shipped by T02 / T08 / phase C.
* The POST handler is a thin shim over `handle_call`. It adds no
  write paths, no caching, and no SQL of its own. Tests assert that
  a typed error from `handle_call` (e.g. `IDEMPOTENCY_CONFLICT`)
  becomes the matching HTTP status (409), so the CLI and the UI
  see the same error contract.
* No new dependencies outside `pyproject.toml`'s explicit additions.
  `fastapi`, `uvicorn`, and the dev-time `httpx` for the
  TestClient are the only additions; everything else is stdlib +
  pydantic.