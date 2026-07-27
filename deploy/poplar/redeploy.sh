#!/usr/bin/env bash
# deploy/poplar/redeploy.sh — fast redeploy for code updates (no reinstall of system config)
# Usage: sudo ./deploy/poplar/redeploy.sh   (run from repo root)
set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
die() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "[redeploy] $*"; }

# ---------------------------------------------------------------------------
# Must be run as root so we can restart the systemd service
# ---------------------------------------------------------------------------
[[ $EUID -eq 0 ]] || die "Run as root: sudo ./deploy/poplar/redeploy.sh"

# ---------------------------------------------------------------------------
# Determine repo root
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
info "Repo root: ${REPO_ROOT}"

# ---------------------------------------------------------------------------
# Determine python (same logic as install.sh)
# ---------------------------------------------------------------------------
PYTHON_BIN="${PYTHON:-python3}"

if [[ -n "${CONDA_PREFIX:-}" ]]; then
    PYTHON_BIN="${CONDA_PREFIX}/bin/python"
elif [[ -n "${VIRTUAL_ENV:-}" ]]; then
    PYTHON_BIN="${VIRTUAL_ENV}/bin/python"
fi

[[ -x "${PYTHON_BIN}" ]] || die "Python not found at ${PYTHON_BIN}. Activate your conda env or venv first."
info "Using python: ${PYTHON_BIN}"

# ---------------------------------------------------------------------------
# Git pull (fast-forward only — refuse merge commits)
# ---------------------------------------------------------------------------
info "Pulling latest changes (fast-forward only)..."
cd "${REPO_ROOT}"

# Ensure we are in a git repo
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "Not a git repository: ${REPO_ROOT}"

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
info "Current branch: ${CURRENT_BRANCH}"

# Attempt fast-forward pull; fail loudly if not possible
if ! git pull --ff-only; then
    die "git pull --ff-only failed. The remote has diverged from local history.
Resolve manually (rebase or merge), then re-run redeploy.sh."
fi

info "git pull OK"

# ---------------------------------------------------------------------------
# Re-install package (editable, in-place; resolves any new deps)
# ---------------------------------------------------------------------------
info "Re-installing KBUtilLib with [api,mcp] extras..."
"${PYTHON_BIN}" -m pip install -e "${REPO_ROOT}[api,mcp]" --quiet
info "pip install complete"

# ---------------------------------------------------------------------------
# Restart the API service
# ---------------------------------------------------------------------------
info "Restarting kbutillib-api service..."
systemctl restart kbutillib-api
info "Service restarted"

# ---------------------------------------------------------------------------
# Report status
# ---------------------------------------------------------------------------
echo ""
echo "=== Service Status ==="
systemctl status kbutillib-api --no-pager || true   # status exits non-zero if degraded; show anyway
echo ""
info "=== Redeploy complete ==="
info "Run 'sudo ./deploy/poplar/status.sh' for a full health check."
