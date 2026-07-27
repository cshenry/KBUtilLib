"""FastAPI HTTP adapter — exposes capability registry as REST endpoints.

All ``fastapi``/``uvicorn`` imports are **lazy** (inside functions).  This
module can be imported without those packages installed; only ``build_app()``
and ``main()`` require them at call time.

Auth
----
The API is **fully open** — no bearer token or other auth is required.  It is
intended for deployment on an internal shared server (e.g. Poplar) where
network-level access control is the primary gate.  An optional request-logging
middleware is included (off by default; pass ``log_requests=True`` to
:func:`build_app`).

Public API
----------
``capability_to_route_meta(spec) -> dict``
    Pure helper — no fastapi needed.  Returns a dict with keys ``path``,
    ``method``, ``input_schema``, ``output_schema``.  Unit-testable offline.

``build_app(registry=None, app=None, log_requests=False) -> FastAPI``
    Build a configured FastAPI application.  Registers endpoints:

    * ``GET /health``              → ``{"status": "ok"}``
    * ``GET /version``             → ``{"version": "<kbutillib version>"}``
    * ``GET /v1/capabilities``     → list of all capability metadata
    * ``GET /v1/capabilities/{name}`` → single capability detail
    * ``POST /v1/tools/{name}``    → call a capability (422 input error,
                                     503 unavailable, 404 not found)

    Auto-publishes ``/openapi.json``, ``/docs``, ``/redoc``.

``main()``
    Console-script entry point for ``kbu-api``.
    Supports ``--host`` (default 127.0.0.1, loopback-only) and ``--port``
    (default 8000). Pass ``--host 0.0.0.0`` to deliberately widen the bind.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kbutillib.core.registry import CapabilityRegistry, CapabilitySpec

__all__ = [
    "capability_to_route_meta",
    "build_app",
    "main",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pure helper — usable without fastapi installed
# ---------------------------------------------------------------------------


def capability_to_route_meta(spec: CapabilitySpec) -> dict[str, Any]:
    """Convert a :class:`~kbutillib.core.registry.CapabilitySpec` to route metadata.

    This function does **not** import ``fastapi``; it is unit-testable offline.

    Parameters
    ----------
    spec:
        A registered :class:`~kbutillib.core.registry.CapabilitySpec`.

    Returns
    -------
    dict
        A dictionary with keys:

        ``path``
            The HTTP path for this capability, e.g.
            ``"/v1/tools/biochem.search_compounds"``.
        ``method``
            The HTTP method string, always ``"POST"`` for tool invocations.
        ``input_schema``
            JSON Schema dict derived from ``spec.input_model.model_json_schema()``
            if an input model is present, otherwise an empty object schema.
        ``output_schema``
            JSON Schema dict derived from ``spec.output_model.model_json_schema()``
            if an output model is present, otherwise an empty object schema.
    """
    path = f"/v1/tools/{spec.name}"

    if spec.input_model is not None:
        try:
            input_schema: dict[str, Any] = spec.input_model.model_json_schema()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "capability_to_route_meta: could not build input schema for %r: %s",
                spec.name,
                exc,
            )
            input_schema = {"type": "object", "properties": {}}
    else:
        input_schema = {"type": "object", "properties": {}}

    if spec.output_model is not None:
        try:
            output_schema: dict[str, Any] = spec.output_model.model_json_schema()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "capability_to_route_meta: could not build output schema for %r: %s",
                spec.name,
                exc,
            )
            output_schema = {"type": "object", "properties": {}}
    else:
        output_schema = {"type": "object", "properties": {}}

    return {
        "path": path,
        "method": "POST",
        "input_schema": input_schema,
        "output_schema": output_schema,
    }


# ---------------------------------------------------------------------------
# Internal: capability detail dict (used in list + detail endpoints)
# ---------------------------------------------------------------------------


def _cap_detail(spec: CapabilitySpec) -> dict[str, Any]:
    """Return a JSON-serialisable detail dict for one capability spec."""
    available, reason = spec.availability()
    meta = capability_to_route_meta(spec)
    return {
        "name": spec.name,
        "domain": spec.domain,
        "summary": spec.effective_summary(),
        "tags": list(spec.tags),
        "available": available,
        "unavailable_reason": reason,
        "input_schema": meta["input_schema"],
        "output_schema": meta["output_schema"],
    }


# ---------------------------------------------------------------------------
# Internal: resolve registry
# ---------------------------------------------------------------------------


def _resolve_registry(
    registry: CapabilityRegistry | None,
    app: Any,
) -> CapabilityRegistry:
    """Return a populated registry, building one if needed."""
    from kbutillib.core.capability import register_all
    from kbutillib.core.registry import get_registry

    if registry is not None:
        return registry

    reg = get_registry()

    if app is not None:
        register_all(app, registry=reg)
    else:
        try:
            from kbutillib import KBUtilLib

            kbu = KBUtilLib()
            register_all(kbu, registry=reg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("build_app: could not auto-register capabilities: %s", exc)

    return reg


# ---------------------------------------------------------------------------
# build_app — lazy fastapi import
# ---------------------------------------------------------------------------


def build_app(
    registry: CapabilityRegistry | None = None,
    app: Any = None,
    *,
    log_requests: bool = False,
) -> Any:  # -> FastAPI
    """Build and return a configured FastAPI application.

    The application is **fully open** — no authentication is required.  This
    is appropriate for deployment on a trusted internal server (Poplar) where
    network access control provides the first line of defence.

    Parameters
    ----------
    registry:
        A pre-built :class:`~kbutillib.core.registry.CapabilityRegistry`.
        When ``None``, the global singleton is used.  If the global singleton
        is empty and *app* is provided, ``register_all(app)`` is called.
        If both are ``None``, a default :class:`~kbutillib.KBUtilLib` is
        constructed and its capabilities are registered.
    app:
        A ``KBUtilLib`` facade instance used to register capabilities when
        *registry* is ``None`` or empty.  Ignored when *registry* is
        provided and already populated.
    log_requests:
        When ``True``, attach a lightweight request-logging middleware that
        logs method, path, and status code at DEBUG level.  Defaults to
        ``False``.

    Returns
    -------
    FastAPI
        A configured application ready to be served by uvicorn.

    Raises
    ------
    ImportError
        If the ``fastapi`` package is not installed.
    """
    try:
        import fastapi  # noqa: F401
        from fastapi import FastAPI, HTTPException
        from fastapi.middleware.cors import CORSMiddleware
    except ImportError as exc:
        raise ImportError(
            "The 'fastapi' package is required to build the HTTP API. "
            "Install it with: pip install 'kbutillib[api]' or pip install 'fastapi[standard]'"
        ) from exc

    import importlib.metadata

    # ------------------------------------------------------------------
    # Resolve registry
    # ------------------------------------------------------------------
    reg = _resolve_registry(registry, app)

    # ------------------------------------------------------------------
    # Get version
    # ------------------------------------------------------------------
    try:
        _version = importlib.metadata.version("KBUtilLib")
    except Exception:  # noqa: BLE001
        _version = "unknown"

    # ------------------------------------------------------------------
    # Create the FastAPI app
    # ------------------------------------------------------------------
    fapp = FastAPI(
        title="KBUtilLib API",
        description=(
            "HTTP surface for KBUtilLib capabilities.  "
            "Open access — no authentication required."
        ),
        version=_version,
    )

    # Open CORS — suitable for an internal shared server
    fapp.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Optional lightweight request logging middleware
    if log_requests:
        try:
            from starlette.middleware.base import BaseHTTPMiddleware
            from starlette.requests import Request

            class _LoggingMiddleware(BaseHTTPMiddleware):
                async def dispatch(self, request: Request, call_next: Any) -> Any:
                    response = await call_next(request)
                    logger.debug(
                        "%s %s → %s",
                        request.method,
                        request.url.path,
                        response.status_code,
                    )
                    return response

            fapp.add_middleware(_LoggingMiddleware)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not attach logging middleware: %s", exc)

    # ------------------------------------------------------------------
    # Utility endpoints
    # ------------------------------------------------------------------

    @fapp.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        """Liveness probe — always returns ``{"status": "ok"}``."""
        return {"status": "ok"}

    @fapp.get("/version", tags=["meta"])
    async def version() -> dict[str, str]:
        """Return the installed KBUtilLib version."""
        return {"version": _version}

    # ------------------------------------------------------------------
    # Capability discovery endpoints
    # ------------------------------------------------------------------

    @fapp.get("/v1/capabilities", tags=["capabilities"])
    async def list_capabilities() -> list[dict[str, Any]]:
        """List all registered capabilities with availability and schema refs."""
        return [_cap_detail(spec) for spec in sorted(reg, key=lambda s: s.name)]

    @fapp.get("/v1/capabilities/{name:path}", tags=["capabilities"])
    async def get_capability(name: str) -> dict[str, Any]:
        """Return detail for a single capability by dotted name.

        Returns 404 if the capability is not registered.
        """
        try:
            from kbutillib.core.errors import CapabilityNotFound

            spec = reg.get(name)
        except CapabilityNotFound:
            raise HTTPException(status_code=404, detail=f"Capability {name!r} not found.")
        return _cap_detail(spec)

    # ------------------------------------------------------------------
    # Tool invocation endpoint
    # ------------------------------------------------------------------

    @fapp.post("/v1/tools/{name:path}", tags=["tools"])
    async def invoke_tool(name: str, body: dict[str, Any] = {}) -> Any:  # noqa: B006
        """Invoke a capability by name.

        * **422** — input validation error (pydantic).
        * **503** — capability backend unavailable.
        * **404** — capability not registered.

        The request body must be a JSON object whose keys match the
        capability's ``input_schema``.
        """
        # Resolve the spec
        try:
            from kbutillib.core.errors import CapabilityNotFound

            spec = reg.get(name)
        except CapabilityNotFound:
            raise HTTPException(status_code=404, detail=f"Capability {name!r} not found.")

        # Availability gate (checked at call time so the server can start
        # even when backends are absent)
        available, reason = spec.availability()
        if not available:
            raise HTTPException(
                status_code=503,
                detail=f"Capability {name!r} is unavailable: {reason or 'backend not installed'}",
            )

        # Validate input via pydantic model if present
        call_kwargs: dict[str, Any] = body if body else {}
        if spec.input_model is not None and body:
            try:
                validated = spec.input_model(**body)
                call_kwargs = validated.model_dump()
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=422,
                    detail=f"Input validation failed: {exc}",
                ) from exc

        # Invoke the capability
        try:
            result = spec.fn(**call_kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Tool %r raised: %s", name, exc)
            raise HTTPException(
                status_code=500,
                detail=f"Tool error: {exc}",
            ) from exc

        return result

    return fapp


# ---------------------------------------------------------------------------
# main() — console script entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """Entry point for the ``kbu-api`` console script.

    Usage
    -----
    ::

        kbu-api                        # bind 127.0.0.1:8000 (loopback)
        kbu-api --host 0.0.0.0         # deliberately widen the bind
        kbu-api --port 9000            # alternate port
        kbu-api --log-requests         # enable per-request logging

    Parameters
    ----------
    argv:
        Argument list; defaults to ``sys.argv[1:]``.
    """
    parser = argparse.ArgumentParser(
        prog="kbu-api",
        description=(
            "KBUtilLib HTTP API server — expose capability registry as REST endpoints.  "
            "Open access, no authentication required."
        ),
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host address (default: 127.0.0.1, loopback-only; pass "
        "0.0.0.0 to deliberately widen the bind).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Bind port (default: 8000).",
    )
    parser.add_argument(
        "--log-requests",
        action="store_true",
        default=False,
        help="Enable per-request logging middleware.",
    )

    args = parser.parse_args(argv)

    try:
        import uvicorn  # type: ignore[import]
    except ImportError:
        print(
            "Error: uvicorn is required to run the API server. "
            "Install it with: pip install 'kbutillib[api]' or pip install 'uvicorn[standard]'",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        fapp = build_app(log_requests=args.log_requests)
    except ImportError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    uvicorn.run(fapp, host=args.host, port=args.port)
