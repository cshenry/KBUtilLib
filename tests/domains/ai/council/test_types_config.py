"""Foundation tests for the LLM council: types, hashing, config and fakes.

Nothing here makes a network call, spends a token, needs a CLI login or needs
a GPU -- that is a hard constraint on the whole council test tree, not a
property of this module.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from kbutillib.domains.ai.council import (
    Ballot,
    CompletionRequest,
    ConfigError,
    CouncilError,
    CouncilTask,
    FakeBackend,
    MenuEntry,
    ReplayBackend,
    Roster,
    RosterError,
    SeatSpec,
    TransportError,
    data_hash,
    default_reuse_namespace,
    entity_hash,
    load_council_config,
    load_legacy_model_inference,
    prompt_hash,
    route_fingerprint,
)
from kbutillib.domains.ai.council.config import (
    DEFAULT_CANONICAL,
    DEFAULT_CANONICAL_VERSION,
    QueueConfig,
    _merge,
)

FIXTURES = Path(__file__).parent / "fixtures"
FILIPE_CONFIG = FIXTURES / "filipe_model_inference.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def seat(seat_name: str, canonical: str, **kwargs) -> SeatSpec:
    """Build a SeatSpec with sane defaults for the field under test."""
    fields = {
        "seat_name": seat_name,
        "endpoint": kwargs.pop("endpoint", "argo"),
        "endpoint_kind": kwargs.pop("endpoint_kind", "argo"),
        "model": kwargs.pop("model", canonical.split("/")[-1]),
        "canonical_model": canonical,
    }
    fields.update(kwargs)
    return SeatSpec(**fields)


def minimal_roster(**kwargs) -> Roster:
    """A valid three-model council, overridable per field."""
    fields = {
        "proposers": [
            seat("proposer_1", "google/gemini-3.5-flash"),
            seat("proposer_2", "openai/gpt-5.4"),
        ],
        "critic": seat("critic", "anthropic/claude-sonnet-5"),
        "meta": seat("meta", "anthropic/claude-opus-5"),
    }
    fields.update(kwargs)
    return Roster(**fields)


def config_dict(**overrides) -> dict:
    """A small but complete native-format config."""
    raw = {
        "endpoints": {
            "argo": {"kind": "argo", "base_url": "https://argo.example/v1"},
            "local": {"kind": "openai", "base_url": "http://localhost:9001/v1"},
        },
        "models": {
            "a": {"endpoint": "argo", "canonical": "vendor/a"},
            "b": {"endpoint": "argo", "canonical": "vendor/b"},
            "c": {"endpoint": "local", "canonical": "vendor/c"},
            "d": {"endpoint": "local", "canonical": "vendor/d"},
            # Spare aliases, so an override can seat a model that is not
            # already on the bench (which the independence rule would refuse).
            "e": {"endpoint": "local", "canonical": "vendor/e"},
            "f": {"endpoint": "local", "canonical": "vendor/f"},
        },
        "profiles": {
            "p": {"proposers": ["a", "b"], "critic": "c", "meta": "d"},
        },
        "default_profile": "p",
        "store": "sqlite:///council-test.sqlite",
    }
    raw.update(overrides)
    return raw


def queue_config_dict(queues: dict, **overrides) -> dict:
    """A native-format config sized for queue validation: two CLI accounts
    (``acct-a`` via ``epA``, ``acct-b`` via ``epB``, each capped at 8) plus one
    non-CLI endpoint (``epC``, an ARGO-kind endpoint, for the batch_size
    refusal case), two proposer seats and one critic and one meta seat -- enough
    seat/account variety to build every queue scenario under test without the
    fixture itself becoming the thing under test.

    *queues* becomes the ``queues`` section verbatim; the caller decides which
    seats each queue owns and at what concurrency.
    """
    raw = {
        "endpoints": {
            "epA": {"kind": "codex_cli", "account": "acct-a", "max_concurrency": 8},
            "epB": {"kind": "codex_cli", "account": "acct-b", "max_concurrency": 8},
            "epC": {"kind": "argo", "base_url": "https://argo.example/v1"},
        },
        "models": {
            "m_p1": {"endpoint": "epA", "canonical": "vendor/p1"},
            "m_p2": {"endpoint": "epB", "canonical": "vendor/p2"},
            "m_critic": {"endpoint": "epA", "canonical": "vendor/critic"},
            "m_meta": {"endpoint": "epC", "canonical": "vendor/meta"},
        },
        "profiles": {
            "p": {
                "proposers": {"proposer_a": "m_p1", "proposer_b": "m_p2"},
                "critic": "m_critic",
                "meta": "m_meta",
            },
        },
        "default_profile": "p",
        "store": "sqlite:///council-test.sqlite",
        "queues": queues,
    }
    raw.update(overrides)
    return raw


def run(coro):
    """Run one coroutine to completion (no pytest-asyncio dependency).

    Deliberately NOT ``asyncio.run``: that sets the thread's current event
    loop to ``None`` on the way out, after which the deprecated
    ``asyncio.get_event_loop()`` raises instead of creating a loop. Other
    modules in this suite still call it (tests/interfaces/test_mcp_server.py
    does), and since ``tests/domains`` collects first, ``asyncio.run`` here
    broke three of their tests from a distance. Owning the loop locally keeps
    the process-global asyncio state exactly as it was found.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def request_for(seat_spec: SeatSpec, **kwargs) -> CompletionRequest:
    """Build a CompletionRequest for a seat."""
    fields = {
        "seat": seat_spec,
        "role": "proposer",
        "system": "you are a proposer",
        "user": "pick from the menu",
        "json_schema": {"type": "object"},
        "trace_id": "item1|proposer|proposer_1",
        "deadline_s": 30.0,
    }
    fields.update(kwargs)
    return CompletionRequest(**fields)


# ---------------------------------------------------------------------------
# Roster invariants
# ---------------------------------------------------------------------------


class TestRosterInvariants:
    """A roster must be N independent models, not N endpoints."""

    def test_valid_roster_constructs(self):
        roster = minimal_roster()
        assert [s.seat_name for s in roster.proposers] == ["proposer_1", "proposer_2"]
        assert roster.critic.seat_name == "critic"
        assert roster.meta.seat_name == "meta"

    def test_two_proposers_on_same_canonical_model_refused(self):
        with pytest.raises(RosterError, match="independent"):
            minimal_roster(
                proposers=[
                    seat("proposer_1", "openai/gpt-5.4", endpoint="argo"),
                    seat("proposer_2", "openai/gpt-5.4", endpoint="local", endpoint_kind="openai"),
                ]
            )

    def test_critic_sharing_canonical_with_proposer_refused_across_endpoints(self):
        """The endpoints differ; the model does not. That is one opinion, twice.

        This is the case the canonical-identity rule exists for: the two seats
        look entirely different in the config and are the same model.
        """
        with pytest.raises(RosterError, match="audit its own vote"):
            minimal_roster(
                critic=seat(
                    "critic",
                    "openai/gpt-5.4",
                    endpoint="codex",
                    endpoint_kind="codex_cli",
                    model="gpt-5.4",
                )
            )

    def test_meta_overlapping_a_proposer_refused_by_default(self):
        with pytest.raises(RosterError, match="allow_meta_overlap"):
            minimal_roster(meta=seat("meta", "openai/gpt-5.4"))

    def test_meta_equal_to_critic_refused_by_default(self):
        with pytest.raises(RosterError, match="allow_meta_overlap"):
            minimal_roster(meta=seat("meta", "anthropic/claude-sonnet-5"))

    def test_meta_overlap_allowed_when_opted_in(self):
        roster = minimal_roster(
            meta=seat("meta", "openai/gpt-5.4"), allow_meta_overlap=True
        )
        assert roster.meta.canonical_model == "openai/gpt-5.4"

    @pytest.mark.parametrize("n", [0, 1])
    def test_fewer_than_two_proposers_refused(self, n):
        proposers = [seat(f"proposer_{i}", f"vendor/m{i}") for i in range(1, n + 1)]
        with pytest.raises(RosterError, match="at least 2 proposers"):
            minimal_roster(proposers=proposers)


# ---------------------------------------------------------------------------
# Fingerprints and hashes
# ---------------------------------------------------------------------------


class TestRosterFingerprint:
    """The fingerprint must depend on what changes answers, and only that."""

    def test_stable_under_dict_key_order(self):
        a = minimal_roster(
            proposers=[
                seat("proposer_1", "vendor/a", extra={"top_p": 0.9, "seed": 7}),
                seat("proposer_2", "vendor/b"),
            ]
        )
        b = minimal_roster(
            proposers=[
                seat("proposer_1", "vendor/a", extra={"seed": 7, "top_p": 0.9}),
                seat("proposer_2", "vendor/b"),
            ]
        )
        assert a.fingerprint() == b.fingerprint()

    def test_independent_of_endpoint_alias(self):
        """Two users naming the same route differently get one fingerprint."""
        renamed = minimal_roster(
            proposers=[
                seat("proposer_1", "google/gemini-3.5-flash", endpoint="anl-argo"),
                seat("proposer_2", "openai/gpt-5.4"),
            ]
        )
        assert renamed.fingerprint() == minimal_roster().fingerprint()

    @pytest.mark.parametrize(
        "field,value",
        [
            ("endpoint_kind", "openai"),
            ("model", "something-else"),
            ("canonical_model", "vendor/other"),
            ("max_output_tokens", 8000),
            ("temperature", 0.7),
            ("extra", {"top_p": 0.5}),
        ],
    )
    def test_changes_when_any_hashed_seat_field_changes(self, field, value):
        base = minimal_roster()
        changed = minimal_roster(
            critic=seat("critic", "anthropic/claude-sonnet-5", **{field: value})
        )
        assert changed.fingerprint() != base.fingerprint()

    def test_changes_when_a_seat_moves_role(self):
        base = minimal_roster()
        swapped = minimal_roster(
            proposers=list(reversed(base.proposers)),
        )
        assert swapped.fingerprint() != base.fingerprint()


class TestContentHashes:
    """Hash helpers are the basis of cross-user answer reuse."""

    def test_entity_hash_collapses_whitespace_and_strips(self):
        assert entity_hash("  alpha   beta \n") == entity_hash("alpha beta")
        assert entity_hash("alpha beta") != entity_hash("alpha betas")

    def test_data_hash_stable_under_key_order(self):
        menu = [MenuEntry(id="GO:1", label="one"), MenuEntry(id="GO:2", label="two")]
        a = data_hash({"source": "uniprot", "note": "x"}, menu)
        b = data_hash({"note": "x", "source": "uniprot"}, menu)
        assert a == b

    def test_data_hash_accepts_models_and_dicts_alike(self):
        from_models = data_hash({}, [MenuEntry(id="GO:1", label="one")])
        from_dicts = data_hash(
            {},
            [
                {
                    "id": "GO:1",
                    "label": "one",
                    "description": None,
                    "rank": None,
                    "score": None,
                    "metadata": {},
                }
            ],
        )
        assert from_models == from_dicts

    def test_data_hash_changes_with_evidence_and_with_menu(self):
        menu = [MenuEntry(id="GO:1", label="one")]
        base = data_hash({"source": "uniprot"}, menu)
        assert data_hash({"source": "interpro"}, menu) != base
        assert data_hash({"source": "uniprot"}, menu + [MenuEntry(id="GO:2", label="two")]) != base
        # Menu order is part of the question: it is a ranked list.
        two_entries = [MenuEntry(id="GO:1", label="one"), MenuEntry(id="GO:2", label="two")]
        assert data_hash({}, two_entries) != data_hash({}, list(reversed(two_entries)))

    def prompt_args(self, **overrides):
        args = {
            "seat": seat("proposer_1", "openai/gpt-5.4"),
            "reuse_namespace": "https://argo.example/v1#",
            "effective_params": {"temperature": 0, "max_output_tokens": 4000},
            "json_schema": {"type": "object", "required": ["picked"]},
            "query_name": "go_term_mapping",
            "query_version": "3",
            "renderer_id_version": "go_v3:1",
            "role": "proposer",
            "system": "system prompt",
            "user": "user prompt",
        }
        args.update(overrides)
        return args

    def test_prompt_hash_stable_under_dict_key_order(self):
        a = prompt_hash(
            **self.prompt_args(
                effective_params={"temperature": 0, "max_output_tokens": 4000},
                json_schema={"type": "object", "required": ["picked"]},
            )
        )
        b = prompt_hash(
            **self.prompt_args(
                effective_params={"max_output_tokens": 4000, "temperature": 0},
                json_schema={"required": ["picked"], "type": "object"},
            )
        )
        assert a == b

    @pytest.mark.parametrize(
        "field,value",
        [
            ("reuse_namespace", "https://other.example/v1#"),
            ("effective_params", {"temperature": 1}),
            ("json_schema", {"type": "object"}),
            ("query_name", "reaction_mapping"),
            ("query_version", "4"),
            ("renderer_id_version", "go_v3:2"),
            ("role", "critic"),
            ("system", "different system prompt"),
            ("user", "different user prompt"),
            ("seat", seat("proposer_1", "openai/gpt-5.5")),
        ],
    )
    def test_prompt_hash_changes_with_each_input(self, field, value):
        base = prompt_hash(**self.prompt_args())
        assert prompt_hash(**self.prompt_args(**{field: value})) != base

    def test_prompt_hash_ignores_the_endpoint_alias(self):
        """Same model, same deployment, differently-named endpoints -> one hash.

        This is what lets two users reuse each other's answers: the alias is
        private naming, not part of the question.
        """
        mine = seat("proposer_1", "openai/gpt-5.4", endpoint="argo")
        theirs = seat("seat_x", "openai/gpt-5.4", endpoint="anl-argo-via-proxy")
        assert prompt_hash(**self.prompt_args(seat=mine)) == prompt_hash(
            **self.prompt_args(seat=theirs)
        )

    def test_prompt_hash_separates_endpoint_kinds(self):
        http = seat("proposer_1", "anthropic/claude-sonnet-5", endpoint_kind="argo")
        cli = seat("proposer_1", "anthropic/claude-sonnet-5", endpoint_kind="claude_cli")
        assert prompt_hash(**self.prompt_args(seat=http)) != prompt_hash(
            **self.prompt_args(seat=cli)
        )


class TestRouteIdentity:
    """route_fingerprint records WHERE an answer came from, with no secrets."""

    def test_distinguishes_endpoint_alias_and_account(self):
        spec = seat("proposer_1", "openai/gpt-5.4")
        a = route_fingerprint(spec, {"kind": "argo", "base_url": "https://argo.example/v1"})
        b = route_fingerprint(
            spec, {"kind": "argo", "base_url": "https://argo.example/v1", "account": "other"}
        )
        assert a != b

    def test_ignores_credentials(self):
        spec = seat("proposer_1", "openai/gpt-5.4")
        plain = {"kind": "argo", "base_url": "https://argo.example/v1"}
        with_key = dict(plain, api_key="sekrit")
        assert route_fingerprint(spec, plain) == route_fingerprint(spec, with_key)

    def test_default_reuse_namespace_normalises_http_and_names_cli(self):
        assert default_reuse_namespace(
            {"kind": "openai", "base_url": "HTTP://Poplar.CELS.anl.gov:8301/v1/"}, "qwen3-32b"
        ) == "http://poplar.cels.anl.gov:8301/v1#"
        assert (
            default_reuse_namespace(
                {"kind": "openai", "base_url": "http://x/v1", "deployment_revision": "r2"}, "m"
            )
            == "http://x/v1#r2"
        )
        assert default_reuse_namespace({"kind": "claude_cli"}, "sonnet") == "claude_cli:sonnet"
        assert (
            default_reuse_namespace({"kind": "argo", "reuse_namespace": "shared"}, "m") == "shared"
        )


# ---------------------------------------------------------------------------
# Packaged default configuration
# ---------------------------------------------------------------------------


class TestPackagedDefaultConfig:
    """The shipped default must be a working council with no user setup."""

    def test_every_packaged_profile_builds_a_roster(self):
        cfg = load_council_config()
        assert sorted(cfg.profiles) == ["argo", "mixed-subscription"]
        for profile in cfg.profiles:
            roster = cfg.roster(profile)
            assert len(roster.proposers) == 3
            assert roster.fingerprint()

    def test_the_packaged_config_declares_no_poplar_endpoint(self):
        """Self-hosted models are the user's to declare (review round 2)."""
        cfg = load_council_config()
        assert sorted(cfg.endpoints) == ["argo", "claude", "codex"]
        for name, endpoint in cfg.endpoints.items():
            assert "poplar" not in name
            assert "poplar" not in (endpoint.base_url or "")
        for model in cfg.models.values():
            assert not (model.canonical or "").startswith("poplar/")

    def test_default_profile_is_mixed_subscription(self):
        cfg = load_council_config()
        assert cfg.default_profile == "mixed-subscription"
        assert cfg.roster().critic.model == "sonnet"
        assert cfg.roster().meta.model == "opus"

    def test_on_a_fresh_machine_with_no_kbutillib_config(self, monkeypatch, tmp_path):
        """Nothing monkeypatched but the environment: a brand-new worker box.

        No ``SharedEnvUtils`` stub, no packaged-data stub, an empty HOME, and
        a cwd with no ``config.yaml`` anywhere above it -- so the ONLY source
        of configuration is the packaged default.  This is the state every
        fresh machine starts in, and every packaged profile must still seat.
        """
        home = tmp_path / "fresh-home"
        (home / "sub").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.chdir(home / "sub")
        assert not list(home.rglob("config.yaml"))

        cfg = load_council_config()

        for profile in cfg.profiles:
            roster = cfg.roster(profile)
            assert len(roster.proposers) == 3
            assert roster.critic.canonical_model not in {
                s.canonical_model for s in roster.proposers
            }
        # The default store path is expanded against the running HOME.
        assert cfg.store.startswith("sqlite:///")
        assert str(home) in cfg.store

    def test_codex_seats_are_marked_unverified(self):
        """The Codex served names are a guess until `kbu council doctor` runs."""
        cfg = load_council_config()
        roster = cfg.roster("mixed-subscription")
        unverified = {s.model for s in roster.proposers if not s.verified}
        assert unverified == {"gpt-5.4", "gpt-5.5"}

    def test_cli_endpoints_default_to_one_call_at_a_time(self):
        cfg = load_council_config()
        assert cfg.endpoint("claude")["max_concurrency"] == 1
        assert cfg.endpoint("codex")["max_concurrency"] == 1
        assert cfg.endpoint("argo")["max_concurrency"] == 8

    def test_snapshot_drops_credentials_but_keeps_env_var_names(self):
        cfg = load_council_config(
            config_dict(
                endpoints={
                    "argo": {
                        "kind": "argo",
                        "base_url": "https://argo.example/v1",
                        "api_key": "sekrit",
                        "api_key_env": "ARGO_KEY",
                        "user_env": "ARGO_USER",
                        "max_total_tokens": 1000,
                    },
                    "local": {"kind": "openai", "base_url": "http://localhost:9001/v1"},
                }
            )
        )
        snap = cfg.snapshot()["endpoints"]["argo"]
        assert "api_key" not in snap
        assert snap["api_key_env"] == "ARGO_KEY"
        assert snap["user_env"] == "ARGO_USER"
        # A budget is not a credential, however much its name looks like one.
        assert snap["max_total_tokens"] == 1000
        assert cfg.snapshot()["default_canonical_version"] == DEFAULT_CANONICAL_VERSION


# ---------------------------------------------------------------------------
# Legacy import of Filipe Liu's real file
# ---------------------------------------------------------------------------


class TestFilipeLegacyConfig:
    """His real file is the legacy-import acceptance case."""

    def test_the_fixture_is_the_real_file_shape(self):
        text = FILIPE_CONFIG.read_text()
        assert "model_inference.yaml" in text  # provenance header
        assert "api_key: REDACTED" in text  # and no live key travels with it
        assert "sk-" not in text

    def test_loads_without_a_legacy_flag(self):
        """Its local endpoints omit `kind`, which is the legacy marker."""
        cfg = load_council_config(FILIPE_CONFIG)
        assert cfg.endpoints["argo"].kind == "argo"
        assert cfg.endpoints["gpt-oss-120b"].kind == "openai"
        assert cfg.default_profile == "argo"

    def test_its_now_unknown_supervisor_key_is_dropped_not_fatal(self, caplog):
        """His real file declares `supervisor: true`, which no longer exists.

        Review round 2 removed the flag with the rest of model management.
        A key a config file carries that this build does not know is WARNED
        about and dropped -- never fatal -- because somebody's working file
        must not stop loading when a field is retired.
        """
        with caplog.at_level("WARNING"):
            cfg = load_council_config(FILIPE_CONFIG)
        assert not hasattr(cfg.endpoints["gpt-oss-120b"], "supervisor")
        assert "supervisor" not in cfg.endpoints["gpt-oss-120b"].model_dump()
        assert any("supervisor" in record.message for record in caplog.records)

    def test_expands_the_vllm_host_variable(self, monkeypatch):
        monkeypatch.delenv("VLLM_HOST", raising=False)
        cfg = load_council_config(FILIPE_CONFIG)
        assert cfg.endpoints["qwen3-32b"].base_url == "http://localhost:8302/v1"
        monkeypatch.setenv("VLLM_HOST", "poplar.cels.anl.gov")
        cfg = load_council_config(FILIPE_CONFIG)
        assert cfg.endpoints["qwen3-32b"].base_url == "http://poplar.cels.anl.gov:8302/v1"

    def test_argo_profile_resolves_from_undeclared_aliases(self):
        """His profiles seat ARGO aliases his `models` table never declares.

        The legacy rule declares exactly those on the file's ARGO endpoint and
        takes their identity from DEFAULT_CANONICAL.
        """
        cfg = load_legacy_model_inference(FILIPE_CONFIG)
        roster = cfg.roster("argo")
        assert [s.canonical_model for s in roster.proposers] == [
            DEFAULT_CANONICAL["gemini35flash"],
            DEFAULT_CANONICAL["gpt54"],
            DEFAULT_CANONICAL["gpt55"],
        ]
        assert roster.critic.canonical_model == DEFAULT_CANONICAL["claudesonnet5"]
        assert roster.meta.canonical_model == DEFAULT_CANONICAL["claudeopus45"]
        # Backfilled onto the argo endpoint, served under the alias itself.
        assert cfg.models["gpt55"].endpoint == "argo"
        assert cfg.models["gpt55"].served_name == "gpt55"

    @pytest.mark.parametrize("profile", ["oss", "oss-next"])
    def test_locally_served_profiles_raise_roster_error_as_documented(self, profile):
        """DOCUMENTED FAILURE, not a defect in the loader.

        His nine local models are declared in `models` but carry no
        `canonical` key, and local vLLM deployments are deliberately absent
        from DEFAULT_CANONICAL (they are not vendor models). Seating one
        therefore raises RosterError naming the alias, which is the designed
        behaviour: a council cannot assert that two seats are different models
        without an identity for each. Adding `canonical: poplar/<alias>` --
        exactly what KBUtilLib's own default config does for the same nine
        models -- is the fix, demonstrated below.
        """
        cfg = load_council_config(FILIPE_CONFIG)
        with pytest.raises(RosterError, match="no canonical identity"):
            cfg.roster(profile)

    def test_the_same_profile_resolves_once_identities_are_declared(self):
        import yaml

        raw = yaml.safe_load(FILIPE_CONFIG.read_text())
        for alias, entry in raw["models"].items():
            entry["canonical"] = f"poplar/{alias}"
        cfg = load_council_config(raw)
        roster = cfg.roster("oss-next")
        assert [s.canonical_model for s in roster.proposers] == [
            "poplar/qwen3.8-27b",
            "poplar/gemma-4-31b",
            "poplar/nemotron-3-super",
        ]


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    """Every rule here fails the load rather than failing a paid call."""

    def test_undeclared_alias_is_a_config_error_even_with_an_argo_endpoint(self):
        """No fall-through to a same-named ARGO model in the native format.

        The config below HAS an ARGO endpoint, so the only thing stopping
        `gpt55` from being silently seated on it is that this is not a legacy
        file. A typo must not quietly bill a different model.
        """
        raw = config_dict()
        raw["profiles"]["p"]["critic"] = "gpt55"
        with pytest.raises(ConfigError, match="not declared in `models`"):
            load_council_config(raw)

    def test_billing_api_on_a_claude_cli_endpoint_is_refused(self):
        raw = config_dict()
        raw["endpoints"]["claude"] = {
            "kind": "claude_cli",
            "executable": "claude",
            "billing": "api",
        }
        with pytest.raises(ConfigError, match="billing: api"):
            load_council_config(raw)

    def test_billing_api_on_a_codex_cli_endpoint_is_refused(self):
        raw = config_dict()
        raw["endpoints"]["codex"] = {"kind": "codex_cli", "billing": "api"}
        with pytest.raises(ConfigError, match="billing: api"):
            load_council_config(raw)

    def test_subscription_billing_on_a_cli_endpoint_is_fine(self):
        raw = config_dict()
        raw["endpoints"]["codex"] = {"kind": "codex_cli"}
        cfg = load_council_config(raw)
        assert cfg.endpoint("codex")["billing"] == "subscription"
        # The executable defaults to the kind's own binary.
        assert cfg.endpoint("codex")["executable"] == "codex"

    def test_max_total_tokens_on_codex_is_refused(self):
        raw = config_dict()
        raw["endpoints"]["codex"] = {"kind": "codex_cli", "max_total_tokens": 100000}
        with pytest.raises(ConfigError, match="reports no token usage"):
            load_council_config(raw)

    def test_max_total_tokens_on_claude_cli_is_allowed(self):
        raw = config_dict()
        raw["endpoints"]["claude"] = {"kind": "claude_cli", "max_total_tokens": 100000}
        assert load_council_config(raw).endpoint("claude")["max_total_tokens"] == 100000

    def test_seat_without_canonical_identity_raises_roster_error(self):
        raw = config_dict()
        raw["models"]["nameless"] = {"endpoint": "local"}
        raw["profiles"]["p"]["critic"] = "nameless"
        cfg = load_council_config(raw)
        with pytest.raises(RosterError, match="no canonical identity"):
            cfg.roster("p")

    def test_no_endpoint_served_name_fallback_for_identity(self):
        """There is deliberately no "<endpoint>:<served_name>" identity.

        A synthesised identity would make two seats on the same real model
        compare as different models, defeating the independence check.
        """
        raw = config_dict()
        raw["models"]["nameless"] = {"endpoint": "local", "served_name": "mystery-7b"}
        raw["profiles"]["q"] = {
            "proposers": ["a", "nameless"],
            "critic": "c",
            "meta": "d",
        }
        with pytest.raises(RosterError):
            load_council_config(raw).roster("q")

    def test_model_on_an_undeclared_endpoint_is_a_config_error(self):
        raw = config_dict()
        raw["models"]["a"] = {"endpoint": "nowhere", "canonical": "vendor/a"}
        with pytest.raises(ConfigError, match="not.*declared"):
            load_council_config(raw)

    def test_unknown_default_profile_is_a_config_error(self):
        raw = config_dict(default_profile="nope")
        with pytest.raises(ConfigError, match="default_profile"):
            load_council_config(raw)

    def test_http_endpoint_without_base_url_is_a_config_error(self):
        raw = config_dict()
        raw["endpoints"]["broken"] = {"kind": "openai"}
        with pytest.raises(ConfigError, match="no base_url"):
            load_council_config(raw)

    def test_unknown_keys_are_warned_and_ignored(self, caplog):
        raw = config_dict()
        raw["endpoints"]["argo"]["frobnicate"] = True
        raw["models"]["a"]["colour"] = "blue"
        raw["profiles"]["p"]["mood"] = "cautious"
        raw["galaxy"] = "andromeda"
        with caplog.at_level(logging.WARNING):
            cfg = load_council_config(raw)
        messages = " ".join(r.getMessage() for r in caplog.records)
        for unknown in ("frobnicate", "colour", "mood", "galaxy"):
            assert unknown in messages
        assert cfg.roster("p")  # and the config still loads

    def test_sqlite_store_inside_dropbox_is_refused(self, tmp_path):
        dropbox = tmp_path / "Dropbox" / "council.sqlite"
        with pytest.raises(ConfigError, match="synced directory"):
            load_council_config(config_dict(store=f"sqlite:///{dropbox}"))

    def test_sqlite_store_outside_dropbox_is_kept_and_expanded(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        cfg = load_council_config(config_dict(store="sqlite:///~/.kbutillib/c.sqlite"))
        assert cfg.store == f"sqlite:///{tmp_path}/.kbutillib/c.sqlite"

    def test_a_non_sqlite_store_url_is_carried_verbatim(self):
        """Config validation does not interpret the URL; ``open_store`` does.

        So a ``postgresql+psycopg://`` URL left in somebody's config survives
        load unchanged and is refused, naming review round 2, the moment the
        store is actually opened -- see
        ``test_store_contract.TestSqliteEntryPoint``.
        """
        url = "postgresql+psycopg://user@db.example/council"
        assert load_council_config(config_dict(store=url)).store == url

    def test_the_user_key_is_read_and_defaults_to_none(self):
        assert load_council_config(config_dict(user="someone")).user == "someone"
        assert load_council_config(config_dict()).user is None
        # Blank is "nothing said", not a label of the empty string.
        assert load_council_config(config_dict(user="   ")).user is None

    def test_unknown_endpoint_lookup_is_a_config_error(self):
        with pytest.raises(ConfigError, match="unknown endpoint"):
            load_council_config(config_dict()).endpoint("nope")


# ---------------------------------------------------------------------------
# Queue partitioning
# ---------------------------------------------------------------------------


class TestQueueConfig:
    """``queues`` is additive: concurrency is owned by the ACCOUNT, not the
    seat, so validation has to resolve every declared profile (an override
    can move a seat to a different account per job) before it can promise a
    worker-process split will never over-subscribe one.
    """

    def _full_coverage(self, **per_queue) -> dict:
        """The four queues that cover every seat/role in ``queue_config_dict``.

        A helper so each scenario test only has to override the one queue it
        cares about -- otherwise every test would have to restate full
        coverage, and a coverage-refusal from an unrelated queue would be
        mistaken for the thing under test.
        """
        queues = {
            "qA": {"seats": ["proposer_a"], "concurrency": 4},
            "qB": {"seats": ["proposer_b"], "concurrency": 4},
            "qC": {"seats": ["critic"], "concurrency": 4},
            "qD": {"seats": ["meta"], "concurrency": 1},
        }
        for name, fields in per_queue.items():
            queues[name] = fields
        return queues

    # -- the four-queue config: three chairs, one combined critic/meta ----

    def test_four_queue_config_loads_and_round_trips(self):
        raw = {
            "endpoints": {
                "codex": {"kind": "codex_cli", "account": "codex-chatgpt", "max_concurrency": 8},
                "argo": {"kind": "argo", "base_url": "https://argo.example/v1"},
                "argo2": {"kind": "argo", "base_url": "https://argo2.example/v1"},
            },
            "models": {
                "sol": {"endpoint": "codex", "canonical": "openai/gpt-5.6-sol"},
                "terra": {"endpoint": "codex", "canonical": "openai/gpt-5.6-terra"},
                "flash": {"endpoint": "argo", "canonical": "google/gemini-3.5-flash"},
                "critic_model": {"endpoint": "argo2", "canonical": "openai/gpt-5.6-critic"},
                "meta_model": {"endpoint": "argo2", "canonical": "google/gemini-3.5-meta"},
            },
            "profiles": {
                "main": {
                    "proposers": {
                        "chair_sol": "sol",
                        "chair_terra": "terra",
                        "chair_flash": "flash",
                    },
                    "critic": "critic_model",
                    "meta": "meta_model",
                },
            },
            "default_profile": "main",
            "store": "sqlite:///council-test.sqlite",
            "queues": {
                "chair-sol": {"seats": ["chair_sol"], "concurrency": 4},
                "chair-terra": {"seats": ["chair_terra"], "concurrency": 4},
                "chair-flash": {"seats": ["chair_flash"], "concurrency": 8},
                "critic-meta": {"seats": ["critic", "meta"], "concurrency": 1},
            },
        }
        cfg = load_council_config(raw)
        assert sorted(cfg.queues) == ["chair-flash", "chair-sol", "chair-terra", "critic-meta"]
        assert cfg.queues["critic-meta"].seats == ["critic", "meta"]
        assert cfg.queues["chair-sol"].concurrency == 4
        assert cfg.queues["chair-sol"].workers == 1
        assert cfg.queues["chair-sol"].batch_size == 1
        assert cfg.roster("main").fingerprint()

        # round-trip: dump the queues section back out and reload it.
        dumped = {name: q.model_dump() for name, q in cfg.queues.items()}
        reloaded = dict(raw)
        reloaded["queues"] = dumped
        cfg2 = load_council_config(reloaded)
        assert cfg2.queues == cfg.queues

    # -- duplicate seat -----------------------------------------------------

    def test_a_seat_in_two_queues_is_refused_naming_both_queues(self):
        queues = self._full_coverage(qB={"seats": ["proposer_a", "proposer_b"], "concurrency": 4})
        with pytest.raises(ConfigError, match="qA.*qB|qB.*qA"):
            load_council_config(queue_config_dict(queues))

    # -- account over-subscription ------------------------------------------

    def test_two_queues_over_an_accounts_cap_are_refused(self):
        queues = self._full_coverage(qA={"seats": ["proposer_a"], "concurrency": 5})
        # qA (5) + qC (4), both on acct-a, sum to 9 > cap 8.
        with pytest.raises(ConfigError) as excinfo:
            load_council_config(queue_config_dict(queues))
        message = str(excinfo.value)
        assert "acct-a" in message
        assert "8" in message
        assert "9" in message
        assert "qA" in message and "qC" in message

    def test_two_queues_summing_exactly_to_the_cap_are_accepted(self):
        # qA (4) + qC (4) on acct-a sum to exactly 8, the endpoint's cap.
        queues = self._full_coverage()
        cfg = load_council_config(queue_config_dict(queues))
        assert cfg.queues["qA"].concurrency == 4
        assert cfg.queues["qC"].concurrency == 4

    def test_workers_multiplies_into_the_account_sum(self):
        # qB alone: concurrency 5 * workers 2 = 10 > acct-b's cap of 8.
        queues = self._full_coverage(qB={"seats": ["proposer_b"], "concurrency": 5, "workers": 2})
        with pytest.raises(ConfigError, match="sum to concurrency 10"):
            load_council_config(queue_config_dict(queues))

    def test_a_profile_override_moving_a_seat_to_another_account_is_caught(self):
        """The subtle case: two DECLARED profiles move `critic` across accounts.

        `roster(profile, overrides)` lets a job move a seat per submission,
        which this static validation cannot see -- but two profiles that
        already disagree on which account a seat lands on must still be
        caught, because validating only `default_profile` would make the
        no-over-subscription promise false for the second profile's jobs.
        """
        raw = queue_config_dict(self._full_coverage())
        raw["models"]["m_critic2"] = {"endpoint": "epB", "canonical": "vendor/critic2"}
        raw["profiles"]["p2"] = {
            "proposers": {"proposer_a": "m_p1", "proposer_b": "m_p2"},
            "critic": "m_critic2",
            "meta": "m_meta",
        }
        with pytest.raises(ConfigError, match="more than one account"):
            load_council_config(raw)

    # -- coverage -------------------------------------------------------------

    def test_a_config_omitting_meta_coverage_is_refused(self):
        queues = {
            "qA": {"seats": ["proposer_a"], "concurrency": 4},
            "qB": {"seats": ["proposer_b"], "concurrency": 4},
            "qC": {"seats": ["critic"], "concurrency": 4},
        }
        with pytest.raises(ConfigError, match="'meta'"):
            load_council_config(queue_config_dict(queues))

    # -- one account per queue -------------------------------------------------

    def test_a_multi_account_queue_is_refused(self):
        queues = {
            "qA": {"seats": ["proposer_a", "proposer_b"], "concurrency": 4},
            "qC": {"seats": ["critic"], "concurrency": 4},
            "qD": {"seats": ["meta"], "concurrency": 1},
        }
        with pytest.raises(ConfigError, match="more than one account"):
            load_council_config(queue_config_dict(queues))

    # -- batch_size -------------------------------------------------------------

    def test_batch_size_above_one_on_a_non_cli_seat_is_refused(self):
        # `meta` sits on epC, which is an argo (non-CLI) endpoint.
        queues = self._full_coverage(qD={"seats": ["meta"], "concurrency": 1, "batch_size": 2})
        with pytest.raises(ConfigError, match="not a batchable backend") as excinfo:
            load_council_config(queue_config_dict(queues))
        message = str(excinfo.value)
        assert "meta" in message
        assert "argo" in message

    def test_batch_size_above_one_on_a_cli_seat_is_allowed(self):
        queues = self._full_coverage(qA={"seats": ["proposer_a"], "concurrency": 1, "batch_size": 20})
        cfg = load_council_config(queue_config_dict(queues))
        assert cfg.queues["qA"].batch_size == 20

    def test_batch_size_defaults_to_one_not_twenty(self):
        cfg = load_council_config(queue_config_dict(self._full_coverage()))
        assert cfg.queues["qA"].batch_size == 1

    @pytest.mark.parametrize("batch_size", [0, 51])
    def test_batch_size_out_of_range_is_refused(self, batch_size):
        queues = self._full_coverage(qA={"seats": ["proposer_a"], "concurrency": 1, "batch_size": batch_size})
        with pytest.raises(ConfigError, match="batch_size"):
            load_council_config(queue_config_dict(queues))

    # -- numeric field validation ----------------------------------------------

    def test_workers_and_concurrency_must_be_at_least_one(self):
        queues = self._full_coverage(qA={"seats": ["proposer_a"], "concurrency": 0})
        with pytest.raises(ConfigError, match="concurrency"):
            load_council_config(queue_config_dict(queues))

        queues = self._full_coverage(qA={"seats": ["proposer_a"], "concurrency": 1, "workers": 0})
        with pytest.raises(ConfigError, match="workers"):
            load_council_config(queue_config_dict(queues))

    def test_a_queue_with_no_seats_is_refused(self):
        queues = self._full_coverage(qA={"seats": [], "concurrency": 1})
        with pytest.raises(ConfigError, match="seats"):
            load_council_config(queue_config_dict(queues))

    # -- additive / opt-in ------------------------------------------------------

    def test_a_config_with_no_queues_loads_unchanged_from_today(self):
        cfg = load_council_config(config_dict())
        assert cfg.queues == {}
        assert cfg.cli_temp_root is None
        # unchanged: the default profile still resolves exactly as before.
        assert cfg.roster("p").fingerprint()

    def test_cli_temp_root_is_declared_and_expanded(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        cfg = load_council_config(config_dict(cli_temp_root="~/cli-temp"))
        assert cfg.cli_temp_root == str(tmp_path / "cli-temp")

    # -- per-entry merge ------------------------------------------------------

    def test_the_per_entry_merge_rule_applies_to_queues(self):
        base = {
            "queues": {
                "q1": {"seats": ["a"], "concurrency": 1},
                "q2": {"seats": ["b"], "concurrency": 2},
            }
        }
        overlay = {"queues": {"q1": {"concurrency": 5}}}
        merged = _merge(base, overlay)
        # q1 is overridden but keeps its untouched `seats`; q2 is untouched.
        assert merged["queues"]["q1"] == {"seats": ["a"], "concurrency": 5}
        assert merged["queues"]["q2"] == {"seats": ["b"], "concurrency": 2}

    # -- the per-job rejection predicate ---------------------------------------

    def test_check_roster_against_queues_accepts_the_validated_default(self):
        cfg = load_council_config(queue_config_dict(self._full_coverage()))
        cfg.check_roster_against_queues(cfg.roster("p"))  # must not raise

    def test_check_roster_against_queues_refuses_an_override_moving_the_account(self):
        raw = queue_config_dict(self._full_coverage())
        raw["models"]["m_critic2"] = {"endpoint": "epB", "canonical": "vendor/critic2"}
        cfg = load_council_config(raw)
        moved = cfg.roster("p", overrides={"critic": "m_critic2"})
        with pytest.raises(ConfigError, match="qC"):
            cfg.check_roster_against_queues(moved, job_id="job-42")

    def test_check_roster_against_queues_is_a_noop_without_queues(self):
        cfg = load_council_config(config_dict())
        cfg.check_roster_against_queues(cfg.roster("p"))  # must not raise


class TestVariableExpansion:
    """${VAR} / ${VAR:-default} in string values."""

    def test_default_is_used_when_unset(self, monkeypatch):
        monkeypatch.delenv("KBU_TEST_HOST", raising=False)
        raw = config_dict()
        raw["endpoints"]["local"]["base_url"] = "http://${KBU_TEST_HOST:-localhost}:9001/v1"
        cfg = load_council_config(raw)
        assert cfg.endpoint("local")["base_url"] == "http://localhost:9001/v1"

    def test_environment_wins_over_the_default(self, monkeypatch):
        monkeypatch.setenv("KBU_TEST_HOST", "poplar")
        raw = config_dict()
        raw["endpoints"]["local"]["base_url"] = "http://${KBU_TEST_HOST:-localhost}:9001/v1"
        cfg = load_council_config(raw)
        assert cfg.endpoint("local")["base_url"] == "http://poplar:9001/v1"

    def test_unset_without_a_default_expands_to_empty(self, monkeypatch):
        monkeypatch.delenv("KBU_TEST_PROXY", raising=False)
        raw = config_dict()
        raw["endpoints"]["argo"]["proxy"] = "${KBU_TEST_PROXY:-}"
        raw["endpoints"]["local"]["proxy"] = "${KBU_TEST_PROXY}"
        cfg = load_council_config(raw)
        assert cfg.endpoint("argo")["proxy"] == ""
        assert cfg.endpoint("local")["proxy"] == ""


class TestSeatOverrides:
    """Overrides are keyed by seat name, not by role-and-guesswork."""

    def test_critic_and_meta_by_name(self):
        cfg = load_council_config(config_dict())
        roster = cfg.roster("p", overrides={"critic": "d", "meta": "c"})
        assert roster.critic.canonical_model == "vendor/d"
        assert roster.meta.canonical_model == "vendor/c"

    def test_single_proposer_seat_by_name(self):
        cfg = load_council_config(config_dict())
        roster = cfg.roster("p", overrides={"proposer_2": "e"})
        assert [s.canonical_model for s in roster.proposers] == ["vendor/a", "vendor/e"]
        assert [s.seat_name for s in roster.proposers] == ["proposer_1", "proposer_2"]

    def test_proposers_list_replaces_the_bench(self):
        cfg = load_council_config(config_dict())
        roster = cfg.roster("p", overrides={"proposers": ["b", "e", "a"]})
        assert [s.seat_name for s in roster.proposers] == [
            "proposer_1",
            "proposer_2",
            "proposer_3",
        ]
        assert [s.canonical_model for s in roster.proposers] == [
            "vendor/b",
            "vendor/e",
            "vendor/a",
        ]

    def test_profile_may_name_its_own_proposer_seats(self):
        raw = config_dict()
        raw["profiles"]["named"] = {
            "proposers": {"fast": "a", "slow": "b"},
            "critic": "c",
            "meta": "d",
        }
        cfg = load_council_config(raw)
        roster = cfg.roster("named", overrides={"slow": "e"})
        assert [s.seat_name for s in roster.proposers] == ["fast", "slow"]
        assert [s.canonical_model for s in roster.proposers] == ["vendor/a", "vendor/e"]

    def test_override_for_a_seat_that_does_not_exist_is_refused(self):
        cfg = load_council_config(config_dict())
        with pytest.raises(ConfigError, match="not a seat in profile"):
            cfg.roster("p", overrides={"proposer_9": "d"})

    def test_override_to_an_undeclared_alias_is_refused(self):
        cfg = load_council_config(config_dict())
        with pytest.raises(ConfigError, match="not declared"):
            cfg.roster("p", overrides={"critic": "gpt55"})

    def test_unknown_profile_is_refused(self):
        with pytest.raises(ConfigError, match="unknown profile"):
            load_council_config(config_dict()).roster("nope")

    def test_model_params_land_on_the_seat(self):
        raw = config_dict()
        raw["models"]["a"]["params"] = {
            "temperature": 0.3,
            "max_output_tokens": 1234,
            "top_p": 0.8,
        }
        seat_spec = load_council_config(raw).roster("p").proposers[0]
        assert seat_spec.temperature == 0.3
        assert seat_spec.max_output_tokens == 1234
        assert seat_spec.extra == {"top_p": 0.8}


# ---------------------------------------------------------------------------
# Test backends
# ---------------------------------------------------------------------------


class TestFakeBackend:
    """The fake must honour the one hard rule: never raise for a failure."""

    def test_returns_the_scripted_dict_as_parsed(self):
        answer = {"picked": [{"id": "GO:1", "confidence": "high", "why": "because"}]}
        backend = FakeBackend(lambda req: answer)
        result = run(backend.complete(request_for(seat("proposer_1", "vendor/a"))))
        assert result.ok is True
        assert result.parsed == answer
        assert result.resolved_model == "a"
        assert result.error is None
        assert result.usage == {"prompt_tokens": 0, "completion_tokens": 0}

    def test_records_every_request(self):
        backend = FakeBackend(lambda req: {"picked": []})
        for i in range(3):
            run(backend.complete(request_for(seat(f"proposer_{i}", f"vendor/m{i}"))))
        assert len(backend.calls) == 3
        assert [c.seat.seat_name for c in backend.calls] == [
            "proposer_0",
            "proposer_1",
            "proposer_2",
        ]

    def test_returned_council_error_becomes_a_failed_result(self):
        error = CouncilError(kind="quota_exhausted", message="out", halts_endpoint=True)
        backend = FakeBackend(lambda req: error)
        result = run(backend.complete(request_for(seat("proposer_1", "vendor/a"))))
        assert result.ok is False
        assert result.error.kind == "quota_exhausted"
        assert result.error.halts_endpoint is True

    def test_returned_exception_becomes_a_failed_result(self):
        backend = FakeBackend(lambda req: TransportError("connection reset"))
        result = run(backend.complete(request_for(seat("proposer_1", "vendor/a"))))
        assert result.ok is False
        assert result.error.kind == "transport"
        assert result.error.retryable is True

    def test_raised_exception_is_converted_not_propagated(self):
        def boom(req):
            raise RuntimeError("scripted explosion")

        backend = FakeBackend(boom)
        result = run(backend.complete(request_for(seat("proposer_1", "vendor/a"))))
        assert result.ok is False
        assert result.error.kind == "transport"
        assert "scripted explosion" in result.error.message

    def test_non_dict_answer_is_invalid_output(self):
        backend = FakeBackend(lambda req: "not a dict")
        result = run(backend.complete(request_for(seat("proposer_1", "vendor/a"))))
        assert result.ok is False
        assert result.error.kind == "invalid_output"

    def test_script_sees_the_request(self):
        seen = {}

        def script(req):
            seen["role"] = req.role
            seen["model"] = req.seat.model
            return {"picked": []}

        backend = FakeBackend(lambda req: script(req))
        run(backend.complete(request_for(seat("critic", "vendor/c"), role="critic")))
        assert seen == {"role": "critic", "model": "c"}

    def test_kind_and_protocol_surface(self):
        backend = FakeBackend(lambda req: {}, kind="fake")
        assert backend.kind == "fake"
        assert run(backend.models()) == []
        assert backend.effective_params(seat("proposer_1", "vendor/a"))["temperature"] == 0
        assert run(backend.aclose()) is None


class TestReplayBackend:
    """Replay answers from a transcript, and fail only the missing call."""

    table = {
        ("item1", "proposer", "proposer_1"): {"picked": [{"id": "GO:1"}]},
        ("item1", "critic", "critic"): {"verdict": "consistent"},
    }

    def test_replays_a_recorded_answer(self):
        backend = ReplayBackend(self.table)
        result = run(
            backend.complete(
                request_for(seat("proposer_1", "vendor/a"), trace_id="item1|proposer|proposer_1")
            )
        )
        assert result.ok is True
        assert result.parsed == {"picked": [{"id": "GO:1"}]}

    def test_replays_by_role_as_well_as_seat(self):
        backend = ReplayBackend(self.table)
        result = run(
            backend.complete(
                request_for(
                    seat("critic", "vendor/c"), role="critic", trace_id="item1|critic|critic"
                )
            )
        )
        assert result.parsed == {"verdict": "consistent"}

    def test_missing_recording_is_invalid_output_not_a_raise(self):
        backend = ReplayBackend(self.table)
        result = run(
            backend.complete(
                request_for(seat("proposer_1", "vendor/a"), trace_id="item9|proposer|proposer_1")
            )
        )
        assert result.ok is False
        assert result.error.kind == "invalid_output"
        assert "item9" in result.error.message

    def test_malformed_trace_id_is_invalid_output(self):
        backend = ReplayBackend(self.table)
        result = run(backend.complete(request_for(seat("proposer_1", "vendor/a"), trace_id="junk")))
        assert result.ok is False
        assert result.error.kind == "invalid_output"
        assert "task_id|role|seat_name" in result.error.message

    def test_records_every_request(self):
        backend = ReplayBackend(self.table)
        run(backend.complete(request_for(seat("proposer_1", "vendor/a"))))
        assert len(backend.calls) == 1
        assert backend.kind == "replay"


# ---------------------------------------------------------------------------
# Record shapes the rest of the package is written against
# ---------------------------------------------------------------------------


class TestRecordShapes:
    """Light coverage of the records later milestones persist."""

    def test_task_and_menu_accept_the_go_evidence_shape(self):
        task = CouncilTask(
            item_id="P12345",
            evidence={"text": "catalyses X", "source": "uniprot"},
            menu=[{"id": "GO:1", "label": "one", "rank": 1}],
        )
        assert task.menu[0].id == "GO:1"
        assert task.menu[0].description is None

    def test_ballot_coerces_pick_dicts(self):
        ballot = Ballot(
            seat="proposer_1",
            picks=[{"id": "GO:1", "confidence": "high", "why": "evidence names it"}],
            overall_confidence="high",
        )
        assert ballot.picks[0].id == "GO:1"
        assert ballot.status == "ok"

    def test_error_ballot_carries_a_serialisable_error(self):
        ballot = Ballot(
            seat="proposer_2",
            status="error",
            error=TransportError("reset").to_error(),
        )
        assert ballot.model_dump()["error"]["kind"] == "transport"
        assert ballot.error.retryable is True

    def test_backend_protocol_is_satisfied_by_the_fakes(self):
        from kbutillib.domains.ai.council import Backend

        assert isinstance(FakeBackend(lambda req: {}), Backend)
        assert isinstance(ReplayBackend({}), Backend)
