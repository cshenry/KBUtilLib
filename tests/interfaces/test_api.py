"""WP5 — FastAPI HTTP adapter tests.

Test groups
-----------
1. ``capability_to_route_meta`` — pure helper, no fastapi needed.
2. Import guard — ``import kbutillib.interfaces.api.app`` must succeed without
   fastapi installed.
3. FastAPI TestClient tests (skipped when fastapi not installed):
   * ``GET /health`` → 200 ``{"status": "ok"}``
   * ``GET /version`` → 200, returns kbutillib version string
   * ``GET /v1/capabilities`` → 200, lists the biochem capabilities
   * ``GET /openapi.json`` → 200, includes tool routes in paths
   * ``POST /v1/tools/<unknown>`` → 404
   * ``POST /v1/tools/<unavailable_cap>`` → 503
"""

from __future__ import annotations

import importlib.util

import pytest

# ---------------------------------------------------------------------------
# Skip condition for tests that require fastapi
# ---------------------------------------------------------------------------

_fastapi_available = importlib.util.find_spec("fastapi") is not None

requires_fastapi = pytest.mark.skipif(
    not _fastapi_available,
    reason="fastapi not installed; install kbutillib[api] to run these tests",
)

# ---------------------------------------------------------------------------
# Helpers — expected capability names (from WP3)
# ---------------------------------------------------------------------------

BIOCHEM_CAP_NAMES = {
    "biochem.search_compounds",
    "biochem.get_compound_by_id",
    "biochem.get_reaction_by_id",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fresh_registry():
    """Return a brand-new, isolated CapabilityRegistry."""
    from kbutillib.core.registry import CapabilityRegistry

    return CapabilityRegistry()


@pytest.fixture()
def biochem_registry(fresh_registry):
    """Return a registry pre-loaded with the 3 biochem capabilities."""
    from kbutillib import KBUtilLib
    from kbutillib.core.capability import register_all

    kbu = KBUtilLib()
    register_all(kbu, registry=fresh_registry)
    return fresh_registry


@pytest.fixture()
def test_client(biochem_registry):
    """Return a Starlette TestClient for the FastAPI app (requires fastapi)."""
    from kbutillib.interfaces.api.app import build_app

    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("starlette TestClient not available")

    fapp = build_app(registry=biochem_registry)
    return TestClient(fapp)


# ---------------------------------------------------------------------------
# 1. Pure helper: capability_to_route_meta (no fastapi needed)
# ---------------------------------------------------------------------------


class TestCapabilityToRouteMeta:
    """``capability_to_route_meta`` maps specs to route dicts offline."""

    def test_returns_dict_with_required_keys(self, biochem_registry):
        """The returned dict must have path, method, input_schema, output_schema."""
        from kbutillib.interfaces.api.app import capability_to_route_meta

        spec = biochem_registry.get("biochem.get_compound_by_id")
        meta = capability_to_route_meta(spec)
        assert set(meta.keys()) == {"path", "method", "input_schema", "output_schema"}

    def test_path_contains_capability_name(self, biochem_registry):
        """The path must include the dotted capability name."""
        from kbutillib.interfaces.api.app import capability_to_route_meta

        spec = biochem_registry.get("biochem.get_reaction_by_id")
        meta = capability_to_route_meta(spec)
        assert "biochem.get_reaction_by_id" in meta["path"]

    def test_method_is_post(self, biochem_registry):
        """Tool invocation routes always use POST."""
        from kbutillib.interfaces.api.app import capability_to_route_meta

        spec = biochem_registry.get("biochem.search_compounds")
        meta = capability_to_route_meta(spec)
        assert meta["method"] == "POST"

    def test_input_schema_from_pydantic_model(self, biochem_registry):
        """input_schema should be a non-trivial JSON Schema when model is present."""
        from kbutillib.interfaces.api.app import capability_to_route_meta

        spec = biochem_registry.get("biochem.get_compound_by_id")
        meta = capability_to_route_meta(spec)
        schema = meta["input_schema"]
        assert isinstance(schema, dict)
        # GetCompoundByIdInput has a 'compound_id' property
        props = schema.get("properties", {})
        assert "compound_id" in props

    def test_output_schema_from_pydantic_model(self, biochem_registry):
        """output_schema should be a non-trivial JSON Schema when model is present."""
        from kbutillib.interfaces.api.app import capability_to_route_meta

        spec = biochem_registry.get("biochem.get_compound_by_id")
        meta = capability_to_route_meta(spec)
        schema = meta["output_schema"]
        assert isinstance(schema, dict)

    def test_no_input_model_gives_empty_schema(self, fresh_registry):
        """A spec without an input_model gets an empty-object schema."""
        from kbutillib.core.registry import CapabilitySpec
        from kbutillib.interfaces.api.app import capability_to_route_meta

        spec = CapabilitySpec(name="test.no_model", fn=lambda: None, domain="test")
        meta = capability_to_route_meta(spec)
        assert meta["input_schema"] == {"type": "object", "properties": {}}
        assert meta["output_schema"] == {"type": "object", "properties": {}}

    def test_all_three_biochem_caps_have_correct_paths(self, biochem_registry):
        """All 3 biochem caps map to /v1/tools/<name>."""
        from kbutillib.interfaces.api.app import capability_to_route_meta

        for name in BIOCHEM_CAP_NAMES:
            spec = biochem_registry.get(name)
            meta = capability_to_route_meta(spec)
            assert meta["path"] == f"/v1/tools/{name}", f"Wrong path for {name}"
            assert meta["method"] == "POST"


# ---------------------------------------------------------------------------
# 2. Import guard — module importable without fastapi
# ---------------------------------------------------------------------------


class TestImportWithoutFastapi:
    """Importing the api module must NOT require fastapi."""

    def test_module_importable(self):
        """``import kbutillib.interfaces.api.app`` succeeds without fastapi."""
        import kbutillib.interfaces.api.app as api_mod

        assert hasattr(api_mod, "build_app")
        assert hasattr(api_mod, "main")
        assert hasattr(api_mod, "capability_to_route_meta")

    def test_capability_to_route_meta_importable(self):
        """The pure helper is accessible without fastapi."""
        from kbutillib.interfaces.api.app import capability_to_route_meta

        assert callable(capability_to_route_meta)

    def test_build_app_raises_import_error_without_fastapi(self, fresh_registry):
        """build_app raises ImportError when fastapi is missing."""
        if _fastapi_available:
            pytest.skip("fastapi is installed; cannot test ImportError path")

        from kbutillib.interfaces.api.app import build_app

        with pytest.raises(ImportError, match="fastapi"):
            build_app(registry=fresh_registry)


# ---------------------------------------------------------------------------
# 3. FastAPI TestClient tests (skipped when fastapi not installed)
# ---------------------------------------------------------------------------


@requires_fastapi
class TestHealthEndpoint:
    """GET /health → 200 {status: ok}."""

    def test_health_returns_200(self, test_client):
        response = test_client.get("/health")
        assert response.status_code == 200

    def test_health_body(self, test_client):
        response = test_client.get("/health")
        assert response.json() == {"status": "ok"}


@requires_fastapi
class TestVersionEndpoint:
    """GET /version → 200, includes version string."""

    def test_version_returns_200(self, test_client):
        response = test_client.get("/version")
        assert response.status_code == 200

    def test_version_has_version_key(self, test_client):
        response = test_client.get("/version")
        data = response.json()
        assert "version" in data
        assert isinstance(data["version"], str)
        assert data["version"]  # non-empty


@requires_fastapi
class TestCapabilitiesEndpoint:
    """GET /v1/capabilities → lists biochem caps."""

    def test_capabilities_returns_200(self, test_client):
        response = test_client.get("/v1/capabilities")
        assert response.status_code == 200

    def test_capabilities_is_list(self, test_client):
        response = test_client.get("/v1/capabilities")
        data = response.json()
        assert isinstance(data, list)

    def test_biochem_caps_present(self, test_client):
        response = test_client.get("/v1/capabilities")
        names = {item["name"] for item in response.json()}
        assert BIOCHEM_CAP_NAMES.issubset(names)

    def test_each_cap_has_required_fields(self, test_client):
        response = test_client.get("/v1/capabilities")
        for item in response.json():
            assert "name" in item
            assert "domain" in item
            assert "summary" in item
            assert "available" in item
            assert "unavailable_reason" in item
            assert "input_schema" in item
            assert "output_schema" in item

    def test_biochem_caps_have_correct_domain(self, test_client):
        response = test_client.get("/v1/capabilities")
        biochem = [i for i in response.json() if i["name"] in BIOCHEM_CAP_NAMES]
        assert len(biochem) == 3
        for cap in biochem:
            assert cap["domain"] == "biochem"


@requires_fastapi
class TestCapabilityDetailEndpoint:
    """GET /v1/capabilities/{name} → single cap detail or 404."""

    def test_known_cap_returns_200(self, test_client):
        response = test_client.get("/v1/capabilities/biochem.get_compound_by_id")
        assert response.status_code == 200

    def test_known_cap_detail_fields(self, test_client):
        response = test_client.get("/v1/capabilities/biochem.get_compound_by_id")
        data = response.json()
        assert data["name"] == "biochem.get_compound_by_id"
        assert data["domain"] == "biochem"
        assert "available" in data

    def test_unknown_cap_returns_404(self, test_client):
        response = test_client.get("/v1/capabilities/does.not.exist")
        assert response.status_code == 404


@requires_fastapi
class TestOpenApiSchema:
    """GET /openapi.json → includes tool routes in paths."""

    def test_openapi_returns_200(self, test_client):
        response = test_client.get("/openapi.json")
        assert response.status_code == 200

    def test_openapi_has_paths(self, test_client):
        response = test_client.get("/openapi.json")
        schema = response.json()
        assert "paths" in schema

    def test_tool_routes_in_openapi(self, test_client):
        """At least the /v1/tools path pattern should appear in OpenAPI."""
        response = test_client.get("/openapi.json")
        schema = response.json()
        paths = schema.get("paths", {})
        # The route uses path param {name}, so check for /v1/tools pattern
        tool_paths = [p for p in paths if "/v1/tools/" in p]
        assert tool_paths, f"No /v1/tools/* routes found in {list(paths.keys())}"

    def test_health_in_openapi(self, test_client):
        response = test_client.get("/openapi.json")
        schema = response.json()
        paths = schema.get("paths", {})
        assert "/health" in paths


@requires_fastapi
class TestToolInvocationEndpoint:
    """POST /v1/tools/{name} — 404 unknown, 503 unavailable, functional call."""

    def test_unknown_tool_returns_404(self, test_client):
        response = test_client.post("/v1/tools/does.not.exist", json={})
        assert response.status_code == 404

    def test_unavailable_cap_returns_503(self, biochem_registry):
        """A capability whose availability() returns (False, reason) → 503."""
        from starlette.testclient import TestClient

        from kbutillib.core.registry import CapabilitySpec
        from kbutillib.interfaces.api.app import build_app

        # Build a registry with an always-unavailable capability.
        # _availability_fn may return bool OR (bool, reason) at runtime.
        def _always_unavailable() -> bool:  # type: ignore[return]
            return False  # type: ignore[return-value]

        unavail_spec = CapabilitySpec(
            name="test.unavailable",
            fn=lambda: None,
            domain="test",
            _availability_fn=_always_unavailable,
        )
        biochem_registry.register(unavail_spec)

        fapp = build_app(registry=biochem_registry)
        client = TestClient(fapp)

        response = client.post("/v1/tools/test.unavailable", json={})
        assert response.status_code == 503
        assert "unavailable" in response.json()["detail"].lower()

    def test_no_auth_assertions(self, test_client):
        """The API is open — no auth required; /health is accessible with no token."""
        # Confirms open access: no Authorization header needed
        response = test_client.get("/health")
        assert response.status_code == 200
        # NOT 401 / 403 — open by design
        assert response.status_code not in (401, 403)
