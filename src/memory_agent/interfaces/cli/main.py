"""CLI: ``memory`` and ``python -m memory_agent``.

Phase C adds:

  * ``memory call --request-file -|--request-file path --json``
    — versioned ``CommandRequest`` dispatcher. Reads one JSON request
    from stdin (or a file path), dispatches sync/async through the
    application layer, writes one JSON ``CommandResponse`` to stdout.
    Diagnostics go to stderr.

  * ``memory job status --job-id …`` / ``job cancel --job-id …`` /
    ``job retry --job-id …`` — read-only job state queries and
    version-checked cancel/retry intents. Cancel never rolls back an
    already-published commit.

  * ``memory worker [--once] [--schedule PATH] [--worker-id ID]`` —
    drives the project-owned worker. ``--once`` runs a single pass
    (used by acceptance tests). The long-running mode is intended to
    be supervised by the project's launchd template (see
    ``design/launchd/com.memory-agent.worker.plist``); this task does
    NOT install or load it.

Exit codes (docs/10):
  0 ok / queued / status read
  2 INVALID_REQUEST / INVALID_VERSION / UNKNOWN_OPERATION / NOT_FOUND
  3 SOURCE_UNAVAILABLE / PROVIDER_UNAVAILABLE / WORKER_OFFLINE
  4 IDEMPOTENCY_CONFLICT / LEASE_EXPIRED
  5 PARTIAL / BUSY
  6 INTERNAL / RECOVERY_REQUIRED / UNSUPPORTED
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from memory_agent.application.call import (
    CallError,
    CallOutcome,
    handle_call,
)
from memory_agent.application.worker import run_worker, run_worker_once


EXIT_OK = 0
EXIT_INVALID = 2
EXIT_UNAVAILABLE = 3
EXIT_IDEMPOTENCY = 4
EXIT_PARTIAL = 5
EXIT_INTERNAL = 6


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memory", description="Memory Agent CLI")
    p.add_argument("--vault", default=os.environ.get("MEMORY_VAULT", "./vault"),
                   help="Vault directory (default: $MEMORY_VAULT or ./vault)")
    p.add_argument("--request-id", default=None)
    p.add_argument("--idempotency-key", default=None)
    p.add_argument("--json", action="store_true", default=True,
                   help="emit one JSON CommandResponse on stdout (default behaviour)")
    p.add_argument("--pretty", action="store_true",
                   help="pretty-print the JSON response (default: compact)")
    sub = p.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init",
                              help="initialise the vault (idempotent; optional --root baselines at EOF)")
    init_p.add_argument("--root", action="append", default=[],
                          help="root of the form <connector>=<path>; repeatable. Pins cursors at end-of-file.")
    sub.add_parser("doctor", help="report vault health")
    sub.add_parser("conflicts", help="list open conflicts")
    scan = sub.add_parser("scan", help="scan configured roots")
    scan.add_argument("--root", action="append", default=[],
                       help="root of the form <connector>=<path>; repeatable")
    scan.add_argument("--mode", choices=["incremental", "backfill"], default="incremental",
                       help="incremental advances the live cursor; backfill records a "
                            "separate backfill_runs row without advancing complete_offset")
    scan.add_argument("--since", default=None,
                       help="RFC3339 (with offset or 'Z') lower bound for backfill")
    scan.add_argument("--until", default=None,
                       help="RFC3339 (with offset or 'Z') upper bound for backfill")
    scan.add_argument("--timezone", default=None,
                       help="IANA timezone recorded on the backfill_runs row; "
                            "does not reinterpret the timestamps")
    plan = sub.add_parser("batch.plan", help="plan batches for a session")
    plan.add_argument("--session-id", required=True)
    plan.add_argument("--branch-id", default="main")
    plan.add_argument("--max-turns", type=int, default=5)
    plan.add_argument("--max-input-tokens", type=int, default=6000)
    proc = sub.add_parser("batch.process", help="process planned batches")
    proc.add_argument("--batch-id", action="append", default=[])
    proc.add_argument("--max-batches", type=int, default=5)
    maint = sub.add_parser("maintain", help="scan + plan + process")
    maint.add_argument("--root", action="append", default=[])
    maint.add_argument("--max-batches", type=int, default=5)
    rec = sub.add_parser("recall", help="explicit recall (keyword or bm25)")
    rec.add_argument("--query", required=True)
    rec.add_argument("--project-id", action="append", default=[])
    rec.add_argument("--domain-id", action="append", default=[])
    rec.add_argument("--limit", type=int, default=8)
    rec.add_argument("--retrieval", choices=["keyword", "bm25"], default="keyword")
    rec.add_argument("--rewrite", choices=["off", "auto"], default="off")
    rec.add_argument("--expand", choices=["off", "auto"], default="off")
    rec.add_argument("--cursor", default=None)
    rec.add_argument("--retrieval-limit", type=int, default=30)
    rec.add_argument("--no-audit", action="store_true",
                     help="skip writing query.retrieved to audit_events")
    dec = sub.add_parser("decide", help="apply decision to a conflict")
    dec.add_argument("--conflict-id", required=True)
    dec.add_argument("--proposal-revision", type=int, required=True)
    dec.add_argument("--action", choices=["adopt_new", "keep_old", "scope_split", "defer"], required=True)
    dec.add_argument("--note", default=None)
    dec.add_argument("--idempotency-key", default=None,
                       help="required for non-defer actions; rejected without it")

    # ---- phase C -----------------------------------------------------------
    call = sub.add_parser("call",
                            help="versioned CommandRequest dispatcher (stdin JSON or --request-file)")
    call.add_argument("--request-file", default=None,
                       help="path to the JSON CommandRequest. Use '-' for stdin.")
    call.add_argument("--mode", choices=["sync", "async"], default="sync",
                       help="sync runs inline; async durably queues the job and returns queued")
    call.add_argument("--idempotency-key", dest="call_idempotency_key", default=None,
                       help="idempotency key; same key + same params returns cached outcome, "
                             + "same key + changed params -> IDEMPOTENCY_CONFLICT")

    job = sub.add_parser("job", help="query / cancel / retry async jobs")
    job_sub = job.add_subparsers(dest="job_command", required=True)
    job_status = job_sub.add_parser("status", help="read job state")
    job_status.add_argument("--job-id", required=True)
    job_cancel = job_sub.add_parser("cancel", help="request cancellation (never rolls back a published commit)")
    job_cancel.add_argument("--job-id", required=True)
    job_cancel.add_argument("--expected-revision", type=int, required=True)
    job_retry = job_sub.add_parser("retry", help="re-queue a failed/cancelled job")
    job_retry.add_argument("--job-id", required=True)
    job_retry.add_argument("--expected-revision", type=int, required=True)

    worker = sub.add_parser("worker", help="project-owned async worker loop")
    worker.add_argument("--once", action="store_true",
                          help="run exactly one pass and exit (used by tests + acceptance)")
    worker.add_argument("--worker-id", default=None,
                          help="stable worker identifier (default: $MEMORY_AGENT_WORKER_ID or 'worker_local')")
    worker.add_argument("--schedule", default=None,
                          help="path to a JSON schedule file (default: <vault>/config/worker_schedule.json)")
    worker.add_argument("--lease-ttl", type=int, default=30,
                          help="lease TTL in seconds (default: 30)")
    worker.add_argument("--stop-file", default=None,
                          help="path to a sentinel file; the worker exits when this file is created")
    worker.add_argument("--force-daily", action="store_true", help="--once: force a daily run")
    worker.add_argument("--force-weekly", action="store_true", help="--once: force a weekly run")
    worker.add_argument("--force-monthly", action="store_true", help="--once: force a monthly run")

    # ---- P7/T15 + P8/T16: loopback audit HTTP/UI -----------------------------
    ui_p = sub.add_parser(
        "ui",
        help="serve the loopback audit UI (binds 127.0.0.1:<port>)",
    )
    ui_p.add_argument("--host", default="127.0.0.1",
                       help="bind host (MUST be 127.0.0.1 / localhost / ::1)")
    ui_p.add_argument("--port", type=int, default=8440,
                       help="loopback port (default 8440 — clear of Octopus on 8420)")
    ui_p.add_argument("--ui-dist", default=None,
                       help="path to the built static UI directory (default: <repo>/web/dist)")
    ui_p.add_argument("--session-token", default=None,
                       help="shared secret for POST /api/v1/commands "
                              "(overrides MEMORY_AGENT_HTTP_TOKEN)")
    ui_p.add_argument("--no-token", action="store_true",
                       help="disable POST writes (audit UI runs in read-only mode)")
    ui_p.add_argument("--reload", action="store_true",
                       help="reload the UI on source changes (development only)")

    # ---- P9 / T17: privacy deletion barrier --------------------------------
    privacy_p = sub.add_parser("privacy",
                                help="privacy deletion barrier (forget / list / receipt / unblock)")
    privacy_sub = privacy_p.add_subparsers(dest="privacy_command", required=True)
    pf = privacy_sub.add_parser("forget",
                                 help="authorise and persist a forget operation")
    pf.add_argument("--kind", choices=["memory", "event", "source_block"],
                     required=True)
    pf.add_argument("--target", action="append", required=True,
                     help="memory_id / event_id / source_file_id; repeatable")
    pf.add_argument("--idempotency-key", required=True)
    pf.add_argument("--note", default=None)
    pl = privacy_sub.add_parser("list",
                                 help="list every privacy receipt")
    pl.add_argument("--kind", choices=["memory", "event", "source_block"],
                     default=None)
    pl.add_argument("--limit", type=int, default=100)
    pr = privacy_sub.add_parser("receipt",
                                 help="read one privacy receipt by op_id")
    pr.add_argument("--op-id", required=True)
    pu = privacy_sub.add_parser("unblock",
                                 help="lift a source_block re-ingest guard")
    pu.add_argument("--op-id", required=True)
    pu.add_argument("--idempotency-key", required=True)

    # ---- P9 / T17: consistent SQLite + files backup / restore ----------------
    backup_p = sub.add_parser("backup",
                                help="consistent SQLite+files backup and restore")
    backup_sub = backup_p.add_subparsers(dest="backup_command", required=True)
    bc = backup_sub.add_parser("create",
                                 help="snapshot the vault to a tarball or directory")
    bc.add_argument("--output", required=True,
                     help="absolute path of the .tar (or directory) to write")
    bc.add_argument("--format", choices=["tar", "dir"], default="tar")
    bc.add_argument("--label", default=None)
    bl = backup_sub.add_parser("list",
                                 help="read a backup bundle's manifest")
    bl.add_argument("--bundle", required=True,
                     help="path of the .tar (or directory) bundle to inspect")
    br = backup_sub.add_parser("restore",
                                 help="restore a backup bundle into a target vault")
    br.add_argument("--bundle", required=True,
                     help="path of the .tar (or directory) bundle")
    br.add_argument("--target", required=True,
                     help="path of the (empty) target vault directory")
    # Privacy safety: the operator's live vault's privacy ledger is
    # always consulted; the CLI no longer accepts an integer version
    # input (which could be forged) and no longer exposes an opt-out
    # flag. Stale or receipt-mismatched bundles are quarantined by
    # default and the target is left unmounted.

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    main._last_namespace = args  # type: ignore[attr-defined]
    vault_root = Path(args.vault).expanduser().resolve()

    try:
        if args.command == "call":
            return _cmd_call(args, vault_root)
        if args.command == "job":
            return _cmd_job(args, vault_root)
        if args.command == "worker":
            return _cmd_worker(args, vault_root)
        if args.command == "ui":
            return _cmd_ui(args, vault_root)
        if args.command == "privacy":
            return _cmd_privacy(args, vault_root)
        if args.command == "backup":
            return _cmd_backup(args, vault_root)
    except CallError as exc:
        return _exit_code_for(exc.code)

    # Legacy path
    params = _params_from_args(args)
    roots = _parse_roots(getattr(args, "root", []) or [])
    if roots:
        params["roots"] = roots
    try:
        response = _legacy_dispatch(
            operation=_op_name(args.command),
            params=params,
            request_id=args.request_id,
            idempotency_key=args.idempotency_key,
            vault_root=vault_root,
        )
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "error", "error": {"code": "INTERNAL", "message": str(exc)}}),
              file=sys.stdout)
        return EXIT_INTERNAL

    sys.stdout.write(json.dumps(response, ensure_ascii=False))
    sys.stdout.write("\n")
    if response.get("status") == "error":
        code = (response.get("error") or {}).get("code", "INTERNAL")
        return _exit_code_for(code)
    return EXIT_OK


def _cmd_call(args: argparse.Namespace, vault_root: Path) -> int:
    raw = _read_request_payload(args)
    try:
        request = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(json.dumps(_error_dict("INVALID_REQUEST",
                                     f"call: stdin is not valid JSON: {exc}")))
        return EXIT_INVALID
    if not isinstance(request, dict):
        print(json.dumps(_error_dict("INVALID_REQUEST",
                                     "call: top-level JSON must be an object")))
        return EXIT_INVALID
    if args.call_idempotency_key and not request.get("idempotency_key"):
        request["idempotency_key"] = args.call_idempotency_key
    if args.request_id and not request.get("request_id"):
        request["request_id"] = args.request_id
    if args.mode and not request.get("mode"):
        request["mode"] = args.mode
    outcome = handle_call(request=request, vault_root=vault_root)
    return _write_call_outcome(outcome)


def _cmd_job(args: argparse.Namespace, vault_root: Path) -> int:
    job_command = args.job_command
    job_id = args.job_id
    # Build a CommandRequest that the call dispatcher can read.
    if job_command == "status":
        payload = {
            "api_version": 1,
            "request_id": f"req_cli_{job_command}_{job_id[:8]}",
            "operation": "job.status",
            "mode": "sync",
            "params": {"job_id": job_id},
        }
    elif job_command == "cancel":
        payload = {
            "api_version": 1,
            "request_id": f"req_cli_{job_command}_{job_id[:8]}",
            "operation": "job.cancel",
            "mode": "sync",
            "params": {
                "job_id": job_id,
                "expected_revision": args.expected_revision,
            },
        }
    elif job_command == "retry":
        payload = {
            "api_version": 1,
            "request_id": f"req_cli_{job_command}_{job_id[:8]}",
            "operation": "job.retry",
            "mode": "sync",
            "params": {
                "job_id": job_id,
                "expected_revision": args.expected_revision,
            },
        }
    else:  # pragma: no cover
        print(json.dumps(_error_dict("INVALID_REQUEST", f"unknown job subcommand {job_command!r}")))
        return EXIT_INVALID
    outcome = handle_call(request=payload, vault_root=vault_root)
    return _write_call_outcome(outcome)


def _cmd_worker(args: argparse.Namespace, vault_root: Path) -> int:
    schedule_path = Path(args.schedule).expanduser() if args.schedule else None
    stop_file = Path(args.stop_file).expanduser() if args.stop_file else None
    if args.once:
        result = run_worker_once(
            vault_root=vault_root,
            worker_id=args.worker_id,
            lease_ttl_seconds=int(args.lease_ttl),
            schedule_path=schedule_path,
            force_daily=args.force_daily,
            force_weekly=args.force_weekly,
            force_monthly=args.force_monthly,
        )
        sys.stdout.write(json.dumps(result.to_dict(), ensure_ascii=False, default=str))
        sys.stdout.write("\n")
        return EXIT_OK
    run_worker(
        vault_root=vault_root,
        worker_id=args.worker_id,
        lease_ttl_seconds=int(args.lease_ttl),
        schedule_path=schedule_path,
        stop_path=stop_file,
    )
    return EXIT_OK


def _cmd_ui(args: argparse.Namespace, vault_root: Path) -> int:
    """Launch the loopback audit HTTP/UI server (P7/T15 + P8/T16).

    Binds only to 127.0.0.1 / localhost / ::1. Loads the packaged
    Vite ``web/dist`` if present; the FastAPI app already mounts the
    directory via ``serve_ui_dist`` so a missing build still serves a
    placeholder page (and a clear ``ui_dist=None`` log line) — that
    fallback is the contract the T14 HTTP task ships today.

    ``--session-token`` (or ``MEMORY_AGENT_HTTP_TOKEN``) sets the
    shared secret the UI must echo in ``X-Memory-Agent-Token`` for
    POST /api/v1/commands. ``--no-token`` forces read-only mode.
    """

    from memory_agent.interfaces.http.app import create_app
    from memory_agent.interfaces.http.local_auth import read_session_token_from_env

    host = str(args.host)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print(json.dumps({
            "status": "error",
            "error": {"code": "INVALID_REQUEST",
                       "message": f"loopback UI cannot bind to {host!r}"},
        }))
        return EXIT_INVALID
    port = int(args.port)
    if port <= 0 or port > 65535:
        print(json.dumps({"status": "error", "error": {"code": "INVALID_REQUEST",
                                                          "message": f"invalid port {port}"}}))
        return EXIT_INVALID

    ui_dist = (
        Path(args.ui_dist).expanduser().resolve()
        if args.ui_dist
        else _resolve_default_ui_dist()
    )
    if ui_dist is None:
        sys.stderr.write(
            "memory ui: web/dist not built; run `npm run build` under web/ "
            "or pass --ui-dist <path>. Falling back to the placeholder page.\n"
        )

    env_token = read_session_token_from_env()
    session_token = args.session_token if args.session_token else env_token
    if args.no_token:
        session_token = None

    http_app = create_app(
        vault_root=vault_root,
        bind_host=host,
        port=port,
        ui_dist=ui_dist,
    )
    if session_token is not None:
        http_app.auth.session_token = session_token
        http_app.app.state.auth.session_token = session_token
    if args.no_token:
        http_app.auth.write_methods_enabled = False
        http_app.app.state.auth.write_methods_enabled = False

    try:
        import uvicorn  # type: ignore[import-not-found]
    except ImportError:
        print(json.dumps({"status": "error", "error": {"code": "INTERNAL",
                                                          "message": "uvicorn is required for `memory ui`"}}))
        return EXIT_INTERNAL

    sys.stderr.write(
        f"memory ui: serving on http://{host}:{port}/  "
        f"(token configured: {bool(session_token)}, "
        f"writes enabled: {http_app.auth.write_methods_enabled}, "
        f"ui_dist: {ui_dist})\n"
    )
    config = uvicorn.Config(http_app.app, host=host, port=port, log_level="info")
    if args.reload:
        config.reload = True
    server = uvicorn.Server(config)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    return EXIT_OK


def _cmd_backup(args: argparse.Namespace, vault_root: Path) -> int:
    """``memory backup create|list|restore`` (P9 / T17).

    Routes through ``handle_call`` so the call dispatcher is the
    single writer of the privacy / backup state and the audit
    trail is consistent with the ``memory call`` path.
    """
    import uuid as _uuid
    sub = args.backup_command
    rid = f"req_cli_backup_{sub}_{_uuid.uuid4().hex[:8]}"
    if sub == "create":
        payload = {
            "api_version": 1,
            "request_id": rid,
            "operation": "backup.create",
            "mode": "sync",
            "params": {
                "output": str(Path(args.output).expanduser().resolve()),
                "format": args.format,
                "label": args.label,
            },
        }
    elif sub == "list":
        payload = {
            "api_version": 1,
            "request_id": rid,
            "operation": "backup.list",
            "mode": "sync",
            "params": {
                "bundle": str(Path(args.bundle).expanduser().resolve()),
            },
        }
    elif sub == "restore":
        # Privacy safety: the live vault's privacy ledger is the
        # only trust root. There is no ``--live-privacy-head-version``
        # (caller-supplied integers cannot stand in for a manifest)
        # and no ``--no-quarantine`` (refusing to quarantine an old
        # bundle is exactly how forgotten content gets resurrected).
        payload = {
            "api_version": 1,
            "request_id": rid,
            "operation": "backup.restore",
            "mode": "sync",
            "params": {
                "bundle": str(Path(args.bundle).expanduser().resolve()),
                "target": str(Path(args.target).expanduser().resolve()),
            },
        }
    else:  # pragma: no cover
        print(json.dumps(_error_dict("INVALID_REQUEST",
                                     f"unknown backup subcommand {sub!r}")))
        return EXIT_INVALID
    outcome = handle_call(request=payload, vault_root=vault_root)
    return _write_call_outcome(outcome)


def _cmd_privacy(args: argparse.Namespace, vault_root: Path) -> int:
    """``memory privacy forget|list|receipt|unblock`` (P9 / T17).

    Routes through ``handle_call`` so the call dispatcher is the
    single writer of privacy state.
    """
    import uuid as _uuid
    sub = args.privacy_command
    rid = f"req_cli_privacy_{sub}_{_uuid.uuid4().hex[:8]}"
    if sub == "forget":
        payload = {
            "api_version": 1,
            "request_id": rid,
            "operation": "privacy.forget",
            "mode": "sync",
            "params": {
                "kind": args.kind,
                "targets": list(args.target),
                "idempotency_key": args.idempotency_key,
                "note": args.note,
            },
        }
    elif sub == "list":
        payload = {
            "api_version": 1,
            "request_id": rid,
            "operation": "privacy.list",
            "mode": "sync",
            "params": {
                "kind": args.kind,
                "limit": int(args.limit),
            },
        }
    elif sub == "receipt":
        payload = {
            "api_version": 1,
            "request_id": rid,
            "operation": "privacy.receipt",
            "mode": "sync",
            "params": {"op_id": args.op_id},
        }
    elif sub == "unblock":
        payload = {
            "api_version": 1,
            "request_id": rid,
            "operation": "privacy.unblock",
            "mode": "sync",
            "params": {
                "op_id": args.op_id,
                "idempotency_key": args.idempotency_key,
            },
        }
    else:  # pragma: no cover
        print(json.dumps(_error_dict("INVALID_REQUEST",
                                     f"unknown privacy subcommand {sub!r}")))
        return EXIT_INVALID
    outcome = handle_call(request=payload, vault_root=vault_root)
    return _write_call_outcome(outcome)


def _resolve_default_ui_dist() -> Path | None:
    """Locate the bundled UI ``dist`` directory.

    Resolution order:

    1. ``<repo>/web/dist`` — the developer checkout (Vite's default
       output location). Used when ``memory_agent`` is being run from
       a working tree that has the frontend source alongside it.
    2. The packaged ``memory_agent.ui.dist`` resource — read via
       :mod:`importlib.resources`. This is what a clean wheel
       install (no developer checkout) resolves to. The wheel
       ships the Vite output via ``[tool.setuptools.package-data]``
       in ``pyproject.toml``.

    Returns ``None`` only when neither candidate is present — the
    FastAPI app then falls back to its built-in placeholder page.
    """

    # 1. Developer checkout.
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "web" / "dist"
        if candidate.exists() and candidate.is_dir():
            return candidate

    # 2. Packaged resource shipped inside the wheel.
    packaged = _packaged_ui_dist_path()
    if packaged is not None:
        return packaged

    return None


def _packaged_ui_dist_path() -> Path | None:
    """Resolve ``memory_agent.ui.dist`` as a real ``Path``.

    Uses :mod:`importlib.resources` so the lookup works from a wheel
    installed by ``pip`` into any site-packages. The wheel ships the
    Vite output via ``[tool.setuptools.package-data]`` in
    ``pyproject.toml``.

    Returns ``None`` if the packaged dist is missing or the resolved
    location is not a directory on disk with an ``index.html`` (the
    the caller then falls back to the placeholder page).
    """

    from importlib.resources import files

    try:
        traversable = files("memory_agent.ui").joinpath("dist")
    except (ModuleNotFoundError, FileNotFoundError, AttributeError):
        return None
    try:
        path = Path(str(traversable))
    except (TypeError, ValueError):
        return None
    if not path.is_dir():
        return None
    if not (path / "index.html").is_file():
        return None
    return path


def _read_request_payload(args: argparse.Namespace) -> str:
    path = args.request_file
    if path is None or path == "-":
        return sys.stdin.read()
    return Path(path).expanduser().read_text(encoding="utf-8")


def _write_call_outcome(outcome: CallOutcome) -> int:
    response = outcome.response
    if getattr(_build_parser_namespace(), "pretty", False):
        sys.stdout.write(json.dumps(response, ensure_ascii=False, default=str, indent=2))
    else:
        sys.stdout.write(json.dumps(response, ensure_ascii=False, default=str))
    sys.stdout.write("\n")
    if response.get("status") == "error":
        code = (response.get("error") or {}).get("code", "INTERNAL")
        return _exit_code_for(code)
    return EXIT_OK


def _build_parser_namespace() -> argparse.Namespace:
    """Cheap accessor for module-global argparse state (--pretty)."""
    return getattr(main, "_last_namespace", argparse.Namespace(pretty=False))


def _error_dict(code: str, message: str) -> dict[str, Any]:
    return {
        "api_version": 1,
        "operation": "call",
        "status": "error",
        "error": {"code": code, "message": message},
    }


def _legacy_dispatch(
    *,
    operation: str,
    params: dict[str, Any],
    request_id: str | None,
    idempotency_key: str | None,
    vault_root: Path,
) -> dict[str, Any]:
    from memory_agent.application.dispatch import dispatch
    return dispatch(
        operation=operation,
        params=params,
        request_id=request_id,
        idempotency_key=idempotency_key,
        vault_root=vault_root,
    )


def _op_name(command: str) -> str:
    return {
        "init": "init",
        "scan": "scan",
        "batch.plan": "batch.plan",
        "batch.process": "batch.process",
        "maintain": "maintain",
        "recall": "recall",
        "conflicts": "conflicts",
        "decide": "decide",
        "doctor": "doctor",
    }[command]


def _params_from_args(args: argparse.Namespace) -> dict[str, Any]:
    cmd = args.command
    if cmd == "recall":
        return {
            "query": args.query,
            "project_ids": list(args.project_id),
            "domain_ids": list(args.domain_id),
            "limit": args.limit,
            "retrieval": args.retrieval,
            "rewrite": args.rewrite,
            "expand": args.expand,
            "cursor": args.cursor,
            "retrieval_limit": args.retrieval_limit,
            "audit": not bool(getattr(args, "no_audit", False)),
        }
    if cmd == "scan":
        params: dict[str, Any] = {"mode": args.mode}
        if getattr(args, "since", None):
            params["since_iso"] = args.since
        if getattr(args, "until", None):
            params["until_iso"] = args.until
        if getattr(args, "timezone", None):
            params["timezone"] = args.timezone
        return params
    if cmd == "batch.plan":
        return {
            "session_id": args.session_id,
            "branch_id": args.branch_id,
            "max_turns_per_batch": args.max_turns,
            "max_input_tokens": args.max_input_tokens,
        }
    if cmd == "batch.process":
        return {
            "batch_ids": list(args.batch_id),
            "max_batches": args.max_batches,
        }
    if cmd == "maintain":
        return {"max_batches": args.max_batches}
    if cmd == "decide":
        return {
            "conflict_id": args.conflict_id,
            "proposal_revision": args.proposal_revision,
            "action": args.action,
            "note": args.note,
            "idempotency_key": args.idempotency_key,
        }
    return {}


def _parse_roots(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _exit_code_for(code: str) -> int:
    return {
        "INVALID_REQUEST": EXIT_INVALID,
        "INVALID_VERSION": EXIT_INVALID,
        "UNKNOWN_OPERATION": EXIT_INVALID,
        "NOT_FOUND": EXIT_INVALID,
        "IDEMPOTENCY_CONFLICT": EXIT_IDEMPOTENCY,
        "SOURCE_UNAVAILABLE": EXIT_UNAVAILABLE,
        "PROVIDER_UNAVAILABLE": EXIT_UNAVAILABLE,
        "WORKER_OFFLINE": EXIT_UNAVAILABLE,
        "BUSY": EXIT_PARTIAL,
        "PARTIAL": EXIT_PARTIAL,
        "LEASE_EXPIRED": EXIT_IDEMPOTENCY,
        "INTERNAL": EXIT_INTERNAL,
        "RECOVERY_REQUIRED": EXIT_INTERNAL,
        "UNSUPPORTED": EXIT_INTERNAL,
    }.get(code, EXIT_INTERNAL)


if __name__ == "__main__":
    raise SystemExit(main())