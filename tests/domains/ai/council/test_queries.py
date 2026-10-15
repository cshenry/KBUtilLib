"""Tests for ``council.queries``: specs, renderers, adapters and the registry.

The load-bearing assertion in this module is BYTE IDENTITY.  The two builtin
query types are verbatim ports of Jose Faria's CONCORDIA councils, and the
only way to know a port is still a port is to compare its output against text
the reference itself produced.  The ``fixtures/queries/*.txt`` files were
generated once, during development, by calling the reference modules'
own prompt constants and block renderers on the ``*_case.json`` inputs
committed beside them.  Nothing here imports those modules: they live on
``/scratch`` on one machine, they import ``openai``, and a test that needed
them would be a test that only runs on poplar.

Nothing in this module makes a network call, spends a token, needs a CLI
login or needs a GPU.  The only I/O is a SQLite file under ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kbutillib.domains.ai.council.queries import (
    NonGenericSchema,
    QueryRegistry,
    QueryType,
    TemplateBindingError,
    UnknownAdapter,
    UnknownRenderer,
    generic_schema_for,
    generic_schemas,
)
from kbutillib.domains.ai.council.queries.builtin import (
    BUILTIN_ADAPTERS,
    BUILTIN_QUERY_TYPES,
    BUILTIN_RENDERERS,
    load_builtin_spec,
)
from kbutillib.domains.ai.council.queries.builtin.adapters import (
    pickup_critic_approved,
)
from kbutillib.domains.ai.council.queries.registry import validate_spec
from kbutillib.domains.ai.council.queries.spec import PACKAGE_DIR
from kbutillib.domains.ai.council.store import open_store
from kbutillib.domains.ai.council.store.errors import QueryTypeExists
from kbutillib.domains.ai.council.types import CouncilTask, InvalidOutput

FIXTURES = Path(__file__).parent / "fixtures" / "queries"
ROLES = ("proposer", "critic", "meta")


# ---------------------------------------------------------------------------
# Fixture cases
# ---------------------------------------------------------------------------


def load_case(name: str) -> dict:
    """One committed fixture case (task, votes, ballots, critic)."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


def expected(name: str, role: str) -> tuple:
    """The ``(system, user)`` text the reference produced for this case."""
    return (
        (FIXTURES / f"{name}.{role}.system.txt").read_text(),
        (FIXTURES / f"{name}.{role}.user.txt").read_text(),
    )


@pytest.fixture()
def go_spec() -> QueryType:
    return validate_spec(load_builtin_spec("go_mf"))


@pytest.fixture()
def pickup_spec() -> QueryType:
    return validate_spec(load_builtin_spec("reaction_pickup"))


@pytest.fixture()
def example_spec() -> QueryType:
    return validate_spec(PACKAGE_DIR / "examples" / "custom_example.yaml")


def go_contexts(case: dict) -> dict:
    """The per-role context the engine hands a GO renderer."""
    return {
        "proposer": {},
        "critic": {"votes": case["votes"], "ballots": case["ballots"]},
        "meta": {
            "votes": case["votes"],
            "ballots": case["ballots"],
            "critic": case["critic"],
        },
    }


def pickup_contexts(case: dict, spec: QueryType) -> dict:
    """The per-role context the engine hands a pickup renderer.

    The meta context's critic is the ADAPTED reply, not the raw one -- the
    engine only ever holds generic shapes -- which is exactly why the adapter
    has to keep the reference reply under ``"raw"``.
    """
    return {
        "proposer": {},
        "critic": {"votes": case["votes"], "ballots": case["ballots"]},
        "meta": {
            "votes": case["votes"],
            "ballots": case["ballots"],
            "critic": spec.postprocess("critic", case["critic_raw"]),
        },
    }


# ---------------------------------------------------------------------------
# Byte-for-byte rendering against the reference
# ---------------------------------------------------------------------------


class TestBuiltinRenderingIsByteIdentical:
    """Every role of both builtins, against text the reference produced."""

    @pytest.mark.parametrize("role", ROLES)
    def test_go_mf(self, go_spec, role):
        case = load_case("go_mf_case")
        task = CouncilTask.model_validate(case["task"])
        system, user = go_spec.render(role, task, go_contexts(case)[role])
        assert (system, user) == expected("go_mf", role)

    @pytest.mark.parametrize("role", ROLES)
    def test_reaction_pickup(self, pickup_spec, role):
        case = load_case("reaction_pickup_case")
        task = CouncilTask.model_validate(case["task"])
        context = pickup_contexts(case, pickup_spec)[role]
        system, user = pickup_spec.render(role, task, context)
        assert (system, user) == expected("reaction_pickup", role)

    def test_reaction_pickup_without_a_promiscuous_ec_cluster(self, pickup_spec):
        """The 5-pick cap and the base system prompt, with no hint spliced in.

        The promiscuity fragment fires on four or more same-EC candidates, so
        the byte-identity check above only ever exercises the 12-cap branch.
        This case has one.
        """
        case = load_case("reaction_pickup_plain_case")
        task = CouncilTask.model_validate(case["task"])
        system, user = pickup_spec.render("proposer", task, {})
        assert (system, user) == expected("reaction_pickup_plain", "proposer")
        assert "PROMISCUOUS ENZYME CLASS DETECTED" not in system
        assert "Pick 0 to 5 reactions." in user

    def test_the_promiscuous_case_does_splice_the_fragment(self, pickup_spec):
        case = load_case("reaction_pickup_case")
        task = CouncilTask.model_validate(case["task"])
        system, user = pickup_spec.render("proposer", task, {})
        assert "PROMISCUOUS ENZYME CLASS DETECTED" in system
        assert "At least 5 of the top 22 candidates share the same EC (1.2.1.3)." in system
        assert "Pick 0 to 12 reactions." in user

    def test_the_critic_sees_the_full_candidate_rendering_and_the_top_20(
        self, pickup_spec
    ):
        """Pickup's critic gets equations, EC and scores -- not GO's short block.

        And it is cut to 20 of the 22 candidates, which is the rule the
        reference's ``top_k`` enforces in both the candidate block and the
        vote table.
        """
        case = load_case("reaction_pickup_case")
        task = CouncilTask.model_validate(case["task"])
        _, user = pickup_spec.render(
            "critic", task, pickup_contexts(case, pickup_spec)["critic"]
        )
        assert "Top-20 ranked candidates" in user
        assert "<=>" in user  # equations are present
        assert "| 1.2.1.3 |" in user  # ECs are present
        assert "| 0.920 |" in user  # scores are present
        # Candidate 21 is past the cut, so it is in neither the candidate
        # block nor the vote table -- though it IS still in the rationale
        # block, because a seat did pick it.  That asymmetry is the
        # reference's, faithfully reproduced: the critic is shown every
        # rationale but only judges the top 20.
        assert "| rxn05021 |" not in user
        assert "\n  rxn05021: " not in user
        assert "| rxn05020 |" in user
        assert "\n  rxn05020: 0" in user
        assert "rxn05021 [low] --" in user


# ---------------------------------------------------------------------------
# A user-registered, template-rendered query type
# ---------------------------------------------------------------------------


class TestUserQueryType:
    """The shipped worked example: templates, validation, postprocess."""

    def test_the_shipped_example_validates(self, example_spec):
        assert example_spec.name == "ec_number_example"
        assert example_spec.renderer is None
        assert example_spec.adapter is None

    def test_it_renders_every_role_from_its_own_templates(self, example_spec):
        task = CouncilTask(
            item_id="adhE",
            evidence={"annotation": "alcohol dehydrogenase", "organism": "E. coli"},
            menu=[
                {
                    "id": "1.1.1.1",
                    "label": "alcohol dehydrogenase",
                    "description": "An alcohol + NAD+ = an aldehyde or ketone + NADH.",
                    "rank": 1,
                    "score": 0.91,
                    "metadata": {"definition_source": "ExplorEnz"},
                },
                {
                    "id": "1.2.1.10",
                    "label": "acetaldehyde dehydrogenase (acetylating)",
                    "description": "Acetaldehyde + CoA + NAD+ = acetyl-CoA + NADH + H+.",
                    "rank": 2,
                    "score": 0.74,
                    "metadata": {},
                },
            ],
        )
        _, proposer = example_spec.render("proposer", task, {})
        assert 'Annotation string: "alcohol dehydrogenase"' in proposer
        assert "  1 | 1.1.1.1 | 0.910 | alcohol dehydrogenase | An alcohol" in proposer

        context = {
            "votes": {"1.1.1.1": 2, "1.2.1.10": 1},
            "ballots": [
                {
                    "seat": "a",
                    "status": "ok",
                    "picks": [{"id": "1.1.1.1", "confidence": "high", "why": "exact"}],
                    "overall_confidence": "high",
                },
                {
                    "seat": "b",
                    "status": "error",
                    "error": {"kind": "timeout", "message": "deadline"},
                },
            ],
            "critic": {"verdict": "consistent", "reason": "fine"},
        }
        _, critic = example_spec.render("critic", task, context)
        assert "  1 | 1.1.1.1 | alcohol dehydrogenase" in critic  # short block
        assert "  1.1.1.1: 2\n  1.2.1.10: 1" in critic
        assert "  - b: ERROR timeout: deadline" in critic

        _, meta = example_spec.render("meta", task, context)
        assert '{"verdict": "consistent", "reason": "fine"}' in meta

    def test_it_postprocesses_its_own_id_field(self, example_spec):
        assert example_spec.postprocess(
            "proposer",
            {
                "picked": [{"ec_id": "1.1.1.1", "confidence": "high", "why": "w"}],
                "overall_confidence": "high",
                "no_functional_content": False,
                "notes": "",
            },
        ) == {
            "picked": [{"id": "1.1.1.1", "confidence": "high", "why": "w"}],
            "overall_confidence": "high",
            "no_functional_content": False,
            "notes": "",
        }
        assert example_spec.postprocess(
            "meta",
            {
                "final_ec_ids": ["1.1.1.1"],
                "final_confidence": "medium",
                "no_functional_content": False,
                "reasoning": "r",
            },
        ) == {
            "final_ids": ["1.1.1.1"],
            "final_confidence": "medium",
            "no_functional_content": False,
            "reasoning": "r",
        }

    def test_an_unknown_binding_is_rejected(self, example_spec):
        spec = example_spec.model_dump(by_alias=True)
        spec["roles"]["proposer"]["user"] = spec["roles"]["proposer"]["user"].replace(
            "$candidates_full", "$candidates_fully"
        )
        with pytest.raises(TemplateBindingError) as excinfo:
            validate_spec(spec)
        assert "$candidates_fully" in str(excinfo.value)
        assert "roles.proposer.user" in str(excinfo.value)

    def test_an_unknown_candidate_binding_is_rejected(self, example_spec):
        spec = example_spec.model_dump(by_alias=True)
        spec["candidate_templates"]["full"] = "  $rank | $id | $meta_not_declared"
        with pytest.raises(TemplateBindingError) as excinfo:
            validate_spec(spec)
        assert "candidate_templates.full" in str(excinfo.value)

    def test_a_declared_metadata_key_IS_a_valid_candidate_binding(self, example_spec):
        spec = example_spec.model_dump(by_alias=True)
        spec["candidate_templates"]["full"] = "  $rank | $id | $meta_definition_source"
        assert validate_spec(spec).candidate_templates["full"].endswith(
            "$meta_definition_source"
        )

    def test_a_malformed_placeholder_is_rejected(self, example_spec):
        spec = example_spec.model_dump(by_alias=True)
        spec["roles"]["critic"]["system"] = "audit this $ thing"
        with pytest.raises(TemplateBindingError) as excinfo:
            validate_spec(spec)
        assert "malformed" in str(excinfo.value)

    def test_a_non_generic_answer_schema_is_rejected(self, example_spec):
        spec = example_spec.model_dump(by_alias=True)
        spec["roles"]["proposer"]["schema"]["properties"]["picked"]["items"][
            "properties"
        ]["weight"] = {"type": "number"}
        with pytest.raises(NonGenericSchema) as excinfo:
            validate_spec(spec)
        assert "proposer" in str(excinfo.value)

    def test_a_renamed_id_field_must_be_renamed_in_the_schema_too(self, example_spec):
        spec = example_spec.model_dump(by_alias=True)
        spec["id_field"] = "enzyme_id"
        with pytest.raises(NonGenericSchema):
            validate_spec(spec)

    def test_a_template_rendered_spec_must_ship_candidate_templates(self, example_spec):
        spec = example_spec.model_dump(by_alias=True)
        spec["candidate_templates"] = {"full": "  $id"}
        with pytest.raises(Exception) as excinfo:
            validate_spec(spec)
        assert "candidate_templates['short']" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Renderer / adapter resolution
# ---------------------------------------------------------------------------


class TestRendererResolution:
    def test_an_unknown_renderer_name_is_refused(self, example_spec):
        spec = example_spec.model_dump(by_alias=True)
        spec["renderer"] = "go_mf_v7"
        with pytest.raises(UnknownRenderer) as excinfo:
            validate_spec(spec)
        assert "go_mf_v7" in str(excinfo.value)

    def test_an_unsupported_renderer_VERSION_is_refused(self):
        """A spec written by an installation ahead of this one is refused.

        Not rendered with the version that happens to be here: a renderer is
        the prompt, so a silent downgrade asks a different question and the
        verdict would be attributed to the wrong one.
        """
        spec = load_builtin_spec("go_mf")
        spec["renderer"] = "go_mf_v3@2"
        with pytest.raises(UnknownRenderer) as excinfo:
            validate_spec(spec)
        assert "version '2'" in str(excinfo.value)
        assert "'1'" in str(excinfo.value)

    def test_an_unsupported_adapter_VERSION_is_refused(self):
        spec = load_builtin_spec("reaction_pickup")
        spec["adapter"] = "reaction_pickup_v4@99"
        with pytest.raises(UnknownAdapter) as excinfo:
            validate_spec(spec)
        assert "reaction_pickup_v4" in str(excinfo.value)

    def test_a_bare_renderer_name_means_whatever_is_installed(self):
        spec = load_builtin_spec("go_mf")
        spec["renderer"] = "go_mf_v3"
        spec["adapter"] = "go_mf_v3"
        assert validate_spec(spec).renderer == "go_mf_v3"

    def test_every_builtin_names_a_renderer_and_an_adapter_that_exist(self):
        for name, version in BUILTIN_QUERY_TYPES:
            spec = validate_spec(load_builtin_spec(name))
            assert spec.version == version
            assert spec.renderer and spec.adapter
            assert spec.renderer.split("@")[0] in BUILTIN_RENDERERS
            assert spec.adapter.split("@")[0] in BUILTIN_ADAPTERS


# ---------------------------------------------------------------------------
# The pickup adapter
# ---------------------------------------------------------------------------


def critique(rxn_id, votes, verdict):
    return {
        "rxn_id": rxn_id,
        "council_vote_count": votes,
        "critic_verdict": verdict,
        "critic_reason": "because",
    }


class TestPickupAdapterApprovalRule:
    """Chris's approval rule (adopted 2026-10-10), on fixture critiques."""

    def test_all_voted_consistent_and_nothing_missed_approves(self):
        reply = {
            "per_reaction_critique": [
                critique("rxn1", 5, "strongly_consistent"),
                critique("rxn2", 3, "consistent"),
                critique("rxn3", 0, "inconsistent"),
            ],
            "missed_picks": [],
            "overall_note": "looks right",
        }
        assert pickup_critic_approved(reply) is True

    def test_a_zero_vote_reaction_rated_consistent_blocks_approval(self):
        reply = {
            "per_reaction_critique": [
                critique("rxn1", 5, "strongly_consistent"),
                critique("rxn3", 0, "consistent"),
            ],
            "missed_picks": [],
            "overall_note": "",
        }
        assert pickup_critic_approved(reply) is False

    def test_any_missed_pick_blocks_approval(self):
        reply = {
            "per_reaction_critique": [critique("rxn1", 5, "strongly_consistent")],
            "missed_picks": [{"rxn_id": "rxn9", "reason": "same EC"}],
            "overall_note": "",
        }
        assert pickup_critic_approved(reply) is False

    def test_a_voted_reaction_rated_inconsistent_blocks_approval(self):
        reply = {
            "per_reaction_critique": [
                critique("rxn1", 5, "strongly_consistent"),
                critique("rxn2", 2, "ambiguous"),
            ],
            "missed_picks": [],
            "overall_note": "",
        }
        assert pickup_critic_approved(reply) is False


class TestPickupAdapterMapping:
    def test_the_fixture_critique_maps_to_the_generic_critic_shape(self, pickup_spec):
        case = load_case("reaction_pickup_case")
        generic = pickup_spec.postprocess("critic", case["critic_raw"])
        assert generic["verdict"] == "inconsistent"
        assert generic["overpicked"] == ["rxn05021"]
        # missed_picks first, then zero-vote reactions the critic endorsed
        # (this fixture's only zero-vote row is rated 'ambiguous', so none).
        assert generic["missed"] == ["rxn05011"]
        assert generic["contentless_input"] is False
        assert generic["reason"].startswith("The core pick is right")
        assert generic["raw"] == case["critic_raw"]

    def test_a_zero_vote_endorsement_lands_in_missed(self, pickup_spec):
        reply = {
            "per_reaction_critique": [
                critique("rxn1", 5, "strongly_consistent"),
                critique("rxn7", 0, "consistent"),
            ],
            "missed_picks": [{"rxn_id": "rxn9", "reason": "same EC"}],
            "overall_note": "",
        }
        generic = pickup_spec.postprocess("critic", reply)
        assert generic["missed"] == ["rxn9", "rxn7"]
        assert generic["verdict"] == "inconsistent"

    def test_approved_maps_to_consistent(self, pickup_spec):
        reply = {
            "per_reaction_critique": [critique("rxn1", 4, "consistent")],
            "missed_picks": [],
            "overall_note": "fine",
        }
        assert pickup_spec.postprocess("critic", reply)["verdict"] == "consistent"

    def test_fifteen_picks_are_accepted_and_sixteen_are_rejected(self, pickup_spec):
        def reply(n):
            return {
                "picked_reactions": [
                    {"rxn_id": f"rxn{i:05d}", "confidence": "high", "rationale": "r"}
                    for i in range(n)
                ],
                "overall_confidence": "high",
                "notes": "",
            }

        assert len(pickup_spec.postprocess("proposer", reply(15))["picked"]) == 15
        with pytest.raises(InvalidOutput) as excinfo:
            pickup_spec.postprocess("proposer", reply(16))
        assert "16 picks" in str(excinfo.value)

    def test_duplicates_collapse_first_wins(self, pickup_spec):
        reply = {
            "picked_reactions": [
                {"rxn_id": "rxnA", "confidence": "high", "rationale": "first"},
                {"rxn_id": "rxnB", "confidence": "low", "rationale": "other"},
                {"rxn_id": "rxnA", "confidence": "low", "rationale": "second"},
            ],
            "overall_confidence": "medium",
            "notes": "",
        }
        picked = pickup_spec.postprocess("proposer", reply)["picked"]
        assert [p["id"] for p in picked] == ["rxnA", "rxnB"]
        assert picked[0]["why"] == "first"
        assert picked[0]["confidence"] == "high"

    def test_the_pick_cap_is_a_prompt_instruction_not_validation(self, pickup_spec):
        """Twelve picks survive the adapter even though the prompt said five.

        The cap relaxation exists for promiscuous enzymes; enforcing it in
        code would discard exactly the picks the V4 prompt was written to
        collect.
        """
        reply = {
            "picked_reactions": [
                {"rxn_id": f"rxn{i}", "confidence": "medium", "rationale": "r"}
                for i in range(12)
            ],
            "overall_confidence": "medium",
            "notes": "",
        }
        assert len(pickup_spec.postprocess("proposer", reply)["picked"]) == 12

    def test_meta_maps_final_picked_rxn_ids_to_final_ids(self, pickup_spec):
        assert pickup_spec.postprocess(
            "meta",
            {
                "final_picked_rxn_ids": ["rxnA", "rxnB", "rxnA"],
                "final_picked_reasons": ["a", "b", "a"],
                "final_confidence": "high",
                "meta_reasoning": "paragraph",
                "partial_agreement_resolutions": [],
            },
        ) == {
            "final_ids": ["rxnA", "rxnB"],
            "final_confidence": "high",
            "no_functional_content": False,
            "reasoning": "paragraph",
        }

    def test_go_adapter_renames_only(self, go_spec):
        assert go_spec.postprocess(
            "meta",
            {
                "final_go_ids": ["GO:0004488"],
                "final_confidence": "high",
                "no_functional_content": False,
                "reasoning": "r",
            },
        )["final_ids"] == ["GO:0004488"]
        assert go_spec.postprocess(
            "proposer",
            {
                "picked": [{"go_id": "GO:0004488", "confidence": "high", "why": "w"}],
                "overall_confidence": "high",
                "no_functional_content": False,
                "notes": "",
            },
        )["picked"] == [{"id": "GO:0004488", "confidence": "high", "why": "w"}]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class TestSchemas:
    def test_the_generic_schemas_are_valid_json_schema(self):
        jsonschema = pytest.importorskip("jsonschema")
        for role, schema in generic_schemas().items():
            jsonschema.Draft202012Validator.check_schema(schema), role

    def test_every_builtin_role_schema_is_valid_json_schema(self):
        jsonschema = pytest.importorskip("jsonschema")
        for name, _version in BUILTIN_QUERY_TYPES:
            spec = validate_spec(load_builtin_spec(name))
            for role in ROLES:
                jsonschema.Draft202012Validator.check_schema(spec.answer_schema(role))

    def test_the_querytype_schema_is_valid_and_accepts_every_shipped_spec(self):
        jsonschema = pytest.importorskip("jsonschema")
        import yaml

        schema = json.loads((PACKAGE_DIR / "querytype.schema.json").read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        validator = jsonschema.Draft202012Validator(schema)
        for path in [
            PACKAGE_DIR / "builtin" / "go_mf.yaml",
            PACKAGE_DIR / "builtin" / "reaction_pickup.yaml",
            PACKAGE_DIR / "examples" / "custom_example.yaml",
        ]:
            validator.validate(yaml.safe_load(path.read_text()))

    def test_go_role_schemas_ARE_the_generic_ones_with_go_id(self, go_spec):
        for role in ROLES:
            assert go_spec.answer_schema(role) == generic_schema_for(role, "go_id")

    def test_the_pickup_proposer_schema_caps_picks_at_fifteen(self, pickup_spec):
        schema = pickup_spec.answer_schema("proposer")
        assert schema["properties"]["picked_reactions"]["maxItems"] == 15

    def test_answer_schema_returns_a_copy(self, go_spec):
        first = go_spec.answer_schema("proposer")
        first["properties"].clear()
        assert go_spec.answer_schema("proposer")["properties"]


# ---------------------------------------------------------------------------
# The registry, against a real SQLite store
# ---------------------------------------------------------------------------


def sqlite_registry(tmp_path: Path, filename: str = "council.sqlite"):
    """A registry over a freshly initialised SQLite store.  No stubs."""
    store = open_store(f"sqlite:///{tmp_path}/{filename}")
    store.init_schema()
    return store, QueryRegistry(store)


class TestRegistry:
    def test_seed_builtins_is_idempotent(self, tmp_path):
        store, registry = sqlite_registry(tmp_path)
        first = registry.seed_builtins()
        assert sorted(s.name for s in first) == ["go_mf", "reaction_pickup"]
        registry.seed_builtins()
        registry.seed_builtins()
        rows = registry.list()
        assert sorted((r["name"], r["version"]) for r in rows) == [
            ("go_mf", "1"),
            ("reaction_pickup", "1"),
        ]
        assert {r["source"] for r in rows} == {"builtin"}

    def test_a_query_type_is_immutable(self, tmp_path):
        store, registry = sqlite_registry(tmp_path)
        registry.seed_builtins()
        spec = load_builtin_spec("go_mf")
        spec["description"] = "a different question entirely"
        with pytest.raises(QueryTypeExists):
            registry.register(spec, created_by="someone")
        # and the stored one is untouched
        assert registry.get("go_mf").description != "a different question entirely"

    def test_a_new_version_registers_beside_the_old_one(self, tmp_path):
        store, registry = sqlite_registry(tmp_path)
        registry.seed_builtins()
        spec = load_builtin_spec("go_mf")
        spec["version"] = "2"
        spec["description"] = "a different question entirely"
        registry.register(spec, created_by="chenry")
        assert registry.get("go_mf", "1").description != spec["description"]
        assert registry.get("go_mf", "2").description == spec["description"]
        # No version argument resolves to the latest ACTIVE version.
        assert registry.get("go_mf").version == "2"

    def test_retire_hides_a_version_from_the_default_lookup(self, tmp_path):
        store, registry = sqlite_registry(tmp_path)
        registry.seed_builtins()
        spec = load_builtin_spec("go_mf")
        spec["version"] = "2"
        spec["description"] = "newer"
        registry.register(spec)
        assert registry.retire("go_mf", "2") is True
        assert registry.get("go_mf").version == "1"
        assert len(registry.list()) == 2
        assert len(registry.list(include_retired=True)) == 3

    def test_registering_a_user_yaml_by_path(self, tmp_path):
        store, registry = sqlite_registry(tmp_path)
        path = PACKAGE_DIR / "examples" / "custom_example.yaml"
        spec = registry.register(path, created_by="chenry")
        assert spec.name == "ec_number_example"
        row = store.get_query_type("ec_number_example", "1")
        assert row["source"] == "user"
        assert row["created_by"] == "chenry"

    def test_an_unknown_query_type_raises_keyerror(self, tmp_path):
        store, registry = sqlite_registry(tmp_path)
        with pytest.raises(KeyError):
            registry.get("not_a_query_type")

    def test_an_invalid_spec_never_reaches_the_store(self, tmp_path):
        store, registry = sqlite_registry(tmp_path)
        spec = load_builtin_spec("go_mf")
        spec["renderer"] = "go_mf_v3@2"
        with pytest.raises(UnknownRenderer):
            registry.register(spec)
        assert store.list_query_types() == []


class TestSpecSurvivesTheDatabase:
    """The no-injection check: nothing stubbed, nothing shared in memory.

    This is the test that distinguishes "the renderer works" from "the
    renderer works on a spec that came out of a database".  Two separate
    store OBJECTS over one file, a registry built on each, and the specs read
    back by the second one are asked to render every role -- compared against
    the same reference fixtures as the in-memory specs.  A spec that lost its
    prompts, its fragments or a schema in the JSON round trip fails here and
    nowhere else.
    """

    def test_seeded_specs_render_byte_identically_after_a_round_trip(self, tmp_path):
        db = tmp_path / "roundtrip.sqlite"

        writer = open_store(f"sqlite:///{db}")
        writer.init_schema()
        QueryRegistry(writer).seed_builtins()

        reader = open_store(f"sqlite:///{db}")
        assert reader is not writer
        registry = QueryRegistry(reader)

        go = registry.get("go_mf")
        case = load_case("go_mf_case")
        task = CouncilTask.model_validate(case["task"])
        contexts = go_contexts(case)
        for role in ROLES:
            assert go.render(role, task, contexts[role]) == expected("go_mf", role)

        pickup = registry.get("reaction_pickup")
        case = load_case("reaction_pickup_case")
        task = CouncilTask.model_validate(case["task"])
        contexts = pickup_contexts(case, pickup)
        for role in ROLES:
            assert pickup.render(role, task, contexts[role]) == expected(
                "reaction_pickup", role
            )

        # The conditional system-prompt fragment survives too -- it is the one
        # piece of prompt text that is not in a role's `system` or `user`.
        plain = load_case("reaction_pickup_plain_case")
        plain_task = CouncilTask.model_validate(plain["task"])
        assert pickup.render("proposer", plain_task, {}) == expected(
            "reaction_pickup_plain", "proposer"
        )

    def test_the_stored_spec_is_byte_equal_to_the_shipped_one(self, tmp_path):
        store, registry = sqlite_registry(tmp_path)
        registry.seed_builtins()
        for name, version in BUILTIN_QUERY_TYPES:
            row = store.get_query_type(name, version)
            shipped = validate_spec(load_builtin_spec(name)).to_spec()
            assert row["spec"] == shipped
