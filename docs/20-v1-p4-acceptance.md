# P4 / T12 acceptance — Telegram Bot API decision loop

Task: implement the smallest complete v1 Telegram decision channel (T12)
— optional project-owned Bot API long-polling receiver inside the single
worker, allowed chat/user validation, short-lived callback tokens bound
to proposal + expected revision + action, a durable outbox with retry
and idempotent send, and a durable update offset with dedupe.

Everything in this report was exercised against a **fake loopback Bot
API server** and **invented fixtures** with a **fake token** supplied
through an environment variable. No real token was read, no real
message was sent, no real session was scanned, no daemon was installed,
nothing was committed or pushed.

Status: **P4 complete, acceptance passed.** T13–T17 are untouched and
belong to later packages.

---

## 1. What was built

| Path | Role |
|---|---|
| `src/memory_agent/adapters/telegram/config.py` | `<vault>/config/telegram.json` loader — stores only references; the bot token comes from the env var named by `token_env` |
| `src/memory_agent/adapters/telegram/client.py` | stdlib-urllib Bot API client (`getUpdates`, `sendMessage`); configurable `base_url` for the fake server; errors redact the token |
| `src/memory_agent/adapters/telegram/formatter.py` | notification text + inline keyboard; `callback_data` carries only an opaque token id + action |
| `src/memory_agent/adapters/telegram/service.py` | the pass: enqueue missing notifications → drain the outbox with backoff → poll once and process updates |
| `src/memory_agent/application/worker.py` | one `_telegram_pass` step per `run_worker_once` (wrapped; never raises) |
| `src/memory_agent/infrastructure/sqlite/migrations.py` | migration 8: `telegram_updates`, `telegram_callback_tokens` |
| `src/memory_agent/infrastructure/sqlite/repositories.py` | `RuntimeStateRepo`, `NotificationOutboxRepo`, `TelegramUpdateRepo`, `TelegramCallbackTokenRepo` |
| `tests/integration/test_telegram_v1.py` | 9 fake-server acceptance tests |

Key invariants:

* **Same application service as CLI.** A valid callback calls
  `dispatch(operation="decide", client_id="user_telegram", …)` —
  the exact service `memory decide` uses, with the same
  `proposal_revision` compare-and-swap.
* **Receipt before offset.** The decision is committed first; then one
  SQLite transaction writes the `telegram_updates` receipt, marks the
  callback token consumed, and advances `runtime_state.telegram.update_offset`.
  A crash before that transaction replays the update and is recognised
  by the `conflict.decided` audit row → `already_applied`, never a
  second commit.
* **Dedupe / ordering.** `update_id` is the primary key of
  `telegram_updates`; an update below the durable offset is recorded as
  `ignored/out_of_order` and never moves the cursor backwards.
* **Identity + token.** `allowed_chats` ∧ `allowed_users` must pass;
  the opaque 16-hex token is bound to (conflict_id, proposal_revision,
  action, chat, user) with an expiry; the button's action must equal
  the token's action; the token must not already be consumed.
* **No commit on bad input.** unknown chat/user, unknown/malformed
  callback, unknown/expired/mismatched/consumed token, stale revision,
  already-decided proposal → `rejected`, audit row, no commit.
* **Outbox.** Notification rows are keyed by
  `sha256(conflict_id, revision, channel, kind)` so a crash between
  HEAD publish and enqueue is repaired on the next pass, and a retry
  never creates a duplicate. Send failures stay `pending` with
  `attempt+1` and `next_retry = now + backoff × min(attempt, 10)`.
* **Bot echo.** `is_bot` senders and non-callback messages are recorded
  as `ignored`; no decision, no source event.
* **No token at rest.** The bot token is read from the environment,
  used only as a URL path segment, and never written to SQLite,
  artifacts, or audit rows (asserted by test).
* **Offline safety.** Every network call is wrapped; an outage leaves
  the outbox and offset untouched, the worker pass still succeeds, and
  local `scan`/`recall`/`decide` are unaffected.
* **Not configured ⇒ no network.** Without `config/telegram.json` (or
  without the token env var) the pass returns
  `enabled: False` without constructing a client.

Config reference (all optional; project-owned file only):

```json
{
  "enabled": true,
  "token_env": "MEMORY_AGENT_TELEGRAM_TOKEN",
  "base_url": "https://api.telegram.org",
  "allowed_chats": ["-1001234567890"],
  "allowed_users": ["555000111"],
  "notify_chat_id": "-1001234567890",
  "notify_user_id": "555000111",
  "callback_ttl_seconds": 86400,
  "poll_timeout_seconds": 0,
  "max_updates_per_pass": 20,
  "backoff_seconds": 30
}
```

---

## 2. Commands and results

Focused acceptance run:

```
$ python3 -m pytest tests/integration/test_telegram_v1.py -q
9 passed in 6.20s
```

Full suite (run after the focused run; the concurrent provider/UI tasks
were not modifying this tree during the run, but treat it as
provisional per the task brief):

```
$ python3 -m pytest -q
339 passed, 1 warning in 59.24s
```

(330 pre-existing tests + 9 new; the single warning is the existing
Starlette `httpx` deprecation notice from `fastapi/testclient`.)

Scenario → test map:

| Acceptance scenario | Test |
|---|---|
| conflict created from invented data → notification sent → valid callback → versioned decide → new HEAD/projection → ack | `test_full_loop_notification_decide_ack` |
| replay update → no second commit; out-of-order update ignored | `test_replayed_and_out_of_order_updates_never_commit_twice` |
| invalid user / chat / token / stale revision (plus expired token, action mismatch, bot callback) → rejected, conflict stays open | `test_invalid_callbacks_are_rejected_without_commit` |
| crash/restart between decision and offset advance → exactly once | `test_crash_between_decision_and_offset_advances_once` |
| offline send + offline poll → pending work preserved, recall still works, retry after backoff succeeds | `test_offline_send_and_poll_preserve_pending_work` |
| no Telegram configured → CLI `decide` works, zero network calls | `test_no_telegram_configured_makes_no_network_calls` |
| bot echo never becomes a decision or a source event | `test_bot_echo_message_is_ignored` |
| no token in DB / artifacts / audit rows | `test_token_never_appears_in_db_or_artifacts` |
| callback token is bound to proposal + revision + action and carries no memory text | `test_callback_token_is_bound_to_revision_and_action` |

Notable assertions inside the loop test:

* pass 1 (via `run_worker_once`, proving worker integration):
  `telegram.enqueued == 1`, `telegram.sent == 1`, and the fake server
  received a `sendMessage` whose inline keyboard contains four buttons.
* pass 2: `decided == 1`, `rejected == 0`; `HEAD.json` moved; the new
  commit manifest contains `mem_ts_proto`; `conflict_projection.status
  == 'decided'`; receipt `status == 'decided'`, `action ==
  'adopt_new'`; cursor `== 1002`.
* pass 3: the ack (`"已记录你的决定"`) is delivered exactly once.

---

## 3. Blockers, deviations and known limits

1. **No real-network validation.** By design: only the fake server was
   used. Real Bot API quirks (429 flood limits, webhook-only bots,
   long-poll timeouts) are out of scope for v1 and would be handled by
   the existing outbox backoff, but are *untested*.
2. **Single-receiver enforcement.** `docs/06` wants one receiver per
   bot. This implementation relies on the existing vault-level worker
   lease (`WorkerLeaseRepo`) — a second `memory worker` on the same
   vault is refused, so two pollers cannot compete. A dedicated
   "bot_id lease" row is not added in v1.
3. **Free-text replies are not parsed in v1.** Only inline-keyboard
   callbacks create decisions; a typed reply is recorded as
   `ignored/not_a_callback`, matching the "ambiguous replies never
   become an overwrite" rule in `docs/05`.
4. **Projection convergence after decide.** The pre-existing
   `_cmd_decide` publishes the commit but leaves `finalize` to the
   standard recovery path (`recover_unfinished_intents`, run at the head
   of every `dispatch`). The Telegram path therefore observes the same
   behaviour as the CLI — the test asserts the new HEAD + manifest
   immediately and the projection after one further dispatch.
   No change was made to `application/dispatch.py` (concurrent-task
   ownership).
5. **CLI / `dispatch.py` untouched.** Configuration is the
   `config/telegram.json` file plus the `MEMORY_AGENT_TELEGRAM_TOKEN`
   environment variable; the only edits outside the new adapter,
   `worker.py`, `repositories.py` and `migrations.py` are this report
   and the T12 row / phase list in `docs/16-v1-implementation-plan.md`.
6. **Rate limiting / snooze semantics.** `defer` is delivered as a
   normal decision (`defer` leaves the proposal open); per-topic
   reminder coalescing and mute are not implemented in v1.
7. **Full-suite result is provisional** — a provider regression repair
   and a UI task were active concurrently; the controller should do the
   final integration run.

## 4. Stop boundary

Work stopped at P4. T13–T17 (audit query service, loopback FastAPI,
React UI, browser integration, privacy/backup/packaging) are untouched.
