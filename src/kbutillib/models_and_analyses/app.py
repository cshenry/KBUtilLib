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
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from ..arc_context import APP_ID, app_us
from ..koros_arc_store import CONTRACT_VERSION
from ..koros_arc_store.exceptions import (
    ContractVersionMismatch,
    RunsRootResolutionError,
)
from .render import (
    DEFAULT_INLINE_ESCHER,
    ArtifactUnavailable,
    GenerationFailed,
    MapUnavailable,
    ModelNotFound,
    RecordUnknown,
    ResolvedArtifact,
    dashboard_cache_lookup,
    escher_cache_lookup,
    find_paired_model_build,
    find_record,
    gc_poll_tokens,
    invalidate_record_cache,
    list_maps_for_record,
    locate_analysis,
    new_poll_token,
    read_or_generate_dashboard,
    read_or_generate_escher,
    read_poll_token,
    resolve_artifact,
    select_default_map,
    write_poll_token,
)
from .service import (
    FITNESS_ANALYSIS_KIND,
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


def _default_escher_factory() -> Any:
    """Construct the real :class:`EscherUtils` (imported lazily).

    Imported inside the function so importing this module never drags in the
    heavy scientific stack. Tests inject a fake factory returning a mock with
    ``create_fitness_dashboard`` / ``create_map_html2`` / ``list_available_maps``.
    """
    from ..domains.notebook.escher_utils import EscherUtils

    return EscherUtils()


def _render_title_subtitle(
    record: Any,
    project: Optional[str],
    arc: Optional[str],
) -> "tuple[str, str]":
    """Build dashboard header text carrying trust tier and arc provenance.

    Binding: the record's ``trust_tier`` and arc provenance must reach the
    rendered view's surrounding chrome so a reader can see everything in it is
    model-generated hypothesis-tier output and which arc it came from. This does
    NOT touch the dashboard internals — ``create_fitness_dashboard`` already takes
    ``title`` and ``subtitle`` arguments, so tier/provenance ride in through them.
    """
    tier = getattr(record, "trust_tier", None) or "hypothesis"
    subject = getattr(record, "subject", "") or ""
    title = f"Fitness · Model dashboard — {subject}".rstrip(" —")
    where = f"{project}/{arc}" if project and arc else "unattributed"
    subtitle = (
        f"trust tier: {tier} (model-generated) · arc: {where} · "
        f"record {getattr(record, 'record_id', '')}"
    )
    return title, subtitle


def _resolve_dashboard_inputs(
    store: Any,
    record_id: str,
) -> "tuple[Any, Any, ResolvedArtifact, ResolvedArtifact, Optional[str], Optional[str]]":
    """Resolve a dashboard request to its records and readable artifacts.

    ``record_id`` names a ``kbdl.fitness_analysis`` record (binding). Returns the
    fitness record, the paired model_build record, the resolved model and fitness
    artifacts, and the ``(project, arc)`` the record was found under. Raises the
    render layer's typed errors (:class:`RecordUnknown`, :class:`ModelNotFound`,
    :class:`ArtifactUnavailable`) which the endpoint maps to status codes.

    This is the ONLY level allowed to read record artifacts for rendering (blob
    discipline). It uses ``list_analyses`` to LOCATE the record; artifact URIs
    live on the record's summary columns.
    """
    fitness_rec, project, arc = find_record(store, record_id)
    model_rec = find_paired_model_build(store, fitness_rec, project, arc)

    model_uri = (model_rec.artifacts or {}).get("model_id")
    fitness_uri = (fitness_rec.artifacts or {}).get("model_id")
    if not model_uri:
        raise ArtifactUnavailable(
            f"model record {model_rec.record_id!r} has no model_id artifact"
        )
    if not fitness_uri:
        raise ArtifactUnavailable(
            f"fitness record {record_id!r} has no model_id artifact"
        )
    model_art = resolve_artifact("model", model_uri)
    fitness_art = resolve_artifact("fitness", fitness_uri)
    return fitness_rec, model_rec, model_art, fitness_art, project, arc


def build_app(
    store_factory: Optional[Callable[[], Any]] = None,
    *,
    root_path: str = "",
    escher_factory: Optional[Callable[[], Any]] = None,
    inline_escher: bool = DEFAULT_INLINE_ESCHER,
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
    import threading

    from fastapi import FastAPI, HTTPException, Query
    from fastapi.responses import HTMLResponse, JSONResponse

    factory = store_factory or make_store
    make_escher = escher_factory or _default_escher_factory

    app = FastAPI(
        title="Models and Analyses",
        root_path=root_path,
    )

    # GC stale poll tokens at startup (binding S18: tokens older than 24h removed).
    try:
        gc_poll_tokens()
    except OSError:
        # A missing/unwritable state dir must not stop the app from starting.
        logger.warning("models_and_analyses: could not GC poll tokens at startup")

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

    # ── run history + delete (dated re-runs grouped by analysis_id) ─────────────

    @app.get("/api/analyses/{analysis_id}/runs")
    def analysis_runs(analysis_id: str) -> dict:
        """Every run of one analysis, newest first — the expanded grouped-row view.

        Re-runs of one analysis are kept as distinct dated rows sharing an
        ``analysis_id``. This is the view behind a collapsed arc-table row: it
        returns every run of exactly this analysis and no run of any other,
        newest-first (the store's S25 tie-break), each carrying the fields the UI
        dates and identifies a run by.

        ``list_analyses`` is keyed on ``(project, arc)`` (there is no
        ``analysis_id``-only query on the store), so this first LOCATES the
        analysis by finding any one of its runs across the portfolio, then filters
        that run's ``(project, arc)`` bucket by ``analysis_id``. An analysis_id
        with no runs anywhere is 404 — not an empty 200 that would hide a typo.
        """
        try:
            store = factory()
        except RunsRootResolutionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        located = locate_analysis(store, analysis_id)
        if located is None:
            raise HTTPException(
                status_code=404,
                detail=f"no analysis with id {analysis_id!r}",
            )
        project, arc = located
        runs = store.list_analyses(project, arc, analysis_id=analysis_id)
        return {
            "analysis_id": analysis_id,
            "run_count": len(runs),
            "runs": [
                {
                    "record_id": run.record_id,
                    "run_uid": run.run_uid,
                    "created_at": run.created_at,
                    "status": run.status,
                    "trust_tier": getattr(run, "trust_tier", None) or "hypothesis",
                }
                for run in runs
            ],
        }

    @app.delete("/api/runs/{record_id}")
    def delete_run(record_id: str) -> dict:
        """Delete exactly one run, invalidate its cache, and report runs remaining.

        Protocol (binding):
          * The run is located by ``record_id`` FIRST. If it does not resolve, 404
            — delete never falls back to "the newest" run of anything.
          * Its ``analysis_id`` is captured BEFORE the delete so the response can
            report how many runs of that analysis remain afterwards. Deleting the
            LAST run of an analysis removes the analysis from every table and
            count; the ``remaining_run_count`` of 0 is what lets the UI warn before
            that happens.
          * ``store.delete_record`` returns ``False`` (never raises) for an unknown
            id; that maps to 404, NOT 500.
          * The deleted record_id's generated-HTML cache entry is invalidated
            INSIDE this path — a re-created record_id must never be served a stale
            page. Cache invalidation does not raise and never blocks the delete.
        """
        try:
            store = factory()
        except RunsRootResolutionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        # Resolve the run first — the analysis it belongs to must be known BEFORE
        # the row is gone, both to report the remaining count and to refuse to
        # delete a run the app cannot identify.
        try:
            record, project, arc = find_record(store, record_id)
        except RecordUnknown as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        analysis_id = record.analysis_id

        deleted = store.delete_record(record_id)
        if not deleted:
            # The row vanished between resolve and delete (a concurrent delete).
            # False is not an error condition on the store — surface it as 404,
            # never a 500.
            raise HTTPException(
                status_code=404,
                detail=f"no run record with id {record_id!r}",
            )

        # Cache invalidation is part of the delete, not a follow-up. A record_id
        # with no generated page invalidates zero entries — that is fine.
        invalidate_record_cache(record_id)

        # How many runs of this analysis remain? Zero means the analysis itself is
        # now gone from every table and count (there is no delete_analysis; the
        # last run going is how an analysis disappears). The UI warns on zero.
        remaining = store.list_analyses(project, arc, analysis_id=analysis_id)
        return {
            "deleted": record_id,
            "analysis_id": analysis_id,
            "remaining_run_count": len(remaining),
        }

    # ── render endpoints (levels 2 and 3) ──────────────────────────────────────

    @app.get("/api/maps")
    def maps(record_id: str = Query(...)) -> dict:
        """List the maps usable for a record's model (delegates to list_available_maps).

        Resolves the record's paired model build, loads the model artifact and
        surfaces :meth:`EscherUtils.list_available_maps`. Degrades: an
        unresolvable artifact returns a clear ``inputs unavailable`` response
        (409), not a stack trace.
        """
        try:
            store = factory()
        except RunsRootResolutionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        try:
            fitness_rec, project, arc = find_record(store, record_id)
        except RecordUnknown as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        # A record that is itself a model build can list maps directly; a fitness
        # record resolves to its paired build.
        try:
            if fitness_rec.kind == FITNESS_ANALYSIS_KIND:
                model_rec = find_paired_model_build(store, fitness_rec, project, arc)
            else:
                model_rec = fitness_rec
        except ModelNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail={"error": "model-not-found", "message": str(exc)},
            ) from exc
        model_uri = (model_rec.artifacts or {}).get("model_id")
        if not model_uri:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "inputs-unavailable",
                    "message": f"model record {model_rec.record_id!r} has no model_id",
                },
            )
        try:
            model_art = resolve_artifact("model", model_uri)
        except ArtifactUnavailable as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": "inputs-unavailable", "message": str(exc)},
            ) from exc
        escher = make_escher()
        available = list_maps_for_record(escher, model_art.value)
        return {"record_id": record_id, "maps": available}

    def _start_generation(
        token: str,
        record_id: str,
        map_name: Optional[str],
        work: Callable[[], Any],
    ) -> None:
        """Run a generation callable on a background thread, updating the token.

        Generation takes seconds; holding the request open would time out against
        the 45s ready probe (binding). So the request writes a ``pending`` token
        and returns 202; this thread does the work and flips the token to
        ``ready`` or ``error`` (naming the artifact on failure).
        """
        write_poll_token(
            token, status="pending", record_id=record_id, map_name=map_name
        )

        def _run() -> None:
            try:
                html_path = work()
            except GenerationFailed as exc:
                write_poll_token(
                    token, status="error", record_id=record_id,
                    map_name=map_name, error=str(exc),
                )
            except Exception as exc:  # noqa: BLE001 - record, never crash the thread
                write_poll_token(
                    token, status="error", record_id=record_id,
                    map_name=map_name, error=repr(exc),
                )
            else:
                write_poll_token(
                    token, status="ready", record_id=record_id,
                    map_name=map_name, html_path=str(html_path),
                )

        threading.Thread(target=_run, daemon=True).start()

    @app.get("/api/models/{record_id}/dashboard")
    def dashboard(record_id: str) -> Any:
        """Render (or serve cached) the fitness/model dashboard for a fitness record.

        Delegates ALL rendering to :meth:`EscherUtils.create_fitness_dashboard`.
        Protocol (binding): 200-with-HTML when cached/ready, 202-with-token while
        generating, 404 model-not-found when no paired build exists, 409
        inputs-unavailable when artifacts do not resolve. Never returns an empty
        200 — an empty page is indistinguishable from a model with no results.
        """
        try:
            store = factory()
        except RunsRootResolutionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        try:
            (
                fitness_rec, _model_rec, model_art, fitness_art, project, arc
            ) = _resolve_dashboard_inputs(store, record_id)
        except RecordUnknown as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ModelNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail={"error": "model-not-found", "message": str(exc)},
            ) from exc
        except ArtifactUnavailable as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": "inputs-unavailable", "message": str(exc)},
            ) from exc

        escher = make_escher()
        try:
            available = list_maps_for_record(escher, model_art.value)
            map_name = select_default_map(available)
        except MapUnavailable as exc:
            raise HTTPException(
                status_code=404,
                detail={"error": "no-map", "message": str(exc)},
            ) from exc

        title, subtitle = _render_title_subtitle(fitness_rec, project, arc)

        # Fast path: already cached → serve 200 immediately, no thread, no token.
        cached = dashboard_cache_lookup(
            record_id=record_id,
            model_artifact=model_art,
            fitness_artifact=fitness_art,
            map_name=map_name,
        )
        if cached is not None:
            return HTMLResponse(cached.read_text(encoding="utf-8"))

        # Cache miss: generation takes seconds. Do NOT hold the request through it
        # (binding: the iframe must not time out against the 45s ready probe).
        # Hand it to a background thread and return a 202 poll token.
        def work() -> Path:
            return read_or_generate_dashboard(
                escher,
                record_id=record_id,
                model_artifact=model_art,
                fitness_artifact=fitness_art,
                map_name=map_name,
                title=title,
                subtitle=subtitle,
                inline_escher=inline_escher,
            )

        token = new_poll_token()
        _start_generation(token, record_id, map_name, work)
        return JSONResponse(
            status_code=202,
            content={"status": "pending", "poll_token": token, "map": map_name},
        )

    @app.get("/api/models/{record_id}/escher")
    def escher_map(
        record_id: str,
        map: Optional[str] = Query(default=None),
    ) -> Any:
        """Render (or serve cached) a single Escher map with flux for a record.

        Delegates to :meth:`EscherUtils.create_map_html2` with flux from the
        selected FBA/FVA record. Same 200/202/404/409 protocol as the dashboard.
        """
        try:
            store = factory()
        except RunsRootResolutionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        try:
            rec, project, arc = find_record(store, record_id)
        except RecordUnknown as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        # Resolve the model: an FBA/FVA/fitness record carries its model_id; a
        # build record is its own model.
        model_uri = (rec.artifacts or {}).get("model_id")
        if not model_uri:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "inputs-unavailable",
                    "message": f"record {record_id!r} has no model_id artifact",
                },
            )
        try:
            model_art = resolve_artifact("model", model_uri)
        except ArtifactUnavailable as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": "inputs-unavailable", "message": str(exc)},
            ) from exc

        escher = make_escher()
        try:
            if map:
                map_name = map
            else:
                available = list_maps_for_record(escher, model_art.value)
                map_name = select_default_map(available)
        except MapUnavailable as exc:
            raise HTTPException(
                status_code=404,
                detail={"error": "no-map", "message": str(exc)},
            ) from exc

        # Flux comes from the selected record's detail blob (the render endpoints
        # are the ONLY levels permitted to call read_detail — blob discipline).
        flux = None
        try:
            detail = store.read_detail(record_id)
        except Exception:  # noqa: BLE001 - a missing/unreadable blob just means no flux
            detail = None
        if isinstance(detail, dict):
            candidate = detail.get("flux")
            if isinstance(candidate, dict):
                flux = candidate

        cached = escher_cache_lookup(
            record_id=record_id,
            model_artifact=model_art,
            map_name=map_name,
        )
        if cached is not None:
            return HTMLResponse(cached.read_text(encoding="utf-8"))

        def work() -> Path:
            return read_or_generate_escher(
                escher,
                record_id=record_id,
                model_artifact=model_art,
                map_name=map_name,
                flux=flux,
            )

        token = new_poll_token()
        _start_generation(token, record_id, map_name, work)
        return JSONResponse(
            status_code=202,
            content={"status": "pending", "poll_token": token, "map": map_name},
        )

    @app.get("/api/render/poll/{token}")
    def poll(token: str) -> Any:
        """Poll a generation token (binding protocol S18).

        200-with-HTML when ready, 202 while pending, 404 on an unknown token, 500
        naming the failing artifact on error. Never an empty 200.
        """
        state = read_poll_token(token)
        if state is None:
            raise HTTPException(status_code=404, detail="unknown poll token")
        status = state.get("status")
        if status == "pending":
            return JSONResponse(status_code=202, content=state)
        if status == "error":
            raise HTTPException(
                status_code=500,
                detail={"error": "generation-failed", "message": state.get("error")},
            )
        # ready → serve the cached HTML the generation thread recorded on the token.
        html_path = state.get("html_path")
        if not html_path or not Path(html_path).exists():
            # The cache entry vanished (evicted / state dir wiped). Treat as gone
            # rather than serving an empty 200.
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "generation-failed",
                    "message": (
                        f"cached HTML for token {token!r} is missing at {html_path!r}"
                    ),
                },
            )
        text = Path(html_path).read_text(encoding="utf-8")
        if not text:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "generation-failed",
                    "message": f"cached HTML for token {token!r} is empty",
                },
            )
        return HTMLResponse(text)

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
