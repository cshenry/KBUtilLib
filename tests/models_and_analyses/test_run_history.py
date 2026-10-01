"""Run-history (GET runs) and delete (DELETE run) endpoint tests.

These exercise the two affordances added for "runs should be dated and grouped,
and you should be able to delete a run from the database" (Chris, 2026-09-24):

  * GET  /api/analyses/{analysis_id}/runs — the expanded view behind a collapsed
    arc-table row: every dated run of ONE analysis, newest first, and no run of
    any other analysis.
  * DELETE /api/runs/{record_id} — remove exactly the named run, invalidate its
    generated-HTML cache entry, and report how many runs of the analysis remain
    (zero means the analysis itself is now gone — the last-run case the UI warns
    on).

Every store here is the SHIPPED :class:`FakeKorosArcStore` (binding T4) — never a
hand-written double. Counts asserted are DISTINCT on ``analysis_id``: an arc with
one analysis re-run six times shows an analysis count of one and a run count of
six.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from kbutillib.koros_arc_store import ProjectRecord
from kbutillib.koros_arc_store_testing import FakeKorosArcStore
from kbutillib.models_and_analyses import build_arc_models
from kbutillib.models_and_analyses.render import (
    ResolvedArtifact,
    invalidate_record_cache,
    locate_analysis,
    read_or_generate_dashboard,
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

    Keeps every render-cache / cache-index write inside the test's tmp dir, exactly
    as ``test_render.py``'s fixture does.
    """
    monkeypatch.setenv("KING_STATE", str(tmp_path))
    return tmp_path


# ── a fake EscherUtils that writes a stub page (mirrors test_render.py) ───────────


class _FakeEscher:
    def list_available_maps(self, model=None):
        return [{"name": "modelseed_core"}]

    def create_fitness_dashboard(self, model_result, fitness_result, map, output_path,
                                 **kwargs):
        Path(output_path).write_text("<html>stub</html>", encoding="utf-8")
        return output_path

    def create_map_html2(self, model, map, output_path, flux=None, **kwargs):
        Path(output_path).write_text("<html>stub</html>", encoding="utf-8")
        return output_path


def _artifact(tmp_path, name="model.json"):
    p = tmp_path / name
    p.write_text('{"x": 1}', encoding="utf-8")
    return ResolvedArtifact(
        key="model", kind="path", value=str(p), mtime=p.stat().st_mtime
    )


# ── seeding helper: N dated re-runs of ONE analysis in one arc ────────────────────


def _seed_reruns(store, project, arc, *, kind, subject, count, model_uri=None):
    """Record ``count`` dated re-runs of one analysis (shared analysis_id).

    Each run gets a distinct ``run_uid`` (→ distinct ``record_id``) and a distinct
    ``created_at`` so the S25 tie-break orders them deterministically. Returns the
    list of record_ids in creation order (OLDEST first).
    """
    record_ids = []
    for i in range(count):
        rec = make_record(
            kind,
            subject,
            model_uri=model_uri,
            run_uid=f"u{i}",
            created_at=f"2026-09-2{i}T10:00:00.000000Z",
        )
        store.record_analysis(project, arc, rec)
        record_ids.append(rec.record_id)
    return record_ids


# ── GET /api/analyses/{analysis_id}/runs ──────────────────────────────────────────


@requires_fastapi
def test_get_runs_returns_every_run_newest_first_and_only_this_analysis():
    from fastapi.testclient import TestClient

    from kbutillib.models_and_analyses.app import build_app

    store = FakeKorosArcStore()
    store.seed_arc(make_arc("p", "a", run_name="A"))
    # Six dated re-runs of one analysis, plus a DIFFERENT analysis in the same arc.
    mine = _seed_reruns(
        store, "p", "a", kind="kbutillib.fba", subject="genomeX",
        model_uri="obj://mX", count=6,
    )
    other = _seed_reruns(
        store, "p", "a", kind="kbutillib.fba", subject="genomeY",
        model_uri="obj://mY", count=2,
    )
    # Derive the analysis_id of the six-run analysis from one of its records.
    six_analysis_id = next(
        r.analysis_id for r in store.list_analyses("p", "a") if r.record_id in mine
    )
    other_ids = set(other)

    app = build_app(store_factory=lambda: store)
    client = TestClient(app)
    resp = client.get(f"/api/analyses/{six_analysis_id}/runs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["analysis_id"] == six_analysis_id
    # An arc with one analysis re-run six times: run count of six.
    assert body["run_count"] == 6
    returned = [r["record_id"] for r in body["runs"]]
    # Every run of THIS analysis, and no run of any other.
    assert set(returned) == set(mine)
    assert other_ids.isdisjoint(returned)
    # Newest first (created_at DESC).
    created = [r["created_at"] for r in body["runs"]]
    assert created == sorted(created, reverse=True)
    # Each run carries the fields the UI dates/identifies a run by.
    for run in body["runs"]:
        assert set(run) == {
            "record_id", "run_uid", "created_at", "status", "trust_tier"
        }


@requires_fastapi
def test_get_runs_unknown_analysis_is_404():
    from fastapi.testclient import TestClient

    from kbutillib.models_and_analyses.app import build_app

    store = FakeKorosArcStore()
    store.seed_arc(make_arc("p", "a", run_name="A"))
    app = build_app(store_factory=lambda: store)
    client = TestClient(app)
    resp = client.get("/api/analyses/does-not-exist/runs")
    assert resp.status_code == 404


@requires_fastapi
def test_get_runs_finds_unattributed_analysis():
    """An analysis with no project/arc (the unattributed bucket) is reachable too."""
    from fastapi.testclient import TestClient

    from kbutillib.models_and_analyses.app import build_app

    store = FakeKorosArcStore()
    ids = _seed_reruns(
        store, None, None, kind="kbutillib.fba", subject="/data/mU.json",
        model_uri="file:///data/mU.json", count=3,
    )
    analysis_id = next(
        r.analysis_id for r in store.list_analyses(None, None) if r.record_id in ids
    )
    app = build_app(store_factory=lambda: store)
    client = TestClient(app)
    resp = client.get(f"/api/analyses/{analysis_id}/runs")
    assert resp.status_code == 200
    assert resp.json()["run_count"] == 3


# ── DELETE /api/runs/{record_id} ──────────────────────────────────────────────────


@requires_fastapi
def test_delete_removes_named_run_and_leaves_siblings_intact():
    from fastapi.testclient import TestClient

    from kbutillib.models_and_analyses.app import build_app

    store = FakeKorosArcStore()
    store.seed_arc(make_arc("p", "a", run_name="A"))
    ids = _seed_reruns(
        store, "p", "a", kind="kbutillib.fba", subject="genomeX",
        model_uri="obj://mX", count=3,
    )
    target = ids[1]  # the middle run
    analysis_id = next(
        r.analysis_id for r in store.list_analyses("p", "a") if r.record_id == target
    )

    app = build_app(store_factory=lambda: store)
    client = TestClient(app)
    resp = client.delete(f"/api/runs/{target}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == target
    assert body["analysis_id"] == analysis_id
    # Two siblings remain — NOT the last-run case.
    assert body["remaining_run_count"] == 2

    remaining = {r.record_id for r in store.list_analyses("p", "a")}
    assert target not in remaining
    assert {ids[0], ids[2]} <= remaining


@requires_fastapi
def test_delete_unknown_record_id_is_404_not_500():
    from fastapi.testclient import TestClient

    from kbutillib.models_and_analyses.app import build_app

    store = FakeKorosArcStore()
    store.seed_arc(make_arc("p", "a", run_name="A"))
    _seed_reruns(
        store, "p", "a", kind="kbutillib.fba", subject="genomeX",
        model_uri="obj://mX", count=1,
    )
    app = build_app(store_factory=lambda: store)
    client = TestClient(app)
    resp = client.delete("/api/runs/no-such-record")
    # delete_record returns False for an unknown id (never raises); that must be a
    # 404, never a 500 (a False translated to a server error).
    assert resp.status_code == 404


@requires_fastapi
def test_delete_last_run_reports_zero_and_analysis_disappears():
    """The last-run case: deleting the last run of an analysis reports remaining 0
    and the analysis vanishes from the arc table and its counts."""
    from fastapi.testclient import TestClient

    from kbutillib.models_and_analyses.app import build_app

    store = FakeKorosArcStore()
    store.seed_arc(make_arc("p", "a", run_name="A"))
    # A model build (so the arc has a model row) plus a single-run fitness analysis.
    build = make_record("kbdl.model_build", "genomeX", model_uri="obj://mX")
    store.record_analysis("p", "a", build)
    fit = make_record(
        "kbdl.fitness_analysis", "genomeX", model_uri="obj://mX", run_uid="only"
    )
    store.record_analysis("p", "a", fit)

    # Before: the arc reports two analyses (build + fitness).
    before = build_arc_models(store, "p", "a")
    analysis_ids_before = {
        a["analysis_id"] for row in before["models"] for a in row["analyses"]
    }
    assert fit.analysis_id in analysis_ids_before

    app = build_app(store_factory=lambda: store)
    client = TestClient(app)
    resp = client.delete(f"/api/runs/{fit.record_id}")
    assert resp.status_code == 200
    assert resp.json()["remaining_run_count"] == 0

    # After: the fitness analysis is gone from every table and count.
    after = build_arc_models(store, "p", "a")
    analysis_ids_after = {
        a["analysis_id"] for row in after["models"] for a in row["analyses"]
    }
    assert fit.analysis_id not in analysis_ids_after


@requires_fastapi
def test_delete_invalidates_generated_html_cache_entry():
    """Deleting a run unlinks that run's cached generated HTML, so a re-created
    record_id is never served a stale page."""
    from fastapi.testclient import TestClient

    from kbutillib.models_and_analyses.app import build_app

    store = FakeKorosArcStore()
    store.seed_arc(make_arc("p", "a", run_name="A"))
    fit = make_record(
        "kbdl.fitness_analysis", "genomeX", model_uri="obj://mX", run_uid="only"
    )
    store.record_analysis("p", "a", fit)

    # Generate a cached page for this record so there is a cache entry to invalidate.
    art = _artifact(_isolated_state_tmp())
    out = read_or_generate_dashboard(
        _FakeEscher(),
        record_id=fit.record_id,
        model_artifact=art,
        fitness_artifact=art,
        map_name="modelseed_core",
        title="t",
        subtitle="s",
    )
    assert out.exists()

    app = build_app(store_factory=lambda: store)
    client = TestClient(app)
    resp = client.delete(f"/api/runs/{fit.record_id}")
    assert resp.status_code == 200
    # The cache file is gone after the delete.
    assert not out.exists()


def _isolated_state_tmp() -> Path:
    """Return the render-cache's sibling tmp for artifact files.

    The autouse fixture pins $KING_STATE to the test's tmp_path; artifacts just
    need to live on disk with a stable mtime, so put them beside the state dir.
    """
    import os

    base = Path(os.environ["KING_STATE"])
    base.mkdir(parents=True, exist_ok=True)
    return base


# ── locate_analysis (the store-scan behind the GET endpoint) ──────────────────────


def test_locate_analysis_finds_bucket_and_none_for_unknown():
    store = FakeKorosArcStore()
    store.seed_arc(make_arc("p", "a", run_name="A"))
    rec = make_record("kbutillib.fba", "genomeX", model_uri="obj://mX")
    store.record_analysis("p", "a", rec)
    assert locate_analysis(store, rec.analysis_id) == ("p", "a")
    assert locate_analysis(store, "nope") is None


def test_locate_analysis_finds_unattributed_bucket():
    store = FakeKorosArcStore()
    rec = make_record(
        "kbutillib.fba", "/data/mU.json", model_uri="file:///data/mU.json"
    )
    store.record_analysis(None, None, rec)
    assert locate_analysis(store, rec.analysis_id) == (None, None)


# ── cache index / invalidate_record_cache unit behaviour ──────────────────────────


def test_invalidate_record_cache_is_noop_for_never_rendered_record():
    """A record with no generated page invalidates zero entries and does not raise."""
    assert invalidate_record_cache("never-rendered") == 0


def test_invalidate_record_cache_unlinks_only_the_named_record(tmp_path):
    """Two records generate pages; invalidating one leaves the other's cache."""
    a = _artifact(_isolated_state_tmp(), name="a.json")
    b = _artifact(_isolated_state_tmp(), name="b.json")
    esch = _FakeEscher()
    out_a = read_or_generate_dashboard(
        esch, record_id="recA", model_artifact=a, fitness_artifact=a,
        map_name="m", title="t", subtitle="s",
    )
    out_b = read_or_generate_dashboard(
        esch, record_id="recB", model_artifact=b, fitness_artifact=b,
        map_name="m", title="t", subtitle="s",
    )
    assert out_a.exists() and out_b.exists()

    removed = invalidate_record_cache("recA")
    assert removed == 1
    assert not out_a.exists()
    assert out_b.exists()
    # A second invalidation of the same record is a no-op (index already pruned).
    assert invalidate_record_cache("recA") == 0
