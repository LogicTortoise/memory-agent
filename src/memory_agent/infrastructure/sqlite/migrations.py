"""SQLite migrations.

Each migration is a tuple (id, sql). apply_initial_migration runs them
in order inside a transaction and records applied ids in schema_migrations.
"""

from __future__ import annotations

import sqlite3

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        # 0001: initial schema covering source_files, segments, source_events,
        # 0001: initial schema covering source_files, segments, source_events,
        # turns, work_units, batches, batch_members, candidate_sets, jobs,
        # runs, model_calls, memory_projection, conflict_projection,
        # commit_intents, audit_events, notification_outbox, runtime_state.
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          id INTEGER PRIMARY KEY,
          applied_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS source_files (
          source_file_id TEXT PRIMARY KEY,
          connector TEXT NOT NULL,
          session_id TEXT NOT NULL,
          branch_id TEXT NOT NULL,
          canonical_path TEXT NOT NULL,
          file_identity TEXT NOT NULL,
          generation INTEGER NOT NULL,
          adapter_version TEXT NOT NULL,
          complete_offset INTEGER NOT NULL DEFAULT 0,
          size INTEGER NOT NULL DEFAULT 0,
          mtime TEXT,
          last_scan_at TEXT,
          last_status TEXT,
          last_error TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_source_files_path_gen
          ON source_files(canonical_path, generation);

        CREATE TABLE IF NOT EXISTS segments (
          segment_id TEXT PRIMARY KEY,
          source_file_id TEXT NOT NULL REFERENCES source_files(source_file_id),
          generation INTEGER NOT NULL,
          path TEXT NOT NULL,
          sha256 TEXT NOT NULL,
          start_offset INTEGER NOT NULL,
          end_offset INTEGER NOT NULL,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS source_events (
          event_id TEXT PRIMARY KEY,
          canonical_event_key TEXT NOT NULL UNIQUE,
          session_id TEXT NOT NULL,
          branch_id TEXT NOT NULL,
          source_file_id TEXT NOT NULL,
          segment_id TEXT NOT NULL REFERENCES segments(segment_id),
          role TEXT NOT NULL,
          kind TEXT NOT NULL,
          revision_hash TEXT NOT NULL,
          source_time TEXT,
          discovered_at TEXT NOT NULL,
          replaces_event_id TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_source_events_session_time
          ON source_events(session_id, branch_id, source_time);

        CREATE TABLE IF NOT EXISTS turns (
          turn_id TEXT PRIMARY KEY,
          session_id TEXT NOT NULL,
          branch_id TEXT NOT NULL,
          ordinal INTEGER NOT NULL,
          revision INTEGER NOT NULL,
          state TEXT NOT NULL,
          first_event_id TEXT NOT NULL,
          last_event_id TEXT NOT NULL,
          UNIQUE(session_id, branch_id, ordinal, revision)
        );

        CREATE TABLE IF NOT EXISTS work_units (
          unit_id TEXT PRIMARY KEY,
          session_id TEXT NOT NULL,
          branch_id TEXT NOT NULL,
          turn_id TEXT,
          event_id TEXT,
          span_start INTEGER,
          span_end INTEGER,
          split_policy TEXT,
          parent_unit_id TEXT,
          epoch INTEGER NOT NULL DEFAULT 1,
          UNIQUE(session_id, branch_id, turn_id, event_id, epoch)
        );

        CREATE TABLE IF NOT EXISTS batches (
          batch_id TEXT PRIMARY KEY,
          session_id TEXT NOT NULL,
          branch_id TEXT NOT NULL,
          policy_version TEXT NOT NULL,
          input_hash TEXT NOT NULL,
          status TEXT NOT NULL,
          attempt INTEGER NOT NULL DEFAULT 0,
          lease_token TEXT,
          lease_until TEXT,
          next_retry_at TEXT,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_batches_status ON batches(status, next_retry_at);

        CREATE TABLE IF NOT EXISTS batch_members (
          batch_id TEXT NOT NULL REFERENCES batches(batch_id),
          unit_id TEXT NOT NULL,
          purpose TEXT NOT NULL,
          ord INTEGER NOT NULL,
          epoch INTEGER NOT NULL,
          active_target INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(batch_id, unit_id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_active_target
          ON batch_members(unit_id, epoch) WHERE active_target = 1;

        CREATE TABLE IF NOT EXISTS candidate_sets (
          candidate_set_id TEXT PRIMARY KEY,
          batch_id TEXT NOT NULL REFERENCES batches(batch_id),
          artifact_ref TEXT NOT NULL,
          sha256 TEXT NOT NULL,
          prompt_version TEXT NOT NULL,
          model TEXT NOT NULL,
          validation_state TEXT NOT NULL,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS jobs (
          job_id TEXT PRIMARY KEY,
          request_id TEXT NOT NULL,
          client_id TEXT NOT NULL,
          idempotency_key TEXT,
          request_hash TEXT NOT NULL,
          operation TEXT NOT NULL,
          params_ref TEXT,
          state TEXT NOT NULL,
          cancel_requested INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          UNIQUE(client_id, idempotency_key)
        );

        CREATE TABLE IF NOT EXISTS runs (
          run_id TEXT PRIMARY KEY,
          job_id TEXT NOT NULL REFERENCES jobs(job_id),
          stage TEXT NOT NULL,
          batch_id TEXT,
          attempt INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL,
          error_code TEXT,
          started_at TEXT NOT NULL,
          ended_at TEXT,
          output_ref TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_runs_job ON runs(job_id, stage);

        CREATE TABLE IF NOT EXISTS model_calls (
          call_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL REFERENCES runs(run_id),
          purpose TEXT NOT NULL,
          provider TEXT NOT NULL,
          model TEXT NOT NULL,
          prompt_version TEXT NOT NULL,
          usage_json TEXT,
          duration_ms INTEGER,
          input_ref TEXT,
          output_ref TEXT,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS memory_projection (
          memory_id TEXT NOT NULL,
          revision INTEGER NOT NULL,
          status TEXT NOT NULL,
          scope_json TEXT NOT NULL,
          project_ids_json TEXT NOT NULL,
          domains_json TEXT NOT NULL,
          commit_id TEXT NOT NULL,
          recorded_at TEXT NOT NULL,
          PRIMARY KEY(memory_id, revision)
        );
        CREATE INDEX IF NOT EXISTS ix_memory_proj_status ON memory_projection(status);

        CREATE TABLE IF NOT EXISTS conflict_projection (
          conflict_id TEXT NOT NULL,
          revision INTEGER NOT NULL,
          status TEXT NOT NULL,
          old_memory_ref TEXT,
          new_memory_ref TEXT,
          commit_id TEXT NOT NULL,
          opened_at TEXT NOT NULL,
          PRIMARY KEY(conflict_id, revision)
        );

        CREATE TABLE IF NOT EXISTS commit_intents (
          commit_id TEXT PRIMARY KEY,
          parent_id TEXT,
          manifest_ref TEXT NOT NULL,
          batch_ids_json TEXT NOT NULL,
          target_unit_ids_json TEXT NOT NULL,
          expected_revisions_json TEXT NOT NULL,
          privacy_epoch TEXT NOT NULL,
          phase TEXT NOT NULL,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS audit_events (
          audit_id TEXT PRIMARY KEY,
          seq INTEGER NOT NULL UNIQUE,
          occurred_at TEXT NOT NULL,
          actor TEXT NOT NULL,
          action TEXT NOT NULL,
          object_type TEXT,
          object_id TEXT,
          correlation_json TEXT,
          before_after_json TEXT,
          outcome TEXT NOT NULL,
          reason_code TEXT,
          details_json TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_audit_object
          ON audit_events(object_type, object_id, seq);

        CREATE TABLE IF NOT EXISTS notification_outbox (
          notification_id TEXT PRIMARY KEY,
          conflict_revision TEXT,
          channel TEXT NOT NULL,
          payload_ref TEXT NOT NULL,
          status TEXT NOT NULL,
          attempt INTEGER NOT NULL DEFAULT 0,
          next_retry TEXT,
          remote_id TEXT,
          UNIQUE(conflict_revision, channel, payload_ref)
        );

        CREATE TABLE IF NOT EXISTS runtime_state (
          key TEXT PRIMARY KEY,
          value_json TEXT NOT NULL,
          revision INTEGER NOT NULL,
          updated_at TEXT NOT NULL
        );
        """,
    ),
    (
        2,
        # 0002: denormalise statement into memory_projection so the
        # reconciler can match duplicate/conflicting candidates
        # without re-reading memory files. Idempotent ALTER TABLE.
        """
        ALTER TABLE memory_projection ADD COLUMN statement TEXT NOT NULL DEFAULT '';
        """,
    ),
    (
        3,
        # 0003: phase B. Track bounded backfill runs separately from
        # the normal scan cursor, and anchor source-file revisions so
        # rotation/truncation/replacement can advance ``generation``
        # without losing the old segment evidence. Idempotent ALTER
        # TABLE; CREATE TABLE IF NOT EXISTS for new tables.
        """
        ALTER TABLE source_files ADD COLUMN revision_seq INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE source_files ADD COLUMN last_prefix_hash TEXT;
        ALTER TABLE source_files ADD COLUMN last_tail_hash TEXT;
        """,
    ),
    (
        5,
        # 0005: P0 source-integrity fix. The legacy ``last_prefix_hash``
        # / ``last_tail_hash`` columns only sampled the first and last
        # 64 KiB of the consumed prefix, so a same-size edit in the
        # untouched middle of a >256 KiB JSONL file was invisible to
        # ``_detect_middle_rewrite``. We now persist a single
        # ``consumed_prefix_sha256`` that covers *every* byte up to
        # ``complete_offset``. The 64 KiB-only columns are retained
        # for backward compatibility with already-shipped vaults but
        # are no longer written or read by the scanner.
        """
        ALTER TABLE source_files ADD COLUMN consumed_prefix_sha256 TEXT;
        """,
    ),
    (
        6,
        # 0006: Phase C. Job lifecycle columns + worker leases + source
        # correction queue. The ``jobs`` table in migration 0001 was a
        # skeleton; Phase C extends it with ``revision``, ``started_at``,
        # ``ended_at``, ``progress_json``, ``result_json``, ``error_json``
        # so the worker can publish machine-readable progress and a
        # durable final result, and so the CLI can reject stale
        # ``job.cancel`` / ``job.retry`` calls. ``worker_leases`` is the
        # authoritative lease store (a worker process holds at most one
        # active lease at a time, identified by ``worker_id`` + a fresh
        # ``lease_token``). ``source_correction_proposals`` is the
        # durable queue for memories that cited a now-rewritten source
        # event; ``memory_correction_overlay`` is the read-only
        # projection the recall path joins to flag/omit affected
        # memories. ``worker_runs`` records each worker pass for
        # diagnostics and idempotent restart. ``worker_runtime_state``
        # is the small key/value store the worker uses to anchor
        # incremental work (last schedule run, last source correction
        # pass, etc.). All migrations are idempotent ``ALTER TABLE`` /
        # ``CREATE TABLE IF NOT EXISTS``.
        """
        ALTER TABLE jobs ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE jobs ADD COLUMN started_at TEXT;
        ALTER TABLE jobs ADD COLUMN ended_at TEXT;
        ALTER TABLE jobs ADD COLUMN progress_json TEXT NOT NULL DEFAULT '{}';
        ALTER TABLE jobs ADD COLUMN result_json TEXT;
        ALTER TABLE jobs ADD COLUMN error_json TEXT;
        ALTER TABLE jobs ADD COLUMN mode TEXT NOT NULL DEFAULT 'sync';
        ALTER TABLE jobs ADD COLUMN last_heartbeat TEXT;

        CREATE TABLE IF NOT EXISTS worker_leases (
            worker_id TEXT PRIMARY KEY,
            lease_token TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            last_heartbeat TEXT NOT NULL,
            current_job_id TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_worker_leases_expiry
            ON worker_leases(expires_at);

        CREATE TABLE IF NOT EXISTS worker_runs (
            run_id TEXT PRIMARY KEY,
            worker_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            leases_acquired INTEGER NOT NULL DEFAULT 0,
            leases_released INTEGER NOT NULL DEFAULT 0,
            jobs_started INTEGER NOT NULL DEFAULT 0,
            jobs_completed INTEGER NOT NULL DEFAULT 0,
            jobs_failed INTEGER NOT NULL DEFAULT 0,
            jobs_cancelled INTEGER NOT NULL DEFAULT 0,
            model_calls INTEGER NOT NULL DEFAULT 0,
            diagnostics_json TEXT NOT NULL DEFAULT '{}',
            outcome TEXT NOT NULL DEFAULT 'running'
        );
        CREATE INDEX IF NOT EXISTS ix_worker_runs_started
            ON worker_runs(started_at);

        CREATE TABLE IF NOT EXISTS source_correction_proposals (
            correction_id TEXT PRIMARY KEY,
            source_file_id TEXT NOT NULL,
            old_event_id TEXT NOT NULL,
            new_event_id TEXT,
            memory_id TEXT NOT NULL,
            memory_revision INTEGER NOT NULL,
            old_revision_hash TEXT NOT NULL,
            new_revision_hash TEXT,
            status TEXT NOT NULL,
            reason TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(memory_id, memory_revision, old_event_id, source_file_id)
        );
        CREATE INDEX IF NOT EXISTS ix_source_correction_status
            ON source_correction_proposals(status, detected_at);
        CREATE INDEX IF NOT EXISTS ix_source_correction_memory
            ON source_correction_proposals(memory_id);

        CREATE TABLE IF NOT EXISTS memory_correction_overlay (
            memory_id TEXT NOT NULL,
            memory_revision INTEGER NOT NULL,
            pending_correction_id TEXT,
            state TEXT NOT NULL,
            affected_event_ids_json TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(memory_id, memory_revision)
        );
        CREATE INDEX IF NOT EXISTS ix_memory_correction_overlay_state
            ON memory_correction_overlay(state);

        CREATE TABLE IF NOT EXISTS worker_runtime_state (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """,
    ),
    (
        4,
        # 0004: backfill audit + per-file scope filter for the bounded
        # backfill. ``source_time_iso`` is RFC3339 UTC; the dispatcher
        # rejects ambiguous naive timestamps so backfill ranges are
        # always timezone-explicit.
        """
        CREATE TABLE IF NOT EXISTS backfill_runs (
            backfill_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            connector TEXT NOT NULL,
            roots_json TEXT NOT NULL,
            since_iso TEXT NOT NULL,
            until_iso TEXT NOT NULL,
            timezone TEXT NOT NULL,
            status TEXT NOT NULL,
            counters_json TEXT NOT NULL DEFAULT '{}',
            source_files_seen INTEGER NOT NULL DEFAULT 0,
            events_scanned INTEGER NOT NULL DEFAULT 0,
            target_events INTEGER NOT NULL DEFAULT 0,
            context_only_events INTEGER NOT NULL DEFAULT 0,
            duplicates INTEGER NOT NULL DEFAULT 0,
            superseded_batches INTEGER NOT NULL DEFAULT 0,
            diagnostics_json TEXT NOT NULL DEFAULT '[]'
        );
        CREATE INDEX IF NOT EXISTS ix_backfill_status
          ON backfill_runs(status, started_at);

        CREATE TABLE IF NOT EXISTS backfill_windows (
            backfill_id TEXT NOT NULL REFERENCES backfill_runs(backfill_id),
            source_file_id TEXT NOT NULL REFERENCES source_files(source_file_id),
            window_start INTEGER NOT NULL,
            window_end INTEGER NOT NULL,
            revision_seq INTEGER NOT NULL,
            PRIMARY KEY(backfill_id, source_file_id)
        );

        CREATE TABLE IF NOT EXISTS oversize_units (
            oversize_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            branch_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            estimated_tokens INTEGER NOT NULL,
            reason TEXT NOT NULL,
            state TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_oversize_session
          ON oversize_units(session_id, branch_id, state);

        CREATE TABLE IF NOT EXISTS work_unit_fragments (
            fragment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_event_id TEXT NOT NULL,
            span_start INTEGER NOT NULL,
            span_end INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            branch_id TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            condition_prefix TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_work_unit_fragments_parent
          ON work_unit_fragments(parent_event_id);
        """,
    ),
    (
        7,
        # 0007: phase C repair-2 — source-correction adopt_new / scope_split
        # need optimistic concurrency and atomic new-memory publish.
        #
        # * ``proposal_revision``           : optimistic-concurrency token
        #   bumped on every status change so ``decide_proposal`` and the
        #   CLI ``decide`` handler can reject stale callers.
        # * ``superseded_memory_id``        : the new memory that the user
        #   adopted (``adopt_new`` / ``scope_split``) once ``publish``
        #   succeeded; NULL until a decision is made.
        # * ``superseded_memory_revision``  : that new memory's revision.
        # * ``superseded_head_commit_id``   : the commit_id the new memory
        #   was pinned to (lets audit / recall distinguish "old HEAD
        #   with the new memory" from "HEAD that never had the new
        #   memory").
        # * ``decided_at``                  : ISO timestamp of the user
        #   decision landing; NULL while the proposal is still
        #   ``pending`` / ``auto_resolved``.
        #
        # The ``ALTER TABLE`` is split per column because SQLite raises
        # if the column already exists; the migration is intentionally
        # additive and safe to re-run via ``apply_initial_migration``.
        """
        ALTER TABLE source_correction_proposals
          ADD COLUMN proposal_revision INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE source_correction_proposals
          ADD COLUMN superseded_memory_id TEXT;
        ALTER TABLE source_correction_proposals
          ADD COLUMN superseded_memory_revision INTEGER;
        ALTER TABLE source_correction_proposals
          ADD COLUMN superseded_head_commit_id TEXT;
        ALTER TABLE source_correction_proposals
          ADD COLUMN decided_at TEXT;
        """,
    ),
    (
        8,
        # 0008: P4 / T12 — Telegram decision channel durable state.
        #
        # * ``telegram_updates``            : one row per received Bot API
        #   update. ``update_id`` is the dedupe key, so a replayed or
        #   out-of-order update can never produce a second commit or a
        #   second receipt. ``status`` records the terminal outcome
        #   (decided / rejected / ignored / duplicate).
        # * ``telegram_callback_tokens``    : short-lived opaque callback
        #   tokens bound to (conflict_id, proposal_revision, action,
        #   chat_id, user_id) with an expiry. The token is a random
        #   opaque id — it never carries memory text, and the bot token
        #   is never stored here.
        # * ``notification_outbox`` already exists (migration 1) and is
        #   the durable at-least-once send queue for this channel.
        """
        CREATE TABLE IF NOT EXISTS telegram_updates (
          update_id INTEGER PRIMARY KEY,
          status TEXT NOT NULL,
          chat_id TEXT,
          user_id TEXT,
          conflict_id TEXT,
          action TEXT,
          reason_code TEXT,
          decision_event_ref TEXT,
          received_at TEXT NOT NULL,
          processed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_telegram_updates_status
          ON telegram_updates(status, received_at);

        CREATE TABLE IF NOT EXISTS telegram_callback_tokens (
          token_id TEXT PRIMARY KEY,
          conflict_id TEXT NOT NULL,
          proposal_revision INTEGER NOT NULL,
          action TEXT NOT NULL,
          chat_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          created_at TEXT NOT NULL,
          consumed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_telegram_tokens_conflict
          ON telegram_callback_tokens(conflict_id, proposal_revision);
        """,
    ),
    (
        9,
        # 0009: P9 / T17 — privacy deletion barrier.
        #
        # * ``privacy_deletions``            : durable mirror of every
        #   forget operation. ``op_id`` is the primary key so a retry
        #   of the same forget is idempotent. ``kind`` is one of
        #   ``memory`` / ``event`` / ``source_block``; ``targets_json``
        #   is the JSON array of ids the receipt names. ``version`` is
        #   the monotonic privacy HEAD version this row was published
        #   under; ``privacy_epoch`` is the human-readable epoch label.
        #   ``note`` is an optional short reason the caller supplied.
        #
        # The recall hot filter queries this table on every recall so
        # the ``kind`` + ``version`` columns are indexed; the JSON
        # ``targets_json`` is walked via ``json_each`` in the service.
        # No content / full-text is stored here.
        #
        # * ``privacy_blocks``              : source-level re-ingest
        #   guard. ``source_file_id`` is the key; ``op_id`` references
        #   the receipt that pinned the source. The scanner consults
        #   this table before registering any new event.
        #
        # No ALTER on existing tables; both new tables are pure
        # additions so the migration is safe to re-run via
        # ``apply_initial_migration``.
        """
        CREATE TABLE IF NOT EXISTS privacy_deletions (
            op_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            targets_json TEXT NOT NULL,
            privacy_epoch TEXT NOT NULL,
            version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            actor TEXT NOT NULL,
            note TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_privacy_deletions_kind
            ON privacy_deletions(kind, version);
        CREATE INDEX IF NOT EXISTS ix_privacy_deletions_version
            ON privacy_deletions(version);

        CREATE TABLE IF NOT EXISTS privacy_blocks (
            source_file_id TEXT PRIMARY KEY,
            op_id TEXT NOT NULL REFERENCES privacy_deletions(op_id),
            blocked_at TEXT NOT NULL
        );
        """,
    ),
]


def apply_initial_migration(conn: sqlite3.Connection) -> None:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    )
    if cur.fetchone() is None:
        # First-time setup: just create the table; the rest of MIGRATIONS
        # run inside one transaction below.
        conn.execute(
            "CREATE TABLE schema_migrations (id INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )

    # Apply any missing migrations in order.
    applied_rows = conn.execute("SELECT id FROM schema_migrations").fetchall()
    applied = {row[0] for row in applied_rows}
    pending_migrations = [(mid, sql) for mid, sql in MIGRATIONS if mid not in applied]
    if not pending_migrations:
        return
    for mid, sql in pending_migrations:
        conn.executescript(sql)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(id, applied_at) VALUES (?, ?)",
            (mid, _now()),
        )


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()