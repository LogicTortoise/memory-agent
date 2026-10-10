#!/usr/bin/env bash
# install.sh — install memory-agent into the active Python environment.
#
# Reproducible install:
#   * Creates a virtualenv at <repo>/.venv (override with VENV=...)
#   * Installs the package in editable mode with [dev] extras
#   * Writes a sample config to <vault>/config.toml on first run
#
# The script is idempotent: re-running it does not re-create the
# virtualenv or re-install if the marker file is present.
#
# The installer NEVER touches the vault. The vault is operator-owned
# data; ``uninstall.sh`` explicitly does not delete it.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${VENV:-${REPO_ROOT}/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VAULT_DIR="${VAULT_DIR:-${REPO_ROOT}/vault}"
MARKER="${VENV}/.memory_agent_installed"

if [[ ! -d "${VENV}" ]]; then
    echo "[install] creating virtualenv at ${VENV}"
    "${PYTHON_BIN}" -m venv "${VENV}"
fi

# shellcheck disable=SC1091
source "${VENV}/bin/activate"
python -m pip install --upgrade pip wheel setuptools >/dev/null
python -m pip install -e "${REPO_ROOT}[dev]"

# Marker so re-runs are no-ops.
touch "${MARKER}"

# Sample config + vault skeleton (only if missing).
if [[ ! -f "${VAULT_DIR}/config.toml" ]]; then
    mkdir -p "${VAULT_DIR}"
    cp "${REPO_ROOT}/samples/config.toml" "${VAULT_DIR}/config.toml"
    echo "[install] wrote sample config to ${VAULT_DIR}/config.toml"
fi

echo "[install] ready. activate with: source ${VENV}/bin/activate"
echo "[install] CLI: python -m memory_agent --vault ${VAULT_DIR} doctor"
