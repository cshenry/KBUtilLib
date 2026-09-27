"""Canonical entity standardization and hashing for the KBDL Clearinghouse.

This module is the single source of truth for entity identity across the
platform: given a raw representation of a biological entity (a function
description, a protein or DNA sequence, a genome assembly, or an ontology
term identifier), it produces a *canonical form* and a stable sha256 hash
of that canonical form. GAA and KBDL delegate to this module rather than
each re-implementing their own standardization rules, so its behavior must
reproduce GAA's existing, already-in-production rules exactly — this
module transcribes those rules; it does not "improve" on them.

Public interface (kept intentionally small and stable):
    STANDARDIZER_VERSION   — version tag for the rules encoded here.
    standardize(entity_type, raw) -> str
    entity_hash(entity_type, raw) -> str
    parse_fasta_contigs(source) -> list[str]
    genome_hash_from_fasta(source) -> str
    canonical_payload(obj) -> bytes
    content_hash(obj) -> str

Supported ``entity_type`` values: ``"function"``, ``"protein"``,
``"gene"``, ``"genome"``, ``"ontology_term"``. See each
``_standardize_*`` helper below for the exact rule it encodes.

Pure standard library — no third-party dependencies (hashlib, json, re,
unicodedata only), so this module can be imported anywhere in the
platform without pulling in heavy optional deps.
"""

from __future__ import annotations

__all__ = [
    "STANDARDIZER_VERSION",
    "standardize",
    "entity_hash",
    "parse_fasta_contigs",
    "genome_hash_from_fasta",
    "canonical_payload",
    "content_hash",
]

import hashlib
import json
import re
import unicodedata
from typing import Any, Iterable

STANDARDIZER_VERSION = "1.1"

_SUPPORTED_ENTITY_TYPES = (
    "function",
    "protein",
    "gene",
    "genome",
    "ontology_term",
)

_WHITESPACE_RUN_RE = re.compile(r"\s+")


def _standardize_function(raw: str) -> str:
    """Standardize a free-text function description.

    Rules (GAA-equivalent, transcribed verbatim):
        (a) Unicode NFC normalization.
        (b) Strip leading and trailing whitespace.
        (c) Collapse every internal run of whitespace to a single ASCII space.
        (d) Lowercase ASCII letters ONLY (A-Z -> a-z); non-ASCII letters are
            left unchanged — this deliberately does NOT use ``str.lower()``,
            which would also fold non-ASCII letters.
        (e) Strip a SINGLE trailing period if present (so ``"x.."`` ->
            ``"x."``, and ``"x."`` -> ``"x"``).
        (f) Keep all other punctuation as-is — no stemming, no synonym
            expansion.
    """
    text = unicodedata.normalize("NFC", raw)
    text = text.strip()
    text = _WHITESPACE_RUN_RE.sub(" ", text)
    text = "".join(
        chr(ord(ch) + 32) if "A" <= ch <= "Z" else ch for ch in text
    )
    if text.endswith("."):
        text = text[:-1]
    return text


def _clean_sequence_letters(raw: str) -> str:
    """Strip FASTA header lines and all whitespace/newlines, then uppercase.

    Shared by the protein and gene rules: both strip any line beginning
    with ``>`` and all whitespace, then uppercase the remaining residue
    letters. Neither rule filters or rejects characters based on an
    expected alphabet — ambiguity codes, unexpected letters, and (for
    protein) a trailing ``*`` all survive untouched other than the case
    fold.
    """
    kept_lines = (
        line for line in raw.splitlines() if not line.startswith(">")
    )
    joined = "".join(kept_lines)
    letters_only = _WHITESPACE_RUN_RE.sub("", joined)
    return letters_only.upper()


def _standardize_protein(raw: str) -> str:
    """Standardize a protein sequence.

    Strips FASTA header lines and all whitespace/newlines, then uppercases.
    IUPAC ambiguity codes (B, Z, J, X, U, O) are PRESERVED after
    uppercasing. A trailing ``*`` (stop codon marker) is NOT stripped. I and
    L are never normalized into one another.
    """
    return _clean_sequence_letters(raw)


def _standardize_gene(raw: str) -> str:
    """Standardize a DNA sequence.

    Strips FASTA header lines and all whitespace/newlines, then uppercases.
    Characters outside ACGTN are PRESERVED (this module does not reject or
    filter them). No reverse-complement normalization is applied.
    """
    return _clean_sequence_letters(raw)


def _standardize_genome(raw: Iterable[str]) -> str:
    """Standardize a genome assembly given as an iterable of contig strings.

    Each contig is canonicalized with the DNA rule; contigs that are EMPTY
    after cleaning are DROPPED (a zero-length record cannot contribute to a
    genome's identity, and dropping it makes an assembly's hash independent
    of stray empty records); the remaining canonical contigs are then
    SORTED, and joined with the ASCII pipe character ``|``. The canonical
    form for a genome may be large; callers are not required to store it —
    only ``entity_hash`` of it need be kept.
    """
    canonical_contigs = [_standardize_gene(contig) for contig in raw]
    canonical_contigs = [contig for contig in canonical_contigs if contig]
    canonical_contigs.sort()
    return "|".join(canonical_contigs)


def parse_fasta_contigs(source: Any) -> list[str]:
    """Parse FASTA into a list of RAW contig sequence strings, in file order.

    This does NOT standardize and does NOT sort — that stays the job of
    ``standardize("genome", ...)``. It only splits a FASTA source into one
    string per ``>`` record, in the order the records appear.

    Args:
        source: A ``str``, ``bytes``, or a readable text or binary stream
            (anything with a ``.read()``). Bytes are decoded as UTF-8.

    Returns:
        A list of raw contig sequence strings, one element per ``>`` record.
        Header lines (``>``) are stripped from each record; the sequence
        lines of a record are joined with newlines preserved so downstream
        cleaning (which strips all whitespace) is unaffected. Text before
        the first ``>`` is not a record and is ignored. A source containing
        no ``>`` at all is treated as a single headerless record (one
        element), so a bare contig file keeps working.
    """
    if hasattr(source, "read"):
        source = source.read()
    if isinstance(source, (bytes, bytearray)):
        source = bytes(source).decode("utf-8")

    lines = source.splitlines()

    if not any(line.startswith(">") for line in lines):
        # No header anywhere: the whole thing is one headerless record.
        return ["\n".join(lines)]

    contigs: list[str] = []
    current: list[str] | None = None
    for line in lines:
        if line.startswith(">"):
            if current is not None:
                contigs.append("\n".join(current))
            current = []
        elif current is not None:
            # Sequence line belonging to the current record; text before the
            # first '>' has current is None and is ignored.
            current.append(line)
    if current is not None:
        contigs.append("\n".join(current))
    return contigs


def genome_hash_from_fasta(source: Any) -> str:
    """Return the genome ``entity_hash`` for a FASTA ``source``.

    Composition of :func:`parse_fasta_contigs` and
    ``entity_hash("genome", ...)``. This is the function an archive reader
    calls when it has a FASTA blob rather than a pre-split list of contigs.
    """
    return entity_hash("genome", parse_fasta_contigs(source))


def _standardize_ontology_term(raw: str) -> str:
    """Standardize an ontology term identifier of the form ``ns:term``.

    Strips surrounding whitespace. If a ``:`` is present, uppercases the
    namespace portion (everything before the first ``:``) and preserves
    the term body VERBATIM, including its case. Input with no ``:`` is
    standardized by whitespace-stripping alone.
    """
    text = raw.strip()
    if ":" not in text:
        return text
    namespace, _, term = text.partition(":")
    return f"{namespace.upper()}:{term}"


_STANDARDIZERS = {
    "function": _standardize_function,
    "protein": _standardize_protein,
    "gene": _standardize_gene,
    "genome": _standardize_genome,
    "ontology_term": _standardize_ontology_term,
}


def _guard_genome_raw(raw: Any) -> None:
    """Reject genome raw forms that would silently produce a wrong hash.

    Two failure modes, both measured in shipped code:

    1. A bare ``str`` or ``bytes``. A ``str`` is iterable, so the genome
       rule would iterate it CHARACTER BY CHARACTER — e.g.
       ``standardize("genome", "ACGTTTGCA")`` would return
       ``"A|A|C|C|G|G|T|T|T"`` instead of the intended single-contig form.
       A genome's raw form is an iterable of contigs; a single sequence
       must be passed as a one-element list.

    2. An element containing a ``>`` header line AFTER its first line. That
       means two or more FASTA records were packed into one string, which
       would be cleaned into a single concatenated contig — a different
       hash from passing the records as separate list elements, with no
       error. An element whose FIRST line is a ``>`` header is a single
       record with its header attached and is left alone.
    """
    if isinstance(raw, (str, bytes, bytearray)):
        raise ValueError(
            "A genome's raw form is an iterable of contigs, not a bare "
            "str/bytes; a str would be iterated character by character and "
            "produce a wrong hash. Pass a single sequence as a one-element "
            "list, e.g. standardize('genome', ['ACGT'])."
        )
    for index, element in enumerate(raw):
        lines = element.splitlines()
        if any(line.startswith(">") for line in lines[1:]):
            raise ValueError(
                f"genome contig element at index {index} contains a '>' "
                "header line after its first line: a multi-record FASTA was "
                "passed where a single contig was expected. Use "
                "genome_hash_from_fasta(source) to hash a multi-record FASTA."
            )


def standardize(entity_type: str, raw: Any) -> str:
    """Return the canonical string form of ``raw`` for the given entity type.

    Args:
        entity_type: One of ``"function"``, ``"protein"``, ``"gene"``,
            ``"genome"``, ``"ontology_term"``.
        raw: The raw representation. A ``str`` for every entity type except
            ``"genome"``, which takes an iterable of contig ``str`` values.

    Returns:
        The canonical string form, per the rules documented on the
        corresponding ``_standardize_*`` helper.

    Raises:
        ValueError: If ``entity_type`` is not one of the supported types.
    """
    try:
        fn = _STANDARDIZERS[entity_type]
    except KeyError:
        supported = ", ".join(_SUPPORTED_ENTITY_TYPES)
        raise ValueError(
            f"Unsupported entity_type {entity_type!r}; "
            f"supported types are: {supported}"
        ) from None
    if entity_type == "genome":
        _guard_genome_raw(raw)
    return fn(raw)


def entity_hash(entity_type: str, raw: Any) -> str:
    """Return the sha256 hex digest of ``standardize(entity_type, raw)``.

    The canonical string's UTF-8 bytes are hashed directly — no additional
    payload wrapping.
    """
    canonical = standardize(entity_type, raw)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_payload(obj: Any) -> bytes:
    """Return a deterministic JSON encoding of ``obj`` as UTF-8 bytes.

    Keys are sorted and no insignificant whitespace is emitted, so two
    logically-equal payloads (e.g. dicts differing only in key insertion
    order) always produce identical bytes.
    """
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def content_hash(obj: Any) -> str:
    """Return the sha256 hex digest of ``canonical_payload(obj)``."""
    return hashlib.sha256(canonical_payload(obj)).hexdigest()
