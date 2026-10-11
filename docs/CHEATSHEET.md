# Operator CHEATSHEET

A recipe-driven companion to the README + acceptance docs. The README is a
happy-path quick start; this sheet is the day-2 cases acceptance tests
repeatedly exercise (`docs/18-v1-phase-b-acceptance.md`,
`docs/19-v1-phase-c-acceptance.md`, `docs/22-v1-p9-acceptance.md`,
`docs/22-v1-p7-p8-acceptance.md`).

Every recipe assumes the venv is activated (`source .venv/bin/activate`)
and uses `python -m memory_agent` so it works under `pip install -e .` and
under a clean `pip install` wheel alike. Substitute `memory` if you have
the console script on `PATH`.

The recipes here are tested by
[`tests/docs/test_cheatsheet_examples.py`](../tests/docs/test_cheatsheet_examples.py)
— that suite parses every fenced bash block, splits the command line, and
asserts each `--flag` is documented by the matching subcommand's `--help`.
Recipes that mutate state must not be run blindly against a real vault.

---

## 1. Initialise a fresh vault and baseline existing files at EOF

```bash
python -m memory_agent --vault /tmp/my_vault init \
    --root "claude-code=/tmp/fake_claude" \
    --root "codex=/tmp/fake_codex"
```

* `--root <connector>=<path>` is repeatable; new roots are baselined
  without disturbing cursors for already-known roots.
* `init` is idempotent. Re-run after adding a new root; the cursor for
  the existing roots stays at EOF.
* After `init`, only files *appended* after the baseline cursor are
  consumed. There is no implicit backfill (recipe 3 covers that).

`docs/15-v1-acceptance.md` — "Explicit init baseline".

---

## 2. Append content → maintain → recall

The end-to-end happy path. The appended line must contain the trigger
word `use` (or whatever your reconciler recognises) at the start; the
recall query must match a token from that line.

```bash
# 2a. Append a fresh complete user message to the session.
cat >> /tmp/fake_claude/projects/proj_x/sess1.jsonl <<'EOF'
{"type":"user","uuid":"u02","message":{"role":"user","content":"use Python for the prototype always"},"timestamp":"2026-10-08T10:01:00Z","sessionId":"sess1","parentUuid":"a01"}
EOF

# 2b. maintain (scan + plan + process). With the fake provider, no
# real Claude auth is required.
MEMORY_AGENT_MODEL_PROVIDER=fake \
    python -m memory_agent --vault /tmp/my_vault maintain \
    --root "claude-code=/tmp/fake_claude"

# 2c. Recall with evidence-backed keyword search.
python -m memory_agent --vault /tmp/my_vault recall \
    --query "Python prototype" --limit 5
```

* `maintain` is the daily combo: `scan → batch.plan → batch.process`. It
  never drops the backlog on the floor — it stops at the configured
  budget (`--max-batches`, default 5).
* Repeated `maintain` with no new content is idempotent: zero new
  `source_events`, zero new memories.

`README.md` "Quick start" — same end-to-end.

---

## 3. Backfill windowed historical content

For ingesting content that pre-dates the EOF baseline. Records a
separate `backfill_runs` row and **does not** advance
`source_files.complete_offset`.

```bash
python -m memory_agent --vault /tmp/my_vault scan \
    --root "claude-code=/tmp/fake_claude" \
    --mode backfill \
    --since 2026-09-01T00:00:00+08:00 \
    --until 2026-09-30T23:59:59+08:00 \
    --timezone Asia/Shanghai
```

* `--since` / `--until` are RFC 3339 (offset or `Z`); naive timestamps
  are rejected with `INVALID_REQUEST`.
* `--timezone` is recorded on the `backfill_runs` row but does **not**
  reinterpret the source timestamps.
* Re-running the same window over the same file is idempotent: zero
  new `source_events`.
* Events inside `[since, until]` become targets. Events outside the
  window can be carried as `context_only` (origin =
  `replayed_history`) but never as targets.

`docs/18-v1-phase-b-acceptance.md` — backfill windowing.

---

## 4. List and decide a conflict

```bash
# 4a. List open conflicts (every row carries its current
# `proposal_revision` — record the value you act on).
python -m memory_agent --vault /tmp/my_vault conflicts

# 4b. Decide. `--idempotency-key` is required for non-defer actions.
python -m memory_agent --vault /tmp/my_vault decide \
    --conflict-id <conflict_id> \
    --proposal-revision 3 \
    --action adopt_new \
    --note "candidate is more recent than the existing active memory" \
    --idempotency-key decide-2026-10-08-attempt-1
```

* `--action` choices: `adopt_new` / `keep_old` / `scope_split` /
  `defer`. `defer` leaves the conflict open; the others publish
  through `CommitService`.
* Stale `--proposal-revision` is rejected with `INVALID_REQUEST`.
  Refresh with `memory conflicts` and try again.
* Same `--idempotency-key` + same params returns the original
  decision; same key + changed params returns `IDEMPOTENCY_CONFLICT`
  (exit 4).

`docs/19-v1-phase-c-acceptance.md` — decide contract; `docs/22-v1-p7-p8-acceptance.md` —
UI decide flow.

---

## 5. Decide a source-correction proposal

A memory that cited a now-rewritten `event_id` lands in the
source-correction queue. The UI surfaces it under "审计与作业 →
filter `source.corrected`"; the CLI path uses
`memory call --operation source_correction.decide`.

```bash
cat > /tmp/src_corr.json <<'EOF'
{
  "api_version": 1,
  "request_id": "src_corr_demo",
  "client_id": "local_script",
  "idempotency_key": "src_corr_demo_2026-10-08",
  "operation": "source_correction.decide",
  "params": {
    "proposal_id": "<proposal_id>",
    "expected_proposal_revision": 1,
    "action": "scope_split"
  }
}
EOF

python -m memory_agent --vault /tmp/my_vault call \
    --request-file /tmp/src_corr.json
```

* `adopt_new` / `scope_split` publish through `CommitService` and
  reference the **new** `event_id` + new revision hash. The old
  memory is marked `superseded`; the proposal row's
  `superseded_memory_id` / `superseded_head_commit_id` are recorded.
* Stale `expected_proposal_revision` is rejected with
  `INVALID_REQUEST`.
* A `publish` failure leaves the proposal `pending` so the operator
  can retry.

`docs/19-v1-phase-c-acceptance.md` — "Source-correction
reconciliation queue"; `docs/22-v1-p9-acceptance.md` — phase C
repair-2 acceptance.

---

## 6. Privacy: forget → list receipts → inspect → unblock

The privacy barrier is the **only** content-removal operation. Every
forget persists an immutable manifest + atomic HEAD swap; the recall
path reads the privacy HEAD twice (start and just before return).

```bash
# 6a. Forget a memory. `--idempotency-key` is required.
python -m memory_agent --vault /tmp/my_vault privacy forget \
    --kind memory \
    --target mem_demo_001 \
    --idempotency-key forget-2026-10-08 \
    --note "user requested removal"

# 6b. List every privacy receipt.
python -m memory_agent --vault /tmp/my_vault privacy list

# 6c. Inspect one receipt.
python -m memory_agent --vault /tmp/my_vault privacy receipt \
    --op-id op_<...>

# 6d. Lift a `source_block` re-ingest guard (does NOT restore memory).
python -m memory_agent --vault /tmp/my_vault privacy unblock \
    --op-id op_<...> \
    --idempotency-key unblock-2026-10-08
```

* `forget` accepts `--kind memory|event|source_block`. Each kind
  targets different rows (`memory_id`, `event_id`,
  `source_file_id`); `--target` is repeatable.
* The receipt is content-free. Only `op_id` / `kind` / `targets` /
  `version` / `created_at` survive. No statement, evidence quote,
  locator, or full-text audit payload is preserved.

`docs/22-v1-p9-acceptance.md` — privacy barrier.

---

## 7. Backup create → list → restore into a clean vault

Consistent SQLite+files snapshot via `sqlite3.Connection.backup()`,
not a `cp` of `state.sqlite`. Restore validates sqlite sha256 +
deletion manifest hash against the **operator's live vault's
authoritative privacy ledger** before materialising.

```bash
# 7a. Create a tar backup. Live vault must have a privacy HEAD.
python -m memory_agent --vault /tmp/my_vault backup create \
    --output /tmp/my_vault.tar \
    --label "before_rollout_2026-10-08"

# 7b. Inspect the manifest.
python -m memory_agent --vault /tmp/my_vault backup list \
    --bundle /tmp/my_vault.tar

# 7c. Restore into a clean, invented vault.
python -m memory_agent --vault /tmp/any_uninitialised_vault \
    backup restore \
    --bundle /tmp/my_vault.tar \
    --target /tmp/my_vault_restored

# 7d. Doctor + recall against the restored vault.
python -m memory_agent --vault /tmp/my_vault_restored doctor
python -m memory_agent --vault /tmp/my_vault_restored recall \
    --query "Python prototype"
```

* Restore **always** consults the live vault's privacy ledger (not a
  caller-supplied integer). An old bundle whose HEAD is behind the
  live ledger is quarantined; the target stays unmounted.
* The unsafe `--no-quarantine` and `--live-privacy-head-version`
  CLI flags have been removed entirely. argparse rejects them as
  unknown — so a forgotten memory cannot be resurrected via an old
  bundle even by an attacker who controls the CLI argv.

`docs/22-v1-p9-acceptance.md` — backup / restore contract.

---

## 8. Diagnose a quarantine: live ledger advanced past the bundle

A restore that fails with "live vault is at privacy HEAD v5, bundle is
at v3 — refusing to mount" is the safety net doing its job. Recovery:

```bash
# 8a. Inspect the live ledger.
python -m memory_agent --vault /tmp/my_vault privacy list | head -5
python -m memory_agent --vault /tmp/my_vault doctor | head -20

# 8b. Decide: forget something newer to bring the live ledger in
# scope, OR restore into a *clean* vault (recipe 7c). Do NOT edit
# `privacy/HEAD.json` by hand — the DB backstop rejects a forged
# same-version bundle that lacks the receipts the DB still remembers.

# 8c. If the bundle is genuinely too old, retire it; do not patch
# the bundle to inflate its HEAD version.
```

* The DB may lag disk (crash-window between HEAD swap and DB mirror)
  but the DB may NOT lead disk. A bundle whose DB records deletion
  rows the on-disk manifest does not have is itself quarantined.
* `memory init` is the only path that writes a v0 privacy HEAD on a
  fresh vault; it requires both absence of deletion manifests AND
  absence of `privacy_deletions` rows in the state DB
  (`ensure_privacy_head`).

`docs/22-v1-p9-acceptance.md` — "fail-closed restore".

---

## 9. Worker: single pass, scheduled long-run

```bash
# 9a. Run exactly one pass (used by tests + acceptance).
python -m memory_agent --vault /tmp/my_vault worker --once \
    --force-daily

# 9b. Long-running supervised worker (intended for launchd).
python -m memory_agent --vault /tmp/my_vault worker \
    --worker-id worker_laptop \
    --lease-ttl 30
```

* `--once` drains queued jobs + (optionally) a single scheduled
  pass, then exits. Tests use this.
* The long-running mode honours
  `<vault>/config/worker_schedule.json` (default
  `every_seconds=1`, `daily_at_hour=2`, weekly after daily
  catch-up, monthly after daily catch-up).
* The worker owns the lease + heartbeat + backpressure.
  `worker_leases` is the vault-level writer lock — only one worker
  process may run a pass at a time. A second worker with a different
  `worker_id` is refused while any active lease exists.
* Stale `running` jobs whose heartbeat has expired are reset to
  `queued` at the start of every pass, so a crashed worker doesn't
  leave the job stuck.

`docs/19-v1-phase-c-acceptance.md` — worker contracts.

---

## 10. UI: launch + reload for dev + read-only mode

```bash
# 10a. Launch the loopback UI on the default port (8440, clear of
# the local Octopus service on 8420).
python -m memory_agent --vault /tmp/my_vault ui \
    --session-token dev-token \
    --ui-dist "$(pwd)/web/dist"
# → http://127.0.0.1:8440/

# 10b. Dev mode with hot reload on source changes (no prod use).
python -m memory_agent --vault /tmp/my_vault ui \
    --session-token dev-token \
    --reload

# 10c. Read-only mode (POST writes disabled; useful for demos).
python -m memory_agent --vault /tmp/my_vault ui --no-token
```

* `--port` overrides the default. `--host` MUST be `127.0.0.1` /
  `localhost` / `::1` — anything else is refused at startup.
* `--no-token` disables POST writes entirely (the UI runs as a
  read-only dashboard). Useful for screencasts where you don't want
  accidental decides.
* The React SPA is shipped inside the wheel via
  `scripts/build_ui.sh`. A clean `pip install` serves the real SPA
  from `memory ui` without a developer checkout.

`docs/22-v1-p7-p8-acceptance.md` — UI launch + read-only mode.

---

## 11. Async `call` with idempotency key + progress query

For batch operations that take longer than the synchronous budget.

```bash
# 11a. Submit a durable async job.
cat > /tmp/daily.json <<'EOF'
{
  "api_version": 1,
  "request_id": "daily_demo",
  "client_id": "local_script",
  "idempotency_key": "daily_demo_2026-10-08",
  "operation": "maintain",
  "mode": "async",
  "params": {
    "connectors": ["claude-code"],
    "max_batches": 20,
    "batch_policy": {"max_turns_per_batch": 5, "max_input_tokens": 6000},
    "commit_policy": "validated"
  },
  "limits": {"deadline_seconds": 1800, "max_model_calls": 60}
}
EOF

python -m memory_agent --vault /tmp/my_vault call \
    --request-file /tmp/daily.json \
    --mode async

# 11b. Poll status.
python -m memory_agent --vault /tmp/my_vault job status \
    --job-id job_<...>

# 11c. Cancel a queued/running job. Cancelling a job that has
# already published commits is a no-op for the commits (the cancel
# stops subsequent batches, not past ones).
python -m memory_agent --vault /tmp/my_vault job cancel \
    --job-id job_<...> \
    --expected-revision 1

# 11d. Retry a failed/cancelled job.
python -m memory_agent --vault /tmp/my_vault job retry \
    --job-id job_<...> \
    --expected-revision 1
```

* `(client_id, idempotency_key)` is bound to a canonical
  `request_hash`. Same key + same params returns the original
  `job_id`; same key + changed params returns `IDEMPOTENCY_CONFLICT`
  (exit 4).
* Unknown fields on `params` / unknown operations return
  `INVALID_REQUEST` (exit 2). The CLI does not silently ignore
  unknown flags.

`docs/19-v1-phase-c-acceptance.md` — async `call` contract.

---

## 12. Diagnose a stale-revision decide failure

The UI surfaces this as a "stale" banner; the CLI surfaces it as
exit 2 with `INVALID_REQUEST`.

```bash
# 12a. Re-list to read the current proposal_revision.
python -m memory_agent --vault /tmp/my_vault conflicts | head -20

# 12b. Retry with the fresh revision. Do NOT add a new flag to
# bypass the version check — the proposal_revision guard is the
# CAS that keeps two operators from clobbering each other.
python -m memory_agent --vault /tmp/my_vault decide \
    --conflict-id <conflict_id> \
    --proposal-revision <current_revision_from_step_a> \
    --action keep_old \
    --idempotency-key decide-2026-10-08-retry
```

* The stale-revision guard is the **only** thing standing between
  two operators / the UI and a silent overwrite of an active
  memory. It is intentionally not bypassable.
* The UI's `stale-banner` testid is asserted in
  `tests-e2e/audit-ui.e2e.test.ts`; that same path is the CLI
  `INVALID_REQUEST` (exit 2) path.

`docs/22-v1-p7-p8-acceptance.md` — stale-revision guard.

---

## Cross-reference

| Topic | Recipe | Doc |
|---|---|---|
| Initialise + baseline | 1 | `docs/15-v1-acceptance.md` |
| Daily maintain loop | 2 | `README.md` "Quick start" |
| Backfill windowing | 3 | `docs/18-v1-phase-b-acceptance.md` |
| Conflict decide | 4 | `docs/19-v1-phase-c-acceptance.md` |
| Source-correction decide | 5 | `docs/19-v1-phase-c-acceptance.md` repair-2 |
| Privacy forget / receipts | 6 | `docs/22-v1-p9-acceptance.md` |
| Backup / restore | 7 | `docs/22-v1-p9-acceptance.md` |
| Quarantine diagnosis | 8 | `docs/22-v1-p9-acceptance.md` fail-closed |
| Worker (single + scheduled) | 9 | `docs/19-v1-phase-c-acceptance.md` |
| UI launch / dev / read-only | 10 | `docs/22-v1-p7-p8-acceptance.md` |
| Async call + job status | 11 | `docs/19-v1-phase-c-acceptance.md` |
| Stale-revision decide | 12 | `docs/22-v1-p7-p8-acceptance.md` |
