"""The ONE result-boundary parameter-set validator every writer shares.

Every clearinghouse write path that produces a ``<type>_result`` row must
carry a parameter set and must validate it identically. There are three such
paths -- manifest planning
(:mod:`~kbutillib.domains.kbase.berdl.clearinghouse_manifest`), the bronze
sharder (:mod:`~kbutillib.domains.kbase.berdl.clearinghouse_shard`) and the
in-pod ``register`` verb
(:mod:`~kbutillib.domains.kbase.berdl.clearinghouse_capability`) -- and if
each carried its own copy of the rule they would drift, which is exactly how
a tool-specific requirement ends up enforced on one path and silently absent
on another. So the rule lives HERE, once, and all three call it.

TWO LAYERS, and only the second one is this module's own. The GENERIC
parameter-set rule (admissible value types, the ``^[a-z][a-z0-9_]*$`` key
alphabet, floats and ``None`` rejected, the canonical JSON text and the
sha256 hash) belongs to
:mod:`kbutillib.domains.identity.parameter_sets` and is NOT re-implemented
here -- this module calls
:func:`~kbutillib.domains.identity.canonical_parameter_set` and
:func:`~kbutillib.domains.identity.parameter_set_hash`. What this module adds
is the per-TOOL layer: requirements that apply to one tool's results and to
no other tool's.

Today that is exactly two tools, and both declared their requirement for the
same reason: a ``{}`` parameter set would not be a "default run" but an
unidentifiable one.

- TRANSYT, whose results are meaningless without the NCBI taxonomy id they
  were computed against (:func:`validate_transyt_parameter_set`).
- ``council``, the AI council. A council result is the verdict of ONE bench
  (which models sat in which roles) answering ONE query version against ONE
  retrieval mapping, so its parameter set is
  ``{query, bench_fingerprint, mapping_hash}``
  (:func:`validate_council_parameter_set`). ``{}`` there would collapse every
  future run into one slot: 47,341 functions would read as already answered
  no matter which bench answered them, and "un-run at THIS version" -- the
  query stage 1 of the ``function_to_go`` pipeline is built on -- would
  always return nothing.

Every other tool gets the generic validation only; this module never invents a
requirement for a tool that has not declared one.

THE TOOL IS READ OFF ``source``, not passed separately. ``source`` is the
``"<tool>/<version>"`` partition value of the ``<type>_result`` tables, so
the tool component is the part before the FIRST ``'/'``, compared
case-insensitively (``@lower`` is recommended on a manifest's ``source``
column but not guaranteed, and a tool-specific rule that a capitalised
``TRANSYT/1.0`` escaped would be worse than no rule).

WHERE EACH CALLER CALLS IT:

- Manifest planning calls it once, at plan time, when the result ``source``
  is a plan-time CONSTANT (``@const(transyt/1.0)``) -- the tool is known
  before a single row is read, so a bad manifest fails before any work.
- The sharder calls it PER ROW when the result ``source`` is a derivation
  (``@lower(algorithm)``, a bare column): the tool is only known once the
  row is in hand, so the plan-time check cannot have been made.
- ``register()`` calls it per row, because each row carries its own
  ``source``.

EVERY CALLER GOES THROUGH :func:`validate_tool_parameter_set`, never through
one tool's validator by name. Each rule is a no-op for a ``source`` naming
another tool, so there is nothing to dispatch on -- which also means a caller
that picked one rule would enforce it and silently skip the rest. That is the
drift this module exists to prevent, so the per-tool layer has exactly one
entry point and a tool declared later needs no caller changed.

Pure standard library plus :mod:`kbutillib.domains.identity`: no pod, no
network, no I/O.
"""

from __future__ import annotations

__all__ = [
    "COUNCIL_FINGERPRINT_PATTERN",
    "COUNCIL_PARAMETER_KEYS",
    "COUNCIL_QUERY_PATTERN",
    "COUNCIL_REFUSED_KEYS",
    "COUNCIL_TOOLS",
    "TRANSYT_TAXONOMY_ID_PATTERN",
    "TRANSYT_TOOLS",
    "result_parameter_set_identity",
    "tool_of_source",
    "validate_council_parameter_set",
    "validate_tool_parameter_set",
    "validate_transyt_parameter_set",
]

import re
from typing import Any, Mapping

from ...identity import (
    ParameterSetError,
    canonical_parameter_set,
    parameter_set_hash,
)

#: The tool components of ``source`` that identify a TRANSYT result, compared
#: case-insensitively. ``transyt_local`` is the same tool run from a local
#: install rather than the service, and carries the same requirement.
TRANSYT_TOOLS: frozenset[str] = frozenset({"transyt", "transyt_local"})

#: The fixed parameter key TRANSYT results must carry, and the pattern its
#: value must match: an NCBI taxonomy id as a DECIMAL STRING with no leading
#: zeros (``"562"``). A string, not an int, so the stored parameter set is
#: byte-identical to what the caller asked for and cannot be reformatted by a
#: JSON reader on the way back out.
TRANSYT_TAXONOMY_ID_KEY = "taxonomy_id"
TRANSYT_TAXONOMY_ID_PATTERN = r"^[1-9][0-9]*$"

_TRANSYT_TAXONOMY_ID_RE = re.compile(TRANSYT_TAXONOMY_ID_PATTERN)

#: The tool components of ``source`` that identify an AI-COUNCIL result,
#: compared case-insensitively. A council run publishes under
#: ``"council/<query_name>@<version>"``
#: (:data:`~kbutillib.domains.genome.ontology_mapping.ontology_mapping_utils.COUNCIL_SOURCE_PREFIX`),
#: so the tool component is the bare word ``council``.
COUNCIL_TOOLS: frozenset[str] = frozenset({"council"})

#: The three keys a council result's parameter set must carry, and nothing
#: here makes any of them optional. Together they are the whole answer to "is
#: this the same experiment": WHICH question (``query``), asked of WHICH bench
#: (``bench_fingerprint``), against WHICH retrieval mapping
#: (``mapping_hash``).
COUNCIL_PARAMETER_KEYS: tuple[str, ...] = (
    "query",
    "bench_fingerprint",
    "mapping_hash",
)

#: ``query`` is ``"<name>@<version>"`` and the VERSION IS REQUIRED. A bare
#: ``"go_mf"`` would leave the version to be guessed, and the version is part
#: of every prompt hash as well as of this partition -- a run quoted against
#: one spec and published against another is exactly what naming it prevents.
COUNCIL_QUERY_PATTERN = r"^[a-z][a-z0-9_]*@[0-9A-Za-z][0-9A-Za-z._-]*$"

#: ``bench_fingerprint`` and ``mapping_hash`` are both sha256 hex digests
#: (:func:`~kbutillib.domains.ai.council.types.canonical_sha256` and
#: ``OntologyMappingUtils.mapping_version()`` respectively), so the legal form
#: is 64 lowercase hex characters. Checking the FORM is all that can be
#: checked here -- this module cannot know which roster produced a digest --
#: but it does catch the two mistakes that matter: a truncated digest, and an
#: unsubstituted template marker left in a rendered manifest.
COUNCIL_FINGERPRINT_PATTERN = r"^[0-9a-f]{64}$"

#: Keys REFUSED in a council parameter set, each because it is provenance and
#: not identity. ``roster_fingerprint``
#: (:meth:`~kbutillib.domains.ai.council.types.Roster.fingerprint`) covers
#: ``endpoint_kind``, the route spelling ``model``, ``max_output_tokens``,
#: ``temperature`` and ``extra``, so moving one seat from the codex CLI to MAG
#: moves it -- and a partition key that moves on a pure routing change would
#: re-partition all 47,341 clearinghouse functions and re-charge ~199M tokens
#: for a cost optimisation. The per-seat ``route_fingerprint`` is narrower
#: still: its own docstring puts it as "where did this come from", not "is
#: this the same question".
#:
#: Both ARE recorded -- on the plan, on every job and call row, and in the
#: published result PAYLOAD. They are simply not keys, and a parameter set
#: that names one is refused HERE rather than discovered after a re-run.
COUNCIL_REFUSED_KEYS: frozenset[str] = frozenset(
    {"roster_fingerprint", "route_fingerprint", "route_fingerprints"}
)

_COUNCIL_QUERY_RE = re.compile(COUNCIL_QUERY_PATTERN)
_COUNCIL_FINGERPRINT_RE = re.compile(COUNCIL_FINGERPRINT_PATTERN)


def tool_of_source(source: Any) -> str:
    """Return the lowercased TOOL component of a result ``source`` value.

    ``source`` is ``"<tool>/<version>"``, so the tool is the text before the
    FIRST ``'/'``; a ``source`` with no ``'/'`` is treated as all tool and no
    version. The result is lowercased, so a tool-specific rule cannot be
    escaped by capitalisation. A non-string ``source`` (``None``, a number)
    yields ``""`` -- it names no tool, so no tool-specific rule applies and
    the generic validation is all that runs.
    """
    if not isinstance(source, str):
        return ""
    return source.split("/", 1)[0].strip().lower()


def validate_transyt_parameter_set(
    params: Mapping[str, Any],
    *,
    source: Any,
    where: str = "parameter_set",
) -> None:
    """Apply the TRANSYT result-boundary rule to ``params``, or raise.

    This is THE shared TRANSYT validator: manifest planning, the sharder and
    ``register()`` all call this one function rather than each re-deriving
    the rule. For a ``source`` whose tool is not in :data:`TRANSYT_TOOLS`
    this is a no-op -- the generic parameter-set rule is the whole contract
    for every other tool.

    For a TRANSYT ``source``, ``params`` must carry
    ``taxonomy_id`` as a ``str`` matching
    :data:`TRANSYT_TAXONOMY_ID_PATTERN`. A TRANSYT result whose parameter set
    is ``{}`` is therefore REFUSED: there is no such thing as a default
    TRANSYT run, because the taxonomy id is not a tuning knob but part of
    what the result means.

    Args:
        params: The parameter set, already known to be a mapping (the generic
            rule checks that; see :func:`result_parameter_set_identity`).
        source: The result ``source`` value, ``"<tool>/<version>"``.
        where: Location prefix for error messages, e.g.
            ``"source 'transyt-run' [source.result].parameter_set"``.

    Raises:
        ParameterSetError: The source is TRANSYT and ``taxonomy_id`` is
            missing, not a string, or not a decimal id with no leading zeros.
    """
    tool = tool_of_source(source)
    if tool not in TRANSYT_TOOLS:
        return
    if TRANSYT_TAXONOMY_ID_KEY not in params:
        raise ParameterSetError(
            f"{where}: source {source!r} is TRANSYT, which requires "
            f"{TRANSYT_TAXONOMY_ID_KEY!r} in its parameter set. A TRANSYT "
            "result is computed against one NCBI taxonomy and is "
            "unidentifiable without it, so {} is not a default run -- it is "
            "an invalid one. Declare e.g. "
            f'{{{TRANSYT_TAXONOMY_ID_KEY} = "562"}}.'
        )
    value = params[TRANSYT_TAXONOMY_ID_KEY]
    if not isinstance(value, str) or isinstance(value, bool):
        raise ParameterSetError(
            f"{where}[{TRANSYT_TAXONOMY_ID_KEY!r}]: source {source!r} is "
            f"TRANSYT; {TRANSYT_TAXONOMY_ID_KEY} must be a STRING matching "
            f"{TRANSYT_TAXONOMY_ID_PATTERN}, got {type(value).__name__} "
            f"{value!r}. Pass the id as a string so the stored parameter set "
            "is byte-identical to what was asked for."
        )
    if not _TRANSYT_TAXONOMY_ID_RE.match(value):
        raise ParameterSetError(
            f"{where}[{TRANSYT_TAXONOMY_ID_KEY!r}]: source {source!r} is "
            f"TRANSYT; {TRANSYT_TAXONOMY_ID_KEY} {value!r} is not an NCBI "
            f"taxonomy id -- it must match {TRANSYT_TAXONOMY_ID_PATTERN} (a "
            'decimal id with no leading zeros, e.g. "562").'
        )


def validate_council_parameter_set(
    params: Mapping[str, Any],
    *,
    source: Any,
    where: str = "parameter_set",
) -> None:
    """Apply the AI-COUNCIL result-boundary rule to ``params``, or raise.

    THE SHARED council validator, for the same reason
    :func:`validate_transyt_parameter_set` is shared: manifest planning, the
    sharder and ``register()`` all call this one function. For a ``source``
    whose tool is not in :data:`COUNCIL_TOOLS` it is a no-op.

    A council result must carry all of :data:`COUNCIL_PARAMETER_KEYS` and
    none of :data:`COUNCIL_REFUSED_KEYS`. The REFUSED keys are checked first:
    a set naming ``roster_fingerprint`` where ``bench_fingerprint`` belongs
    fails both checks, and "you used provenance as a key" names the actual
    mistake where "bench_fingerprint is absent" only describes its shadow.
    ``{}`` IS REFUSED, and
    that refusal is the point: ``<T>_result`` is keyed on
    ``parameter_set_hash``, so a council source declaring ``{}`` would land
    every run in ONE slot at
    :data:`~kbutillib.domains.identity.DEFAULT_PARAMETER_SET_HASH`. Stage 1 of
    the ``function_to_go`` pipeline defines "un-run" as the absence of a
    current-state row at THIS run's hash; collapse the hash and the 47,341
    functions in the clearinghouse read as answered by whichever bench got
    there first, forever.

    Args:
        params: The parameter set, already known to be a mapping (the generic
            rule checks that; see :func:`result_parameter_set_identity`).
        source: The result ``source`` value, ``"council/<name>@<version>"``.
        where: Location prefix for error messages.

    Raises:
        ParameterSetError: A required key is missing or malformed, or a
            provenance-only key was used as part of the partition.
    """
    tool = tool_of_source(source)
    if tool not in COUNCIL_TOOLS:
        return

    refused = sorted(set(params) & COUNCIL_REFUSED_KEYS)
    if refused:
        raise ParameterSetError(
            f"{where}: source {source!r} is an AI-council result; "
            f"{refused} may not appear in its parameter set. Those are "
            "PROVENANCE ('where did this come from', 'was this configured "
            "identically'), not identity ('is this the same experiment'), and "
            "they move when a seat is re-routed to another gateway -- which "
            "would re-partition the whole corpus for a pure cost "
            "optimisation. Use 'bench_fingerprint' as the key and publish the "
            "rest in the result payload."
        )

    missing = [key for key in COUNCIL_PARAMETER_KEYS if key not in params]
    if missing:
        raise ParameterSetError(
            f"{where}: source {source!r} is an AI-council result, whose "
            f"parameter set must carry {list(COUNCIL_PARAMETER_KEYS)}; "
            f"{missing} {'is' if len(missing) == 1 else 'are'} absent. A "
            "council verdict is one bench's answer to one query version "
            "against one retrieval mapping, so {} is not a default run -- it "
            "is an unidentifiable one, and it would collapse every future run "
            "into a single parameter_set_hash slot."
        )

    query = params["query"]
    if not isinstance(query, str) or isinstance(query, bool):
        raise ParameterSetError(
            f"{where}['query']: source {source!r} is an AI-council result; "
            f"'query' must be a STRING '<name>@<version>', got "
            f"{type(query).__name__} {query!r}."
        )
    if not _COUNCIL_QUERY_RE.match(query):
        raise ParameterSetError(
            f"{where}['query']: source {source!r} is an AI-council result; "
            f"'query' {query!r} is not '<name>@<version>' -- it must match "
            f"{COUNCIL_QUERY_PATTERN}. The VERSION is required: it is part of "
            "this partition and of every prompt hash, so a query that left it "
            "to be guessed could be quoted against one spec and published "
            'against another. Write e.g. "go_mf@1".'
        )

    # ONE SUBSTITUTION, NOT TWO. The council's result ``source`` is
    # ``"council/<query>"`` and the parameter set's ``query`` is that same
    # ``<query>``, so a rendered manifest that substituted one and missed the
    # other would publish under a partition whose two halves disagree about
    # which query version answered. The shipped manifest preset renders both
    # from ``PLAN.json``'s single ``query`` field; this is the check that it
    # did.
    declared_query = str(source).split("/", 1)[1] if "/" in str(source) else ""
    if declared_query and declared_query != query:
        raise ParameterSetError(
            f"{where}['query']: source {source!r} names query "
            f"{declared_query!r} but the parameter set says {query!r}. The "
            "result 'source' partition and the parameter set must agree on "
            "which query version produced the row -- they are two spellings "
            "of one fact, and a manifest that substituted one and missed the "
            "other is the way they come apart."
        )

    for key in ("bench_fingerprint", "mapping_hash"):
        value = params[key]
        if not isinstance(value, str) or isinstance(value, bool):
            raise ParameterSetError(
                f"{where}[{key!r}]: source {source!r} is an AI-council "
                f"result; {key} must be a STRING sha256 digest matching "
                f"{COUNCIL_FINGERPRINT_PATTERN}, got "
                f"{type(value).__name__} {value!r}."
            )
        if not _COUNCIL_FINGERPRINT_RE.match(value):
            raise ParameterSetError(
                f"{where}[{key!r}]: source {source!r} is an AI-council "
                f"result; {key} {value!r} is not a sha256 digest -- it must "
                f"match {COUNCIL_FINGERPRINT_PATTERN} (64 LOWERCASE hex "
                "characters). A truncated digest and an unsubstituted "
                "template marker both fail here, which is cheaper than "
                "discovering either after 47,341 rows have landed under a "
                "hash nobody can reproduce."
            )


def validate_tool_parameter_set(
    params: Mapping[str, Any],
    *,
    source: Any,
    where: str = "parameter_set",
) -> None:
    """Apply EVERY tool-specific result-boundary rule, in one call.

    THE SINGLE ENTRY POINT for the per-tool layer, and the reason it exists is
    the drift this module's header warns about. Each rule is a no-op for a
    ``source`` naming another tool, so there is nothing to dispatch on and
    nothing for a caller to choose between -- which means a caller that
    reached for ``validate_transyt_parameter_set`` BY NAME would silently
    enforce one tool's rule and skip every other tool's. Call this instead,
    and a tool declared later is enforced on every path without touching any
    of them.

    :func:`result_parameter_set_identity` calls this after the generic rule;
    the bronze sharder calls it directly for a STATIC parameter set whose
    result ``source`` was a derivation (the tool was unknown at plan time, so
    the plan-time check could not have been made).

    Args:
        params: The parameter set, already known to be a mapping.
        source: The result ``source`` value, ``"<tool>/<version>"``.
        where: Location prefix for error messages.

    Raises:
        ParameterSetError: Whichever tool rule applies rejected ``params``.
    """
    validate_transyt_parameter_set(params, source=source, where=where)
    validate_council_parameter_set(params, source=source, where=where)


def result_parameter_set_identity(
    params: Any,
    *,
    source: Any,
    where: str = "parameter_set",
) -> tuple[str, str]:
    """Validate a result row's parameter set and return ``(canonical, hash)``.

    Runs BOTH layers in order -- the generic parameter-set rule first (so a
    non-mapping, a float, a bad key or a lone surrogate is reported by the
    rule that owns it, with its own path-naming message), then the
    tool-specific layer via :func:`validate_tool_parameter_set`, which runs
    every declared tool rule (each a no-op for a ``source`` naming another
    tool). Going through that one function rather than naming a tool's
    validator here is what keeps a tool declared later from being enforced on
    some write paths and not others.

    ``where`` prefixes the message of EITHER layer exactly once, so a caller
    can wrap the result in its own error type without re-prefixing: the
    generic rule's own ``params['evalue']`` path then reads as a sub-path of
    the location this function was called for.

    Args:
        params: The caller's parameter set (a mapping; anything else is
            refused by the generic rule).
        source: The result ``source`` value, used only to resolve the tool.
        where: Location prefix applied to both layers' messages.

    Returns:
        ``(canonical_json, parameter_set_hash)`` -- the canonical JSON text
        to store in the registry and the 64-char lowercase hex digest to
        store on the result row.

    Raises:
        ParameterSetError: Either layer rejected ``params``.
    """
    try:
        canonical = canonical_parameter_set(params)
    except ParameterSetError as exc:
        raise ParameterSetError(f"{where}: {exc}") from exc
    validate_tool_parameter_set(params, source=source, where=where)
    return canonical, parameter_set_hash(params)
