#!/usr/bin/env bash
# build_ui.sh — rebuild the bundled React UI assets for the Python wheel.
#
# Reproducible build:
#   * Runs `npm run build` under web/ (which is `tsc --noEmit && vite build`)
#   * Mirrors web/dist into src/memory_agent/ui/dist so the hashed
#     assets ship as package data and `memory ui` can serve the real
#     SPA from a clean wheel install with no developer checkout.
#
# Re-run this script after editing anything under web/src. The output
# is content-hashed, so a fresh build produces new filenames and the
# old ones are cleaned up.
#
# The script is idempotent: re-running it after a no-op frontend change
# still produces the same dist tree (Vite is deterministic) and the
# mirror step replaces the entire target directory.
#
# No network beyond `npm install` (if node_modules is missing).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB_DIR="${REPO_ROOT}/web"
PACKAGE_DIST="${REPO_ROOT}/src/memory_agent/ui/dist"

if [[ ! -d "${WEB_DIR}" ]]; then
    echo "[build_ui] web/ directory missing; abort." >&2
    exit 1
fi

if [[ ! -d "${WEB_DIR}/node_modules" ]]; then
    echo "[build_ui] installing npm dependencies (no node_modules)"
    (cd "${WEB_DIR}" && npm install)
fi

echo "[build_ui] running vite build"
(cd "${WEB_DIR}" && npm run build)

if [[ ! -f "${WEB_DIR}/dist/index.html" ]]; then
    echo "[build_ui] web/dist/index.html missing after build; abort." >&2
    exit 1
fi

echo "[build_ui] mirroring web/dist -> ${PACKAGE_DIST}"
rm -rf "${PACKAGE_DIST}"
mkdir -p "${PACKAGE_DIST}"
cp -R "${WEB_DIR}/dist/." "${PACKAGE_DIST}/"

echo "[build_ui] done. bundled files:"
find "${PACKAGE_DIST}" -type f -print | sort