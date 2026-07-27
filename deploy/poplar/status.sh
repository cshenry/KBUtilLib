#!/usr/bin/env bash
# deploy/poplar/status.sh — quick health check for the KBUtilLib API service
# Usage: sudo ./deploy/poplar/status.sh   (run from repo root)
#        (sudo needed only for systemctl; curl checks work without it)
set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info() { echo "[status] $*"; }
ok()   { echo "  [OK]   $*"; }
fail() { echo "  [FAIL] $*" >&2; }

API_BASE="http://localhost:8000"
EXIT_CODE=0

# ---------------------------------------------------------------------------
# 1. systemd service status
# ---------------------------------------------------------------------------
echo ""
echo "=== systemd: kbutillib-api ==="
systemctl status kbutillib-api --no-pager || {
    fail "kbutillib-api is not running or in a degraded state"
    EXIT_CODE=1
}

# ---------------------------------------------------------------------------
# 2. /health
# ---------------------------------------------------------------------------
echo ""
echo "=== GET ${API_BASE}/health ==="
HEALTH_RESPONSE=$(curl -sf "${API_BASE}/health" 2>/dev/null) || {
    fail "Could not reach ${API_BASE}/health — is the service up?"
    EXIT_CODE=1
    HEALTH_RESPONSE=""
}
if [[ -n "${HEALTH_RESPONSE}" ]]; then
    echo "${HEALTH_RESPONSE}" | python3 -m json.tool && ok "/health returned valid JSON" || {
        fail "/health response is not valid JSON: ${HEALTH_RESPONSE}"
        EXIT_CODE=1
    }
fi

# ---------------------------------------------------------------------------
# 3. /version
# ---------------------------------------------------------------------------
echo ""
echo "=== GET ${API_BASE}/version ==="
VERSION_RESPONSE=$(curl -sf "${API_BASE}/version" 2>/dev/null) || {
    fail "Could not reach ${API_BASE}/version"
    EXIT_CODE=1
    VERSION_RESPONSE=""
}
if [[ -n "${VERSION_RESPONSE}" ]]; then
    echo "${VERSION_RESPONSE}" | python3 -m json.tool && ok "/version returned valid JSON" || {
        fail "/version response is not valid JSON: ${VERSION_RESPONSE}"
        EXIT_CODE=1
    }
fi

# ---------------------------------------------------------------------------
# 4. /v1/capabilities
# ---------------------------------------------------------------------------
echo ""
echo "=== GET ${API_BASE}/v1/capabilities ==="
CAP_RESPONSE=$(curl -sf "${API_BASE}/v1/capabilities" 2>/dev/null) || {
    fail "Could not reach ${API_BASE}/v1/capabilities"
    EXIT_CODE=1
    CAP_RESPONSE=""
}
if [[ -n "${CAP_RESPONSE}" ]]; then
    echo "${CAP_RESPONSE}" | python3 -m json.tool && ok "/v1/capabilities returned valid JSON" || {
        fail "/v1/capabilities response is not valid JSON: ${CAP_RESPONSE}"
        EXIT_CODE=1
    }
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
if [[ ${EXIT_CODE} -eq 0 ]]; then
    info "=== All checks PASSED ==="
else
    info "=== One or more checks FAILED (exit ${EXIT_CODE}) ==="
fi

exit "${EXIT_CODE}"
