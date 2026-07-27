# KBUtilLib — Poplar Deployment (systemd + nginx)

Deployment artifacts for the KBUtilLib API server on Poplar (or any Linux host with systemd and nginx).
No Docker. No auth — open/public access by design (internal shared server). See [Auth note](#auth) below.

---

## Prerequisites

| Requirement | Details |
|---|---|
| Python ≥ 3.10 | conda env **or** venv strongly recommended |
| nginx | `sudo apt install nginx` |
| systemd | standard on modern Ubuntu/Debian/RHEL |
| git | repo must already be cloned on the host |

---

## One-Time Install

All scripts are run **from the repo root** with `sudo` (needed to write to `/etc` and run `systemctl`).

```bash
# 1. Clone or ensure the repo is available on Poplar
git clone <repo-url> /opt/KBUtilLib   # or wherever you want
cd /opt/KBUtilLib

# 2. Activate your conda env or venv BEFORE running install.sh
source /path/to/anaconda3/etc/profile.d/conda.sh
conda activate kbutillib
# — or —
source /path/to/venv/bin/activate

# 3. Run the installer
sudo -E ./deploy/poplar/install.sh
#    -E preserves CONDA_PREFIX / VIRTUAL_ENV so install.sh picks the right python
```

`install.sh` will:
1. Verify Python ≥ 3.10.
2. `pip install -e '.[api,mcp]'` (editable, in-place).
3. Write `/etc/systemd/system/kbutillib-api.service` (with your user/path substituted).
4. Enable and start the `kbutillib-api` service.
5. Write `/etc/nginx/sites-available/kbutillib` and symlink to `sites-enabled/`.
6. Reload nginx.

The API will be live at `http://<server-ip>/` (port 80 via nginx → 127.0.0.1:8000).

---

## Redeploy (code updates)

```bash
cd /opt/KBUtilLib
conda activate kbutillib       # or activate venv
sudo -E ./deploy/poplar/redeploy.sh
```

`redeploy.sh` will:
1. `git pull --ff-only` — refuses if the remote has diverged (merge commit required first).
2. `pip install -e '.[api,mcp]'` — picks up any new dependencies.
3. `systemctl restart kbutillib-api` — restarts with new code.
4. Prints service status.

> **Tip:** If `git pull --ff-only` fails, rebase locally, push, then redeploy.

---

## Health / Status Check

```bash
sudo ./deploy/poplar/status.sh
```

Reports:
- `systemctl status kbutillib-api`
- `GET /health` → JSON
- `GET /version` → JSON
- `GET /v1/capabilities` → JSON

Exit 0 if all checks pass, exit 1 if any fail.

---

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /health` | Service liveness check |
| `GET /version` | Package version + build info |
| `GET /v1/capabilities` | List all registered capabilities (from the capability registry) |
| `POST /v1/tools/{name}` | Invoke a capability by name |
| `GET /openapi.json` | OpenAPI schema (full) |
| `GET /docs` | Swagger UI |

---

## MCP Endpoint

The **API server** (`kbu-api` / `kbutillib.interfaces.api.app`) is a **FastAPI HTTP server only**.
It does not expose an MCP-over-HTTP endpoint natively.

To use MCP from a remote client:
- Run `kbu-mcp` (stdio MCP server) on the same host.
- Or set up an MCP-over-HTTP gateway/proxy in front of `kbu-mcp` (not included here).

The MCP stdio server binary is installed as `kbu-mcp` after `pip install -e '.[mcp]'`.

---

## Auth

**No auth is configured by design.** This deployment is intended for an internal shared server
(Poplar) where open access is acceptable.

To add authentication later, choose one of:

### Option A — nginx HTTP basic auth
```nginx
# In nginx.conf, inside the location / block:
auth_basic "KBUtilLib API";
auth_basic_user_file /etc/nginx/.htpasswd;
```
Generate password file: `sudo htpasswd -c /etc/nginx/.htpasswd <username>`

### Option B — Bearer token auth (FastAPI layer)
The FastAPI app (`interfaces/api/auth.py`) supports bearer-token auth middleware.
Enable it by setting the `KBU_API_TOKEN` environment variable in the systemd unit:
```ini
Environment=KBU_API_TOKEN=<your-secret-token>
```

### Option C — Reverse proxy auth (Nginx + OAuth2 Proxy, Authentik, etc.)
Place an auth proxy (e.g. `oauth2-proxy`) in front of nginx.

---

## File Inventory

```
deploy/poplar/
├── install.sh              # One-time install (systemd + nginx)
├── redeploy.sh             # Fast redeploy for code updates
├── status.sh               # Health check
├── kbutillib-api.service   # systemd unit template (placeholders substituted by install.sh)
├── nginx.conf              # nginx virtual host (no auth, port 80)
└── README.md               # This file
```

---

## Troubleshooting

**Service won't start:**
```bash
journalctl -u kbutillib-api -n 50 --no-pager
```

**nginx config errors:**
```bash
sudo nginx -t
```

**Port conflict (something already on 8000):**
Edit `kbutillib-api.service` and `nginx.conf` to use a different port, then re-run `install.sh`.

**Python not found after conda activate:**
Pass `PYTHON=/path/to/python` explicitly: `sudo PYTHON=$(which python) ./deploy/poplar/install.sh`
