"""Seating an ``anthropic`` endpoint: the Literal, the config branch, the
rate-limit bucket rules, ``billing_wallet`` and the temperature distinction.

Assertions are on published results: loaded configs, resolved seats,
``ConfigError``/``RosterError`` messages and the worker's own limit objects.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from kbutillib.domains.ai.council.config import (
    ANTHROPIC_RATE_LIMIT_BUCKETS,
    DEFAULT_CANONICAL,
    DEFAULT_CANONICAL_BY_KIND,
    DEFAULT_MAX_CONCURRENCY,
    load_council_config,
)
from kbutillib.domains.ai.council.engine.worker import CouncilWorker
from kbutillib.domains.ai.council.types import (
    ENDPOINT_KINDS,
    HTTP_KINDS,
    ConfigError,
    RosterError,
    SeatSpec,
    default_reuse_namespace,
)

BUCKET_HAIKU = ANTHROPIC_RATE_LIMIT_BUCKETS["anthropic/claude-haiku-4.5"]
BUCKET_OPUS5 = ANTHROPIC_RATE_LIMIT_BUCKETS["anthropic/claude-opus-5"]
BUCKET_OPUS4 = ANTHROPIC_RATE_LIMIT_BUCKETS["anthropic/claude-opus-4.8"]
BUCKET_SONNET5 = ANTHROPIC_RATE_LIMIT_BUCKETS["anthropic/claude-sonnet-5"]


def anth(account: Optional[str], **extra: Any) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"kind": "anthropic", "api_key_env": "ANTHROPIC_API_KEY",
                             "billing_wallet": "anthropic-credit"}
    if account is not None:
        entry["account"] = account
    entry.update(extra)
    return entry


def cfg(endpoints: Dict[str, Any], models: Dict[str, Any], **top: Any) -> Dict[str, Any]:
    raw: Dict[str, Any] = {"endpoints": endpoints, "models": models, "profiles": {}}
    raw.update(top)
    return raw


def one_haiku(**endpoint_extra: Any) -> Dict[str, Any]:
    return cfg({"a-haiku": anth(BUCKET_HAIKU, **endpoint_extra)},
               {"haiku45": {"endpoint": "a-haiku", "served_name": "claude-haiku-4-5"}})


class TestTypes:
    def test_seatspec_accepts_anthropic(self) -> None:
        seat = SeatSpec(seat_name="p1", endpoint="a", endpoint_kind="anthropic",
                        model="claude-haiku-4-5", canonical_model="anthropic/claude-haiku-4.5")
        assert seat.endpoint_kind == "anthropic"

    def test_endpoint_kinds_but_not_http_kinds(self) -> None:
        assert "anthropic" in ENDPOINT_KINDS
        assert "anthropic" not in HTTP_KINDS
        assert HTTP_KINDS == ("argo", "openai")


class TestEndpointBranch:
    def test_loads_without_base_url_or_reuse_namespace(self) -> None:
        config = load_council_config(one_haiku())
        endpoint = config.endpoint("a-haiku")
        assert endpoint["base_url"] == "https://api.anthropic.com"
        assert endpoint["billing"] == "api"
        assert endpoint["max_concurrency"] == DEFAULT_MAX_CONCURRENCY["anthropic"] == 4
        namespace = default_reuse_namespace(endpoint, "claude-haiku-4-5")
        # Non-empty and derived from normalised base_url + deployment_revision.
        # NOT asserted distinct from another alias's: sharing
        # "https://api.anthropic.com#" between aliases is deliberate.
        assert namespace == "https://api.anthropic.com#"
        revised = dict(endpoint, deployment_revision="r2")
        assert default_reuse_namespace(revised, "claude-haiku-4-5") == "https://api.anthropic.com#r2"

    def test_billing_subscription_refused(self) -> None:
        with pytest.raises(ConfigError, match="billing: subscription"):
            load_council_config(one_haiku(billing="subscription"))

    def test_missing_account_refused(self) -> None:
        raw = cfg({"a-haiku": anth(None)},
                  {"haiku45": {"endpoint": "a-haiku", "served_name": "claude-haiku-4-5"}})
        with pytest.raises(ConfigError, match="declares no account"):
            load_council_config(raw)

    def test_account_not_equal_to_bucket_refused(self) -> None:
        raw = cfg({"a-haiku": anth("my-haiku")},
                  {"haiku45": {"endpoint": "a-haiku", "served_name": "claude-haiku-4-5"}})
        with pytest.raises(ConfigError) as excinfo:
            load_council_config(raw)
        assert BUCKET_HAIKU in str(excinfo.value) and "my-haiku" in str(excinfo.value)

    def test_missing_billing_wallet_refused_on_anthropic(self) -> None:
        raw = one_haiku()
        del raw["endpoints"]["a-haiku"]["billing_wallet"]
        with pytest.raises(ConfigError, match="billing_wallet"):
            load_council_config(raw)

    def test_non_anthropic_billing_wallet_defaults_to_account(self) -> None:
        config = load_council_config(cfg(
            {"mag": {"kind": "openai", "base_url": "http://x/v1", "account": "mag-shared"},
             "local": {"kind": "openai", "base_url": "http://y/v1"}},
            {},
        ))
        assert config.endpoint("mag")["billing_wallet"] == "mag-shared"
        assert config.endpoint("local")["billing_wallet"] is None


class TestBuckets:
    def test_unknown_canonical_refused_naming_it_and_the_override(self) -> None:
        raw = cfg({"a-x": anth("anthropic-x")},
                  {"newclaude": {"endpoint": "a-x", "served_name": "claude-new",
                                 "canonical": "anthropic/claude-new-9"}})
        with pytest.raises(ConfigError) as excinfo:
            load_council_config(raw)
        message = str(excinfo.value)
        assert "anthropic/claude-new-9" in message
        assert "rate_limit_buckets" in message

    def test_bucket_table_overridable_from_configuration(self) -> None:
        raw = cfg({"a-x": anth("anthropic-new")},
                  {"newclaude": {"endpoint": "a-x", "served_name": "claude-new",
                                 "canonical": "anthropic/claude-new-9"}},
                  rate_limit_buckets={"anthropic/claude-new-9": "anthropic-new"})
        config = load_council_config(raw)
        assert config.rate_limit_bucket_table()["anthropic/claude-new-9"] == "anthropic-new"
        # And an override can REMAP a shipped model.
        remap = cfg({"a-haiku": anth("custom")},
                    {"haiku45": {"endpoint": "a-haiku", "served_name": "claude-haiku-4-5"}},
                    rate_limit_buckets={"anthropic/claude-haiku-4.5": "custom"})
        assert load_council_config(remap).endpoint("a-haiku")["account"] == "custom"

    def test_one_endpoint_spanning_two_buckets_refused(self) -> None:
        raw = cfg({"a": anth(BUCKET_HAIKU)},
                  {"haiku45": {"endpoint": "a", "served_name": "claude-haiku-4-5"},
                   "opus5": {"endpoint": "a", "served_name": "claude-opus-5"}})
        with pytest.raises(ConfigError) as excinfo:
            load_council_config(raw)
        assert BUCKET_HAIKU in str(excinfo.value) and BUCKET_OPUS5 in str(excinfo.value)

    def test_two_aliases_sharing_account_across_buckets_refused_naming_both(self) -> None:
        raw = cfg({"a-haiku": anth(BUCKET_HAIKU), "a-opus": anth(BUCKET_HAIKU)},
                  {"haiku45": {"endpoint": "a-haiku", "served_name": "claude-haiku-4-5"},
                   "opus5": {"endpoint": "a-opus", "served_name": "claude-opus-5"}})
        with pytest.raises(ConfigError) as excinfo:
            load_council_config(raw)
        message = str(excinfo.value)
        assert BUCKET_HAIKU in message and BUCKET_OPUS5 in message

    def test_two_aliases_sharing_bucket_and_account_accepted(self) -> None:
        raw = cfg({"a-opus48": anth(BUCKET_OPUS4), "a-opus48b": anth(BUCKET_OPUS4)},
                  {"opus48": {"endpoint": "a-opus48", "served_name": "claude-opus-4-8"},
                   "opus47": {"endpoint": "a-opus48b", "served_name": "claude-opus-4-7",
                              "canonical": "anthropic/claude-opus-4.7"}})
        config = load_council_config(raw)
        assert config.endpoint("a-opus48")["account"] == config.endpoint("a-opus48b")["account"]


class TestCanonical:
    def test_table_contents(self) -> None:
        assert DEFAULT_CANONICAL_BY_KIND["anthropic"] == {
            "claude-haiku-4-5": "anthropic/claude-haiku-4.5",
            "claude-sonnet-5": "anthropic/claude-sonnet-5",
            "claude-sonnet-4-6": "anthropic/claude-sonnet-4.6",
            "claude-opus-5": "anthropic/claude-opus-5",
            "claude-opus-4-8": "anthropic/claude-opus-4.8",
        }

    def test_collides_with_argo_on_purpose(self) -> None:
        assert DEFAULT_CANONICAL_BY_KIND["anthropic"]["claude-sonnet-5"] == DEFAULT_CANONICAL["claudesonnet5"]
        assert DEFAULT_CANONICAL_BY_KIND["anthropic"]["claude-opus-5"] == DEFAULT_CANONICAL["claudeopus5"]

    def test_REFUSAL_PRESERVED_argo_and_anthropic_sonnet5_cannot_both_propose(self) -> None:
        """GUARD: this asserts a REFUSAL is preserved.  Seating ARGO's
        ``claudesonnet5`` and Anthropic's ``claude-sonnet-5`` as two proposers
        would count one model as two independent votes.  If this test starts
        failing because the roster ACCEPTS, the fix is to restore the refusal,
        never to delete this test or add a per-wallet canonical suffix."""
        raw = cfg(
            {"argo": {"kind": "argo", "base_url": "https://apps.inside.anl.gov/argoapi/v1"},
             "a-sonnet5": anth(BUCKET_SONNET5)},
            {"claudesonnet5": {"endpoint": "argo"},
             "sonnet5": {"endpoint": "a-sonnet5", "served_name": "claude-sonnet-5"},
             "gemini35flash": {"endpoint": "argo"},
             "gpt55": {"endpoint": "argo"}},
            profiles={"dup": {"proposers": ["claudesonnet5", "sonnet5"], "critic": "gemini35flash",
                              "meta": "gpt55"}},
        )
        config = load_council_config(raw)
        with pytest.raises(RosterError, match="anthropic/claude-sonnet-5"):
            config.roster("dup")


class TestSeatTemperature:
    def _seat(self, kind: str, params: Dict[str, Any]) -> SeatSpec:
        if kind == "anthropic":
            raw = cfg({"a-haiku": anth(BUCKET_HAIKU)},
                      {"m": {"endpoint": "a-haiku", "served_name": "claude-haiku-4-5", "params": params}})
        else:
            raw = cfg({"local": {"kind": "openai", "base_url": "http://x/v1"}},
                      {"m": {"endpoint": "local", "canonical": "qwen/qwen3", "params": params}})
        return load_council_config(raw)._seat("proposer_1", "m")

    def test_anthropic_omitted_is_none(self) -> None:
        assert self._seat("anthropic", {}).temperature is None

    def test_anthropic_explicit_zero_is_zero(self) -> None:
        seat = self._seat("anthropic", {"temperature": 0})
        assert seat.temperature == 0
        assert seat.temperature is not None

    def test_anthropic_explicit_nonzero_kept(self) -> None:
        seat = self._seat("anthropic", {"temperature": 0.3})
        assert seat.temperature == 0.3
        assert "temperature" not in seat.extra

    def test_non_anthropic_omitted_still_zero(self) -> None:
        seat = self._seat("openai", {})
        assert seat.temperature == 0
        assert seat.temperature is not None


class TestBuildLimitsReuse:
    """Asserts the EXISTING ``CouncilWorker._build_limits`` groups anthropic
    endpoints as the account rule intends (it is not modified)."""

    def _config(self) -> Any:
        return load_council_config(cfg(
            {
                "a-haiku": anth(BUCKET_HAIKU, max_concurrency=4, max_requests_per_minute=50),
                "a-opus5": anth(BUCKET_OPUS5, max_concurrency=3, max_requests_per_minute=40),
                "a-opus48": anth(BUCKET_OPUS4, max_concurrency=6, max_requests_per_minute=90),
                "a-opus47": anth(BUCKET_OPUS4, max_concurrency=2, max_requests_per_minute=120),
            },
            {
                "haiku45": {"endpoint": "a-haiku", "served_name": "claude-haiku-4-5"},
                "opus5": {"endpoint": "a-opus5", "served_name": "claude-opus-5"},
                "opus48": {"endpoint": "a-opus48", "served_name": "claude-opus-4-8"},
                "opus47": {"endpoint": "a-opus47", "served_name": "claude-opus-4-7",
                           "canonical": "anthropic/claude-opus-4.7"},
            },
        ))

    def test_three_accounts_three_limits_and_shared_one(self, store: Any) -> None:
        worker = CouncilWorker(store, self._config(),
                               {"a-haiku": None, "a-opus5": None, "a-opus48": None, "a-opus47": None},
                               user="test")
        limits = worker._limit_by_endpoint
        distinct = {id(limits[name]) for name in ("a-haiku", "a-opus5", "a-opus48")}
        assert len(distinct) == 3
        shared = limits["a-opus48"]
        assert shared is limits["a-opus47"]
        assert shared.capacity == 2  # min declared max_concurrency
        assert shared.pacer is not None
        assert shared.pacer.requests_per_minute == 90.0  # min declared rpm
