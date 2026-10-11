"""Repository adapters on top of the SQLite connection.

Each method is a thin wrapper around parameterised SQL; this is the only
file (besides migrations.py) that writes SQL.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from memory_agent.contracts.events import ConversationEvent


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_in(seconds: int) -> str:
    """Return an ISO 8601 UTC timestamp ``seconds`` from now."""
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


class SourceFileRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get_or_register(
        self,
        *,
        source_file_id: str,
        connector: str,
        session_id: str,
        branch_id: str,
        canonical_path: str,
        file_identity: str,
        adapter_version: str,
        generation: int,
    ) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM source_files WHERE source_file_id = ?",
            (source_file_id,),
        ).fetchone()
        if row is not None:
            return dict(row)
        self.conn.execute(
            """INSERT INTO source_files(
                source_file_id, connector, session_id, branch_id,
                canonical_path, file_identity, adapter_version,
                generation, complete_offset
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (
                source_file_id, connector, session_id, branch_id,
                canonical_path, file_identity, adapter_version, generation,
            ),
        )
        row = self.conn.execute(
            "SELECT * FROM source_files WHERE source_file_id = ?",
            (source_file_id,),
        ).fetchone()
        return dict(row)

    def update_cursor(
        self,
        *,
        source_file_id: str,
        complete_offset: int,
        size: int,
        mtime: str | None,
        last_status: str,
        last_error: str | None = None,
    ) -> None:
        self.conn.execute(
            """UPDATE source_files SET
                complete_offset = ?, size = ?, mtime = ?, last_scan_at = ?,
                last_status = ?, last_error = ?
            WHERE source_file_id = ?""",
            (
                complete_offset, size, mtime, _now(), last_status, last_error,
                source_file_id,
            ),
        )

    def get(self, source_file_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM source_files WHERE source_file_id = ?",
            (source_file_id,),
        ).fetchone()
        return dict(row) if row else None


class SegmentRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def insert(
        self,
        *,
        segment_id: str,
        source_file_id: str,
        generation: int,
        path: str,
        sha256: str,
        start_offset: int,
        end_offset: int,
    ) -> None:
        self.conn.execute(
            """INSERT INTO segments(
                segment_id, source_file_id, generation, path, sha256,
                start_offset, end_offset, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                segment_id, source_file_id, generation, path, sha256,
                start_offset, end_offset, _now(),
            ),
        )


class EventRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def upsert(self, event: ConversationEvent, segment_id: str) -> bool:
        # Idempotent insert: returns False if duplicate.
        try:
            self.conn.execute(
                """INSERT INTO source_events(
                    event_id, canonical_event_key, session_id, branch_id,
                    source_file_id, segment_id, role, kind, revision_hash,
                    source_time, discovered_at, replaces_event_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.event_id,
                    event.canonical_event_key(),
                    event.source.session_id,
                    event.source.branch_id,
                    event.source.source_file_id,
                    segment_id,
                    event.role,
                    event.kind,
                    event.revision_hash,
                    event.source.source_time,
                    event.source.discovered_at,
                    event.replaces_event_id,
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False


class TurnRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def upsert(self, turn: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO turns(
                turn_id, session_id, branch_id, ordinal, revision,
                state, first_event_id, last_event_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                turn["turn_id"], turn["session_id"], turn["branch_id"],
                turn["ordinal"], turn.get("revision", 1), turn["state"],
                turn["first_event_id"], turn["last_event_id"],
            ),
        )

    def list_open(self, session_id: str, branch_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM turns WHERE session_id = ? AND branch_id = ? AND state IN ('open','incomplete') ORDER BY ordinal",
            (session_id, branch_id),
        ).fetchall()
        return [dict(r) for r in rows]


class BatchRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(self, batch: dict[str, Any], members: list[dict[str, Any]]) -> None:
        # If a batch with the same id already exists, skip (idempotent re-plan).
        existing = self.conn.execute(
            "SELECT 1 FROM batches WHERE batch_id = ?",
            (batch["batch_id"],),
        ).fetchone()
        if existing is not None:
            return
        self.conn.execute(
            """INSERT INTO batches(
                batch_id, session_id, branch_id, policy_version,
                input_hash, status, attempt, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
            (
                batch["batch_id"], batch["session_id"], batch["branch_id"],
                batch["policy_version"], batch["input_hash"], batch.get("status", "ready"),
                _now(),
            ),
        )
        for m in members:
            self.conn.execute(
                """INSERT INTO batch_members(
                    batch_id, unit_id, purpose, ord, epoch, active_target
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    batch["batch_id"], m["unit_id"], m["purpose"],
                    m["order"], m.get("epoch", 1), int(m.get("active_target", 0)),
                ),
            )

    def claim(self, batch_id: str, lease_token: str, lease_until: str) -> bool:
        cur = self.conn.execute(
            "UPDATE batches SET status='leased', lease_token=?, lease_until=? "
            "WHERE batch_id=? AND (status IN ('ready','retry_wait') OR lease_until < ?)",
            (lease_token, lease_until, batch_id, _now()),
        )
        return cur.rowcount == 1

    def finalize(self, batch_id: str, new_status: str, lease_token: str) -> bool:
        cur = self.conn.execute(
            "UPDATE batches SET status=?, lease_token=NULL, lease_until=NULL "
            "WHERE batch_id=? AND lease_token=?",
            (new_status, batch_id, lease_token),
        )
        return cur.rowcount == 1

    def supersede_active_targets(self, unit_ids: list[str], epoch: int) -> int:
        """Release any active_target rows for these units at this epoch."""
        if not unit_ids:
            return 0
        placeholders = ",".join("?" for _ in unit_ids)
        cur = self.conn.execute(
            f"DELETE FROM batch_members WHERE unit_id IN ({placeholders}) AND epoch = ?",
            (*unit_ids, epoch),
        )
        return cur.rowcount

    def get(self, batch_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if row is None:
            return None
        members = self.conn.execute(
            "SELECT * FROM batch_members WHERE batch_id = ? ORDER BY ord",
            (batch_id,),
        ).fetchall()
        out = dict(row)
        out["members"] = [dict(m) for m in members]
        return out

    def list_pending(self, limit: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM batches WHERE status IN ('ready','retry_wait') "
            "ORDER BY created_at LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


class CandidateSetRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def insert(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO candidate_sets(
                candidate_set_id, batch_id, artifact_ref, sha256,
                prompt_version, model, validation_state, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["candidate_set_id"], row["batch_id"], row["artifact_ref"],
                row["sha256"], row["prompt_version"], row["model"],
                row["validation_state"], _now(),
            ),
        )


class CommitIntentRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def insert(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO commit_intents(
                commit_id, parent_id, manifest_ref, batch_ids_json,
                target_unit_ids_json, expected_revisions_json, privacy_epoch,
                phase, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["commit_id"], row.get("parent_id"), row["manifest_ref"],
                json.dumps(row["batch_ids"]),
                json.dumps(row["target_unit_ids"]),
                json.dumps(row["expected_revisions"]),
                row["privacy_epoch"], row["phase"], _now(),
            ),
        )

    def update_phase(self, commit_id: str, phase: str) -> None:
        self.conn.execute(
            "UPDATE commit_intents SET phase = ? WHERE commit_id = ?",
            (phase, commit_id),
        )

    def get(self, commit_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM commit_intents WHERE commit_id = ?", (commit_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_by_phase(self, phase: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM commit_intents WHERE phase = ?", (phase,)
        ).fetchall()
        return [dict(r) for r in rows]


class MemoryProjectionRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def upsert(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO memory_projection(
                memory_id, revision, status, scope_json,
                project_ids_json, domains_json, commit_id, recorded_at, statement
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["memory_id"], row["revision"], row["status"],
                json.dumps(row.get("scope", {})),
                json.dumps(row.get("project_ids", [])),
                json.dumps(row.get("domains", [])),
                row["commit_id"], _now(),
                row.get("statement", ""),
            ),
        )

    def list_active(self, project_ids: list[str] | None = None) -> list[dict[str, Any]]:
        if project_ids is None:
            rows = self.conn.execute(
                "SELECT * FROM memory_projection WHERE status = 'active' ORDER BY recorded_at"
            ).fetchall()
            return [dict(r) for r in rows]
        # Project-id filtering: scope_json contains the list; do simple
        # LIKE in JSON. v0.1 is intentionally coarse.
        like_clauses = [f"project_ids_json LIKE ?"] * len(project_ids)
        params: list[Any] = [f'%"{p}"%' for p in project_ids]
        rows = self.conn.execute(
            f"SELECT * FROM memory_projection WHERE status = 'active' AND "
            f"({' OR '.join(like_clauses)}) ORDER BY recorded_at",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def list_archived(self) -> list[dict[str, Any]]:
        """Return every archived memory row.

        Phase C repair-2: ``include_archived=True`` on a recall
        request must surface rows whose ``status='archived'``; v0.1
        silently dropped them. The default (``False``) still hides
        archived rows, matching the v1 baseline.
        """
        rows = self.conn.execute(
            "SELECT * FROM memory_projection WHERE status = 'archived' "
            "ORDER BY recorded_at"
        ).fetchall()
        return [dict(r) for r in rows]


class ConflictRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def open(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO conflict_projection(
                conflict_id, revision, status, old_memory_ref,
                new_memory_ref, commit_id, opened_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                row["conflict_id"], row["revision"], row["status"],
                row.get("old_memory_ref"), row.get("new_memory_ref"),
                row["commit_id"], _now(),
            ),
        )

    def list_open(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM conflict_projection WHERE status IN ('open','pending') ORDER BY opened_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self, conflict_id: str, revision: int, new_status: str) -> None:
        self.conn.execute(
            "UPDATE conflict_projection SET status = ? WHERE conflict_id = ? AND revision = ?",
            (new_status, conflict_id, revision),
        )


class AuditRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def append(self, event: dict[str, Any]) -> None:
        # Compute next seq atomically (single-row UPDATE).
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM audit_events"
        ).fetchone()
        next_seq = int(row["next"])
        self.conn.execute(
            """INSERT INTO audit_events(
                audit_id, seq, occurred_at, actor, action,
                object_type, object_id, correlation_json,
                before_after_json, outcome, reason_code, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event["audit_id"], next_seq, event["occurred_at"],
                event["actor"], event["action"],
                event.get("object_type"), event.get("object_id"),
                json.dumps(event.get("correlation", {})),
                json.dumps(event.get("before_after", {})),
                event["outcome"], event.get("reason_code"),
                json.dumps(event.get("details", {})),
            ),
        )

    def list_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM audit_events ORDER BY seq DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


class JobRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get_by_idempotency(self, client_id: str, key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE client_id = ? AND idempotency_key = ?",
            (client_id, key),
        ).fetchone()
        return dict(row) if row else None

    def get(self, job_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        return dict(row) if row else None

    def insert(self, job: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO jobs(
                job_id, request_id, client_id, idempotency_key, request_hash,
                operation, params_ref, state, mode, cancel_requested, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (
                job["job_id"], job["request_id"], job["client_id"],
                job.get("idempotency_key"), job["request_hash"],
                job["operation"], job.get("params_ref"),
                job.get("state", "queued"),
                job.get("mode", "sync"),
                _now(),
            ),
        )

    def update_state(self, job_id: str, state: str, *, expected_revision: int | None = None) -> bool:
        """Update state; if ``expected_revision`` is given, only update
        when the current row's ``revision`` matches. Returns True on success.
        """
        if expected_revision is None:
            cur = self.conn.execute(
                "UPDATE jobs SET state = ? WHERE job_id = ?",
                (state, job_id),
            )
            return cur.rowcount >= 0  # rowcount 0 is also fine (no-op)
        cur = self.conn.execute(
            "UPDATE jobs SET state = ?, revision = revision + 1 "
            "WHERE job_id = ? AND revision = ?",
            (state, job_id, expected_revision),
        )
        return cur.rowcount == 1

    def mark_started(self, job_id: str) -> None:
        self.conn.execute(
            "UPDATE jobs SET state = 'running', started_at = ?, "
            "last_heartbeat = ?, revision = revision + 1 "
            "WHERE job_id = ?",
            (_now(), _now(), job_id),
        )

    def heartbeat(self, job_id: str) -> None:
        self.conn.execute(
            "UPDATE jobs SET last_heartbeat = ? WHERE job_id = ?",
            (_now(), job_id),
        )

    def set_progress(self, job_id: str, progress: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE jobs SET progress_json = ?, revision = revision + 1, "
            "last_heartbeat = ? WHERE job_id = ?",
            (json.dumps(progress), _now(), job_id),
        )

    def set_result(self, job_id: str, result: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE jobs SET result_json = ?, ended_at = ?, state = 'succeeded', "
            "revision = revision + 1 WHERE job_id = ?",
            (json.dumps(result), _now(), job_id),
        )

    def set_error(self, job_id: str, error: dict[str, Any], *,
                  final_state: str = "failed") -> None:
        self.conn.execute(
            "UPDATE jobs SET error_json = ?, ended_at = ?, state = ?, "
            "revision = revision + 1 WHERE job_id = ?",
            (json.dumps(error), _now(), final_state, job_id),
        )

    def request_cancel(self, job_id: str, expected_revision: int) -> bool:
        cur = self.conn.execute(
            "UPDATE jobs SET cancel_requested = 1, revision = revision + 1 "
            "WHERE job_id = ? AND revision = ?",
            (job_id, expected_revision),
        )
        return cur.rowcount == 1

    def is_cancel_requested(self, job_id: str) -> bool:
        row = self.conn.execute(
            "SELECT cancel_requested FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        return bool(row and int(row["cancel_requested"] or 0) == 1)

    def count_by_state(self, state: str) -> int:
        """Return the number of jobs currently in ``state``.

        Cheap integer count over the ``jobs`` table — used by the
        ``/healthz?verbose=true`` endpoint to surface a queued-jobs
        count without pulling every row. Treats ``None`` / empty state
        as ``0``.
        """
        if not state:
            return 0
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE state = ?",
            (state,),
        ).fetchone()
        return int(row["c"] or 0) if row else 0

    def next_queued(self) -> dict[str, Any] | None:
        """Return the oldest queued async job or None.

        Two flavours of queued work are picked up here:
          * ``state = 'queued' AND cancel_requested = 0`` — ready to run.
          * ``state = 'queued' AND cancel_requested = 1`` — already
            cancelled; we flip it to ``cancelled`` directly without
            dispatching.

        The atomic claim marks ``state='running'`` only when the job is
        still in ``queued`` and the cancel flag matches what the worker
        observed. The caller is responsible for re-queueing or
        finalising.
        """
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE state = 'queued' "
            "ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        if int(out.get("cancel_requested") or 0) == 1:
            # Cancellation observed while still queued. Finalise now.
            self.conn.execute(
                "UPDATE jobs SET state='cancelled', ended_at = ?, "
                "error_json = ?, revision = revision + 1 "
                "WHERE job_id = ? AND state = 'queued'",
                (
                    _now(),
                    json.dumps({"code": "BUSY", "message": "cancelled before run"}),
                    out["job_id"],
                ),
            )
            return None
        cur = self.conn.execute(
            "UPDATE jobs SET state = 'running', started_at = ?, "
            "last_heartbeat = ?, revision = revision + 1 "
            "WHERE job_id = ? AND state = 'queued' AND cancel_requested = 0",
            (_now(), _now(), out["job_id"]),
        )
        if cur.rowcount != 1:
            return None
        # Re-fetch to capture the new revision + started_at
        fresh = self.conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (out["job_id"],)
        ).fetchone()
        return dict(fresh) if fresh else None

    def reclaim_stale_running(
        self, *, older_than_iso: str | None = None,
    ) -> list[str]:
        """Reset ``running`` jobs whose heartbeat has expired back to
        ``queued`` so a new worker pass can pick them up.

        Phase C repair-1: a worker that claimed a job and then died
        (process crash, OOM kill, etc.) leaves the job in
        ``state='running'`` with no live lease to finalise it. Without
        this reclaim the job stays stuck forever. The default
        threshold is the worker's lease window — any ``running`` job
        whose ``last_heartbeat`` is older than that window has no live
        owner and is fair game for the next worker.

        The reclaim is *atomic per row*: each row's
        ``state='running' AND last_heartbeat < threshold`` CAS prevents
        two concurrent reclaims from racing on the same job. Returns
        the list of reclaimed ``job_id``s so the caller can audit
        them in the worker_runs diagnostics.
        """
        threshold = older_than_iso or _iso_in(-30)  # anything > 30s stale
        rows = self.conn.execute(
            "SELECT job_id FROM jobs WHERE state = 'running' "
            "AND last_heartbeat IS NOT NULL AND last_heartbeat < ?",
            (threshold,),
        ).fetchall()
        reclaimed: list[str] = []
        for row in rows:
            jid = str(row["job_id"])
            cur = self.conn.execute(
                "UPDATE jobs SET state = 'queued', started_at = NULL, "
                "revision = revision + 1 "
                "WHERE job_id = ? AND state = 'running' "
                "AND last_heartbeat < ?",
                (jid, threshold),
            )
            if cur.rowcount == 1:
                reclaimed.append(jid)
        return reclaimed

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


class WorkerLeaseRepo:
    """Project-owned worker lease store.

    A worker process holds at most one active lease at a time; the
    lease is identified by ``(worker_id, lease_token)``. Acquired leases
    expire automatically; the worker must heartbeat before expiry or
    drop the lease. A new process can reclaim an expired lease to
    avoid duplicate publication after restart.

    Phase C repair-1: ``acquire`` is the **vault-level writer lock**.
    A second ``acquire`` from a *different* ``worker_id`` is refused
    while any other worker's lease is still active, so two worker
    processes cannot both run a pass at the same time against the
    same vault. The previous per-worker keying allowed multiple
    workers to coexist and stomp on each other's writes
    (``audit_events`` ``MAX(seq)+1``, ``source_correction_proposals``
    upsert, ``memory_projection`` finalise, ``worker_runtime_state``
    keys, ``files/`` HEAD swap). With the new behaviour the lock is
    strictly one-at-a-time; expired leases are reclaimable by a
    different worker, just like before.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def acquire(self, *, worker_id: str, lease_token: str,
                ttl_seconds: int, current_job_id: str | None) -> bool:
        """Try to acquire the vault-level writer lease.

        * If an **active** lease exists for a *different* worker,
          refuse (``False``). This is the writer lock — only one
          worker process may run a pass against this vault at a time.
        * If an **active** lease exists for the *same* worker, only
          reclaim it once it has expired.
        * If no active lease exists, attempt the insert. A concurrent
          insert by another process is detected via the UNIQUE
          constraint on ``worker_id`` and reported as ``False``.
        """
        now = _now()
        active = self.conn.execute(
            "SELECT worker_id, lease_token, expires_at FROM worker_leases "
            "WHERE expires_at >= ? LIMIT 1",
            (now,),
        ).fetchone()
        if active is not None:
            if str(active["worker_id"]) != worker_id:
                # A different worker holds the active writer lease —
                # refuse to run concurrently.
                return False
            # Our own lease is still active. Only reclaim if expired.
            cur = self.conn.execute(
                """UPDATE worker_leases SET
                    lease_token = ?, acquired_at = ?, expires_at = ?,
                    last_heartbeat = ?, current_job_id = ?
                WHERE worker_id = ? AND expires_at < ?""",
                (
                    lease_token, _now(), _iso_in(ttl_seconds),
                    _now(), current_job_id,
                    worker_id, _now(),
                ),
            )
            return cur.rowcount == 1
        # No active lease: try to insert; a parallel writer will trip
        # the UNIQUE constraint on ``worker_id`` and we report failure.
        try:
            cur = self.conn.execute(
                """INSERT INTO worker_leases(
                    worker_id, lease_token, acquired_at, expires_at,
                    last_heartbeat, current_job_id
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    worker_id, lease_token, _now(),
                    _iso_in(ttl_seconds), _now(),
                    current_job_id,
                ),
            )
            return cur.rowcount == 1
        except sqlite3.IntegrityError:
            return False

    def heartbeat(self, *, worker_id: str, lease_token: str,
                  ttl_seconds: int) -> bool:
        cur = self.conn.execute(
            """UPDATE worker_leases SET
                last_heartbeat = ?, expires_at = ?
            WHERE worker_id = ? AND lease_token = ?""",
            (_now(), _iso_in(ttl_seconds), worker_id, lease_token),
        )
        return cur.rowcount == 1

    def release(self, *, worker_id: str, lease_token: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM worker_leases "
            "WHERE worker_id = ? AND lease_token = ?",
            (worker_id, lease_token),
        )
        return cur.rowcount == 1

    def is_valid_lease(self, *, worker_id: str, lease_token: str) -> bool:
        row = self.conn.execute(
            "SELECT expires_at FROM worker_leases "
            "WHERE worker_id = ? AND lease_token = ?",
            (worker_id, lease_token),
        ).fetchone()
        if row is None:
            return False
        return _iso_in(-1) < str(row["expires_at"])

    def expire_stale(self) -> int:
        """Drop leases whose ``expires_at`` is in the past. Returns count."""
        cur = self.conn.execute(
            "DELETE FROM worker_leases WHERE expires_at < ?", (_now(),)
        )
        return cur.rowcount

    def list_active(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM worker_leases WHERE expires_at >= ?",
            (_now(),),
        ).fetchall()
        return [dict(r) for r in rows]

    def get(self, worker_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM worker_leases WHERE worker_id = ?",
            (worker_id,),
        ).fetchone()
        return dict(row) if row else None


class WorkerRunRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def insert(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO worker_runs(
                run_id, worker_id, started_at, leases_acquired, leases_released,
                jobs_started, jobs_completed, jobs_failed, jobs_cancelled,
                model_calls, diagnostics_json, outcome
            ) VALUES (?, ?, ?, 0, 0, 0, 0, 0, 0, 0, ?, 'running')""",
            (row["run_id"], row["worker_id"], row["started_at"],
             json.dumps(row.get("diagnostics", {}))),
        )

    def increment(self, run_id: str, field: str, delta: int = 1) -> None:
        if field not in {
            "leases_acquired", "leases_released", "jobs_started",
            "jobs_completed", "jobs_failed", "jobs_cancelled",
            "model_calls",
        }:
            raise ValueError(f"unknown field {field!r}")
        self.conn.execute(
            f"UPDATE worker_runs SET {field} = {field} + ? WHERE run_id = ?",
            (delta, run_id),
        )

    def finalize(self, run_id: str, *, outcome: str,
                 diagnostics: dict[str, Any] | None = None) -> None:
        self.conn.execute(
            "UPDATE worker_runs SET ended_at = ?, outcome = ?, "
            "diagnostics_json = ? WHERE run_id = ?",
            (_now(), outcome,
             json.dumps(diagnostics or {}), run_id),
        )

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM worker_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


class WorkerRuntimeStateRepo:
    """Tiny key/value store for the worker's incremental bookkeeping."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM worker_runtime_state WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["value_json"])

    def upsert(self, key: str, value: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO worker_runtime_state(key, value_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at""",
            (key, json.dumps(value), _now()),
        )


class SourceCorrectionProposalRepo:
    """Durable queue of memories affected by a rewritten source event.

    ``status`` lifecycle:
      * ``pending``        — discovered; recall marks/omits the memory
      * ``decided_keep``   — user decided to keep the old memory
      * ``decided_re_link``— user adopted the rewritten text; a new
        memory has been published and the old one is superseded
      * ``decided_obsolete`` — the old memory is now retired
      * ``auto_resolved``  — the rewrite did not change the relevant
        text and no user action is required (only set by a deterministic
        ``scan`` / ``reconcile`` step; the worker never auto-relinks)
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert(self, row: dict[str, Any]) -> bool:
        """Insert or refresh a pending correction. Returns True on insert."""
        cur = self.conn.execute(
            """INSERT INTO source_correction_proposals(
                correction_id, source_file_id, old_event_id, new_event_id,
                memory_id, memory_revision, old_revision_hash, new_revision_hash,
                status, reason, detected_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_id, memory_revision, old_event_id, source_file_id)
            DO UPDATE SET
                new_event_id = COALESCE(excluded.new_event_id, source_correction_proposals.new_event_id),
                new_revision_hash = COALESCE(excluded.new_revision_hash, source_correction_proposals.new_revision_hash),
                reason = excluded.reason,
                status = CASE
                    WHEN source_correction_proposals.status IN (
                        'pending', 'auto_resolved'
                    ) THEN excluded.status
                    ELSE source_correction_proposals.status
                END,
                updated_at = excluded.updated_at""",
            (
                row["correction_id"], row["source_file_id"],
                row["old_event_id"], row.get("new_event_id"),
                row["memory_id"], row["memory_revision"],
                row["old_revision_hash"], row.get("new_revision_hash"),
                row.get("status", "pending"),
                row.get("reason", "source_rewritten"),
                row.get("detected_at", _now()),
                _now(),
            ),
        )
        # rowcount: 1 = inserted, 2 = updated by ON CONFLICT, 0 = no-op.
        # We return True only when a fresh row was created so the caller
        # can decide whether to bump the overlay. "No-op" means an
        # already-decided correction was preserved (correct behaviour).
        return cur.rowcount >= 1

    def update_status(self, correction_id: str, status: str,
                      *, revision_note: str | None = None) -> None:
        details_extra = json.dumps({"note": revision_note}) if revision_note else "{}"
        # ``proposal_revision`` is an optimistic-concurrency token. Bumping
        # it on every status change lets ``decide_proposal`` / the CLI
        # ``decide`` handler reject stale callers without a separate
        # version table.
        self.conn.execute(
            "UPDATE source_correction_proposals "
            "SET status = ?, updated_at = ?, proposal_revision = proposal_revision + 1, "
            "    reason = CASE WHEN ? != '' THEN ? ELSE reason END "
            "WHERE correction_id = ?",
            (status, _now(), details_extra, details_extra, correction_id),
        )

    def update_decision(self, *, correction_id: str, status: str,
                        superseded_memory_id: str | None,
                        superseded_memory_revision: int | None,
                        superseded_head_commit_id: str | None,
                        decision_note: str | None = None) -> bool:
        """Apply the post-publish state for an ``adopt_new`` / ``scope_split``.

        Bumps ``proposal_revision`` and sets ``decided_at`` so concurrent
        callers (e.g. a second ``decide`` arriving after a successful
        first one) are rejected. Returns ``True`` if the update actually
        flipped the row; ``False`` if the row was already at the new
        status (or gone), in which case the caller must treat the
        decision as a no-op rather than re-publish.
        """
        cur = self.conn.execute(
            "UPDATE source_correction_proposals "
            "SET status = ?, "
            "    proposal_revision = proposal_revision + 1, "
            "    decided_at = ?, "
            "    superseded_memory_id = COALESCE(?, superseded_memory_id), "
            "    superseded_memory_revision = COALESCE(?, superseded_memory_revision), "
            "    superseded_head_commit_id = COALESCE(?, superseded_head_commit_id), "
            "    reason = CASE WHEN ? != '' THEN ? ELSE reason END, "
            "    updated_at = ? "
            "WHERE correction_id = ? "
            "  AND status IN ('pending', 'auto_resolved')",
            (
                status,
                _now(),
                superseded_memory_id,
                superseded_memory_revision,
                superseded_head_commit_id,
                json.dumps({"note": decision_note}) if decision_note else "",
                json.dumps({"note": decision_note}) if decision_note else "",
                _now(),
                correction_id,
            ),
        )
        return cur.rowcount >= 1

    def list_pending(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM source_correction_proposals "
            "WHERE status IN ('pending', 'auto_resolved') "
            "ORDER BY detected_at LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_for_memory(self, memory_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM source_correction_proposals WHERE memory_id = ? "
            "ORDER BY detected_at",
            (memory_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get(self, correction_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM source_correction_proposals WHERE correction_id = ?",
            (correction_id,),
        ).fetchone()
        return dict(row) if row else None

    def count_pending(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM source_correction_proposals "
            "WHERE status IN ('pending', 'auto_resolved')"
        ).fetchone()
        return int(row["c"]) if row else 0


class MemoryCorrectionOverlayRepo:
    """Read-only projection consumed by the recall path. Each row
    carries the list of ``old_event_id``s that triggered a correction
    and the active overlay state. Recall joins against this when
    computing per-item warning flags."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO memory_correction_overlay(
                memory_id, memory_revision, pending_correction_id, state,
                affected_event_ids_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_id, memory_revision) DO UPDATE SET
                pending_correction_id = excluded.pending_correction_id,
                state = excluded.state,
                affected_event_ids_json = excluded.affected_event_ids_json,
                updated_at = excluded.updated_at""",
            (
                row["memory_id"], row["memory_revision"],
                row.get("pending_correction_id"),
                row.get("state", "ok"),
                json.dumps(row.get("affected_event_ids", [])),
                _now(),
            ),
        )

    def list_with_pending(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM memory_correction_overlay "
            "WHERE state != 'ok' ORDER BY updated_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def get(self, memory_id: str, revision: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM memory_correction_overlay "
            "WHERE memory_id = ? AND memory_revision = ?",
            (memory_id, revision),
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["affected_event_ids"] = json.loads(
            out.pop("affected_event_ids_json") or "[]"
        )
        return out

    def clear(self, memory_id: str, revision: int) -> None:
        self.conn.execute(
            "DELETE FROM memory_correction_overlay "
            "WHERE memory_id = ? AND memory_revision = ?",
            (memory_id, revision),
        )


class BackfillRunRepo:
    """Audit + coverage record for bounded backfill runs.

    Phase B keeps backfill ranges separate from the normal scan cursor
    so re-running the same range is idempotent and never advances the
    live ``complete_offset`` for an incremental scan.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def insert(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO backfill_runs(
                backfill_id, started_at, connector, roots_json,
                since_iso, until_iso, timezone, status, counters_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["backfill_id"], row["started_at"], row["connector"],
                json.dumps(row.get("roots", {})),
                row["since_iso"], row["until_iso"], row["timezone"],
                row.get("status", "running"),
                json.dumps(row.get("counters", {})),
            ),
        )

    def finalize(
        self, backfill_id: str, *,
        status: str,
        counters: dict[str, Any],
        diagnostics: list[dict[str, Any]],
        source_files_seen: int,
        events_scanned: int,
        target_events: int,
        context_only_events: int,
        duplicates: int,
        superseded_batches: int,
    ) -> None:
        self.conn.execute(
            """UPDATE backfill_runs SET
                finished_at = ?, status = ?, counters_json = ?,
                diagnostics_json = ?, source_files_seen = ?,
                events_scanned = ?, target_events = ?, context_only_events = ?,
                duplicates = ?, superseded_batches = ?
            WHERE backfill_id = ?""",
            (
                _now(), status, json.dumps(counters),
                json.dumps(diagnostics), source_files_seen,
                events_scanned, target_events, context_only_events,
                duplicates, superseded_batches, backfill_id,
            ),
        )

    def record_window(
        self, *, backfill_id: str, source_file_id: str,
        window_start: int, window_end: int, revision_seq: int,
    ) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO backfill_windows(
                backfill_id, source_file_id, window_start, window_end, revision_seq
            ) VALUES (?, ?, ?, ?, ?)""",
            (backfill_id, source_file_id, window_start, window_end, revision_seq),
        )

    def window_for(self, source_file_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT * FROM backfill_windows
               WHERE source_file_id = ?
               ORDER BY window_end DESC LIMIT 1""",
            (source_file_id,),
        ).fetchone()
        return dict(row) if row else None

    def has_run(self, *, connector: str, since_iso: str, until_iso: str) -> bool:
        row = self.conn.execute(
            """SELECT 1 FROM backfill_runs
               WHERE connector = ? AND since_iso = ? AND until_iso = ?
                 AND status IN ('success', 'noop') LIMIT 1""",
            (connector, since_iso, until_iso),
        ).fetchone()
        return row is not None

    def get(self, backfill_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM backfill_runs WHERE backfill_id = ?",
            (backfill_id,),
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["roots"] = json.loads(out.pop("roots_json") or "{}")
        out["counters"] = json.loads(out.pop("counters_json") or "{}")
        out["diagnostics"] = json.loads(out.pop("diagnostics_json") or "[]")
        return out

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM backfill_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["roots"] = json.loads(d.pop("roots_json") or "{}")
            d["counters"] = json.loads(d.pop("counters_json") or "{}")
            d["diagnostics"] = json.loads(d.pop("diagnostics_json") or "[]")
            out.append(d)
        return out


class OversizeUnitRepo:
    """Durable needs_context records for unfragmentable oversize turns.

    Phase B persists these up-front in ``plan_batches_for_session`` so
    they survive replanning and surface in ``memory doctor``.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO oversize_units(
                oversize_id, session_id, branch_id, event_id,
                estimated_tokens, reason, state, policy_version,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(oversize_id) DO UPDATE SET
                state = excluded.state,
                estimated_tokens = excluded.estimated_tokens,
                policy_version = excluded.policy_version,
                updated_at = excluded.updated_at""",
            (
                row["oversize_id"], row["session_id"], row["branch_id"],
                row["event_id"], row["estimated_tokens"], row["reason"],
                row["state"], row["policy_version"],
                _now(), _now(),
            ),
        )

    def list_open(
        self, session_id: str | None = None, branch_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if session_id is not None and branch_id is not None:
            rows = self.conn.execute(
                "SELECT * FROM oversize_units WHERE session_id = ? "
                "AND branch_id = ? AND state IN ('needs_context') "
                "ORDER BY created_at",
                (session_id, branch_id),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM oversize_units WHERE state IN ('needs_context') "
                "ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]


class WorkUnitFragmentRepo:
    """Audit / coverage record for split fragments."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def record(self, row: dict[str, Any]) -> int:
        cur = self.conn.execute(
            """INSERT INTO work_unit_fragments(
                parent_event_id, span_start, span_end, session_id,
                branch_id, policy_version, condition_prefix, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["parent_event_id"], row["span_start"], row["span_end"],
                row["session_id"], row["branch_id"], row["policy_version"],
                row.get("condition_prefix"), _now(),
            ),
        )
        return int(cur.lastrowid or 0)

    def fragments_for(self, parent_event_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM work_unit_fragments WHERE parent_event_id = ? "
            "ORDER BY span_start",
            (parent_event_id,),
        ).fetchall()
        return [dict(r) for r in rows]


class RuntimeStateRepo:
    """Generic key/value runtime state (Telegram cursor, worker heartbeats).

    Per ``docs/12`` this table holds *small* scheduling / cursor state
    only — never memory text and never secrets such as the bot token.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, key: str) -> Any | None:
        row = self.conn.execute(
            "SELECT value_json FROM runtime_state WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["value_json"])
        except json.JSONDecodeError:
            return None

    def put(self, key: str, value: Any) -> None:
        """Upsert a runtime-state key, bumping the revision counter."""
        row = self.conn.execute(
            "SELECT revision FROM runtime_state WHERE key = ?", (key,)
        ).fetchone()
        revision = (int(row["revision"]) + 1) if row is not None else 1
        self.conn.execute(
            """INSERT INTO runtime_state(key, value_json, revision, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                revision = excluded.revision,
                updated_at = excluded.updated_at""",
            (key, json.dumps(value), revision, _now()),
        )


class NotificationOutboxRepo:
    """Durable at-least-once send queue for channel notifications.

    ``notification_id`` is deterministically derived from
    (conflict_id, proposal_revision, channel, notification_kind) so a
    replayed enqueue never creates a second row, and a send that is
    retried after a crash keeps the same remote-observable identity.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def enqueue(
        self,
        *,
        notification_id: str,
        conflict_revision: str | None,
        channel: str,
        payload_ref: str,
        next_retry: str | None = None,
    ) -> bool:
        """Insert a pending row. Returns True when a new row was created."""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO notification_outbox(
                notification_id, conflict_revision, channel, payload_ref,
                status, attempt, next_retry, remote_id
            ) VALUES (?, ?, ?, ?, 'pending', 0, ?, NULL)""",
            (notification_id, conflict_revision, channel, payload_ref,
             next_retry or _now()),
        )
        return bool(cur.rowcount)

    def due(self, *, channel: str, now_iso: str | None = None, limit: int = 20
            ) -> list[dict[str, Any]]:
        now_iso = now_iso or _now()
        rows = self.conn.execute(
            """SELECT * FROM notification_outbox
            WHERE channel = ? AND status = 'pending'
              AND (next_retry IS NULL OR next_retry <= ?)
            ORDER BY next_retry, notification_id
            LIMIT ?""",
            (channel, now_iso, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_sent(self, notification_id: str, remote_id: str) -> None:
        self.conn.execute(
            "UPDATE notification_outbox SET status = 'sent', remote_id = ? "
            "WHERE notification_id = ?",
            (remote_id, notification_id),
        )

    def mark_retry(self, notification_id: str, *, attempt: int,
                   next_retry: str) -> None:
        self.conn.execute(
            "UPDATE notification_outbox SET status = 'pending', attempt = ?, "
            "next_retry = ? WHERE notification_id = ?",
            (attempt, next_retry, notification_id),
        )

    def get(self, notification_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM notification_outbox WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()
        return dict(row) if row is not None else None


class TelegramUpdateRepo:
    """Durable dedupe / receipt table for received Telegram updates."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def exists(self, update_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM telegram_updates WHERE update_id = ?", (update_id,)
        ).fetchone()
        return row is not None

    def record(
        self,
        *,
        update_id: int,
        status: str,
        received_at: str,
        chat_id: str | None = None,
        user_id: str | None = None,
        conflict_id: str | None = None,
        action: str | None = None,
        reason_code: str | None = None,
        decision_event_ref: str | None = None,
        processed_at: str | None = None,
    ) -> None:
        """Insert a receipt row. Ignores duplicates (returns without write)."""
        self.conn.execute(
            """INSERT OR IGNORE INTO telegram_updates(
                update_id, status, chat_id, user_id, conflict_id, action,
                reason_code, decision_event_ref, received_at, processed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (update_id, status, chat_id, user_id, conflict_id, action,
             reason_code, decision_event_ref, received_at, processed_at),
        )

    def get(self, update_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM telegram_updates WHERE update_id = ?", (update_id,)
        ).fetchone()
        return dict(row) if row is not None else None


class TelegramCallbackTokenRepo:
    """Short-lived opaque callback tokens bound to proposal + revision + action."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(
        self,
        *,
        token_id: str,
        conflict_id: str,
        proposal_revision: int,
        action: str,
        chat_id: str,
        user_id: str,
        expires_at: str,
    ) -> None:
        self.conn.execute(
            """INSERT INTO telegram_callback_tokens(
                token_id, conflict_id, proposal_revision, action,
                chat_id, user_id, expires_at, created_at, consumed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
            (token_id, conflict_id, proposal_revision, action,
             chat_id, user_id, expires_at, _now()),
        )

    def get(self, token_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM telegram_callback_tokens WHERE token_id = ?",
            (token_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def mark_consumed(self, token_id: str, when: str | None = None) -> None:
        self.conn.execute(
            "UPDATE telegram_callback_tokens SET consumed_at = ? "
            "WHERE token_id = ? AND consumed_at IS NULL",
            (when or _now(), token_id),
        )


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()