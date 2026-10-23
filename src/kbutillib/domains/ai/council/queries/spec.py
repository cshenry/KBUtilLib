"""``QueryType``: everything domain-specific about one council question.

A query type is the ONLY place a vocabulary lives.  It says what an item's
evidence looks like, what a menu entry looks like, how the three prompts are
written, what shape each role must answer in, and how to rename that answer's
domain fields to the generic ones the engine counts votes on.  The engine
itself never learns the words "GO term" or "reaction".

Two ways to render exist, and which one a spec gets is decided by one field:

* ``renderer`` SET -- a builtin shipped in :mod:`.builtin.renderers`.  The two
  builtins are verbatim ports of CONCORDIA prompt assemblies whose formatting
  (fixed-width rank columns, conditional segments, a per-item promiscuity
  fragment) is past what a template engine should be asked to express.  Naming
  a renderer this installation does not have REFUSES the query rather than
  falling back: a renderer is the prompt, so the wrong one asks a different
  question and nothing downstream could tell.
* ``renderer`` UNSET -- the ``string.Template`` renderer in :mod:`.template`,
  driven by the spec's own templates and its closed binding list.

A spec is immutable once saved (the store enforces that per ``name, version``)
because a finished job's verdicts only mean anything against the exact spec
that produced them.  Edit a query type by registering a new version.

Annotation note: pydantic evaluates field annotations and this repository's CI
matrix still includes Python 3.9, so model fields use ``Optional[...]`` rather
than PEP 604 unions -- the same note as :mod:`..types` and :mod:`..config`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..types import CouncilTask
from .errors import NonGenericSchema, QuerySpecError
from .template import (
    ROLES,
    allowed_bindings,
    allowed_candidate_bindings,
    check_template,
    render_with_templates,
)

__all__ = [
    "PACKAGE_DIR",
    "RoleSpec",
    "QueryType",
    "generic_schemas",
    "generic_schema_for",
    "generic_postprocess",
    "final_ids_key",
    "renderer_id_version",
    "load_spec_mapping",
    "BATCH_ITEM_KEY_FIELD",
    "BATCH_SYSTEM_SUFFIX",
    "wrap_batch_answer_schema",
    "render_batch_user",
]

_GENERIC_SCHEMAS_RESOURCE = "generic_schemas.json"

#: This package's directory on disk.  Used to reach the files that are DATA
#: rather than importable modules -- ``querytype.schema.json`` and
#: ``examples/custom_example.yaml`` -- from a CLI, a doc build or a test.  The
#: shipped resources themselves are read through ``importlib.resources``, which
#: also works from inside a zipped wheel; this is a convenience for callers
#: that want a path.
PACKAGE_DIR = Path(__file__).resolve().parent


def generic_schemas() -> Dict[str, Dict[str, Any]]:
    """The package's generic answer schemas, as shipped.

    Read from ``generic_schemas.json`` rather than written in Python so the
    one authority a user can be pointed at ("your schema must equal this
    file") is a file, not a docstring.
    """
    from importlib.resources import files

    text = files(__package__).joinpath(_GENERIC_SCHEMAS_RESOURCE).read_text()
    return json.loads(text)


def generic_schema_for(role: str, id_field: str) -> Dict[str, Any]:
    """The generic schema for *role* with the id field renamed to *id_field*.

    The ONLY permitted difference between a user spec's answer schema and the
    shipped generic one: ``picked[].id`` becomes ``picked[].<id_field>`` and
    ``final_ids`` becomes ``final_<id_field>s``.  Everything else -- the
    enums, the required lists, ``additionalProperties: false`` -- is fixed,
    because the engine reads those answers positionally by key.
    """
    if role not in ROLES:
        raise QuerySpecError(f"unknown role {role!r}; expected one of {', '.join(ROLES)}")
    schema = generic_schemas()[role]
    if role == "proposer" and id_field != "id":
        item = schema["properties"]["picked"]["items"]
        item["properties"][id_field] = item["properties"].pop("id")
        item["required"] = [id_field if r == "id" else r for r in item["required"]]
    elif role == "meta":
        final_key = final_ids_key(id_field)
        if final_key != "final_ids":
            properties = schema["properties"]
            properties[final_key] = properties.pop("final_ids")
            schema["required"] = [
                final_key if r == "final_ids" else r for r in schema["required"]
            ]
    return schema


def renderer_id_version(query: Any) -> str:
    """Identity of HOW a query type renders prompts and reads answers.

    Part of the prompt hash, so that two installations only reuse each other's
    answers when they would have asked and read them the same way.  The query
    type's own ``(name, version)`` is in the hash as well, but it is not
    enough on its own: a spec names its renderer and adapter as
    ``"<name>@<version>"``, and an installation shipping a different version
    of either asks or scores a different question under the same spec name.

    Lives HERE, beside the renderer it identifies, rather than in the worker
    that first needed it: the quote stage has to reproduce a prompt hash
    exactly, and :mod:`..quote` may not import :mod:`..engine` (the engine
    reaches the backends, and a quote must be unable to open a socket).  One
    definition, imported by both -- ``engine.worker`` re-exports this name,
    so every existing caller is unaffected.
    """
    renderer = getattr(query, "renderer", None) or "template"
    adapter = getattr(query, "adapter", None) or "generic"
    id_field = getattr(query, "id_field", None) or "id"
    return f"{renderer}|{adapter}|{id_field}"


def final_ids_key(id_field: str) -> str:
    """The meta answer's id-list key for a spec whose id field is *id_field*.

    ``go_id`` -> ``final_go_ids``; the degenerate ``id`` -> ``final_ids``, so
    a spec that never renamed anything needs no special case.
    """
    return "final_ids" if id_field == "id" else f"final_{id_field}s"


def _canonical(value: Any) -> str:
    """Order-insensitive JSON form, for comparing two schemas."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class RoleSpec(BaseModel):
    """The system prompt, user prompt and answer schema for one role.

    ``fragments`` holds named extra prompt text a BUILTIN renderer splices in
    conditionally -- currently only CONCORDIA's promiscuous-enzyme hint, which
    is appended to the reaction-pickup proposer's system prompt for items
    whose EC is shared by four or more candidates.  Keeping it here rather
    than as a Python constant means every word a model is shown still lives in
    the query type, which is the thing that gets versioned and stored.
    """

    model_config = ConfigDict(populate_by_name=True)

    system: str
    user: str
    # ``schema`` is the name in the YAML and in the stored spec; it cannot be
    # the Python attribute name because pydantic refuses a field that shadows
    # ``BaseModel.schema``.
    json_schema: Dict[str, Any] = Field(default_factory=dict, alias="schema")
    fragments: Dict[str, str] = Field(default_factory=dict)


class QueryType(BaseModel):
    """One saved query type.  Implements the ``QueryRenderer`` protocol."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    version: str = "1"
    description: str = ""
    entity_field: str
    evidence_fields: List[str] = Field(default_factory=list)
    menu_fields: List[str] = Field(default_factory=list)
    id_field: str
    roles: Dict[str, RoleSpec]
    candidate_templates: Dict[str, str] = Field(default_factory=dict)
    critic_truncate_chars: int = 1200
    renderer: Optional[str] = None
    adapter: Optional[str] = None
    attribution: str = ""

    # -- validation -------------------------------------------------------

    @model_validator(mode="after")
    def _check(self) -> "QueryType":
        """Reject a spec that could not be rendered or counted.

        Raises :class:`.errors.QuerySpecError` rather than pydantic's
        ``ValidationError`` so a caller catching ``CouncilException`` sees
        every refusal from this package the same way.
        """
        if not self.name:
            raise QuerySpecError("a query type needs a name")
        if not self.id_field:
            raise QuerySpecError(f"query type {self.name!r} needs an id_field")
        missing = [role for role in ROLES if role not in self.roles]
        if missing:
            raise QuerySpecError(
                f"query type {self.name!r} is missing role(s) {', '.join(missing)}"
            )
        extra = sorted(set(self.roles) - set(ROLES))
        if extra:
            raise QuerySpecError(
                f"query type {self.name!r} declares unknown role(s) {', '.join(extra)}"
            )
        if not self.evidence_fields:
            raise QuerySpecError(
                f"query type {self.name!r} declares no evidence_fields"
            )
        if self.entity_field not in self.evidence_fields:
            raise QuerySpecError(
                f"query type {self.name!r} has entity_field {self.entity_field!r}, "
                f"which is not among its evidence_fields "
                f"({', '.join(self.evidence_fields)})"
            )
        if self.renderer is None:
            # A template-rendered spec has no other way to show the menu, so
            # the absence of a candidate template is a prompt with no
            # candidates in it -- a council asked to pick from nothing.
            for which in ("full", "short"):
                if not self.candidate_templates.get(which):
                    raise QuerySpecError(
                        f"query type {self.name!r} names no builtin renderer, so it "
                        f"must supply candidate_templates[{which!r}]"
                    )
        return self

    # -- the QueryRenderer protocol ---------------------------------------

    def render(
        self,
        role: str,
        task: CouncilTask,
        context: Optional[Mapping[str, Any]] = None,
    ) -> Tuple[str, str]:
        """Return the ``(system, user)`` prompts for *role*.

        *context* is ``{}`` for a proposer, ``{"votes", "ballots"}`` for the
        critic and ``{"votes", "ballots", "critic"}`` for meta.
        """
        if self.renderer:
            from .builtin import resolve_renderer

            return resolve_renderer(self.renderer)(self, role, task, dict(context or {}))
        return render_with_templates(self, role, task, context)

    def answer_schema(self, role: str) -> Dict[str, Any]:
        """Return the JSON Schema this role's answer must satisfy.

        A deep copy: the schema goes into the prompt hash and into a backend's
        structured-output request, and a caller that mutated the stored one
        would silently change the identity of every later call.
        """
        if role not in self.roles:
            raise QuerySpecError(
                f"query type {self.name!r} has no role {role!r}"
            )
        return copy.deepcopy(self.roles[role].json_schema)

    def postprocess(self, role: str, parsed: Mapping[str, Any]) -> Dict[str, Any]:
        """Map a domain answer onto the generic shape for *role*."""
        if self.adapter:
            from .builtin import resolve_adapter

            return resolve_adapter(self.adapter)(self, role, dict(parsed or {}))
        return generic_postprocess(self, role, parsed)

    # -- serialisation ----------------------------------------------------

    def to_spec(self) -> Dict[str, Any]:
        """The stored form: plain JSON-able dict, ``schema`` spelled as such."""
        return self.model_dump(by_alias=True)

    def final_ids_key(self) -> str:
        """The meta answer's id-list key for this spec."""
        return final_ids_key(self.id_field)

    # -- registration-time checks -----------------------------------------

    def check_templates(self) -> None:
        """Reject any template using a binding outside the closed list.

        Only meaningful for a template-rendered spec; a builtin renderer's
        prompts are ``str.format`` templates carrying no ``$`` bindings at
        all, so running this over them would check nothing.
        """
        if self.renderer:
            return
        role_allowed = allowed_bindings(self.evidence_fields)
        for role in ROLES:
            role_spec = self.roles[role]
            check_template(role_spec.system, role_allowed, f"roles.{role}.system")
            check_template(role_spec.user, role_allowed, f"roles.{role}.user")
        candidate_allowed = allowed_candidate_bindings(self.menu_fields)
        for which, text in self.candidate_templates.items():
            check_template(text, candidate_allowed, f"candidate_templates.{which}")

    def check_generic_schemas(self) -> None:
        """Reject a user spec whose answer schemas are not the generic ones.

        Skipped for a builtin, which pairs a non-generic schema with an
        adapter that knows how to read it.  A user spec has no adapter, so an
        invented answer shape would reach the engine unmapped -- the votes
        would be counted off a key that is not there.
        """
        if self.renderer or self.adapter:
            return
        for role in ROLES:
            expected = generic_schema_for(role, self.id_field)
            actual = self.roles[role].json_schema
            if _canonical(actual) != _canonical(expected):
                raise NonGenericSchema(
                    f"query type {self.name!r} role {role!r} does not use the "
                    "package's generic answer schema.  A user-registered query "
                    "type may rename only the id field; expected:\n"
                    f"{json.dumps(expected, indent=2, sort_keys=True)}"
                )


def generic_postprocess(
    spec: QueryType, role: str, parsed: Mapping[str, Any]
) -> Dict[str, Any]:
    """Rename a domain answer's id fields to the generic ones.

    The whole of the default mapping: ``picked[].<id_field>`` becomes
    ``picked[].id`` and ``final_<id_field>s`` becomes ``final_ids``.  Nothing
    else is touched, because nothing else differs -- a spec whose answers need
    more than a rename needs a builtin adapter.
    """
    parsed = dict(parsed or {})
    if role == "proposer":
        picked = []
        for pick in parsed.get("picked") or ():
            pick = dict(pick) if isinstance(pick, Mapping) else {}
            picked.append(
                {
                    "id": str(pick.get(spec.id_field, pick.get("id", "")) or ""),
                    "confidence": pick.get("confidence", "medium"),
                    "why": pick.get("why", "") or "",
                }
            )
        return {
            "picked": picked,
            "overall_confidence": parsed.get("overall_confidence"),
            "no_functional_content": bool(parsed.get("no_functional_content")),
            "notes": parsed.get("notes", "") or "",
        }
    if role == "critic":
        return {
            "verdict": parsed.get("verdict"),
            "reason": parsed.get("reason", "") or "",
            "overpicked": list(parsed.get("overpicked") or ()),
            "missed": list(parsed.get("missed") or ()),
            "contentless_input": bool(parsed.get("contentless_input")),
        }
    if role == "meta":
        key = final_ids_key(spec.id_field)
        raw_ids = parsed.get(key)
        if raw_ids is None:
            raw_ids = parsed.get("final_ids") or ()
        return {
            "final_ids": [str(value) for value in raw_ids],
            "final_confidence": parsed.get("final_confidence"),
            "no_functional_content": bool(parsed.get("no_functional_content")),
            "reasoning": parsed.get("reasoning", "") or "",
        }
    raise QuerySpecError(f"unknown role {role!r}; expected one of {', '.join(ROLES)}")


def load_spec_mapping(source: Union[str, Path, Mapping[str, Any]]) -> Dict[str, Any]:
    """Return a spec mapping from a dict, a YAML path or YAML text.

    A plain string is treated as a path when it names an existing file and as
    YAML text otherwise, so a CLI can pass ``--spec`` through unexamined.
    """
    if isinstance(source, Mapping):
        return dict(source)

    import yaml

    if isinstance(source, Path):
        return dict(yaml.safe_load(source.read_text()) or {})

    text = str(source)
    candidate = Path(text)
    try:
        exists = candidate.exists()
    except OSError:  # pragma: no cover - path too long to even stat
        exists = False
    if exists:
        return dict(yaml.safe_load(candidate.read_text()) or {})
    return dict(yaml.safe_load(text) or {})


# ---------------------------------------------------------------------------
# Batched invocation -- the render/answer-schema surface for one seat
# answering several call rows in one backend invocation (t8).
#
# Batching changes the TRANSPORT only: a single role's rendered prompts are
# bundled into one numbered-list user prompt and the role's own answer schema
# is wrapped so a reply can carry back several answers, each tagged with the
# opaque ``item_key`` (a call id) it belongs to.  Nothing here knows about the
# store, a seat or a backend -- see ``engine.worker`` for assembly, dispatch
# and 1:N settlement, which is where that per-call context lives.
# ---------------------------------------------------------------------------

#: The key every batched answer element must carry back, naming which call
#: row it answers.  Opaque to the model: it is handed out verbatim and read
#: back verbatim, never interpreted.
BATCH_ITEM_KEY_FIELD = "item_key"

#: Appended to a role's system prompt for a BATCHED invocation only.  The only
#: change to that prompt: everything else about asking the question is
#: unchanged, so a batch of one is the same question a single call would ask,
#: plus this one instruction.
BATCH_SYSTEM_SUFFIX = (
    "\n\nThis request bundles several independent items into a single call. "
    "Answer every item below and return your answers as a JSON object with an "
    "`answers` array; each element of that array must carry back the exact "
    f"`{BATCH_ITEM_KEY_FIELD}` of the item it answers, alongside the answer "
    "fields described above for this role. The items are independent: do not "
    "let one item's evidence, menu or answer influence another item's answer."
)


def wrap_batch_answer_schema(role_schema: Mapping[str, Any]) -> Dict[str, Any]:
    """Wrap one role's answer schema for a batched reply.

    The wrapped schema is ``{"answers": [{"item_key": <str>, **role_schema}]}``:
    one array element per item, each carrying the opaque
    :data:`BATCH_ITEM_KEY_FIELD` plus the role's EXISTING answer shape,
    unchanged.  The engine validates the wrapper (does ``answers`` exist and
    is it an array?) separately from each element's role payload, and strips
    ``item_key`` before that per-element validation runs -- so a single
    malformed element never fails every other answer in the same reply (see
    ``engine.worker._settle_batch_answers``).  This function only builds the
    schema; it does not validate anything itself.
    """
    item_schema = copy.deepcopy(dict(role_schema or {}))
    properties = dict(item_schema.get("properties") or {})
    properties[BATCH_ITEM_KEY_FIELD] = {"type": "string"}
    item_schema["properties"] = properties
    required = [key for key in (item_schema.get("required") or []) if key != BATCH_ITEM_KEY_FIELD]
    required.append(BATCH_ITEM_KEY_FIELD)
    item_schema["required"] = required
    item_schema.setdefault("type", "object")
    return {
        "type": "object",
        "properties": {"answers": {"type": "array", "items": item_schema}},
        "required": ["answers"],
    }


def render_batch_user(items: Sequence[Tuple[str, str]]) -> str:
    """Render *items* -- ``(item_key, rendered_user_text)`` pairs -- as one
    numbered-block batch prompt.

    A plain, unambiguous delimiter naming the opaque item key, rather than
    asking the model to invent or preserve an ordering: settlement reads
    answers back by ``item_key``, never by position, so nothing here depends
    on the model preserving the order these blocks were sent in.
    """
    blocks = [
        f"### Item {index} (item_key: {item_key})\n{text}"
        for index, (item_key, text) in enumerate(items, start=1)
    ]
    return "\n\n".join(blocks)
