"""Canonical parameter sets for KBase Clearinghouse results.

A clearinghouse result is keyed by the entity it describes, the tool that
produced it, the tool version — and a **parameter-set hash**, so the same
protein run through the same tool version with different parameters (a
different threshold, TRANSYT's required NCBI taxonomy id) is a distinct
result rather than one run silently overwriting the other.

The parameter-set rule
----------------------
A *parameter set* is a mapping of **only** the parameters the caller set on
top of the tool version's defaults. A default run is therefore ``{}`` — the
empty mapping — and its hash is :data:`DEFAULT_PARAMETER_SET_HASH`.

**Resource parameters (threads, memory, paths, batch sizes, hostnames) must
never appear in a parameter set.** They change how fast the tool runs, not
what it computes, so including them would fragment one scientific result
across many hashes.

An explicitly passed default is *not* normalised away: if the caller names a
parameter, it is in the set, even when its value equals the tool default.
The writer records what the caller asked for; this module does not know the
tool's defaults and does not try to guess them.

Admissible values (validated at every depth):

* a ``str`` — but not one containing a lone surrogate (any code point in
  U+D800..U+DFFF), which has no UTF-8 encoding;
* an ``int`` with ``abs(value) <= 2**53 - 1``, so the value survives a
  round-trip through any IEEE-754 double-backed JSON reader;
* a ``bool`` — checked *before* ``int``, because ``True`` is an ``int`` in
  Python. A bool is a bool, never an int, and ``{"a": True}`` and
  ``{"a": 1}`` are different parameter sets with different hashes;
* a ``list`` of admissible values, whose **order is preserved** (it is part
  of the identity of the set);
* a ``dict`` passing these same rules.

``float`` is rejected: decimals are passed as strings (``"1e-5"``), because
no two JSON writers agree on how to render a float. ``None`` is rejected: an
unset parameter is an absent key, not a null one. Every key at every depth
must match ``^[a-z][a-z0-9_]*$``.

Violations raise :class:`ParameterSetError` (a ``ValueError`` subclass)
whose message names the path to the offending value, e.g.
``params['opts'][2]: float not allowed; pass decimals as strings``.

Fixed parameter keys
--------------------
So that two writers never name the same parameter differently, these keys
are fixed. A tool listed here must use exactly the key named.

================ =============== ========== ==================================
Tool             Key             Required?  Value
================ =============== ========== ==================================
TRANSYT          ``taxonomy_id`` required   NCBI taxonomy id as a decimal
                                            string with no leading zeros
                                            (e.g. ``"562"``). A TRANSYT
                                            result whose parameter set is
                                            ``{}`` is invalid.
================ =============== ========== ==================================

Public interface:
    ParameterSetError            — raised on any violation of the rule above.
    canonical_parameter_set(params) -> str
    parameter_set_hash(params) -> str
    DEFAULT_PARAMETER_SET_HASH   — hash of the default (empty) parameter set.

Pure standard library — the canonicalisation and hashing themselves are
delegated to :func:`~.standardizers.canonical_payload` and
:func:`~.standardizers.content_hash`, which this module does not
re-implement.
"""

from __future__ import annotations

__all__ = [
    "DEFAULT_PARAMETER_SET_HASH",
    "ParameterSetError",
    "canonical_parameter_set",
    "parameter_set_hash",
]

import re
from typing import Any

from .standardizers import canonical_payload, content_hash

#: sha256 of the two bytes ``{}`` — the hash of the default parameter set.
#: Asserted equal to ``parameter_set_hash({})`` by the test suite.
DEFAULT_PARAMETER_SET_HASH = (
    "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
)

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: Largest integer magnitude that survives an IEEE-754 double round-trip.
_MAX_SAFE_INT = 2**53 - 1

_ROOT_PATH = "params"


class ParameterSetError(ValueError):
    """A parameter set violated the parameter-set rule.

    The message always names the path to the offending value or key, e.g.
    ``params['opts'][2]: float not allowed; pass decimals as strings``.
    """


def _has_lone_surrogate(value: str) -> bool:
    """Return True if ``value`` contains a code point in U+D800..U+DFFF."""
    return any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def _validate_value(value: Any, path: str) -> None:
    """Validate one value at ``path``, recursing into lists and mappings."""
    # bool before int: True is an int in Python, and a bool is never an int here.
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INT:
            raise ParameterSetError(
                f"{path}: int magnitude exceeds 2**53 - 1 ({_MAX_SAFE_INT}); "
                f"pass large numbers as strings"
            )
        return
    if isinstance(value, float):
        raise ParameterSetError(
            f"{path}: float not allowed; pass decimals as strings"
        )
    if isinstance(value, str):
        if _has_lone_surrogate(value):
            raise ParameterSetError(
                f"{path}: string contains a lone surrogate (U+D800..U+DFFF); "
                f"it has no UTF-8 encoding"
            )
        return
    if isinstance(value, dict):
        _validate_mapping(value, path)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_value(item, f"{path}[{index}]")
        return
    if value is None:
        raise ParameterSetError(
            f"{path}: None not allowed; an unset parameter is an absent key"
        )
    raise ParameterSetError(
        f"{path}: {type(value).__name__} not allowed; "
        f"values must be str, int, bool, list or mapping"
    )


def _validate_mapping(mapping: dict[Any, Any], path: str) -> None:
    """Validate every key and value of ``mapping`` at ``path``."""
    for key, value in mapping.items():
        if not isinstance(key, str):
            raise ParameterSetError(
                f"{path}[{key!r}]: invalid key; keys must be strings matching "
                f"{_KEY_RE.pattern}"
            )
        if not _KEY_RE.match(key):
            raise ParameterSetError(
                f"{path}[{key!r}]: invalid key; keys must match {_KEY_RE.pattern}"
            )
        _validate_value(value, f"{path}[{key!r}]")


def _validate_parameter_set(params: Any) -> None:
    """Validate a whole parameter set, raising ParameterSetError if invalid."""
    if not isinstance(params, dict):
        raise ParameterSetError(
            f"{_ROOT_PATH}: mapping required, got {type(params).__name__}"
        )
    _validate_mapping(params, _ROOT_PATH)


def canonical_parameter_set(params: Any) -> str:
    """Return the canonical JSON text of the parameter set ``params``.

    Keys are sorted at every depth and no insignificant whitespace is
    emitted, so two logically-equal parameter sets always produce identical
    text. List order is preserved. Non-ASCII characters are emitted
    literally (the text is UTF-8, not escaped ASCII).

    Raises:
        ParameterSetError: if ``params`` violates the parameter-set rule;
            the message names the path to the offending value.
    """
    _validate_parameter_set(params)
    return canonical_payload(params).decode("utf-8")


def parameter_set_hash(params: Any) -> str:
    """Return the sha256 hex digest of ``canonical_parameter_set(params)``.

    The digest is lowercase hex and 64 characters long. The default (empty)
    parameter set hashes to :data:`DEFAULT_PARAMETER_SET_HASH`.

    Raises:
        ParameterSetError: if ``params`` violates the parameter-set rule;
            the message names the path to the offending value.
    """
    _validate_parameter_set(params)
    return content_hash(params)
