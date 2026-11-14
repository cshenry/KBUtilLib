"""Seating an ``alcf`` endpoint: the kind, the endpoint branch, the output-token
ceiling floor, the canonical table, and no regression for existing profiles.

Assertions are on published results: loaded configs, resolved seats and
``ConfigError``/``RosterError`` messages.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from kbutillib.domains.ai.council.config import (
    DEFAULT_CANONICAL_BY_KIND,
    DEFAULT_MAX_CONCURRENCY,
    _packaged_default,
    load_council_config,
)
from kbutillib.domains.ai.council.types import (
    ENDPOINT_KINDS,
    HTTP_KINDS,
    ConfigError,
    RosterError,
    SeatSpec,
    bench_fingerprint,
    route_fingerprint,
)

TARA_URL = "https://inference-api.alcf.anl.gov/resource_server/tara/vllm/v1"


def alcf(**over: Any) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "kind": "alcf",
        "options": {"cluster": "tara", "framework": "vllm", "token_command": ["tok"]},
    }
    entry.update(over)
    return entry


def opts(**over: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {"cluster": "tara", "framework": "vllm", "token_command": ["tok"]}
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def model(endpoint: str = "alcf-tara", served: str = "openai/gpt-oss-120b", **params: Any) -> Dict[str, Any]:
    return {"endpoint": endpoint, "served_name": served, "params": params}


def cfg(endpoints: Dict[str, Any], models: Dict[str, Any], profiles: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"endpoints": endpoints, "models": models, "profiles": profiles or {}}


def one(params: Optional[Dict[str, Any]] = None, **endpoint: Any) -> Dict[str, Any]:
    params = {"max_output_tokens": 32000} if params is None else params
    return cfg({"alcf-tara": alcf(**endpoint)}, {"g120": model(**params)})


class TestTypes:
    def test_alcf_is_a_kind_and_an_http_kind(self) -> None:
        assert "alcf" in ENDPOINT_KINDS
        assert "alcf" in HTTP_KINDS
        assert "anthropic" not in HTTP_KINDS

    def test_endpoint_kind_is_still_a_literal(self) -> None:
        with pytest.raises(ValueError):
            SeatSpec(seat_name="p1", endpoint="a", endpoint_kind="alcf2", model="m", canonical_model="c")

    def test_canonical_table_holds_only_bench_a(self) -> None:
        table = DEFAULT_CANONICAL_BY_KIND["alcf"]
        assert table == {
            "openai/gpt-oss-120b": "openai/gpt-oss-120b",
            "gpt-oss-120b": "openai/gpt-oss-120b",
            "openai/gpt-oss-20b": "openai/gpt-oss-20b",
            "gpt-oss-20b": "openai/gpt-oss-20b",
            "google/gemma-4-31B-it-262k": "google/gemma-4-31b-it",
            "gemma-4-31B-it": "google/gemma-4-31b-it",
            "beam-1.0-fp8": "reflection/beam-1.0",
            "inkling-bf16": "thinkingmachines/inkling",
        }

    def test_default_concurrency(self) -> None:
        assert DEFAULT_MAX_CONCURRENCY["alcf"] == 16


class TestEndpointBranch:
    def test_valid_endpoint_loads_and_derives_base_url(self) -> None:
        config = load_council_config(one())
        assert config.endpoints["alcf-tara"].base_url == TARA_URL
        assert config.endpoints["alcf-tara"].max_concurrency == 16

    def test_service_root_override(self) -> None:
        raw = one()
        raw["endpoints"]["alcf-tara"]["options"]["service_root"] = "https://x.example/rs/"
        config = load_council_config(raw)
        assert config.endpoints["alcf-tara"].base_url == "https://x.example/rs/tara/vllm/v1"

    def test_consistent_explicit_base_url_accepted(self) -> None:
        config = load_council_config(one(base_url="https://h.example/tara/vllm/v1"))
        assert config.endpoints["alcf-tara"].base_url == "https://h.example/tara/vllm/v1"

    @pytest.mark.parametrize("missing", ["cluster", "framework"])
    def test_missing_cluster_or_framework_refused(self, missing: str) -> None:
        with pytest.raises(ConfigError) as exc:
            load_council_config(one(options=opts(**{missing: None})))
        assert "alcf-tara" in str(exc.value)
        assert f"options.{missing}" in str(exc.value)

    def test_inconsistent_base_url_refused(self) -> None:
        with pytest.raises(ConfigError) as exc:
            load_council_config(one(base_url="https://h.example/metis/vllm/v1"))
        assert "alcf-tara" in str(exc.value)

    def test_both_token_sources_refused(self) -> None:
        with pytest.raises(ConfigError) as exc:
            load_council_config(one(options=opts(token_import="pkg:fn")))
        assert "alcf-tara" in str(exc.value) and "both" in str(exc.value)

    def test_neither_token_source_refused(self) -> None:
        with pytest.raises(ConfigError) as exc:
            load_council_config(one(options=opts(token_command=None)))
        assert "alcf-tara" in str(exc.value) and "neither" in str(exc.value)

    def test_token_import_alone_accepted(self) -> None:
        load_council_config(one(options=opts(token_command=None, token_import="pkg:fn")))


class TestCeiling:
    def test_missing_ceiling_refused_naming_seat_and_floor(self) -> None:
        with pytest.raises(ConfigError) as exc:
            load_council_config(one(params={}))
        text = str(exc.value)
        assert "g120" in text and "alcf-tara" in text and "32000" in text

    def test_31999_refused_and_32000_accepted(self) -> None:
        with pytest.raises(ConfigError) as exc:
            load_council_config(one(params={"max_output_tokens": 31999}))
        assert "g120" in str(exc.value) and "32000" in str(exc.value)
        load_council_config(one(params={"max_output_tokens": 32000}))

    def test_legacy_max_tokens_counts(self) -> None:
        raw = cfg(
            {"alcf-tara": alcf()},
            {
                "a": model(served="openai/gpt-oss-120b", max_tokens=32000),
                "b": model(served="openai/gpt-oss-20b", max_tokens=32000),
            },
            {"p": {"proposers": ["a", "b"], "critic": "a", "meta": "b"}},
        )
        seat = load_council_config(raw).roster("p").proposers[0]
        assert seat.max_output_tokens == 32000

    def test_legacy_max_tokens_below_floor_refused(self) -> None:
        with pytest.raises(ConfigError):
            load_council_config(one(params={"max_tokens": 16000}))

    def test_other_kinds_keep_their_default(self) -> None:
        raw = cfg(
            {"o": {"kind": "openai", "base_url": "http://x/v1"}},
            {"m": {"endpoint": "o", "served_name": "m", "canonical": "v/m"}},
        )
        load_council_config(raw)


def two_cluster(a: str = "g120a", b: str = "g120b") -> Dict[str, Any]:
    return cfg(
        {
            "alcf-tara": alcf(),
            "alcf-metis": alcf(options=opts(cluster="metis")),
        },
        {
            a: model("alcf-tara", "openai/gpt-oss-120b", max_output_tokens=32000),
            b: model("alcf-metis", "gpt-oss-120b", max_output_tokens=32000),
            "g20": model("alcf-tara", "openai/gpt-oss-20b", max_output_tokens=32000),
            "gem": model("alcf-tara", "gemma-4-31B-it", max_output_tokens=32000),
        },
        {
            "dup": {"proposers": [a, b], "critic": "g20", "meta": "gem"},
            "ok": {"proposers": [a, "g20"], "critic": "gem", "meta": "gem"},
        },
    )


class TestRoster:
    def test_same_model_on_two_clusters_is_not_two_proposers(self) -> None:
        config = load_council_config(two_cluster())
        with pytest.raises(RosterError):
            config.roster("dup")

    def test_seat_carries_alcf_kind_and_ceiling(self) -> None:
        config = load_council_config(two_cluster())
        seat = config.roster("ok").proposers[0]
        assert seat.endpoint_kind == "alcf"
        assert seat.canonical_model == "openai/gpt-oss-120b"
        assert seat.max_output_tokens == 32000
        assert config.endpoints[seat.endpoint].base_url == TARA_URL


# Pinned: profile -> bench fingerprint and per-seat
# (seat, endpoint, kind, served model, canonical, ceiling, route fingerprint).
PINNED = {
    'argo': {
        "bench": '57e32d5c5d785a7d2bcbf7e69287575ebbf330fad41783b329668f5f9b4015fc',
        "seats": [
            ('proposer_1', 'argo', 'argo', 'gemini35flash', 'google/gemini-3.5-flash', 4000, "e7ffa262bc4dd5d63b835118f23bf1ee76d99b81731699177df38772acd08a12"),
            ('proposer_2', 'argo', 'argo', 'gpt54', 'openai/gpt-5.4', 4000, "167ff99ff95652ec76880ecbf63f565dc75feadca299526deefe20e6de882f6a"),
            ('proposer_3', 'argo', 'argo', 'gpt55', 'openai/gpt-5.5', 4000, "e67bb46a197fc312a1b2707d568c7d845caf3efea4e52107741af64ba5301521"),
            ('critic', 'argo', 'argo', 'claudesonnet5', 'anthropic/claude-sonnet-5', 4000, "04e70079224dc0effc96a3bfa3e88aa047789d67365398d03b379b75911d77f6"),
            ('meta', 'argo', 'argo', 'claudeopus45', 'anthropic/claude-opus-4.5', 4000, "9e6f7e816bce59c4d351382eced96ffceeaaae7703588deabd2bd520edef5d4f"),
        ],
    },
    'mixed-subscription': {
        "bench": '85e7218352d8aba1abc55efa791989ce7ffe6368a0686a9ee17c8c2be7903be2',
        "seats": [
            ('proposer_1', 'argo', 'argo', 'gemini35flash', 'google/gemini-3.5-flash', 4000, "e7ffa262bc4dd5d63b835118f23bf1ee76d99b81731699177df38772acd08a12"),
            ('proposer_2', 'codex', 'codex_cli', 'gpt-5.4', 'openai/gpt-5.4', 4000, "14f87862ea28991c506bb841caca194871fb080abe7bbcef21300d326a36ff18"),
            ('proposer_3', 'codex', 'codex_cli', 'gpt-5.5', 'openai/gpt-5.5', 4000, "3fac80ccca8943c6ba013d9827f372dc2b86d57443d092748c370b01fe231813"),
            ('critic', 'claude', 'claude_cli', 'sonnet', 'anthropic/claude-sonnet-5', 4000, "ca4807745c4fa0d3983ea11939bf92c7097424fb76b15da2f09c715d90e4f092"),
            ('meta', 'claude', 'claude_cli', 'opus', 'anthropic/claude-opus-5', 4000, "626fd0ce60144157b9bf7060cd324be01bb080d2fc6ca06c98078412cbf3e7ce"),
        ],
    },
}


def test_packaged_profiles_resolve_unchanged() -> None:
    """Pinned from the base commit (ed7b839) BEFORE alcf was added."""
    config = load_council_config(_packaged_default())
    assert sorted(config.profiles) == sorted(PINNED)
    for name, expected in PINNED.items():
        roster = config.roster(name)
        assert bench_fingerprint(roster) == expected["bench"], name
        seats = [*roster.proposers, roster.critic, roster.meta]
        got = [
            (s.seat_name, s.endpoint, s.endpoint_kind, s.model, s.canonical_model,
             s.max_output_tokens, route_fingerprint(s, config.endpoint(s.endpoint)))
            for s in seats
        ]
        assert got == expected["seats"], name
