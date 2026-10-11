"""FastAPI app factory (T14).

Wires the loopback HTTP service together:

  * Audit read routes (``GET /api/v1/...``) — no auth beyond loopback
  * Command write routes (``POST /api/v1/commands``,
    ``GET /api/v1/jobs/{id}``) — Origin allowlist + session token
  * Static UI assets served from the packaged ``ui-dist`` directory
    when present, with a tiny placeholder page fallback so a freshly
    installed ``memory ui`` does not 404 on ``/``

The server ONLY binds to 127.0.0.1 (loopback). The ``uvicorn`` runner
in ``main.py`` rejects any non-loopback bind; ``create_app`` accepts
a ``bind_host`` argument so tests can pin a per-vault port and the
production code path always passes ``127.0.0.1``.

The factory is intentionally side-effect free — no DB connection is
opened at construction time, so unit tests can build an app against
an empty vault and only the routes that hit the DB will fail.

``Config`` keys:

  * ``memory_agent.session_token`` — shared secret for write auth.
    Falls back to ``MEMORY_AGENT_HTTP_TOKEN``.
  * ``memory_agent.allowed_origins`` — list of accepted ``Origin``
    headers (no wildcard).
  * ``memory_agent.write_methods_enabled`` — bool, default True.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response

from memory_agent.application.audit_queries import AuditQueries
from memory_agent.contracts.audit import AUDIT_SCHEMA_VERSION
from memory_agent.infrastructure.files import VaultLayout
from memory_agent.infrastructure.sqlite import open_state_db
from memory_agent.interfaces.http.audit_routes import router as audit_router
from memory_agent.interfaces.http.audit_routes import root_router as root_page_router
from memory_agent.interfaces.http.audit_routes import serve_ui_dist
from memory_agent.interfaces.http.command_routes import router as command_router
from memory_agent.interfaces.http.local_auth import (
    LocalAuthConfig,
    load_auth_config,
    read_session_token_from_env,
)
from memory_agent import __version__


# Default loopback UI port. 8440 is chosen to stay clear of the local
# Octopus controller (which occupies 8420 on this machine); operators
# can still override via `memory ui --port`. See docs/22-v1-p7-p8.
DEFAULT_PORT = 8440


@dataclass
class HttpApp:
    """The assembled app + the resources tests need to introspect.

    ``app`` is the FastAPI instance; ``vault`` is the resolved
    :class:`VaultLayout`; ``audit`` is the audit service bound to the
    vault; ``auth`` is the auth config; ``port`` is the loopback
    port the server should bind to (always 127.0.0.1).
    """

    app: FastAPI
    vault: VaultLayout
    audit: AuditQueries
    auth: LocalAuthConfig
    port: int
    ui_dist: Path | None = None


def create_app(
    *,
    vault_root: Path,
    config: dict[str, Any] | None = None,
    bind_host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    ui_dist: Path | None = None,
    auth_config: LocalAuthConfig | None = None,
) -> HttpApp:
    """Build a FastAPI app bound to the loopback vault.

    The ``bind_host`` argument is **only** allowed to be ``"127.0.0.1"``
    or a loopback alias. Anything else raises ``ValueError`` so the
    server cannot accidentally listen on a public interface.
    """

    if bind_host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(
            f"loopback HTTP server cannot bind to {bind_host!r}; "
            "use 127.0.0.1 / localhost / ::1 only"
        )

    vault = VaultLayout(vault_root)
    audit = AuditQueries(vault=vault)
    if auth_config is None:
        auth_config = load_auth_config(
            config=config,
            env_token=read_session_token_from_env(),
            port=port,
        )
    # Always include the bound loopback origin in the allowlist.
    loopback_origin = f"http://127.0.0.1:{port}"
    if loopback_origin not in auth_config.allowed_origins:
        auth_config.allowed_origins.append(loopback_origin)

    app = FastAPI(
        title="memory-agent ui",
        version=__version__,
        docs_url="/api/v1/docs",
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
    )
    app.state.vault = vault
    app.state.audit = audit
    app.state.auth = auth_config
    app.state.port = port

    app.include_router(audit_router)
    app.include_router(command_router)

    # ``/healthz`` must be registered BEFORE the SPA catch-all so the
    # catch-all's ``{full_path:path}`` does not shadow it. The order is
    # load-bearing: FastAPI/Starlette routes are matched in registration
    # order, not by specificity.
    #
    # The default response is a 4-field liveness ping — cheap enough
    # that any CI / k8s probe can hammer it. Add ``?verbose=true`` and
    # the endpoint also reports the privacy HEAD version, the worker
    # lease state, last scan / commit timestamps, conflict + memory
    # counts, the projection_state and the queued-jobs counter. The
    # verbose path consults the audit service (which already owns
    # these reads) plus the live privacy ledger; failures degrade to
    # ``None`` / ``"unavailable"`` so a verbose probe never fails the
    # whole endpoint just because one downstream read blew up.
    @app.get("/healthz", include_in_schema=False)
    def healthz(verbose: bool = False) -> dict[str, Any]:
        base = {
            "ok": True,
            "service": "memory-agent-ui",
            "loopback_only": True,
            "write_methods_enabled": auth_config.write_methods_enabled,
            "session_token_configured": bool(auth_config.session_token),
        }
        if not verbose:
            return base

        # Verbose mode. Cheap, single round-trip per downstream.
        from memory_agent.application.backup import (
            BackupError,
            read_live_privacy_ledger,
        )
        from memory_agent.infrastructure.sqlite.repositories import JobRepo

        verbose_block: dict[str, Any] = {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "projection_state": "unavailable",
            "privacy_head_version": None,
            "privacy_head_empty": None,
            "worker_lease_active": False,
            "queued_jobs": 0,
            "running_jobs": 0,
            "failed_jobs": 0,
            "last_scan_at": None,
            "last_commit_at": None,
            "memories_active": 0,
            "conflicts_open": 0,
            "degraded_reasons": [],
        }

        # Audit overview — single service call, returns counts + header.
        if vault.state_db.exists():
            try:
                conn = open_state_db(vault.state_db)
            except Exception as exc:  # pragma: no cover — best-effort probe
                verbose_block["degraded_reasons"].append(
                    f"state_db_open:{type(exc).__name__}"
                )
            else:
                try:
                    overview = audit.overview(conn=conn)
                    verbose_block["projection_state"] = overview.header.projection_state
                    verbose_block["worker_lease_active"] = overview.counts.worker_lease_active
                    verbose_block["last_scan_at"] = overview.counts.last_scan_at
                    verbose_block["last_commit_at"] = overview.counts.last_commit_at
                    verbose_block["memories_active"] = overview.counts.memories_active
                    verbose_block["conflicts_open"] = overview.counts.conflicts_open
                    if overview.warnings:
                        verbose_block["degraded_reasons"].extend(overview.warnings)
                    # Queued / running / failed jobs counts ride on the
                    # same connection so they share one round-trip.
                    job_repo = JobRepo(conn)
                    verbose_block["queued_jobs"] = job_repo.count_by_state("queued")
                    verbose_block["running_jobs"] = job_repo.count_by_state("running")
                    verbose_block["failed_jobs"] = job_repo.count_by_state("failed")
                except Exception as exc:  # pragma: no cover — best-effort probe
                    verbose_block["degraded_reasons"].append(
                        f"audit_overview:{type(exc).__name__}"
                    )
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
        else:
            verbose_block["degraded_reasons"].append("vault_not_initialised")

        # Live privacy HEAD version — fail closed if it cannot be read.
        try:
            ledger = read_live_privacy_ledger(vault)
            verbose_block["privacy_head_version"] = ledger.version
            verbose_block["privacy_head_empty"] = ledger.is_empty
        except BackupError as exc:
            verbose_block["degraded_reasons"].append(
                f"privacy_ledger:{type(exc).__name__}"
            )
        except Exception as exc:  # pragma: no cover — best-effort probe
            verbose_block["degraded_reasons"].append(
                f"privacy_ledger:{type(exc).__name__}"
            )

        return {**base, "verbose": verbose_block}

    static_app = serve_ui_dist(ui_dist)
    if static_app is not None:
        # The Vite build puts every asset under ``/assets/<hash>.{js,css}``.
        # Register the API routers first so the audit namespace wins;
        # then mount the static dist + add SPA fall-through routes.
        from starlette.staticfiles import StaticFiles

        assets_dir = ui_dist / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="ui-assets")

        @app.get("/", include_in_schema=False)
        def _serve_spa_root() -> FileResponse:
            return FileResponse(str(ui_dist / "index.html"), media_type="text/html")

        @app.get("/{full_path:path}", include_in_schema=False)
        def _serve_spa_path(full_path: str) -> Response:
            # Anything matching the API namespace was already routed
            # above; this catch-all only fires for the SPA's HTML5
            # history routes (``/sources``, ``/batches``, …).
            return FileResponse(str(ui_dist / "index.html"), media_type="text/html")
    else:
        # No built UI — keep the placeholder ``/`` route from the
        # audit task so a freshly-installed ``memory ui`` still has a
        # useful response on ``/``.
        app.include_router(root_page_router)

    return HttpApp(
        app=app,
        vault=vault,
        audit=audit,
        auth=auth_config,
        port=port,
        ui_dist=ui_dist,
    )


__all__ = ["HttpApp", "create_app", "DEFAULT_PORT"]