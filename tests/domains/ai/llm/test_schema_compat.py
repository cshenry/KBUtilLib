"""The schema-compatibility boundary: what gets stripped, and what gets refused.

``llm/apis/schema_compat.py`` exists because of two measured live failures,
and the two functions here are tested against the SHIPPED ``go_mf`` specs
rather than against hand-written look-alikes -- the packaged schemas are what
a real run hands a backend, and the point of both checks is what they say
about those:

* issue #77 -- the claude CLI cannot resolve the 2020-12 meta-schema ref, so
  every ``go_mf@1`` role schema (all three declare ``$schema``) fails it.  The
  verbatim stderr is in
  ``tests/domains/ai/council/fixtures/cli_envelopes/claude_schema_key_stderr.txt``.
* issue #76 -- ``go_mf@1``'s proposer omits ``notes`` from ``required``, which
  OpenAI strict structured outputs refuse with a 400.

Pure functions only.  The two backends' behaviour at the real call path is
asserted in ``tests/domains/ai/council/test_cli_backends.py``, against a fake
CLI binary.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict

import pytest

from kbutillib.domains.ai.council.queries.builtin import load_builtin_spec
from kbutillib.domains.ai.council.queries.registry import validate_spec
from kbutillib.domains.ai.llm.apis.schema_compat import (
    META_KEYWORD,
    strict_problems,
    strip_meta_keywords,
    walk_schema,
)

DIALECT = "https://json-schema.org/draft/2020-12/schema"


def role_schema(version: str, role: str) -> Dict[str, Any]:
    """The schema the packaged ``go_mf@<version>`` spec stores for *role*."""
    spec = validate_spec(load_builtin_spec("go_mf", version))
    return dict(spec.roles[role].json_schema)


#: A schema that trips nothing in the enumerated set, whose ONLY deviation
#: from what a strict provider wants is the dialect declaration.  The
#: distinction the two functions must not blur: this is a refusal for claude
#: and a non-event for codex.
STRICT_PLUS_DIALECT: Dict[str, Any] = {
    "$schema": DIALECT,
    "$id": "https://example.invalid/answer",
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict"],
    "properties": {"verdict": {"enum": ["ok", "no"]}},
}


class TestStrictProblemsOnThePackagedSchemas:
    """What the lint says about the schemas that actually shipped."""

    def test_go_mf_v1_proposer_names_notes(self) -> None:
        """Issue #76, from the schema the 2026-10-19 run was asked under.

        The 400 named the field: ``'schema.required' is required to include
        every key in properties. Missing 'notes'``.  So must the lint.
        """
        problems = strict_problems(role_schema("1", "proposer"))

        assert len(problems) == 1, problems
        assert problems[0].startswith("$: ")
        assert "required" in problems[0]
        assert "notes" in problems[0]

    def test_a_root_dialect_is_not_a_problem_on_its_own(self) -> None:
        """``$schema`` is claude's complaint, never a strict-mode one.

        Codex was observed to accept a root ``$schema`` AND ``$id`` and answer
        normally (``fixtures/cli_envelopes/codex_schema_key_ok.jsonl``), so a
        lint that reported it would have a codex seat refuse a schema the
        provider serves.
        """
        assert strict_problems(STRICT_PLUS_DIALECT) == []

    def test_go_mf_v4_meta_names_additional_properties(self) -> None:
        """``@4`` relaxed its meta role to ``additionalProperties: true``.

        A deliberate change (25 of 184 meta calls were rejected for an extra
        key under ``@3``), and it is exactly what makes that role unservable
        on a strict provider -- which is the fact the lint must surface rather
        than paper over.
        """
        problems = strict_problems(role_schema("4", "meta"))

        assert len(problems) == 1, problems
        assert problems[0].startswith("$: ")
        assert "additionalProperties" in problems[0]

    @pytest.mark.parametrize("role", ["proposer", "critic"])
    def test_the_two_fixed_v4_roles_are_clean(self, role: str) -> None:
        assert strict_problems(role_schema("4", role)) == []


class TestStrictProblemsNesting:
    """A defect anywhere in the document is a defect, and the path says where."""

    def test_a_defs_entry_carries_its_own_path(self) -> None:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["pick"],
            "properties": {"pick": {"$ref": "#/$defs/Pick"}},
            "$defs": {
                "Pick": {
                    "type": "object",
                    "properties": {"go_id": {"type": "string"}},
                }
            },
        }

        problems = strict_problems(schema)

        assert [p for p in problems if p.startswith("$.$defs.Pick: ")] == problems
        assert len(problems) == 2, problems
        assert any("additionalProperties" in p for p in problems)
        assert any("go_id" in p for p in problems)

    def test_an_items_object_carries_its_own_path(self) -> None:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["picked"],
            "properties": {
                "picked": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["go_id"],
                        "properties": {
                            "go_id": {"type": "string"},
                            "why": {"type": "string"},
                        },
                    },
                }
            },
        }

        problems = strict_problems(schema)

        assert all(p.startswith("$.properties.picked.items: ") for p in problems)
        assert any("additionalProperties" in p for p in problems)
        assert any("why" in p for p in problems)

    def test_an_anyof_branch_carries_its_index(self) -> None:
        """An ``anyOf`` BELOW the root is walked; a root one is its own rule."""
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["answer"],
            "properties": {
                "answer": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "object", "properties": {"n": {"type": "integer"}}},
                    ]
                }
            },
        }

        problems = strict_problems(schema)

        assert all(
            p.startswith("$.properties.answer.anyOf[1]: ") for p in problems
        ), problems
        assert any("additionalProperties" in p for p in problems)
        assert any("n" in p for p in problems)

    def test_a_root_anyof_is_refused_as_such(self) -> None:
        """Reported as a root union, not as "the root is not an object".

        Said once and said precisely: a lint that reported the same fault
        twice would read as two independent defects.  The branches are still
        walked, so the object branch's own missing ``additionalProperties``
        is reported under its index -- that is a second, real fault.
        """
        problems = strict_problems(
            {"anyOf": [{"type": "object"}, {"type": "string"}]}
        )

        assert problems[0] == (
            "$: the root must not be an anyOf -- strict mode needs one object"
        )
        assert not any("must be an object node" in p for p in problems)
        assert problems[1:] == [
            "$.anyOf[0]: an object node must set additionalProperties to "
            "exactly false (absent)"
        ]

    def test_a_non_object_root_is_a_problem(self) -> None:
        problems = strict_problems({"type": "array", "items": {"type": "string"}})

        assert problems == [
            "$: the root must be an object node (type: object, with properties)"
        ]

    @pytest.mark.parametrize("keyword", ["allOf", "not", "if", "then", "else"])
    def test_the_unsupported_keywords_are_reported(self, keyword: str) -> None:
        """``allOf`` and ``not`` above all: strict mode supports neither.

        Parametrised over the whole family because the walker DESCENDS into
        these same keywords, and a check that only fired on a keyword it did
        not traverse would be the easy mistake to make.
        """
        body = (
            [{"type": "object", "additionalProperties": False}]
            if keyword == "allOf"
            else {"type": "object", "additionalProperties": False}
        )
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["x"],
            "properties": {"x": {"type": "string"}},
            keyword: body,
        }

        problems = strict_problems(schema)

        assert f"$: {keyword} is not supported in strict mode" in problems

    @pytest.mark.parametrize("keyword", ["dependentRequired", "dependentSchemas"])
    def test_the_dependency_keywords_are_reported(self, keyword: str) -> None:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["x"],
            "properties": {"x": {"type": "string"}},
            keyword: {"x": ["y"]},
        }

        assert f"$: {keyword} is not supported in strict mode" in strict_problems(
            schema
        )

    def test_a_const_or_enum_value_shaped_like_a_schema_is_not_linted(self) -> None:
        """Data is data.  A generic dict-walk would lint an example value.

        ``properties.shape.const`` here is a perfectly good non-strict schema
        -- as a VALUE the answer must equal.  Linting it would refuse a schema
        that is correct.
        """
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["shape"],
            "properties": {
                "shape": {
                    "const": {"type": "object", "properties": {"a": {}}},
                    "default": {"type": "object", "properties": {"b": {}}},
                    "examples": [{"type": "object", "properties": {"c": {}}}],
                }
            },
        }

        assert strict_problems(schema) == []

    def test_the_input_is_not_mutated(self) -> None:
        schema = copy.deepcopy(role_schema("1", "proposer"))
        before = json.dumps(schema, sort_keys=True)

        strict_problems(schema)

        assert json.dumps(schema, sort_keys=True) == before


class TestStripMetaKeywords:
    """``$schema`` goes; nothing else does."""

    def test_a_nested_dialect_declaration_is_removed_too(self) -> None:
        """Every node the walker visits, not just the root.

        A ``$defs`` entry declaring its own dialect fails the claude CLI
        exactly as a root one does -- the validator's complaint is about the
        ref, and it does not care how deep it was.
        """
        schema = {
            "$schema": DIALECT,
            "type": "object",
            "additionalProperties": False,
            "required": ["pick", "tags"],
            "properties": {
                "pick": {"$schema": DIALECT, "$ref": "#/$defs/Pick"},
                "tags": {
                    "type": "array",
                    "items": {"$schema": DIALECT, "type": "string"},
                },
            },
            "$defs": {"Pick": {"$schema": DIALECT, "type": "object"}},
        }

        stripped = strip_meta_keywords(schema)

        assert META_KEYWORD not in json.dumps(stripped)
        # Everything else survives, including the keys that merely LOOK like
        # the one that was removed.
        assert stripped["properties"]["pick"] == {"$ref": "#/$defs/Pick"}
        assert stripped["$defs"]["Pick"] == {"type": "object"}
        assert stripped["required"] == ["pick", "tags"]

    def test_id_and_ref_and_defs_are_kept(self) -> None:
        """Measured: claude accepts ``$id`` + ``$defs`` + a local ``$ref``.

        It fails only on the ``$schema`` dialect URL, so stripping the rest
        would throw away working structure to fix a problem it does not cause.
        """
        stripped = strip_meta_keywords(STRICT_PLUS_DIALECT)

        assert META_KEYWORD not in stripped
        assert stripped["$id"] == "https://example.invalid/answer"
        assert stripped["properties"] == {"verdict": {"enum": ["ok", "no"]}}
        assert stripped["additionalProperties"] is False

    def test_a_property_literally_named_schema_survives(self) -> None:
        """``properties["$schema"]`` is a FIELD NAME, not a dialect declaration.

        The keys of a ``properties`` map are data.  A strip that read them as
        keywords would delete a field the answer is required to carry, and the
        reply would then fail validation for a reason nothing explains.
        """
        schema = {
            "$schema": DIALECT,
            "type": "object",
            "additionalProperties": False,
            "required": ["$schema", "value"],
            "properties": {
                "$schema": {"type": "string", "description": "the dialect we used"},
                "value": {"type": "integer"},
            },
        }

        stripped = strip_meta_keywords(schema)

        assert META_KEYWORD not in stripped
        assert stripped["properties"]["$schema"] == {
            "type": "string",
            "description": "the dialect we used",
        }
        assert stripped["required"] == ["$schema", "value"]

    def test_a_dialect_inside_const_or_enum_survives(self) -> None:
        """A ``$schema`` key inside a value is the user's data, not a keyword."""
        schema = {
            "$schema": DIALECT,
            "type": "object",
            "additionalProperties": False,
            "required": ["dialect", "doc"],
            "properties": {
                "dialect": {"const": DIALECT},
                "doc": {
                    "enum": [
                        {"$schema": DIALECT, "title": "a document the answer may be"},
                        "none",
                    ]
                },
            },
        }

        stripped = strip_meta_keywords(schema)

        assert stripped["properties"]["dialect"]["const"] == DIALECT
        assert stripped["properties"]["doc"]["enum"][0] == {
            "$schema": DIALECT,
            "title": "a document the answer may be",
        }
        # The ROOT declaration still went.
        assert META_KEYWORD not in stripped

    def test_the_caller_s_schema_is_not_mutated(self) -> None:
        """The input is usually the stored spec, which must not move.

        The whole reason this fix lives at the backend boundary is that
        rewriting a stored schema would change its ``prompt_hash`` and
        re-identify every historical call.
        """
        schema = copy.deepcopy(role_schema("1", "proposer"))
        before = json.dumps(schema, sort_keys=True)

        stripped = strip_meta_keywords(schema)

        assert json.dumps(schema, sort_keys=True) == before
        assert META_KEYWORD in schema
        assert META_KEYWORD not in stripped

    @pytest.mark.parametrize("version", ["1", "3", "4"])
    @pytest.mark.parametrize("role", ["proposer", "critic", "meta"])
    def test_every_packaged_go_mf_role_comes_out_dialect_free(
        self, version: str, role: str
    ) -> None:
        """The #77 half, over every schema a go_mf run can hand a claude seat."""
        stripped = strip_meta_keywords(role_schema(version, role))

        assert META_KEYWORD not in json.dumps(stripped)

    def test_an_empty_schema_is_an_empty_dict(self) -> None:
        """A seat with no schema must not become a crash or a refusal."""
        assert strip_meta_keywords({}) == {}


class TestTheWalkerVisitsSchemaValuedLocationsOnly:
    """One walker serves both functions, so its reach is asserted directly."""

    def test_the_visited_paths(self) -> None:
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "$defs": {"D": {"type": "string"}},
            "definitions": {"E": {"type": "string"}},
            "items": {"type": "string"},
            "additionalProperties": {"type": "string"},
            "anyOf": [{"type": "string"}],
            "oneOf": [{"type": "string"}],
            "allOf": [{"type": "string"}],
            "prefixItems": [{"type": "string"}],
            "not": {"type": "string"},
            "if": {"type": "string"},
            "then": {"type": "string"},
            "else": {"type": "string"},
            "const": {"properties": {"nope": {}}},
            "enum": [{"properties": {"nope": {}}}],
            "default": {"properties": {"nope": {}}},
            "examples": [{"properties": {"nope": {}}}],
        }

        paths = [path for path, _node in walk_schema(schema)]

        assert paths == [
            "$",
            "$.properties.a",
            "$.$defs.D",
            "$.definitions.E",
            "$.items",
            "$.additionalProperties",
            "$.not",
            "$.if",
            "$.then",
            "$.else",
            "$.anyOf[0]",
            "$.oneOf[0]",
            "$.allOf[0]",
            "$.prefixItems[0]",
        ]

    def test_a_boolean_additional_properties_is_not_descended_into(self) -> None:
        paths = [path for path, _ in walk_schema({"additionalProperties": False})]

        assert paths == ["$"]

    def test_nodes_are_yielded_by_reference(self) -> None:
        """What lets ONE walk do the rewrite as well as the lint."""
        schema: Dict[str, Any] = {"properties": {"a": {"type": "string"}}}

        for _path, node in walk_schema(schema):
            node.pop("type", None)

        assert schema == {"properties": {"a": {}}}
