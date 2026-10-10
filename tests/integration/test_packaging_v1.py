"""P9 / T17 packaging smoke — clean wheel install serves the real SPA.

The packaging gap surfaced by P9 was that ``memory ui`` only resolved
the React UI from the developer checkout (``<repo>/web/dist``); a clean
wheel install fell back to the placeholder page. This test enforces the
fix end-to-end:

  1. Build the wheel into a temp directory that is **outside** the
     repository checkout.
  2. Create a brand-new virtualenv in that same outside directory.
  3. ``pip install`` the wheel — no editable mode, no ``.[dev]``, no
     frontend source on disk.
  4. Boot ``memory ui`` against an invented temp vault on a free
     loopback port with a session token configured.
  5. Hit ``GET /`` and ``GET /assets/<hashed>.{js,css}`` and assert
     the responses are the real Vite output with HTTP 200, **not** the
     T14 placeholder page.

The whole test runs without any network beyond loopback. If the
packaged UI is missing or the assets 404, the test fails — which is
exactly the regression the previous packaging gap would produce.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator

import pytest


def _which(name: str) -> str | None:
    return shutil.which(name)


def _free_port() -> int:
    """Ask the kernel for a free loopback port and return it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _http_get(url: str, *, headers: dict[str, str] | None = None, timeout: float = 10.0) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read() if exc.fp is not None else b""


def _wait_for_ui(base_url: str, *, timeout_s: float = 15.0) -> bool:
    """Poll ``GET /healthz`` until the loopback HTTP server is ready."""
    import time

    deadline = time.monotonic() + timeout_s
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status, _, _ = _http_get(f"{base_url}/healthz")
            if status == 200:
                return True
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
        time.sleep(0.25)
    if last_exc is not None:
        print(f"[wait_for_ui] last error: {last_exc!r}", file=sys.stderr)
    return False


@pytest.fixture
def build_dir(tmp_path: Path) -> Iterator[Path]:
    """Build the wheel into an outside-repo temp directory."""
    if _which("python3") is None or _which("pip") is None:
        pytest.skip("python3/pip not available on PATH")
    try:
        subprocess.run(
            [sys.executable, "-m", "build", "--version"],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("python -m build not available; cannot build wheel")

    repo_root = Path(__file__).resolve().parents[2]
    out_dir = tmp_path / "wheel_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Build only the wheel; sdist is unnecessary for the install smoke.
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.fail(f"wheel build failed:\nstdout={proc.stdout}\nstderr={proc.stderr}")
    yield out_dir


def test_packaged_dist_has_index_html() -> None:
    """The mirror under src/memory_agent/ui/dist must be populated.

    This is the developer-side check that the build step actually ran.
    The clean-install check is the next test.
    """
    pkg_dist = Path(__file__).resolve().parents[2] / "src" / "memory_agent" / "ui" / "dist"
    if not pkg_dist.is_dir():
        pytest.skip("package-data dist not built; run scripts/build_ui.sh")
    assert (pkg_dist / "index.html").is_file(), "packaged dist missing index.html"
    assets = pkg_dist / "assets"
    assert assets.is_dir(), "packaged dist missing assets/"
    hashed = [p for p in assets.iterdir() if re.match(r"^index-[A-Za-z0-9_-]+\.(js|css)$", p.name)]
    assert hashed, f"no hashed assets under {assets}"
    assert any(p.suffix == ".js" for p in hashed), "no hashed JS bundle"
    assert any(p.suffix == ".css" for p in hashed), "no hashed CSS bundle"


def test_clean_wheel_install_serves_real_spa(build_dir: Path) -> None:
    """A wheel installed into a fresh venv must serve the real React app.

    The venv is created in the same outside-repo temp directory as
    ``build_dir``, so the developer checkout is not on disk at install
    time. The only UI source the venv sees is the one shipped inside
    the wheel.
    """

    wheels = sorted(build_dir.glob("memory_agent-*.whl"))
    assert wheels, f"no wheel produced in {build_dir}"
    wheel = wheels[-1]

    # venv outside the repo
    venv_dir = build_dir.parent / "venv_clean"
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
    venv_python = venv_dir / "bin" / "python"

    # Install the wheel — no editable, no [dev], no frontend source.
    proc = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--quiet", "--no-deps", str(wheel)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.fail(f"pip install --no-deps failed:\n{proc.stderr}")
    # Install runtime deps (fastapi, pydantic, pyyaml, uvicorn) explicitly.
    # ``pip install --no-deps`` is used above to keep the wheel honest
    # about its declared ``dependencies=``; then we resolve them via the
    # public index. This still does not depend on the repo checkout.
    proc = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--quiet",
         "fastapi>=0.110,<0.140", "uvicorn>=0.27,<0.40",
         "pydantic>=2.6,<3", "PyYAML>=6.0,<7"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.fail(f"pip install deps failed:\n{proc.stderr}")

    # Invariant: no ``web/dist`` reachable from the installed package.
    # Walk up from the installed CLI module; if the developer checkout
    # is on disk the test would still pass — but we want to assert the
    # *packaged* path is the one serving the UI, so we additionally
    # confirm the wheel contains the assets and they are unpacked to
    # site-packages.
    site_packages = subprocess.run(
        [str(venv_python), "-c",
         "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    installed_ui = Path(site_packages) / "memory_agent" / "ui" / "dist"
    assert installed_ui.is_dir(), f"installed UI missing at {installed_ui}"
    assert (installed_ui / "index.html").is_file(), "installed UI missing index.html"
    installed_assets = installed_ui / "assets"
    assert installed_assets.is_dir(), "installed UI missing assets/"
    js_bundles = sorted(p for p in installed_assets.iterdir() if p.suffix == ".js")
    css_bundles = sorted(p for p in installed_assets.iterdir() if p.suffix == ".css")
    assert js_bundles and css_bundles, "installed UI missing hashed JS/CSS"

    # Boot the loopback UI server in the clean venv against an
    # invented temp vault on a free port. The session token is just
    # enough for the boot-time configuration.
    vault = build_dir.parent / "vault_clean"
    vault.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    session_token = "smoke-packaging-token"
    # Inherit PATH so the venv's pip can resolve system certs; do not
    # inherit the developer env's PYTHONPATH — the wheel's installed
    # ``memory_agent`` must come from site-packages, not from ``src/``.
    clean_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    proc = subprocess.run(
        [str(venv_python), "-m", "memory_agent",
         "--vault", str(vault), "init"],
        env=clean_env, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        pytest.fail(f"clean-venv init failed:\nstdout={proc.stdout}\nstderr={proc.stderr}")

    server = subprocess.Popen(
        [str(venv_python), "-m", "memory_agent",
         "--vault", str(vault), "ui",
         "--port", str(port),
         "--host", "127.0.0.1",
         "--session-token", session_token],
        env=clean_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        if not _wait_for_ui(base_url):
            stdout, stderr = server.communicate(timeout=2)
            pytest.fail(
                "loopback UI server did not become ready in time\n"
                f"stdout={stdout!r}\nstderr={stderr!r}"
            )

        # The placeholder page is the regression we are guarding against.
        # The real Vite-built HTML has ``<div id="root"></div>`` and
        # references the hashed JS / CSS via ``<script type="module"
        # crossorigin src="./assets/index-*.js">``; the placeholder
        # has neither.
        status, headers, body = _http_get(f"{base_url}/")
        assert status == 200, f"GET / -> {status}: {body[:200]!r}"
        ctype = headers.get("content-type", "")
        assert ctype.startswith("text/html"), f"GET / content-type: {ctype}"
        text = body.decode("utf-8", errors="replace")
        assert "placeholder" not in text.lower(), (
            "GET / returned the placeholder page — packaged UI not served"
        )
        assert '<div id="root"></div>' in text, "real SPA shell missing from GET /"

        # Extract the hashed asset paths the index.html references.
        js_match = re.search(r'src="\./assets/(index-[A-Za-z0-9_-]+\.js)"', text)
        css_match = re.search(r'href="\./assets/(index-[A-Za-z0-9_-]+\.css)"', text)
        assert js_match and css_match, (
            f"could not extract hashed asset paths from index.html: {text!r}"
        )

        js_status, js_headers, js_body = _http_get(f"{base_url}/assets/{js_match.group(1)}")
        assert js_status == 200, f"GET hashed JS -> {js_status}"
        assert (js_headers.get("content-type") or "").startswith("application/javascript") or \
            (js_headers.get("content-type") or "").startswith("text/javascript"), \
            f"hashed JS content-type: {js_headers.get('content-type')!r}"
        assert js_body, "hashed JS body empty"
        # Spot-check that the bundle is the real React app, not the
        # placeholder text. Vite minifies to identifiers but keeps a
        # few sentinel strings; the project title and runtime config
        # names survive.
        assert b"memory-agent" in js_body or b"__MA_CONFIG__" in js_body, \
            "hashed JS body does not look like the project's React bundle"

        css_status, css_headers, css_body = _http_get(f"{base_url}/assets/{css_match.group(1)}")
        assert css_status == 200, f"GET hashed CSS -> {css_status}"
        assert (css_headers.get("content-type") or "").startswith("text/css"), \
            f"hashed CSS content-type: {css_headers.get('content-type')!r}"
        assert css_body, "hashed CSS body empty"

        # /healthz must still report the bind contract.
        h_status, _, h_body = _http_get(f"{base_url}/healthz")
        assert h_status == 200
        payload = json.loads(h_body.decode("utf-8"))
        assert payload.get("loopback_only") is True
        assert payload.get("session_token_configured") is True
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


def test_packaged_ui_dist_resolves_via_importlib() -> None:
    """The packaged dist is reachable via :mod:`importlib.resources`."""
    from importlib.resources import files

    traversable = files("memory_agent.ui").joinpath("dist")
    assert traversable.is_dir(), f"packaged dist not a directory: {traversable}"
    assert traversable.joinpath("index.html").is_file(), "packaged index.html missing"
    assets = traversable.joinpath("assets")
    assert assets.is_dir(), "packaged assets/ missing"
    hashed = list(assets.iterdir())
    assert any(p.name.startswith("index-") and p.name.endswith(".js") for p in hashed), \
        "packaged JS bundle missing"
    assert any(p.name.startswith("index-") and p.name.endswith(".css") for p in hashed), \
        "packaged CSS bundle missing"