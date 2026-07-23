# Docker Compose Deployment

Run the KBUtilLib **API** and **MCP** servers in containers.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) ≥ 24
- Docker Compose plugin (bundled with Docker Desktop; on Linux: `docker compose version`)

## Start

```bash
docker compose -f deploy/docker/compose.yaml up -d
```

Both images are built from the repo root `Dockerfile` on first run.

## Services

| Service | URL | Purpose |
|---------|-----|---------|
| `api` | http://localhost:8000 | REST API (open — no auth by design) |
| `mcp` | http://localhost:8001 | MCP server for agents / editors |

## Health Check

```bash
curl http://localhost:8000/health
```

A `200 OK` with `{"status":"ok"}` (or similar) confirms the API is up.

## Rebuild After Code Changes

```bash
docker compose -f deploy/docker/compose.yaml build --no-cache
docker compose -f deploy/docker/compose.yaml up -d
```

## Stop

```bash
docker compose -f deploy/docker/compose.yaml down
```

## Logs

```bash
docker compose -f deploy/docker/compose.yaml logs -f
# one service only:
docker compose -f deploy/docker/compose.yaml logs -f api
```

## Security Note

**No authentication is configured by design** (open/public profile).  
For production deployments exposed to the internet, place a reverse proxy with auth in front
(e.g. nginx + basic-auth, Traefik + forward-auth, Caddy + JWT middleware).
