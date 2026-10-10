#!/usr/bin/env bash
# uninstall.sh — remove the memory-agent package from the active
# environment.
#
# The vault is *operator-owned* personal data; the uninstaller does
# NOT delete it. To delete the vault, run
# ``rm -rf <vault>`` yourself after uninstalling.
#
# What this script removes:
#   * the editable install of memory-agent
#   * the venv at <repo>/.venv (override with VENV=...)
#
# What this script preserves:
#   * the vault directory (any path the operator points to)
#   * source-cached wheels under <repo>/.cache/pip
#   * the launchd template (operator must ``launchctl unload`` it)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${VENV:-${REPO_ROOT}/.venv}"

if [[ -d "${VENV}" ]]; then
    # shellcheck disable=SC1091
    source "${VENV}/bin/activate"
    pip uninstall -y memory-agent >/dev/null 2>&1 || true
    echo "[uninstall] removed editable install"
    rm -rf "${VENV}"
    echo "[uninstall] removed ${VENV}"
else
    echo "[uninstall] no venv at ${VENV}; nothing to do"
fi

echo "[uninstall] done. The vault (if any) was NOT deleted."
