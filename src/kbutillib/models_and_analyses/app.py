"""FastAPI wiring and the ``models-and-analyses`` console entry point.

``fastapi`` and ``uvicorn`` are imported LAZILY inside functions (never at module
scope), exactly as :mod:`kbutillib.interfaces.api.app` does, so importing this
module — and the whole data layer behind it — never requires them. The pure data
layer in :mod:`~kbutillib.models_and_analyses.service` carries the logic and is
tested without a running server.

CAC conformance (king/docs/CROSS_APP_COMMUNICATION.md, plane 1):

  * I1 — KING IS NOT A RUNTIME DEPENDENCY. The app runs and is useful with KING
    absent. ``--no-king`` skips manifest self-registration entirely; plain
    ``serve`` would self-register (task p5 owns writing the manifest, so this
    skeleton only records intent, it does not write one). Standalone there is no
    user identity, so anything keyed on "the current user" DEGRADES to "everything
    readable on this filesystem" rather than failing.
  * I4 — APP_ID / app_us() come from :mod:`arc_context`, never hardcoded.
  * I5 — contract_version gate on startup (integer equality; any difference
    refuses startup naming both versions).
  * I6 — ISOLATION OF AUTHORITY. The app reaches data through its own filesystem
    and REST access only. It uses NONE of KING's authorized MCP servers or
    credentials and assumes no inherited authority.

The two proxy gotchas (both recorded in KIND's docs/APP_INTEGRATION.md) are
handled below where they arise.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import TYPE_CHECKING, Any, Callable, Optional

from ..arc_context import APP_ID, app_us
from ..koros_arc_store import CONTRACT_VERSION
from ..koros_arc_store.exceptions import (
    ContractVersionMismatch,
    RunsRootResolutionError,
)
from .service import (
    build_arc_models,
    build_portfolio,
    check_startup_contract_version,
    resolve_app_state_dir,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import FastAPI

logger = logging.getLogger("kbutillib.models_and_analyses")

__all__ = [
    "build_app",
    "make_store",
    "render_index_html",
    "main",
]


# ── store construction ──────────────────────────────────────────────────────────


def make_store(runs_root: Optional[str] = None) -> Any:
    """Construct the :class:`KorosArcStore` the app reads through.

    The app holds NO filesystem knowledge of its own: it never walks the runs tree
    with ``pathlib``; the store is the only door. Runs-root resolution is the
    store's pinned chain (explicit ``--runs-root``, then ``$KOROS_HOME/runs``, then
    ``$KING_KOROS_RUNS``, then raise) — this app does not add a fallback.

    Imported lazily so importing this module in an environment with no runs tree
    does not raise at import time; the ``RunsRootResolutionError`` surfaces when a
    store is actually constructed.
    """
    from ..koros_arc_store import KorosArcStore

    return KorosArcStore(runs_root)


# ── HTML index (minimal; this task is not a frontend exercise) ───────────────────


def render_index_html(root_path: str = "") -> str:
    """Render the minimal HTML index that lists the portfolio and links into arcs.

    ``root_path`` is the proxy prefix the app is served under. Every URL this page
    constructs is built by joining onto ``root_path`` with its trailing slash
    PRESERVED — the trailing slash is load-bearing behind KIND's proxy. The page
    is a thin client that fetches ``<root>/api/portfolio`` and renders it; the
    heavy lifting stays server-side.
    """
    # Normalise: keep exactly one trailing slash on the base so relative joins
    # land correctly under a non-root proxy path (the trailing slash is
    # load-bearing — dropping it rewrites every asset URL one segment too high).
    base = root_path.rstrip("/") + "/" if root_path else "/"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Models and Analyses</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 2rem; }}
 .arc {{ margin-left: 1.5rem; }}
 .empty {{ color: #888; }}
 .unattributed {{ color: #a60; }}
 code {{ background: #f4f4f4; padding: 0 .3em; }}
</style>
</head>
<body>
<h1>Models and Analyses</h1>
<p>App <code>{APP_ID}</code> (module <code>{app_us()}</code>),
   contract version {CONTRACT_VERSION}.</p>
<div id="portfolio">Loading portfolio…</div>
<script>
const BASE = {base!r};
async function load() {{
  const res = await fetch(BASE + "api/portfolio");
  const data = await res.json();
  const el = document.getElementById("portfolio");
  el.innerHTML = "";
  for (const proj of data.projects) {{
    const h = document.createElement("h2");
    h.textContent = proj.name + " (" + proj.arc_count + " arcs)";
    el.appendChild(h);
    for (const arc of proj.arcs) renderArc(el, proj.name, arc);
  }}
  const u = data.unattributed;
  const uh = document.createElement("h2");
  uh.className = "unattributed";
  uh.textContent = "Unattributed (" + u.total_analyses + " analyses)";
  el.appendChild(uh);
}}
function renderArc(el, project, arc) {{
  const d = document.createElement("div");
  d.className = "arc" + (arc.empty ? " empty" : "");
  const link = BASE + "api/arcs/" + encodeURIComponent(project)
    + "/" + encodeURIComponent(arc.slug) + "/models";
  d.innerHTML = "<a href='" + link + "'>" + (arc.title || arc.slug) + "</a>"
    + " — " + arc.total_analyses + " analyses"
    + (arc.empty ? " (empty)" : "");
  el.appendChild(d);
  for (const child of (arc.children || [])) renderArc(d, project, child);
}}
load();
</script>
</body>
</html>
"""


# ── FastAPI app ──────────────────────────────────────────────────────────────────


def build_app(
    store_factory: Optional[Callable[[], Any]] = None,
    *,
    root_path: str = "",
) -> "FastAPI":
    """Build the FastAPI application (requires ``fastapi`` at call time).

    ``store_factory`` returns the store the endpoints read through; it defaults to
    :func:`make_store`, and tests inject a factory returning the shipped fake.
    ``root_path`` is the proxy prefix; it is passed straight to FastAPI so the app
    serves correctly under a non-root ``--root-path``.

    THE TWO PROXY GOTCHAS:

    1. This app DELIBERATELY does not set ``FORWARDED_ALLOW_IPS`` or
       ``UVICORN_PROXY_HEADERS`` (and does not enable uvicorn's ``proxy_headers``).
       KIND runs behind a double proxy that sends
       ``x-forwarded-proto: "https,http"``; trusting it rewrites asset URLs under
       ``/hub/...`` and the embedded iframe renders BLANK with no error. This is
       exactly the setting a developer reaches for when an app behind a proxy
       misbehaves — so its absence is intentional, not an oversight.
    2. The app serves correctly under a non-root ``--root-path`` (passed here),
       and every URL constructed downstream preserves its trailing slash (the
       trailing slash is load-bearing).
    """
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse

    factory = store_factory or make_store

    app = FastAPI(
        title="Models and Analyses",
        root_path=root_path,
    )

    @app.get("/health")
    def health() -> dict:
        """Liveness probe — the KIND manifest ready_probe points here.

        Responds successfully WITHOUT touching the run database or the runs tree
        (binding T3): a probe that failed merely because no store is present would
        make a working app undiscoverable.
        """
        return {
            "status": "ok",
            "app": APP_ID,
            "contract_version": CONTRACT_VERSION,
        }

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        """Minimal HTML index listing the portfolio and linking into arc views."""
        return render_index_html(root_path=root_path)

    @app.get("/api/portfolio")
    def portfolio() -> dict:
        """Every project and arc with per-arc analysis counts and tiers."""
        try:
            store = factory()
        except RunsRootResolutionError as exc:
            # Misconfigured (no runs tree) is distinct from empty — say so.
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return build_portfolio(store)

    @app.get("/api/arcs/{project}/{arc}/models")
    def arc_models(project: str, arc: str) -> dict:
        """One row per model in the arc, with analyses and the agreement matrix."""
        try:
            store = factory()
        except RunsRootResolutionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return build_arc_models(store, project, arc)

    return app


# ── console entry point ──────────────────────────────────────────────────────────


def _register_manifest() -> None:
    """Self-register this app's KIND manifest (task p5 owns writing it).

    In the full build ``serve`` (without ``--no-king``) would write/refresh the
    KIND manifest into the app state directory so KIND can discover the app. That
    manifest content is task p5's responsibility; this skeleton only ensures the
    state directory exists and logs the intent, so ``--no-king`` has an observable
    thing to skip. It performs NO privileged action and needs no KING at runtime.
    """
    state_dir = resolve_app_state_dir()
    logger.info(
        "models_and_analyses: manifest self-registration point (state_dir=%s); "
        "manifest content is owned by task p5",
        state_dir,
    )


def main(argv: Optional[list] = None) -> None:
    """Console entry point: ``models-and-analyses serve <port> [--root-path P] [--no-king]``.

    On PATH with no cwd assumption and no venv-relative path, so KIND's
    ``_resolve_exe`` finds it whether KBUtilLib is a checkout, an editable install
    or a pipx install.

    Startup order:
      1. Contract-version gate (I5): if KING's contract version is visible via
         ``$KING_CONTRACT_VERSION``, compare by integer equality — any difference
         refuses startup with an error naming both versions. Absent (standalone /
         ``--no-king``) it is a no-op (I1).
      2. Manifest self-registration, unless ``--no-king`` (I1).
      3. Serve.
    """
    import os

    parser = argparse.ArgumentParser(
        prog=APP_ID,
        description="The KOROS Models and Analyses web app.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Serve the web app.")
    serve.add_argument("port", type=int, help="Port to bind.")
    serve.add_argument(
        "--root-path",
        default="",
        help="Proxy path prefix the app is served under (e.g. /apps/models). "
        "Its trailing slash is preserved on constructed URLs.",
    )
    serve.add_argument(
        "--runs-root",
        default=None,
        help="Explicit KOROS runs root; otherwise $KOROS_HOME/runs, then "
        "$KING_KOROS_RUNS (the store's pinned chain).",
    )
    serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host (default 127.0.0.1, loopback-only).",
    )
    serve.add_argument(
        "--no-king",
        action="store_true",
        default=False,
        help="Skip KIND manifest self-registration entirely (invariant I1: KING "
        "is not a runtime dependency; the app runs standalone).",
    )
    args = parser.parse_args(argv)

    # I5 — contract-version gate. KING exposes its version via
    # $KING_CONTRACT_VERSION; when unset (standalone / --no-king), the gate is a
    # no-op and the app runs (I1).
    king_version_raw = os.environ.get("KING_CONTRACT_VERSION")
    king_version: Optional[int]
    if king_version_raw is None or king_version_raw == "":
        king_version = None
    else:
        try:
            king_version = int(king_version_raw)
        except ValueError:
            print(
                f"Error: $KING_CONTRACT_VERSION must be an integer, got "
                f"{king_version_raw!r}",
                file=sys.stderr,
            )
            sys.exit(2)
    try:
        check_startup_contract_version(king_version)
    except ContractVersionMismatch as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(3)

    if not args.no_king:
        _register_manifest()

    try:
        import uvicorn  # type: ignore[import]
    except ImportError:
        print(
            "Error: uvicorn is required to serve. Install with "
            "'pip install kbutillib[api]' or 'pip install uvicorn[standard]'.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        app = build_app(
            store_factory=lambda: make_store(args.runs_root),
            root_path=args.root_path,
        )
    except ImportError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    # NOTE: proxy_headers / forwarded-allow-ips are DELIBERATELY not enabled here
    # (see build_app docstring, gotcha #1). Do not add them.
    uvicorn.run(app, host=args.host, port=args.port)
