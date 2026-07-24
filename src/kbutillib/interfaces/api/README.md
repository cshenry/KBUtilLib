# kbutillib.interfaces.api — FastAPI HTTP Server

Exposes the capability registry as a bearer-authenticated HTTP API. Use it when you need to call KBUtilLib capabilities from external systems, notebooks on remote kernels, or services that cannot embed a Python process.

## What lives here

| File | Purpose |
|------|---------|
| `app.py` | FastAPI application — routes, lifespan, capability dispatch |
| `auth.py` | Bearer token validation middleware |
| `middleware.py` | Request logging, error normalization, CORS |

## Install

```console
pip install -e ".[api]"
```

This adds `fastapi`, `uvicorn`, and `httpx` to your environment.

## Run

```console
kbu-api
```

The server starts on `http://0.0.0.0:8000` by default. Override with environment variables:

```console
KBU_API_HOST=127.0.0.1 KBU_API_PORT=9000 kbu-api
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness check — returns `{"status": "ok"}` |
| `GET` | `/version` | Package version string |
| `GET` | `/v1/capabilities` | List all registered capabilities with metadata |
| `POST` | `/v1/tools/{name}` | Invoke a capability by name |

## Authentication

Set `KBU_API_TOKEN` in the environment before starting the server:

```console
export KBU_API_TOKEN="your-secret-token"
kbu-api
```

Pass it in requests as a bearer token:

```console
curl -H "Authorization: Bearer your-secret-token" http://localhost:8000/v1/capabilities
```

```console
curl -X POST http://localhost:8000/v1/tools/biochem.search_compounds \
  -H "Authorization: Bearer your-secret-token" \
  -H "Content-Type: application/json" \
  -d '{"query": "atp", "limit": 5}'
```

## Interactive docs

Visit `http://localhost:8000/docs` for the Swagger UI after starting the server. All capability endpoints are listed with input/output schemas.

## Verify

```console
curl http://localhost:8000/health   # {"status": "ok"}
curl http://localhost:8000/v1/capabilities | python -m json.tool
```
