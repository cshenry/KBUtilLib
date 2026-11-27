"""The versioned, PREHASHED delivery manifest (``delivery_schema_version`` 1).

A producer that has already computed its own entity hashes (KBDLJobRunningPrototype,
or anything else that does not go through the bronze sharder) hands the
clearinghouse a DELIVERY: a directory of parquet batch files plus one
``delivery.json`` describing them. This module is the ONE definition of that
contract -- the file layout, the checksums, the schemas, the dependency order
and the batch-id derivation -- and the checked-in golden fixture
(``tests/berdl/fixtures/delivery_v1``) is its executable statement. Every
producer builds through :func:`build_delivery` and every consumer reads through
:func:`validate_delivery`; both pass the fixture.

LAYOUT (relative to the delivery root)::

    delivery.json                      the manifest (this contract)
    run.json                           the shard-run inventory ingest_shards reads
                                       (derived from delivery.json by
                                       :func:`write_run_inventory`)
    <table>/<batch_id>.parquet         one file per batch

MANIFEST FIELDS (``delivery_schema_version`` 1)::

    delivery_schema_version  1
    delivery_id              ULID-shaped, derived from the batches' content digests
    producer                 free text naming the producer
    observed_at              ISO-8601 'Z' timestamp every row carries
    standardizer_version     the identity standardizer version the hashes use
    stage_order              ["methods", "entities", "aliases", "links"]
    schemas                  {table: [[column, sql_type], ...]} for each table present
    batches                  [ {order, stage, table, batch_id, file, rows, bytes,
                                sha256, content_sha256, key_column, hash_min,
                                hash_max, method_names (links only)} ... ]

DEPENDENCY ORDER: ``methods`` -> ``entities`` -> ``aliases`` -> ``links``.
(``parameter_set`` registry rows, when delivered, ride with ``methods``; they are
the registry a method's ``parameter_set_hash`` resolves against.) ``batches`` is
listed in that order and :func:`validate_delivery` refuses any other.

BATCH-ID DERIVATION. ``content_sha256`` is the sha256 of the canonical JSON of
the batch's rows sorted by key and WITHOUT ``ingest_batch_id``. ``batch_id`` is
``ulid_from_digest(sha256(f"{table}\\0{content_sha256}"))`` -- 26 Crockford base32
characters (the existing batch-id shape), deterministic, so re-delivering the
same content yields the same ids and every row's ``ingest_batch_id`` equals its
batch's id. ``sha256`` is the file's byte digest (file bytes are the
transport's concern and are not asserted to reproduce across pyarrow versions;
``content_sha256`` is what a rebuild is compared on).

RANGE FIELDS. ``key_column`` is the column ``hash_min``/``hash_max`` bound:
``method_name`` for ``method`` batches, ``subject_hash`` for joining tables,
``parameter_set_hash`` for the registry and ``entity_hash`` otherwise. A link
batch also carries ``method_names`` -- the methods its rows cite.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import clearinghouse_ingest as ci
from . import clearinghouse_links as cl
from . import clearinghouse_schema as schema

__all__ = [
    "DELIVERY_SCHEMA_VERSION",
    "DELIVERY_FILE",
    "STAGES",
    "DeliveryError",
    "stage_of_table",
    "ulid_from_digest",
    "batch_id_for",
    "content_sha256",
    "expected_columns",
    "build_delivery",
    "read_delivery",
    "validate_delivery",
    "write_run_inventory",
]

DELIVERY_SCHEMA_VERSION = 1
DELIVERY_FILE = "delivery.json"
STAGES = ("methods", "entities", "aliases", "links")

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class DeliveryError(ValueError):
    """The delivery violates the contract; nothing should be loaded from it."""


def stage_of_table(table: str) -> str:
    """The dependency stage a table belongs to."""
    if table in (schema.METHOD_TABLE, schema.PARAMETER_SET_TABLE):
        return "methods"
    if table in schema.JOIN_TABLES:
        return "links"
    if table.endswith("_alias"):
        return "aliases"
    return "entities"


def expected_columns(table: str) -> tuple[tuple[str, str], ...]:
    """The declared ``(name, sql_type)`` columns of ``table``, from the schema."""
    for config in schema.table_configs():
        if config["name"] == table:
            return tuple(
                (part.strip().split()[0], part.strip().split()[1].upper())
                for part in config["schema_sql"].split(",")
                if part.strip()
            )
    raise DeliveryError(f"{table!r} is not a clearinghouse table")


def ulid_from_digest(digest: bytes) -> str:
    """Encode the first 128 bits of ``digest`` as a 26-char Crockford ULID shape."""
    value = int.from_bytes(digest[:16], "big")
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def _canonical_rows_json(rows: Sequence[Mapping[str, Any]]) -> str:
    return json.dumps(
        [
            {k: v for k, v in sorted(row.items()) if k != "ingest_batch_id"}
            for row in rows
        ],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def content_sha256(rows: Sequence[Mapping[str, Any]]) -> str:
    """sha256 of the canonical JSON of ``rows`` WITHOUT ``ingest_batch_id``."""
    return hashlib.sha256(_canonical_rows_json(rows).encode("utf-8")).hexdigest()


def batch_id_for(table: str, content_digest: str) -> str:
    """The derived batch id for ``table`` rows with this ``content_sha256``."""
    return ulid_from_digest(
        hashlib.sha256(f"{table}\0{content_digest}".encode("utf-8")).digest()
    )


def _key_column(table: str) -> str:
    return ci.range_column_for_table(table)


def _sort_key(table: str, row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(row[c]) for c in ci.key_columns_for_table(table))


def build_delivery(
    root: str | Path,
    tables: Sequence[tuple[str, Sequence[Mapping[str, Any]]]],
    *,
    producer: str,
    observed_at: str,
    standardizer_version: str,
) -> dict[str, Any]:
    """Write a delivery under ``root`` and return its manifest.

    ``tables`` is ``[(table, rows), ...]`` in ANY order; the manifest lists the
    batches in dependency order. Rows carry every declared column except
    ``ingest_batch_id`` (stamped from the derived batch id) -- ``observed_at``
    is set from the argument when a row lacks it. One batch per table.
    """
    import pyarrow as pa  # noqa: PLC0415
    import pyarrow.parquet as pq  # noqa: PLC0415

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    schemas: dict[str, list[list[str]]] = {}
    stage_rank = {name: i for i, name in enumerate(STAGES)}
    ordered = sorted(
        enumerate(tables),
        key=lambda item: (
            stage_rank[stage_of_table(item[1][0])],
            ci.load_rank(item[1][0]),
            item[0],
        ),
    )
    for _index, (table, rows) in ordered:
        columns = expected_columns(table)
        names = [c for c, _t in columns]
        prepared = []
        for row in rows:
            full = {c: row.get(c) for c in names}
            if full.get("observed_at") is None:
                full["observed_at"] = observed_at
            prepared.append(full)
        prepared.sort(key=lambda r: _sort_key(table, r))
        digest = content_sha256(prepared)
        batch_id = batch_id_for(table, digest)
        for row in prepared:
            row["ingest_batch_id"] = batch_id
        directory = root / table
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{batch_id}.parquet"
        arrow_schema = pa.schema([(c, pa.string()) for c in names])
        pq.write_table(
            pa.Table.from_pylist(prepared, schema=arrow_schema), path
        )
        key_column = _key_column(table)
        keys = [str(r[key_column]) for r in prepared]
        entry: dict[str, Any] = {
            "order": len(entries),
            "stage": stage_of_table(table),
            "table": table,
            "batch_id": batch_id,
            "file": f"{table}/{path.name}",
            "rows": len(prepared),
            "bytes": path.stat().st_size,
            "sha256": ci.file_sha256(path),
            "content_sha256": digest,
            "key_column": key_column,
            "hash_min": keys[0] if keys else None,
            "hash_max": keys[-1] if keys else None,
        }
        if table in schema.JOIN_TABLES:
            entry["method_names"] = sorted({r["method_name"] for r in prepared})
        entries.append(entry)
        schemas[table] = [[c, t] for c, t in columns]
    delivery_id = ulid_from_digest(
        hashlib.sha256(
            "\0".join(e["content_sha256"] for e in entries).encode("utf-8")
        ).digest()
    )
    manifest = {
        "delivery_schema_version": DELIVERY_SCHEMA_VERSION,
        "delivery_id": delivery_id,
        "producer": producer,
        "observed_at": observed_at,
        "standardizer_version": standardizer_version,
        "stage_order": list(STAGES),
        "schemas": schemas,
        "batches": entries,
    }
    (root / DELIVERY_FILE).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def read_delivery(root: str | Path) -> dict[str, Any]:
    """Read ``<root>/delivery.json``; refuse a missing file or other version."""
    path = Path(root) / DELIVERY_FILE
    if not path.is_file():
        raise DeliveryError(f"{path} does not exist; not a delivery")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    version = manifest.get("delivery_schema_version")
    if version != DELIVERY_SCHEMA_VERSION:
        raise DeliveryError(
            f"{path} declares delivery_schema_version {version!r}; this code "
            f"implements {DELIVERY_SCHEMA_VERSION}. Refusing."
        )
    return manifest


def validate_delivery(root: str | Path) -> dict[str, Any]:
    """Check EVERY clause of the contract against the files; return the manifest.

    Raises :class:`DeliveryError` naming the first violation: wrong version;
    batches out of dependency order; a schema that is not the declared table
    schema; a missing file, wrong size/sha256/row count; a ``content_sha256``
    or ``batch_id`` that does not re-derive; a row whose ``ingest_batch_id`` is
    not its batch's; ``hash_min``/``hash_max`` not the first/last key; a method
    row, or a link row, that breaks its own rules
    (:mod:`clearinghouse_links`, including symmetric ordering); a link citing a
    method this delivery does not carry, or an endpoint no delivered entity
    batch holds (an orphan); ``method_names`` not equal to the methods cited.
    """
    root = Path(root)
    manifest = read_delivery(root)
    if manifest.get("stage_order") != list(STAGES):
        raise DeliveryError(f"stage_order must be {list(STAGES)}")
    batches = manifest.get("batches", [])
    last_rank = -1
    tables: dict[str, list[dict[str, Any]]] = {}
    for position, entry in enumerate(batches):
        table = entry.get("table")
        where = f"batch {position} ({table})"
        if entry.get("order") != position:
            raise DeliveryError(f"{where}: order must be {position}")
        stage = stage_of_table(str(table))
        if entry.get("stage") != stage:
            raise DeliveryError(f"{where}: stage must be {stage!r}")
        rank = STAGES.index(stage)
        if rank < last_rank:
            raise DeliveryError(
                f"{where}: dependency order is methods -> entities -> aliases "
                f"-> links; a {stage} batch follows a later stage"
            )
        last_rank = rank
        declared = [list(c) for c in expected_columns(str(table))]
        if manifest.get("schemas", {}).get(table) != declared:
            raise DeliveryError(f"{where}: schema is not the declared {table} schema")
        try:
            data = ci.verify_batch_file(
                entry,
                shard_root=root,
                expected_batch_id=str(entry["batch_id"]),
                expected_sha256=str(entry.get("sha256") or ""),
            )
        except ci.ClearinghouseBatchIntegrityError as exc:
            raise DeliveryError(f"{where}: {exc}") from exc
        if data.column_names != [c for c, _t in expected_columns(str(table))]:
            raise DeliveryError(f"{where}: file columns {data.column_names} are not the declared columns")
        if entry.get("bytes") != (root / entry["file"]).stat().st_size:
            raise DeliveryError(f"{where}: bytes does not match the file size")
        rows = data.to_pylist()
        digest = content_sha256(rows)
        if digest != entry.get("content_sha256"):
            raise DeliveryError(f"{where}: content_sha256 does not re-derive from the rows")
        if batch_id_for(str(table), digest) != entry["batch_id"]:
            raise DeliveryError(f"{where}: batch_id does not re-derive (table + content_sha256)")
        key_column = _key_column(str(table))
        if entry.get("key_column") != key_column:
            raise DeliveryError(f"{where}: key_column must be {key_column!r}")
        keys = [str(r[key_column]) for r in rows]
        if rows and (entry.get("hash_min") != min(keys) or entry.get("hash_max") != max(keys)):
            raise DeliveryError(f"{where}: hash_min/hash_max are not the range of {key_column}")
        tables.setdefault(str(table), []).extend(rows)
        if table in schema.JOIN_TABLES:
            cited = sorted({r["method_name"] for r in rows})
            if entry.get("method_names") != cited:
                raise DeliveryError(f"{where}: method_names {entry.get('method_names')} != cited {cited}")

    # row-level rules
    strip = ("observed_at", "ingest_batch_id")
    methods = {}
    if schema.METHOD_TABLE in tables:
        rows = [{k: v for k, v in r.items() if k not in strip} for r in tables[schema.METHOD_TABLE]]
        try:
            valid, _reg = cl.validate_method_rows(rows)
        except cl.LinkRefusedError as exc:
            raise DeliveryError(str(exc)) from exc
        methods = {m["method_name"] for m in valid}
        registry = {r["parameter_set_hash"] for r in tables.get(schema.PARAMETER_SET_TABLE, [])}
        for m in valid:
            if m["parameter_set_hash"] not in registry:
                raise DeliveryError(
                    f"method {m['method_name']!r}: parameter_set_hash has no "
                    "parameter_set row in this delivery"
                )
    entities: dict[str, set[str]] = {}
    for table, rows in tables.items():
        if table.endswith("_entity"):
            entities[table[: -len("_entity")]] = {r["entity_hash"] for r in rows}
    for table in schema.JOIN_TABLES:
        if table not in tables:
            continue
        rows = [{k: v for k, v in r.items() if k not in strip} for r in tables[table]]
        try:
            checked = cl.validate_link_rows(table, rows)
        except cl.LinkRefusedError as exc:
            raise DeliveryError(str(exc)) from exc
        if checked["skipped_self_pairs"] or checked["collapsed_identical"]:
            raise DeliveryError(f"{table}: a delivery carries no self-pairs or duplicate rows")
        subject_type, object_type = schema.JOIN_TABLE_ENDPOINTS[table]
        for r in checked["rows"]:
            if r["method_name"] not in methods:
                raise DeliveryError(f"{table}: cites method {r['method_name']!r} not in this delivery")
            if r["subject_hash"] not in entities.get(subject_type, set()):
                raise DeliveryError(f"{table}: orphan subject {r['subject_hash']} (no {subject_type}_entity row)")
            if r["object_hash"] not in entities.get(object_type, set()):
                raise DeliveryError(f"{table}: orphan object {r['object_hash']} (no {object_type}_entity row)")
    return manifest


def write_run_inventory(root: str | Path) -> Path:
    """Derive ``<root>/run.json`` (the shard-run inventory) from ``delivery.json``.

    This is the bridge to the consumer: ``ClearinghouseCapability.ingest_shards``
    reads ``run.json``, and a delivery's batches are exactly shard batches with
    method entries (range field ``method_name``) and link entries (``table``,
    ``method_names``, range field ``subject_hash``). The file is written only
    after :func:`validate_delivery` passes.
    """
    from .clearinghouse_shard import RUN_MANIFEST_FILE, RUN_MANIFEST_VERSION

    root = Path(root)
    manifest = validate_delivery(root)
    run = {
        "run_manifest_version": RUN_MANIFEST_VERSION,
        "run_id": manifest["delivery_id"],
        "created_at": manifest["observed_at"],
        "observed_at": manifest["observed_at"],
        "standardizer_version": manifest["standardizer_version"],
        "delivery_schema_version": DELIVERY_SCHEMA_VERSION,
        "slices": [],
        "units": [],
        "batches": [
            {
                "table": b["table"],
                "batch_id": b["batch_id"],
                "file": b["file"],
                "rows": b["rows"],
                "bytes": b["bytes"],
                "rows_deduplicated": 0,
                "key_column": b["key_column"],
                "hash_min": b["hash_min"],
                "hash_max": b["hash_max"],
                "sha256": b["sha256"],
                "source": [manifest["producer"]],
                "slice": None,
                "unit_id": None,
                "attempt_id": "delivery",
                **({"method_names": b["method_names"]} if "method_names" in b else {}),
            }
            for b in manifest["batches"]
        ],
        "skipped": [],
        "sources": [],
    }
    path = root / RUN_MANIFEST_FILE
    path.write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    return path
