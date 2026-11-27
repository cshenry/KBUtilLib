"""Pure validation for the ``method`` table and the seven joining tables.

THE RULE (lw-1): no relationship between two entities is ever written into a
``<type>_result`` payload. A relationship is a ROW of one of the seven joining
tables (:data:`~kbutillib.domains.kbase.berdl.clearinghouse_schema.JOIN_TABLES`),
produced by a METHOD row (:data:`~...clearinghouse_schema.METHOD_TABLE`).

Everything here is transport-free: it validates and canonicalizes rows and
raises :class:`LinkRefusedError` (a ``ValueError``) naming the row index and the
rule. :class:`~kbutillib.domains.kbase.berdl.clearinghouse_capability.ClearinghouseCapability`
adds the reads that need the lake (committed conflicts, existing methods,
orphans) and the appends. Keeping the rules pure means the register verbs, the
delivery manifest and the producers all apply ONE definition.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from ...identity import ParameterSetError, canonical_parameter_set, parameter_set_hash
from . import clearinghouse_schema as schema

__all__ = [
    "LinkRefusedError",
    "canonical_metadata",
    "canonical_json_sha256",
    "validate_method_rows",
    "validate_link_rows",
    "metadata_conflict_key",
    "GENE_ID_PATTERN",
]

#: Columns a caller may never supply: the writer owns the clock.
SERVER_COLUMNS = ("observed_at", "ingest_batch_id")

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")
_CURIE = re.compile(r"^[^\s:]+:\S+$")

#: Namespaced gene ids: ``KBDL:<genome_hash first 16 hex>.<contig>.<cds>``,
#: ``CDM:<feature_id>``, ``PATRIC:<fig id>``.
GENE_ID_PATTERN = re.compile(
    r"^(KBDL:[0-9a-f]{16}\.[0-9]+\.[0-9]+|CDM:\S+|PATRIC:\S+)$"
)


class LinkRefusedError(ValueError):
    """A method or link row broke a rule. Nothing has been written."""


# -- canonical JSON ---------------------------------------------------------


def _no_floats(value: Any, *, path: str = "$") -> None:
    if isinstance(value, float):
        raise ValueError(
            f"a float at {path} is not allowed in canonical metadata; carry "
            "fractional numbers as decimal strings"
        )
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"non-string key {key!r} at {path}")
            _no_floats(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _no_floats(item, path=f"{path}[{index}]")


def canonical_metadata(value: Any) -> str:
    """Canonical JSON text: sorted keys, UTF-8 (not ASCII-escaped), compact,
    no floats; ``'{}'`` for an empty or absent mapping.

    Raises:
        ValueError: ``value`` holds a float or a non-string key.
    """
    if value is None:
        return "{}"
    _no_floats(value)
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )


def canonical_json_sha256(value: Any) -> str:
    """sha256 hex of the canonical JSON of ``value`` (used for link_keys)."""
    return hashlib.sha256(canonical_metadata(value).encode("utf-8")).hexdigest()


def _parse_strict(text: str) -> Any:
    """Parse JSON refusing floats, duplicate keys and NaN/Infinity."""

    def _pairs(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        seen: set[str] = set()
        for key, _v in pairs:
            if key in seen:
                raise ValueError(f"duplicate key {key!r}")
            seen.add(key)
        return dict(pairs)

    def _float(token: str) -> Any:
        raise ValueError(
            f"float {token} is not allowed; carry fractional numbers as "
            "decimal strings"
        )

    def _constant(token: str) -> Any:
        raise ValueError(f"{token} is not allowed")

    return json.loads(
        text,
        object_pairs_hook=_pairs,
        parse_float=_float,
        parse_constant=_constant,
    )


def _fail(kind: str, index: int, rule: str, row: Any = None) -> LinkRefusedError:
    detail = ""
    if isinstance(row, Mapping):
        shown = {
            k: row.get(k)
            for k in (
                "method_name",
                "subject_hash",
                "object_hash",
                "relation",
                "link_key",
            )
            if k in row
        }
        detail = f" (row: {shown})"
    return LinkRefusedError(
        f"{kind} row {index}: {rule}{detail}. Nothing has been written."
    )


# -- method rows ------------------------------------------------------------


def validate_method_rows(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate and canonicalize ``method`` rows; collapse identical ones.

    Returns ``(methods, registry_rows)``: the methods in first-seen order
    (columns: all of :func:`schema.method_column_names` except the server
    columns) and one parameter-set registry row (``parameter_set_hash``,
    ``canonical_json``) per distinct hash.

    Raises:
        LinkRefusedError: any rule below, naming the row index --
        not a mapping; a server column supplied; NULL/empty in any identity
        column; ``method_name`` not matching
        :data:`schema.METHOD_NAME_PATTERN` or over 128 characters;
        ``parameters_json`` not canonical JSON under the parameter-set rules;
        ``parameter_set_hash`` not equal to the hash recomputed from
        ``parameters_json``; a second row with the same ``method_name`` and
        any identity column different (within the call).
    """
    out: dict[str, dict[str, Any]] = {}
    registry: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise _fail("method", index, f"expected a mapping, got {type(row).__name__}")
        for column in SERVER_COLUMNS:
            if column in row:
                raise _fail(
                    "method", index,
                    f"caller-supplied {column!r} is refused; the writer generates it",
                    row,
                )
        unknown = set(row) - set(schema.method_column_names())
        if unknown:
            raise _fail("method", index, f"unknown column(s) {sorted(unknown)}", row)
        for column in (*schema.METHOD_IDENTITY_COLUMNS, "parameters_json"):
            value = row.get(column)
            if value is None:
                raise _fail("method", index, f"NULL {column} (identity columns are never NULL)", row)
            if not isinstance(value, str) or not value.strip():
                raise _fail("method", index, f"{column} must be a non-empty string", row)
        description = row.get("description")
        if not isinstance(description, str):
            raise _fail("method", index, "description must be a string (static text; never run counts)", row)
        name = row["method_name"]
        if len(name) > schema.METHOD_NAME_MAX_LENGTH or not schema.METHOD_NAME_PATTERN.match(name):
            raise _fail(
                "method", index,
                f"method_name {name!r} must match {schema.METHOD_NAME_PATTERN.pattern} "
                f"and be at most {schema.METHOD_NAME_MAX_LENGTH} characters "
                "(<algorithm>@<version>/<label>)",
                row,
            )
        text = row["parameters_json"]
        try:
            params = _parse_strict(text)
            if not isinstance(params, dict):
                raise ValueError("parameters_json must be a JSON object")
            canonical = canonical_parameter_set(params)
            digest = parameter_set_hash(params)
        except (ValueError, ParameterSetError) as exc:
            raise _fail("method", index, f"parameters_json is not valid under the parameter-set rules: {exc}", row) from exc
        if canonical != text:
            raise _fail(
                "method", index,
                f"parameters_json is not canonical (canonical form: {canonical!r})",
                row,
            )
        if row["parameter_set_hash"] != digest:
            raise _fail(
                "method", index,
                f"parameter_set_hash {row['parameter_set_hash']!r} does not match "
                f"the hash recomputed from parameters_json ({digest})",
                row,
            )
        clean = {
            "method_name": name,
            "algorithm": row["algorithm"],
            "algorithm_version": row["algorithm_version"],
            "parameters_json": canonical,
            "parameter_set_hash": digest,
            "source_dataset": row["source_dataset"],
            "description": description,
        }
        prior = out.get(name)
        if prior is not None:
            if any(prior[c] != clean[c] for c in schema.METHOD_IDENTITY_COLUMNS):
                differing = sorted(
                    c for c in schema.METHOD_IDENTITY_COLUMNS if clean[c] != prior[c]
                )
                raise _fail(
                    "method", index,
                    f"conflicting redefinition of method {name!r}: {differing} "
                    "differ from an earlier row of this call (a new source "
                    "snapshot gets a new name)",
                    row,
                )
            continue
        out[name] = clean
        registry.setdefault(
            digest, {"parameter_set_hash": digest, "canonical_json": canonical}
        )
    return list(out.values()), list(registry.values())


# -- link rows --------------------------------------------------------------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_keys(kind: str, index: int, meta: Mapping[str, Any], keys: Sequence[str], row: Any) -> None:
    missing = [k for k in keys if k not in meta or meta[k] is None or meta[k] == ""]
    if missing:
        raise _fail(kind, index, f"missing required metadata key(s) {missing}", row)


def _check_metadata(table: str, index: int, row: Mapping[str, Any], meta: dict[str, Any]) -> None:
    kind = "link"
    relation = row["relation"]
    link_key = row["link_key"]
    if table == schema.FUNCTION_X_ONTOLOGY_TERM:
        _require_keys(kind, index, meta, ["basis"], row)
        basis = meta["basis"]
        if basis not in ("definition", "inferred"):
            raise _fail(kind, index, f"metadata.basis must be 'definition' or 'inferred', got {basis!r}", row)
        wanted = schema.FUNCTION_ONTOLOGY_BASIS[relation]
        if basis != wanted:
            raise _fail(
                kind, index,
                f"relation {relation!r} requires metadata.basis == {wanted!r}, got {basis!r}",
                row,
            )
        if basis == "definition":
            _require_keys(kind, index, meta, ["field"], row)
            if link_key != "":
                raise _fail(kind, index, "link_key must be '' for basis 'definition'", row)
        else:
            evidence = meta.get("evidence")
            if not isinstance(evidence, dict) or not evidence:
                raise _fail(kind, index, "metadata.evidence must be a non-empty object for basis 'inferred'", row)
            if not _HEX64.match(link_key):
                raise _fail(kind, index, "link_key must be the sha256 hex of the canonical source payload for basis 'inferred'", row)
    elif table == schema.ONTOLOGY_TERM_X_ONTOLOGY_TERM:
        _require_keys(kind, index, meta, ["source_predicate", "asserting_curie", "asserted_curie"], row)
        if not _HEX64.match(link_key):
            raise _fail(kind, index, "link_key must be the sha256 hex of the canonical JSON of the original directed source assertion", row)
    elif table == schema.FUNCTION_X_FUNCTION:
        _require_keys(kind, index, meta, ["via_term"], row)
        if link_key != meta["via_term"]:
            raise _fail(kind, index, "link_key must equal metadata.via_term (the via_term CURIE)", row)
    elif table == schema.PROTEIN_X_FUNCTION:
        if "ko_id" in meta:
            if meta["ko_id"] != link_key or not link_key.startswith("KO:"):
                raise _fail(kind, index, "for a KofamScan row link_key must be the KO CURIE and equal metadata.ko_id", row)
        elif link_key != "":
            raise _fail(kind, index, "link_key must be '' when metadata has no ko_id", row)
    elif table == schema.GENE_X_PROTEIN:
        _require_keys(kind, index, meta, ["gene_id", "translation_table"], row)
        if meta["gene_id"] != link_key:
            raise _fail(kind, index, "metadata.gene_id must equal link_key", row)
        if not GENE_ID_PATTERN.match(link_key):
            raise _fail(kind, index, f"gene_id {link_key!r} is not namespaced (KBDL:<16hex>.<contig>.<cds>, CDM:<id> or PATRIC:<id>)", row)
        tt = meta["translation_table"]
        if not (_is_int(tt) and tt > 0) and not (isinstance(tt, str) and tt.isdigit() and int(tt) > 0):
            raise _fail(kind, index, "metadata.translation_table must be a positive integer", row)
    elif table == schema.GENE_X_GENOME:
        _require_keys(kind, index, meta, ["gene_id", "contig_hash", "begin", "end", "strand"], row)
        for flag in ("partial_begin", "partial_end"):
            if flag not in meta or not isinstance(meta[flag], bool):
                raise _fail(kind, index, f"metadata.{flag} is required and must be a boolean", row)
        if meta["gene_id"] != link_key:
            raise _fail(kind, index, "metadata.gene_id must equal link_key", row)
        if not GENE_ID_PATTERN.match(link_key):
            raise _fail(kind, index, f"gene_id {link_key!r} is not namespaced (KBDL:<16hex>.<contig>.<cds>, CDM:<id> or PATRIC:<id>)", row)
        if not (isinstance(meta["contig_hash"], str) and _HEX64.match(meta["contig_hash"])):
            raise _fail(kind, index, "metadata.contig_hash must be 64 lowercase hex characters", row)
        begin, end, strand = meta["begin"], meta["end"], meta["strand"]
        if not (_is_int(begin) and _is_int(end)) or begin < 1 or not begin < end:
            raise _fail(kind, index, f"coordinates must be integers, 1-based inclusive, with begin < end (got begin={begin!r}, end={end!r})", row)
        if not _is_int(strand) or strand not in (1, -1):
            raise _fail(kind, index, f"metadata.strand must be 1 or -1, got {strand!r}", row)
    elif table == schema.GENOME_X_GENOME:
        _require_keys(kind, index, meta, ["ani", "af_query", "af_reference", "original_orientation"], row)
        for key in ("ani", "af_query", "af_reference"):
            if not (isinstance(meta[key], str) and _DECIMAL.match(meta[key])):
                raise _fail(kind, index, f"metadata.{key} must be a decimal string", row)


def validate_link_rows(
    table: str,
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate and canonicalize rows for one joining table.

    Returns ``{"rows": [...], "collapsed_identical": n, "skipped_self_pairs": n}``.
    Rows carry exactly :func:`schema.join_table_column_names` minus the server
    columns, ``metadata`` as canonical JSON text. Byte-identical rows collapse.

    Raises:
        LinkRefusedError: an unknown table; a row that is not a mapping or
        carries a server column; a NULL/empty key field (subject_hash,
        object_hash, relation, method_name) or a NULL link_key; a hash that is
        not 64 lowercase hex; a relation outside the table's closed
        vocabulary; missing required metadata keys; non-canonical metadata
        JSON (or a float in it); a symmetric row with
        ``subject_hash > object_hash``; metadata rule violations (including
        the gene_x_genome coordinate rules); two rows with the same key and
        different metadata.
    """
    if table not in schema.JOIN_TABLES:
        raise LinkRefusedError(
            f"unknown join table {table!r}; expected one of "
            f"{list(schema.JOIN_TABLES)}. Nothing has been written."
        )
    vocabulary = schema.JOIN_RELATIONS[table]
    by_key: dict[tuple[str, ...], dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    collapsed = 0
    self_pairs = 0
    allowed = set(schema.join_table_column_names(table)) - set(SERVER_COLUMNS)
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise _fail("link", index, f"expected a mapping, got {type(row).__name__}")
        for column in SERVER_COLUMNS:
            if column in row:
                raise _fail("link", index, f"caller-supplied {column!r} is refused; the writer generates it", row)
        unknown = set(row) - allowed
        if unknown:
            raise _fail("link", index, f"unknown column(s) {sorted(unknown)}", row)
        for column in schema.JOIN_KEY_COLUMNS:
            if row.get(column) is None:
                raise _fail("link", index, f"NULL {column} (every key field is never NULL)", row)
            if not isinstance(row[column], str):
                raise _fail("link", index, f"{column} must be a string", row)
        for column in ("subject_hash", "object_hash"):
            if not _HEX64.match(row[column]):
                raise _fail("link", index, f"{column} must be 64 lowercase hex characters", row)
        if not row["method_name"]:
            raise _fail("link", index, "empty method_name", row)
        relation = row["relation"]
        if relation not in vocabulary:
            raise _fail("link", index, f"relation {relation!r} is outside the {table} vocabulary {list(vocabulary)}", row)
        # metadata: canonical JSON text, or a mapping we canonicalize.
        raw_meta = row.get("metadata", "{}")
        if raw_meta is None:
            raise _fail("link", index, "NULL metadata (use '{}' when empty)", row)
        try:
            if isinstance(raw_meta, str):
                parsed = _parse_strict(raw_meta)
                if not isinstance(parsed, dict):
                    raise ValueError("metadata must be a JSON object")
                canonical = canonical_metadata(parsed)
                if canonical != raw_meta:
                    raise _fail(
                        "link", index,
                        f"non-canonical metadata JSON (canonical form: {canonical!r}; "
                        "sorted keys, UTF-8, compact, no floats)",
                        row,
                    )
            elif isinstance(raw_meta, Mapping):
                parsed = dict(raw_meta)
                canonical = canonical_metadata(parsed)
            else:
                raise ValueError("metadata must be canonical JSON text or a mapping")
        except LinkRefusedError:
            raise
        except ValueError as exc:
            raise _fail("link", index, f"invalid metadata JSON: {exc}", row) from exc
        # Symmetric: stored once, subject < object in byte order.
        subject, obj = row["subject_hash"], row["object_hash"]
        if relation in schema.SYMMETRIC_RELATIONS:
            if subject == obj:
                self_pairs += 1
                continue
            if subject > obj:
                raise _fail(
                    "link", index,
                    f"symmetric relation {relation!r} must be stored once with "
                    "subject_hash < object_hash in byte order",
                    row,
                )
        _check_metadata(table, index, row, parsed)
        clean = {
            "subject_hash": subject,
            "object_hash": obj,
            "relation": relation,
            "method_name": row["method_name"],
            "link_key": row["link_key"],
            "metadata": canonical,
        }
        key = tuple(clean[c] for c in schema.JOIN_KEY_COLUMNS)
        prior = by_key.get(key)
        if prior is not None:
            if prior["metadata"] != canonical:
                raise _fail(
                    "link", index,
                    "same key (subject_hash, object_hash, relation, method_name, "
                    "link_key) as an earlier row of this batch but different metadata",
                    row,
                )
            collapsed += 1
            continue
        by_key[key] = clean
        ordered.append(clean)
    return {
        "rows": ordered,
        "collapsed_identical": collapsed,
        "skipped_self_pairs": self_pairs,
    }


def metadata_conflict_key(row: Mapping[str, Any]) -> tuple[str, ...]:
    """The fill-only key tuple of a link row."""
    return tuple(row[c] for c in schema.JOIN_KEY_COLUMNS)
