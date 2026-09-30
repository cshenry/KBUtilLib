"""Tests for the render layer (levels 2 and 3) of the Models and Analyses app.

The render layer DELEGATES all rendering to
:class:`kbutillib.domains.notebook.escher_utils.EscherUtils`; these tests mock
those calls (``create_fitness_dashboard`` / ``create_map_html2`` /
``list_available_maps``) and NEVER assert on generated HTML content — that is code
this PRD does not own. They assert the delegation, the ``inline_escher=False``
passthrough, the disk-cache reuse/invalidation, the DEGRADE path for
unresolvable/unreadable artifacts, the deterministic map selection, and the poll
token schema/GC.

Most of the file exercises the pure ``render.py`` functions, which need no web
framework; the endpoint-shape tests use FastAPI's TestClient and are skipped when
fastapi is absent (matching the established pattern in ``test_app.py``).
"""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pytest

from kbutillib.koros_arc_store import ProjectRecord
from kbutillib.models_and_analyses.render import (
    POLL_TOKEN_RE,
    ArtifactUnavailable,
    GenerationFailed,
    MapUnavailable,
    ModelNotFound,
    RecordUnknown,
    ResolvedArtifact,
    cache_key,
    find_paired_model_build,
    find_record,
    gc_poll_tokens,
    new_poll_token,
    read_or_generate_dashboard,
    read_or_generate_escher,
    read_poll_token,
    resolve_artifact,
    select_default_map,
    write_poll_token,
)

from .conftest import make_arc, make_record

_fastapi_available = importlib.util.find_spec("fastapi") is not None
requires_fastapi = pytest.mark.skipif(
    not _fastapi_available,
    reason="fastapi not installed; install kbutillib[api] to run these tests",
)


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """Point resolve_app_state_dir at a per-test tmp dir via $KING_STATE.

    resolve_app_state_dir reads $KING_STATE at CALL time, so setting it here keeps
    every cache/poll write inside the test's tmp dir. Note the binding: the state
    dir is ``$KING_STATE/state/models-and-analyses`` (kind-apps is NOT interpolated
    inside $KING_STATE).
    """
    monkeypatch.setenv("KING_STATE", str(tmp_path))
    return tmp_path


# ── a fake EscherUtils that records its calls (no real rendering) ─────────────────


class FakeEscher:
    """Records delegate calls and writes a stub HTML file, asserting nothing about it."""

    def __init__(self, *, maps=None, fail=False):
        self._maps = maps if maps is not None else [{"name": "modelseed_core"}]
        self._fail = fail
        self.dashboard_calls = []
        self.map_calls = []
        self.list_calls = []

    def list_available_maps(self, model=None):
        self.list_calls.append(model)
        return self._maps

    def create_fitness_dashboard(self, model_result, fitness_result, map, output_path,
                                 **kwargs):
        self.dashboard_calls.append(
            {
                "model_result": model_result,
                "fitness_result": fitness_result,
                "map": map,
                "output_path": output_path,
                "kwargs": kwargs,
            }
        )
        if self._fail:
            raise RuntimeError("boom")
        Path(output_path).write_text("<html>stub dashboard</html>", encoding="utf-8")
        return output_path

    def create_map_html2(self, model, map, output_path, flux=None, **kwargs):
        self.map_calls.append(
            {"model": model, "map": map, "output_path": output_path, "flux": flux}
        )
        if self._fail:
            raise RuntimeError("boom")
        Path(output_path).write_text("<html>stub map</html>", encoding="utf-8")
        return output_path


def _artifact(tmp_path, name="model.json", content='{"x": 1}'):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return ResolvedArtifact(
        key="model", kind="path", value=str(p), mtime=p.stat().st_mtime
    )


# ── artifact resolution: DEGRADE, don't crash ────────────────────────────────────


def test_resolve_file_path_artifact(tmp_path):
    p = tmp_path / "m.json"
    p.write_text("{}", encoding="utf-8")
    art = resolve_artifact("model", str(p))
    assert art.kind == "path"
    assert art.value == str(p)
    assert art.mtime == p.stat().st_mtime


def test_resolve_file_uri_artifact(tmp_path):
    p = tmp_path / "m.json"
    p.write_text("{}", encoding="utf-8")
    art = resolve_artifact("model", f"file://{p}")
    assert art.value == str(p)


def test_resolve_missing_file_is_unavailable_not_exception(tmp_path):
    with pytest.raises(ArtifactUnavailable) as ei:
        resolve_artifact("model", str(tmp_path / "nope.json"))
    assert "not present" in str(ei.value)
    assert "model" in str(ei.value)


def test_resolve_unreadable_file_names_the_artifact(tmp_path):
    p = tmp_path / "locked.json"
    p.write_text("{}", encoding="utf-8")
    p.chmod(0o000)
    try:
        with pytest.raises(ArtifactUnavailable) as ei:
            resolve_artifact("model", str(p))
        # The error must NAME the artifact so an unreadable input is not mistaken
        # for a model with no results.
        assert "unreadable" in str(ei.value)
        assert str(p) in str(ei.value)
    finally:
        p.chmod(0o644)


def test_resolve_obj_uri_without_resolver_is_unavailable():
    """An object-store artifact with no reachable store DEGRADES, never crashes."""
    with pytest.raises(ArtifactUnavailable) as ei:
        resolve_artifact("model", "obj://abc123")
    assert "object store is not reachable" in str(ei.value)


def test_resolve_obj_uri_with_resolver(tmp_path):
    p = tmp_path / "fetched.json"
    p.write_text("{}", encoding="utf-8")
    art = resolve_artifact("model", "obj://abc", object_resolver=lambda oid: str(p))
    assert art.value == str(p)


# ── record + paired-model resolution ─────────────────────────────────────────────


def _seed(store):
    store.seed_project(ProjectRecord(name="p", path=Path("/runs/p"), arc_count=1))
    store.seed_arc(make_arc("p", "a", run_name="A"))
    return store


def test_find_record_locates_by_id_and_arc(store):
    _seed(store)
    rec = make_record("kbdl.fitness_analysis", "gX", model_uri="file:///data/mX.json")
    store.record_analysis("p", "a", rec)
    found, project, arc = find_record(store, rec.record_id)
    assert found.record_id == rec.record_id
    assert (project, arc) == ("p", "a")


def test_find_record_unknown_raises(store):
    _seed(store)
    with pytest.raises(RecordUnknown):
        find_record(store, "does-not-exist")


def test_paired_model_build_found(store):
    _seed(store)
    build = make_record("kbdl.model_build", "gX", model_uri="obj://mX", run_uid="b1")
    fit = make_record("kbdl.fitness_analysis", "gX", model_uri="obj://mX", run_uid="f1")
    store.record_analysis("p", "a", build)
    store.record_analysis("p", "a", fit)
    paired = find_paired_model_build(store, fit, "p", "a")
    assert paired.record_id == build.record_id


def test_paired_model_build_missing_raises_model_not_found(store):
    _seed(store)
    fit = make_record("kbdl.fitness_analysis", "gX", model_uri="obj://mX")
    store.record_analysis("p", "a", fit)
    with pytest.raises(ModelNotFound):
        find_paired_model_build(store, fit, "p", "a")


# ── deterministic map selection ──────────────────────────────────────────────────


def test_select_default_map_prefers_core():
    maps = [{"name": "modelseed_global"}, {"name": "modelseed_core"}, {"name": "z"}]
    assert select_default_map(maps) == "modelseed_core"


def test_select_default_map_falls_back_to_global_then_first():
    assert select_default_map([{"name": "modelseed_global"}, {"name": "z"}]) == (
        "modelseed_global"
    )
    assert select_default_map([{"name": "zeta"}, {"name": "alpha"}]) == "zeta"


def test_select_default_map_empty_raises_no_map():
    with pytest.raises(MapUnavailable):
        select_default_map([])


# ── dashboard generation: delegation + inline_escher=False + cache ───────────────


def test_dashboard_delegates_and_passes_inline_escher_false(tmp_path):
    esch = FakeEscher()
    art = _artifact(tmp_path)
    out = read_or_generate_dashboard(
        esch,
        record_id="r1",
        model_artifact=art,
        fitness_artifact=art,
        map_name="modelseed_core",
        title="T",
        subtitle="S",
    )
    assert out.exists()
    assert len(esch.dashboard_calls) == 1
    call = esch.dashboard_calls[0]
    # The endpoint renders NOTHING itself — it hands off to create_fitness_dashboard.
    assert call["map"] == "modelseed_core"
    # THE binding: inline_escher=False is what gets passed on the in-app path.
    assert call["kwargs"]["inline_escher"] is False
    # Tier/provenance ride in via title/subtitle without touching the dashboard.
    assert call["kwargs"]["title"] == "T"
    assert call["kwargs"]["subtitle"] == "S"


def test_dashboard_inline_escher_true_is_a_toggle(tmp_path):
    esch = FakeEscher()
    art = _artifact(tmp_path)
    read_or_generate_dashboard(
        esch,
        record_id="r1",
        model_artifact=art,
        fitness_artifact=art,
        map_name="m",
        title="T",
        subtitle="S",
        inline_escher=True,
    )
    assert esch.dashboard_calls[0]["kwargs"]["inline_escher"] is True


def test_dashboard_cache_reused_on_unchanged_mtimes(tmp_path):
    esch = FakeEscher()
    art = _artifact(tmp_path)
    kw = dict(
        record_id="r1", model_artifact=art, fitness_artifact=art,
        map_name="m", title="T", subtitle="S",
    )
    read_or_generate_dashboard(esch, **kw)
    read_or_generate_dashboard(esch, **kw)
    # Second call is a cache HIT — the renderer ran exactly once.
    assert len(esch.dashboard_calls) == 1


def test_dashboard_cache_invalidated_on_changed_mtime(tmp_path):
    esch = FakeEscher()
    p = tmp_path / "model.json"
    p.write_text("{}", encoding="utf-8")
    art1 = ResolvedArtifact("model", "path", str(p), p.stat().st_mtime)
    read_or_generate_dashboard(
        esch, record_id="r1", model_artifact=art1, fitness_artifact=art1,
        map_name="m", title="T", subtitle="S",
    )
    # Move the mtime forward and re-resolve — the key must change → regenerate.
    future = time.time() + 100
    import os
    os.utime(p, (future, future))
    art2 = ResolvedArtifact("model", "path", str(p), p.stat().st_mtime)
    assert art2.mtime != art1.mtime
    read_or_generate_dashboard(
        esch, record_id="r1", model_artifact=art2, fitness_artifact=art2,
        map_name="m", title="T", subtitle="S",
    )
    assert len(esch.dashboard_calls) == 2


def test_dashboard_generation_failure_names_artifacts(tmp_path):
    esch = FakeEscher(fail=True)
    art = _artifact(tmp_path)
    with pytest.raises(GenerationFailed) as ei:
        read_or_generate_dashboard(
            esch, record_id="r1", model_artifact=art, fitness_artifact=art,
            map_name="m", title="T", subtitle="S",
        )
    assert art.value in str(ei.value)
    # A failed generation must not leave a poisoned cache file behind.
    key = cache_key("r1", [art, art], suffix="dashboard:m")
    assert not (tmp_path / "state" / "models-and-analyses" / "render-cache" /
                f"{key}.html").exists()


def test_dashboard_empty_output_is_generation_failure(tmp_path, monkeypatch):
    """An empty page is indistinguishable from a model with no results → error."""
    class EmptyEscher(FakeEscher):
        def create_fitness_dashboard(self, *a, **k):
            self.dashboard_calls.append(k)
            Path(a[3]).write_text("", encoding="utf-8")
            return a[3]

    esch = EmptyEscher()
    art = _artifact(tmp_path)
    with pytest.raises(GenerationFailed) as ei:
        read_or_generate_dashboard(
            esch, record_id="r1", model_artifact=art, fitness_artifact=art,
            map_name="m", title="T", subtitle="S",
        )
    assert "empty page" in str(ei.value)


# ── single-map escher generation ─────────────────────────────────────────────────


def test_escher_delegates_with_flux(tmp_path):
    esch = FakeEscher()
    art = _artifact(tmp_path)
    flux = {"rxn1_c0": 1.5}
    out = read_or_generate_escher(
        esch, record_id="r1", model_artifact=art, map_name="modelseed_core", flux=flux
    )
    assert out.exists()
    assert len(esch.map_calls) == 1
    assert esch.map_calls[0]["flux"] == flux
    assert esch.map_calls[0]["map"] == "modelseed_core"


def test_escher_cache_reused(tmp_path):
    esch = FakeEscher()
    art = _artifact(tmp_path)
    kw = dict(record_id="r1", model_artifact=art, map_name="m")
    read_or_generate_escher(esch, **kw)
    read_or_generate_escher(esch, **kw)
    assert len(esch.map_calls) == 1


# ── poll token schema + GC (binding S18) ─────────────────────────────────────────


def test_new_poll_token_matches_schema():
    token = new_poll_token()
    assert POLL_TOKEN_RE.match(token)
    assert token.startswith("pt-")


def test_write_and_read_poll_token_schema():
    token = new_poll_token()
    write_poll_token(token, status="pending", record_id="r1", map_name="m")
    state = read_poll_token(token)
    assert state["status"] == "pending"
    assert state["record_id"] == "r1"
    assert state["map"] == "m"
    assert "updated_at" in state


def test_read_unknown_poll_token_is_none():
    assert read_poll_token("pt-00000000-0000-0000-0000-000000000000") is None
    # A malformed token never matches the schema.
    assert read_poll_token("not-a-token") is None


def test_poll_token_error_carries_message():
    token = new_poll_token()
    write_poll_token(
        token, status="error", record_id="r1", map_name="m", error="artifact X failed"
    )
    state = read_poll_token(token)
    assert state["status"] == "error"
    assert state["error"] == "artifact X failed"


def test_gc_removes_tokens_older_than_24h(tmp_path):
    old = new_poll_token()
    fresh = new_poll_token()
    write_poll_token(old, status="ready", record_id="r1", map_name="m")
    write_poll_token(fresh, status="ready", record_id="r2", map_name="m")
    # Age the old token's file past the 24h cutoff.
    old_path = tmp_path / "state" / "models-and-analyses" / "poll" / f"{old}.json"
    stale = time.time() - (25 * 3600)
    import os
    os.utime(old_path, (stale, stale))
    removed = gc_poll_tokens()
    assert removed == 1
    assert read_poll_token(old) is None
    assert read_poll_token(fresh) is not None


# ── endpoint shape (TestClient) — skipped without fastapi ─────────────────────────


def _build_client(store, esch):
    from fastapi.testclient import TestClient

    from kbutillib.models_and_analyses.app import build_app

    app = build_app(store_factory=lambda: store, escher_factory=lambda: esch)
    return TestClient(app)


@requires_fastapi
def test_maps_endpoint_surfaces_list_available_maps(store, tmp_path):
    _seed(store)
    p = tmp_path / "mX.json"
    p.write_text("{}", encoding="utf-8")
    build = make_record("kbdl.model_build", "gX", model_uri=f"file://{p}")
    store.record_analysis("p", "a", build)
    esch = FakeEscher(maps=[{"name": "modelseed_core"}, {"name": "other"}])
    client = _build_client(store, esch)
    resp = client.get("/api/maps", params={"record_id": build.record_id})
    assert resp.status_code == 200
    assert resp.json()["maps"] == [{"name": "modelseed_core"}, {"name": "other"}]
    assert esch.list_calls  # delegated to list_available_maps


@requires_fastapi
def test_dashboard_endpoint_202_then_poll_200_html(store, tmp_path):
    """Protocol: cache miss → 202 + poll token; poll → 200 HTML once ready.

    Generation runs off the request thread so the request never blocks (binding).
    """
    _seed(store)
    p = tmp_path / "mX.json"
    p.write_text("{}", encoding="utf-8")
    build = make_record("kbdl.model_build", "gX", model_uri=f"file://{p}", run_uid="b")
    fit = make_record(
        "kbdl.fitness_analysis", "gX", model_uri=f"file://{p}", run_uid="f"
    )
    store.record_analysis("p", "a", build)
    store.record_analysis("p", "a", fit)
    esch = FakeEscher()
    client = _build_client(store, esch)
    resp = client.get(f"/api/models/{fit.record_id}/dashboard")
    assert resp.status_code == 202
    token = resp.json()["poll_token"]
    assert POLL_TOKEN_RE.match(token)

    # The background thread flips the token to ready; poll until it is (bounded).
    for _ in range(50):
        pr = client.get(f"/api/render/poll/{token}")
        if pr.status_code == 200:
            break
        assert pr.status_code == 202  # pending, never an empty 200
        time.sleep(0.05)
    assert pr.status_code == 200
    assert "text/html" in pr.headers["content-type"]
    # Delegated, and the in-app path passed inline_escher=False.
    assert esch.dashboard_calls[0]["kwargs"]["inline_escher"] is False

    # A second request is now a cache HIT → served 200 directly, no new token.
    resp2 = client.get(f"/api/models/{fit.record_id}/dashboard")
    assert resp2.status_code == 200
    assert len(esch.dashboard_calls) == 1  # renderer ran exactly once


@requires_fastapi
def test_dashboard_endpoint_unavailable_artifacts_no_exception(store, tmp_path):
    _seed(store)
    # An obj:// artifact with no reachable store → 409 inputs-unavailable, not 500.
    build = make_record("kbdl.model_build", "gX", model_uri="obj://mX", run_uid="b")
    fit = make_record("kbdl.fitness_analysis", "gX", model_uri="obj://mX", run_uid="f")
    store.record_analysis("p", "a", build)
    store.record_analysis("p", "a", fit)
    client = _build_client(store, FakeEscher())
    resp = client.get(f"/api/models/{fit.record_id}/dashboard")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "inputs-unavailable"


@requires_fastapi
def test_dashboard_endpoint_404_model_not_found(store, tmp_path):
    _seed(store)
    p = tmp_path / "mX.json"
    p.write_text("{}", encoding="utf-8")
    # A fitness record with NO paired build in the arc.
    fit = make_record("kbdl.fitness_analysis", "gX", model_uri=f"file://{p}")
    store.record_analysis("p", "a", fit)
    client = _build_client(store, FakeEscher())
    resp = client.get(f"/api/models/{fit.record_id}/dashboard")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "model-not-found"


@requires_fastapi
def test_dashboard_endpoint_404_no_map(store, tmp_path):
    _seed(store)
    p = tmp_path / "mX.json"
    p.write_text("{}", encoding="utf-8")
    build = make_record("kbdl.model_build", "gX", model_uri=f"file://{p}", run_uid="b")
    fit = make_record(
        "kbdl.fitness_analysis", "gX", model_uri=f"file://{p}", run_uid="f"
    )
    store.record_analysis("p", "a", build)
    store.record_analysis("p", "a", fit)
    esch = FakeEscher(maps=[])  # no maps → 404 no-map
    client = _build_client(store, esch)
    resp = client.get(f"/api/models/{fit.record_id}/dashboard")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "no-map"


@requires_fastapi
def test_poll_unknown_token_is_404(store):
    client = _build_client(store, FakeEscher())
    resp = client.get("/api/render/poll/pt-00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


@requires_fastapi
def test_poll_ready_serves_html(store, tmp_path):
    # Pre-seed a ready token pointing at an existing HTML file.
    html = tmp_path / "ready.html"
    html.write_text("<html>ready</html>", encoding="utf-8")
    token = new_poll_token()
    write_poll_token(
        token, status="ready", record_id="r1", map_name="m", html_path=str(html)
    )
    client = _build_client(store, FakeEscher())
    resp = client.get(f"/api/render/poll/{token}")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


@requires_fastapi
def test_poll_error_is_500_naming_the_artifact(store):
    token = new_poll_token()
    write_poll_token(
        token, status="error", record_id="r1", map_name="m",
        error="dashboard generation failed for record 'r1' (model=/x)",
    )
    client = _build_client(store, FakeEscher())
    resp = client.get(f"/api/render/poll/{token}")
    assert resp.status_code == 500
    assert "model=/x" in resp.json()["detail"]["message"]
