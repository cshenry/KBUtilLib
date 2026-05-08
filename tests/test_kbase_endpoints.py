"""Tests for kbase_endpoints URL helper module."""

import pytest

from kbutillib.kbase_endpoints import (
    base_url,
    env_from_url,
    narrative_url,
    service_url,
)


class TestBaseUrl:
    def test_prod(self):
        assert base_url("prod") == "https://kbase.us/services"

    def test_appdev(self):
        assert base_url("appdev") == "https://appdev.kbase.us/services"

    def test_ci(self):
        assert base_url("ci") == "https://ci.kbase.us/services"

    def test_case_insensitive(self):
        assert base_url("PROD") == base_url("prod")
        assert base_url("CI") == base_url("ci")

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown KBase environment"):
            base_url("staging")


class TestServiceUrl:
    def test_workspace(self):
        assert service_url("workspace") == "https://kbase.us/services/ws"

    def test_ws_alias(self):
        assert service_url("ws") == "https://kbase.us/services/ws"

    def test_ee2(self):
        assert service_url("ee2", "prod") == "https://kbase.us/services/ee2"

    def test_ee2_ci(self):
        assert service_url("ee2", "ci") == "https://ci.kbase.us/services/ee2"

    def test_shock(self):
        assert service_url("shock", "appdev") == "https://appdev.kbase.us/services/shock-api"

    def test_handle_service(self):
        assert service_url("handle_service") == "https://kbase.us/services/handle_service"

    def test_unknown_service_raises(self):
        with pytest.raises(ValueError, match="Unknown KBase service"):
            service_url("nonexistent")

    def test_case_insensitive_service(self):
        assert service_url("WORKSPACE") == service_url("workspace")


class TestNarrativeUrl:
    def test_prod(self):
        assert narrative_url("prod") == "https://narrative.kbase.us"

    def test_ci(self):
        assert narrative_url("ci") == "https://ci.kbase.us"

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown KBase environment"):
            narrative_url("bad")


class TestEnvFromUrl:
    def test_prod_url(self):
        assert env_from_url("https://kbase.us/services/ws") == "prod"

    def test_ci_url(self):
        assert env_from_url("https://ci.kbase.us/services/ws") == "ci"

    def test_appdev_url(self):
        assert env_from_url("https://appdev.kbase.us/services/ee2") == "appdev"

    def test_defaults_to_prod(self):
        assert env_from_url("https://some-other-host.com/foo") == "prod"
