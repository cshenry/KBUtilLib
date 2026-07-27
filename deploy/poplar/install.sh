#!/usr/bin/env bash
# deploy/poplar/install.sh — one-time install for KBUtilLib API on Poplar (systemd + nginx)
# Usage: sudo ./deploy/poplar/install.sh   (run from repo root)
set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
die() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "[install] $*"; }

# ---------------------------------------------------------------------------
# Must be run as root (sudo) so we can write to /etc and run systemctl
# ---------------------------------------------------------------------------
[[ $EUID -eq 0 ]] || die "Run as root: sudo ./deploy/poplar/install.sh"

# ---------------------------------------------------------------------------
# Determine repo root (where this script lives, two levels up)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
info "Repo root: ${REPO_ROOT}"

# ---------------------------------------------------------------------------
# Verify Python >= 3.10
# ---------------------------------------------------------------------------
PYTHON_BIN="${PYTHON:-python3}"

# If a conda env is active, prefer its python; otherwise fall back to venv or system
if [[ -n "${CONDA_PREFIX:-}" ]]; then
    PYTHON_BIN="${CONDA_PREFIX}/bin/python"
    info "Using conda env python: ${PYTHON_BIN}"
elif [[ -n "${VIRTUAL_ENV:-}" ]]; then
    PYTHON_BIN="${VIRTUAL_ENV}/bin/python"
    info "Using venv python: ${PYTHON_BIN}"
else
    info "No conda or venv detected — using system python: ${PYTHON_BIN}"
    info "TIP: activate a conda env or venv before running install.sh for isolation."
fi

[[ -x "${PYTHON_BIN}" ]] || die "Python not found at ${PYTHON_BIN}"

PYTHON_VERSION=$("${PYTHON_BIN}" -c "import sys; print('%d.%d' % sys.version_info[:2])")
PYTHON_MAJOR=$("${PYTHON_BIN}" -c "import sys; print(sys.version_info.major)")
PYTHON_MINOR=$("${PYTHON_BIN}" -c "import sys; print(sys.version_info.minor)")

if [[ "${PYTHON_MAJOR}" -lt 3 ]] || { [[ "${PYTHON_MAJOR}" -eq 3 ]] && [[ "${PYTHON_MINOR}" -lt 10 ]]; }; then
    die "Python >= 3.10 required; found ${PYTHON_VERSION}"
fi
info "Python ${PYTHON_VERSION} OK"

# ---------------------------------------------------------------------------
# Determine service user
# ---------------------------------------------------------------------------
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$(logname 2>/dev/null || echo kbutillib)}}"
info "Service user: ${SERVICE_USER}"

# ---------------------------------------------------------------------------
# Install Python package (editable)
# ---------------------------------------------------------------------------
info "Installing KBUtilLib with [api,mcp] extras (editable)..."
"${PYTHON_BIN}" -m pip install -e "${REPO_ROOT}[api,mcp]" --quiet
info "pip install complete"

# ---------------------------------------------------------------------------
# Install systemd unit
# ---------------------------------------------------------------------------
SERVICE_SRC="${SCRIPT_DIR}/kbutillib-api.service"
SERVICE_DST="/etc/systemd/system/kbutillib-api.service"

[[ -f "${SERVICE_SRC}" ]] || die "Missing ${SERVICE_SRC}"

info "Installing systemd unit to ${SERVICE_DST}..."
sed \
    -e "s|<SERVICE_USER>|${SERVICE_USER}|g" \
    -e "s|<REPO_PATH>|${REPO_ROOT}|g" \
    -e "s|<CONDA_OR_VENV_PYTHON>|${PYTHON_BIN}|g" \
    "${SERVICE_SRC}" > "${SERVICE_DST}"

chmod 644 "${SERVICE_DST}"

systemctl daemon-reload
systemctl enable kbutillib-api
systemctl start kbutillib-api
info "systemd unit enabled and started"

# ---------------------------------------------------------------------------
# Install nginx config
# ---------------------------------------------------------------------------
NGINX_SRC="${SCRIPT_DIR}/nginx.conf"
NGINX_AVAIL="/etc/nginx/sites-available/kbutillib"
NGINX_ENABLED="/etc/nginx/sites-enabled/kbutillib"

[[ -f "${NGINX_SRC}" ]] || die "Missing ${NGINX_SRC}"

command -v nginx >/dev/null 2>&1 || die "nginx not found — install nginx first"

info "Installing nginx config to ${NGINX_AVAIL}..."
cp "${NGINX_SRC}" "${NGINX_AVAIL}"
chmod 644 "${NGINX_AVAIL}"

# Symlink into sites-enabled
if [[ -L "${NGINX_ENABLED}" ]]; then
    rm "${NGINX_ENABLED}"
fi
ln -s "${NGINX_AVAIL}" "${NGINX_ENABLED}"
info "nginx config symlinked to ${NGINX_ENABLED}"

nginx -t
systemctl reload nginx
info "nginx reloaded"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
info "=== Install complete ==="
info "API is running on http://127.0.0.1:8000 (proxied via nginx on port 80)"
info "Run 'sudo ./deploy/poplar/status.sh' to verify health."
