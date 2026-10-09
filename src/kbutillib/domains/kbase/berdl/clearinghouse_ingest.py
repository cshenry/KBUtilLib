"""Shard-ingest mechanics for the clearinghouse backfill -- the FILL-ONLY layer.

This module holds everything the ingest half of the backfill needs that is
NOT a transport call: the pure key-matching rule that makes a load idempotent,
the batch-integrity checks that run before a file is used, the load-execution
inventory and its durable ledger, and the per-namespace OS lock. The transport
side -- reading the live key ranges and handing survivors to
``BerdlCapability.load()`` -- stays in
:meth:`~kbutillib.domains.kbase.berdl.clearinghouse_capability.ClearinghouseCapability.ingest_shards`,
which is the only sanctioned write path.

WHY THIS LAYER IS SEPARATE AND MOSTLY PURE. The backfill runs for days inside
a rootless JupyterHub pod with no init system, so it is killed and resumed
routinely, and every decision it makes about "has this batch already landed?"
has to be reproducible without a lakehouse. Putting the decision rules in
functions over in-memory tables means they can be proven by test rather than
argued about from a run log -- and the Spark glue that executes them at scale
is then written to MIRROR a function that is already proven, not to be the
only statement of the rule.

THE SHAPE OF THE INGEST (Chris's review, 2026-10-06). The backfill runs
ENTIRELY in the BERDL pod and pulls from the pod's own sources; the bronze
sharder writes its batches to the pod's LOCAL disk (persistent home,
``~/clearinghouse-shards/``). Spark EXECUTORS cannot read that disk, so a
bronze-mode ``load()`` pointed at a local path cannot work cluster-side: it is
the driver that must read each batch and hand the rows over as a DataFrame.
That is what :func:`read_batch_table` plus DataFrame-mode ``load()`` do, and
it is why there is no object-storage staging and no S3 path anywhere here.

FILL-ONLY IS NOT ATOMIC. The survivors of a batch are computed against the
target's keys as they stood at the moment of the read. A writer OUTSIDE this
process that appends a colliding key between that read and the append will
leave a duplicate; the per-namespace lock (:class:`NamespaceLock`) excludes
other *clearinghouse ingests*, not arbitrary writers, and nothing here tries
to close that window. The lake is append-only and has no transaction we could
take, so the honest statement is the one in this paragraph rather than a
guarantee the code cannot keep.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from .clearinghouse_derivation import SLOT_KEY_COLUMNS
from .clearinghouse_schema import PARAMETER_SET_TABLE

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pyarrow as pa


# --------------------------------------------------------------------------
# Where state lives
# --------------------------------------------------------------------------

#: Environment variable naming the ROOT under which every load execution keeps
#: its state directory. The ledger must NOT live beside the shards: the shard
#: directory is the sharder's output and may be regenerated, moved or deleted
#: between loads, while the ledger is the only record of what has been written
#: to the lake and must outlive it.
STATE_DIR_ENV = "KBU_CLEARINGHOUSE_STATE_DIR"

#: Default state root when :data:`STATE_DIR_ENV` is unset. Under the pod's
#: persistent home, so a pod restart does not lose the ledger.
DEFAULT_STATE_ROOT = "~/.kbu/clearinghouse/runs"

#: The JSON-lines ledger file inside an execution's state directory.
LEDGER_FILENAME = "ingest_ledger.jsonl"

#: The execution binding file inside an execution's state directory: which
#: shard run it draws from, which namespace it is bound to, and the DERIVED
#: inventory of batches it will load (see :func:`derive_execution_inventory`).
EXECUTION_FILENAME = "execution.json"

#: Environment variable naming the ONE pod-visible directory the per-namespace
#: ingest locks live in. It is deliberately not under the state dir: two
#: executions with different state dirs writing one namespace must contend,
#: and they can only do that through a shared path.
LOCK_DIR_ENV = "KBU_CLEARINGHOUSE_LOCK_DIR"

#: Default shared lock directory on the BERDL pod.
DEFAULT_LOCK_DIR = "/global_share/KBaseUtilities/clearinghouse-locks"


# --------------------------------------------------------------------------
# Ledger states
# --------------------------------------------------------------------------

#: Written BEFORE an append, with the post-filter ``expected_rows`` already
#: counted from a MATERIALISED survivor set. Non-terminal: a ``started`` line
#: with no terminal line means the process died around the append and whether
#: the rows landed is unknown until reconcile asks the table.
LEDGER_STARTED = "started"

#: Terminal: the batch's survivors are in the table (or there were none, in
#: which case no ``load()`` was made at all).
LEDGER_COMMITTED = "committed"

#: Terminal: the batch was refused or its append raised. A failed batch blocks
#: verification; it is not silently retried.
LEDGER_FAILED = "failed"

#: What a ULID looks like (Crockford base32, no I/L/O/U) -- the shape every
#: ``ingest_batch_id`` has, and the ONLY thing allowed into the reconcile
#: count's SQL text, since Spark has no bind channel.
ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")

#: 64-char lowercase hex -- the shape of every ``entity_hash`` /
#: ``parameter_set_hash`` and the only thing allowed into a range predicate.
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class ClearinghouseBatchIntegrityError(RuntimeError):
    """A shard batch failed an integrity check and was not used.

    Raised by :func:`verify_batch_file` for a manifest-version mismatch, a
    path that escapes the shard root, a sha256 mismatch, a row count that
    disagrees with the inventory, or a row whose ``ingest_batch_id`` is not
    the batch's own. Every one of those means the file on disk is not the file
    the inventory describes, so nothing about it can be trusted -- including
    the hash range the fill-only read is restricted to, which is exactly the
    value that decides what is filtered out.
    """


class ClearinghouseExecutionBindingError(RuntimeError):
    """A load execution was resumed against the wrong target or shard run.

    An execution's ledger is bound to ONE target namespace and ONE shard run:
    its ``expected_rows`` and its per-batch counts are statements about that
    namespace. Resuming it against another one would compare counts from a
    table the rows were never written to, so it refuses.
    """


class ClearinghouseLockUnavailableError(RuntimeError):
    """The shared lock directory could not be created or written.

    NEVER falls back to a private lock: a lock nobody else can see is not a
    lock, and the failure it would permit (two ingests appending the same
    batch to one namespace) is precisely what the lock exists to prevent.
    """


class ClearinghouseNamespaceLockedError(RuntimeError):
    """Another live process holds this namespace's ingest lock."""


# --------------------------------------------------------------------------
# Key modes -- WHAT MAKES A ROW "ALREADY PRESENT"
# --------------------------------------------------------------------------


def key_columns_for_table(table: str) -> tuple[str, ...]:
    """The columns whose tuple decides a row is already in ``table``.

    Three modes, one per table family, and they are not interchangeable:

    - ``<type>_entity`` and ``<type>_content`` -- ``("entity_hash",)``. The
      hash IS the identity (the entity type is carried by the physical table,
      per ``clearinghouse_capability`` Rule 3), and a second row for one hash
      in a ``<type>_content`` table has NO current-state derivation to
      collapse it, so re-appending one is the specific damage fill-only
      exists to prevent.
    - ``<type>_result`` -- the slot key, IMPORTED from
      :data:`~kbutillib.domains.kbase.berdl.clearinghouse_derivation.SLOT_KEY_COLUMNS`
      and never restated here. A result row is superseded within its slot by
      ``observed_at``/``ingest_batch_id``, so "already present" means the
      whole slot tuple already exists -- a re-run of the same tool over the
      same entity with the same parameters.
    - :data:`~kbutillib.domains.kbase.berdl.clearinghouse_schema.PARAMETER_SET_TABLE`
      -- ``("parameter_set_hash",)``. The registry is a hash-keyed dictionary;
      a hash always maps to the same canonical text, so a second row is
      redundant rather than wrong, but a re-run that re-appends the whole
      registry is pure waste and makes the table's row count meaningless.

    Raises:
        ValueError: ``table`` is not a clearinghouse table this module knows
            how to key. Guessing a key mode would silently disable fill-only.
    """
    if table == PARAMETER_SET_TABLE:
        return ("parameter_set_hash",)
    if table.endswith("_result"):
        return tuple(SLOT_KEY_COLUMNS)
    if table.endswith("_entity") or table.endswith("_content"):
        return ("entity_hash",)
    raise ValueError(
        f"key_columns_for_table: {table!r} is not a clearinghouse table with a "
        "known fill-only key mode (expected <type>_entity, <type>_content, "
        f"<type>_result or {PARAMETER_SET_TABLE!r}). Refusing to guess -- a "
        "wrong key mode silently disables fill-only."
    )


def range_column_for_table(table: str) -> str:
    """The hash column a batch's ``[hash_min, hash_max]`` window applies to.

    The sharder emits each batch sorted on ONE key column and records that
    column's min and max, so the fill-only read can ask the live table for
    only the keys that could possibly collide. For the fourteen entity /
    content / result tables that column is ``entity_hash``; for the registry
    it is ``parameter_set_hash``.
    """
    return "parameter_set_hash" if table == PARAMETER_SET_TABLE else "entity_hash"


def fill_only_survivors(
    batch: "pa.Table",
    existing_keys: "pa.Table",
    key_columns: Sequence[str],
) -> "pa.Table":
    """Rows of ``batch`` whose key tuple is NOT in ``existing_keys``.

    THE STATEMENT OF FILL-ONLY, and the specification the Spark anti-join in
    ``ClearinghouseCapability`` mirrors. A left-anti-join of ``batch`` against
    ``existing_keys`` on ``key_columns``:

    - Row ORDER and the batch's full SCHEMA are preserved; only whole rows are
      dropped. The survivors are appended as they were sharded.
    - DUPLICATES WITHIN THE BATCH ARE BOTH KEPT (or both dropped) -- a key
      present twice in the left side appears twice in a Spark anti-join too,
      and the sharder already deduplicates within a batch, so diverging here
      would only hide a sharder bug.
    - A NULL ANYWHERE IN A ROW'S KEY TUPLE MAKES IT A SURVIVOR. This is not a
      nicety: SQL ``NULL`` never equals ``NULL``, so Spark's anti-join cannot
      match such a row and keeps it. A Python set would happily match
      ``None == None`` and DROP it, and then the pure function and the engine
      would disagree about real data. Null keys are a schema violation the
      write boundary rejects elsewhere; here they merely must not cause the
      two implementations to diverge.
    - An ``existing_keys`` row with a NULL in its tuple matches nothing, for
      the same reason.

    Args:
        batch: The shard batch's rows, as read by :func:`read_batch_table`.
        existing_keys: The key columns already in the target, as read back
            from the live table (restricted to the batch's hash range).
        key_columns: From :func:`key_columns_for_table`.

    Returns:
        A ``pyarrow.Table`` with ``batch``'s schema holding only the survivors.

    Raises:
        ValueError: ``key_columns`` is empty, or a key column is missing from
            either table. A missing key column cannot mean "nothing matches":
            that would fail OPEN and re-append everything.
    """
    import pyarrow as pa  # noqa: PLC0415 -- keep this module import-cheap

    keys = tuple(key_columns)
    if not keys:
        raise ValueError(
            "fill_only_survivors: key_columns is empty, so no row could ever "
            "be shown present and fill-only would be a no-op. Refusing."
        )
    for column in keys:
        if column not in batch.column_names:
            raise ValueError(
                f"fill_only_survivors: batch has no key column {column!r} "
                f"(columns: {batch.column_names}). Refusing -- treating a "
                "missing key as 'nothing matches' re-appends every row."
            )
        if column not in existing_keys.column_names:
            raise ValueError(
                f"fill_only_survivors: existing_keys has no key column "
                f"{column!r} (columns: {existing_keys.column_names}). "
                "Refusing -- the read that produced it did not answer the "
                "question fill-only asked."
            )
    if batch.num_rows == 0:
        return batch

    present: set[tuple[Any, ...]] = set()
    if existing_keys.num_rows:
        existing_columns = [existing_keys.column(c).to_pylist() for c in keys]
        for values in zip(*existing_columns):
            if any(value is None for value in values):
                continue  # NULL matches nothing, exactly as in SQL
            present.add(tuple(values))

    if not present:
        return batch

    batch_columns = [batch.column(c).to_pylist() for c in keys]
    mask = [
        True
        if any(value is None for value in values)
        else tuple(values) not in present
        for values in zip(*batch_columns)
    ]
    return batch.filter(pa.array(mask, type=pa.bool_()))


# --------------------------------------------------------------------------
# Batch integrity -- EVERY check runs before the file is used
# --------------------------------------------------------------------------


def resolve_batch_path(shard_root: Path, relative: str) -> Path:
    """Resolve an inventory ``file`` entry under ``shard_root``, or refuse.

    The inventory is data read off disk, so its ``file`` field is untrusted
    input: an absolute path, or one containing ``..``, would make the ingest
    read a file the shard run never published (and whose sha256 the inventory
    cannot vouch for). Containment is checked AFTER resolution, so a symlink
    pointing out of the root is caught too.

    Raises:
        ClearinghouseBatchIntegrityError: ``relative`` is absolute, or
            resolves outside ``shard_root``.
    """
    if os.path.isabs(relative):
        raise ClearinghouseBatchIntegrityError(
            f"batch integrity: inventory file entry {relative!r} is an "
            "ABSOLUTE path. Every batch must lie under the shard root the "
            "inventory describes, because that root is what its sha256 and "
            "row counts were computed over. Refusing to read it."
        )
    root = shard_root.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ClearinghouseBatchIntegrityError(
            f"batch integrity: inventory file entry {relative!r} resolves to "
            f"{candidate} which is OUTSIDE the shard root {root}. Refusing to "
            "read it -- a batch outside the root is not a batch this run "
            "published."
        ) from exc
    return candidate


def file_sha256(path: Path, *, chunk_bytes: int = 1 << 20) -> str:
    """Stream ``path`` and return its sha256 hex digest.

    Streamed rather than read whole: a batch is sized for a Spark append, not
    for the driver's memory, and the whole point of the streaming sharder was
    to stop holding one in RAM.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_bytes), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_batch_table(path: Path) -> "pa.Table":
    """Read one shard batch file into memory as a ``pyarrow.Table``.

    The DRIVER reads the batch -- see the module docstring. Parquet is the
    sharder's only output format; anything else is refused rather than guessed
    at, because a mis-read batch would be filtered against the wrong keys.
    """
    import pyarrow.parquet as pq  # noqa: PLC0415 -- keep this module import-cheap

    suffix = path.suffix.lower()
    if suffix != ".parquet":
        raise ClearinghouseBatchIntegrityError(
            f"batch integrity: {path.name} is not a .parquet file (suffix "
            f"{suffix!r}). The bronze sharder emits parquet only; refusing to "
            "guess a reader."
        )
    return pq.read_table(path)


def verify_batch_file(
    entry: Mapping[str, Any],
    *,
    shard_root: Path,
    expected_batch_id: str,
    expected_sha256: str,
) -> "pa.Table":
    """Run EVERY integrity check on one batch, then return its rows.

    Five checks, in the order that makes each one meaningful, all BEFORE the
    file's contents are used for anything:

    1. PATH CONTAINMENT -- the file lies under the shard root
       (:func:`resolve_batch_path`) and exists.
    2. SHA256 -- the bytes are the bytes the inventory published. Every later
       check, and the hash range the fill-only read is restricted to, is a
       statement about THOSE bytes.
    3. READABILITY -- it parses as parquet.
    4. ROW COUNT -- it holds exactly the rows the inventory says. A short file
       would make ``expected_rows`` a lie and verification unprovable.
    5. PER-ROW BATCH ID -- every row's ``ingest_batch_id`` equals
       ``expected_batch_id``. This is the column reconcile and verification
       count on; a row carrying a different id is invisible to both, so the
       batch could never be shown to have landed.

    Args:
        entry: The batch's inventory entry (``file``, ``rows``, ...).
        shard_root: The directory ``run.json`` describes.
        expected_batch_id: The id every row must carry. For a DERIVED
            inventory this is the SOURCE batch id, because the file on disk
            still carries the ids the sharder wrote; the rewrite to the
            execution's own id happens after this check passes (see
            :func:`retag_batch_ids`).
        expected_sha256: The digest from the inventory entry.

    Returns:
        The batch's rows.

    Raises:
        ClearinghouseBatchIntegrityError: Any check failed; the message names
            which one and the batch it refused.
    """
    relative = entry.get("file")
    if not isinstance(relative, str) or not relative:
        raise ClearinghouseBatchIntegrityError(
            f"batch integrity: inventory entry for batch "
            f"{expected_batch_id!r} has no 'file'. Refusing."
        )
    path = resolve_batch_path(shard_root, relative)
    if not path.is_file():
        raise ClearinghouseBatchIntegrityError(
            f"batch integrity: batch {expected_batch_id!r} names {relative!r} "
            f"which does not exist under {shard_root}. Refusing."
        )
    if not expected_sha256:
        raise ClearinghouseBatchIntegrityError(
            f"batch integrity: batch {expected_batch_id!r} has no recorded "
            "sha256, so the file on disk cannot be shown to be the file the "
            "run published. Refusing."
        )
    actual = file_sha256(path)
    if actual != expected_sha256:
        raise ClearinghouseBatchIntegrityError(
            f"batch integrity: batch {expected_batch_id!r} file {relative!r} "
            f"has sha256 {actual} but the inventory recorded "
            f"{expected_sha256}. The file is not the file the run published, "
            "so its row count and hash range cannot be trusted either -- and "
            "the hash range is what decides which rows fill-only filters. "
            "Refusing this batch."
        )
    table = read_batch_table(path)
    expected_rows = entry.get("rows")
    if isinstance(expected_rows, int) and table.num_rows != expected_rows:
        raise ClearinghouseBatchIntegrityError(
            f"batch integrity: batch {expected_batch_id!r} file {relative!r} "
            f"holds {table.num_rows} rows but the inventory recorded "
            f"{expected_rows}. Refusing."
        )
    if "ingest_batch_id" not in table.column_names:
        raise ClearinghouseBatchIntegrityError(
            f"batch integrity: batch {expected_batch_id!r} file {relative!r} "
            "has no 'ingest_batch_id' column. That column is what reconcile "
            "and verification count on, so a batch without it can never be "
            "shown to have landed. Refusing."
        )
    ids = set(table.column("ingest_batch_id").to_pylist())
    if ids - {expected_batch_id}:
        offending = sorted(str(value) for value in (ids - {expected_batch_id}))[:3]
        raise ClearinghouseBatchIntegrityError(
            f"batch integrity: batch {expected_batch_id!r} file {relative!r} "
            f"carries other ingest_batch_id values ({', '.join(offending)}"
            f"{', ...' if len(ids - {expected_batch_id}) > 3 else ''}). Rows "
            "whose id is not the batch's own are invisible to the per-batch "
            "count that proves the append landed. Refusing."
        )
    return table


def retag_batch_ids(table: "pa.Table", batch_id: str) -> "pa.Table":
    """Rewrite every row's ``ingest_batch_id`` to ``batch_id``.

    A NEW load execution never adopts the shard run's batch ids -- see
    :func:`derive_execution_inventory` for why -- so the rows it appends must
    carry ITS id, or the per-batch count that proves the append landed would
    count another execution's rows. The source id survives as provenance in
    the inventory entry, not in the data.
    """
    import pyarrow as pa  # noqa: PLC0415 -- keep this module import-cheap

    index = table.column_names.index("ingest_batch_id")
    column = pa.array([batch_id] * table.num_rows, type=pa.string())
    return table.set_column(index, "ingest_batch_id", column)


# --------------------------------------------------------------------------
# The load execution and its derived inventory
# --------------------------------------------------------------------------


def new_execution_id() -> str:
    """Mint a load-execution id: a ULID, from the sharder's one minter."""
    from .clearinghouse_shard import new_ulid  # noqa: PLC0415 - import-cheap

    return new_ulid()


def resolve_state_root() -> Path:
    """The root under which every execution keeps its state directory."""
    configured = os.environ.get(STATE_DIR_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path(DEFAULT_STATE_ROOT).expanduser()


def execution_state_dir(execution_id: str) -> Path:
    """Where one load execution's ledger and binding live."""
    return resolve_state_root() / execution_id


def read_run_inventory(shard_dir: Path) -> dict[str, Any]:
    """Read ``<shard_dir>/run.json``, refusing a directory without one.

    The inventory is REQUIRED, not optional. Scanning the directory for files
    instead (what the previous implementation did) cannot tell a complete
    published batch from a half-written one, has no sha256 to check the bytes
    against, and has no hash range to restrict the fill-only read to -- so
    every integrity check and the whole idempotence story depend on this file
    existing.

    Raises:
        ClearinghouseBatchIntegrityError: No ``run.json``, unreadable, or a
            ``run_manifest_version`` this code does not implement.
    """
    from .clearinghouse_shard import (  # noqa: PLC0415 - import-cheap
        RUN_MANIFEST_FILE,
        RUN_MANIFEST_VERSION,
        read_run_manifest,
    )

    path = shard_dir / RUN_MANIFEST_FILE
    if not path.is_file():
        raise ClearinghouseBatchIntegrityError(
            f"ingest: {shard_dir} has no {RUN_MANIFEST_FILE} -- the run "
            "inventory the bronze sharder publishes. There is nothing to "
            "ingest without it: the inventory is the only source of each "
            "batch's sha256 (so the bytes can be checked), its row count (so "
            "the append can be proven) and its hash range (so fill-only can "
            "read back only the keys that could collide). Point this at a "
            "directory a completed `kbu clearinghouse shard` run published."
        )
    try:
        run = read_run_manifest(shard_dir)
    except ValueError as exc:
        raise ClearinghouseBatchIntegrityError(
            f"ingest: {path} is not readable as JSON: {exc}. Refusing."
        ) from exc
    version = run.get("run_manifest_version")
    if version != RUN_MANIFEST_VERSION:
        raise ClearinghouseBatchIntegrityError(
            f"ingest: {path} declares run_manifest_version {version!r} but "
            f"this code implements {RUN_MANIFEST_VERSION}. The field exists "
            "precisely so a changed meaning is refused rather than "
            "misread. Refusing."
        )
    return run


def _unit_order(run: Mapping[str, Any]) -> list[Any]:
    """Unit ids in publication order, with a slot for un-unitised batches."""
    order: list[Any] = [unit.get("unit_id") for unit in run.get("units", [])]
    for batch in run.get("batches", []):
        unit = batch.get("unit_id")
        if unit not in order:
            order.append(unit)
    return order


def _registry_first(batches: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Order one unit's batches with the parameter-set registry FIRST.

    REGISTRY BEFORE RESULTS is a writer obligation: a ``<type>_result`` row
    whose ``parameter_set_hash`` has no registry row is an orphan -- the hash
    is opaque, so nothing can say what parameters produced that result. The
    order is imposed HERE, over whatever order ``run.json`` happens to list,
    so an inventory that published the registry batch LAST still loads it
    first. Within the registry/non-registry split the inventory's own order is
    preserved (``sorted`` is stable), because that is publication order and
    the sharder's batches are append-only.
    """
    return sorted(
        (dict(batch) for batch in batches),
        key=lambda batch: 0 if batch.get("table") == PARAMETER_SET_TABLE else 1,
    )


def derive_execution_inventory(
    run: Mapping[str, Any],
    *,
    execution_id: str,
    adopt_source_batch_ids: bool = False,
) -> list[dict[str, Any]]:
    """Build a load execution's own inventory from a shard run's ``run.json``.

    A LOAD EXECUTION IS NOT A SHARD RUN. One shard run can be loaded more than
    once -- into a scratch namespace to check it, then into production; or
    re-loaded after a namespace is rebuilt -- and each load is a separate
    thing with its own ledger. If an execution reused the sharder's
    ``ingest_batch_id`` values, the per-batch count that proves an append
    landed could not tell this execution's rows from the previous one's, so
    the second load into the same namespace would be unverifiable and the
    promotion of a checked run to production would have no record of its own.

    So a NEW execution mints a FRESH ULID per batch and records the source run
    id, source batch id and source sha256 as provenance. The rows are retagged
    to the fresh id on the way in (:func:`retag_batch_ids`), which is also what
    makes a fill-only reload of already-landed files verifiable: the SLOT keys
    collide (so nothing is re-appended) while the batch ids do not (so the
    count is this execution's own).

    ``adopt_source_batch_ids=True`` is the one exception: when a shard run and
    its FIRST load execution are one operation (``backfill run``, which shards
    and loads under a single id), the ids the sharder just wrote into the rows
    are this execution's own ids and retagging them would be pointless churn.

    The returned order is the LOAD order: units in publication order, and
    within each unit the registry batch first (:func:`_registry_first`).
    """
    entries: list[dict[str, Any]] = []
    source_run_id = run.get("run_id")
    batches = run.get("batches", [])
    for unit_id in _unit_order(run):
        unit_batches = [b for b in batches if b.get("unit_id") == unit_id]
        for source in _registry_first(unit_batches):
            source_batch_id = str(source.get("batch_id"))
            batch_id = (
                source_batch_id if adopt_source_batch_ids else new_execution_id()
            )
            entries.append(
                {
                    "batch_id": batch_id,
                    "table": source.get("table"),
                    "file": source.get("file"),
                    "rows": source.get("rows"),
                    "key_column": source.get("key_column"),
                    "hash_min": source.get("hash_min"),
                    "hash_max": source.get("hash_max"),
                    "unit_id": unit_id,
                    "execution_id": execution_id,
                    "source_run_id": source_run_id,
                    "source_batch_id": source_batch_id,
                    "source_sha256": source.get("sha256"),
                    "adopted_source_batch_id": bool(adopt_source_batch_ids),
                }
            )
    return entries


def batch_parameter_set_hashes(
    entry: Mapping[str, Any], *, shard_root: Path
) -> set[str]:
    """The distinct ``parameter_set_hash`` values one result batch carries.

    Read straight off the batch file (one column), because the UNIT REGISTRY
    CHECK has to compare what a unit's results CLAIM against what the registry
    HOLDS, and only the file says what the results claim.
    """
    import pyarrow.parquet as pq  # noqa: PLC0415 -- keep this module import-cheap

    path = resolve_batch_path(shard_root, str(entry.get("file")))
    table = pq.read_table(path, columns=["parameter_set_hash"])
    return {
        value
        for value in table.column("parameter_set_hash").to_pylist()
        if value is not None
    }


# --------------------------------------------------------------------------
# The durable ledger
# --------------------------------------------------------------------------


class ExecutionLedger:
    """One load execution's append-only, fsynced JSON-lines ledger.

    THE LEDGER IS THE ONLY RECORD OF WHAT WAS WRITTEN, so every line is
    flushed AND ``fsync``ed before the call returns. A ``started`` line that
    is still in the page cache when the pod dies is worse than no ledger at
    all: resume would see no trace of a batch whose rows had landed, re-ingest
    it, and leave the duplicate ``<type>_content`` row that has no derivation
    to collapse it.

    Lines are appended, never rewritten. The LAST line for a batch id is its
    state, so ``started`` then ``committed`` reads as committed while a lone
    ``started`` reads as the ambiguous state reconcile resolves.
    """

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / LEDGER_FILENAME

    def append(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Append one record, fsynced, and return it as written."""
        from datetime import datetime, timezone  # noqa: PLC0415 - import-cheap

        line = dict(record)
        line.setdefault("at", datetime.now(timezone.utc).isoformat())
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return line

    def records(self) -> dict[str, dict[str, Any]]:
        """The LAST record per ``batch_id``.

        A malformed line is skipped rather than fatal: a crash mid-write
        leaves at most one truncated trailing line, and failing a days-long
        resume over it would be absurd.
        """
        out: dict[str, dict[str, Any]] = {}
        if not self.path.exists():
            return out
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(record, dict):
                continue
            batch_id = record.get("batch_id")
            if batch_id is None or record.get("state") is None:
                continue
            out[str(batch_id)] = record
        return out

    def state_of(self, batch_id: str) -> str | None:
        record = self.records().get(batch_id)
        return None if record is None else str(record.get("state"))


def read_execution(state_dir: Path) -> dict[str, Any]:
    """Read an execution's binding file, or ``{}`` when it has none yet."""
    path = Path(state_dir) / EXECUTION_FILENAME
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_execution(state_dir: Path, binding: Mapping[str, Any]) -> Path:
    """Write an execution's binding file atomically and fsync it.

    The binding carries the DERIVED INVENTORY, which is immutable once
    written: its fresh batch ids are the ids in the rows and in the ledger, so
    regenerating them on resume would orphan every count. Temp file plus
    ``os.replace``, so a crash leaves the old binding rather than a truncated
    one.
    """
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / EXECUTION_FILENAME
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(binding), indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def bind_execution(
    *,
    execution_id: str,
    shard_dir: Path,
    namespace: str,
    tenant: str,
    dataset: str,
    run: Mapping[str, Any],
    adopt_source_batch_ids: bool = False,
) -> dict[str, Any]:
    """Load an execution's binding, creating it on first use, and CHECK it.

    A new execution derives its inventory once and persists it. A resumed one
    reads back exactly what it derived -- the inventory is never regenerated,
    because the batch ids in it are the ids already in the lake and in the
    ledger.

    Raises:
        ClearinghouseExecutionBindingError: The execution exists and is bound
            to a different namespace or a different shard run. Its counts are
            statements about the namespace it was bound to; reading them
            against another table compares nothing to nothing.
    """
    state_dir = execution_state_dir(execution_id)
    existing = read_execution(state_dir)
    if existing:
        if existing.get("namespace") != namespace:
            raise ClearinghouseExecutionBindingError(
                f"ingest: execution {execution_id!r} is bound to namespace "
                f"{existing.get('namespace')!r} but this call targets "
                f"{namespace!r}. An execution's ledger -- its expected_rows "
                "and its per-batch counts -- is a statement about ONE "
                "namespace, so resuming it against another would verify "
                "against a table its rows were never written to. Use a new "
                "--run-id for a different target."
            )
        if existing.get("source_run_id") != run.get("run_id"):
            raise ClearinghouseExecutionBindingError(
                f"ingest: execution {execution_id!r} was built from shard run "
                f"{existing.get('source_run_id')!r} but {shard_dir} publishes "
                f"run {run.get('run_id')!r}. Refusing -- the execution's "
                "inventory names batches of the run it was derived from."
            )
        return existing
    from datetime import datetime, timezone  # noqa: PLC0415 - import-cheap

    binding = {
        "execution_id": execution_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "shard_dir": str(Path(shard_dir).resolve()),
        "namespace": namespace,
        "tenant": tenant,
        "dataset": dataset,
        "source_run_id": run.get("run_id"),
        "run_manifest_version": run.get("run_manifest_version"),
        "inventory": derive_execution_inventory(
            run,
            execution_id=execution_id,
            adopt_source_batch_ids=adopt_source_batch_ids,
        ),
    }
    return binding


# --------------------------------------------------------------------------
# The per-namespace ingest lock
# --------------------------------------------------------------------------


def resolve_lock_dir() -> Path:
    """The ONE pod-visible directory the namespace ingest locks live in."""
    configured = os.environ.get(LOCK_DIR_ENV)
    return Path(configured).expanduser() if configured else Path(DEFAULT_LOCK_DIR)


def _lock_file_name(namespace: str) -> str:
    """A lock file name for a target namespace (``tenant.dataset``)."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", namespace)
    return f"{safe}.lock"


class NamespaceLock:
    """An exclusive, OS-held lock on one target namespace's ingest.

    ``fcntl.flock`` on a file in the SHARED lock directory, held by the live
    process for the whole ingest. Three properties, each of which a
    hand-rolled lock file gets wrong:

    - PROCESS DEATH RELEASES IT. The kernel drops the lock when the fd closes,
      so a killed pod does not leave a namespace permanently unloadable. The
      holder record in the file is for the HUMAN (and for the takeover
      report); it is never what grants or denies the lock.
    - IT IS SHARED, NOT PRIVATE. The directory is one pod-visible path
      (:data:`LOCK_DIR_ENV`), deliberately NOT under the state dir, so two
      executions with different state dirs loading one namespace contend.
    - IT NEVER FALLS BACK. If the directory cannot be created or written, this
      refuses (:class:`ClearinghouseLockUnavailableError`) rather than taking
      a private lock nobody else can see.
    """

    def __init__(self, namespace: str) -> None:
        self.namespace = namespace
        self.path = resolve_lock_dir() / _lock_file_name(namespace)
        self._fd: int | None = None
        self.previous_holder: dict[str, Any] | None = None

    def acquire(self, *, execution_id: str) -> dict[str, Any]:
        directory = self.path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666)
        except OSError as exc:
            raise ClearinghouseLockUnavailableError(
                f"ingest: cannot use the shared clearinghouse lock directory "
                f"{directory} ({exc}). The per-namespace ingest lock MUST be "
                "visible to every process that could load this namespace, so "
                "there is no private fallback -- a lock nobody else can see "
                "would permit exactly the concurrent double-append it exists "
                f"to prevent. Create the directory, or set ${LOCK_DIR_ENV} to "
                "a path this pod and its peers share. Nothing has been "
                "written."
            ) from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            holder = self._read_holder(fd)
            os.close(fd)
            raise ClearinghouseNamespaceLockedError(
                f"ingest: namespace {self.namespace!r} is already being "
                f"loaded. The lock {self.path} is held by "
                f"{_describe_holder(holder)}. Two concurrent fill-only "
                "ingests into one namespace can each read the target before "
                "the other appends, so both would keep the same rows and "
                "duplicate them. Wait for that run, or load a different "
                f"namespace. ({exc.strerror or exc})"
            ) from exc
        previous = self._read_holder(fd)
        if previous and not previous.get("released_at"):
            # flock was granted, so whoever wrote this record no longer holds
            # the lock -- the kernel released it when that process died.
            self.previous_holder = previous
        self._fd = fd
        record = {
            "namespace": self.namespace,
            "execution_id": execution_id,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "started_at": _now_iso(),
        }
        self._write_holder(record)
        return record

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            self._fd = fd
            self._write_holder(
                {
                    "namespace": self.namespace,
                    "host": socket.gethostname(),
                    "pid": os.getpid(),
                    "released_at": _now_iso(),
                }
            )
        except OSError:  # pragma: no cover - best effort; the lock still drops
            pass
        finally:
            self._fd = None
            os.close(fd)

    def _write_holder(self, record: Mapping[str, Any]) -> None:
        fd = self._fd
        if fd is None:  # pragma: no cover - defensive
            return
        payload = (json.dumps(dict(record)) + "\n").encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        os.truncate(fd, 0)
        os.write(fd, payload)
        os.fsync(fd)

    @staticmethod
    def _read_holder(fd: int) -> dict[str, Any] | None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 65536).decode("utf-8", "replace").strip()
        except OSError:  # pragma: no cover - defensive
            return None
        if not raw:
            return None
        try:
            record = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return record if isinstance(record, dict) else None


def _describe_holder(holder: Mapping[str, Any] | None) -> str:
    """Name the lock's holder for a refusal message."""
    if not holder:
        return "an unidentified process (the lock file carries no holder record)"
    return (
        f"pid {holder.get('pid')} on host {holder.get('host')!r} "
        f"(execution {holder.get('execution_id')!r}, started "
        f"{holder.get('started_at')})"
    )


def _now_iso() -> str:
    from datetime import datetime, timezone  # noqa: PLC0415 - import-cheap

    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Validators used on the transport boundary
# --------------------------------------------------------------------------


def validate_batch_id(batch_id: str) -> str:
    """Return ``batch_id`` if it is a ULID, else raise.

    The reconcile and verification counts filter on ``ingest_batch_id``, and
    the Spark engine has NO bind channel, so that value reaches the SQL text.
    It is validated against :data:`ULID_RE` immediately before it does --
    the same discipline ``clearinghouse_capability`` applies to every hash.
    """
    if not isinstance(batch_id, str) or not ULID_RE.match(batch_id):
        raise ValueError(
            f"ingest: {batch_id!r} is not a ULID ingest_batch_id. It would be "
            "inlined into a COUNT(*) predicate (Spark cannot bind "
            "parameters), so anything but a validated ULID is refused."
        )
    return batch_id


def validate_hex64(value: str, *, where: str) -> str:
    """Return ``value`` if it is 64 lowercase hex characters, else raise.

    A batch's ``hash_min``/``hash_max`` bound the fill-only read's ``BETWEEN``
    predicate and are likewise inlined, so they are validated here first. An
    UPPERCASE hash would compare wrongly as well as being un-inlinable: the
    tables store lowercase hex, so a range in another case silently selects
    nothing and fill-only would fail OPEN.
    """
    if not isinstance(value, str) or not HEX64_RE.match(value):
        raise ValueError(
            f"ingest: {where} is {value!r}, not 64 lowercase hex characters. "
            "It bounds the fill-only range read and is inlined into SQL "
            "(Spark cannot bind parameters), so it is refused. A range that "
            "selects nothing would make fill-only fail OPEN and re-append "
            "every row."
        )
    return value
