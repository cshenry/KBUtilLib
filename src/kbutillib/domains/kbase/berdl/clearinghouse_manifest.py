"""Bootstrap-load manifest: the declarative TOML that describes a source.

A clearinghouse bootstrap has two physically separate stages -- SHARD (this
file plus :mod:`clearinghouse_shard`) and INGEST (``load`` on
:class:`~kbutillib.domains.kbase.berdl.clearinghouse_capability.ClearinghouseCapability`)
-- and they are separate on purpose (see :mod:`clearinghouse_shard`). This
module owns the SHARD stage's declarative front door: a TOML manifest, its
parse, and the plan-time validator that turns a manifest into a per-table
plan the sharder can execute.

WHY TOML, AND WHY IT IS CONFIG AND NOT A LANGUAGE. A source is described
declaratively in TOML -- matching KBUtilLib's existing ``kbu-project.toml``
convention -- never as a bespoke Python script per source. The manifest maps
raw source columns onto a clearinghouse target table; the mapping vocabulary
is CLOSED AND SMALL (:data:`DERIVATIONS`). Anything a manifest cannot express
is not a reason to widen the manifest -- it is a source adapter in Python
(see :mod:`clearinghouse_shard`). A manifest is configuration; if you find
yourself wanting a conditional, a loop, or arithmetic in it, the logic
belongs in an adapter.

THE SHAPE, per source::

    [[source]]
    name        = "gaa-v2-genes"
    adapter     = "file"               # file | mongo | lakehouse
    path        = ".../V2_Genes.parquet"
    format      = "parquet"            # parquet | jsonl | tsv (file adapter)
    entity_type = "gene"
    kinds       = ["entity", "content"]

      [source.hash]
      raw_column  = "dna_sequence"     # the column carrying the raw value the
                                       # standardizer hashes
      # OR: precomputed = "<column>"   # source already carries a digest; it is
      #                                # RE-CANONICALISED through
      #                                # encode_entity_hash and NEVER trusted raw

      [source.content]
      sequence            = "dna_sequence"
      seq_length          = "@len(dna_sequence)"
      protein_entity_hash = "@hash(protein, protein_sequence)"

The MAPPING half -- ``entity_type``, ``kinds``, ``[source.hash]``,
``[source.content]``, ``[source.result]`` -- is IDENTICAL across all three
adapters. Only the source-locating keys differ (``path``/``format`` for
``file``; a connection/collection for ``mongo``; a table FQN for
``lakehouse``). That is the seam that makes the adapters interchangeable: the
sharder standardizes, hashes, sorts and writes the SAME way regardless of
where the records came from.

VALIDATION HAPPENS AT PLAN TIME, NOT AT WRITE TIME. :func:`shard_plan`
resolves a manifest to a per-table :class:`TablePlan` list and validates
every rule below. A manifest that passes :func:`shard_plan` is guaranteed
shardable -- a plan failure is always cheaper than an ingest failure, and an
ingest failure at this scale is expensive. The rules enforced, each naming
the offending key in its message:

  - ``entity_type`` must be one of
    :data:`~kbutillib.domains.kbase.berdl.clearinghouse_schema.ENTITY_TYPES`.
  - ``kinds`` must be a non-empty subset of ``entity`` / ``content`` /
    ``result``.
  - Every column a ``[source.content]`` block names must exist in the target
    type's content schema, read from
    :func:`clearinghouse_schema.column_names` -- NOT duplicated here. A
    manifest naming ``sequence`` for ``genome`` is rejected here, with the
    legal column list in the message, rather than at ingest.
  - ``source`` on a ``[source.result]`` block must be present and non-empty:
    it is the PARTITION column on every ``<type>_result`` table, and an empty
    partition value is a permanent scar on the table.
  - Every ``@derivation`` used anywhere in a mapping must be one of the closed
    :data:`DERIVATIONS` vocabulary; an unknown ``@name`` is rejected by name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # py 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on <3.11
    import tomli as tomllib  # type: ignore[no-redef]

from . import clearinghouse_schema as schema

#: The closed derivation vocabulary. A mapping value is either a plain source
#: column name (a bare string, no leading ``@``) or a ``@derivation(...)``
#: drawn from exactly this set. The set is deliberately small: a manifest is
#: CONFIG, not a language, and anything that needs more expressiveness is a
#: source adapter in Python, not a wider vocabulary here.
#:
#:   - ``@len(col)``       -- character length of ``col``'s value.
#:   - ``@hash(type, col)``-- ``standardizers.entity_hash(type, col_value)``;
#:     used e.g. for ``gene_content.protein_entity_hash``.
#:   - ``@column(col)``    -- the raw value of ``col`` (an explicit passthrough
#:     for when a bare-string column name would be ambiguous with a literal).
#:   - ``@json(a, b, ...)``-- a canonical JSON object of the named columns,
#:     used for a ``result`` payload.
#:   - ``@const(value)``   -- a literal constant, same on every row.
#:   - ``@lower(col)``     -- ASCII-lowercased value of ``col``. Earns its
#:     place on a real hazard: ``source`` is the partition column on all five
#:     ``<type>_result`` tables, so ``BAKTA/1.9`` and ``bakta/1.9`` would be
#:     two permanent partitions holding the same tool's output and no query
#:     filtering on one would see the other. Use it on ``source``.
DERIVATIONS: tuple[str, ...] = (
    "len",
    "hash",
    "column",
    "json",
    "const",
    "lower",
)

#: The three kinds a source may target, mirroring
#: :data:`clearinghouse_schema._KINDS` (kept as its own public constant so
#: callers do not reach into a private schema name).
KINDS: tuple[str, ...] = ("entity", "content", "result")

#: Matches a ``@name(args)`` derivation head. The argument text is captured
#: raw and split by the derivation's own arity rules; only the NAME is matched
#: here so an unknown ``@name`` is rejected by name rather than by a parse
#: error.
_DERIVATION_RE = re.compile(r"^@(?P<name>[A-Za-z_]+)\((?P<args>.*)\)$")


class ManifestError(ValueError):
    """A manifest failed to parse or validate.

    A subclass of :class:`ValueError` so callers can catch either. Every
    message names the offending source, block, and key so a plan failure
    points straight at the manifest text to fix.
    """


@dataclass(frozen=True)
class Derivation:
    """A parsed mapping value: either a bare column ref or a ``@derivation``.

    ``name`` is ``"column"`` for a bare-string mapping value (normalised to
    the explicit passthrough form) or the derivation name otherwise. ``args``
    is the ordered argument list, already split and stripped. ``raw`` is the
    original manifest text, kept for error messages.
    """

    name: str
    args: tuple[str, ...]
    raw: str


@dataclass(frozen=True)
class TablePlan:
    """A validated, ready-to-shard plan for ONE target table.

    One :class:`Source` with ``kinds = ["entity", "content"]`` resolves to two
    ``TablePlan`` objects -- one per kind -- because each kind is a distinct
    physical table with its own directory in the bronze layout. The sharder
    consumes a flat list of ``TablePlan`` and needs nothing from the manifest
    beyond what is captured here.
    """

    source_name: str
    entity_type: str
    kind: str
    #: The physical target table name, always via
    #: :func:`clearinghouse_schema.table_name`.
    table: str
    #: Parsed hash spec: ``("raw", column, entity_type)`` or
    #: ``("precomputed", column, entity_type)``.
    hash_source: tuple[str, str, str]
    #: Mapping of target column name -> parsed :class:`Derivation`. For an
    #: ``entity`` kind this is empty (entity tables carry only the generic
    #: identity columns the sharder fills itself).
    columns: dict[str, Derivation]


@dataclass(frozen=True)
class Source:
    """One ``[[source]]`` block, parsed but not yet resolved to table plans."""

    name: str
    adapter: str
    entity_type: str
    kinds: tuple[str, ...]
    #: Adapter-specific source-locating keys (``path``/``format`` for file,
    #: etc.), passed through to the adapter untouched. The mapping half never
    #: reads these.
    locator: Mapping[str, Any]
    hash_spec: Mapping[str, Any]
    content: Mapping[str, Any]
    result: Mapping[str, Any]


@dataclass(frozen=True)
class Manifest:
    """A parsed manifest: an ordered list of :class:`Source` blocks."""

    sources: tuple[Source, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------
# Parse
# --------------------------------------------------------------------------

#: Keys of a ``[[source]]`` table that belong to the MAPPING half (validated
#: here) rather than the adapter-specific locator. Everything else in the
#: block is treated as a locator key and handed to the adapter untouched.
_MAPPING_KEYS = frozenset(
    {"name", "adapter", "entity_type", "kinds", "hash", "content", "result"}
)


def load_manifest(source: str | Path) -> Manifest:
    """Parse a manifest from a TOML file path or a TOML string.

    Args:
        source: A filesystem path (``str`` or :class:`~pathlib.Path`) to a
            ``.toml`` manifest, or the TOML text itself. A value that names an
            existing file is read from disk; otherwise it is parsed as text.

    Returns:
        The parsed :class:`Manifest`. This performs STRUCTURAL parsing only
        (shapes and required keys); semantic validation -- legal columns,
        derivation vocabulary, non-empty result source -- happens in
        :func:`shard_plan`, so a manifest that parses is not yet guaranteed
        shardable.

    Raises:
        ManifestError: The TOML is malformed, has no ``[[source]]`` blocks, or
            a block is missing a required structural key.
    """
    text = _read_manifest_text(source)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"manifest is not valid TOML: {exc}") from exc

    raw_sources = data.get("source")
    if not raw_sources:
        raise ManifestError(
            "manifest has no [[source]] blocks; at least one is required."
        )
    if not isinstance(raw_sources, list):
        raise ManifestError(
            "manifest 'source' must be an array of tables ([[source]]), "
            f"got {type(raw_sources).__name__}."
        )

    sources = tuple(_parse_source(raw, index=i) for i, raw in enumerate(raw_sources))
    return Manifest(sources=sources)


def _read_manifest_text(source: str | Path) -> str:
    if isinstance(source, Path):
        return source.read_text(encoding="utf-8")
    # A str that names a readable file is loaded; otherwise treated as TOML.
    try:
        candidate = Path(source)
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    except OSError:
        pass
    return source


def _parse_source(raw: Any, *, index: int) -> Source:
    if not isinstance(raw, Mapping):
        raise ManifestError(
            f"[[source]] #{index} is not a table, got {type(raw).__name__}."
        )
    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise ManifestError(
            f"[[source]] #{index} is missing a non-empty string 'name'."
        )
    adapter = raw.get("adapter")
    if not adapter or not isinstance(adapter, str):
        raise ManifestError(
            f"source {name!r} is missing a non-empty string 'adapter' "
            "(one of: file, mongo, lakehouse)."
        )
    entity_type = raw.get("entity_type")
    if not entity_type or not isinstance(entity_type, str):
        raise ManifestError(
            f"source {name!r} is missing a non-empty string 'entity_type'."
        )
    kinds = raw.get("kinds")
    if not isinstance(kinds, list) or not kinds:
        raise ManifestError(
            f"source {name!r} 'kinds' must be a non-empty array, e.g. "
            '["entity", "content"].'
        )
    if not all(isinstance(k, str) for k in kinds):
        raise ManifestError(f"source {name!r} 'kinds' must be strings.")

    locator = {k: v for k, v in raw.items() if k not in _MAPPING_KEYS}
    return Source(
        name=name,
        adapter=adapter,
        entity_type=entity_type,
        kinds=tuple(kinds),
        locator=locator,
        hash_spec=dict(raw.get("hash") or {}),
        content=dict(raw.get("content") or {}),
        result=dict(raw.get("result") or {}),
    )


# --------------------------------------------------------------------------
# Derivation parsing
# --------------------------------------------------------------------------


def parse_derivation(value: Any, *, where: str) -> Derivation:
    """Parse ONE mapping value into a :class:`Derivation`.

    A bare string with no leading ``@`` is a source-column reference,
    normalised to ``@column`` form. A ``@name(args)`` value is validated
    against the closed :data:`DERIVATIONS` vocabulary and its arity checked.

    Args:
        value: The raw manifest value (a string).
        where: A human-readable location for error messages, e.g.
            ``"source 'gaa-v2-genes' [source.content].seq_length"``.

    Raises:
        ManifestError: ``value`` is not a string, is a ``@name`` outside the
            vocabulary, or a known derivation with the wrong arity.
    """
    if not isinstance(value, str):
        raise ManifestError(
            f"{where}: mapping value must be a string, got "
            f"{type(value).__name__}."
        )
    raw = value.strip()
    match = _DERIVATION_RE.match(raw)
    if match is None:
        if raw.startswith("@"):
            raise ManifestError(
                f"{where}: {raw!r} looks like a @derivation but is not "
                "well-formed; expected @name(args)."
            )
        # A bare column reference -> explicit @column passthrough.
        return Derivation(name="column", args=(raw,), raw=raw)

    name = match.group("name")
    if name not in DERIVATIONS:
        raise ManifestError(
            f"{where}: unknown @derivation @{name}; the vocabulary is closed "
            f"and small -- one of {', '.join('@' + d for d in DERIVATIONS)}. "
            "Anything needing more is a source adapter in Python, not a wider "
            "manifest."
        )
    args = _split_args(match.group("args"))
    _check_arity(name, args, where=where)
    return Derivation(name=name, args=tuple(args), raw=raw)


def _split_args(arg_text: str) -> list[str]:
    """Split a derivation's argument text on commas, stripping whitespace.

    Empty argument text yields no arguments (not a single empty string), so
    ``@const()`` parses as zero args and fails its arity check with a clear
    message rather than looking like one empty-string arg.
    """
    stripped = arg_text.strip()
    if not stripped:
        return []
    return [part.strip() for part in stripped.split(",")]


def _check_arity(name: str, args: Sequence[str], *, where: str) -> None:
    """Reject a known derivation used with the wrong number of arguments."""
    if name in ("len", "column", "lower"):
        expected = "exactly one column argument"
        ok = len(args) == 1 and bool(args[0])
    elif name == "hash":
        expected = "exactly two arguments: @hash(entity_type, column)"
        ok = len(args) == 2 and all(args)
    elif name == "const":
        expected = "exactly one argument: @const(value)"
        ok = len(args) == 1
    elif name == "json":
        expected = "one or more column arguments: @json(a, b, ...)"
        ok = len(args) >= 1 and all(args)
    else:  # pragma: no cover - guarded by the DERIVATIONS membership check
        return
    if not ok:
        raise ManifestError(
            f"{where}: @{name} takes {expected}, got {list(args)!r}."
        )


# --------------------------------------------------------------------------
# Plan-time validation
# --------------------------------------------------------------------------


def shard_plan(manifest: Manifest) -> list[TablePlan]:
    """Resolve and validate a manifest to a flat list of :class:`TablePlan`.

    This is the single plan-time gate: a manifest that passes here is
    guaranteed shardable, because every rule the sharder relies on is checked
    now, against the SAME schema definition (via
    :func:`clearinghouse_schema.column_names`) the ingest DDL is built from. A
    plan failure is always cheaper than an ingest failure.

    Each source expands to one ``TablePlan`` per declared kind (a distinct
    physical table, hence a distinct bronze directory).

    Raises:
        ManifestError: Any validation rule fails; the message names the
            offending source and key.
    """
    plans: list[TablePlan] = []
    for source in manifest.sources:
        plans.extend(_plan_source(source))
    return plans


def _plan_source(source: Source) -> list[TablePlan]:
    name = source.name
    etype = source.entity_type

    if etype not in schema.ENTITY_TYPES:
        raise ManifestError(
            f"source {name!r}: unknown entity_type {etype!r}; expected one of "
            f"{', '.join(schema.ENTITY_TYPES)}."
        )

    unknown_kinds = [k for k in source.kinds if k not in KINDS]
    if unknown_kinds:
        raise ManifestError(
            f"source {name!r}: unknown kind(s) {unknown_kinds!r}; kinds must "
            f"be a subset of {', '.join(KINDS)}."
        )

    hash_source = _plan_hash(source)

    plans: list[TablePlan] = []
    for kind in source.kinds:
        table = schema.table_name(etype, kind)
        if kind == "content":
            columns = _plan_content(source)
        elif kind == "result":
            columns = _plan_result(source)
        else:  # entity: only the generic identity columns, filled by the sharder
            columns = {}
        plans.append(
            TablePlan(
                source_name=name,
                entity_type=etype,
                kind=kind,
                table=table,
                hash_source=hash_source,
                columns=columns,
            )
        )
    return plans


def _plan_hash(source: Source) -> tuple[str, str, str]:
    """Validate ``[source.hash]`` -> ``(mode, column, entity_type)``.

    Exactly one of ``raw_column`` / ``precomputed`` must be present.

    ``precomputed`` accepts the source's hash RULE on trust: the sharder
    passes the digest through
    :func:`clearinghouse_schema.encode_entity_hash`, which validates only the
    digest FORMAT (64 lowercase hex characters) and cannot check WHICH rule
    produced it. A digest whose rule is unknown should be spot-checked against
    a re-derivation from sequence before a bulk load depends on it.

    ``raw_column`` is REJECTED for ``entity_type == "genome"``. A genome's
    identity comes from its contig SET, which no single column can carry;
    routing a genome through ``raw_column`` would hand a single string to the
    genome standardizer, which iterates it character by character and produces
    a wrong hash silently. Genomes route via ``precomputed`` or the
    ``KBDLHashGenomes`` job instead. This keeps the module's contract: a
    manifest that passes ``shard_plan()`` must be shardable, and a plan
    failure is always cheaper than an ingest failure.
    """
    name = source.name
    spec = source.hash_spec
    if not spec:
        raise ManifestError(
            f"source {name!r}: missing [source.hash] block; it must name "
            "either raw_column or precomputed."
        )
    raw_column = spec.get("raw_column")
    precomputed = spec.get("precomputed")
    if bool(raw_column) == bool(precomputed):
        raise ManifestError(
            f"source {name!r} [source.hash]: exactly one of raw_column or "
            "precomputed must be set (not both, not neither)."
        )
    if raw_column:
        if not isinstance(raw_column, str):
            raise ManifestError(
                f"source {name!r} [source.hash].raw_column must be a string."
            )
        if source.entity_type == "genome":
            raise ManifestError(
                f"source {name!r} [source.hash]: raw_column is not allowed for "
                "entity_type 'genome'. A genome's identity comes from its "
                "contig SET, which no single column can carry; a raw_column "
                "would pass one string to the genome standardizer and be "
                "iterated character by character into a wrong hash. Route a "
                "genome through precomputed or the KBDLHashGenomes job."
            )
        return ("raw", raw_column, source.entity_type)
    if not isinstance(precomputed, str):
        raise ManifestError(
            f"source {name!r} [source.hash].precomputed must be a string."
        )
    return ("precomputed", precomputed, source.entity_type)


def _plan_content(source: Source) -> dict[str, Derivation]:
    """Validate a ``[source.content]`` block against the type's content schema.

    EVERY column the block names must exist in the target type's content
    schema, read from :func:`clearinghouse_schema.column_names` -- the legal
    set is never duplicated here. An unknown column is rejected with the full
    legal list in the message.
    """
    name = source.name
    etype = source.entity_type
    if not source.content:
        raise ManifestError(
            f"source {name!r}: kind 'content' is declared but there is no "
            "[source.content] block mapping its columns."
        )
    legal = schema.column_names(etype, "content")
    # The sharder fills these identity/provenance columns itself; a manifest
    # neither needs nor may remap them.
    reserved = {"entity_hash", "standardizer_version", "observed_at", "ingest_batch_id"}
    columns: dict[str, Derivation] = {}
    for column, value in source.content.items():
        if column not in legal:
            raise ManifestError(
                f"source {name!r} [source.content]: column {column!r} is not "
                f"in the content schema for entity_type {etype!r}. Legal "
                f"columns: {', '.join(legal)}."
            )
        if column in reserved:
            raise ManifestError(
                f"source {name!r} [source.content]: column {column!r} is "
                "filled by the sharder from the standardizer and must not be "
                "mapped in the manifest."
            )
        columns[column] = parse_derivation(
            value, where=f"source {name!r} [source.content].{column}"
        )
    return columns


def _plan_result(source: Source) -> dict[str, Derivation]:
    """Validate a ``[source.result]`` block, including the non-empty ``source``.

    The result ``source`` column is the PARTITION column on every
    ``<type>_result`` table; an empty partition value is a permanent scar, so
    it is required and non-empty AT PLAN TIME. Every other named column must
    exist in the generic result schema.
    """
    name = source.name
    if not source.result:
        raise ManifestError(
            f"source {name!r}: kind 'result' is declared but there is no "
            "[source.result] block mapping its columns."
        )
    legal = schema.column_names(source.entity_type, "result")
    reserved = {"entity_hash", "entity_type", "observed_at", "ingest_batch_id"}

    result_source = source.result.get("source")
    if result_source is None or (
        isinstance(result_source, str) and not result_source.strip()
    ):
        raise ManifestError(
            f"source {name!r} [source.result]: 'source' is required and must "
            "be non-empty -- it is the partition column on the "
            f"{source.entity_type}_result table and an empty partition value "
            "is a permanent scar on the table."
        )

    columns: dict[str, Derivation] = {}
    for column, value in source.result.items():
        if column not in legal:
            raise ManifestError(
                f"source {name!r} [source.result]: column {column!r} is not "
                f"in the result schema. Legal columns: {', '.join(legal)}."
            )
        if column in reserved:
            raise ManifestError(
                f"source {name!r} [source.result]: column {column!r} is "
                "filled by the sharder and must not be mapped in the manifest."
            )
        columns[column] = parse_derivation(
            value, where=f"source {name!r} [source.result].{column}"
        )
    return columns
