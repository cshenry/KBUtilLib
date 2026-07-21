#!/usr/bin/env bash
# Deploy/enable the Remote LP-Solver user-level systemd unit on H100.
#
# Per agent-io/prds/remote-lp-solver/fullprompt.md ("Deployment on H100" /
# S13): H100 has no sudo but linger is already enabled, so a user-level
# systemd unit survives logout with no extra step. This script is
# idempotent -- safe to re-run after a `git pull` in ~/projects/KBUtilLib.
#
# Usage:
#   kbutillib/services/lp_solver/deploy/enable_lp_solver.sh
#
# What it does:
#   1. Creates the dedicated venv ~/venvs/lp-solver (fastapi + uvicorn +
#      KBUtilLib installed editable) if it doesn't exist yet.
#   2. Confirms port 8091 is free via `ss -tln` (the PRD requires this check
#      -- H100 has no port registry).
#   3. Installs/refreshes ~/.config/systemd/user/lp-solver.service from the
#      template committed alongside this script.
#   4. `daemon-reload` (only meaningfully needed when the unit file itself
#      changed, but cheap/idempotent to always run) + `enable --now`.
#
# Restarting after a plain code pull (no unit-file change) doesn't need
# this script -- just:
#   systemctl --user restart lp-solver.service

set -euo pipefail

REPO_DIR="${LP_SOLVER_REPO_DIR:-$HOME/projects/KBUtilLib}"
VENV_DIR="${LP_SOLVER_VENV_DIR:-$HOME/venvs/lp-solver}"
UNIT_DIR="$HOME/.config/systemd/user"
LOG_DIR="$HOME/.lp-solver/logs"
PORT=8091

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_TEMPLATE="$SCRIPT_DIR/lp-solver.service"

echo "== Remote LP-Solver deploy =="
echo "repo:  $REPO_DIR"
echo "venv:  $VENV_DIR"
echo "unit:  $UNIT_DIR/lp-solver.service"

if [[ ! -d "$REPO_DIR" ]]; then
  echo "ERROR: $REPO_DIR does not exist. Clone KBUtilLib there first (S13:" >&2
  echo "  WorkingDirectory=%h/projects/KBUtilLib)." >&2
  exit 1
fi

# 1. Dedicated venv with fastapi/uvicorn + KBUtilLib editable.
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "-- creating venv at $VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet fastapi uvicorn
"$VENV_DIR/bin/pip" install --quiet -e "$REPO_DIR"

# 2. Port-free check (no port registry on H100 -- S13/"Deployment on H100").
if command -v ss >/dev/null 2>&1; then
  if ss -tln 2>/dev/null | awk '{print $4}' | grep -q ":${PORT}\$"; then
    echo "WARNING: port $PORT already has a listener:" >&2
    ss -tln | grep ":${PORT}" >&2 || true
    echo "Refusing to enable the unit until $PORT is confirmed free." >&2
    exit 1
  fi
  echo "-- port $PORT is free"
else
  echo "WARNING: 'ss' not found; skipping the port-free check." >&2
fi

# 3. Install the unit file + log dir.
mkdir -p "$UNIT_DIR" "$LOG_DIR"
cp "$UNIT_TEMPLATE" "$UNIT_DIR/lp-solver.service"

# 4. Reload + enable + start.
systemctl --user daemon-reload
systemctl --user enable --now lp-solver.service

echo "-- lp-solver.service status:"
systemctl --user --no-pager status lp-solver.service || true

echo
echo "Done. After future code pulls (no unit-file change), just:"
echo "  systemctl --user restart lp-solver.service"
