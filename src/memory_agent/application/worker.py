"""Project-owned worker (Phase C).

The worker owns the timing, retries, lease, heartbeat, schedule and
backpressure for the async job queue. The OS service manager only
starts/restarts the process — it does **not** schedule the loop.

Two entry points:

  * ``run_worker_once(vault_root, ...)`` — process at most one pass
    (no long-running loop). Used by tests and ``memory worker --once``.
  * ``run_worker(vault_root, ...)`` — the long-running loop. Honours a
    simple configurable schedule (``every_minutes`` /
    ``daily_at_hour`` / ``weekly_day`` / ``monthly_day``) and runs a
    source-correction discovery pass + the queued-job drain on every
    tick. Both call into ``_run_pass`` so behaviour is shared.

Guarantees (Phase C acceptance):

  * The worker scans backoff the queued-job queue at every tick and
    makes **zero model calls** when there is no increment. ``model_calls``
    on the worker run row equals the count of rows added to the
    ``model_calls`` table during the pass.
  * Each tick acquires a fresh ``lease_token`` for ``worker_id``. The
    heartbeat extends the lease; an expired lease can be reclaimed by
    a different process so two workers cannot both publish the same
    job.
  * A vault-level writer lock guarantees only one worker process may
    run a pass at a time. ``WorkerLeaseRepo.acquire`` refuses a second
    worker while any *other* worker's lease is still active; a
    different worker may reclaim only after the previous lease has
    expired (Phase C repair-1).
  * Stale ``running`` jobs whose ``last_heartbeat`` is older than the
    lease window are reset to ``queued`` at the start of each pass so
    a crashed worker can be recovered without leaving jobs stuck
    forever. The reclaim is durable and observable in the jobs row.
  * Cancellation: ``cancel_requested=1`` on the job makes the worker
    stop after the current step. Already-published ``commit`` rows are
    *not* rolled back (cancel never undoes a successful publish).
  * Worker-owned schedules: a configurable JSON schedule under
    ``<vault>/config/worker_schedule.json`` (or the ``--schedule``
    flag) defines the wake-up cadence. The worker also runs a
    source-correction discovery pass on each tick so source rewrites
    are caught even when no scan/plan/process call has happened yet.
  * Daily/weekly/monthly cadence: the worker keeps ``last_daily_run``,
    ``last_weekly_run`` and ``last_monthly_run`` in
    ``worker_runtime_state``. Weekly/monthly are only triggered after
    a daily catch-up has run in the same wall-clock window.

Phase C repair-2 — real scheduled work:

  * The daily pass now actually **scans** the configured roots
    (read from ``<vault>/config.toml`` / ``<vault>/config.json``),
    walks the queued async jobs, then bumps ``last_daily_run`` **only
    if** either step produced a real effect (new event rows / drained
    jobs) — and never if a step raised. A worker that ran nothing
    leaves the timestamp untouched so a subsequent hourly check still
    sees the daily catch-up is needed.
  * The weekly pass produces a *conservative*, auditable review
    proposal: it walks the memory projection and surfaces a small set
    of obvious anomalies (e.g. one project carrying 30+ active
    memories with the same kind, or two near-duplicate statements).
    The proposal is written to
    ``<vault>/artifacts/reviews/weekly_<date>.json`` with an
    ``audit_id``; nothing is published automatically.
  * The monthly pass is similar but operates on the *domain* axis:
    it walks ``memory_projection.primary_domain`` and proposes
    domain merges / splits when the same primary domain is held by
    both a global and a project-scoped memory with overlapping text.
    Output lives in
    ``<vault>/artifacts/reviews/monthly_<date>.json``.
  * ``memory call --operation review --params period=week|month``
    runs the same code path on demand and returns the proposal in
    the response so the CLI can print it. ``review`` is no longer
    ``UNSUPPORTED``.

The worker itself does **not** instantiate the model provider for
the schedule pass — review proposals are deterministic (count + text
similarity, no LLM call). The provider is only lazily constructed
inside ``legacy_dispatch`` for model-requiring operations
(``batch.process`` / ``maintain``); a pass that only drains
model-free jobs (``scan`` / ``init`` / ``doctor`` / ``batch.plan`` /
``conflicts`` / ``decide`` / ``job.*``) makes zero model calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory_agent.application.dispatch import dispatch as legacy_dispatch
from memory_agent.application.recovery import recover_unfinished_intents
from memory_agent.application.source_correction import (
    discover_affected_memories,
    refresh_overlay,
)
from memory_agent.infrastructure.files import VaultLayout
from memory_agent.infrastructure.sqlite import open_state_db
from memory_agent.infrastructure.sqlite.repositories import (
    AuditRepo,
    JobRepo,
    MemoryProjectionRepo,
    WorkerLeaseRepo,
    WorkerRunRepo,
    WorkerRuntimeStateRepo,
)


DEFAULT_SCHEDULE = {
    "every_seconds": 1,           # tick cadence for tests
    "daily_at_hour": 2,           # local clock hour for daily catch-up
    "weekly_day": "sun",          # weekly window anchor
    "weekly_at_hour": 3,          # weekly window hour
    "monthly_day": 1,             # monthly window anchor (1..28)
    "monthly_at_hour": 4,         # monthly window hour
}


@dataclass
class WorkerPassResult:
    """Result of one worker pass."""

    worker_id: str
    lease_acquired: bool = False
    lease_released: bool = False
    jobs_claimed: list[str] = field(default_factory=list)
    jobs_succeeded: list[str] = field(default_factory=list)
    jobs_failed: list[str] = field(default_factory=list)
    jobs_cancelled: list[str] = field(default_factory=list)
    model_calls_before: int = 0
    model_calls_after: int = 0
    source_correction: dict[str, Any] | None = None
    telegram: dict[str, Any] = field(default_factory=dict)
    daily_run: bool = False
    weekly_run: bool = False
    monthly_run: bool = False
    diagnostics: dict[str, Any] = field(default_factory=dict)
    outcome: str = "ok"

    @property
    def model_calls(self) -> int:
        return max(0, self.model_calls_after - self.model_calls_before)

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "lease_acquired": self.lease_acquired,
            "lease_released": self.lease_released,
            "jobs_claimed": list(self.jobs_claimed),
            "jobs_succeeded": list(self.jobs_succeeded),
            "jobs_failed": list(self.jobs_failed),
            "jobs_cancelled": list(self.jobs_cancelled),
            "model_calls": self.model_calls,
            "source_correction": self.source_correction,
            "telegram": dict(self.telegram),
            "daily_run": self.daily_run,
            "weekly_run": self.weekly_run,
            "monthly_run": self.monthly_run,
            "diagnostics": dict(self.diagnostics),
            "outcome": self.outcome,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str, *parts: str) -> str:
    h = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{h[:16]}"


def _now_local() -> datetime:
    return datetime.now().astimezone()


def _iso_in(seconds: int) -> str:
    """ISO-8601 UTC timestamp ``seconds`` from now (negative = past)."""
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _read_schedule(schedule_path: Path | None) -> dict[str, Any]:
    base = dict(DEFAULT_SCHEDULE)
    if schedule_path is not None and schedule_path.exists():
        try:
            overrides = json.loads(schedule_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            overrides = {}
        base.update({k: v for k, v in overrides.items() if k in base})
    return base


def _schedule_path(vault: VaultLayout) -> Path:
    return vault.config_path.parent / "worker_schedule.json"


def run_worker_once(
    *,
    vault_root: Path,
    worker_id: str | None = None,
    lease_ttl_seconds: int = 30,
    heartbeat_every_seconds: float = 0.0,
    schedule_path: Path | None = None,
    force_daily: bool = False,
    force_weekly: bool = False,
    force_monthly: bool = False,
) -> WorkerPassResult:
    """Run exactly one worker pass and return the result.

    Tests use this to drive a single tick deterministically. The
    pass:
      1. expires stale leases
      2. acquires a fresh lease (or reclaims an expired one)
      3. drains queued jobs
      4. runs source-correction discovery
      5. evaluates the schedule for daily / weekly / monthly runs
      6. releases the lease and writes a worker_runs row
    """
    vault = VaultLayout(vault_root)
    schedule = _read_schedule(schedule_path or _schedule_path(vault))
    worker_id = worker_id or os.environ.get("MEMORY_AGENT_WORKER_ID") or \
        "worker_local"
    lease_token = _new_id("lease", worker_id, _now_iso())
    result = WorkerPassResult(worker_id=worker_id)

    conn = open_state_db(vault.state_db)
    try:
        lease_repo = WorkerLeaseRepo(conn)
        run_repo = WorkerRunRepo(conn)
        job_repo = JobRepo(conn)
        run_id = _new_id("run", worker_id, _now_iso())
        run_repo.insert({
            "run_id": run_id,
            "worker_id": worker_id,
            "started_at": _now_iso(),
            "diagnostics": {"schedule": schedule},
        })

        # 1. expire stale leases.
        expired = lease_repo.expire_stale()

        # 2. acquire fresh lease (reclaim expired if any).
        acquired = lease_repo.acquire(
            worker_id=worker_id,
            lease_token=lease_token,
            ttl_seconds=lease_ttl_seconds,
            current_job_id=None,
        )
        if not acquired:
            # Someone else owns an active lease — refuse to run.
            result.diagnostics["lease_conflict"] = True
            result.outcome = "skipped"
            run_repo.finalize(run_id, outcome="skipped",
                              diagnostics=result.to_dict())
            return result
        result.lease_acquired = True
        run_repo.increment(run_id, "leases_acquired")

        try:
            # 3. recover any half-finished intents.
            recover_unfinished_intents(vault=vault, conn=conn)

            # 3a. reclaim any ``running`` job whose heartbeat has
            #     expired. A worker that crashed mid-job (or was
            #     killed by the OS) leaves the job in ``running`` with
            #     no live lease. Without this reclaim the job stays
            #     stuck forever; with it, the next pass picks it up.
            reclaimed = job_repo.reclaim_stale_running(
                older_than_iso=_iso_in(-lease_ttl_seconds),
            )
            result.diagnostics["reclaimed_running"] = reclaimed

            # 4. drain queued jobs.
            drain_queued_jobs(
                vault=vault,
                conn=conn,
                lease_token=lease_token,
                heartbeat=heartbeat_every_seconds,
                result=result,
                run_id=run_id,
                run_repo=run_repo,
            )

            # 5. source-correction discovery.
            corr = discover_affected_memories(vault=vault, conn=conn)
            overlay_touched = refresh_overlay(conn=conn)
            result.source_correction = {
                **corr.to_dict(),
                "overlay_rows_touched": overlay_touched,
            }
            run_repo.increment(run_id, "model_calls", _count_model_calls(conn))
            result.model_calls_after = _count_model_calls(conn)

            # 5a. Telegram decision channel (P4/T12): enqueue missing
            #     conflict notifications, drain the durable outbox with
            #     retry, and poll the single long-polling receiver once.
            #     Wrapped so a network outage leaves pending work and
            #     never blocks local scan / recall.
            result.telegram = _telegram_pass(vault=vault, conn=conn)

            # 6. schedule-aware runs.
            _maybe_daily_pass(
                vault=vault, conn=conn, schedule=schedule, result=result,
                force=force_daily,
            )
            _maybe_weekly_pass(
                vault=vault, conn=conn, schedule=schedule, result=result,
                force=force_weekly,
            )
            _maybe_monthly_pass(
                vault=vault, conn=conn, schedule=schedule, result=result,
                force=force_monthly,
            )
        finally:
            lease_repo.release(worker_id=worker_id, lease_token=lease_token)
            result.lease_released = True
            run_repo.increment(run_id, "leases_released")
            run_repo.finalize(run_id, outcome=result.outcome,
                              diagnostics=result.to_dict())
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return result


def run_worker(
    *,
    vault_root: Path,
    worker_id: str | None = None,
    lease_ttl_seconds: int = 30,
    schedule_path: Path | None = None,
    stop_path: Path | None = None,
) -> None:
    """Long-running worker loop. Blocks until ``stop_path`` exists."""
    vault = VaultLayout(vault_root)
    schedule = _read_schedule(schedule_path or _schedule_path(vault))
    every = int(schedule.get("every_seconds", 60))
    stop_path = stop_path or (vault.root / "worker.stop")
    while True:
        try:
            run_worker_once(
                vault_root=vault_root,
                worker_id=worker_id,
                lease_ttl_seconds=lease_ttl_seconds,
                schedule_path=schedule_path,
            )
        except Exception as exc:  # noqa: BLE001
            # never let a single tick crash the loop
            try:
                conn = open_state_db(vault.state_db)
                AuditRepo(conn).append({
                    "audit_id": _new_id("aud", "worker", _now_iso()),
                    "actor": "worker",
                    "action": "worker.pass_crashed",
                    "occurred_at": _now_iso(),
                    "object_type": "worker",
                    "object_id": worker_id or "worker_local",
                    "correlation": {},
                    "before_after": {},
                    "outcome": "error",
                    "reason_code": "INTERNAL",
                    "details": {"error": f"{type(exc).__name__}: {exc}"},
                })
                conn.close()
            except Exception:
                pass
        if stop_path is not None and stop_path.exists():
            try:
                stop_path.unlink()
            except OSError:
                pass
            return
        time.sleep(max(1, every))


def _telegram_pass(*, vault: VaultLayout, conn) -> dict[str, Any]:
    """Run one Telegram pass; never raises.

    When Telegram is not configured (no ``config/telegram.json`` or no
    token in the environment) this returns ``{"enabled": False}`` and
    makes **zero** network calls.
    """
    try:
        from memory_agent.adapters.telegram.service import run_telegram_pass
        return run_telegram_pass(vault=vault, conn=conn)
    except Exception as exc:  # noqa: BLE001
        return {"enabled": False, "error": f"{type(exc).__name__}: {exc}"}


def drain_queued_jobs(
    *,
    vault: VaultLayout,
    conn,
    lease_token: str,
    heartbeat: float = 0.0,
    result: WorkerPassResult,
    run_id: str,
    run_repo: WorkerRunRepo,
) -> None:
    """Drain every queued async job once.

    The job state machine here is:
        queued → running → (succeeded | failed | cancelled)
    ``running`` means *this* worker holds the lease; the job's
    ``last_heartbeat`` is refreshed each step so a stale worker can be
    detected. A job whose ``cancel_requested`` flag was set while the
    pass was running is finalised as ``cancelled`` instead of
    completing the step.

    Model-provider opt-in is **never** instantiated by the drain loop.
    Model-free operations (``scan`` / ``init`` / ``doctor`` /
    ``batch.plan`` / ``conflicts`` / ``decide`` / ``job.*``) are
    dispatched through ``legacy_dispatch`` which does not touch the
    provider at all; model-requiring operations (``batch.process``,
    ``maintain``) instantiate the provider *inside* ``legacy_dispatch``
    on the first call, so a ``ModelError`` from a missing / disabled
    Claude CLI is caught and the job is marked ``failed`` instead of
    leaving it ``running`` after the exception escapes.
    """
    job_repo = JobRepo(conn)
    audit = AuditRepo(conn)
    while True:
        if result.outcome == "skipped":
            return
        # Pre-check the queue for any already-cancelled jobs that need
        # to be marked cancelled; we record them in the result so
        # callers can see them without touching the DB directly.
        pre_cancelled = conn.execute(
            "SELECT job_id FROM jobs WHERE state = 'queued' "
            "AND cancel_requested = 1 ORDER BY created_at"
        ).fetchall()
        for row in pre_cancelled:
            cid = str(row["job_id"])
            if cid in result.jobs_cancelled:
                continue
            result.jobs_cancelled.append(cid)
            run_repo.increment(run_id, "jobs_cancelled")
            audit.append({
                "audit_id": _new_id("aud", cid, "cancel-queued"),
                "actor": "worker",
                "action": "job.cancelled",
                "occurred_at": _now_iso(),
                "object_type": "job",
                "object_id": cid,
                "correlation": {"job_id": cid, "worker_id": result.worker_id},
                "before_after": {"state": "queued"},
                "outcome": "success",
                "reason_code": None,
                "details": {"reason": "cancel_observed_while_queued"},
            })
        job = job_repo.next_queued()
        if job is None:
            return
        result.jobs_claimed.append(str(job["job_id"]))
        run_repo.increment(run_id, "jobs_started")
        params_ref = str(job.get("params_ref") or "")
        params_blob = _load_params_artifact(vault, params_ref) or {}
        operation = str(job.get("operation") or params_blob.get("operation") or "")
        params = dict(params_blob.get("params") or {})
        try:
            job_repo.heartbeat(str(job["job_id"]))
            # Cancellation observed before any work runs.
            if job_repo.is_cancel_requested(str(job["job_id"])):
                _finalise_cancelled(job_repo, audit, job, run_repo, run_id, result)
                continue
            outcome = _execute_operation(
                operation=operation,
                params=params,
                vault=vault,
                conn=conn,
                request_id=str(job.get("request_id") or "worker"),
                base_lease_token=lease_token,
            )
            if job_repo.is_cancel_requested(str(job["job_id"])):
                _finalise_cancelled(job_repo, audit, job, run_repo, run_id, result)
                continue
            job_repo.set_result(str(job["job_id"]), outcome.get("result") or {})
            result.jobs_succeeded.append(str(job["job_id"]))
            run_repo.increment(run_id, "jobs_completed")
            audit.append({
                "audit_id": _new_id("aud", job["job_id"], "ok"),
                "actor": "worker",
                "action": f"{operation}.succeeded",
                "occurred_at": _now_iso(),
                "object_type": "job",
                "object_id": job["job_id"],
                "correlation": {"job_id": job["job_id"], "worker_id": result.worker_id},
                "before_after": {},
                "outcome": "success",
                "reason_code": None,
                "details": {"operation": operation},
            })
        except Exception as exc:  # noqa: BLE001
            err = {"code": "INTERNAL", "message": f"{type(exc).__name__}: {exc}"}
            # ModelError from a missing Claude CLI provider opt-in is
            # the most likely cause; surface it as a real failure so
            # the job does not stay ``running`` after the pass ends.
            try:
                from memory_agent.adapters.models import ModelError
                if isinstance(exc, ModelError):
                    err["code"] = "PROVIDER_UNAVAILABLE"
            except ImportError:  # pragma: no cover
                pass
            job_repo.set_error(str(job["job_id"]), err)
            result.jobs_failed.append(str(job["job_id"]))
            run_repo.increment(run_id, "jobs_failed")
            audit.append({
                "audit_id": _new_id("aud", job["job_id"], "fail"),
                "actor": "worker",
                "action": f"{operation}.failed",
                "occurred_at": _now_iso(),
                "object_type": "job",
                "object_id": job["job_id"],
                "correlation": {"job_id": job["job_id"], "worker_id": result.worker_id},
                "before_after": {},
                "outcome": "error",
                "reason_code": err.get("code", "INTERNAL"),
                "details": {"error": err},
            })


def _finalise_cancelled(
    job_repo: JobRepo, audit: AuditRepo, job: dict[str, Any],
    run_repo: WorkerRunRepo, run_id: str, result: WorkerPassResult,
) -> None:
    job_repo.set_error(
        str(job["job_id"]),
        {"code": "BUSY", "message": "cancelled by user"},
        final_state="cancelled",
    )
    result.jobs_cancelled.append(str(job["job_id"]))
    run_repo.increment(run_id, "jobs_cancelled")
    audit.append({
        "audit_id": _new_id("aud", job["job_id"], "cancel"),
        "actor": "worker",
        "action": "job.cancelled",
        "occurred_at": _now_iso(),
        "object_type": "job",
        "object_id": job["job_id"],
        "correlation": {"job_id": job["job_id"], "worker_id": result.worker_id},
        "before_after": {},
        "outcome": "success",
        "reason_code": None,
        "details": {"state": "cancelled"},
    })


def _execute_operation(
    *,
    operation: str,
    params: dict[str, Any],
    vault: VaultLayout,
    conn,
    request_id: str,
    base_lease_token: str,
) -> dict[str, Any]:
    """Dispatch one operation, returning the inner ``result`` dict."""
    legacy = legacy_dispatch(
        operation=operation,
        params=params,
        request_id=request_id,
        client_id="worker",
        vault_root=vault.root,
    )
    if legacy.get("status") != "ok":
        raise RuntimeError(
            f"{operation} failed: {legacy.get('error') or legacy}"
        )
    return legacy


def _read_configured_roots(vault: VaultLayout, conn=None) -> dict[str, str]:
    """Read the per-connector ``roots`` config from the vault.

    The authoritative source is the ``source_files`` table — every
    ``init`` and ``scan`` registers a source file there, and the
    per-connector root is the parent of every canonical path. We
    fall back to the ``config.toml`` / ``config.json`` file for
    cold vaults that have never scanned; the file may be JSON or
    TOML (the dispatcher's ``_cmd_init`` writes JSON to
    ``config.toml`` for backwards compatibility), so we try both
    parsers so a mislabelled extension does not silently drop the
    configured roots. Empty / malformed files yield an empty dict,
    not an error.
    """
    # 1. Build a connector -> parent-path map from source_files.
    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT DISTINCT connector, canonical_path FROM source_files"
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        roots: dict[str, str] = {}
        for row in rows:
            connector = str(row["connector"])
            path = str(row["canonical_path"])
            parent = str(Path(path).parent)
            roots.setdefault(connector, parent)
        if roots:
            return roots
    # 2. Fall back to the config file.
    candidates = [vault.config_path, vault.root / "config.json"]
    for path in candidates:
        if not path.exists():
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        cfg: Any = None
        for parser in (_try_toml, _try_json):
            cfg = parser(raw)
            if cfg is not None:
                break
        if cfg is None:
            continue
        roots_cfg = cfg.get("roots") if isinstance(cfg, dict) else None
        if isinstance(roots_cfg, dict):
            return {str(k): str(v) for k, v in roots_cfg.items() if v}
    return {}


def _try_toml(raw: str) -> Any:
    try:
        import tomllib as _toml
    except ImportError:  # pragma: no cover
        try:
            import tomli as _toml  # type: ignore[no-redef]
        except ImportError:
            return None
    try:
        return _toml.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def _try_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def _safe_scan(vault: VaultLayout, conn, roots: dict[str, str]) -> dict[str, Any]:
    """Run ``scan_once`` on the configured roots and return the result.

    Returns a small dict with counters; never raises. Errors are
    captured under ``diagnostics`` so the daily pass can still bump
    its timestamp when the failure is transient (we want the audit
    row, but a missing root must not crash the worker pass).
    """
    from memory_agent.application.scan import default_adapters, scan_once
    if not roots:
        return {
            "files_seen": 0,
            "new_events": 0,
            "rotations": 0,
            "middle_rewrites": 0,
            "diagnostics": [{"code": "no_roots_configured"}],
        }
    try:
        result = scan_once(
            vault=vault, conn=conn,
            adapters=default_adapters(), roots=roots,
            mode="incremental",
        )
        return {
            "files_seen": result.files_seen,
            "new_events": result.new_events,
            "rotations": result.rotations,
            "middle_rewrites": result.middle_rewrites,
            "duplicate_events": result.duplicate_events,
            "diagnostics": result.diagnostics,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "files_seen": 0,
            "new_events": 0,
            "rotations": 0,
            "middle_rewrites": 0,
            "diagnostics": [{
                "code": "scan_failed",
                "error": f"{type(exc).__name__}: {exc}",
            }],
        }


def _maybe_daily_pass(
    *, vault: VaultLayout, conn, schedule: dict[str, Any],
    result: WorkerPassResult, force: bool = False,
) -> None:
    """Daily catch-up: scan configured roots, drain any pending
    recall of recent events, and bump the timestamp only when real
    work happened.

    Contract (phase C repair-2):

      * If the configured window says we already ran today, skip
        (unless ``force=True``).
      * Read the configured roots from ``<vault>/config.toml`` (or
        ``config.json``). When no roots are configured (cold vault
        or wrong path), the daily pass is a no-op and the timestamp
        is **not** bumped — the next tick will retry.
      * Run ``scan_once`` on the configured roots. Any error is
        captured; the pass never raises.
      * The timestamp advances only when either (a) the scan
        observed new events or a rewrite, OR (b) a queued async job
        that was already in flight (e.g. ``maintain``) was
        completed by this same pass. The first condition ensures
        "no increment → no timestamp bump" so the next day's
        scheduler still sees the daily catch-up as needed.
      * Model calls: the scan path makes zero provider calls; this
        is a model-free operation.
    """
    state = WorkerRuntimeStateRepo(conn)
    last = state.get("last_daily_run") or {}
    target_hour = int(schedule.get("daily_at_hour", 2))
    now = _now_local()
    if not force and (now.hour != target_hour or last.get("date") == now.date().isoformat()):
        return
    roots = _read_configured_roots(vault, conn)
    scan_summary = _safe_scan(vault, conn, roots)
    # The pass ran queued jobs already (step 4 of run_worker_once).
    # ``result.jobs_succeeded`` is the list of job_ids that landed
    # ``succeeded`` during this same pass.
    had_increment = (
        int(scan_summary.get("new_events") or 0) > 0
        or int(scan_summary.get("rotations") or 0) > 0
        or int(scan_summary.get("middle_rewrites") or 0) > 0
    )
    had_job_completion = bool(result.jobs_succeeded)
    if not (had_increment or had_job_completion):
        # No real work this pass. Leave the timestamp alone so the
        # next pass inside the daily window keeps trying.
        result.diagnostics["daily_pass_no_increment"] = {
            "scan": scan_summary,
            "queued_jobs_succeeded": list(result.jobs_succeeded),
        }
        return
    state.upsert("last_daily_run", {
        "date": now.date().isoformat(),
        "at": _now_iso(),
        "worker_id": result.worker_id,
        "new_events": int(scan_summary.get("new_events") or 0),
        "rotations": int(scan_summary.get("rotations") or 0),
        "middle_rewrites": int(scan_summary.get("middle_rewrites") or 0),
        "queued_jobs_succeeded": list(result.jobs_succeeded),
        "roots": roots,
    })
    result.daily_run = True
    result.diagnostics["daily_pass_increment"] = {
        "scan": scan_summary,
        "queued_jobs_succeeded": list(result.jobs_succeeded),
    }


def _review_proposal_path(
    vault: VaultLayout, *, period: str, day: str,
) -> Path:
    """Build the per-period review artifact path."""
    root = vault.artifacts_dir / "reviews"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{period}_{day}.json"


def _write_review_proposal(
    *,
    vault: VaultLayout,
    period: str,
    today: str,
    proposals: list[dict[str, Any]],
    metrics: dict[str, Any],
    worker_id: str,
) -> dict[str, Any]:
    """Persist a review proposal artifact and audit row.

    The artifact is JSON; the response includes its path and the
    list of proposals. ``proposals`` is intentionally small
    (deterministic anomaly heuristics, no LLM). The audit row is
    written by the worker pass that triggered the review.
    """
    target = _review_proposal_path(vault, period=period, day=today)
    audit_id = _new_id("review", period, today, worker_id)
    payload = {
        "period": period,
        "date": today,
        "worker_id": worker_id,
        "audit_id": audit_id,
        "created_at": _now_iso(),
        "metrics": metrics,
        "proposals": proposals,
    }
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "period": period,
        "date": today,
        "path": str(target),
        "audit_id": audit_id,
        "proposal_count": len(proposals),
        "proposals": proposals,
        "metrics": metrics,
    }


def _weekly_proposals(
    vault: VaultLayout, conn,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Conservative weekly review: a small set of obvious anomalies.

    Heuristics (all deterministic, no model call):

      * A single project with 30+ active memories of the same kind —
        a candidate for ``split``.
      * Two near-duplicate statements (token Jaccard ≥ 0.85) with
        the same scope — a candidate for ``merge``.

    Output is bounded to a small list. Anything larger is left for a
    future review package; we never auto-publish.
    """
    repo = MemoryProjectionRepo(conn)
    rows = repo.list_active()
    metrics = {
        "active_memories": len(rows),
        "projects": {},
        "kinds": {},
    }
    # Aggregate by (project, kind).
    bucket: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        scope = _safe_json(row.get("scope_json")) or {}
        proj = (scope.get("project_ids") or [None])[0] or "_global"
        kind = str(row.get("kind") or "requirement")
        bucket.setdefault((proj, kind), []).append(row)
        metrics["projects"][str(proj)] = metrics["projects"].get(str(proj), 0) + 1
        metrics["kinds"][kind] = metrics["kinds"].get(kind, 0) + 1
    proposals: list[dict[str, Any]] = []
    for (proj, kind), group in bucket.items():
        if len(group) >= 30:
            proposals.append({
                "kind": "split_candidate",
                "project_id": proj,
                "memory_kind": kind,
                "memory_count": len(group),
                "reason": (
                    f"project {proj!r} carries {len(group)} active "
                    f"{kind!r} memories; consider splitting into a "
                    "sub-rule or a dedicated task scope"
                ),
                "sample_memory_ids": [
                    str(r.get("memory_id")) for r in group[:5]
                ],
            })
    # Near-duplicate detection (cheap token Jaccard on the statement).
    statements: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        stmt = (row.get("statement") or "").strip().lower()
        if not stmt:
            continue
        tokens = re.findall(r"[\w一-鿿]+", stmt)
        key = " ".join(sorted(tokens))
        statements.setdefault(key, []).append(row)
    for key, group in statements.items():
        if len(group) >= 2 and _all_same_scope(group):
            proposals.append({
                "kind": "merge_candidate",
                "scope": _safe_json(group[0].get("scope_json")) or {},
                "memory_count": len(group),
                "shared_tokens": key,
                "memory_ids": [str(r.get("memory_id")) for r in group],
                "reason": (
                    f"{len(group)} memories share the same token set and "
                    "scope; consider merging into a single canonical "
                    "statement"
                ),
            })
    # Cap the proposal list so a misconfigured vault does not
    # produce an unbounded review artifact.
    proposals = proposals[:20]
    return proposals, metrics


def _monthly_proposals(
    vault: VaultLayout, conn,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Conservative monthly review: domain-level anomalies.

    Heuristics:

      * A primary domain held by both a ``global`` and a
        ``project``-scoped memory with overlapping tokens — a
        candidate for ``promote_to_global`` or ``narrow_scope``.
    """
    repo = MemoryProjectionRepo(conn)
    rows = repo.list_active()
    by_domain: dict[str, list[dict[str, Any]]] = {}
    metrics = {
        "active_memories": len(rows),
        "primary_domains": {},
    }
    for row in rows:
        domain = str(row.get("primary_domain") or "").strip()
        if not domain:
            continue
        by_domain.setdefault(domain, []).append(row)
        metrics["primary_domains"][domain] = metrics["primary_domains"].get(domain, 0) + 1
    proposals: list[dict[str, Any]] = []
    for domain, group in by_domain.items():
        if len(group) < 2:
            continue
        global_rows = [r for r in group if _row_level(r) == "global"]
        project_rows = [r for r in group if _row_level(r) == "project"]
        if global_rows and project_rows:
            proposals.append({
                "kind": "scope_overlap",
                "primary_domain": domain,
                "global_count": len(global_rows),
                "project_count": len(project_rows),
                "global_memory_ids": [str(r.get("memory_id")) for r in global_rows],
                "project_memory_ids": [str(r.get("memory_id")) for r in project_rows],
                "reason": (
                    f"primary domain {domain!r} is held by both a global "
                    "memory and one or more project-scoped memories; "
                    "consider promote_to_global or narrow_scope to "
                    "remove the ambiguity"
                ),
            })
    proposals = proposals[:20]
    return proposals, metrics


def _row_level(row: dict[str, Any]) -> str | None:
    scope = _safe_json(row.get("scope_json")) or {}
    return scope.get("level") if isinstance(scope, dict) else None


def _safe_json(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _all_same_scope(rows: list[dict[str, Any]]) -> bool:
    if not rows:
        return False
    first = _safe_json(rows[0].get("scope_json")) or {}
    for row in rows[1:]:
        if (_safe_json(row.get("scope_json")) or {}) != first:
            return False
    return True


def _maybe_weekly_pass(
    *, vault: VaultLayout, conn, schedule: dict[str, Any],
    result: WorkerPassResult, force: bool = False,
) -> None:
    """Weekly classification review.

    Phase C repair-2: produce a conservative review proposal
    artifact. Bump the timestamp only when the proposal was
    successfully written; on any failure the timestamp is preserved
    so a future pass can retry. Model calls: zero.

    The pass is suppressed unless a daily catch-up has run in the
    same wall-clock window (``last_daily_run.date == today`` or the
    current pass observed real work). The dependency is on the
    daily timestamp, not on the weekly one.
    """
    state = WorkerRuntimeStateRepo(conn)
    last_weekly = state.get("last_weekly_run") or {}
    last_daily = state.get("last_daily_run") or {}
    weekly_day = str(schedule.get("weekly_day", "sun")).lower()[:3]
    weekly_hour = int(schedule.get("weekly_at_hour", 3))
    now = _now_local()
    if not force and not _is_weekly_window(now, weekly_day, weekly_hour, last_weekly):
        return
    daily_done_today = (
        result.daily_run
        or (last_daily.get("date") == now.date().isoformat())
    )
    if not daily_done_today:
        return
    try:
        proposals, metrics = _weekly_proposals(vault, conn)
        review = _write_review_proposal(
            vault=vault, period="weekly",
            today=now.date().isoformat(),
            proposals=proposals, metrics=metrics,
            worker_id=result.worker_id,
        )
    except Exception as exc:  # noqa: BLE001
        result.diagnostics["weekly_review_failed"] = (
            f"{type(exc).__name__}: {exc}"
        )
        return
    state.upsert("last_weekly_run", {
        "date": now.date().isoformat(),
        "at": _now_iso(),
        "worker_id": result.worker_id,
        "proposal_count": len(proposals),
        "path": review["path"],
        "audit_id": review["audit_id"],
    })
    result.weekly_run = True
    result.diagnostics["weekly_review"] = review


def _maybe_monthly_pass(
    *, vault: VaultLayout, conn, schedule: dict[str, Any],
    result: WorkerPassResult, force: bool = False,
) -> None:
    """Monthly domain review.

    Phase C repair-2: write a conservative review proposal
    artifact. Bump the timestamp only when the proposal was
    successfully written. Model calls: zero.

    The pass is suppressed unless a daily catch-up has run in the
    same wall-clock window (``last_daily_run.date == today`` or the
    current pass observed real work). The dependency is on the
    daily timestamp, not on the monthly one.
    """
    state = WorkerRuntimeStateRepo(conn)
    last_monthly = state.get("last_monthly_run") or {}
    last_daily = state.get("last_daily_run") or {}
    monthly_day = int(schedule.get("monthly_day", 1))
    monthly_hour = int(schedule.get("monthly_at_hour", 4))
    now = _now_local()
    if not force and not _is_monthly_window(now, monthly_day, monthly_hour, last_monthly):
        return
    daily_done_today = (
        result.daily_run
        or (last_daily.get("date") == now.date().isoformat())
    )
    if not daily_done_today:
        return
    try:
        proposals, metrics = _monthly_proposals(vault, conn)
        review = _write_review_proposal(
            vault=vault, period="monthly",
            today=now.date().isoformat(),
            proposals=proposals, metrics=metrics,
            worker_id=result.worker_id,
        )
    except Exception as exc:  # noqa: BLE001
        result.diagnostics["monthly_review_failed"] = (
            f"{type(exc).__name__}: {exc}"
        )
        return
    state.upsert("last_monthly_run", {
        "date": now.date().isoformat(),
        "at": _now_iso(),
        "worker_id": result.worker_id,
        "proposal_count": len(proposals),
        "path": review["path"],
        "audit_id": review["audit_id"],
    })
    result.monthly_run = True
    result.diagnostics["monthly_review"] = review


# ---------------------------------------------------------------------------
# review (CLI surface)
# ---------------------------------------------------------------------------


def run_review(
    *,
    vault_root: Path,
    period: str,
    worker_id: str | None = None,
) -> dict[str, Any]:
    """Synchronous ``review`` handler.

    Phase C repair-2: ``review`` is no longer ``UNSUPPORTED``. The
    handler produces the same conservative proposal artifact the
    weekly / monthly worker pass produces, but on demand and
    regardless of the schedule window. The proposal is written to
    ``<vault>/artifacts/reviews/<period>_<date>.json`` and a single
    ``review.produced`` audit row is appended.

    Returns the proposal payload (the artifact's contents). The
    caller (``dispatch._cmd_review``) wraps this in a
    ``CommandResponse``.
    """
    if period not in {"week", "month"}:
        raise ValueError(
            f"review: period must be 'week' or 'month', got {period!r}"
        )
    vault = VaultLayout(vault_root)
    conn = open_state_db(vault.state_db)
    try:
        if period == "week":
            proposals, metrics = _weekly_proposals(vault, conn)
        else:
            proposals, metrics = _monthly_proposals(vault, conn)
        today = _now_local().date().isoformat()
        review = _write_review_proposal(
            vault=vault, period=("weekly" if period == "week" else "monthly"),
            today=today, proposals=proposals, metrics=metrics,
            worker_id=worker_id or "review_cli",
        )
        # Bump the matching ``last_weekly_run`` / ``last_monthly_run``
        # timestamp so a subsequent worker pass within the same day
        # sees the review as already done.
        state = WorkerRuntimeStateRepo(conn)
        state.upsert(
            "last_weekly_run" if period == "week" else "last_monthly_run",
            {
                "date": today,
                "at": _now_iso(),
                "worker_id": worker_id or "review_cli",
                "proposal_count": len(proposals),
                "path": review["path"],
                "audit_id": review["audit_id"],
                "trigger": "review_cli",
            },
        )
        AuditRepo(conn).append({
            "audit_id": _new_id("aud", "review", period, today),
            "actor": "review_cli",
            "action": "review.produced",
            "occurred_at": _now_iso(),
            "object_type": "review",
            "object_id": review["audit_id"],
            "correlation": {"period": period, "path": review["path"]},
            "before_after": {},
            "outcome": "success",
            "reason_code": None,
            "details": {
                "period": period,
                "proposal_count": len(proposals),
                "path": review["path"],
            },
        })
        return review
    finally:
        try:
            conn.close()
        except Exception:
            pass


_DAY_NAME_TO_WEEKDAY = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}


def _is_weekly_window(
    now: datetime, weekly_day: str, weekly_hour: int,
    last: dict[str, Any],
) -> bool:
    target = _DAY_NAME_TO_WEEKDAY.get(weekly_day, 6)
    if now.weekday() != target:
        return False
    if now.hour != weekly_hour:
        return False
    if last.get("date") == now.date().isoformat():
        return False
    return True


def _is_monthly_window(
    now: datetime, monthly_day: int, monthly_hour: int,
    last: dict[str, Any],
) -> bool:
    if now.day != monthly_day:
        return False
    if now.hour != monthly_hour:
        return False
    if last.get("date") == now.date().isoformat():
        return False
    return True


def _count_model_calls(conn) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM model_calls").fetchone()
    return int(row["c"] or 0)


def _load_params_artifact(vault: VaultLayout, params_ref: str) -> dict[str, Any] | None:
    if not params_ref:
        return None
    path = Path(params_ref)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


__all__ = [
    "DEFAULT_SCHEDULE",
    "WorkerPassResult",
    "drain_queued_jobs",
    "run_review",
    "run_worker",
    "run_worker_once",
]