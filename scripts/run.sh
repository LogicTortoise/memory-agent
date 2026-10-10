#!/usr/bin/env bash
# run.sh — run a memory-agent subcommand against an existing vault.
#
# Usage:
#   ./scripts/run.sh doctor
#   ./scripts/run.sh init --root "claude-code=/tmp/fake_claude"
#   ./scripts/run.sh recall --query "Python prototype"
#   ./scripts/run.sh worker --once
#
# The script activates the venv created by ``install.sh`` and
# forwards every argument to ``python -m memory_agent``. ``$@`` is
# not interpreted — the call uses argv, not shell parsing.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${VENV:-${REPO_ROOT}/.venv}"
VAULT_DIR="${VAULT_DIR:-${REPO_ROOT}/vault}"

if [[ ! -d "${VENV}" ]]; then
    echo "[run] venv not found at ${VENV}; run scripts/install.sh first" >&2
    exit 1
fi

# shellcheck disable=SC1091
source "${VENV}/bin/activate"

if [[ ! -f "${VAULT_DIR}/state/state.sqlite" ]]; then
    echo "[run] vault not initialised at ${VAULT_DIR}; running init first"
    python -m memory_agent --vault "${VAULT_DIR}" init
fi

exec python -m memory_agent --vault "${VAULT_DIR}" "$@"
