"""kbutillib.interfaces.api — FastAPI HTTP adapter for the capability registry.

Exposes all registered KBUtilLib capabilities as REST endpoints.  FastAPI and
uvicorn are imported **lazily** inside the functions that need them, so this
package can be imported without those packages installed.

Public surface
--------------
``build_app(registry=None, app=None) -> FastAPI``
    Build and return a configured FastAPI application.
``main()``
    Console-script entry point for the ``kbu-api`` launcher.
"""
