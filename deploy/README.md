# Deployment

KBUtilLib can be deployed as an always-on API server for shared server use.

## Poplar (systemd + nginx)

See [`poplar/`](./poplar/) for the full setup. Quick steps:

1. Install: `bash deploy/poplar/install.sh`
2. Start: `sudo systemctl start kbutillib-api`
3. Status: `bash deploy/poplar/status.sh`
4. Redeploy after changes: `bash deploy/poplar/redeploy.sh`

The nginx config (`poplar/nginx.conf`) proxies port 8765 → `/api/kbutillib/`.

> **Note**: No auth is configured by default. For internal shared-server use this is
> intentional. Add nginx basic_auth or JWT middleware for public exposure.

## Docker

See [`docker/`](./docker/) for the Docker Compose setup. Quick start:

```bash
pip install kbutillib[all]
cd deploy/docker
docker compose up
```

Services:
- `kbu-api` — FastAPI server (port 8765)
- `kbu-mcp` — MCP stdio server (for agent use)

## Extras required

Both deploys need `kbutillib[api]` (FastAPI + uvicorn). For MCP, also `kbutillib[mcp]`.
Install everything: `pip install -e .[all]`
