"""Deterministic identity derivation for run records.

The whole replace-never-duplicate guarantee rests on two *different* producers
hashing byte-identical input for one logical analysis, so the derivation lives
in one shared helper here rather than in each producer (S18). Identity is split
into two tiers:

  * ``analysis_id`` GROUPS every run of one logical analysis. It is derived from
    the kind, the subject and the caller-supplied ``significant_params`` — the
    parameters that change the result. Two runs of the same method against
    different reference databases are different analyses and must not collide.
  * ``record_id`` is the per-RUN identity, derived from ``analysis_id`` and a
    caller-supplied ``run_uid``. A re-run is a new ``run_uid`` and therefore a
    new ``record_id`` (a new dated row); a retry reuses the ``run_uid`` and
    therefore replaces.

``canonical_json`` is load-bearing: RFC 8259, UTF-8, keys sorted by code point,
compact separators, no whitespace, strings NFC-normalised. Float values in
``significant_params`` are rejected outright (``bad_float_param``) — pinning a
float *format* does not stop ``0.1 + 0.2`` and cross-language parsers differing
in the last bit while every producer believes it complies, so a producer with a
float parameter must format it to a string itself.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any

from .exceptions import RecordValidationError

# The NUL separator between hashed fields (U+0000), per S18.
_NUL = "\x00"

# Display truncation length for record_id / analysis_id — DISPLAY ONLY, never
# for identity.
DISPLAY_ID_LENGTH = 32


def _nfc(text: str) -> str:
    """NFC-normalise a string, per the canonical_json contract."""
    return unicodedata.normalize("NFC", text)


def _canonicalize(value: Any) -> Any:
    """Recursively validate and normalise a value for canonical JSON.

    Permitted leaf types are ``str`` (NFC-normalised), ``int``, ``bool`` and
    ``None``; containers are ``list`` and ``dict`` of permitted values. A
    ``float`` anywhere raises :class:`RecordValidationError` with code
    ``bad_float_param`` — see the module docstring for why a format is not
    enough. ``bool`` is checked before ``int`` because ``bool`` is an ``int``
    subclass in Python.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        raise RecordValidationError(
            "bad_float_param",
            "float values are not permitted in significant_params; "
            "format the value to a string with the precision that matters",
        )
    if value is None or isinstance(value, int):
        return value
    if isinstance(value, str):
        return _nfc(value)
    if isinstance(value, list):
        return [_canonicalize(item) for item in value]
    if isinstance(value, dict):
        return {_nfc(str(k)): _canonicalize(v) for k, v in value.items()}
    raise RecordValidationError(
        "bad_float_param",
        f"unsupported type in significant_params: {type(value).__name__}",
    )


def canonical_json(value: Any) -> str:
    """Serialise *value* to canonical JSON (S18).

    Keys are sorted by code point, separators are ``(',', ':')`` with no
    whitespace, output is UTF-8, and strings are NFC-normalised. Float values
    anywhere in the structure raise ``RecordValidationError(bad_float_param)``.
    """
    canonical = _canonicalize(value)
    return json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def derive_analysis_id(kind: str, subject: str, significant_params: Any) -> str:
    """Derive the analysis_id that GROUPS every run of one logical analysis.

    ``analysis_id = sha256( lower(kind) + NUL + lower(subject) + NUL +
    canonical_json(significant_params) ).hexdigest()``.

    ``kind`` and ``subject`` are lower-cased for identity only; the record still
    stores ``subject`` verbatim. ``significant_params`` is caller-supplied per
    kind — this module holds no per-kind map (that would make it domain-aware).
    """
    payload = (
        _nfc(kind).lower()
        + _NUL
        + _nfc(subject).lower()
        + _NUL
        + canonical_json(significant_params)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def derive_record_id(analysis_id: str, run_uid: str) -> str:
    """Derive the per-run record_id from an analysis_id and a run_uid.

    ``record_id = sha256( analysis_id + NUL + run_uid ).hexdigest()``. A re-run
    supplies a new ``run_uid`` and thus a new ``record_id``; a retry reuses the
    ``run_uid`` and thus replaces.
    """
    payload = analysis_id + _NUL + run_uid
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def display_id(full_id: str) -> str:
    """Truncate a full hex id to :data:`DISPLAY_ID_LENGTH` chars for DISPLAY ONLY.

    Never use the result for identity or as a key — it is a UI convenience.
    """
    return full_id[:DISPLAY_ID_LENGTH]
