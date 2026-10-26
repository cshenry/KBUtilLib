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
    adapter     = "file"               # file | mongo | lakehouse | arc
    path        = ".../V2_Genes.parquet"
    format      = "parquet"            # parquet | jsonl | tsv (file adapter)
    entity_type = "gene"
    kinds       = ["entity", "content"]
    observed_at_column = "updated_at"  # OPTIONAL. See below.

      [source.hash]
      raw_column  = "dna_sequence"     # the column carrying the raw value the
                                       # standardizer hashes
      # OR: precomputed = "<column>"   # source already carries a digest; it is
      #                                # RE-CANONICALISED through
      #                                # encode_entity_hash and NEVER trusted raw
      # OR: contig_set  = "<column>"   # GENOME ONLY: the column holds the
      #                                # genome's contig sequences as a LIST
      #                                # of strings, hashed as a SET

      [source.content]
      sequence            = "dna_sequence"
      seq_length          = "@len(dna_sequence)"
      protein_entity_hash = "@hash(protein, protein_sequence)"

      [source.result]
      source        = "@lower(algorithm)"
      result_type   = "@const(functional_annotation)"
      payload       = "@json(function_id, score)"
      parameter_set = {}                 # REQUIRED; {} declares a default run
      # OR: parameter_set_column = "<column>"   # the PER-ROW alternative: the
      #                                 # column holds this row's parameter set
      #                                 # as a JSON OBJECT (a mapping)

``observed_at_column`` IS PART OF THE MAPPING HALF, NOT THE LOCATOR. It
names a SOURCE column carrying each record's observation time (an ISO-8601
string or a datetime, normalised to UTC by the sharder). It is OPTIONAL;
omitted, the sharder stamps its own run start time on every row of the
source. Either way ``observed_at`` is NEVER NULL on a bronze row -- it is one
of the two columns
:mod:`~kbutillib.domains.kbase.berdl.clearinghouse_derivation` orders the
current-state window function by, so a null there makes "the current row"
undefined for that slot. Because it is mapping, not locator, it is listed in
:data:`_MAPPING_KEYS`: a top-level key this module does not claim falls
through to the adapter's locator untouched, where a mongo or lakehouse
adapter would ignore it and the sharder would never see it -- the declaration
would be silently inert.

The MAPPING half -- ``entity_type``, ``kinds``, ``observed_at_column``,
``[source.hash]``, ``[source.content]``, ``[source.result]`` -- is IDENTICAL
across all three adapters. Only the source-locating keys differ (``path``/``format`` for
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
  - ``[source.hash]`` declares EXACTLY ONE of ``raw_column``,
    ``precomputed`` or ``contig_set``; ``contig_set`` only for
    ``entity_type = "genome"`` and ``raw_column`` never for it.
  - EXACTLY ONE of ``parameter_set`` / ``parameter_set_column`` on a
    ``[source.result]`` block. ``parameter_set`` is an inline TOML table (a
    mapping after parsing), never a ``@derivation`` string;
    ``parameter_set_column`` is a source column NAME -- see below.
  - Every ``@derivation`` used anywhere in a mapping must be one of the closed
    :data:`DERIVATIONS` vocabulary; an unknown ``@name`` is rejected by name.

THE RESULT ``parameter_set``, and why it is not a mapping value like the
others. A clearinghouse result is keyed by ``(entity, result_type, source,
PARAMETER_SET_HASH)``, so the same protein run through the same tool version
with different parameters is a distinct result rather than one run silently
overwriting the other. The manifest therefore has to say which parameter set
the whole source represents, and it says it as DATA -- an inline table::

    parameter_set = {}                      # a default run
    parameter_set = { taxonomy_id = "562" } # TRANSYT, which requires one

It is NOT routed through :func:`parse_derivation`: a derivation names a
source COLUMN and produces a string per row, whereas a parameter set is a
structured constant for the source as a whole, validated against the
parameter-set rule (:mod:`kbutillib.domains.identity.parameter_sets`) and
hashed ONCE at plan time. Note TOML parses ``1.0e-5`` as a float, which the
rule rejects -- decimals are passed as strings (``evalue = "1e-5"``) -- so
``parameter_set = { evalue = 1.0e-5 }`` fails HERE rather than producing a
result row keyed by a hash no second writer could reproduce.

``parameter_set`` is declared once for the source. ITS PER-ROW ALTERNATIVE IS
``parameter_set_column = "<column>"``, and EXACTLY ONE OF THE TWO must be
declared (not both, not neither -- a result source must still say which
parameter set it represents; see :func:`_plan_parameter_set`). The named
column holds a MAPPING per row, which the sharder validates and canonicalises
through the SAME rule the static form uses
(:func:`clearinghouse_parameters.result_parameter_set_identity`), so the two
forms produce byte-identical canonical text and an identical hash for an
equal parameter set. A row whose value is not a mapping, carries a float, a
``None``, or a bad key is a
:class:`~clearinghouse_shard.ShardParameterSetError` -- that ONE row is
skipped and counted, rather than the run failing, because a per-row set is
source DATA and one bad record is not a reason to abandon the other
hundreds. The registry then gets one row per DISTINCT per-row set, exactly as
it does for the static form.

WHY A COLUMN AND NOT A DERIVATION. The per-row form is still DATA, not a
mapping value: it names the column whose VALUE IS ALREADY the parameter set,
and the sharder reads it with no derivation vocabulary involved. It exists
for an adapter that computes the set per record -- the ``arc`` adapter yields
``{"taxonomy_id": "<ncbi taxid>"}`` on TRANSYT rows and ``{}`` on every other
tool's, which no single static declaration could express because one source
feeds several tools.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # py 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on <3.11
    import tomli as tomllib  # type: ignore[no-redef]

from ...identity import ParameterSetError
from . import clearinghouse_parameters as cp
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
#:   - ``@prefix(text, col)`` -- ``col``'s value with the literal ``text``
#:     prepended, or ``None`` when ``col`` is null. NULL-PRESERVING is the
#:     whole reason it is a derivation and not string concatenation in an
#:     adapter: ``genome_content.fasta_reference`` is a POINTER, and
#:     ``"obj://" + None`` would write the literal pointer
#:     ``"obj://None"`` -- a reference to an object that does not exist,
#:     indistinguishable in the lake from a real one. A missing pointer must
#:     stay missing. The prefix text is the FIRST argument and may contain
#:     colons and slashes (``@prefix(obj://,fasta_object_id)``); it may not contain a
#:     comma, which is the argument separator.
#:   - ``@concat(text, col, text, col, ...)`` -- ``@prefix`` generalised to a
#:     pointer built from MORE THAN ONE column. The arguments STRICTLY
#:     ALTERNATE literal text and column name, starting with literal text, so
#:     the argument count is always even; a literal may be empty (``@concat(,
#:     a, :, b)`` is ``"<a>:<b>"``) but a column name may not. NULL IN, NULL
#:     OUT, and it is null-preserving across EVERY named column, for exactly
#:     the reason ``@prefix`` is: ``genome_content.fasta_reference`` for a
#:     KBDL archive member is
#:     ``@concat(obj://,archive_object_id,#,entry_name)``, and a row
#:     missing either half would otherwise write
#:     ``"obj://None#x"`` -- a pointer to nothing, indistinguishable
#:     in the lake from a real one. Literals may contain colons; they may not
#:     contain a comma, which is the argument separator.
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
    "prefix",
    "concat",
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
    #: The validated ``[source.result].parameter_set`` mapping, or ``None``
    #: for a non-``result`` kind AND for a source that declared the PER-ROW
    #: form instead (``parameter_set_column``), whose set is not a constant
    #: and so has no plan-time value. Carried as DATA (not a derivation)
    #: because it is a structured constant for the whole source.
    parameter_set: dict[str, Any] | None = None
    #: Its canonical JSON text -- the registry's ``canonical_json`` value.
    #: Computed ONCE here at plan time, never per row.
    parameter_set_json: str | None = None
    #: Its 64-char lowercase hex sha256 -- the ``parameter_set_hash`` the
    #: sharder stamps on every result row. Computed once, at plan time.
    parameter_set_hash: str | None = None
    #: The source's optional ``observed_at_column``, carried through from
    #: :class:`Source` so the sharder needs nothing but the plan.
    observed_at_column: str | None = None
    #: The SOURCE column holding each row's own parameter set, when the
    #: source declared the per-row form; ``None`` for the static form (where
    #: ``parameter_set``/``_json``/``_hash`` carry the plan-time values) and
    #: for every non-``result`` kind. Exactly one of this and
    #: ``parameter_set_hash`` is set on a ``result`` plan, which is how the
    #: sharder knows whether to stamp the plan's hash or derive the row's.
    parameter_set_column: str | None = None


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
    #: Optional top-level ``observed_at_column``: the name of a SOURCE column
    #: carrying this record's observation time. ``None`` means the sharder
    #: stamps its run start time on every row of this source instead. Either
    #: way ``observed_at`` is never null -- see
    #: :func:`clearinghouse_shard.resolve_observed_at`.
    observed_at_column: str | None = None


@dataclass(frozen=True)
class BackfillInfo:
    """The optional top-level ``[backfill]`` table: INFORMATIONAL ONLY.

    A manifest may carry a ``[backfill]`` table describing what a backfill of
    it pulls from and what it needs in the environment. Nothing in the shard
    or ingest path reads it -- ``kbu clearinghouse backfill list`` prints it so
    an operator can see what a preset will reach for BEFORE running it, and
    ``needs_env`` is what makes an unresolved ``${VAR}`` diagnosable rather
    than merely fatal.

    It is parsed into this object precisely SO THAT IT IS NOT A LOCATOR. The
    table lives at the top level, not inside a ``[[source]]`` block, so it
    could never have been mistaken for an adapter key -- but it would have
    been silently ignored, and a typo in it would then be invisible. Parsing
    it validates its two keys and rejects anything else by name.
    """

    #: Human-readable identifiers of the stores a backfill of this manifest
    #: reads, e.g. ``["mongo://poplar:27017/database.seq_protein_bakta"]``.
    pulls_from: tuple[str, ...] = field(default_factory=tuple)
    #: Environment variable names the manifest's placeholders expect. Advisory:
    #: an unresolved ``${VAR}`` fails at load regardless of whether it is
    #: listed here, and a listed-but-unused name is not an error.
    needs_env: tuple[str, ...] = field(default_factory=tuple)
    #: Optional ``[backfill.tool_versions]``: tool name -> version string,
    #: used by an adapter that must build a result ``source`` of the form
    #: ``"<tool>/<version>"`` for SEVERAL tools out of ONE store. Unlike the
    #: other two keys this one IS READ -- the ``arc`` adapter resolves each
    #: annotation row's version through it (see
    #: :meth:`clearinghouse_shard.SourceAdapter.bind_backfill_info`) and
    #: defaults any tool not listed to
    #: :data:`DEFAULT_TOOL_VERSION`.
    #:
    #: It lives in ``[backfill]`` rather than in a ``[[source]]`` locator
    #: because the version is a property of the OPERATOR'S INSTALLATION, not
    #: of where the records are: four sources read one arc store and all four
    #: must label the same tool the same way, and duplicating the table per
    #: source is how two of them would come to disagree -- producing two
    #: permanent ``source`` partitions for one tool.
    tool_versions: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Manifest:
    """A parsed manifest: an ordered list of :class:`Source` blocks."""

    sources: tuple[Source, ...] = field(default_factory=tuple)
    #: The optional ``[backfill]`` table, or ``None`` when absent.
    backfill: BackfillInfo | None = None


# --------------------------------------------------------------------------
# Parse
# --------------------------------------------------------------------------

#: Keys of a ``[[source]]`` table that belong to the MAPPING half (validated
#: here) rather than the adapter-specific locator. Everything else in the
#: block is treated as a locator key and handed to the adapter untouched.
_MAPPING_KEYS = frozenset(
    {
        "name",
        "adapter",
        "entity_type",
        "kinds",
        "hash",
        "content",
        "result",
        # Not a locator key. ``observed_at_column`` names a SOURCE column the
        # sharder reads per row for ``observed_at``; it is part of the mapping
        # half (adapter-independent), so it must be listed here or it would
        # fall through to the adapter's locator and be read as, say, a mongo
        # connection key -- silently ignored by the adapter and silently
        # unseen by the sharder.
        "observed_at_column",
    }
)


#: Matches one INNERMOST ``${VAR}`` or ``${VAR:-default}`` placeholder. The
#: name is a shell-style identifier; the default may be empty but may contain
#: NO braces, which is what makes the match innermost -- an outer
#: ``${A:-${B}/x}`` does not match until ``${B}`` has been substituted. See
#: :func:`expand_placeholders` for why that matters.
_PLACEHOLDER_RE = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^{}]*))?\}"
)

#: Anything that still LOOKS like a placeholder after expansion finished.
#: Used only to turn a pathological input into a named error rather than an
#: unbounded loop.
_PLACEHOLDER_RESIDUE_RE = re.compile(r"\$\{[^{}]*\}")

#: How many innermost-first passes :func:`expand_placeholders` will make. One
#: pass per nesting level, and a hard cap so an environment value that itself
#: contains ``${...}`` cannot drive an unbounded loop.
_MAX_EXPANSION_PASSES = 8

#: An INJECTED variable: the directory the manifest file itself was read from.
#: It is what lets a preset shipped as PACKAGE DATA point at a fixture shipped
#: beside it -- ``${MANIFEST_DIR}/file-example.parquet`` -- without the
#: manifest having to know where pip put the wheel, and without the preset
#: loader special-casing one preset's path. It is AUTHORITATIVE, not a default:
#: the environment cannot override it, because the manifest's own location is
#: a fact about the file rather than an operator preference. It is unavailable
#: (and so an error, like any unset bare variable) when the manifest was
#: parsed from a TOML string with no path.
MANIFEST_DIR_VAR = "MANIFEST_DIR"


def expand_placeholders(
    value: str,
    *,
    where: str,
    env: Mapping[str, str] | None = None,
) -> str:
    """Expand ``${VAR}`` / ``${VAR:-default}`` and a LEADING ``~`` in one string.

    A manifest is config, and a preset shipped as package data cannot hardcode
    one operator's home directory, one pod's mount point or one store's
    hostname. This is the whole of the substitution vocabulary -- two forms and
    a tilde, no command substitution, no arithmetic, no nesting -- for the same
    reason :data:`DERIVATIONS` is closed: a manifest that needs more than this
    needs an adapter, not a wider language.

    EXPANSION IS INNERMOST-FIRST, so a DEFAULT may itself be a placeholder:
    ``${KBU_FIXTURE:-${MANIFEST_DIR}/example.parquet}`` resolves to the
    override when it is set and to the shipped fixture otherwise. One pass
    per nesting level, capped at :data:`_MAX_EXPANSION_PASSES`.

    ``${VAR}`` with no value in the environment is a :class:`ManifestError`
    NAMING THE VARIABLE. That is the point of the form: a preset that silently
    expanded an unset variable to the empty string would resolve
    ``${ARC_ROOT}/store`` to ``/store`` and then fail hundreds of lines later
    with a path nobody recognises. ``${VAR:-default}`` is the form that says
    "unset is fine", and it never raises.

    The tilde is expanded LAST and only when it LEADS the fully substituted
    result, so ``${HOME_OVERRIDE:-~/clearinghouse}`` works and a tilde in the
    middle of a path (a legal filename character) is left alone.

    Args:
        value: The raw manifest string.
        where: A human description of the value's location, used in the error
            message (e.g. ``"source 'arc-proteins' key 'path'"``).
        env: The environment to read; defaults to :data:`os.environ`.

    Returns:
        The expanded string.

    Raises:
        ManifestError: A ``${VAR}`` placeholder with no default is unset or
            empty in the environment.
    """
    environ = os.environ if env is None else env
    missing: list[str] = []

    def _substitute(match: re.Match[str]) -> str:
        name = match.group("name")
        default = match.group("default")
        resolved = environ.get(name)
        if resolved:
            return resolved
        if default is not None:
            return default
        missing.append(name)
        return ""

    expanded = value
    for _ in range(_MAX_EXPANSION_PASSES):
        nxt = _PLACEHOLDER_RE.sub(_substitute, expanded)
        if nxt == expanded:
            break
        expanded = nxt
    if not missing and _PLACEHOLDER_RESIDUE_RE.search(expanded):
        raise ManifestError(
            f"{where}: placeholder expansion did not settle after "
            f"{_MAX_EXPANSION_PASSES} passes and {expanded!r} still contains "
            "a ${...} token. Expansion is innermost-first, one pass per "
            "nesting level, and is capped so that an environment value which "
            "itself contains a placeholder cannot drive an unbounded loop. "
            "Flatten the value."
        )
    if missing:
        names = ", ".join(sorted(dict.fromkeys(missing)))
        raise ManifestError(
            f"{where}: unresolved manifest placeholder(s) ${{{names}}} -- "
            f"{'these variables are' if len(set(missing)) > 1 else 'this variable is'} "
            "not set in the environment and the value declares no default. "
            "Either export it, or write it as ${NAME:-<default>} if an unset "
            "variable is acceptable. (An unset ${NAME} is refused rather than "
            "expanded to the empty string, which would turn '${ROOT}/store' "
            "into '/store' and fail much later with a path nobody recognises.)"
        )
    if expanded.startswith("~"):
        expanded = str(Path(expanded).expanduser())
    return expanded


def _expand_locator_value(value: Any, *, where: str, env: Mapping[str, str] | None) -> Any:
    """Expand placeholders through a locator value of any shape.

    Strings are expanded; lists and mappings are walked so a nested locator
    (``decompress = { sequence = "${FIELD}" }``) is covered too. Numbers,
    booleans and dates pass through untouched -- there is nothing in them to
    expand, and coercing them to strings to find out would change the value
    the adapter receives.
    """
    if isinstance(value, str):
        return expand_placeholders(value, where=where, env=env)
    if isinstance(value, Mapping):
        return {
            key: _expand_locator_value(item, where=f"{where}.{key}", env=env)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _expand_locator_value(item, where=f"{where}[{i}]", env=env)
            for i, item in enumerate(value)
        ]
    return value


#: The version an adapter uses for a tool the ``[backfill.tool_versions]``
#: table does not list. ``"1.0"`` and not ``"unknown"``: ``source`` is the
#: permanent partition column on every ``<type>_result`` table, so the value
#: has to be a VERSION, and Chris's instruction (2026-10-06) is to use the
#: installed version where it can be determined and ``1.0`` where it cannot.
DEFAULT_TOOL_VERSION = "1.0"

#: The keys ``[backfill]`` defines. ``tool_versions`` is a sub-TABLE, not an
#: array of strings, so it is validated separately from the other two.
_BACKFILL_KEYS = frozenset({"pulls_from", "needs_env", "tool_versions"})


def _parse_backfill(raw: Any) -> BackfillInfo:
    """Parse the optional top-level ``[backfill]`` table."""
    if not isinstance(raw, Mapping):
        raise ManifestError(
            "manifest '[backfill]' must be a table with optional "
            f"'pulls_from' and 'needs_env' arrays, got {type(raw).__name__}."
        )
    unknown = sorted(set(raw) - _BACKFILL_KEYS)
    if unknown:
        raise ManifestError(
            f"manifest '[backfill]' has unknown key(s) {unknown}; only "
            "'pulls_from' (what a backfill of this manifest reads), "
            "'needs_env' (the environment variables its placeholders expect) "
            "and 'tool_versions' (tool name -> version string, read by an "
            "adapter that labels several tools out of one store) are "
            "defined. The table is parsed rather than ignored precisely so a "
            "typo in it is visible."
        )
    fields: dict[str, tuple[str, ...]] = {}
    for key in ("pulls_from", "needs_env"):
        value = raw.get(key)
        if value is None:
            fields[key] = ()
            continue
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            raise ManifestError(
                f"manifest '[backfill]' key {key!r} must be an array of "
                f"non-empty strings, got {value!r}."
            )
        fields[key] = tuple(item.strip() for item in value)
    return BackfillInfo(
        pulls_from=fields["pulls_from"],
        needs_env=fields["needs_env"],
        tool_versions=_parse_tool_versions(raw.get("tool_versions")),
    )


def _parse_tool_versions(raw: Any) -> dict[str, str]:
    """Validate ``[backfill.tool_versions]`` -> a plain ``{tool: version}``.

    Every key and value must be a non-empty string. A version given as a
    TOML number is rejected by name, because ``1.0`` parses as the float
    ``1.0`` and would stringify to ``"1.0"`` here but could stringify
    differently elsewhere -- and ``source`` is a permanent partition value,
    so one tool must never end up with two spellings of one version.
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ManifestError(
            "manifest '[backfill.tool_versions]' must be a table of "
            f"tool -> version strings, got {type(raw).__name__}."
        )
    out: dict[str, str] = {}
    for tool, version in raw.items():
        if not isinstance(version, str) or not version.strip():
            raise ManifestError(
                f"manifest '[backfill.tool_versions]' key {tool!r} must be a "
                f"non-empty STRING version, got {type(version).__name__} "
                f"{version!r}. Quote it (version = \"1.0\"): an unquoted 1.0 "
                "is a TOML float, and 'source' is a permanent partition "
                "value that must have exactly one spelling."
            )
        out[str(tool)] = version.strip()
    return out


def load_manifest(
    source: str | Path, *, env: Mapping[str, str] | None = None
) -> Manifest:
    """Parse a manifest from a TOML file path or a TOML string.

    PLACEHOLDERS ARE EXPANDED HERE, at load, in every ``[[source]]`` LOCATOR
    value -- see :func:`expand_placeholders`. Load is the right seam because
    everything downstream (``shard_plan``, the adapters, the run inventory's
    ``manifest_sha256``) then sees ONE resolved manifest: an adapter never has
    to know that ``path`` might still contain a ``${VAR}``, and a run's
    provenance records what was actually read rather than a template of it.
    The MAPPING half is deliberately NOT expanded -- a ``@const(${VAR})`` in a
    result column would make the same manifest produce different partition
    values on different machines, which is the opposite of what the mapping
    half is for.

    Args:
        source: A filesystem path (``str`` or :class:`~pathlib.Path`) to a
            ``.toml`` manifest, or the TOML text itself. A value that names an
            existing file is read from disk; otherwise it is parsed as text.
        env: The environment placeholder expansion reads; defaults to
            :data:`os.environ`. Tests pass an explicit mapping.

    Returns:
        The parsed :class:`Manifest`. This performs STRUCTURAL parsing only
        (shapes and required keys); semantic validation -- legal columns,
        derivation vocabulary, non-empty result source -- happens in
        :func:`shard_plan`, so a manifest that parses is not yet guaranteed
        shardable.

    Raises:
        ManifestError: The TOML is malformed, has no ``[[source]]`` blocks, a
            block is missing a required structural key, ``[backfill]`` is
            malformed, or a ``${VAR}`` placeholder is unresolved.
    """
    text = _read_manifest_text(source)
    env = _expansion_env(source, env)
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

    sources = tuple(
        _parse_source(raw, index=i, env=env) for i, raw in enumerate(raw_sources)
    )
    raw_backfill = data.get("backfill")
    backfill = None if raw_backfill is None else _parse_backfill(raw_backfill)
    return Manifest(sources=sources, backfill=backfill)


def _expansion_env(
    source: str | Path, env: Mapping[str, str] | None
) -> Mapping[str, str]:
    """The environment expansion reads, with :data:`MANIFEST_DIR_VAR` injected.

    Injected AFTER the caller's environment so it cannot be overridden: a
    manifest's own directory is a fact about the file, and an operator who had
    ``MANIFEST_DIR`` exported for some unrelated reason would otherwise
    silently repoint every shipped preset's fixture. When the manifest came
    from a TOML string there is no directory, so the variable is simply absent
    and a manifest that uses it fails with the normal "unresolved placeholder"
    error naming it -- which is the honest answer.
    """
    base = dict(os.environ if env is None else env)
    path = _manifest_file(source)
    if path is not None:
        base[MANIFEST_DIR_VAR] = str(path.parent)
    else:
        base.pop(MANIFEST_DIR_VAR, None)
    return base


def _manifest_file(source: str | Path) -> Path | None:
    """The file ``source`` names, or ``None`` when it is TOML text."""
    try:
        candidate = Path(source)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None
    try:
        if candidate.is_file():
            return candidate.resolve()
    except OSError:  # pragma: no cover - e.g. a name longer than PATH_MAX
        return None
    return None


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


def _parse_source(
    raw: Any, *, index: int, env: Mapping[str, str] | None = None
) -> Source:
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
            "(one of: file, mongo, lakehouse, arc)."
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

    observed_at_column = raw.get("observed_at_column")
    if observed_at_column is not None and (
        not isinstance(observed_at_column, str) or not observed_at_column.strip()
    ):
        raise ManifestError(
            f"source {name!r}: 'observed_at_column', when given, must be a "
            "non-empty string naming a SOURCE column carrying each record's "
            "observation time (an ISO-8601 string or a datetime). Omit it to "
            "stamp the shard run's start time on every row."
        )

    # Placeholders are expanded over the LOCATOR half only (see
    # load_manifest): a locator names where the data lives, which legitimately
    # differs per machine and per operator, while the mapping half must be
    # byte-identical everywhere or the same manifest would write different
    # partition values on different hosts.
    locator = {
        key: _expand_locator_value(
            value, where=f"source {name!r} key {key!r}", env=env
        )
        for key, value in raw.items()
        if key not in _MAPPING_KEYS
    }
    return Source(
        name=name,
        adapter=adapter,
        entity_type=entity_type,
        kinds=tuple(kinds),
        locator=locator,
        hash_spec=dict(raw.get("hash") or {}),
        content=dict(raw.get("content") or {}),
        result=dict(raw.get("result") or {}),
        observed_at_column=(
            observed_at_column.strip() if observed_at_column is not None else None
        ),
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
    elif name == "prefix":
        expected = "exactly two arguments: @prefix(text, column)"
        # args[0] -- the literal prefix text -- must be non-empty (an empty
        # prefix is @column), and so must the column name.
        ok = len(args) == 2 and all(args)
    elif name == "concat":
        expected = (
            "an EVEN number of arguments, at least four, strictly alternating "
            "literal text and column name starting with the literal: "
            "@concat(text, column, text, column, ...). A literal may be empty; "
            "a column name may not. Two arguments is @prefix -- use that"
        )
        # Four is the minimum because a two-argument @concat is exactly
        # @prefix, and two spellings of one rule is how a manifest grammar
        # stops being readable.
        ok = (
            len(args) >= 4
            and len(args) % 2 == 0
            and all(args[index] for index in range(1, len(args), 2))
        )
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
        parameter_set: dict[str, Any] | None = None
        parameter_set_json: str | None = None
        parameter_set_hash: str | None = None
        parameter_set_column: str | None = None
        if kind == "content":
            columns = _plan_content(source)
        elif kind == "result":
            columns = _plan_result(source)
            (
                parameter_set,
                parameter_set_json,
                parameter_set_hash,
                parameter_set_column,
            ) = _plan_parameter_set(source, columns)
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
                parameter_set=parameter_set,
                parameter_set_json=parameter_set_json,
                parameter_set_hash=parameter_set_hash,
                observed_at_column=source.observed_at_column,
                parameter_set_column=parameter_set_column,
            )
        )
    return plans


def _plan_hash(source: Source) -> tuple[str, str, str]:
    """Validate ``[source.hash]`` -> ``(mode, column, entity_type)``.

    Exactly one of ``raw_column`` / ``precomputed`` / ``contig_set`` must be
    present.

    ``contig_set`` IS GENOME-ONLY, and it is the mode that makes a genome
    hashable from a manifest at all. A genome's identity is its contig SET,
    so the hashable value is a LIST of contig sequences rather than a scalar;
    this mode names the column carrying that list and hands it to
    ``standardizers.entity_hash("genome", list)`` unchanged. The list-ness is
    checked AT SHARD TIME, per row, not here -- a column's values are not
    visible at plan time -- and a row whose value is anything else (notably a
    ``str``, which the genome standardizer would iterate character by
    character into a plausible-looking wrong hash) is a
    :class:`~clearinghouse_shard.ShardValueError` for that row, counted like
    any other unmappable record. Declaring it for a non-genome type is
    refused HERE, because that is a manifest mistake and not a data one.

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
    contig_set = spec.get("contig_set")
    declared = [
        key
        for key, value in (
            ("raw_column", raw_column),
            ("precomputed", precomputed),
            ("contig_set", contig_set),
        )
        if value
    ]
    if len(declared) != 1:
        raise ManifestError(
            f"source {name!r} [source.hash]: exactly one of raw_column, "
            f"precomputed or contig_set must be set (not both, not neither); "
            f"got {declared or 'none'}."
        )
    if contig_set:
        if not isinstance(contig_set, str):
            raise ManifestError(
                f"source {name!r} [source.hash].contig_set must be a string."
            )
        if source.entity_type != "genome":
            raise ManifestError(
                f"source {name!r} [source.hash]: contig_set is allowed ONLY "
                f"for entity_type 'genome', not {source.entity_type!r}. It "
                "names a column holding a LIST of contig sequences, which is "
                "what a genome's identity is a set of; no other entity type "
                "is hashed from a collection."
            )
        return ("contig_set", contig_set, source.entity_type)
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
                "genome through contig_set (a column holding the contig "
                "sequence LIST), precomputed, or the KBDLHashGenomes job."
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


#: The ``[source.result]`` keys that declare the source's PARAMETER SET and
#: are therefore NOT target columns -- they are stripped before the
#: column-mapping loop and handled by :func:`_plan_parameter_set`.
#:
#: Two members, and EXACTLY ONE of them must appear on a result source: the
#: static ``parameter_set`` (one set for the whole source, hashed once at plan
#: time) or the per-row ``parameter_set_column`` (a source column whose value
#: IS that row's set, canonicalised and hashed per row through the same
#: rule). Both is an error and neither is an error -- see
#: :func:`_plan_parameter_set`.
_PARAMETER_SET_KEYS: tuple[str, ...] = ("parameter_set", "parameter_set_column")

#: The per-row member of :data:`_PARAMETER_SET_KEYS`, named so neither this
#: module nor the sharder spells it as a literal twice.
PARAMETER_SET_COLUMN_KEY = "parameter_set_column"


def _plan_result(source: Source) -> dict[str, Derivation]:
    """Validate a ``[source.result]`` block, including the non-empty ``source``.

    The result ``source`` column is the PARTITION column on every
    ``<type>_result`` table; an empty partition value is a permanent scar, so
    it is required and non-empty AT PLAN TIME. Every other named column must
    exist in the generic result schema -- except the
    :data:`_PARAMETER_SET_KEYS`, which declare the source's parameter set
    rather than a target column and are validated by
    :func:`_plan_parameter_set`.
    """
    name = source.name
    if not source.result:
        raise ManifestError(
            f"source {name!r}: kind 'result' is declared but there is no "
            "[source.result] block mapping its columns."
        )
    legal = schema.column_names(source.entity_type, "result")
    # The sharder fills these itself: the identity/provenance columns, and
    # parameter_set_hash, which it computes from the declared parameter_set.
    reserved = {
        "entity_hash",
        "entity_type",
        "observed_at",
        "ingest_batch_id",
        "parameter_set_hash",
    }

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
        if column in _PARAMETER_SET_KEYS:
            continue  # not a target column; see _plan_parameter_set
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


def constant_result_source(columns: Mapping[str, Derivation]) -> str | None:
    """Return the result ``source`` value if it is known at PLAN TIME.

    Only ``@const(<tool>/<version>)`` names a source that is the same on
    every row, so only that form can be checked against a tool-specific rule
    before any record is read. ``@lower(algorithm)``, a bare column name and
    every other derivation resolve per row, so they return ``None`` and the
    sharder makes the check itself (see
    :mod:`clearinghouse_parameters`).
    """
    derivation = columns.get("source")
    if derivation is None or derivation.name != "const":
        return None
    return derivation.args[0]


def _plan_parameter_set(
    source: Source, columns: Mapping[str, Derivation]
) -> tuple[dict[str, Any] | None, str | None, str | None, str | None]:
    """Validate a result source's parameter-set declaration.

    EXACTLY ONE of :data:`_PARAMETER_SET_KEYS` must be declared, and the two
    return shapes are disjoint:

    - STATIC ``parameter_set``: the set is a constant for the whole source, so
      it is validated and hashed ONCE, HERE. Returns
      ``(mapping, canonical_json, hash, None)``. Computing it at plan time
      rather than per row is the point -- a billion rows cost one
      canonicalisation and one sha256.
    - PER-ROW ``parameter_set_column``: the set is source DATA and is not
      knowable until a record is in hand, so nothing is hashed here. Returns
      ``(None, None, None, column_name)`` and the sharder runs the SAME
      validator per row (see
      :func:`clearinghouse_shard._build_row`). All that is checked now is
      that the value is a non-empty string naming a column.

    The TRANSYT result-boundary rule is applied here too, but ONLY for the
    static form AND only when the result ``source`` is a plan-time constant
    (see :func:`constant_result_source`) -- a manifest whose ``source`` is a
    derivation has no tool until a row is in hand, and the sharder applies
    the same shared validator per row for that case. The per-row form is
    therefore always validated per row, on both layers.

    Raises:
        ManifestError: Neither key is declared, both are, ``parameter_set``
            is not a table, ``parameter_set_column`` is not a non-empty
            string, or the static set fails the parameter-set rule -- the
            last carrying the originating
            :class:`~kbutillib.domains.identity.ParameterSetError` message
            VERBATIM (it is already prefixed with the manifest location, so
            the offending path inside the set is named exactly once).
    """
    name = source.name

    declared = [key for key in _PARAMETER_SET_KEYS if key in source.result]
    if not declared:
        raise ManifestError(
            f"source {name!r} [source.result]: one of 'parameter_set' or "
            "'parameter_set_column' is required. A result is keyed by "
            "(entity, result_type, source, parameter_set_hash), so every "
            "result source must say which parameter set it represents. "
            "Declare parameter_set = {} to say this is a DEFAULT run (the "
            "tool version's own defaults, nothing overridden); declare the "
            "parameters the caller set otherwise, e.g. parameter_set = "
            '{ taxonomy_id = "562" }; or declare parameter_set_column = '
            '"<column>" when each RECORD carries its own set.'
        )
    if len(declared) > 1:
        raise ManifestError(
            f"source {name!r} [source.result]: declare EXACTLY ONE of "
            f"{', '.join(repr(k) for k in _PARAMETER_SET_KEYS)}, not both. "
            "They are alternatives, not layers: 'parameter_set' says the set "
            "is one constant for the whole source and is hashed once at plan "
            "time, 'parameter_set_column' says each record carries its own "
            "and is hashed per row. A manifest declaring both leaves it "
            "undefined which hash a result row is keyed by."
        )

    key = declared[0]
    if key == PARAMETER_SET_COLUMN_KEY:
        column = source.result[key]
        if not isinstance(column, str) or not column.strip():
            raise ManifestError(
                f"source {name!r} [source.result].{PARAMETER_SET_COLUMN_KEY} "
                "must be a non-empty string naming a SOURCE column whose "
                f"value is this row's parameter set, got "
                f"{type(column).__name__} {column!r}. It is a column NAME, "
                "not a @derivation and not the set itself."
            )
        return (None, None, None, column.strip())

    where = f"source {name!r} [source.result].parameter_set"
    params = source.result[key]
    if not isinstance(params, dict):
        raise ManifestError(
            f"{where}: must be an inline TOML table, got "
            f"{type(params).__name__}. It is DATA, not a @derivation: write "
            'parameter_set = {} or parameter_set = { taxonomy_id = "562" }. '
            "To take the set from a column instead, declare "
            f"{PARAMETER_SET_COLUMN_KEY} = \"<column>\"."
        )

    try:
        canonical, digest = cp.result_parameter_set_identity(
            params, source=constant_result_source(columns), where=where
        )
    except ParameterSetError as exc:
        raise ManifestError(str(exc)) from exc
    return dict(params), canonical, digest, None
