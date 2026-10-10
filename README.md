# Memory Agent

> **Status: v1 complete.** All of T01–T17 ships. v0.1 (the keyword recall + conservative reconcile baseline) is complete and stable.
>
> **408 Python tests pass** (339 prior baseline + 69 P9/T17 incl. 3 packaging smoke + 10 privacy-HEAD durability / fail-closed restore + 12 privacy-ledger DB backstop) plus **15 Vitest** + **16 Playwright**.
>
> | Phase | Package (summary) | Acceptance |
> |---|---|---|
> | Phase A | T09 query enhancement — opt-in BM25, typed rewrite provider, hard-refusal planner, stable cursor, trace artifact, audit row, scope-before-corpus, Chinese bigrams + English word tokenisation | [`docs/17`](docs/17-v1-phase-a-acceptance.md) |
> | Phase B | T04 / T05 / T06 / T10 — bounded backfill, rotation / truncation / middle-rewrite handling, half-UTF-8 safe tail, long-turn fragmentation with deterministic text boundaries + condition-prefix carry, durable `needs_context`, branch / parent / turn identity; **P0 source-integrity fix**: `consumed_prefix_sha256` covers every byte of the consumed prefix | [`docs/18`](docs/18-v1-phase-b-acceptance.md) |
> | Phase C + repair-2 | T10 async `call` + jobs + project-owned worker with leases / schedules, T11 OS service-manager template, source-correction reconciliation queue with `adopt_new` / `scope_split`, inline discovery in `scan`, real opt-in Claude rewrite provider, `review` no longer `UNSUPPORTED` | [`docs/19`](docs/19-v1-phase-c-acceptance.md) |
> | P4 / T12 | Project-owned Telegram Bot API adapter — durable update offset + dedupe, short-lived callback tokens bound to proposal + revision + action, allowed chat / user validation, durable outbox + decision ack, decisions through the same `decide` application service as the CLI, bot echo ignored, no Telegram configured ⇒ CLI fallback and zero network calls; fake-server acceptance only, no real token read or message sent | [`docs/20`](docs/20-v1-p4-acceptance.md) |
> | P5 / T13 + P6 / T14 | Audit query service (metadata-first DTOs, server-side cursors, redacted list views, end-to-end trace `source → batch → model call → candidate → commit`) + loopback FastAPI (127.0.0.1 only, Origin + session-token checks, no wildcard CORS, shared application service) | [`docs/21`](docs/21-v1-p5-p6-acceptance.md) |
> | P7 / T15 + P8 / T16 | React 18 + TypeScript + Vite audit UI; real FastAPI + fixture vault integration; 15 Vitest + 16 Playwright tests (navigation, evidence expansion, decision idempotency, stale-revision guard, dark theme, narrow viewport, keyboard nav) | [`docs/22-v1-p7-p8`](docs/22-v1-p7-p8-acceptance.md) |
> | P9 / T17 | Privacy deletion (durable manifest + atomic privacy HEAD, query-time double-read of the deletion version, source-level re-ingest guard); consistent SQLite + files backup via the stdlib `sqlite3.Connection.backup` API; doctor extension (schema, HEAD refs, source / cursor, intents, deletion manifest, recoverability); reproducible install / run / uninstall (uninstall never deletes the vault); **packaging of the React bundle** — `scripts/build_ui.sh` mirrors `web/dist/` into `src/memory_agent/ui/dist/`, `[tool.setuptools.package-data]` + `MANIFEST.in` ship it inside both the wheel and the sdist, `_resolve_default_ui_dist` falls back to `importlib.resources.files("memory_agent.ui").joinpath("dist")` so a clean `pip install` serves the real SPA, not the T14 placeholder | [`docs/22-v1-p9`](docs/22-v1-p9-acceptance.md) |

A local, file-backed personal memory agent. It scans Claude Code and Codex session logs (with sanitised fixtures, never your real history unless you point it there), extracts explicit user requirements with a conservative reconciler, and serves them back through a keyword or BM25 recall command. **v0.1 is real, runnable, and self-contained** — no worker, no Telegram, no UI.

This repository implements [docs/14-v1-development-plan.md](docs/14-v1-development-plan.md) and was originally drafted as a design-only blueprint at `/Users/Hht/Documents/Default Project/memory-agent/`. The design docs (`docs/00`–`docs/13`) are preserved for context; **the implemented scope is `docs/14`, `docs/15`, `docs/16` (plan), `docs/17` (phase A acceptance), `docs/18` (phase B acceptance), `docs/19` (phase C acceptance), `docs/20` (P4), `docs/21` (P5/P6), and `docs/22` (P9)**. The blueprint path is a snapshot reference; this GitHub directory is the active development root and the only one that should receive edits, commits, or CI.

## What works in v0.1

| Capability | Where it lives | v0.1 behaviour |
|---|---|---|
| Explicit init baseline | `src/memory_agent/application/dispatch.py::_cmd_init` | `init` creates the SQLite schema; `--root <connector>=<path>` additionally pins every file under that root at end-of-file. No historical content is consumed on first `maintain` after `init`. Re-running `init` with new roots baselines *new* roots without touching cursors for already-known ones. **Backfill is not available in v0.1** — only content appended after `init` is consumed; there is no command to ingest pre-existing history. |
| Two Readers (Claude Code, Codex) | `src/memory_agent/adapters/sessions/` | Read JSONL session files with explicit roots, configurable schemas, unknown formats fail closed (diagnostic + cursor stays before the bad line). |
| Atomic incremental cursors | `src/memory_agent/application/scan.py` + `infrastructure/sqlite/` | Segment write + event insert + cursor update are one SQLite transaction. `UnsupportedFormatError` / `bad_json` on a complete record stops the cursor with a diagnostic — never advances over bad data. Repeated scan = zero new events when nothing changed. Zero model calls during scan. |
| Bounded batch planner | `src/memory_agent/application/plan_batches.py` + `domain/batching.py` | 5 turns / 6000 input tokens by default; oversize becomes needs_context; one active target per batch, bound to a specific `work_units.event_id`. |
| Real Claude CLI provider (default) | `src/memory_agent/adapters/models/` | `ClaudeCLIModelProvider` shells out to `claude -p --output-format json` with a hard subprocess timeout, output-size cap, and tool/hook restrictions. Refuses to run unless `MEMORY_AGENT_ENABLE_CLAUDE_CLI=1` is set. Auth / timeout / malformed / provider-unavailable all return non-`ok` statuses — never a silent `no_memory`. Records every attempt in the `model_calls` SQLite table via an injected recorder. |
| Fake provider (tests only) | `src/memory_agent/adapters/models/` | `MEMORY_AGENT_MODEL_PROVIDER=fake` (or explicit DI) returns `FakeModelProvider`. Used by end-to-end tests so CI does not depend on real Claude auth. |
| Conservative extraction | `src/memory_agent/application/process_batches.py` + `adapters/models/` | `EvidenceRef` must point at a real `work_units` row, must use `role=user` / `origin=user_session`, and its quote must equal the persisted event's source slice verbatim. Empty / timeout / malformed / provider-unavailable → batch fails closed. |
| Reconciler + commit | `src/memory_agent/application/{reconcile,recovery}.py` + `commit.py` | Non-conflicting candidates become immutable Markdown + JSON; conflicts become pending `conflict_projection` entries that `memory conflicts` lists. Pending-only and no_memory batches still produce a durable manifest commit; HEAD advances once and `finalize()` materialises the projection rows (memory + conflict) in one SQL transaction. Crash recovery aborts prepared intents whose caller is gone and surfaces recovery_required for missing/corrupt manifests. |
| `decide` (version-checked accept/reject) | `src/memory_agent/application/dispatch.py::_cmd_decide` | Requires `--idempotency-key`. Reads the current open revision from `conflict_projection`; rejects stale revisions. `adopt_new` writes the candidate as a new active memory and supersedes the old; `keep_old` leaves the existing memory in place; `scope_split` writes a narrower-scope copy; `defer` leaves it open. |
| Keyword recall | `src/memory_agent/application/recall.py` | Explicit command only. Each item's `quote` is the **exact slice of the persisted source event text** and its `locator` is that event's own byte range + record ordinal in the raw JSONL — never an offset into the memory Markdown. Project and domain filters read the committed scope projected at finalize; a memory with no resolvable/verifiable source evidence is **omitted with a warning**, never quoted approximately. Original query fingerprint kept. |
| **Opt-in BM25 (v1 phase A)** | `src/memory_agent/application/search/bm25.py` + `application/recall.py` | **Only** invoked when `retrieval="bm25"` is passed on an explicit recall request. Never imported / called from scan, batch, reconcile, or maintain — enforced by `tests/unit/test_bm25_isolation.py`. In-memory BM25 over the **filtered** corpus (scope filter runs before the index is built). Chinese text is split into character bigrams plus a controlled-alias set; English words and exact versions / paths / IDs are kept whole. Rank fusion: original variant weight 2, sum of rewrite/expansion weights ≤ 1 — the original always outweighs everything combined. |
| **QueryPlan + planner (v1 phase A)** | `src/memory_agent/application/search/planner.py` + `domain/query_policy.py` | `QueryPlan` stores the original query, hard filters, exact identifiers, negations, at most one rewrite and two expansions, each with provenance and accept/reject reason. If a model rewrite is unavailable, fails, or violates a hard constraint (introduces a new project / domain / date, drops a negation), the planner rejects it with a machine-readable reason and runs original-only retrieval. **No excluded memory content is sent to the model.** |
| **Stable cursor + trace artifact + audit row (v1 phase A)** | `src/memory_agent/application/recall.py` | Each recall result carries a HEAD-anchored `snapshot_id`, an opaque `next_cursor` bound to `(snapshot_id, plan_fingerprint)`, a `required_constraints_complete` flag, and a `query_trace` whose variants / rejected variants / fallback reason are machine-readable. The full plan is persisted to `<vault>/artifacts/queries/<trace_id>.json`, and one `query.retrieved` audit row is appended (skip with `audit=False`). A cursor from a different snapshot or different plan fingerprint is rejected as `stale_cursor_rejected=True` rather than mixed into a different snapshot. |
| **Typed rewrite provider (v1 phase A + repair-2)** | `src/memory_agent/application/search/rewrite_provider.py` | `RewriteProvider` protocol with three implementations: `IdentityRewriteProvider` (the safe default that returns `None`/empty), `StaticRewriteProvider` (returns pre-canned values, raises on demand for tests), and `ClaudeCLIRewriteProvider` (a real opt-in Claude CLI provider gated on `MEMORY_AGENT_ENABLE_CLAUDE_CLI=1`, phase C repair-2). The default is the identity stub; the live provider is opt-in. Provider failures fall back to original-only retrieval and record the reason in the trace. `FakeRewriteProvider` is a test-friendly variant that the recall service consumes directly. |
| **`include_archived` + source-correction overlay in recall (v1 phase C + repair-2)** | `src/memory_agent/application/recall.py` + `MemoryProjectionRepo.list_archived` + `MemoryCorrectionOverlayRepo` | `RecallParams.include_archived=True` surfaces rows whose `status='archived'`; the default (`False`) still hides them. The recall service also consults the source-correction overlay: a memory with a `pending_correction` row emits a `pending_correction:<id>`` warning so the caller sees the memory is awaiting a user decision on a source rewrite instead of returning it silently. The cursor contract is unchanged: `next_cursor` is bound to `(snapshot_id, plan_fingerprint)`; a follow-up call with the same cursor is accepted iff both match the current HEAD and the same plan. |
| **Bounded backfill command (v1 phase B)** | `src/memory_agent/application/scan.py::scan_once` + `src/memory_agent/contracts/commands.py::ScanRequest` + `src/memory_agent/interfaces/cli/main.py` | `memory scan --mode backfill --since <iso> --until <iso> --timezone <tz>` is a separate scan run that records a `backfill_runs` row and **never advances `complete_offset`**. Naive (no-tz) timestamps are rejected with `INVALID_REQUEST`; ambiguous dates cannot backfill by accident. Each backfill is recorded as its own `backfill_runs` row and re-running the same range over the same window is idempotent (zero new `source_events` rows on the second pass). Events inside `[since, until]` become targets; events outside the window can be carried as `context_only` (origin = `replayed_history`) but never as targets. |
| **Source revision + rotation handling (v1 phase B)** | `src/memory_agent/application/scan.py` + `src/memory_agent/infrastructure/sqlite/migrations.py` (`revision_seq`, `consumed_prefix_sha256`) | When a file is truncated / replaced / mid-rewritten between scans, `source_files.generation` and `revision_seq` advance and a new segment file is written under the new generation. The old segment is preserved on disk for audit; unconsumed `batch_members` bound to the previous generation are superseded and a `source.corrected` audit row is recorded. Renames with the same `(dev, ino)` preserve the existing cursor. Half UTF-8 characters and partial JSONL lines never advance the cursor — the safe byte boundary is recorded and the next scan resumes there. **P0 source-integrity fix:** `consumed_prefix_sha256` fingerprints *every* byte up to `complete_offset` (not just the first/last 64 KiB), so a same-size edit in the untouched middle of a >256 KiB JSONL file is detected and triggers a new generation — see `docs/18-v1-phase-b-acceptance.md`. Already-committed memories that referenced the pre-rewrite `event_id` are *not* re-walked by phase B; the correction reconciliation queue is an explicit v1 release blocker. |
| **Long-turn fragmentation + durable needs_context (v1 phase B)** | `src/memory_agent/domain/batching.py::split_user_turn_by_tokens` + `src/memory_agent/application/plan_batches.py` + `oversize_units` table | A user turn whose `estimated_tokens` exceeds the 6000-token cap is split at deterministic text boundaries (paragraph → list item → sentence → line break → whitespace). Each fragment carries `span_start` / `span_end` in code points, an optional `condition_prefix` carry, and a `parent_event_id` so the source event is marked complete only after every fragment is accounted for. A turn that cannot be safely split (no boundary, per-character chunk still over budget) becomes a durable `oversize_units` row with `state="needs_context"` and `reason="oversize_no_safe_split"` — never an active batch target. The 20-turn / 12-turn packing tests confirm 4 / 3 batches, every user turn becomes one active target. |
| **Async `call` + project-owned worker (v1 phase C + repair-2)** | `src/memory_agent/application/call.py` + `src/memory_agent/application/worker.py` + `src/memory_agent/interfaces/cli/main.py` | `memory call --request-file - --json` accepts a versioned `CommandRequest` over stdin; `mode="sync"` runs inline, `mode="async"` durably persists a `jobs` row and returns `status="queued"` + `job_id` + `worker_offline` warning. `(client_id, idempotency_key)` is bound to a canonical `request_hash`; same key + same params returns the original `job_id`; same key + changed params returns `IDEMPOTENCY_CONFLICT` (exit 4). Unknown fields on `params` / unknown operations return `INVALID_REQUEST`. The `review` operation is supported in **phase C repair-2**: it runs the conservative weekly / monthly review pipeline (deterministic anomaly heuristics, no model call) and writes a proposal artifact under `<vault>/artifacts/reviews/`. `memory worker --once` runs one pass; the long-running `memory worker` honours `<vault>/config/worker_schedule.json` (default `every_seconds=1`, `daily_at_hour=2`, weekly after daily catch-up, monthly after daily catch-up). The worker owns the lease + heartbeat + backpressure; **`worker_leases` is the vault-level writer lock (Phase C repair-1) — only one worker process may run a pass at a time, and a second worker with a different `worker_id` is refused while any active lease exists**. Expired leases are reclaimable by a different worker. **Phase C repair-2 — real scheduled work:** the daily pass actually scans the configured roots (read from `source_files` or `config.toml`) and only advances `last_daily_run` when an increment was observed or a queued job was drained; weekly / monthly produce conservative review proposals (anomaly heuristics, no LLM call) and bump their timestamps only on a successful write. The worker itself never instantiates the model provider, so model-free queued jobs (`scan` / `init` / `doctor` / `batch.plan` / `conflicts` / `decide` / `review` / `source_correction.decide` / `job.*`) drain end-to-end without touching the CLI provider, so a worker pass with no queued jobs (or only model-free queued jobs) makes **zero model calls** and never raises `ModelError` from a missing opt-in. A stale `running` job whose heartbeat has expired is reset to `queued` at the start of every pass (`JobRepo.reclaim_stale_running`) so a crashed worker doesn't leave the job stuck forever. `memory job status`, `memory job cancel`, `memory job retry` are versioned, route through the explicit `vault_root` parameter (no module-global holder), and `cancel` never rolls back an already-published commit. |
| **Source-correction reconciliation queue (v1 phase C + repair-2)** | `src/memory_agent/application/source_correction.py` + `source_correction_proposals` / `memory_correction_overlay` tables | A durable queue for memories that cited a now-rewritten `event_id`. Discovery walks every committed memory, finds the matching anchor (same `source_file_id` + same record anchor + different `revision_hash`), and emits a stable `correction_id` proposal with `status="pending"` (or `auto_resolved` when the new segment still contains the quoted span verbatim). The overlay is the read path recall will join against; **phase C repair-2** also refreshes the overlay *inline* at the end of every `scan` so a recall observed before the next worker pass still sees the pending-correction row and emits a `pending_correction:<id>` warning instead of returning the stale memory silently. The user closes the proposal via `decide_proposal(action="keep_old" | "adopt_new" | "scope_split" | "defer")`, or via the new `memory call --operation source_correction.decide` (with `expected_proposal_revision` for optimistic concurrency). **Phase C repair-2 — `adopt_new` / `scope_split`:** the new memory is written through `CommitService` (file → manifest → HEAD swap → finalize) referencing the *new* event_id and new revision hash; the old memory is marked `superseded` and the proposal row's `superseded_memory_id` / `superseded_head_commit_id` are recorded. A stale `expected_proposal_revision` is rejected with `INVALID_REQUEST`; a `publish` failure leaves the proposal `pending` and the overlay `pending_correction` so the user can retry. The worker never silently re-links a memory to changed user text. |
| **launchd template (v1 phase C)** | `design/launchd/com.memory-agent.worker.plist` + `design/launchd/README.md` | Phase C ships the launchd plist as a *template*; the worker is meant to be supervised by the OS service manager. The plist only restarts the process on crash — it does **not** schedule. **This template is NOT installed in this revision; no `launchctl load` was performed.** |
| **P4 / T12 Telegram decision channel** | `src/memory_agent/adapters/telegram/{config,client,formatter,service}.py` + `telegram_updates` / `telegram_callback_tokens` / `notification_outbox` tables | The same application service as the CLI; a valid callback calls `dispatch(operation="decide", client_id="user_telegram", …)` with the same `proposal_revision` compare-and-swap. Update offset is durable; `update_id` is the dedupe key. Short-lived opaque callback tokens bound to (conflict_id, proposal_revision, action, chat_id, user_id) with an expiry. The bot token is read from the environment variable named by `token_env`; no secrets at rest. Outbox is at-least-once; send failures stay `pending` with `attempt+1` and `next_retry = now + backoff × min(attempt, 10)`. Bot echo is ignored; offline safety: a network outage leaves the outbox and offset untouched and local scan / recall / decide still work. **Not configured ⇒ no network.** Acceptance uses a fake Bot API server; no real token was read or any message sent. |
| **P5 / T13 audit query service** | `src/memory_agent/application/audit_queries.py` + `src/memory_agent/contracts/audit.py` | Metadata-first read DTOs over the existing SQLite + files layout, server-side pagination with deterministic cursors, DB/HEAD reconcile status, redacted list views, full bodies only on the explicit evidence endpoint, and an end-to-end trace `source → batch → model call → candidate → commit`. |
| **P6 / T14 loopback FastAPI** | `src/memory_agent/interfaces/http/{app,audit_routes,command_routes,local_auth}.py` | Bind-only-to-127.0.0.1 HTTP service that exposes every docs/10 endpoint and forwards `POST /api/v1/commands` through the same `handle_call` dispatcher the CLI uses. Origin allowlist + session token enforcement on writes, no wildcard CORS, no token in URLs or log lines, and the model provider is never instantiated by any read path. |
| **P9 / T17 privacy deletion barrier** | `src/memory_agent/application/privacy.py` + `src/memory_agent/contracts/privacy.py` + `privacy_deletions` / `privacy_blocks` tables | `memory privacy forget --kind memory|event|source_block --target … --idempotency-key …` writes an immutable manifest file under `<vault>/privacy/deletions/<op_id>.json` and bumps `<vault>/privacy/HEAD.json` atomically before mirroring into SQLite. No statement, evidence quote, locator, or full-text audit payload is preserved — only the receipt metadata. The recall path reads the privacy HEAD at the *start* and re-reads it *just before return* so even a fresh DB projection or a stale backup cannot resurrect erased content. `source_block` pins the source_files cursor and the scanner refuses to register any new event for the blocked source. The receipt is content-free; unblock lifts the source_block re-ingest guard but does not restore memory content. **`memory init` atomically establishes a zero-version `privacy/HEAD.json`** on a genuinely fresh vault (no HEAD, no deletion manifests, no `privacy_deletions` rows in the state DB) via `ensure_privacy_head`, refusing when orphan manifests OR `privacy_deletions` rows already exist (DB backstop); `read_live_privacy_ledger` refuses to synthesise a v0 ledger when the HEAD is missing, refuses a HEAD whose state DB records deletion rows with no matching on-disk manifest, and refuses a HEAD whose state DB records an `op_id` / `kind` / `targets` / `version` mismatch with the on-disk receipt — so an old v0 bundle can no longer be restored into an un-initialised `--vault` *and* a forged same-version bundle can no longer slip past the live ledger's hash check. See [`docs/22-v1-p9-acceptance.md`](docs/22-v1-p9-acceptance.md). |
| **P9 / T17 consistent SQLite + files backup / restore** | `src/memory_agent/application/backup.py` + `src/memory_agent/contracts/backup.py` | `memory backup create --output <path>` snapshots the live vault via the standard-library `sqlite3.Connection.backup()` API (not `cp` of `state.sqlite`) under a brief recover-unfinished-intents pause; HEAD + privacy HEAD + segments + memories + commits + artifacts are copied in the same run with sha256. `memory backup restore --bundle <path> --target <vault>` always consults the *operator's live vault's* authoritative privacy ledger (`privacy/HEAD.json` + `privacy/deletions/`) — never a caller-supplied integer and never an empty restore target — and refuses to mount a bundle that is older than the live ledger or that matches the live version but is missing any current deletion receipt. **`read_live_privacy_ledger` refuses a missing HEAD even when the deletions directory is empty**, so an un-initialised `--vault` cannot be silently bootstrapped into a v0 ledger (fail-closed restore). The unsafe `--no-quarantine` and `--live-privacy-head-version` CLI flags have been removed entirely; argparse rejects them as unknown so a forgotten memory cannot be resurrected via an old bundle even by an attacker who controls the CLI argv. **`backup.create` refuses when the live vault has no privacy HEAD** so the silent v0 synthesis path is closed at the snapshot boundary too. **`read_live_privacy_ledger` cross-checks every `privacy_deletions` row against the on-disk manifest** (matching `op_id` / `kind` / `targets` / `version`) so a forged same-version bundle that has been sanitised of the receipts the DB still remembers cannot pass; the DB may lag disk (crash-window between HEAD swap and DB mirror) but the DB may NOT lead disk. Quarantine fails closed: target stays unmounted on any privacy mismatch. Direct restore into a clean invented vault; doctor + recall work end-to-end against the restored vault. Tar-slip / path-traversal guard rejects any member whose name contains `..` or starts with `/`. |
| **P9 / T17 doctor extension** | `src/memory_agent/application/doctor.py` | `run_doctor` now reports schema version, HEAD refs, source cursor integrity (`source_file_missing` / `cursor_beyond_size`), commit intent recoverability (`recovery_required` when HEAD has neither the parent nor the commit of a pending intent), and privacy manifest consistency (`privacy_manifest_drift` / `privacy_db_ahead_of_head` / `privacy_db_behind_disk`). The doctor never prints full memory text or evidence quotes — only `memory_id` / `op_id` / counts / paths. |
| **P9 / T17 packaging** | `scripts/{install,run,uninstall,build_ui}.sh` + `samples/config.toml` + `src/memory_agent/ui/dist/` (mirrored via `scripts/build_ui.sh`) + `pyproject.toml` `[tool.setuptools.package-data]` + `MANIFEST.in` | Reproducible install (`python3 -m venv .venv && pip install -e '.[dev]'`), the venv is reused on re-runs (marker file), `scripts/uninstall.sh` explicitly does **not** delete the vault. `scripts/build_ui.sh` runs `npm run build` under `web/` and mirrors the hashed Vite output into `src/memory_agent/ui/dist/`; the mirror ships inside the wheel (`[tool.setuptools.package-data]`) and the sdist (`MANIFEST.in`), and `_resolve_default_ui_dist` falls back to `importlib.resources.files("memory_agent.ui").joinpath("dist")` so a clean `pip install` (no developer checkout, no editable install) serves the real React SPA from `memory ui`. `samples/config.toml` ships with the repo and is copied into the vault by `install.sh` on first run if the vault's own config is missing. The launchd template (`design/launchd/com.memory-agent.worker.plist`) is **not** loaded. |

## What is **not** in v0.1 (and why)

Deferred to v1 by design — see [docs/14](docs/14-v1-development-plan.md) and the matching P1 section of `docs/13`:

- **Worker / schedules / launchd**: shipped in v1 phase C; install via `python -m memory_agent --vault <vault> worker` (the launchd template is at `design/launchd/com.memory-agent.worker.plist` and is **not loaded by this revision**).
- **Telegram decision channel**: shipped in v1 P4 / T12 (fake-server acceptance; no real token was read).
- **HTTP audit API + React UI**: shipped in v1 P5/P6/P7/P8 (audit query service + loopback FastAPI + React/TypeScript/Vite audit UI + Playwright integration tests). `memory ui` serves the real Vite-built React bundle from a clean wheel install via the packaged `importlib.resources` fallback; the T14 placeholder page only renders when no `ui_dist` is reachable.
- ~~BM25 at query time, model rewrite/expansion, reranking~~ → **shipped in phase A** (`docs/17`). `memory recall --retrieval bm25` is now opt-in.
- **Live Claude Code CLI smoke in CI**: blocked by OAuth and nested-invocation constraints. Provider adapter is implemented and unit-tested against stub CLIs; activating it for real requires explicit user auth (`MEMORY_AGENT_ENABLE_CLAUDE_CLI=1`).
- **Full backup/privacy deletion design**: shipped in v1 P9 / T17 (privacy HEAD + durable manifest + consistent SQLite+files backup/restore; uninstall does not delete the vault).
- **Implicit historical backfill**: v0.1 only consumes content appended after `init`/baseline. **There is no command to backfill existing history in v0.1**, and no documented recipe to do so. If you need backfill in the future, that capability is a v1 item — for now, install fresh and append content from this point on. We do **not** advise editing `source_files.complete_offset` manually; that breaks the per-segment contract and the cursor is not designed to be hand-edited.

## What is **not** in v1 (out of scope)

Items genuinely **outside v1 scope** — they remain deferred past v1 even though every T01–T17 row ships:

- **Vector recall, semantic embeddings, persistent search services.** No external vector DB, no embedding model, no FTS-backed index. Recall runs over the in-memory scope-filtered corpus; BM25 is opt-in (`--retrieval bm25`) but still in-process. See [`docs/16-v1-implementation-plan.md`](docs/16-v1-implementation-plan.md) for the out-of-v1 list and [`docs/22-v1-p9-acceptance.md`](docs/22-v1-p9-acceptance.md) for the privacy / backup / doctor / packaging acceptance that does ship.
- **Live Claude Code CLI smoke in CI.** Provider adapter is implemented and unit-tested against stub CLIs; activating it for real requires explicit user auth (`MEMORY_AGENT_ENABLE_CLAUDE_CLI=1`). Acceptance does not depend on a real Claude CLI round-trip.
- **Localhost as a write-authorisation.** Per the `docs/07` caveat, the loopback FastAPI binds 127.0.0.1 / localhost / ::1 but **localhost alone is not described as securing writes**; writes require the configured session token (or Origin allowlist for browser callers). This is a documented limit, not a deferred package.
- **Implicit historical backfill.** v0.1 only consumes content appended after `init` / baseline; v1 adds an explicit opt-in `memory scan --mode backfill --since … --until …` (`docs/18-v1-phase-b-acceptance.md`). Manual edits to `source_files.complete_offset` are not supported — the per-segment contract is not designed to be hand-edited.

## Quick start (with sanitised fixtures)

The quick start below is the same end-to-end exercised by
`tests/integration/test_cli_e2e.py::TestCLISubprocessE2E::test_init_then_maintain_then_recall`.
The appended line matches the recall query verbatim so the final
`recall` command returns the committed memory.

```bash
# 1. Install (editable, dev deps)
python3 -m pip install -e ".[dev]"

# 2. Create a sanitised Claude Code session for testing
mkdir -p /tmp/fake_claude/projects/proj_x
cat > /tmp/fake_claude/projects/proj_x/sess1.jsonl <<'EOF'
{"type":"user","uuid":"u01","message":{"role":"user","content":"use Python for the prototype"},"timestamp":"2026-10-08T10:00:00Z","sessionId":"sess1","parentUuid":null}
{"type":"assistant","uuid":"a01","message":{"role":"assistant","content":"OK, will use Python."},"timestamp":"2026-10-08T10:00:01Z","sessionId":"sess1","parentUuid":"u01"}
EOF

# 3. Initialise the vault AND baseline the existing files at end-of-file.
#    The session above already exists on disk, so this first init will
#    not pick up any historical content. Only files appended AFTER
#    this command are seen by `maintain`.
python3 -m memory_agent --vault /tmp/my_vault init \
    --root "claude-code=/tmp/fake_claude"

# 4. Append a brand-new complete user message to the session. The
#    trigger word `use` at the start ensures the fake provider
#    extracts it; the query in step 6 matches its text exactly.
cat >> /tmp/fake_claude/projects/proj_x/sess1.jsonl <<'EOF'
{"type":"user","uuid":"u02","message":{"role":"user","content":"use Python for the prototype always"},"timestamp":"2026-10-08T10:01:00Z","sessionId":"sess1","parentUuid":"a01"}
EOF

# 5. Run maintain. The default model provider requires real Claude
#    auth. For development / tests, set MEMORY_AGENT_MODEL_PROVIDER=fake
#    so the in-process fake provider is used instead. Production
#    users should also set MEMORY_AGENT_ENABLE_CLAUDE_CLI=1 and use
#    MEMORY_AGENT_MODEL_PROVIDER=cli (the default).
MEMORY_AGENT_MODEL_PROVIDER=fake \
    python3 -m memory_agent --vault /tmp/my_vault maintain \
    --root "claude-code=/tmp/fake_claude"

# 6. Recall (evidence-backed). The query must match a token that
#    appears in the appended user message above.
python3 -m memory_agent --vault /tmp/my_vault recall --query "Python prototype"

# 7. Privacy deletion barrier (v1 P9). Idempotency-key is required.
python3 -m memory_agent --vault /tmp/my_vault privacy forget \
    --kind memory --target mem_demo --idempotency-key demo01

# 8. Consistent backup (v1 P9). The tar is a content-free metadata
#    bundle that restore validates before materialising.
python3 -m memory_agent --vault /tmp/my_vault backup create \
    --output /tmp/my_vault.tar --label "smoke"

# 9. Restore into a clean invented vault.
python3 -m memory_agent --vault /tmp/any_vault backup restore \
    --bundle /tmp/my_vault.tar --target /tmp/my_vault_restored

# 10. Health check
python3 -m memory_agent --vault /tmp/my_vault doctor
```

The CLI prints exactly one JSON CommandResponse per invocation on stdout; nothing else. Diagnostics go to stderr.

> **Real Claude auth is required for production.** `cli` is the default provider; until you set `MEMORY_AGENT_ENABLE_CLAUDE_CLI=1` and have `claude` on `PATH`, `maintain` will fail with a real provider-unavailable error rather than silently producing empty memories.

## Safety guarantees

1. **No real history is read by default.** `init --root <connector>=<path>` pins every existing file at end-of-file; only files appended (or created) after init are consumed. There is no implicit backfill command in v0.1.
2. **Quote == source slice.** Evidence validation slices the source text with Unicode code points and rejects mismatches before any candidate becomes a memory. Each piece of evidence must also reference a real `work_units` row. Recall re-derives the quote and locator from the immutable segment rather than from the memory body, so a quote it returns is always an exact source slice.
3. **User-only evidence.** `role != "user"` or non-`user_session` origin is refused by `domain/evidence.py`.
4. **Conservative reconcile.** Conflicting scopes produce *pending proposals* (and `conflict_projection` rows), never silent overwrites. v0.1 is not a full semantic conflict detector; it catches the obvious cases and stays conservative on ambiguous ones.
5. **No arbitrary winner.** Two candidates in the *same* batch with overlapping scopes and materially different statements form a contradiction group; **every** member goes to pending and none is published. Source order never decides which conflicting instruction wins. A pre-existing active memory is never replaced without a `decide`.
6. **Scope is explicit.** A candidate with no scope, or with a scope whose level and IDs disagree (e.g. a bare `"project"` naming no project), is rejected and the batch fails closed. An unknown scope is never widened to global; only an explicit `global` is global.
7. **Files are authoritative.** SQLite is for cursors / queues / lease / audit. Memory text lives in Markdown + JSON. HEAD swaps are atomic; recovery reconciles prepared intents.
8. **Model never touches the filesystem.** Provider adapters receive only JSON-shaped data; no shell tools, no file writes, no network tools. The CLI provider hard-restricts tool use via the `--tools ""` flag and enforces a subprocess timeout.
9. **Fail-closed provider.** A missing CLI / auth failure / timeout / malformed JSON returns a non-`ok` status. The batch keeps retrying and, after the attempt budget, ends `failed`. v0.1 never lowers a real error to a silent `no_memory`.
10. **Exit codes are stable.** 0 ok / queued / status read; 2 invalid request; 3 unavailable; 4 idempotency; 5 partial; 6 internal / recovery / unsupported.
11. **Privacy forget is the only content-removal operation.** Every forget persists an immutable manifest + atomic HEAD swap; the recall path reads the privacy HEAD twice (at the start and just before returning) so a torn write or stale DB projection cannot resurrect erased content. A `source_block` re-ingest guard pins the cursor and the scanner refuses to register new events. **`memory init` is the only path that writes a v0 privacy HEAD into a vault that previously had none**, via `ensure_privacy_head`; the DB backstop refuses the v0 synthesis if the live vault's `state.sqlite` still records any `privacy_deletions` rows (the file ledger alone is not enough — a vault whose on-disk HEAD + manifests were lost but whose DB still records the forget is exactly the resurrection path this guards). `read_live_privacy_ledger` never synthesises v0, refuses a missing HEAD, refuses a HEAD whose state DB records deletion rows with no matching on-disk manifest, and refuses a HEAD whose state DB records an `op_id` / `kind` / `targets` / `version` mismatch with the on-disk receipt — so an un-initialised live vault cannot be used as the restore trust root to silently mount an old bundle *and* a forged same-version bundle cannot slip past the live ledger's hash check. See [`docs/22-v1-p9-acceptance.md`](docs/22-v1-p9-acceptance.md).
12. **Backup is content-free metadata.** Restore validates sqlite sha256 + deletion manifest hash before materialising; an old bundle without the latest deletion manifest is quarantined rather than mounted. **`read_live_privacy_ledger` refuses a missing HEAD regardless of whether the deletions directory is empty**; **`backup.create` refuses when the live vault has no privacy HEAD** (a bundle without a HEAD would let a forged v0 bundle slip through any future restore); the live ledger also cross-checks every `privacy_deletions` row against the on-disk manifest so a forged same-version bundle that has been sanitised of the receipts the DB still remembers cannot pass. The only operator-authorised safe initialisation is the explicit `memory init`, which proves the absence of deletion manifests AND the absence of `privacy_deletions` rows in the state DB before writing the v0 HEAD. The `scripts/uninstall.sh` deliberately does not delete the vault.

## Repository layout

```
memory-agent/
  pyproject.toml
  src/memory_agent/
    contracts/            # versioned, validated wire types (Pydantic v2)
    domain/               # pure DP rules — batching, scope, lifecycle, evidence, query policy
    application/          # use-case orchestration; depends on ports only
    adapters/
      sessions/           # Claude Code + Codex readers
      models/             # Fake (test) + Claude CLI providers + recorder
      telegram/           # P4 / T12 Bot API adapter (service, client, formatter, config)
    infrastructure/
      sqlite/             # WAL + migrations + repositories
      files/              # atomic write, immutable segments, vault layout
    interfaces/
      cli/                # `memory` / `python -m memory_agent`
      http/               # P6 / T14 loopback FastAPI (audit + command routes)
    prompts/              # reserved for v1 prompt templates
  tests/
    fixtures/             # sanitised session/contract fixtures (never real user data)
    unit/                 # contract + domain + model-provider tests
    integration/          # SQLite, scan, plan, source binding, CLI e2e
  scripts/                # P9 / T17 install / run / uninstall + launchd template
  samples/                # P9 / T17 sample config
  docs/                   # 00–22 design + plan + acceptance
  design/                 # static UI mocks (T15)
  research/               # background research (not a runtime dep)
  .workspace/             # historical research artefacts (see README inside)
```

## Tests

```bash
python3 -m pytest tests/ -q
```

408 tests cover:

- Contract roundtrip + version + unknown-field rejection (`tests/unit/test_contracts.py`)
- Domain rules: batching, evidence, scope, lifecycle, query policy (`tests/unit/test_domain.py`)
- BM25 isolation: BM25 is **never** imported by scan/batch/reconcile/commit/recovery/dispatch/CLI (`tests/unit/test_bm25_isolation.py`)
- BM25 tokenisation (Chinese character bigrams + English words + exact IDs) and reciprocal-rank fusion (`tests/unit/test_bm25.py`)
- Recall-side hard constraints: exact-IDs, negation, project/date drift refusal (`tests/unit/test_query_policy.py`)
- Typed rewrite provider + planner (original-only fallback on provider failure; at most 1 rewrite + 2 expansions) (`tests/unit/test_planner.py`)
- Model provider: stub-CLI success / bad JSON / auth-error / timeout shapes; recorder; fake provider via env (`tests/unit/test_model_provider.py`)
- SQLite: idempotent event registration, transaction rollback, lease-token rejection, recovery (`tests/integration/test_sqlite_state.py`)
- Readers + scan: append, partial final line, repeated scan, no model calls, fail-closed on unsupported records (`tests/integration/test_readers_scan.py`)
- Source-event binding: each member bound to its own event_id; non-user events cannot become active targets; locator preserved (`tests/integration/test_source_binding.py`)
- CLI subprocess e2e: init baseline; maintain → recall with evidence; repeat maintain idempotence; cross-batch conflict; decide adopt_new / keep_old / idempotency-key / stale conflict; unsupported record stops cursor; bad provider failure path (`tests/integration/test_cli_e2e.py`)
- CLI entry points and unknown-operation handling (`tests/integration/test_cli_entry.py`)
- Batch coverage, commit recovery, and publish protocol (`tests/integration/test_blocking_defects.py`, `tests/integration/test_publish_protocol.py`)
- Exact source evidence, scope validation, and recall filtering (`tests/integration/test_p0_evidence_and_scope.py`)
- **Phase A pipeline**: keyword vs BM25, Chinese bigrams, scope-before-corpus, cross-project isolation, provider-failure fallback, trace artifact, audit row, stale cursor, unknown field rejection, retrieval_limit semantics (`tests/integration/test_recall_pipeline_v1.py`)
- **Phase A CLI subprocess**: keyword + BM25 recall via `memory recall --retrieval …`; machine-readable JSON; maintain never runs a hidden recall; unknown CLI flag rejected (`tests/integration/test_cli_subprocess_v1.py`)
- **Phase B**: rotation, truncation, middle-rewrite, backfill windowing (`tests/integration/test_phase_b_v1.py`, `tests/integration/test_phase_b_repair_v1.py`)
- **Phase C**: async `call`, project-owned worker with leases / schedules, source-correction queue, `review` proposal (`tests/integration/test_phase_c_v1.py`, `tests/integration/test_phase_c_repair1.py`, `tests/integration/test_phase_c_repair2.py`)
- **P4 / T12 Telegram**: fake-server acceptance — full loop, replay / out-of-order, invalid callbacks, crash-window, offline safety, bot echo, no token in storage, callback token binding (`tests/integration/test_telegram_v1.py`)
- **P5 / T13 audit query service**: overview / sources / session turns / batches / batch evidence / memory history / conflicts / audit events / query traces, with pagination + redaction + projection state (`tests/integration/test_audit_queries.py`)
- **P6 / T14 HTTP**: loopback bind refusal, audit DTOs, pagination, evidence redaction, POST commands routing through `handle_call`, Origin + token enforcement, jobs status, placeholder page, zero-model-call guarantee (`tests/integration/test_http_api.py`)
- **P9 / T17 privacy**: manifest + HEAD + DB + audit atomics; query-time double-read of deletion version; recall drops stale projection rows; source_block pins cursor + scanner re-ingest guard; crash-window between manifest-write and DB-mirror (manifest is the authority); unblock revokes source_block without restoring memory; trace artefacts are content-free (`tests/integration/test_privacy_v1.py`)
- **P9 / T17 backup / restore**: `sqlite3.Connection.backup` snapshot; HEAD + manifest + immutable file + privacy manifest with sha256; restoration into a clean invented vault; doctor + recall end-to-end against the restored vault; quarantine of an old bundle; detection of corrupt sqlite / tar-slip / missing DB; deletion manifest merge; after-forget resurrection prevention; **`memory init` atomically establishes a v0 `privacy/HEAD.json`** on a fresh vault via `ensure_privacy_head`, refusing when deletion manifests exist without a HEAD; **`read_live_privacy_ledger` raises `BackupError` on a missing HEAD** so an un-initialised `--vault` cannot be silently bootstrapped into a v0 ledger; backup create preserves the v0 HEAD; CLI subprocess proves the full quarantine scenario `init live → backup v0 → forget → --vault /new/uninitialized restore --bundle old --target /empty` fails with the target unmounted (`tests/integration/test_backup_v1.py`)
- **P9 / T17 doctor extension**: clean vault, pending intent → recovery_required, cursor beyond size, missing source file, privacy manifest drift, recoverability probe, DB lag after crash (`tests/integration/test_doctor_v1.py`)

- Contract roundtrip + version + unknown-field rejection (`tests/unit/test_contracts.py`)
- Domain rules: batching, evidence, scope, lifecycle, query policy (`tests/unit/test_domain.py`)
- BM25 isolation: BM25 is **never** imported by scan/batch/reconcile/commit/recovery/dispatch/CLI (`tests/unit/test_bm25_isolation.py`)
- BM25 tokenisation (Chinese character bigrams + English words + exact IDs) and reciprocal-rank fusion (`tests/unit/test_bm25.py`)
- Recall-side hard constraints: exact-IDs, negation, project/date drift refusal (`tests/unit/test_query_policy.py`)
- Typed rewrite provider + planner (original-only fallback on provider failure; at most 1 rewrite + 2 expansions) (`tests/unit/test_planner.py`)
- Model provider: stub-CLI success / bad JSON / auth-error / timeout shapes; recorder; fake provider via env (`tests/unit/test_model_provider.py`)
- SQLite: idempotent event registration, transaction rollback, lease-token rejection, recovery (`tests/integration/test_sqlite_state.py`)
- Readers + scan: append, partial final line, repeated scan, no model calls, fail-closed on unsupported records (`tests/integration/test_readers_scan.py`)
- Source-event binding: each member bound to its own event_id; non-user events cannot become active targets; locator preserved (`tests/integration/test_source_binding.py`)
- CLI subprocess e2e: init baseline; maintain → recall with evidence; repeat maintain idempotence; cross-batch conflict; decide adopt_new / keep_old / idempotency-key / stale conflict; unsupported record stops cursor; bad provider failure path (`tests/integration/test_cli_e2e.py`)
- CLI entry points and unknown-operation handling (`tests/integration/test_cli_entry.py`)
- Batch coverage, commit recovery, and publish protocol (`tests/integration/test_blocking_defects.py`, `tests/integration/test_publish_protocol.py`)
- Exact source evidence, scope validation, and recall filtering (`tests/integration/test_p0_evidence_and_scope.py`)
- **Phase A pipeline**: keyword vs BM25, Chinese bigrams, scope-before-corpus, cross-project isolation, provider-failure fallback, trace artifact, audit row, stale cursor, unknown field rejection, retrieval_limit semantics (`tests/integration/test_recall_pipeline_v1.py`)
- **Phase A CLI subprocess**: keyword + BM25 recall via `memory recall --retrieval …`; machine-readable JSON; maintain never runs a hidden recall; unknown CLI flag rejected (`tests/integration/test_cli_subprocess_v1.py`)
- **Phase B**: rotation, truncation, middle-rewrite, backfill windowing (`tests/integration/test_phase_b_v1.py`, `tests/integration/test_phase_b_repair_v1.py`)
- **Phase C**: async `call`, project-owned worker with leases / schedules, source-correction queue, `review` proposal (`tests/integration/test_phase_c_v1.py`, `tests/integration/test_phase_c_repair1.py`, `tests/integration/test_phase_c_repair2.py`)
- **P4 / T12 Telegram**: fake-server acceptance — full loop, replay / out-of-order, invalid callbacks, crash-window, offline safety, bot echo, no token in storage, callback token binding (`tests/integration/test_telegram_v1.py`)
- **P5 / T13 audit query service**: overview / sources / session turns / batches / batch evidence / memory history / conflicts / audit events / query traces, with pagination + redaction + projection state (`tests/integration/test_audit_queries.py`)
- **P6 / T14 HTTP**: loopback bind refusal, audit DTOs, pagination, evidence redaction, POST commands routing through `handle_call`, Origin + token enforcement, jobs status, placeholder page, zero-model-call guarantee (`tests/integration/test_http_api.py`)
- **P9 / T17 privacy**: manifest + HEAD + DB + audit atomics; query-time double-read of deletion version; recall drops stale projection rows; source_block pins cursor + scanner re-ingest guard; crash-window between manifest-write and DB-mirror (manifest is the authority); unblock revokes source_block without restoring memory; trace artefacts are content-free (`tests/integration/test_privacy_v1.py`)
- **P9 / T17 backup / restore**: `sqlite3.Connection.backup` snapshot; HEAD + manifest + immutable file + privacy manifest with sha256; restoration into a clean invented vault; doctor + recall end-to-end against the restored vault; quarantine of an old bundle; detection of corrupt sqlite / tar-slip / missing DB; deletion manifest merge; after-forget resurrection prevention (`tests/integration/test_backup_v1.py`)
- **P9 / T17 doctor extension**: clean vault, pending intent → recovery_required, cursor beyond size, missing source file, privacy manifest drift, recoverability probe, DB lag after crash (`tests/integration/test_doctor_v1.py`)
- **P9 / T17 CLI subprocess**: `memory privacy forget|list|receipt|unblock` and `memory backup create|list|restore` happy paths + rejected invocations (`tests/integration/test_cli_v1_p9.py`)

## Acceptance status

- **v0.1** (baseline, complete): [`docs/15-v1-acceptance.md`](docs/15-v1-acceptance.md) — 143 tests, full per-P0 PASS/FAIL table.
- **v1 phase A** (this revision, complete): [`docs/17-v1-phase-a-acceptance.md`](docs/17-v1-phase-a-acceptance.md) — 67 new tests on top of the 143 baseline, all green.
- **v1 phase B** (this revision, complete): [`docs/18-v1-phase-b-acceptance.md`](docs/18-v1-phase-b-acceptance.md) — full per-package PASS/FAIL.
- **v1 phase C** + **phase C repair-1 + repair-2** (this revision, complete): [`docs/19-v1-phase-c-acceptance.md`](docs/19-v1-phase-c-acceptance.md).
- **v1 P4 / T12** (this revision, complete): [`docs/20-v1-p4-acceptance.md`](docs/20-v1-p4-acceptance.md) — fake-server acceptance only; no real token was read or any message sent.
- **v1 P5 / T13 + P6 / T14** (this revision, complete): [`docs/21-v1-p5-p6-acceptance.md`](docs/21-v1-p5-p6-acceptance.md).
- **v1 P7 / T15 + P8 / T16** (this revision, complete): [`docs/22-v1-p7-p8-acceptance.md`](docs/22-v1-p7-p8-acceptance.md) — React/TypeScript/Vite audit UI, real FastAPI + fixture vault, 15 Vitest + 16 Playwright tests.
- **v1 P9 / T17** (this revision, complete): [`docs/22-v1-p9-acceptance.md`](docs/22-v1-p9-acceptance.md).
- **v1 full scope** (forward map): [`docs/16-v1-implementation-plan.md`](docs/16-v1-implementation-plan.md). All of T01–T17 ships.

## What v0.1 is *not* claiming

- It is **not** a full semantic conflict detector. The reconciler is conservative; many obvious conflicts will land in the pending queue awaiting a user decision via `memory decide`.
- It is **not** a query expansion or BM25 system *by default*; phase A ships opt-in BM25 (`--retrieval bm25`); see [`docs/17`](docs/17-v1-phase-a-acceptance.md) for the precise contract.
- It is **not** running on your real history. The default install starts at end-of-file baseline; you must opt-in by passing roots, and there is no command to backfill.
- It is **not** running a live Claude CLI by default. `MEMORY_AGENT_ENABLE_CLAUDE_CLI=1` is required for real Claude auth.
- It is **not** a deployable package with macOS launchd configs or Telegram bot setup. Those are P2–P7 of the v1 plan and are now shipped (launchd template at `design/launchd/`, Telegram adapter in P4, HTTP/UI in P6/P7/P8, packaging in P9).

## What phase A is *not* claiming

- It is **not** a real-model-backed rewrite/expansion path. The typed `RewriteProvider` ships with safe stubs only; a live Claude-backed provider is a separate v1 package.
- It is **not** a persistent search service, vector DB, or SQLite FTS.
- It is **not** a UI / HTTP API. The trace artifact + audit row are designed to be readable by the future loopback HTTP API (P5/P6 of the plan), but the UI is not shipped in phase A.
- It is **not** full v1. Privacy / backup / packaging / UI / Playwright are P6–P9 of `docs/16` and explicitly out of phase A; **P6 / P7 / P8 / P9 are all shipped** (see above), and all of T01–T17 ships.

## Repository origin

The project was originally drafted as a design-only blueprint at `/Users/Hht/Documents/Default Project/memory-agent/`. That blueprint was copied as the starting design into this GitHub directory (`/Users/Hht/Documents/10.github/memory-agent/`) and then implemented here. The blueprint path is a frozen snapshot reference; the GitHub directory is the active development root and the only one that should receive edits, commits, or CI. The two paths may diverge.

**This local repository has no configured Git remote and no published GitHub repository.** `pyproject.toml` therefore declares no `Homepage` / `Source` / `Issues` URLs: those fields would advertise a repository that does not exist. Add them back when a remote actually exists.
