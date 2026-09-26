"""App-level tests: proxy-setting discipline, standalone I1, health, schema.

The proxy-setting assertions, the standalone I1 proof and the API schema
assertions run WITHOUT fastapi installed (they inspect source and exercise the
pure data layer). The FastAPI TestClient tests are skipped when fastapi is
absent, matching ``tests/interfaces/test_api.py``'s established pattern.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

from kbutillib.arc_context import APP_ID
from kbutillib.koros_arc_store import CONTRACT_VERSION
from kbutillib.koros_arc_store_testing import FakeKorosArcStore
from kbutillib.models_and_analyses import build_arc_models, build_portfolio
from kbutillib.models_and_analyses.app import render_index_html

from .conftest import make_arc, make_record

_APP_PY = Path(__file__).resolve().parent.parent.parent / "src" / "kbutillib" / (
    "models_and_analyses"
) / "app.py"

_fastapi_available = importlib.util.find_spec("fastapi") is not None
requires_fastapi = pytest.mark.skipif(
    not _fastapi_available,
    reason="fastapi not installed; install kbutillib[api] to run these tests",
)


# ── proxy-setting discipline (gotcha #1) — runs without fastapi ──────────────────


def test_app_never_sets_forwarded_allow_ips_or_proxy_headers():
    """The app must NOT set FORWARDED_ALLOW_IPS or UVICORN_PROXY_HEADERS, and must
    not enable uvicorn's proxy_headers. Behind KIND's double proxy those rewrite
    asset URLs and render the iframe blank. Asserted by AST so a string in a
    comment/docstring is not a false positive."""
    source = _APP_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # 1. No os.environ assignment to the two forbidden env vars.
    forbidden_env = {"FORWARDED_ALLOW_IPS", "UVICORN_PROXY_HEADERS"}
    for node in ast.walk(tree):
        # os.environ["X"] = ... / os.environ.setdefault("X", ...)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Attribute)
                    and target.value.attr == "environ"
                ):
                    key = getattr(target.slice, "value", None)
                    assert key not in forbidden_env, (
                        f"app.py assigns os.environ[{key!r}] — forbidden"
                    )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "setdefault":
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and arg.value in forbidden_env:
                        pytest.fail(f"app.py setdefault {arg.value!r} — forbidden")

    # 2. uvicorn.run(...) must not pass proxy_headers=True or forwarded_allow_ips.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
        ):
            kwargs = {kw.arg for kw in node.keywords if kw.arg}
            assert "proxy_headers" not in kwargs, "uvicorn.run must not set proxy_headers"
            assert "forwarded_allow_ips" not in kwargs, (
                "uvicorn.run must not set forwarded_allow_ips"
            )


# ── trailing-slash discipline (gotcha #2) — runs without fastapi ─────────────────


def test_index_preserves_trailing_slash_under_root_path():
    html = render_index_html(root_path="/apps/models")
    # The JS base must keep exactly one trailing slash — load-bearing.
    assert "'/apps/models/'" in html or '"/apps/models/"' in html
    # And URLs join onto it as "api/portfolio" (relative to the slash-terminated base).
    assert 'BASE + "api/portfolio"' in html


def test_index_root_path_empty_is_root_slash():
    html = render_index_html(root_path="")
    assert "'/'" in html


# ── I1: standalone (no KING) proof — runs without fastapi ────────────────────────


def test_portfolio_works_standalone_with_no_king(monkeypatch):
    """I1 proof: with no KING present and no KING env vars, /api/portfolio's data
    layer produces a working result. This IS the standalone proof, as a test."""
    for var in ("KING_STATE", "KING_CONTRACT_VERSION", "KOROS_ARC"):
        monkeypatch.delenv(var, raising=False)
    store = FakeKorosArcStore()
    store.seed_arc(make_arc("p", "a", run_name="A"))
    store.record_analysis(
        "p", "a", make_record("kbdl.model_build", "g", model_uri="obj://m")
    )
    portfolio = build_portfolio(store)
    assert portfolio["projects"][0]["name"] == "p"
    # Degrades to "everything readable" — no user identity required.
    assert portfolio["app"]["id"] == APP_ID


# ── API response schema is part of the contract ──────────────────────────────────


def test_portfolio_schema(seeded_store):
    portfolio = build_portfolio(seeded_store)
    assert set(portfolio) == {"app", "counts_are_distinct_on", "projects", "unattributed"}
    assert set(portfolio["app"]) == {"id", "module", "contract_version"}
    proj = portfolio["projects"][0]
    assert set(proj) == {"name", "arc_count", "arcs"}
    arc = proj["arcs"][0]
    expected_arc_keys = {
        "slug", "project", "title", "role", "leg_of", "parent", "created_at",
        "valid", "invalid_reason", "empty", "total_analyses",
        "most_recent_analysis_at", "counts", "counts_are_distinct_on",
        "tier_counts", "attribution", "children",
    }
    assert set(arc) == expected_arc_keys
    unattr = portfolio["unattributed"]
    assert set(unattr) == {
        "attribution", "total_analyses", "empty", "most_recent_analysis_at",
        "counts", "counts_are_distinct_on", "tier_counts",
    }


def test_arc_models_schema(seeded_store):
    result = build_arc_models(seeded_store, "projA", "main")
    assert set(result) == {
        "app", "project", "arc", "counts_are_distinct_on", "model_count",
        "foreign_record_count", "models",
    }
    row = result["models"][0]
    expected_row_keys = {
        "model_id", "display_name", "subject", "genome", "template",
        "reaction_count", "gene_count", "gapfill_media", "trust_tier",
        "inferred", "analysis_count", "counts_are_distinct_on", "analyses",
        "agreement",
    }
    assert set(row) == expected_row_keys
    analysis = row["analyses"][0]
    assert set(analysis) == {
        "analysis_id", "kind", "status", "created_at", "trust_tier",
        "provenance", "inferred", "run_count", "runs",
    }
    run = analysis["runs"][0]
    assert set(run) == {"record_id", "run_uid", "created_at", "status"}


def test_agreement_schema(seeded_store):
    result = build_arc_models(seeded_store, "projA", "main")
    agreement = result["models"][0]["agreement"]
    assert set(agreement) == {
        "tp", "fp", "tn", "fn", "mcc", "precision", "recall",
        "excluded_no_measured", "trust_tier", "metric_version", "comparison",
    }


# ── import guard: importing the app must not require fastapi ──────────────────────


def test_app_module_imports_without_fastapi():
    """Importing the app module must succeed even without fastapi (lazy imports)."""
    import kbutillib.models_and_analyses.app as app_mod

    assert hasattr(app_mod, "build_app")
    assert hasattr(app_mod, "main")


# ── FastAPI TestClient tests (skipped when fastapi absent) ───────────────────────


@requires_fastapi
def test_health_endpoint_needs_no_store():
    """T3: /health responds successfully without touching the run database."""
    from fastapi.testclient import TestClient

    from kbutillib.models_and_analyses.app import build_app

    def exploding_factory():
        raise AssertionError("/health must not construct a store")

    app = build_app(store_factory=exploding_factory)
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["contract_version"] == CONTRACT_VERSION


@requires_fastapi
def test_portfolio_endpoint_standalone(monkeypatch):
    """I1 over HTTP: /api/portfolio works with no KING present."""
    from fastapi.testclient import TestClient

    from kbutillib.models_and_analyses.app import build_app

    for var in ("KING_STATE", "KING_CONTRACT_VERSION"):
        monkeypatch.delenv(var, raising=False)
    store = FakeKorosArcStore()
    store.seed_arc(make_arc("p", "a", run_name="A"))
    store.record_analysis(
        "p", "a", make_record("kbdl.model_build", "g", model_uri="obj://m")
    )
    app = build_app(store_factory=lambda: store)
    client = TestClient(app)
    resp = client.get("/api/portfolio")
    assert resp.status_code == 200
    assert resp.json()["projects"][0]["name"] == "p"


@requires_fastapi
def test_endpoints_never_read_detail_blob():
    """BLOB DISCIPLINE over HTTP: neither endpoint opens a detail blob."""
    from fastapi.testclient import TestClient

    from kbutillib.models_and_analyses.app import build_app

    store = FakeKorosArcStore()
    store.seed_arc(make_arc("p", "a", run_name="A"))
    store.record_analysis(
        "p", "a", make_record("kbdl.model_build", "g", model_uri="obj://m")
    )
    app = build_app(store_factory=lambda: store)
    client = TestClient(app)
    client.get("/api/portfolio")
    client.get("/api/arcs/p/a/models")
    assert store.blob_reads == 0
