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

Today that is exactly one tool, TRANSYT, whose results are meaningless
without the NCBI taxonomy id they were computed against -- so a TRANSYT
result whose parameter set is ``{}`` is not a "default run", it is an
unidentifiable one. Every other tool gets the generic validation only; this
module never invents a requirement for a tool that has not declared one.

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

Pure standard library plus :mod:`kbutillib.domains.identity`: no pod, no
network, no I/O.
"""

from __future__ import annotations

__all__ = [
    "TRANSYT_TAXONOMY_ID_PATTERN",
    "TRANSYT_TOOLS",
    "result_parameter_set_identity",
    "tool_of_source",
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
    tool-specific layer via :func:`validate_transyt_parameter_set`.

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
    validate_transyt_parameter_set(params, source=source, where=where)
    return canonical, parameter_set_hash(params)
