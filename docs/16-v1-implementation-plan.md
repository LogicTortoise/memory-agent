# Memory Agent v1 Implementation Plan

Date: 2026-10-08. Path: `/Users/Hht/Documents/10.github/memory-agent/`.
Active development root: this repository (no remote). Default branch: `main`.

This plan maps the remaining work to the original T01–T17 design packages
defined in [`docs/07-delivery-and-evaluation.md`](07-delivery-and-evaluation.md),
ordered by dependency and risk, and groups them into work packages that
can each ship as a coherent unit. It is the source of truth for **what
v1 actually delivers**. Items deferred beyond v1 are listed explicitly
so they are not silently mislabelled.

v0.1 (`docs/14`, `docs/15`) is the baseline. All 216 baseline tests
pass at the start of phase B (`python3 -m pytest tests/ -q` →
`216 passed in ~35s`). Phase A (T09 query enhancement) is also
complete; see [`docs/17-v1-phase-a-acceptance.md`](17-v1-phase-a-acceptance.md).

## Original design package → v1 status

| T-ID | Title | v0.1 status | v1 status |
|---|---|---|---|
| T01 | Contracts | done | re-validated; v1 adds explicit BackfillRequest + BackfillResult envelopes; rejects unknown CLI flags. |
| T02 | SQLite | done | re-validated; v1 phase B adds `backfill_runs` table for bounded-backfill audit and a `revision_seq` on `source_files` for generation/revision anchoring. |
| T03 | File store + recovery | done | re-validated; segment writes + cursor updates remain in one transaction; rotation triggers a new generation and preserves the old segment file. |
| T04 | Two Readers + atomic cursors | done | re-validated; v1 phase B keeps role/branch/parent/turn lineage for both Claude Code and Codex, refuses to merge equal text across distinct sessions, and enforces `context_only` never becomes target evidence. |
| T05 | Incremental scan | partial (append-only) | **v1 phase B extends** this with rotation/truncation/replacement handling, explicit bounded backfill (`scan --mode backfill --since … --until …`), middle-rewrite detection, isolated run/coverage record, idempotent re-runs, and timezone-aware since/until. Default first-run baseline remains end-of-file; backfill is opt-in and runs against a separate `backfill_runs` row. |
| T06 | Turn assembly + batch planning | done | re-validated; v1 phase B adds long-turn fragmentation at deterministic text boundaries, the 5 turns + 6000 input-token cap (including context/tool/schema overhead), and explicit `needs_context`/`oversize` durable state with unique active ownership per fragment. Cross-batch reference context is source-linked and context_only. |
| T07 | Provider + extraction | done (real CLI + fake) | re-validated; typed Claude CLI provider + bounded prompt/output/timeout. **Phase B does not change the provider.** |
| T08 | Scope + commit | done | re-validated; non-conflicting candidates publish via `CommitService`; conflict projection drives `decide`. |
| T09 | Query | done (keyword) | **shipped in phase A**: opt-in BM25, typed `RewriteProvider`, scope-filter-before-corpus, stable cursor, trace artifact, audit row. A real optional Claude rewrite provider is wired but explicitly off by default; rejection reason is machine-readable. |
| T10 | Script entry (CLI surface) | done (sync) | re-validated; v1 phase B adds `scan --mode backfill --since … --until …` and keeps stable exit codes. **Async `call`, jobs table, independent worker, schedules, OS service manager templates are deferred to P2/P3** and not in scope for phase B. |
| T11 | Worker | not done | **shipped in phase C** (see `docs/19`); project-owned `memory worker` with per-worker lease + heartbeat, configurable schedule (`<vault>/config/worker_schedule.json`), `memory worker --once` for acceptance, launchd template at `design/launchd/com.memory-agent.worker.plist` (not installed by phase C). |
| T12 | Telegram Bot API adapter | **done (P4)** | **shipped in P4** (see `docs/20`): optional project-owned Bot API adapter in the single worker — durable update offset + dedupe (`telegram_updates`, `runtime_state`), short-lived opaque callback tokens bound to proposal + revision + action, allowed chat/user validation, durable outbox with retry + decision ack, decisions through the same `decide` application service as the CLI, bot echo ignored, no Telegram configured ⇒ CLI fallback and zero network calls. Fake-server acceptance only; no real token was read or any message sent. |
| T13 | Audit query service | done (`memory doctor`/`conflicts` + audit log) | **shipped in P5** (see `docs/21`): metadata-first read DTOs, server-side pagination with deterministic cursors, DB/HEAD reconcile status, redacted list views, full bodies only on the explicit evidence endpoint, end-to-end trace `source → batch → model call → candidate → commit`. |
| T14 | Loopback HTTP (FastAPI) | not done | **shipped in P6** (see `docs/21`): bind-only-to-127.0.0.1 HTTP service, `POST /api/v1/commands` goes through the same `handle_call` dispatcher as the CLI, Origin allowlist + session token enforcement on writes, no wildcard CORS, no token in URLs or log lines, model provider is never instantiated by any read path. |
| T15 | React/TypeScript/Vite UI | not done | **shipped in P7** (see `docs/22-v1-p7-p8-acceptance.md`): React 18 + TypeScript + Vite source tree under `web/`, hashed production bundle under `web/dist/`, fixture seeder + 15 Vitest unit tests + 16 Playwright e2e tests (8 desktop + 8 narrow). |
| T16 | UI integration (Playwright) | not done | **shipped in P8** (see `docs/22-v1-p7-p8-acceptance.md`): real FastAPI + fixture vault behind the loopback server; end-to-end navigation source → batch evidence → memory → conflict; double-click idempotency; stale second action is blocked; dark theme + narrow viewport + keyboard navigation. |
| T17 | Privacy deletion + backup/restore + doctor + packaging | partial (`doctor` only) | **shipped in P9** (see `docs/22-v1-p9-acceptance.md`): durable independent privacy manifest + atomic privacy HEAD; recall-time double-read of deletion version; source-level re-ingest guard; consistent SQLite+files backup via the standard-library backup API; quarantine of an old bundle that lacks the latest deletion manifest; reproducible install / run / uninstall scripts (`scripts/install.sh`, `scripts/run.sh`, `scripts/uninstall.sh` — uninstall never deletes the vault); sample config (`samples/config.toml`); launchd template left as the operator-facing template and **not** loaded by this revision; **`scripts/build_ui.sh`** + `pyproject.toml` `[tool.setuptools.package-data]` + `MANIFEST.in` + `importlib.resources` resolution in `memory ui` so the wheel ships the built React SPA and a clean install serves the real interface (not the T14 placeholder). **A backup tarball is now described as restorable only after it has been tested.** The P9 layer ships the audit query service's redaction contract, so backups that contain forgotten memories still drop the forgotten content after restore. |

The v1 release boundary now includes **all T01–T17 packages**. Every
row in the table above is shipped and has a corresponding acceptance
report (`docs/17`–`docs/22`); the README status line is **v1 complete**.
The historical "do not announce v1 release until P2–P9 land" rule was
satisfied as each P2–P9 acceptance report landed — see `docs/19`,
`docs/20`, `docs/21`, `docs/22-v1-p7-p8-acceptance.md`, and
`docs/22-v1-p9-acceptance.md` for the acceptance evidence, and the
"What was not in phase B" section below for the dated historical
boundary.

## Phase execution order

1. **Phase A (shipped)** — T09 query enhancement. [`docs/17-v1-phase-a-acceptance.md`](17-v1-phase-a-acceptance.md) is the acceptance report.
2. **Phase B (shipped)** — T04 + T05 + T06 + T10 source/revision/backfill + long-turn fragmentation + bounded CLI surface for backfill. [`docs/18-v1-phase-b-acceptance.md`](18-v1-phase-b-acceptance.md) is the acceptance report.
3. **Phase C (shipped)** — T10 async `call` + jobs + project-owned worker + source-correction queue + launchd template. [`docs/19-v1-phase-c-acceptance.md`](19-v1-phase-c-acceptance.md) is the acceptance report.
4. **P4 (shipped)** — T12 Telegram Bot API adapter with durable update offset, dedupe, versioned `decide`, ack/outbox; fake server tests, no real token use. [`docs/20-v1-p4-acceptance.md`](20-v1-p4-acceptance.md) is the acceptance report.
5. **P5 (shipped)** — T13 audit query service. [`docs/21-v1-p5-p6-acceptance.md`](21-v1-p5-p6-acceptance.md) is the acceptance report.
6. **P6 (shipped)** — T14 loopback FastAPI with Origin / session-token checks for writes, no wildcard CORS, shared application service. [`docs/21-v1-p5-p6-acceptance.md`](21-v1-p5-p6-acceptance.md) is the acceptance report.
7. **P7 (shipped)** — T15 React/TypeScript/Vite UI. [`docs/22-v1-p7-p8-acceptance.md`](22-v1-p7-p8-acceptance.md) is the acceptance report.
8. **P8 (shipped)** — T16 real API/decision integration and browser test. [`docs/22-v1-p7-p8-acceptance.md`](22-v1-p7-p8-acceptance.md) is the acceptance report.
9. **P9 (shipped)** — T17 privacy deletion barrier, consistent SQLite+files backup/restore, doctor, and packaging that wires the P7/P8-built SPA into the Python wheel. [`docs/22-v1-p9-acceptance.md`](22-v1-p9-acceptance.md) is the acceptance report.

Every phase above is shipped. Phases A–C + P4 land the v0.1 → v1
contract work; P5/P6 add the audit query service and loopback HTTP;
P7/P8 ship the React UI + Playwright integration; P9 ships the
privacy / backup / doctor / packaging work and the wheel now serves
the real SPA from a clean install. Every T01–T17 row in the table
above has a corresponding acceptance report (`docs/17`–`docs/22`).
The README status line is **v1 complete**.

## Phase B work packages

Each phase-B package lists: preconditions, owned paths, explicit
exclusions, falsifiable acceptance, and what is deferred to a later
package without mislabelling phase B complete.

#### Phase B.1 — Backfill command + bounded scope

**Preconditions.** v0.1 scan + plan, phase A contracts.

**Owned paths.**
- `src/memory_agent/application/scan.py::scan_once` — accepts
  `mode="incremental" | "backfill"` and `since`/`until` boundaries.
- `src/memory_agent/application/dispatch.py::_cmd_scan` — wires the
  backfill params and records a `backfill_runs` row.
- `src/memory_agent/contracts/commands.py` — extends `ScanRequest`
  with the new fields and rejects unknown fields.
- `src/memory_agent/interfaces/cli/main.py` — `scan --mode backfill
  --since … --until … --connector …`.
- `src/memory_agent/infrastructure/sqlite/migrations.py` — adds the
  `backfill_runs` table (id, started_at, finished_at, connector,
  roots_json, since_iso, until_iso, timezone, status, counters_json).
- `src/memory_agent/infrastructure/sqlite/repositories.py` —
  `BackfillRunRepo`.

**Exclusions.** No persistence of pre-`since` content into the event
table; no implicit replay on `init`; no live model call.

**Acceptance.**
- Default first-run baseline still pins cursors at end-of-file.
- `scan --mode backfill --since <iso> --until <iso>` registers a row
  in `backfill_runs`, scans the configured roots for events whose
  `source_time` is in `[since, until]`, and never lets a context
  reference *before* `since` become a new target event.
- Re-running the same backfill is idempotent (zero new
  `source_events` rows on the second pass) and the second run is
  recorded as a distinct `backfill_runs` row with status `noop`.
- Ambiguous dates (naive strings) are rejected with
  `INVALID_REQUEST` unless `--timezone` is provided; the same applies
  to `until <= since`.

#### Phase B.2 — Rotation/truncation/replacement handling

**Preconditions.** Phase B.1.

**Owned paths.**
- `src/memory_agent/application/scan.py::scan_once` — adds
  `disposition` handling for `append`, `unchanged`, `replaced`,
  `truncated`, `middle_rewrite`, `unknown_format`, and bumps
  `source_files.generation` when `replaces_event_id` is set.
- `src/memory_agent/infrastructure/sqlite/migrations.py` — adds
  `revision_seq` to `source_files` and ensures the existing
  `complete_offset`/`file_identity`/`generation` triple is preserved
  on rotation.

**Exclusions.** No silent skip on stale events; no fabrication of
`replaces_event_id`; no live model call.

**Acceptance.**
- Append (three writes with a half UTF-8 character at the seam) only
  consumes complete records; the half character stays pending and the
  cursor stops at the safe boundary.
- Truncate/replace with a new file identity creates a new generation
  and a new segment file; the old segment file is **preserved** under
  its original path and marked superseded by audit; the cursor moves
  to the new file's offset.
- Rename with the same `file_identity` keeps the existing progress.
- Middle rewrite (a record before the cursor is changed) creates a
  new generation, marks the unconsumed batch as `superseded`, and
  records an audit row of type `source.corrected`.
- Unknown/corrupt complete records stop the cursor at the byte
  position with a diagnostic and never silently skip.

#### Phase B.3 — Long-turn fragmentation + bounded batching

**Preconditions.** Phase B.2.

**Owned paths.**
- `src/memory_agent/domain/batching.py` — adds
  `split_oversize_turn(turn, max_input_tokens)` that splits at
  deterministic text boundaries (paragraph, list item, sentence)
  with original code-point spans and a `condition_prefix` carry
  when safely possible.
- `src/memory_agent/application/plan_batches.py` — fragments
  oversize user turns into `work_unit`s with `parent_unit_id`
  pointing back at the source event; non-fragmentable oversize
  becomes a `needs_context` durable state with a `reason` and
  never becomes a target.
- `src/memory_agent/contracts/batches.py` — `BatchPolicy` carries
  `max_input_tokens`; the planner never exceeds it (including
  context/tool snippets/schema overhead).
- `src/memory_agent/infrastructure/sqlite/migrations.py` —
  `work_units` table already has `span_start`, `span_end`,
  `split_policy`, `parent_unit_id`; no schema change.

**Exclusions.** No fragment that violates the 5-turns-and-tokens cap;
no `unsafe_candidate` for an unfragmented oversize turn; no live model
call.

**Acceptance.**
- 20 user turns → 4 batches; 12 user turns → 3 batches; tools do
  not become turns.
- A 12k-token user turn is split into fragments at deterministic
  boundaries; each fragment has `span_start`/`span_end` in code
  points, a `condition_prefix` carry when the fragment starts
  mid-condition, and `parent_unit_id` pointing at the source event.
- A user turn that cannot be safely split (no safe boundary, refuses
  to carry the condition prefix) becomes a durable
  `needs_context` state with `reason="oversize_no_safe_split"` and
  no unsafe candidate is published.
- A source event is marked complete only after all of its fragments
  are accounted for in the batch plan; cross-batch reference context
  is `context_only` and never counted as a target.

#### Phase B.4 — Branch/parent/turn identity + replay hygiene

**Preconditions.** Phase B.3.

**Owned paths.**
- `src/memory_agent/adapters/sessions/__init__.py` — `ClaudeCodeReader`
  and `CodexReader` keep `parent_id` / `turn_key` /
  `logical_message_id` as the unique key; context_only rows from
  replayed history are marked `origin="replayed_history"` /
  `context_only=true`.
- `src/memory_agent/application/plan_batches.py` — never demotes a
  context_only event into an active target even if it carries the
  same text as a separate user message; `event_id` uniqueness is
  preserved across sessions.

**Acceptance.**
- Two distinct sessions that contain the same user text produce two
  distinct `source_events.event_id`s and two distinct
  `work_units.unit_id`s; the planner never collapses them.
- A replayed history block (parent lineage pointing at a previous
  session) becomes `context_only` for the current session and never
  a target.

#### Phase B.5 — CLI surface for backfill

**Preconditions.** Phase B.1–B.4.

**Owned paths.**
- `src/memory_agent/interfaces/cli/main.py` — `scan --mode {incremental,
  backfill} --since <iso> --until <iso> --timezone <tz>`.
- `src/memory_agent/application/dispatch.py::_cmd_scan` —
  `_cmd_backfill_status` returns the most recent `backfill_runs`
  row.

**Acceptance.**
- CLI subprocess test on a clean temp vault: init → backfill →
  incremental append → no duplicate target events.
- CLI exit codes remain stable (`0`/`2`/`5`/`6`); unknown flags
  rejected by argparse.

## v0.1 defects discovered during the audit (addressed by phase A + B)

Phase A addressed five of the six defects listed in the previous plan
revision (see `docs/17`). The remaining defect
(`QueryPlan/Variant` provenance beyond `origin`) is also addressed in
phase A. No new defects are introduced by phase B.

## What was not in phase B (and its current v1 status)

> **Historical note (dated 2026-10-09).** This section preserves the
> phase B release boundary from the original v1 plan. Every "future
> blocker" listed below has since landed. The bullets are split into
> two groups so the historical phase B boundary can never be confused
> with current v1 status: (a) packages that were deferred *from phase
> B into v1* and are now shipped, and (b) capabilities that are
> genuinely out of v1 scope. Cross-references point at the acceptance
> reports that closed each row.

### Formerly deferred v1 packages — now shipped

- T11 project-owned worker + OS service-manager schedules (P2/P3).
  → **Shipped in phase C.** See [`docs/19-v1-phase-c-acceptance.md`](19-v1-phase-c-acceptance.md).
- T12 Telegram Bot API adapter with durable update offset, dedupe,
  versioned `decide`, ack/outbox, and fake server tests (P4).
  → **Shipped in P4.** See [`docs/20-v1-p4-acceptance.md`](20-v1-p4-acceptance.md).
- T13 dedicated audit query service (P5).
  → **Shipped in P5.** See [`docs/21-v1-p5-p6-acceptance.md`](21-v1-p5-p6-acceptance.md).
- T14 loopback FastAPI with Origin/session-token checks for writes,
  no wildcard CORS, shared application service (P6).
  → **Shipped in P6.** See [`docs/21-v1-p5-p6-acceptance.md`](21-v1-p5-p6-acceptance.md).
- T15 React/TypeScript/Vite UI (P7).
  → **Shipped in P7.** See [`docs/22-v1-p7-p8-acceptance.md`](22-v1-p7-p8-acceptance.md).
- T16 real API/decision integration and browser test (P8).
  → **Shipped in P8.** See [`docs/22-v1-p7-p8-acceptance.md`](22-v1-p7-p8-acceptance.md).
- T17 privacy deletion barrier and consistent SQLite+files backup /
  restore, doctor-driven restore check, independent packaging/runbook
  (P9). **A backup tarball is now described as restorable only after
  it has been tested**; localhost alone is still not described as
  securing writes.
  → **Shipped in P9.** See [`docs/22-v1-p9-acceptance.md`](22-v1-p9-acceptance.md). The wheel ships the P7/P8-built React SPA so a clean `pip install` serves the real interface, not the T14 placeholder.
- **Source-correction reconciliation queue (NEW, surfaced by the P0
  source-integrity fix in phase B.2).** Phase B writes a
  `source.corrected` audit row and bumps `source_files.generation`
  / `revision_seq` for the source row, but the `memory_projection`
  rows that referenced the pre-rewrite `event_id` are **not** walked
  by phase B alone. The reconciliation queue was added in phase C
  + phase C repair-2: a separate queue / worker (a) drains the
  `source.corrected` audit log, (b) decides per memory whether to
  retire / deprecate / re-link against the new `event_id`, and (c)
  leaves an audit-trail of every decision. `adopt_new` / `scope_split`
  publish through `CommitService`. See
  [`docs/19-v1-phase-c-acceptance.md`](19-v1-phase-c-acceptance.md)
  for the precise boundary between "detected" and "reconciled".

### Out of v1 scope (deferred beyond v1)

- Vector recall.
- Semantic embeddings.
- Persistent search services.

The README status line is **v1 complete**. The historical rule
"do not announce v1 release until P2–P9 land" was satisfied as each
P2–P9 acceptance report landed; see `docs/19`, `docs/20`, `docs/21`,
`docs/22-v1-p7-p8-acceptance.md`, and `docs/22-v1-p9-acceptance.md`
for the evidence.