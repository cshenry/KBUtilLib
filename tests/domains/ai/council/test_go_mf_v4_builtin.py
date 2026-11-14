"""``go_mf@4`` -- the query spec the 2026-10 GO corpus was actually decided under.

``go_mf@4`` existed only inside the live council store's ``query_types`` table
until it was extracted into ``builtin/go_mf_v4.yaml``.  Twenty-eight finished
jobs and 47,185 decided items were produced under it, so a bench run under any
other version is not comparable to that baseline -- which makes "is the
committed file the same question the store holds?" the load-bearing question
here, and the reason this module checks the ONE field that separates ``@4``
from ``@3`` rather than trusting the extraction.

``@3`` is committed beside ``@4`` so that check is a test and not a claim in a
work record: the pair differ in exactly three places and
:meth:`TestV4IsV3PlusOneField.test_the_only_differences_from_v3` enumerates
them.

Nothing here makes a network call or touches the live store.  The only I/O is
a SQLite file under ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kbutillib.domains.ai.council.queries import QueryRegistry, UnknownRenderer
from kbutillib.domains.ai.council.queries.builtin import (
    BUILTIN_QUERY_TYPES,
    BUILTIN_SPEC_FILES,
    builtin_spec_paths,
    builtin_versions,
    load_builtin_spec,
)
from kbutillib.domains.ai.council.queries.registry import validate_spec
from kbutillib.domains.ai.council.queries.spec import PACKAGE_DIR
from kbutillib.domains.ai.council.store import open_store
from kbutillib.domains.ai.council.types import CouncilTask

FIXTURES = Path(__file__).parent / "fixtures" / "queries"
ROLES = ("proposer", "critic", "meta")


@pytest.fixture
def v4():
    return validate_spec(load_builtin_spec("go_mf", "4"))


@pytest.fixture
def v3():
    return validate_spec(load_builtin_spec("go_mf", "3"))


class TestGoMfV4Loads:
    def test_it_loads_through_validate_spec(self, v4):
        assert v4.name == "go_mf"
        assert v4.version == "4"
        assert v4.id_field == "go_id"
        assert v4.entity_field == "text"
        assert v4.renderer == "go_mf_v3@1"
        assert v4.adapter == "go_mf_v3@1"
        assert v4.critic_truncate_chars == 1200

    def test_the_meta_schema_allows_extra_keys_and_the_others_do_not(self, v4):
        """The whole of the ``@3 -> @4`` change, as an assertion.

        ``@3`` discarded 25 of 184 meta answers -- 13.6% of the meta seat --
        for carrying one extra commentary key alongside four complete required
        fields.  ``@4`` relaxes the meta role ONLY: a proposer or critic that
        invented a key would still be refused, because those answers are read
        for their picks and the extra key could be the pick.
        """
        assert v4.answer_schema("meta")["additionalProperties"] is True
        assert v4.answer_schema("proposer")["additionalProperties"] is False
        assert v4.answer_schema("critic")["additionalProperties"] is False

    def test_a_meta_answer_with_an_extra_key_validates_under_v4_not_v3(self, v3, v4):
        """What ``additionalProperties: true`` buys, against a real validator."""
        jsonschema = pytest.importorskip("jsonschema")
        answer = {
            "final_go_ids": ["GO:0004672"],
            "final_confidence": "high",
            "no_functional_content": False,
            "reasoning": "The annotation names a protein kinase.",
            "commentary": "an extra key of the kind that cost 25 of 184 answers",
        }
        jsonschema.Draft202012Validator(v4.answer_schema("meta")).validate(answer)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(v3.answer_schema("meta")).validate(answer)

    def test_the_attribution_survived_the_extraction(self, v4):
        """The provenance is the point of committing the file at all."""
        assert "CONCORDIA" in v4.attribution
        assert "go_mf@4" in v4.attribution
        assert "25 of 184" in v4.attribution


class TestV4IsV3PlusOneField:
    """The extraction check: ``@4`` is ``@3`` with ONE field changed."""

    def test_the_only_differences_from_v3(self, v3, v4):
        before, after = v3.to_spec(), v4.to_spec()
        assert sorted(before) == sorted(after)
        differing = sorted(key for key in after if before[key] != after[key])
        assert differing == ["attribution", "roles", "version"]

        assert (before["version"], after["version"]) == ("3", "4")
        # @4's attribution is @3's plus an appended record of the change.
        assert after["attribution"].startswith(before["attribution"])

        # Inside `roles`, only meta's schema moved, and only one key of it.
        assert before["roles"]["proposer"] == after["roles"]["proposer"]
        assert before["roles"]["critic"] == after["roles"]["critic"]
        meta_before, meta_after = before["roles"]["meta"], after["roles"]["meta"]
        assert meta_before["system"] == meta_after["system"]
        assert meta_before["user"] == meta_after["user"]
        schema_before = dict(meta_before["schema"])
        schema_after = dict(meta_after["schema"])
        assert schema_before.pop("additionalProperties") is False
        assert schema_after.pop("additionalProperties") is True
        assert schema_before == schema_after

    def test_the_prompts_are_byte_identical_to_v3(self, v3, v4):
        """No prompt text changed, so a prompt is not why answers differ."""
        for role in ROLES:
            assert v4.roles[role].system == v3.roles[role].system
            assert v4.roles[role].user == v3.roles[role].user

    @pytest.mark.parametrize("role", ROLES)
    def test_it_actually_renders_and_renders_what_v3_renders(self, v3, v4, role):
        """``validate_spec`` only RESOLVES the renderer; this calls it.

        Compared against ``@3``'s output on the committed fixture case rather
        than against a reference file, because the reference text committed
        beside this suite is ``@1``'s and ``@1``'s proposer prompt is not
        ``@3``'s.  Equality with ``@3`` is the claim that matters: the two ask
        the identical question.
        """
        case = json.loads((FIXTURES / "go_mf_case.json").read_text())
        task = CouncilTask.model_validate(case["task"])
        context = {
            "proposer": {},
            "critic": {"votes": case["votes"], "ballots": case["ballots"]},
            "meta": {
                "votes": case["votes"],
                "ballots": case["ballots"],
                "critic": case["critic"],
            },
        }[role]
        system, user = v4.render(role, task, context)
        assert system and user
        assert (system, user) == v3.render(role, task, context)


class TestVersionAwareBuiltins:
    """A builtin YAML is addressed by ``(name, version)``, not by name.

    Addressing by name alone is what made this a code change rather than a
    data add: ``load_builtin_spec("go_mf")`` read ``go_mf.yaml`` whatever
    version was asked for, so a second ``go_mf`` entry would have registered
    ``@1``'s prompts under ``@4``'s number.
    """

    def test_every_shipped_version_has_its_own_file(self):
        assert builtin_versions("go_mf") == ["1", "3", "4"]
        assert sorted(BUILTIN_SPEC_FILES) == sorted(BUILTIN_QUERY_TYPES)
        names = [path.name for path in builtin_spec_paths()]
        assert len(set(names)) == len(names)
        for name, version in BUILTIN_QUERY_TYPES:
            path = PACKAGE_DIR / "builtin" / BUILTIN_SPEC_FILES[(name, version)]
            assert path.exists(), path

    def test_each_file_declares_the_name_and_version_it_is_filed_under(self):
        for name, version in BUILTIN_QUERY_TYPES:
            spec = load_builtin_spec(name, version)
            assert (spec["name"], spec["version"]) == (name, version)

    def test_a_name_with_several_versions_refuses_a_version_less_load(self):
        with pytest.raises(UnknownRenderer) as excinfo:
            load_builtin_spec("go_mf")
        assert "3 versions" in str(excinfo.value)
        # A name shipping exactly one version still resolves without one.
        assert load_builtin_spec("reaction_pickup")["version"] == "1"

    def test_a_version_this_installation_does_not_ship_is_refused(self):
        with pytest.raises(UnknownRenderer) as excinfo:
            load_builtin_spec("go_mf", "2")
        assert "no version '2'" in str(excinfo.value)


class TestSeedingIsVersionAware:
    def test_go_mf_1_still_registers_unchanged_alongside_go_mf_4(self, tmp_path: Path):
        """The regression the version-aware seeding exists to prevent.

        Both versions reach the store, each carrying its OWN spec: ``@1``'s
        row is byte-equal to ``go_mf.yaml`` and ``@4``'s to
        ``go_mf_v4.yaml``.  Under the name-only loader both rows would have
        held ``go_mf.yaml``, differing from each other in nothing at all.
        """
        store = open_store(f"sqlite:///{tmp_path}/council.sqlite")
        store.init_schema()
        seeded = QueryRegistry(store).seed_builtins()

        assert sorted((spec.name, spec.version) for spec in seeded) == sorted(
            BUILTIN_QUERY_TYPES
        )
        for name, version in BUILTIN_QUERY_TYPES:
            row = store.get_query_type(name, version)
            assert row is not None, (name, version)
            assert row["source"] == "builtin"
            assert row["spec"] == validate_spec(load_builtin_spec(name, version)).to_spec()

        one = store.get_query_type("go_mf", "1")["spec"]
        four = store.get_query_type("go_mf", "4")["spec"]
        assert one["roles"]["meta"]["schema"]["additionalProperties"] is False
        assert four["roles"]["meta"]["schema"]["additionalProperties"] is True
        assert one["roles"]["proposer"]["user"] != four["roles"]["proposer"]["user"]

    def test_seeding_twice_is_still_a_no_op(self, tmp_path: Path):
        store = open_store(f"sqlite:///{tmp_path}/council.sqlite")
        store.init_schema()
        registry = QueryRegistry(store)
        registry.seed_builtins()
        registry.seed_builtins()
        rows = registry.list()
        assert sorted((row["name"], row["version"]) for row in rows) == sorted(
            BUILTIN_QUERY_TYPES
        )

    def test_a_versionless_lookup_resolves_to_the_newest_seeded_version(
        self, tmp_path: Path
    ):
        """Documented consequence of seeding three ``go_mf`` versions.

        Asserted rather than left implicit because it MOVED: before ``@3`` and
        ``@4`` were shipped, ``get("go_mf")`` was ``@1``.  Callers that mean a
        particular question pin it.
        """
        store = open_store(f"sqlite:///{tmp_path}/council.sqlite")
        store.init_schema()
        registry = QueryRegistry(store)
        registry.seed_builtins()
        assert registry.get("go_mf").version == "4"
        assert registry.get("go_mf", "1").version == "1"
