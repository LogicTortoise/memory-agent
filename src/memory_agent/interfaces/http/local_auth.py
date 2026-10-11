"""Local-auth middleware for the loopback HTTP API (T14).

Auth model:

  * Read methods (``GET``) do not require any auth beyond the loopback
    bind (the server only listens on 127.0.0.1). This matches docs/10:
    "默认 ``memory ui`` 绑定 127.0.0.1，端口可配置" — the OS-level
    loopback is the only auth the read path needs.
  * Write methods (``POST /api/v1/commands``, ``POST /api/v1/jobs/.../...``)
    require both:

      1. **Origin allowlist.** The ``Origin`` header MUST match the
         configured ``allowed_origins`` set *exactly* (case-insensitive
         scheme/host, port-sensitive). A missing ``Origin`` header on a
         browser-driven POST is rejected (curl can opt in by sending a
         matching origin). A wildcard ``*`` is never accepted; wildcards
         in CORS are an explicit docs/10 no-go.
      2. **Session token.** A ``X-Memory-Agent-Token`` header MUST
         equal the token the server was started with. Tokens are read
         from the config file (``memory_agent.session_token``) or the
         ``MEMORY_AGENT_HTTP_TOKEN`` environment variable. The token
         is **never** placed in a URL or echoed into a log line; only
         a constant-prefixed fingerprint is logged on mismatch.

The middleware is intentionally stateless. It does NOT enforce CSRF
tokens because the loopback bind + Origin allowlist are the only
secrets. There is no in-memory session state.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Iterable

from fastapi import HTTPException, Request, status


@dataclass
class LocalAuthConfig:
    """Loopback HTTP auth config.

    ``allowed_origins`` is the *exact* set of acceptable ``Origin``
    values for write methods. The default is a single entry —
    ``http://127.0.0.1:<port>`` — and ``app.py`` extends it with the
    actual port at startup. The set is empty when the operator opts
    in to "POST disabled".

    ``session_token`` is the shared secret clients must echo in the
    ``X-Memory-Agent-Token`` header. When ``None``, the server
    refuses all writes with ``503 Service Unavailable`` until a token
    is configured.
    """

    allowed_origins: list[str] = field(default_factory=list)
    session_token: str | None = None
    # Default port for the loopback audit UI HTTP service. 8440 is chosen
    # to stay clear of the local Octopus controller (which occupies 8420
    # on this machine); the operator can still override with
    # ``--ui-port`` / ``memory ui --port``. See docs/22-v1-p7-p8.
    bind_port: int = 8440
    write_methods_enabled: bool = True

    def origin_allowed(self, origin: str | None) -> bool:
        if not self.write_methods_enabled:
            return False
        if origin is None:
            return False
        o = origin.strip().lower()
        return any(o == a.strip().lower() for a in self.allowed_origins)


def load_auth_config(
    *,
    config: dict[str, object] | None = None,
    env_token: str | None = None,
    port: int = 8440,
) -> LocalAuthConfig:
    """Build a :class:`LocalAuthConfig` from ``config.toml`` + env.

    Keys recognised:

      * ``memory_agent.session_token``: shared secret (overridden by
        ``MEMORY_AGENT_HTTP_TOKEN`` if set, so tests can swap the
        token without rewriting the config).
      * ``memory_agent.allowed_origins``: list of strings; default
        ``["http://127.0.0.1:<port>"]``.
      * ``memory_agent.write_methods_enabled``: bool, default True.
    """

    cfg = dict(config or {})
    raw_token = cfg.get("session_token")
    token: str | None = (
        env_token
        if env_token is not None
        else (raw_token if isinstance(raw_token, str) else None)
    )
    allowed = cfg.get("allowed_origins")
    if isinstance(allowed, list) and all(isinstance(o, str) for o in allowed):
        origins = [str(o) for o in allowed]
    else:
        origins = [f"http://127.0.0.1:{port}"]
    write_enabled = bool(cfg.get("write_methods_enabled", True))
    return LocalAuthConfig(
        allowed_origins=origins,
        session_token=token,
        bind_port=int(port),
        write_methods_enabled=write_enabled,
    )


def read_session_token_from_env() -> str | None:
    """Read the operator-supplied session token from the environment.

    ``None`` means "no token configured"; the server must refuse all
    writes in that case.
    """

    raw = os.environ.get("MEMORY_AGENT_HTTP_TOKEN")
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def _token_fingerprint(token: str | None) -> str:
    """Stable, non-reversible fingerprint for log lines.

    Avoids logging the actual token value (the secret must never leak
    into stderr / journal / crash dumps).
    """

    if not token:
        return "<none>"
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def token_fingerprint(token: str | None) -> str:
    """Public helper so the HTTP layer can log mismatches without
    leaking the secret.
    """

    return _token_fingerprint(token)


async def enforce_write_auth(request: Request) -> None:
    """FastAPI dependency enforcing Origin + session token on writes.

    Raises ``HTTPException`` (401 / 403 / 503) on failure. The
    dependency does not read the request body, so it cannot be
    confused by a malformed body — body validation happens after.
    """

    cfg: LocalAuthConfig | None = getattr(request.app.state, "auth", None)
    if cfg is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="auth not configured",
        )
    if not cfg.write_methods_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="write methods disabled",
        )
    if not cfg.session_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="session_token not configured",
        )
    origin = request.headers.get("origin") or request.headers.get("Origin")
    if not cfg.origin_allowed(origin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"origin not allowed: {origin!r}",
        )
    presented = request.headers.get("X-Memory-Agent-Token")
    if presented is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing X-Memory-Agent-Token header",
        )
    if not _safe_compare(presented, cfg.session_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid session token",
        )


def _safe_compare(a: str, b: str) -> bool:
    """Constant-time-ish string compare.

    ``hmac.compare_digest`` is the standard helper for secret compares;
    using ``==`` would leak a timing oracle on each byte. We do not
    need cryptographic strength — just a stable compare that does not
    amplify byte-by-byte into measurable time.
    """

    import hmac
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


__all__ = [
    "LocalAuthConfig",
    "enforce_write_auth",
    "load_auth_config",
    "read_session_token_from_env",
    "token_fingerprint",
]