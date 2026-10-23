"""Shard-ingest mechanics for the clearinghouse backfill -- the FILL-ONLY layer.

This module holds everything the ingest half of the backfill needs that is
NOT a transport call: the pure key-matching rule that makes a load idempotent,
the PRE-COMMIT JOIN ASSERTIONS that make a wrong join key a loud failure, the
ARRIVAL decision that tells sync lag from a lost file, the batch-integrity
checks that run before a file is used, the load-execution inventory and its
durable ledger, the LOAD RECEIPT, and the per-namespace OS lock. The transport
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

WHY THE JOIN ASSERTIONS AND THE ARRIVAL DECISION ARE HERE AND NOT IN THE
CAPABILITY. Both are rules about data, and both were added because fill-only's
correct behaviour -- "a batch with no surviving rows is committed without a
write at all" -- is indistinguishable from two different failures: a wrong
join key (the batch joins nothing) and a batch that has not finished syncing
(the bytes are not the published bytes). Each had a reported exit status of 0.
Putting the rules in functions over in-memory tables and inventory entries
means the counts and the deadline arithmetic can be PROVEN by test, and the
capability is then written to call a rule that is already proven. See the two
section headers below.

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

#: NON-TERMINAL: the batch's bytes did not match the sha256 ``run.json``
#: published and the TRANSFER DEADLINE had not passed, so the file is treated
#: as STILL ARRIVING rather than as corrupt. The line persists the first
#: observation -- when it was made, and what was observed -- so a retry
#: resumes the deadline from the first sighting instead of restarting
#: verification from nothing, and so the evidence in a later failed-delivery
#: report is the whole history rather than one sample.
#:
#: WHY A STATE AND NOT AN IMMEDIATE FAILURE. The producer writes the shard run
#: on one machine and a sync client carries it to the pod; the client does not
#: guarantee the producer's write ORDER, so ``run.json`` can arrive before the
#: bytes it describes (observed: the client reported a hand-off directory up
#: to date while one of its three files was absent on the consumer). A first
#: digest mismatch therefore cannot be told apart from a lost file -- only
#: ELAPSED TIME can. Alarming on the first mismatch alarms on every normal
#: sync lag; treating it as transient forever lets a genuinely lost file wait
#: silently.
LEDGER_SYNCING = "syncing"

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


class ClearinghouseJoinAssertionError(RuntimeError):
    """A batch failed the pre-commit join assertions and was not committed.

    Raised by :func:`raise_for_join_report` over the report
    :func:`join_assertions` builds. The message names every failed assertion
    AND ITS COUNT, because the counts are the whole diagnostic: "0 of 19,268
    source keys resolve" and "19,268 of 19,268 resolve" are the same code path
    with the same exit status today, and the first one is a wrong join key.
    """


class ClearinghouseTransferIncompleteError(RuntimeError):
    """A batch's bytes are not yet the bytes ``run.json`` published.

    STILL SYNCING, not corrupt: raised only while the transfer deadline has
    not passed. The correct response is to re-run the load, which resumes
    from the ledger.
    """


class ClearinghouseFailedDeliveryError(RuntimeError):
    """A batch's bytes never became the bytes ``run.json`` published.

    Raised once the transfer deadline has passed, with the evidence a human
    needs to act: which batch, expected versus observed size, expected versus
    observed digest.
    """


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
# The PRE-COMMIT join assertions -- EVALUATED ON THE INPUTS
# --------------------------------------------------------------------------
#
# WHY THIS EXISTS AND WHY IT IS NOT A RELATIONSHIP TEST ON THE JOIN'S RESULT.
# Fill-only commits a batch with no surviving rows WITHOUT a write at all
# (:meth:`ClearinghouseCapability._ingest_one_batch`), which is exactly right
# for a re-run of a landed run and exactly wrong for a wrong join key -- and
# the two are indistinguishable from the outside: both report an action, a
# zero and an exit status of 0. Measured 2026-10-21 on the `function_to_go`
# council run: the council's own ``items.entity_hash`` matched 0 of 19,268
# clearinghouse hashes. That load would have been reported as a clean
# idempotent no-op.
#
# THE TRAP, and it is why this defect class survives having tests: A
# RELATIONSHIP TEST EVALUATED OVER AN EMPTY JOIN PASSES VACUOUSLY. "every
# joined row has a non-null key", "no joined key is ambiguous" and "the joined
# rows all resolve" are all TRUE of zero rows. So every assertion below is
# evaluated on the SOURCE SIDE -- the batch's own rows, and the target's key
# space read independently -- and never on what the join returned.
#
# ALL FOUR REPORT A COUNT. The numbers are the diagnostic: "0 of 19,268
# resolve" and "19,268 of 19,268 resolve" are the same code path with the same
# exit status today, and only the count tells them apart.


#: The coverage floor the load path asserts when the caller declares none: a
#: result batch's join keys must ALL name an entity the clearinghouse already
#: holds.
#:
#: WHY 1.0 AND NOT A TOLERANCE. A ``<type>_result`` row whose ``entity_hash``
#: names no row in ``<type>_entity`` is an ORPHAN in exactly the sense
#: ``clearinghouse_capability.parameter_set_orphans`` already treats as a hard
#: failure: the hash is opaque, so nothing can say what the result is ABOUT.
#: A result load is never the thing that mints identity -- the entity and
#: content rows are -- so there is no legitimate reason for a result key not
#: to resolve, and a floor below 1.0 would only be a place for a real
#: mismatch to hide. ``--coverage-floor`` lowers it for a deliberately partial
#: load, and the report says what the measured coverage was either way.
DEFAULT_COVERAGE_FLOOR = 1.0


def resolvable_source_table(table: str) -> str | None:
    """The table a batch's join keys must RESOLVE against, or ``None``.

    ``<type>_result`` resolves against ``<type>_entity``: a result row is a
    statement ABOUT an entity the clearinghouse already knows, so its
    ``entity_hash`` must be one the entity table holds.

    ``None`` for ``<type>_entity``, ``<type>_content`` and the parameter-set
    registry, and that is not a gap: those batches are what MINTS the identity
    they carry, so there is nothing for them to resolve against and a
    coverage assertion over them would be a statement about the future. The
    other three assertions still apply to them.
    """
    if table == PARAMETER_SET_TABLE:
        return None
    if table.endswith("_result"):
        return f"{table[: -len('_result')]}_entity"
    return None


def join_assertions(
    batch: "pa.Table",
    *,
    table: str,
    batch_id: str,
    resolvable: Sequence[str] | set[str] | None = None,
    coverage_floor: float = DEFAULT_COVERAGE_FLOOR,
) -> dict[str, Any]:
    """The four pre-commit assertions over ONE batch, with a count for each.

    Evaluated entirely on the INPUTS: ``batch`` is the source rows as the
    inventory published them, and ``resolvable`` is the target's key space
    read independently of any join (see
    :meth:`ClearinghouseCapability._resolvable_join_keys`). Nothing here looks
    at a join result, which is the whole point -- see the section header.

    The four:

    1. **NON-EMPTY SOURCE.** A batch with zero rows is a PRODUCER failure, not
       a clean load. A shard run over an empty export publishes an inventory,
       passes every integrity check, appends nothing and exits zero today.
    2. **NON-NULL KEYS.** A null join key cannot match anything -- SQL
       ``NULL`` never equals ``NULL``, which :func:`fill_only_survivors`
       documents and mirrors -- so it survives the anti-join and lands as a
       row nothing can ever find again.
    3. **AT MOST ONE SOURCE ROW PER TARGET SLOT.** Two rows of one batch
       claiming the same slot tuple (:func:`key_columns_for_table`) is
       ambiguous: both are appended, and which one the current-state
       derivation picks is decided by an ``observed_at`` tie-break rather than
       by anything the producer said. The sharder deduplicates within a batch,
       so a violation is a producer bug and is reported as one.
    4. **COVERAGE AT OR ABOVE THE FLOOR.** The share of distinct non-null
       source keys that the target can resolve. ``resolvable=None`` means the
       question does not apply to this table family
       (:func:`resolvable_source_table`) and the check is recorded as skipped
       rather than silently passed.

    ZERO RESOLUTION IS FATAL WHATEVER THE FLOOR. A non-empty batch none of
    whose keys resolve is the 0-of-19,268 case, and ``--coverage-floor 0``
    must not be a way to wave it through -- a floor expresses tolerance for a
    PARTIAL mismatch, and total non-resolution is not partial.

    Args:
        batch: The batch's rows, as :func:`read_batch_table` read them.
        table: The target table, which decides the join key
            (:func:`range_column_for_table`) and the slot key
            (:func:`key_columns_for_table`).
        batch_id: Named in every failure message.
        resolvable: The target's resolvable key values, or ``None`` when the
            table family has nothing to resolve against.
        coverage_floor: The declared floor, in ``[0, 1]``.

    Returns:
        The report: ``table``, ``batch_id``, ``join_key``, ``source_rows``,
        ``source_keys`` (distinct, non-null), ``null_keys``,
        ``ambiguous_slots``, ``resolvable_keys``, ``resolved_keys``,
        ``unresolved_keys``, ``coverage``, ``coverage_floor``,
        ``coverage_checked``, ``ok`` and ``failures``.

    Raises:
        ValueError: ``batch`` has no join-key column, or ``coverage_floor`` is
            outside ``[0, 1]``. Neither can be interpreted as a passing
            assertion.
    """
    floor = float(coverage_floor)
    if not 0.0 <= floor <= 1.0:
        raise ValueError(
            f"join_assertions: coverage_floor={coverage_floor!r} is outside "
            "[0, 1]. A floor is a share of the source keys; a value outside "
            "that range cannot be compared to one."
        )
    join_key = range_column_for_table(table)
    if join_key not in batch.column_names:
        raise ValueError(
            f"join_assertions: batch {batch_id!r} for {table!r} has no join "
            f"key column {join_key!r} (columns: {batch.column_names}). "
            "Refusing -- a missing key column cannot be read as 'every key is "
            "fine'."
        )

    slot_columns = key_columns_for_table(table)
    key_values = batch.column(join_key).to_pylist()
    null_keys = sum(1 for value in key_values if value is None)
    distinct_keys = {value for value in key_values if value is not None}

    slot_values = [batch.column(column).to_pylist() for column in slot_columns]
    slot_counts: dict[tuple[Any, ...], int] = {}
    for tuple_values in zip(*slot_values):
        slot_counts[tuple_values] = slot_counts.get(tuple_values, 0) + 1
    ambiguous_slots = sum(1 for count in slot_counts.values() if count > 1)

    coverage_checked = resolvable is not None
    resolvable_keys = None if resolvable is None else len(set(resolvable))
    resolved_keys: int | None = None
    unresolved_keys: int | None = None
    coverage: float | None = None
    if coverage_checked:
        held = set(resolvable or ())
        resolved_keys = len(distinct_keys & held)
        unresolved_keys = len(distinct_keys) - resolved_keys
        coverage = (
            1.0 if not distinct_keys else resolved_keys / len(distinct_keys)
        )

    failures: list[str] = []
    if batch.num_rows == 0:
        failures.append(
            f"{table} batch {batch_id}: the expected input is EMPTY (0 source "
            "rows). A shard run with zero source rows is a PRODUCER failure, "
            "not a clean load -- an empty export shards, passes every "
            "integrity check, appends nothing and exits zero, which is "
            "indistinguishable from a re-run of a landed run."
        )
    if null_keys:
        failures.append(
            f"{table} batch {batch_id}: {null_keys} of {batch.num_rows} "
            f"source row(s) have a NULL {join_key}. SQL NULL never equals "
            "NULL, so such a row matches nothing, survives the fill-only "
            "anti-join and lands as a row nothing can find again."
        )
    if ambiguous_slots:
        failures.append(
            f"{table} batch {batch_id}: {ambiguous_slots} target slot(s) "
            f"({', '.join(slot_columns)}) are claimed by more than one source "
            "row, so a source key maps to more than one row for one slot. "
            "Which of them the current-state derivation picks is then decided "
            "by an observed_at tie-break rather than by the producer. The "
            "sharder deduplicates within a batch, so this is a producer bug."
        )
    if coverage_checked and distinct_keys and resolved_keys == 0:
        failures.append(
            f"{table} batch {batch_id}: ZERO of {len(distinct_keys)} distinct "
            f"{join_key} value(s) resolve against "
            f"{resolvable_source_table(table)} (which holds "
            f"{resolvable_keys} key(s) in this batch's hash range). A "
            "non-empty source that resolves nothing is a WRONG JOIN KEY, not "
            "an idempotent no-op, and no coverage floor waives it."
        )
    elif coverage_checked and coverage is not None and coverage < floor:
        failures.append(
            f"{table} batch {batch_id}: {resolved_keys} of "
            f"{len(distinct_keys)} distinct {join_key} value(s) resolve "
            f"against {resolvable_source_table(table)} -- coverage "
            f"{coverage:.6f}, below the declared floor {floor:.6f}. "
            f"{unresolved_keys} key(s) name no entity the clearinghouse "
            "holds, and a result row whose entity nothing can find is an "
            "orphan."
        )

    return {
        "table": table,
        "batch_id": batch_id,
        "join_key": join_key,
        "slot_key": list(slot_columns),
        "source_rows": batch.num_rows,
        "source_keys": len(distinct_keys),
        "null_keys": null_keys,
        "ambiguous_slots": ambiguous_slots,
        "resolvable_keys": resolvable_keys,
        "resolved_keys": resolved_keys,
        "unresolved_keys": unresolved_keys,
        "coverage": coverage,
        "coverage_floor": floor,
        "coverage_checked": coverage_checked,
        "ok": not failures,
        "failures": failures,
    }


def raise_for_join_report(report: Mapping[str, Any]) -> None:
    """Raise :class:`ClearinghouseJoinAssertionError` when a report failed.

    Separate from :func:`join_assertions` because the load path LEDGERS the
    failure and carries on to the next batch rather than unwinding the whole
    execution -- one refused batch does not invalidate the batches that
    already committed, and a ledgered failure blocks
    :meth:`ClearinghouseCapability.verify_run` so it cannot be lost. Callers
    that want the exception (and the pure tests) use this.
    """
    failures = list(report.get("failures") or ())
    if failures:
        raise ClearinghouseJoinAssertionError(
            "ingest: the pre-commit join assertions refused this batch "
            "BEFORE anything was committed:\n  - " + "\n  - ".join(failures)
        )


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
    observed_sha256: str | None = None,
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
        observed_sha256: The file's digest, when the caller has ALREADY
            streamed it -- :func:`transfer_state` does, immediately before
            this, to decide whether the bytes have arrived. Passed through so
            a full-size batch is hashed ONCE per load rather than twice; the
            check itself is unchanged, and omitting it simply hashes here.

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
    actual = file_sha256(path) if observed_sha256 is None else observed_sha256
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
                    # Carried for the ARRIVAL check's expected-versus-observed
                    # size evidence. None for an inventory written before the
                    # sharder recorded it.
                    "bytes": source.get("bytes"),
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


def assert_unit_in_inventory(
    inventory: Sequence[Mapping[str, Any]],
    unit: str,
    *,
    shard_dir: Path | str,
) -> None:
    """Refuse a ``--unit`` that names no unit in the execution's inventory.

    A typo'd unit label would otherwise be a NO-OP that reports success: every
    entry is skipped, no batch is ingested, the envelope says zero batches and
    exits zero. An operator loading 256 units one at a time would read that as
    "already done". So the label is checked against what the inventory
    actually holds, and the available labels are named in the error.

    Raises:
        ClearinghouseBatchIntegrityError: ``unit`` matches no inventory entry.
    """
    present = [str(entry.get("unit_id")) for entry in inventory]
    if unit in present:
        return
    available = sorted(dict.fromkeys(present))
    raise ClearinghouseBatchIntegrityError(
        f"ingest: --unit {unit!r} names no unit in the run at {shard_dir}. "
        f"Its inventory holds {len(available)} unit(s): "
        f"{available[:8]}{', ...' if len(available) > 8 else ''}. Refusing "
        "rather than ingesting nothing and exiting zero, which an operator "
        "loading units one at a time would read as 'already done'."
    )


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
# Arrival -- "run.json exists" IS NOT PROOF THAT THE BATCHES ARRIVED
# --------------------------------------------------------------------------
#
# The producer shards on one machine and a sync client carries the run
# directory to the pod. TWO THINGS ARE NOT TRUE OF THAT HAND-OFF, and the
# marker-file reflex depends on both:
#
#   * The client does not guarantee the PRODUCER'S WRITE ORDER, so ``run.json``
#     -- the file that describes the batches -- can arrive before the bytes it
#     describes. The marker is therefore not a completeness signal.
#   * The client's own "up to date" is not a completeness signal either:
#     observed on the previous hand-off, the client reported the directory up
#     to date while one of its three files was absent on the consumer.
#
# The five-check ingest gate (:func:`verify_batch_file`) already verifies each
# batch's sha256, so the detection is in place. WHAT WAS MISSING IS WHAT
# HAPPENS WHEN IT FAILS, and neither extreme is safe:
#
#   * Treating the first mismatch as CORRUPTION alarms on every normal sync
#     lag, so the alarm gets ignored and then the real one does too.
#   * Treating it as TRANSIENT forever means a genuinely lost file waits
#     silently and the load never completes and never says why.
#
# A FIRST OBSERVATION CANNOT TELL THEM APART. Only elapsed time can. So the
# first mismatch is ``syncing`` -- retry, with the first sighting PERSISTED
# (:data:`LEDGER_SYNCING`) so the retry resumes the clock instead of
# restarting verification from nothing -- and past the TRANSFER DEADLINE the
# same mismatch is a FAILED DELIVERY, raised with evidence.


#: How long after ``run.json``'s mtime a missing or mismatched batch is still
#: "arriving". Six hours: the 2026-10-20 hand-off's shard directory is ~19k
#: rows of result parquet and a cross-machine sync of that size completes in
#: minutes, so six hours is an order of magnitude of slack and still bounded
#: well inside the day an operator would wait before asking.
DEFAULT_TRANSFER_DEADLINE_SECONDS = 6 * 60 * 60

#: The batch file is not there at all.
TRANSFER_ABSENT = "absent"

#: The batch file is there and its bytes are not the published bytes.
TRANSFER_DIGEST_MISMATCH = "digest_mismatch"


def run_manifest_mtime(shard_dir: Path | str) -> float | None:
    """``run.json``'s mtime as a POSIX timestamp, or ``None`` when absent.

    The transfer deadline is measured from THE MARKER'S OWN mtime and not from
    now, nor from when the load happened to start: the marker is written by
    the producer at the end of the shard run, so it dates the hand-off. A
    deadline measured from the load's start would reset every time an operator
    re-ran the command, which is precisely the silent wait this guards
    against.
    """
    from .clearinghouse_shard import RUN_MANIFEST_FILE  # noqa: PLC0415

    path = Path(shard_dir) / RUN_MANIFEST_FILE
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def transfer_state(
    entry: Mapping[str, Any],
    *,
    shard_root: Path,
    manifest_mtime: float | None,
    now: float | None = None,
    deadline_seconds: int = DEFAULT_TRANSFER_DEADLINE_SECONDS,
    first_observed_at: str | None = None,
) -> dict[str, Any]:
    """Classify one batch's arrival: arrived, ``syncing`` or failed delivery.

    ``state == "arrived"`` means the file is present and its bytes ARE the
    bytes the inventory published -- the normal five-check gate takes over.
    Use :func:`has_arrived` to ask that question; the digest it streamed
    rides back in ``observed_sha256`` so the gate reuses it.

    Otherwise the return carries the EVIDENCE a human needs, named in the
    report and in the eventual exception: which batch, expected versus
    observed size, expected versus observed digest.

    Args:
        entry: The batch's inventory entry (``file``, ``sha256``, ``bytes``).
        shard_root: The directory ``run.json`` describes.
        manifest_mtime: ``run.json``'s mtime (:func:`run_manifest_mtime`).
            ``None`` -- the marker is gone -- makes any mismatch a failed
            delivery immediately: without the marker's date there is no clock
            to be patient against, and a shard directory whose own inventory
            vanished mid-load is not a transfer in progress.
        now: Override the clock, for tests.
        deadline_seconds: The declared deadline.
        first_observed_at: The ``first_observed_at`` a previous ``syncing``
            line persisted. Carried through so the evidence is the whole
            history; the deadline itself is measured from ``manifest_mtime``.

    Returns:
        ``{"state": "arrived", "observed_sha256": ...}`` when the bytes are
        the published bytes -- the digest travels back so the five-check gate
        does not stream a full-size batch a second time -- or a dict with
        ``state`` (``"syncing"`` or ``"failed_delivery"``), ``reason``,
        ``batch_id``, ``file``,
        ``expected_sha256``, ``observed_sha256``, ``expected_bytes``,
        ``observed_bytes``, ``deadline_seconds``, ``elapsed_s``,
        ``manifest_mtime``, ``deadline_at`` and ``first_observed_at``.
    """
    from datetime import datetime, timezone  # noqa: PLC0415 - import-cheap

    relative = str(entry.get("file") or "")
    batch_id = str(entry.get("batch_id"))
    expected_sha = str(entry.get("source_sha256") or entry.get("sha256") or "")
    expected_bytes = entry.get("bytes")
    expected_bytes = (
        int(expected_bytes) if isinstance(expected_bytes, int) else None
    )

    observed_bytes: int | None = None
    observed_sha: str | None = None
    reason: str | None = None
    if not relative:
        reason = TRANSFER_ABSENT
    else:
        try:
            path = resolve_batch_path(shard_root, relative)
        except ClearinghouseBatchIntegrityError:
            # A path that escapes the root is NOT an arrival problem and must
            # not be waited out -- the normal gate refuses it outright, and no
            # amount of waiting could turn it into a batch this run published.
            # Reported as arrived WITH NO DIGEST, so the gate re-reads (and
            # refuses) rather than being handed a digest for a file outside
            # the root.
            return {"state": "arrived", "observed_sha256": None}
        if not path.is_file():
            reason = TRANSFER_ABSENT
        else:
            observed_bytes = path.stat().st_size
            observed_sha = file_sha256(path)
            if not expected_sha or observed_sha == expected_sha:
                # Either it matches, or the inventory published no digest at
                # all -- which is a different refusal, made by the normal
                # gate, and not something a wait could fix. The digest is
                # handed back so the gate does not stream the file a SECOND
                # time: a full-size batch is sized for a Spark append, not for
                # being hashed twice per load.
                return {"state": "arrived", "observed_sha256": observed_sha}
            reason = TRANSFER_DIGEST_MISMATCH

    clock = datetime.now(timezone.utc).timestamp() if now is None else float(now)
    elapsed = None if manifest_mtime is None else max(0.0, clock - manifest_mtime)
    expired = elapsed is None or elapsed > float(deadline_seconds)
    observed_iso = datetime.fromtimestamp(clock, timezone.utc).isoformat()
    return {
        "state": "failed_delivery" if expired else "syncing",
        "reason": reason,
        "batch_id": batch_id,
        "table": entry.get("table"),
        "file": relative,
        "expected_sha256": expected_sha or None,
        "observed_sha256": observed_sha,
        "expected_bytes": expected_bytes,
        "observed_bytes": observed_bytes,
        "deadline_seconds": int(deadline_seconds),
        "manifest_mtime": (
            None
            if manifest_mtime is None
            else datetime.fromtimestamp(manifest_mtime, timezone.utc).isoformat()
        ),
        "deadline_at": (
            None
            if manifest_mtime is None
            else datetime.fromtimestamp(
                manifest_mtime + float(deadline_seconds), timezone.utc
            ).isoformat()
        ),
        "elapsed_s": elapsed,
        "first_observed_at": first_observed_at or observed_iso,
        "last_observed_at": observed_iso,
    }


def has_arrived(state: Mapping[str, Any]) -> bool:
    """Whether a :func:`transfer_state` verdict says the bytes are here."""
    return state.get("state") == "arrived"


def describe_transfer(state: Mapping[str, Any]) -> str:
    """The one-line evidence sentence for a ``syncing``/failed-delivery state.

    Names the batch, expected versus observed size and expected versus
    observed digest -- the three things a human needs to decide whether to
    wait, re-sync or re-shard -- and says which of the two verdicts it is and
    why.
    """
    reason = state.get("reason")
    where = f"{state.get('table')} batch {state.get('batch_id')} ({state.get('file')})"
    if reason == TRANSFER_ABSENT:
        observed = "the file is ABSENT"
    else:
        observed = (
            f"observed sha256 {state.get('observed_sha256')}, "
            f"{state.get('observed_bytes')} byte(s)"
        )
    expected = (
        f"expected sha256 {state.get('expected_sha256')}, "
        f"{state.get('expected_bytes')} byte(s)"
    )
    if state.get("state") == "syncing":
        return (
            f"{where}: {observed}; {expected}. Treating this as STILL "
            f"SYNCING -- run.json's mtime is {state.get('manifest_mtime')} and "
            f"the transfer deadline is {state.get('deadline_at')} "
            f"({state.get('deadline_seconds')}s). First observed "
            f"{state.get('first_observed_at')}. Re-run the load; the ledger "
            "resumes from here and the batches that already committed are "
            "skipped."
        )
    return (
        f"FAILED DELIVERY. {where}: {observed}; {expected}. The transfer "
        f"deadline ({state.get('deadline_seconds')}s from run.json's mtime "
        f"{state.get('manifest_mtime')}, i.e. {state.get('deadline_at')}) has "
        f"passed -- first observed {state.get('first_observed_at')}, still "
        f"wrong at {state.get('last_observed_at')}. This is a lost file, not "
        "sync lag: re-sync the shard directory from the producer, or re-shard."
    )


def raise_for_transfer(state: Mapping[str, Any]) -> None:
    """Raise the right error for a transfer state, with the evidence in it."""
    if has_arrived(state):
        return
    message = describe_transfer(state)
    if state.get("state") == "syncing":
        raise ClearinghouseTransferIncompleteError(message)
    raise ClearinghouseFailedDeliveryError(message)


# --------------------------------------------------------------------------
# THE RECEIPT -- verify's envelope, reconciled against ITEMS SUBMITTED
# --------------------------------------------------------------------------
#
# "Loaded 19,268" is not a complete reconciliation when the plan submitted
# 19,276: THE EIGHT RESIDUAL ITEMS ARE THE INTERESTING NUMBER, and a row count
# cannot name them because it never knew about them. An item can also carry
# several GO terms, so a ``function_result`` ROW count is not an ITEM count
# either, and the receipt reports the two separately rather than letting one
# stand in for the other.
#
# IT MUST BE RE-EXPORTABLE, NOT A ONE-SHOT. The Iceberg commit and the
# ``LOADED.json`` write are two unrelated writes with no transaction between
# them, so a crash in the gap loses the ACKNOWLEDGEMENT while the data sits
# safely landed -- and a producer that read "no receipt" as "not loaded" would
# ask for a reload of rows that are already there. Every field below is
# derived from the ingest ledger, the execution's inventory, the target's
# snapshot history and the producer's own publish record, so re-running
# ``verify`` reproduces the SAME receipt. There is deliberately no timestamp
# and no run-specific nonce in the body: the envelope that carries it has a
# ``generated_at``, and a receipt that changed every time it was exported
# could not be compared to the one that was lost.


#: The receipt's filename in the producer's run directory. ``verify`` runs
#: in-pod; the run directory is the synced surface the producer reads back.
RECEIPT_FILENAME = "LOADED.json"

#: Bumped only on a BREAKING change to the receipt body.
RECEIPT_VERSION = 1

#: The producer's stage-5 record, beside the shard directory it published.
#: Written by ``kbu ontomap council-publish``; the receipt reads the item
#: accounting out of it because ITEMS ARE NOT IN THE SHARD RUN -- an item with
#: no verdict produced no row, so it is invisible to anything downstream of
#: the export, and those are exactly the items the reconciliation is about.
PUBLISH_RECORD_FILENAME = "PUBLISH.json"


def read_publish_record(shard_dir: Path | str) -> dict[str, Any]:
    """The producer's ``PUBLISH.json`` from beside ``shard_dir``, or ``{}``.

    Beside and not inside: the shard directory is the sharder's output and its
    inventory is ``run.json``, which this must not add a second manifest to.
    ``{}`` -- a shard run published by something other than ``council-publish``
    -- is a degraded but honest answer, and the receipt says so in its notes
    rather than inventing an item count.
    """
    path = Path(shard_dir).resolve().parent / PUBLISH_RECORD_FILENAME
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return dict(raw) if isinstance(raw, dict) else {}


def build_receipt(
    verify_result: Mapping[str, Any],
    *,
    publish_record: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the load receipt from a :meth:`verify_run` result.

    Pure, and derived ONLY from inputs that do not change between exports --
    see the section header for why that is the requirement rather than a
    nicety.

    Args:
        verify_result: What :meth:`ClearinghouseCapability.verify_run`
            returned.
        publish_record: The producer's ``PUBLISH.json``
            (:func:`read_publish_record`). Absent, the ITEM half of the
            reconciliation is reported as ``None`` with a note, never as the
            row count -- an item count a receipt guessed would be worse than
            one it declined to state.

    Returns:
        The receipt: ``receipt_version``, ``execution_id``, ``source_run_id``,
        ``namespace``, ``items_submitted``, ``items_with_verdict``,
        ``items_unresolved``, ``result_rows_written``, ``declines_by_reason``,
        ``snapshot_ids``, ``parameter_set_hashes``, ``reconciled``, ``notes``,
        ``ok`` and ``discrepancies``.
    """
    publish = dict(publish_record or {})
    counts = dict(publish.get("counts") or {})

    def _count(key: str) -> int | None:
        value = counts.get(key)
        return int(value) if isinstance(value, int) else None

    items_submitted = _count("items_submitted")
    items_with_verdict = _count("items_with_verdict")
    items_unresolved = _count("items_unresolved")

    batches = list(verify_result.get("batches") or ())
    result_rows = 0
    snapshot_ids: dict[str, list[str]] = {}
    for report in verify_result.get("tables") or ():
        name = str(report.get("name"))
        snapshot_ids[name] = [str(value) for value in report.get("snapshot_ids") or ()]
    for report in batches:
        table = str(report.get("name"))
        rows = report.get("ledger_rows")
        if (
            table.endswith("_result")
            and table != PARAMETER_SET_TABLE
            and isinstance(rows, int)
        ):
            result_rows += rows

    notes: list[str] = []
    if not publish:
        notes.append(
            "no "
            + PUBLISH_RECORD_FILENAME
            + " beside the shard directory, so the ITEM half of the "
            "reconciliation is unknown. The row counts below are complete; "
            "items submitted, items with a verdict and items unresolved are "
            "null rather than stood in for by a row count -- one item can "
            "carry several result rows, so the two are not the same number."
        )
    if (
        items_submitted is not None
        and items_with_verdict is not None
        and items_unresolved is not None
        and items_submitted != items_with_verdict + items_unresolved
    ):
        notes.append(
            f"the publish record does not add up: {items_submitted} item(s) "
            f"submitted but {items_with_verdict} with a verdict plus "
            f"{items_unresolved} unresolved. The producer's own accounting is "
            "wrong, so neither number can be reconciled against the rows."
        )
    if items_with_verdict is not None and result_rows < items_with_verdict:
        notes.append(
            f"{result_rows} result row(s) landed for {items_with_verdict} "
            "item(s) with a verdict. One item can carry several result rows, "
            "so more rows than items is normal -- FEWER is not, and means "
            "items that had an answer did not land."
        )
    if items_unresolved:
        notes.append(
            f"{items_unresolved} item(s) are UNRESOLVED: they were submitted "
            "and have no verdict, so no row was published for them and the "
            "next plan will re-select them. They are not a load failure; they "
            "are the residual the next run has to pick up."
        )

    reconciled = (
        items_submitted is not None
        and items_with_verdict is not None
        and items_unresolved is not None
        and items_submitted == items_with_verdict + items_unresolved
        and result_rows >= items_with_verdict
    )
    return {
        "receipt_version": RECEIPT_VERSION,
        "execution_id": verify_result.get("execution_id")
        or verify_result.get("run_id"),
        "source_run_id": verify_result.get("source_run_id"),
        "namespace": verify_result.get("namespace"),
        "unit": verify_result.get("unit"),
        "items_submitted": items_submitted,
        "items_with_verdict": items_with_verdict,
        "items_unresolved": items_unresolved,
        "result_rows_written": result_rows,
        "declines_by_reason": dict(publish.get("declines_by_reason") or {}),
        "snapshot_ids": snapshot_ids,
        "parameter_set_hashes": [
            str(value) for value in verify_result.get("parameter_set_hashes") or ()
        ],
        "reconciled": reconciled,
        "notes": notes,
        "ok": bool(verify_result.get("ok")),
        "discrepancies": [str(line) for line in verify_result.get("discrepancies") or ()],
    }


def write_receipt(path: Path | str, receipt: Mapping[str, Any]) -> Path:
    """Write a receipt atomically, and return where it went.

    Atomic for the reason every file on the synced run surface is: the
    producer reads this back to decide whether a reload is needed, and a
    half-written receipt is worse than none -- it would be parsed.
    """
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(receipt), indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


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
