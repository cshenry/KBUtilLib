"""The bronze sharder and the source-adapter interface beneath the manifest.

This module is the SHARD stage of a clearinghouse bootstrap. It reads the
sources a :func:`~kbutillib.domains.kbase.berdl.clearinghouse_manifest.shard_plan`
names through a SOURCE ADAPTER, standardizes and hashes every record through
:mod:`kbutillib.domains.identity.standardizers` (never its own rule), stamps
:data:`~kbutillib.domains.identity.standardizers.STANDARDIZER_VERSION` on every
row, sorts each shard by ``entity_hash``, and writes bronze parquet laid out
ONE DIRECTORY PER TARGET TABLE with one file per batch, named for that
batch's ULID ``ingest_batch_id``.

WHY SHARD IS A SEPARATE STAGE FROM INGEST -- do not merge them for tidiness.
Hashing billions of sequences is CPU-bound, and the pod (a rootless
JupyterHub container that restarts routinely) is the WEAKEST machine in the
fleet for it. Sharding needs CPU, disk and NO CREDENTIALS, so it runs anywhere
-- h100, poplar, a laptop for a small source. Ingest needs the in-pod write
path. Bronze shards on disk are also re-ingestible: if an ingest fails, the
expensive hashing work survives on disk. Consequently this module makes NO
network call, needs NO token, and MUST NOT import ``berdl_notebook_utils`` or
``data_lakehouse_ingest`` at module scope or anywhere else.

THE ADAPTER CONTRACT, and why it makes adapters interchangeable. Chris named
the two real bootstrap sources in review and NEITHER IS A FILE: a ~300M-protein
MongoDB store on poplar, and ``kbaseincubator.genome_clearhouse`` on the
lakehouse. A file-reading verb cannot reach either. The fix is an adapter layer
BENEATH the manifest, not a wider manifest: an adapter yields records; the
sharder standardizes, hashes, sorts and writes them the SAME way regardless of
source. Three implementations are PLANNED -- ``file``, ``mongo``, ``lakehouse``
-- and all three are now built here. An adapter
NEVER writes to Iceberg itself -- there is exactly one write path and it is the
``load`` verb on
:class:`~kbutillib.domains.kbase.berdl.clearinghouse_capability.ClearinghouseCapability`.
Where an adapter RUNS follows its source: ``file`` needs only CPU and disk;
``mongo`` must run where it can reach poplar; ``lakehouse`` must run in-pod.
Only ``file`` and ``mongo`` are credential-free. No machine is hardcoded
anywhere.

SORTING IS NOT OPTIONAL, AND IT IS THE ONE IRREVERSIBLE THING THIS STAGE
DOES. The clearinghouse's dominant read is a random 64-hex-character EQUALITY
probe against a 1-4 billion row table. Iceberg prunes data files using per-file
min/max column statistics in its manifests. On randomly scattered hashes those
statistics prune NOTHING and every probe approaches a full scan; on clustered
hashes Iceberg's own docs cite up to 10x from file pruning. So each shard is
sorted by ``entity_hash`` and each shard covers a CONTIGUOUS hash range, giving
every data file a NARROW ``entity_hash`` min/max -- exactly the statistic a
point probe prunes on. This CANNOT be fixed later: Iceberg table properties are
metadata and can be set afterwards, partition specs can be evolved, code is
free to change, but the physical order of rows inside already-written data
files is none of those -- changing it is a full ``rewrite_data_files`` over the
whole table, a multi-terabyte replay at this scale. Every other decision in
this PRD is reversible; this one is paid once, at load time. And it is CHEAP
here: a local sort of a ~512 MB shard is nothing next to the hashing the
sharder already does.

DO NOT try to set an Iceberg sort order, ``write.distribution-mode``,
``write.target-file-size-bytes``, or a Parquet Bloom filter from here.
``table_configs()`` emits only ``name``, ``schema_sql`` and an optional
``partition_by`` -- there is NO properties key, so table properties are not
expressible through this write path at all (the same shape as the BINARY gap
that produced dev 1219). What this stage controls is the BYTES it writes; it
uses exactly that -- physical row order within each file.

THE RANGE-CUTTING ALGORITHM IS PINNED -- do not invent another. The sharder
must emit shards sorted by ``entity_hash`` and covering contiguous ranges, and
it cannot globally sort 300M rows nor sample before streaming. It does not need
to: ``entity_hash`` is a sha256 digest, so it is UNIFORMLY DISTRIBUTED across
the hex space by construction -- the very property that makes it useless as a
partition key makes equal-width lexicographic ranges hold approximately equal
numbers of DISTINCT HASHES, with no sampling and no prior pass. The steps:

  1. Estimate total bytes for the target table.
  2. ``N = ceil(total_bytes / target_bytes)``.
  3. Cut ``[00..00, ff..ff]`` into ``N`` equal-width ranges -- boundaries are
     COMPUTED, not observed.
  4. Stream the source ONCE into a bounded SORT BUFFER, routing each built row
     to its range by the leading characters of its key. When the buffer
     reaches ``target_bytes`` it is bucketed by range, each bucket sorted and
     appended as one more SORTED RUN file on disk.
  5. Produce each range, one at a time, by an EXTERNAL k-way merge of its
     runs, dropping duplicates and cutting output batches as the merged
     stream goes by.

EQUAL HASHES ARE NOT EQUAL ROW COUNTS, AND STEP 5 IS AN EXTERNAL MERGE FOR
THAT REASON. Uniformly distributed distinct hashes do NOT imply uniformly
distributed RECORDS: a function corpus maps millions of records onto a few
thousand repeated strings, so one hash -- hence one range -- can carry
millions of rows. The sharder therefore never sizes a range from an estimate
and sorts it in memory; it never holds a range at all. What it holds is the
sort buffer (bounded by ``target_bytes``), the merge frontier (bounded
absolutely by :data:`_MERGE_ROW_BUDGET` rows, because the per-run read batch
shrinks as runs are added, and by :data:`_MAX_MERGE_FANIN` open files,
because a wider range is reduced in multi-pass groups first) and the output
batch being accumulated. Peak residency is independent of source size.

SKEW IS BOUNDED, NOT ASSUMED AWAY: an output batch is cut as soon as it
reaches 1.8x ``target_bytes`` (:data:`SKEW_SPLIT_FACTOR`, Iceberg's own
rewrite threshold), so a range that runs large yields several bounded batches
rather than one over-large shard, and each is reported with ``split=True``.
Range boundary ties go to the LOWER range, always.

ENFORCE IT, DO NOT TRUST IT: :func:`_assert_sorted_and_disjoint` reads every
emitted shard back and asserts its key column is ascending and that shard
ranges are non-overlapping apart from a shared BOUNDARY value (adjacent
batches may touch at one hash, because a result table's sort key is the
five-column slot key and a cut can fall between two slots of one hash), and
FAILS THE BUILD if not.

EVERY ROW CARRIES A NON-NULL ``observed_at`` AND A PER-BATCH ULID
``ingest_batch_id``. Those are the two columns
:mod:`clearinghouse_derivation` orders the current-state window function by,
so a null in the first or a constant in the second makes "the current row"
for a slot undefined. ``observed_at`` is the source's declared
``observed_at_column`` (normalised to UTC) or the run's start time;
``ingest_batch_id`` is a ULID minted per OUTPUT BATCH and equal to that
batch's file stem, which also makes a name collision between two sources,
two slices or two runs impossible.

THE RUN INVENTORY. ``<out>/run.json`` is an APPEND-ONLY record of the whole
run: every batch with its sha256, row counts, key-column range, contributing
sources, ``unit_id`` and ``attempt_id``, plus per-source skip counts and
whether each source's unit was EXHAUSTED. A unit's batches are published
atomically (temp file + :func:`os.replace`) when that unit finishes, and a
published entry is never rewritten -- so anything in the inventory is final
even while the run continues. See :func:`publish_unit` and, for addressing
one unit, :func:`unit_ids` / :func:`batches_for_unit`.

TARGET FILE SIZE. :data:`DEFAULT_TARGET_BYTES` is ~512 MB -- the conventional
Iceberg target-file neighbourhood, coarse enough that per-batch ledger overhead
is negligible and fine enough that a pod restart during ingest costs minutes
rather than hours. It is configurable (``target_bytes``). This number is NOT
backed by a measurement at this scale; it is a convention, stated as such.

THE PARAMETER-SET REGISTRY IS WRITTEN BY THIS STAGE, AND FIRST. Every
``<type>_result`` row carries a ``parameter_set_hash``, stamped from the
plan's ``parameter_set`` (validated and hashed ONCE at plan time -- see
:func:`clearinghouse_manifest._plan_parameter_set`, never per row). The hash
alone is opaque, so :func:`shard_manifest` also emits a shard for the
sixteenth table, the parameter-set registry
(:data:`~clearinghouse_schema.PARAMETER_SET_TABLE`), holding one row per
DISTINCT parameter set the run's result sources used. The registry has NO
``entity_hash``: it is keyed, sorted, deduplicated and range-bounded on
``parameter_set_hash``, and each batch records which column its bounds refer
to. It is written as IMMUTABLE BATCHES PER UNIT -- a unit emits only the sets
it is the first to discover, so "one registry shard per run" means one
logical table that may hold many batch files. It is written BEFORE
any result shard, and ingested before any result table
(:meth:`~clearinghouse_capability.ClearinghouseCapability.ingest_shards`
orders it first), so no result row ever exists whose hash the registry
cannot explain. That ordering is a WRITER obligation on every write path,
not a reader's problem.
"""

from __future__ import annotations

import gzip
import hashlib
import heapq
import json
import math
import os
import re
import secrets
import shutil
import tarfile
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from ...identity import ParameterSetError, standardizers
from . import clearinghouse_derivation as cd
from . import clearinghouse_manifest as cm
from . import clearinghouse_parameters as cp
from . import clearinghouse_schema as schema

#: Conventional Iceberg target file size, ~512 MB. See the module docstring:
#: convention, not a measurement at this scale.
DEFAULT_TARGET_BYTES = 512 * 1024 * 1024

#: Iceberg's own rewrite threshold. A spill file larger than
#: ``SKEW_SPLIT_FACTOR * target_bytes`` is split at its median hash rather than
#: written as one over-large shard.
SKEW_SPLIT_FACTOR = 1.8

#: The page size the off-pod read transport materialises per request, pinned
#: from the confront-round-2 review. The value earns its precision from an
#: observed "-0 spike" at exactly this size; it is OBSERVED and enforced by NO
#: code we own, so it is overridable per adapter. The paging loop that uses it
#: (in an adapter that pages) MUST continue on an exactly-full page -- that is
#: what makes a wrong constant safe rather than silently truncating a source.
OFF_POD_PAGE_SIZE = 5000

#: Width of the hex entity_hash. A shard's routing key is the leading hex
#: characters of this digest.
_HASH_HEX_CHARS = 64


# --------------------------------------------------------------------------
# Source-adapter interface
# --------------------------------------------------------------------------


class SourceAdapter(ABC):
    """The interface every source adapter implements. ``file``, ``lakehouse``
    and ``mongo`` are all built.

    An adapter's ONLY job is to yield the raw records of a source as plain
    ``dict`` rows keyed by source column name. It NEVER hashes, sorts, writes
    parquet, or touches Iceberg -- the sharder does all of that, identically,
    for whatever an adapter yields. That is the seam that makes ``file``,
    ``mongo`` and ``lakehouse`` interchangeable: the mapping half of a manifest
    (which target table, which columns, which @derivations) is adapter
    -independent, and only the source-locating keys differ.

    Where an adapter runs follows its source (see the module docstring). This
    base class hardcodes no machine and imports no driver.
    """

    #: The manifest ``adapter`` key this class implements. Subclasses set it.
    adapter_name: str = ""

    def __init__(self, source: cm.Source):
        self.source = source
        #: Per-reason counts of records this adapter itself declined to yield.
        #: Subclasses increment; :meth:`skipped` exposes it. The sharder MERGES
        #: this with its own skip counts so one source's total is one number.
        self._skipped: dict[str, int] = {}
        #: ``None`` until ``iter_records`` has been entered, ``False`` while it
        #: is mid-flight or was abandoned, ``True`` only when the adapter drove
        #: its unit to natural exhaustion. See :meth:`exhausted`.
        self._exhausted: bool | None = None
        #: The backfill RUN CONTEXT, or ``None`` when the adapter is driven
        #: directly (a plain ``kbu clearinghouse shard``, or a test). See
        #: :meth:`bind_run_context`.
        self.run_context: Any = None
        #: The unit label :meth:`restrict_to_unit` was given, or ``None``.
        self._unit: str | None = None
        #: The manifest's ``[backfill]`` table, or ``None``. See
        #: :meth:`bind_backfill_info`.
        self.backfill_info: cm.BackfillInfo | None = None
        #: THIS READER's remaining ``--limit`` allowance. ``None`` until
        #: resolved from the run context on the first record, then a count or
        #: ``-1`` for unlimited. See :meth:`_admit_record` for why the cap is
        #: per reader rather than per run.
        self._budget_remaining: int | None = None

    @abstractmethod
    def iter_records(self) -> Iterator[Mapping[str, Any]]:
        """Yield source records as dict rows keyed by source column name."""

    @abstractmethod
    def estimate_bytes(self) -> int:
        """Return an estimate of the source's total size in bytes.

        Used only to choose the shard COUNT (``ceil(total / target_bytes)``);
        it need not be exact, because equal-width hash ranges self-balance and
        the skew guard catches any range that still runs large. An adapter that
        cannot cheaply size its source may return an upper-ish estimate.
        """

    def estimate_rows(self) -> int | None:
        """Return a CHEAP estimated row count, or ``None`` if there is none.

        "Cheap" is the whole contract: metadata, not a scan. ``kbu
        clearinghouse plan`` reports this, and plan MUST NOT iterate records --
        counting a 264M-document source to print a number costs the same read
        as the sharding run itself, which is why ``plan`` used to be unusable
        on a real source. An adapter with no metadata path returns ``None`` and
        ``plan`` reports the count as unavailable rather than paying for it.

        The default is ``None``, so an adapter that cannot do this cheaply
        implements nothing.
        """
        return None

    def estimate_rows_method(self) -> str:
        """Name the mechanism :meth:`estimate_rows` used, for the plan envelope.

        An estimate whose PROVENANCE is unstated cannot be judged: "6" from
        parquet footer metadata is exact, "6" from a sampled statistic is not.
        ``plan`` prints this as ``estimate_method`` beside the count. The
        default pairs with the default ``estimate_rows`` of ``None``.
        """
        return "unavailable"

    def skipped(self) -> dict[str, int]:
        """Return per-reason counts of records the ADAPTER declined to yield.

        Keys are short stable reason slugs (the sharder's own are
        ``null-observed_at`` and ``shard-value-error``); values are counts. The
        sharder merges this mapping with its own per-source counts into
        ``run.json``'s ``skipped`` list, so a source that dropped records never
        reports a silently shorter row count with no explanation.

        The default returns an empty mapping, so an adapter that skips nothing
        implements nothing.
        """
        return dict(self._skipped)

    def exhausted(self) -> bool | None:
        """Whether the last :meth:`iter_records` drained this adapter's UNIT.

        Recorded in ``run.json`` per source, SEPARATELY from the emitted and
        accepted row counts, because the two answer different questions. "I
        emitted 5,000,000 rows" says nothing about whether the source held
        5,000,000 or 264,000,000: a cursor killed at five million and a
        collection that genuinely held five million produce identical counts.
        Only exhaustion distinguishes "this unit is complete" from "this unit
        stopped", and completeness is what a later ingest or a resume decision
        turns on.

        A ``--limit``/early-break stop is explicitly NOT exhaustion: a caller
        that stops consuming the iterator leaves this ``False``, because the
        flag is set only when the adapter's own loop runs off the end of its
        source.

        Returns ``True`` (drained), ``False`` (entered but not drained), or
        ``None`` (never iterated).
        """
        return self._exhausted

    # -- units of work ------------------------------------------------------

    def units(self) -> tuple[str, ...]:
        """The UNIT LABELS this source decomposes into, in processing order.

        A UNIT IS THE SMALLEST THING A BACKFILL CAN FINISH. ``kbu
        clearinghouse backfill run`` shards, ingests, verifies and records ONE
        unit before it starts the next, so a run killed anywhere loses at most
        the unit in flight -- which is only true if a unit is small enough to
        finish and self-contained enough to redo. Mongo's unit is a
        two-hex-digit prefix of its sort key (1/256 of the key space, a
        uniform sample rather than a biased prefix); the genome archive's will
        be one archive.

        THE DEFAULT IS ONE UNIT, and that is the honest answer for a source
        with no natural decomposition: a parquet file is read start to finish
        or not at all, so the file adapter does not override this. An adapter
        that returns the default is run as a single unit that completes only
        when every source and table in its manifest verifies.

        Returned as labels, not objects, because the labels are what travel:
        they are the directory name under ``<out>/units/``, the key in
        ``backfill-state.json`` and the ``slice`` recorded on every batch in
        ``run.json``. They must therefore be stable across runs and safe as a
        path segment.
        """
        return (SINGLE_UNIT,)

    def watermark(self) -> str | None:
        """The HIGH-WATER MARK of what this read covered, or ``None``.

        An ISO-8601 timestamp string: the greatest source-side modification
        time among the records this adapter READ. ``shard_manifest`` records
        it on the source's entry in ``run.json``, so an operator can see what
        a run covered without re-querying the store, and a later incremental
        run has a published starting point rather than a guess.

        IT IS NOT A COMPLETION CLAIM, and must not be read as one. A watermark
        says "no record modified after this time was read"; whether the
        records that WERE read all landed is
        :meth:`exhausted` plus the unit's verification, and an adapter whose
        read was capped or whose objects were unavailable still has a
        perfectly good watermark for the window it covered. An adapter that
        PERSISTS a watermark for the next run to start from is therefore
        responsible for refusing to do so on a capped or incomplete run; this
        hook only reports.

        The default is ``None`` -- the honest answer for a source with no
        modification time to take a maximum of (a parquet file, a lake
        table), which is why the ``file`` adapter does not override it.
        """
        return None

    def incomplete_reasons(self) -> tuple[str, ...]:
        """Why this read is INCOMPLETE despite reaching the end of its source.

        Empty (the default) means "nothing to report", which is the honest
        answer for every adapter whose only failure mode is stopping early --
        :meth:`exhausted` already carries that.

        IT EXISTS FOR THE RETRYABLE FAILURE, which exhaustion cannot express.
        An adapter that walked its whole selection but could not fetch three
        of the objects it needed has run off the end of its source
        (``exhausted`` is ``True``) and every batch it DID write verifies --
        so by both of the signals that existed before this hook, the unit is
        ``done``. It is not: three records are missing and a retry would get
        them. Each string names one such failure, and
        ``clearinghouse_backfill.unit_outcome`` turns a non-empty tuple into
        ``incomplete``, which is what makes a later ``--resume`` come back for
        them.

        A NON-RETRYABLE drop is NOT reported here. A record the store itself
        cannot supply (a missing result file, an annotation for a genome that
        was never built) is counted in :meth:`skipped` and the unit is done:
        retrying it would read the same absence again.
        """
        return ()

    def checkpoint_candidates(self) -> tuple[str, ...]:
        """Identity keys of the checkpoint CANDIDATES this read wrote.

        An adapter that persists a watermark for the next run writes it as a
        candidate first (see :meth:`watermark` for why an adapter may not
        publish one itself), and the driver promotes it once the rows are
        ingested and verified. This hook is how the driver learns WHICH
        candidates are this unit's.

        It exists because the candidates do not live in the unit's output
        directory -- a watermark has to be found by the NEXT run, so it lives
        under the clearinghouse state root, in ONE directory shared by every
        source of every manifest. There is consequently nothing about a
        candidate file on disk that says which unit earned it, and a driver
        that promoted whatever it found there would advance one store's
        watermark on an unrelated run's success. ``shard_manifest`` records
        these keys on the source's entry in ``run.json`` so they travel with
        the unit that earned them, and
        ``clearinghouse_backfill.run_backfill`` promotes exactly these.

        The default is empty -- the honest answer for every adapter that
        keeps no cross-run watermark, which is all of them but ``arc``.
        """
        return ()

    def bind_backfill_info(self, info: cm.BackfillInfo | None) -> None:
        """Attach the manifest's top-level ``[backfill]`` table, or ``None``.

        A SECOND binding hook rather than a constructor argument, for the
        same reason :meth:`bind_run_context` is one: every existing adapter
        and every test that builds one directly keeps working, and ``None``
        stays a first-class case.

        Almost nothing reads it -- ``pulls_from`` and ``needs_env`` are
        informational. ``tool_versions`` is not: an adapter that labels
        SEVERAL tools out of ONE store (the ``arc`` store holds BAKTA, PROKKA,
        DRAM2, KOFAMSCAN, TRANSYT and RAST annotations) has to resolve each
        row's ``"<tool>/<version>"`` from somewhere, and a per-source locator
        copy of the table is how two of the four sources reading that store
        would come to disagree -- writing two permanent ``source`` partitions
        for one tool.
        """
        self.backfill_info = info

    def tool_version(self, tool: str) -> str:
        """The version string for ``tool``, from the bound ``tool_versions``.

        Looked up case-sensitively first and then by exact upper/lower-cased
        form, so a table written ``BAKTA = "1.9"`` serves a record whose tool
        arrives as ``bakta``. A tool the table does not list (or no bound
        ``[backfill]`` at all) resolves to
        :data:`clearinghouse_manifest.DEFAULT_TOOL_VERSION`, which is Chris's
        instruction (2026-10-06): the installed version where it can be
        determined, ``1.0`` where it cannot.
        """
        table = (
            self.backfill_info.tool_versions
            if self.backfill_info is not None
            else {}
        )
        for candidate in (tool, tool.upper(), tool.lower()):
            if candidate in table:
                return table[candidate]
        return cm.DEFAULT_TOOL_VERSION

    def restrict_to_unit(self, unit: str | None) -> None:
        """Restrict this adapter's read to ONE of its :meth:`units`.

        The counterpart to :meth:`units`: the backfill driver asks for the
        labels, then runs the adapter once per label with the read narrowed to
        that label. ``None`` means no restriction.

        The default ACCEPTS only its own single label and REFUSES anything
        else, by name. An adapter that silently ignored a unit label it does
        not implement would read its whole source once per label -- 256 full
        reads of a parquet file, each ingested fill-only so that 255 of them
        wrote nothing, with nothing in the output to show the restriction had
        been dropped.
        """
        if unit is None or unit == SINGLE_UNIT:
            self._unit = None
            return
        raise ManifestSourceError(
            f"source {self.source.name!r}: adapter {self.adapter_name!r} runs "
            f"as ONE unit and cannot be restricted to unit {unit!r}. Its "
            f"units() reports {list(self.units())}. A source with no natural "
            "decomposition is a single unit -- it is read start to finish or "
            "not at all -- so there is no narrower read to ask it for."
        )

    def unit(self) -> str | None:
        """The unit label this adapter was restricted to, or ``None``."""
        return self._unit

    # -- the backfill run context -------------------------------------------

    def bind_run_context(self, context: Any) -> None:
        """Attach the backfill RUN CONTEXT this adapter runs under.

        The context is the adapter's only channel to anything durable: a
        ``--limit`` budget (see :meth:`_admit_record`) and small per-source,
        per-unit JSON state it can persist BEFORE a long wait and read back
        after a restart. The genome-archive adapter needs the latter -- it
        submits a hash job, waits hours, and must not resubmit on a resume --
        and an adapter that kept that id in memory would lose it to the first
        pod restart.

        ``None`` (the default) is a first-class case: a plain ``kbu
        clearinghouse shard`` has no backfill run, so every context-dependent
        behaviour degrades to "unlimited and nothing persisted" rather than
        requiring a context to exist.
        """
        self.run_context = context

    def _admit_record(self) -> bool:
        """Claim one record against the run's ``--limit`` budget.

        ``True`` means yield the record; ``False`` means the budget is spent
        and the adapter must ``break``, which leaves :meth:`exhausted` at
        ``False`` so the unit is recorded ``partial`` rather than ``done``.
        That distinction is the whole reason ``--limit`` is routed through the
        context rather than applied by the driver slicing the iterator: the
        driver cannot tell a source that genuinely held N rows from one it
        stopped at N, and only the adapter's own loop can.

        THE CAP IS PER READER, deliberately, and this is the one subtlety
        worth stating. A manifest source with ``kinds = ["entity",
        "content"]`` resolves to two plans, each with its own adapter instance
        and its own read of the source; and the arc store's manifest has
        several sources over ONE frozen ordered record selection. In both
        cases every reader must see THE SAME first ``--limit`` records, which
        is what a per-reader cap over an ordered source gives. A single budget
        shared across readers would instead spend the whole allowance on the
        first one and hand the rest zero -- ``--limit 10`` would load 10
        ``protein_entity`` rows and 0 ``protein_content`` rows, which is not a
        sample of anything. Cross-reader CONSISTENCY therefore comes from the
        source being ordered and frozen (the adapter's job: mongo sorts on
        ``sort_key``, the arc adapter freezes its selection), not from the
        arithmetic here.

        With no context, or a context with no budget, this is always ``True``.
        """
        if self._budget_remaining is None:
            context = self.run_context
            limit = None
            if context is not None:
                reader_limit = getattr(context, "record_limit", None)
                if reader_limit is not None:
                    limit = reader_limit(self.source.name)
            # -1 is the sentinel for "unlimited", kept out of the None state so
            # the limit is resolved from the context exactly once per read.
            self._budget_remaining = -1 if limit is None else int(limit)
        if self._budget_remaining < 0:
            return True
        if self._budget_remaining == 0:
            return False
        self._budget_remaining -= 1
        return True


#: Registry of adapter implementations by manifest ``adapter`` name. Every
#: name is DECLARED here so a manifest can be validated against the full set
#: of adapter names (the mapping half must be adapter-independent). All of
#: them are registered with real classes (see :data:`_ADAPTER_CLASSES`);
#: :func:`get_adapter` still distinguishes "not a real adapter name" from a
#: declared name with no class, but the latter no longer occurs for any of
#: them.
ADAPTER_NAMES: tuple[str, ...] = (
    "file",
    "mongo",
    "lakehouse",
    "arc",
    "kbdl_hash",
)

#: The unit label an adapter with no natural decomposition reports from
#: :meth:`SourceAdapter.units`. Short, stable, and safe as a path segment --
#: it is the directory name under ``<out>/units/`` for a single-unit run.
SINGLE_UNIT = "all"

#: Every two-lowercase-hex-digit prefix, in order: the 256 units a mongo
#: source decomposes into. ``_id`` is a 64-character lowercase-hex sha256, so
#: each prefix selects a UNIFORM 1/256 sample of the collection rather than a
#: biased leading window.
HEX_PREFIXES: tuple[str, ...] = tuple(f"{i:02x}" for i in range(256))

#: A ``--slice`` value: one two-lowercase-hex-digit prefix, or an inclusive
#: ``AA-BB`` range of them. Uppercase is REJECTED rather than folded: the key
#: space is lowercase hex, so ``"0F"`` would select nothing while looking like
#: it selected something, and silently lowercasing it would hide the fact that
#: the operator's mental model of the keys is wrong.
_SLICE_RE = re.compile(r"^(?P<lo>[0-9a-f]{2})(?:-(?P<hi>[0-9a-f]{2}))?$")


def parse_slice(spec: str) -> tuple[str, str]:
    """Parse a ``--slice`` value into an INCLUSIVE ``(lo, hi)`` prefix pair.

    ``"00"`` is the single prefix ``("00", "00")``; ``"00-0f"`` is
    ``("00", "0f")``, sixteen prefixes. Both ends must be two lowercase hex
    digits and ``lo <= hi``.

    Raises:
        ValueError: Malformed, not lowercase hex, or ``lo > hi``.
    """
    if not isinstance(spec, str) or not spec:
        raise ValueError(
            "--slice must be a two-lowercase-hex-digit prefix ('00') or an "
            f"inclusive range of them ('00-0f'); got {spec!r}."
        )
    match = _SLICE_RE.match(spec)
    if match is None:
        raise ValueError(
            f"--slice {spec!r} is not a valid slice. It must be two LOWERCASE "
            "hex digits ('00', 'a3', 'ff') or an inclusive range of two such "
            "prefixes ('00-0f'). The key space is lowercase hex, so an "
            "uppercase prefix would match nothing while looking like it "
            "matched something."
        )
    lo = match.group("lo")
    hi = match.group("hi") or lo
    if int(lo, 16) > int(hi, 16):
        raise ValueError(
            f"--slice {spec!r} runs backwards: {lo!r} > {hi!r}. The range is "
            "inclusive and ascending, matching the ascending sort the cursor "
            "reads in."
        )
    return lo, hi


def slice_prefixes(spec: str | None) -> tuple[str, ...]:
    """The prefixes one ``--slice`` selects -- ALL 256 when ``spec`` is ``None``.

    Each returned prefix is one UNIT of work. Omitting ``--slice`` is
    therefore a 256-unit run over the whole key space, not an unsliced
    single-unit read of the collection: a 264M-document source cannot be
    sharded, ingested and verified as one unit that a pod restart could then
    lose in its entirety.
    """
    if spec is None:
        return HEX_PREFIXES
    lo, hi = parse_slice(spec)
    return tuple(f"{i:02x}" for i in range(int(lo, 16), int(hi, 16) + 1))


def unit_key_range(prefix: str) -> tuple[str, str | None]:
    """The half-open ``[min_key, max_key)`` one prefix selects.

    ``"00"`` -> ``("00", "01")``; ``"fe"`` -> ``("fe", "ff")``; ``"ff"`` ->
    ``("ff", None)``. The upper bound is OMITTED for ``"ff"`` because there is
    no successor prefix and nothing sorts above ``"ff..."`` in a lowercase-hex
    key space -- so the range is open at the top, which is the only way the
    256 ranges cover the space exactly once with no gap and no overlap.

    Raises:
        ValueError: Not two lowercase hex digits.
    """
    lo, hi = parse_slice(prefix)
    if lo != hi:
        raise ValueError(
            f"unit_key_range takes ONE prefix, not a range; got {prefix!r}. "
            "A range is a set of units (see slice_prefixes), and each unit "
            "gets its own key range."
        )
    value = int(lo, 16)
    if value == 0xFF:
        return lo, None
    return lo, f"{value + 1:02x}"


def slice_key_range(spec: str | None) -> tuple[str | None, str | None]:
    """The half-open key range a whole ``--slice`` covers, for reporting.

    ``None`` for both bounds means the whole key space. This is what the
    envelope prints; the per-unit ranges the adapters actually read come from
    :func:`unit_key_range`.
    """
    if spec is None:
        return None, None
    lo, hi = parse_slice(spec)
    return lo, unit_key_range(hi)[1]


class FileSourceAdapter(SourceAdapter):
    """The ``file`` adapter: reads a local parquet / jsonl / tsv file.

    Needs only CPU and disk -- no credentials, no network -- so it runs
    anywhere. The manifest locator keys are ``path`` (required) and ``format``
    (``parquet`` | ``jsonl`` | ``tsv``; inferred from the path suffix if
    omitted).
    """

    adapter_name = "file"

    def _path(self) -> Path:
        path = self.source.locator.get("path")
        if not path or not isinstance(path, str):
            raise ManifestSourceError(
                f"source {self.source.name!r}: file adapter requires a "
                "non-empty string 'path'."
            )
        return Path(path)

    def _format(self) -> str:
        fmt = self.source.locator.get("format")
        if fmt:
            return str(fmt).lower()
        suffix = self._path().suffix.lstrip(".").lower()
        if suffix in ("parquet", "jsonl", "tsv"):
            return suffix
        raise ManifestSourceError(
            f"source {self.source.name!r}: file adapter could not infer "
            f"'format' from path {self._path().name!r}; set format explicitly "
            "(parquet | jsonl | tsv)."
        )

    def estimate_bytes(self) -> int:
        path = self._path()
        if not path.exists():
            raise ManifestSourceError(
                f"source {self.source.name!r}: file adapter path {str(path)!r} "
                "does not exist."
            )
        return path.stat().st_size

    def estimate_rows(self) -> int | None:
        """Exact row count from parquet FOOTER METADATA; ``None`` for text.

        A parquet footer records ``num_rows`` for the file, so the count is
        exact and costs one footer read regardless of file size -- the whole
        point of the :meth:`SourceAdapter.estimate_rows` contract. A jsonl or
        tsv file carries no such metadata and the only way to count it is to
        read it, so this returns ``None`` rather than paying for a scan behind
        the caller's back.
        """
        if self._format() != "parquet":
            return None
        return int(pq.ParquetFile(str(self._path())).metadata.num_rows)

    def estimate_rows_method(self) -> str:
        if self._format() != "parquet":
            return "unavailable"
        return "parquet-footer-num-rows"

    def iter_records(self) -> Iterator[Mapping[str, Any]]:
        fmt = self._format()
        path = self._path()
        self._exhausted = False
        if fmt == "parquet":
            rows = self._iter_parquet(path)
        elif fmt == "jsonl":
            rows = self._iter_jsonl(path)
        elif fmt == "tsv":
            rows = self._iter_tsv(path)
        else:  # pragma: no cover - guarded by _format
            raise ManifestSourceError(
                f"source {self.source.name!r}: unsupported file format {fmt!r}."
            )
        for row in rows:
            # The ``--limit`` budget is claimed HERE, in the adapter's own
            # loop, so a budget stop is distinguishable from a file that
            # genuinely held that many rows: a break leaves _exhausted False
            # and the unit is recorded `partial`, never `done`.
            if not self._admit_record():
                return
            yield row
        # Reached only by running off the end of the file -- a caller that
        # breaks out of the loop never gets here, which is exactly the
        # distinction exhausted() exists to record.
        self._exhausted = True

    @staticmethod
    def _iter_parquet(path: Path) -> Iterator[Mapping[str, Any]]:
        parquet_file = pq.ParquetFile(str(path))
        for batch in parquet_file.iter_batches():
            for row in batch.to_pylist():
                yield row

    @staticmethod
    def _iter_jsonl(path: Path) -> Iterator[Mapping[str, Any]]:
        import json

        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield json.loads(line)

    @staticmethod
    def _iter_tsv(path: Path) -> Iterator[Mapping[str, Any]]:
        import csv

        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            for row in reader:
                yield dict(row)


#: A conservative per-row byte estimate used only by
#: :meth:`LakehouseSourceAdapter.estimate_bytes` to turn a cheap ``COUNT(*)``
#: into a shard COUNT. The estimate need not be exact -- equal-width hash
#: ranges self-balance and the skew guard splits any range that still runs
#: large (see the module docstring's range-cutting section), so an over- or
#: under-estimate at worst changes how many equal-width ranges are cut, never
#: correctness. A genome source row (a handful of short identifier/metric
#: columns; NO inlined sequence -- genome content stores a POINTER, and this
#: source carries none) is well under this figure, so it is an UPPER-ish
#: estimate, which is the side to err on for a count-only sizing.
LAKEHOUSE_BYTES_PER_ROW = 1024


class LakehouseSourceAdapter(SourceAdapter):
    """The ``lakehouse`` adapter: reads a lake table through ``query()``.

    Reads the raw records of ONE Iceberg table in the BER Data Lakehouse via
    :meth:`~kbutillib.domains.kbase.berdl.capability.BerdlCapability.query` --
    a READ, never a write. It yields each row as a plain ``dict`` keyed by
    source column name and does nothing else: it does not hash, sort, write
    parquet, or emit any ``INSERT``/``MERGE``. All of that is the sharder's
    job (and the sanctioned in-pod ``load`` verb's), identically for whatever
    an adapter yields -- which is the seam that makes ``file``, ``mongo`` and
    ``lakehouse`` interchangeable (see the module docstring's adapter
    contract).

    Why a READ adapter and not a one-line in-lake ``INSERT ... SELECT``. The
    genome source (``kbaseincubator.genome_clearhouse``) sits in the SAME
    tenant and catalog as the write target
    (:data:`~kbutillib.domains.kbase.berdl.clearinghouse_schema.CLEARINGHOUSE_NAMESPACE`),
    so ``INSERT INTO ...clearinghouse.genome_entity SELECT ... FROM
    ...genome_clearhouse.<t>`` looks obviously correct and is about one line.
    It is forbidden: an ``INSERT``/``MERGE`` on the Spark path BYPASSES
    ``data_lakehouse_ingest.ingest``, which is what applies schema enforcement
    on every sanctioned write -- the very enforcement whose absence let
    ``BINARY`` break this corpus once (dev 1219), a failure that only surfaced
    at a real write. So the adapter READS, the sharder writes BRONZE, and the
    ``load`` verb ingests through the sanctioned path exactly as for a file
    source. This class contains no ``INSERT`` and no ``MERGE``.

    Locator keys (the mapping half of the manifest is adapter-independent --
    only these source-locating keys differ; see
    :mod:`clearinghouse_manifest`):

    - ``table`` (required) -- the source table name within the namespace, e.g.
      ``"genome_quality"``.
    - ``namespace`` (optional) -- the fully-qualified source namespace.
      Defaults to
      :data:`~kbutillib.domains.kbase.berdl.clearinghouse_schema.SOURCE_GENOME_CLEARHOUSE_NAMESPACE`
      (``kbaseincubator.genome_clearhouse``), the genome source Chris named.
      Taken from that NAMED CONSTANT, never a string literal here, because it
      differs from the write-target namespace by five characters and one
      missing ``in`` (see the constant's adjacency warning).

    Where it runs: reading a lake table needs the read transport
    ``BerdlCapability.query`` resolves by locus -- Trino in-pod, the read-only
    REST transport off-pod. It never needs the write path, so it can run
    wherever ``query()`` can reach the lake (unlike the ``load`` verb, which
    is in-pod only). Tests inject a fake capability and require no pod.

    Args:
        source: The parsed ``[[source]]`` block.
        capability: The object exposing ``query(sql, ...)`` /
            ``locus()`` -- a real
            :class:`~kbutillib.domains.kbase.berdl.capability.BerdlCapability`
            in production, a fake in tests. Constructed lazily (deferred
            import) on first use when not given, so simply building the
            adapter -- as :func:`get_adapter` does -- needs no pod package
            and makes no network call.
    """

    adapter_name = "lakehouse"

    def __init__(self, source: cm.Source, *, capability: Any = None):
        super().__init__(source)
        self._capability = capability

    def _get_capability(self) -> Any:
        """Return the capability, deferring the import until first use.

        The import is deferred (not at module scope) so this module stays
        pod-free and importable off-pod exactly like the rest of the shard
        stage -- ``BerdlCapability`` itself defers every pod dependency to
        first use, so importing it here is safe, but keeping the import inside
        the method costs nothing and keeps the module-scope import list free
        of anything a caller who never touches the lakehouse adapter pays for.
        """
        if self._capability is None:
            from .capability import BerdlCapability  # noqa: PLC0415

            self._capability = BerdlCapability()
        return self._capability

    def _namespace(self) -> str:
        """Resolve the source namespace from the locator or the named default.

        Never a string literal here -- an explicit ``namespace`` locator key
        is honoured; otherwise the default is the NAMED constant
        :data:`~kbutillib.domains.kbase.berdl.clearinghouse_schema.SOURCE_GENOME_CLEARHOUSE_NAMESPACE`.
        """
        namespace = self.source.locator.get("namespace")
        if namespace is None:
            return schema.SOURCE_GENOME_CLEARHOUSE_NAMESPACE
        if not isinstance(namespace, str) or not namespace.strip():
            raise ManifestSourceError(
                f"source {self.source.name!r}: lakehouse adapter 'namespace', "
                "when given, must be a non-empty string (the fully-qualified "
                "source namespace, e.g. 'kbaseincubator.genome_clearhouse')."
            )
        return namespace

    def _table(self) -> str:
        """Return the required source table name from the locator."""
        table = self.source.locator.get("table")
        if not table or not isinstance(table, str):
            raise ManifestSourceError(
                f"source {self.source.name!r}: lakehouse adapter requires a "
                "non-empty string 'table' (the source table name within the "
                "namespace, e.g. 'genome_quality')."
            )
        return table

    def _fqn(self) -> str:
        """Build the double-quoted, per-segment FQN of the source table.

        Quoted with ANSI double quotes, one pair per dot-separated namespace
        segment plus the table -- NOT backticks. The read routes through
        ``BerdlCapability.query()``, which defaults to Trino in-pod (and REST
        -> Trino off-pod), and Trino REJECTS backticks (a real defect this
        repo already paid for -- see 5fc10ba / 95eac55 and the docstring
        correction dab0fcc). A dotted namespace quoted as a single identifier
        does not resolve, so each segment gets its own quote pair.
        """
        namespace = self._namespace()
        segments = [seg for seg in namespace.split(".") if seg]
        return ".".join(f'"{seg}"' for seg in [*segments, self._table()])

    def estimate_bytes(self) -> int:
        """Estimate the source's total size via ``COUNT(*)`` x a per-row figure.

        Used only to pick the shard COUNT (see :meth:`SourceAdapter.estimate_bytes`);
        it need not be exact. A cheap ``SELECT COUNT(*)`` is far cheaper than
        streaming the table, and equal-width hash ranges self-balance around
        whatever count it yields. Returns at least 1 so
        ``ceil(total / target_bytes)`` is always >= 1.
        """
        sql = f"SELECT COUNT(*) AS n FROM {self._fqn()}"
        rows = self._query(sql)
        count = 0
        if rows:
            row = rows[0]
            value = row.get("n") if isinstance(row, dict) else None
            if value is None and isinstance(row, dict) and row:
                # A cursor without a column alias may key the single column
                # differently; fall back to the first value.
                value = next(iter(row.values()))
            count = int(value or 0)
        return max(1, count * LAKEHOUSE_BYTES_PER_ROW)

    def iter_records(self) -> Iterator[Mapping[str, Any]]:
        """Yield every source row as a dict, paging off-pod so nothing truncates.

        In-pod ``query()`` (Trino) returns the full result, so one statement
        suffices. Off-pod the read-only REST transport caps each page, so this
        pages with an explicit ``limit``/``offset`` and KEEPS PAGING while a
        page comes back EXACTLY :data:`OFF_POD_PAGE_SIZE` rows long -- a full
        page is evidence of nothing, and stopping on it would silently
        truncate a source (see :data:`OFF_POD_PAGE_SIZE`). It stops only on a
        page shorter than the cap. The ``ORDER BY`` on paging is what makes
        ``limit``/``offset`` a STABLE window across pages rather than a
        possibly-overlapping-or-gapping one.
        """
        base_sql = f"SELECT * FROM {self._fqn()}"
        self._exhausted = False
        if self._get_capability().locus() == "in_pod":
            for row in self._query(base_sql):
                yield dict(row)
            self._exhausted = True
            return
        # Off-pod: page a stable window until a short page arrives.
        paged_sql = base_sql + f" ORDER BY {self._order_key()}"
        offset = 0
        while True:
            page = self._query(paged_sql, limit=OFF_POD_PAGE_SIZE, offset=offset)
            for row in page:
                yield dict(row)
            if len(page) < OFF_POD_PAGE_SIZE:
                break
            offset += OFF_POD_PAGE_SIZE
        self._exhausted = True

    def estimate_rows(self) -> int | None:
        """Row count from a single ``SELECT COUNT(*)`` -- one statement, no scan.

        ``COUNT(*)`` on an Iceberg table is answered from manifest metadata
        rather than by reading data files, so it satisfies
        :meth:`SourceAdapter.estimate_rows`'s cheapness contract. This is the
        same read :meth:`estimate_bytes` already makes; it is not re-derived
        from the byte estimate because that would divide by
        :data:`LAKEHOUSE_BYTES_PER_ROW`, turning an exact count into a figure
        wrong by whatever that convention is wrong by.
        """
        rows = self._query(f"SELECT COUNT(*) AS n FROM {self._fqn()}")
        if not rows:
            return 0
        row = rows[0]
        value = row.get("n") if isinstance(row, dict) else None
        if value is None and isinstance(row, dict) and row:
            value = next(iter(row.values()))
        return int(value or 0)

    def estimate_rows_method(self) -> str:
        return "lakehouse-count-star"

    def _order_key(self) -> str:
        """The ORDER BY expression that makes off-pod paging a stable window.

        Uses the hash column the plan hashes on when it is a ``precomputed``
        digest already present in the source, else the raw hash column -- a
        column guaranteed to exist on every row -- so the paging window is
        deterministic and reproducible. The column name is drawn from the
        parsed hash spec, not hardcoded, so it follows whatever the manifest
        names.
        """
        column = self.source.hash_spec.get("precomputed") or self.source.hash_spec.get(
            "raw_column"
        )
        if not column or not isinstance(column, str):
            # No hash column to order on (e.g. an entity-only source with a
            # hash rule this adapter cannot see); order by the first source
            # column is not knowable here, so fall back to the table's own
            # implicit order via a constant -- a single-column ORDER BY 1 keeps
            # the window stable enough for paging.
            return "1"
        return f'"{column}"'

    def _query(
        self, sql: str, *, limit: int | None = None, offset: int = 0
    ) -> list[Mapping[str, Any]]:
        """Run one read through the capability and normalise its rows to dicts.

        In-pod: ``query(sql)`` (default Trino engine) already returns dict
        rows. Off-pod: ``query(sql, limit=, offset=)`` returns the REST
        result dict ``{'success', 'data', ...}``; a failed read raises here so
        it never masquerades as an empty one. Spark ``Row`` (if a fake yields
        one) is coerced via ``asDict()``.
        """
        capability = self._get_capability()
        if capability.locus() == "in_pod":
            rows = capability.query(sql)
        else:
            rows = capability.query(sql, limit=limit, offset=offset)
        return _normalize_rows(rows)


def _normalize_rows(rows: Any) -> list[Mapping[str, Any]]:
    """Normalise a capability's result to a list of dict rows across loci.

    Off-pod ``OffPodTransport.query`` returns ``{'success', 'data', ...}``; an
    unsuccessful result raises rather than being read as empty. In-pod Trino
    returns dict rows already; Spark ``Row`` exposes ``asDict()``. A bare
    tuple is wrapped positionally as a last resort so nothing is dropped.
    """
    if isinstance(rows, dict):
        if rows.get("success") is False:
            raise ManifestSourceError(
                f"lakehouse read failed: {rows.get('error')!r}"
            )
        rows = rows.get("data", [])
    out: list[Mapping[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            out.append(row)
        elif hasattr(row, "asDict"):
            out.append(row.asDict())
        else:
            out.append({str(i): value for i, value in enumerate(row)})
    return out


#: Default batch size for the Mongo cursor. A large batch amortises round-trips
#: over a 400M+ document read without materialising the collection; it is
#: overridable per source via the ``batch_size`` locator key.
MONGO_BATCH_SIZE = 5000

#: A conservative per-document byte estimate used only by
#: :meth:`MongoSourceAdapter.estimate_bytes` to turn a cheap
#: ``estimated_document_count()`` into a shard COUNT. As with the lakehouse
#: figure, it need not be exact -- equal-width hash ranges self-balance and the
#: skew guard splits any range that still runs large (see the module docstring),
#: so an over- or under-estimate at worst changes how many ranges are cut, never
#: correctness. A ``seq_protein`` document is ``_id`` (64 hex chars) plus a
#: ~200-300 byte compressed blob that decompresses to a few hundred residues; an
#: annotation document is a functional-role string and a small array. This
#: figure is an UPPER-ish per-row estimate against the decompressed sequence
#: size, which is the side to err on for a count-only sizing.
MONGO_BYTES_PER_DOC = 1024

#: The default ``join.on`` field. BOTH collections of the surveyed store are
#: keyed by ``_id`` and the ``_id`` index is the ONLY index that exists, so a
#: join on any other field would be a collection scan per page -- which is why
#: the default is the one field that is always indexed, and why overriding it
#: is a decision about the store's indexes rather than a convenience.
MONGO_JOIN_DEFAULT_ON = "_id"

#: The ``join`` locator's closed key set, rejected BY NAME so a typo
#: (``field`` for ``fields``) fails loudly rather than silently joining
#: nothing onto every record.
_MONGO_JOIN_KEYS = frozenset({"collection", "on", "fields", "decompress"})

#: Skip reason: the primary record had NO document in the join collection, so
#: the fields the mapping needs (for ``seq_protein_kofam``, the sequence the
#: entity hash is computed from) do not exist for it. Counted rather than
#: fabricated: an annotation whose protein is not in ``seq_protein`` cannot be
#: tied to a protein identity, and a retry would read the same absence again.
MONGO_SKIP_NO_JOIN_MATCH = "no-join-match"

#: Skip-reason PREFIX for a record that failed ``require_fields``; the field
#: name is appended (``missing:ko``). The field is IN the reason because one
#: source may require several, and "41,000 records dropped" is not actionable
#: while "41,000 missing:ko" is.
MONGO_SKIP_MISSING_PREFIX = "missing:"


@dataclass(frozen=True)
class MongoJoinSpec:
    """A parsed ``join`` locator: ONE lookup collection, ONE ``$in`` per page.

    ``fields`` is ``{output_field: input_field}`` -- the same direction as the
    ``decompress`` locator -- and ``decompress`` lists OUTPUT names (the join's
    own side), because by the time a joined value is decompressed it is already
    living under its output name.
    """

    collection: str
    on: str
    fields: Mapping[str, str]
    decompress: tuple[str, ...]


class MongoSourceAdapter(SourceAdapter):
    """The ``mongo`` adapter: reads a MongoDB collection with a batched cursor.

    Reads the documents of ONE MongoDB collection and yields each as a plain
    ``dict`` row keyed by field name. Like every adapter it does NOTHING else --
    no hashing, no sorting, no parquet, no Iceberg. The sharder standardizes,
    hashes, sorts and writes whatever this yields, identically to what the
    ``file`` and ``lakehouse`` adapters yield (see the module docstring's
    adapter contract). This is the seam that makes the three interchangeable:
    the MAPPING half of the manifest is adapter-independent; only the
    source-locating keys below differ.

    THE SOURCE, from the OP-B survey (2026-09-30, live on ``bioseed_mongo`` on
    poplar). An "annotated protein set" is a JOIN across collections in one
    database, all keyed by ``_id`` = a 64-character lowercase-hex sha256:

    - ``seq_protein`` carries the SEQUENCE only, as ``z_seq`` -- a
      zlib-compressed amino-acid blob (magic ``0x789c``), no other fields. It
      feeds ``protein_content`` (sequence, seq_length) and, via the sequence
      hash, ``protein_entity``.
    - ``protein_to_rast2`` (and the other per-tool collections) carry
      ANNOTATIONS only. Its annotation fields are OPTIONAL (~28% of documents
      are ``_id``-only stubs), and its ``quality`` sub-fields are stored as
      STRINGS, not numbers. It feeds ``protein_result``.

    A single manifest source names ONE collection, and the join ACROSS ENTITY
    TYPES is expressed as SEVERAL sources in one manifest -- one per entity
    type, each hashing on its own identity -- not as a join inside this
    adapter. That keeps the adapter a pure reader and the sharder's
    range-cutting untouched. The ``protein_content`` source reads
    ``seq_protein`` and DECOMPRESSES ``z_seq`` (see ``decompress`` below); the
    ``protein_result`` source reads ``protein_to_rast2``.

    THE ``join`` LOCATOR IS THE ONE EXCEPTION, and it exists because the
    annotation collections DO NOT CARRY THE SEQUENCE. ``seq_protein_kofam``
    holds ``{_id, ko}`` and nothing else; ``protein_to_rast2`` holds
    ``{_id, function?, ...}``. The clearinghouse identity of a protein is
    computed FROM ITS SEQUENCE (never from ``_id`` -- see above), so a
    ``protein_result`` source over either collection has nothing to hash
    unless the sequence is fetched from ``seq_protein`` alongside it. That is
    a per-record LOOKUP on an indexed key, not a relational join: the adapter
    reads a PAGE of the primary collection and issues ONE
    ``find({on: {"$in": [...page ids...]}})`` against the join collection, so
    the cost is one extra round trip per page rather than one per record. See
    ``join`` below.

    IMPORTANT -- the Mongo ``_id`` is NOT trusted as the clearinghouse
    ``entity_hash``. Both are 64-hex sha256, but whether Mongo's ``_id`` equals
    the value :mod:`kbutillib.domains.identity.standardizers` computes from the
    sequence is unverified. A ``seq_protein`` manifest therefore hashes on the
    DECOMPRESSED SEQUENCE (``raw_column``), letting the standardizer recompute
    the identity, rather than trusting ``_id`` via ``precomputed``. Whether the
    two agree is a spot-check for the manifest's derivation, deliberately NOT
    hardcoded here.

    Locator keys (only these source-locating keys differ from ``file`` /
    ``lakehouse``; the mapping half is adapter-independent):

    - ``uri`` (optional) -- a full ``mongodb://...`` connection string. If
      omitted, ``host`` (default ``"localhost"``) and ``port`` (default
      ``27017``) are used. No machine is hardcoded: the connection comes
      entirely from the manifest, so the same adapter runs wherever the store
      is reachable (for this source, poplar localhost). AUTH IS DISABLED on the
      surveyed store; no credential key exists or is read.
    - ``database`` (required) -- the database name (the survey's is literally
      ``"database"``).
    - ``collection`` (required) -- the collection name, e.g. ``"seq_protein"``
      or ``"protein_to_rast2"``.
    - ``batch_size`` (optional) -- cursor batch size; defaults to
      :data:`MONGO_BATCH_SIZE`. The read is a batched cursor, never a
      collection-into-memory load.
    - ``decompress`` (optional) -- a mapping ``{output_field: input_field}``
      (e.g. ``{"sequence": "z_seq"}``). For each named ``input_field`` carrying
      a zlib blob (or ``bytes``-like value), the adapter zlib-decompresses it,
      decodes it as UTF-8, and exposes the result under ``output_field`` so the
      manifest's ``[source.content]`` can map a plain ``sequence`` column. The
      raw compressed field is dropped from the yielded row. A stub document
      missing the input field yields the row unchanged (no output field), so a
      manifest that requires the sequence naturally fails that record rather
      than fabricating one.
    - ``sort_key`` (optional) -- the field the cursor sorts and pages on;
      defaults to ``"_id"``. Sorting on a stable key is what makes the read
      RESUMABLE (via ``start_after``) and PARALLELISABLE across disjoint key
      ranges (via ``min_key`` / ``max_key``): the sharder runs shards in
      parallel over disjoint slices of this key.
    - ``min_key`` / ``max_key`` (optional) -- restrict the read to the
      half-open ``[min_key, max_key)`` range of ``sort_key``. A parallel run
      cuts the ``_id`` space into disjoint ranges and hands one to each adapter
      instance; the ranges never overlap, so no document is read twice.
    - ``start_after`` (optional) -- resume a killed read strictly AFTER this
      ``sort_key`` value, so a restart does not replay the whole collection.
    - ``join`` (optional) -- a mapping
      ``{collection, on, fields, decompress}`` that merges fields from a
      SECOND collection of the same database into every record:

      * ``collection`` (required) -- the join collection's name, e.g.
        ``"seq_protein"``.
      * ``on`` (optional) -- the field joined on, in BOTH collections;
        defaults to :data:`MONGO_JOIN_DEFAULT_ON`. The surveyed store has no
        index but ``_id``, so overriding this asks for a collection scan per
        page.
      * ``fields`` (required) -- ``{output_field: input_field}``, read off the
        join document and merged into the record under ``output_field``. Also
        the PROJECTION: nothing else is fetched from the join collection.
      * ``decompress`` (optional) -- a list of OUTPUT names to
        zlib-decompress and UTF-8 decode in place, for a joined blob like
        ``z_seq``. Every name must appear in ``fields``.

      ONE ``$in`` PER PAGE, and the page is ``batch_size`` records -- the same
      granularity the primary cursor already reads at, rather than a second
      tunable that could disagree with it. A record with NO document in the
      join collection is SKIPPED and counted under
      :data:`MONGO_SKIP_NO_JOIN_MATCH`; a record whose join document exists
      but lacks a projected field simply does not get that output field, which
      is exactly what ``decompress`` does for a stub (the mapping then fails
      that record rather than the adapter fabricating a value).
    - ``require_fields`` (optional) -- a list of field names every record must
      carry non-empty. A record missing one, or holding ``None`` or an empty
      string / list / mapping in one, is SKIPPED and counted under
      ``missing:<field>`` (see :data:`MONGO_SKIP_MISSING_PREFIX`). It is what
      makes ``protein_to_rast2``'s ~28% ``_id``-only stubs a COUNTED,
      explained drop rather than 112M rows of nulls or 112M rows counted
      ``shard-value-error``. A record is counted under the FIRST
      requirement it fails, so the counts sum to the number of records
      dropped.

      THE CHECK RUNS TWICE, by design, and the accounting is identical either
      way. A requirement naming a field the join or ``decompress`` PRODUCES
      can only be judged after the merge; one naming a plain primary field can
      be judged before it, and judging it there is what keeps the adapter from
      fetching ~112M sequences for records it is about to drop. Both report
      the same reason key, so where a record was dropped is invisible in
      ``skipped()`` -- only that it was, and why.

    Where it runs: wherever it can reach the Mongo store. No machine is
    hardcoded; the connection is taken entirely from the manifest. ``pymongo``
    is imported lazily on first read (deferred, exactly like the lakehouse
    adapter defers its pod import) so simply CONSTRUCTING the adapter -- as
    :func:`get_adapter` does during manifest validation -- needs no driver and
    makes no connection. Tests inject a fake collection and never touch a live
    database.

    Args:
        source: The parsed ``[[source]]`` block.
        collection: An object exposing ``find(...)`` /
            ``estimated_document_count()`` -- a real pymongo ``Collection`` in
            production, a fake in tests. When ``None`` (the default) a client
            is connected lazily on first use from the locator keys.
        join_collection: The collection the ``join`` locator looks up,
            injected the same way and for the same reason as ``collection``.
            When ``None`` (the default) it is resolved off the lazily built
            client; a test that injects ``collection`` and declares a ``join``
            must inject this too, because an injected primary collection means
            there is no client to resolve the second one from.
    """

    adapter_name = "mongo"

    def __init__(
        self,
        source: cm.Source,
        *,
        collection: Any = None,
        join_collection: Any = None,
    ):
        super().__init__(source)
        self._collection = collection
        self._join_collection = join_collection
        self._client = None
        #: The ``(min_key, max_key)`` pair :meth:`restrict_to_unit` set, or
        #: ``None``. ``max_key`` is ``None`` for the final ``"ff"`` prefix.
        self._unit_range: tuple[str, str | None] | None = None

    # -- locator resolution --------------------------------------------------

    def _database_name(self) -> str:
        database = self.source.locator.get("database")
        if not database or not isinstance(database, str):
            raise ManifestSourceError(
                f"source {self.source.name!r}: mongo adapter requires a "
                "non-empty string 'database'."
            )
        return database

    def _collection_name(self) -> str:
        collection = self.source.locator.get("collection")
        if not collection or not isinstance(collection, str):
            raise ManifestSourceError(
                f"source {self.source.name!r}: mongo adapter requires a "
                "non-empty string 'collection'."
            )
        return collection

    def _batch_size(self) -> int:
        raw = self.source.locator.get("batch_size", MONGO_BATCH_SIZE)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ManifestSourceError(
                f"source {self.source.name!r}: mongo adapter 'batch_size' must "
                f"be an integer, got {raw!r}."
            )
        if value <= 0:
            raise ManifestSourceError(
                f"source {self.source.name!r}: mongo adapter 'batch_size' must "
                f"be a positive integer, got {value}."
            )
        return value

    def _sort_key(self) -> str:
        key = self.source.locator.get("sort_key", "_id")
        if not key or not isinstance(key, str):
            raise ManifestSourceError(
                f"source {self.source.name!r}: mongo adapter 'sort_key', when "
                "given, must be a non-empty string."
            )
        return key

    def _decompress_spec(self) -> Mapping[str, str]:
        spec = self.source.locator.get("decompress")
        if spec is None:
            return {}
        if not isinstance(spec, Mapping) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in spec.items()
        ):
            raise ManifestSourceError(
                f"source {self.source.name!r}: mongo adapter 'decompress' must "
                "be a mapping of {output_field: input_field} strings, e.g. "
                '{"sequence": "z_seq"}.'
            )
        return dict(spec)

    def _join_spec(self) -> MongoJoinSpec | None:
        """Parse the ``join`` locator, or ``None`` when the source declares none.

        Every key is checked BY NAME against :data:`_MONGO_JOIN_KEYS`: a typo
        in ``fields`` would otherwise leave the join producing no columns at
        all, which looks exactly like a store that lost a collection.
        """
        raw = self.source.locator.get("join")
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise ManifestSourceError(
                f"source {self.source.name!r}: mongo adapter 'join' must be a "
                "mapping with keys 'collection', 'on', 'fields' and "
                f"'decompress', got {type(raw).__name__}."
            )
        unknown = sorted(set(raw) - _MONGO_JOIN_KEYS)
        if unknown:
            raise ManifestSourceError(
                f"source {self.source.name!r}: mongo adapter 'join' has "
                f"unknown key(s) {unknown}; only "
                f"{sorted(_MONGO_JOIN_KEYS)} are read."
            )
        collection = raw.get("collection")
        if not collection or not isinstance(collection, str):
            raise ManifestSourceError(
                f"source {self.source.name!r}: mongo adapter 'join' requires a "
                "non-empty string 'collection' naming the collection to look "
                'up, e.g. "seq_protein".'
            )
        on = raw.get("on", MONGO_JOIN_DEFAULT_ON)
        if not on or not isinstance(on, str):
            raise ManifestSourceError(
                f"source {self.source.name!r}: mongo adapter 'join.on', when "
                f"given, must be a non-empty string (default "
                f"{MONGO_JOIN_DEFAULT_ON!r})."
            )
        fields = raw.get("fields")
        if (
            not isinstance(fields, Mapping)
            or not fields
            or not all(
                isinstance(k, str) and isinstance(v, str) and k and v
                for k, v in fields.items()
            )
        ):
            raise ManifestSourceError(
                f"source {self.source.name!r}: mongo adapter 'join.fields' must "
                "be a non-empty mapping of {output_field: input_field} "
                'strings, e.g. {"sequence": "z_seq"}; it is also the '
                "projection, so a join that fetches nothing is a mistake."
            )
        decompress = raw.get("decompress", ())
        if isinstance(decompress, str) or not isinstance(decompress, Sequence):
            raise ManifestSourceError(
                f"source {self.source.name!r}: mongo adapter 'join.decompress' "
                'must be a list of output field names, e.g. ["sequence"].'
            )
        if not all(isinstance(item, str) and item for item in decompress):
            raise ManifestSourceError(
                f"source {self.source.name!r}: mongo adapter 'join.decompress' "
                "must be a list of non-empty output field names."
            )
        stray = sorted(set(decompress) - set(fields))
        if stray:
            raise ManifestSourceError(
                f"source {self.source.name!r}: mongo adapter 'join.decompress' "
                f"names {stray}, which 'join.fields' does not produce; it "
                "lists OUTPUT names (the keys of 'fields'), not the join "
                "collection's own field names."
            )
        return MongoJoinSpec(
            collection=collection,
            on=on,
            fields=dict(fields),
            decompress=tuple(decompress),
        )

    def _require_fields(self) -> tuple[str, ...]:
        """Parse the ``require_fields`` locator; empty when none is declared."""
        raw = self.source.locator.get("require_fields")
        if raw is None:
            return ()
        if (
            isinstance(raw, str)
            or not isinstance(raw, Sequence)
            or not all(isinstance(item, str) and item for item in raw)
        ):
            raise ManifestSourceError(
                f"source {self.source.name!r}: mongo adapter 'require_fields' "
                'must be a list of non-empty field names, e.g. ["ko"].'
            )
        return tuple(raw)

    def units(self) -> tuple[str, ...]:
        """The 256 two-hex-digit prefixes of ``sort_key``: this source's units.

        ``_id`` (and any sha256 sort key) is 64 lowercase hex characters, so a
        two-digit prefix selects a UNIFORM 1/256 sample of the collection --
        which is why a backfill slices by prefix rather than by offset. Each
        prefix is one unit of work: shard it, ingest it, verify it, record it,
        move on.

        This is the full set the source COULD be cut into. The driver's
        ``--slice`` narrows which of them a given run processes; it does not
        change what the source decomposes into.
        """
        return HEX_PREFIXES

    def restrict_to_unit(self, unit: str | None) -> None:
        """Narrow the read to one two-hex-digit prefix of ``sort_key``.

        Sets the half-open range ``[unit, next(unit))`` -- and OMITS the upper
        bound for ``"ff"``, which has no successor prefix; its range is
        ``["ff", ...)``, open at the top, which is correct because nothing
        sorts above ``"ff..."`` in a lowercase-hex key space.

        The restriction is held on the adapter rather than written back into
        the (frozen) locator, and :meth:`_query_filter` prefers it over the
        manifest's own ``min_key``/``max_key``. A manifest that already pins a
        narrower range and a driver that then asks for a unit are a
        CONTRADICTION, not an intersection, so the explicit per-unit
        instruction wins and the manifest's range is reported as overridden
        rather than silently ANDed into something neither party asked for.
        """
        if unit is None:
            self._unit = None
            self._unit_range = None
            return
        self._unit_range = unit_key_range(unit)
        self._unit = unit

    def _query_filter(self) -> dict[str, Any]:
        """Build the ``find`` filter from min_key / max_key / start_after.

        The ``sort_key`` range is half-open ``[min_key, max_key)`` so adjacent
        parallel slices never overlap on a boundary value; ``start_after`` is a
        strict ``$gt`` resume point. All three are optional; with none set the
        filter is empty and the whole collection is read.

        A :meth:`restrict_to_unit` range OVERRIDES the locator's own
        ``min_key``/``max_key`` -- see that method.
        """
        sort_key = self._sort_key()
        conditions: dict[str, Any] = {}
        if self._unit_range is not None:
            min_key, max_key = self._unit_range
        else:
            min_key = self.source.locator.get("min_key")
            max_key = self.source.locator.get("max_key")
        start_after = self.source.locator.get("start_after")
        if start_after is not None:
            conditions["$gt"] = start_after
        elif min_key is not None:
            conditions["$gte"] = min_key
        if max_key is not None:
            conditions["$lt"] = max_key
        return {sort_key: conditions} if conditions else {}

    # -- connection ----------------------------------------------------------

    def _get_collection(self) -> Any:
        """Return the collection, connecting lazily on first use.

        The ``pymongo`` import is deferred here (never at module scope) so this
        module stays driver-free and importable anywhere the shard stage runs,
        and merely building the adapter -- as :func:`get_adapter` does during
        validation -- neither imports pymongo nor opens a connection. AUTH is
        disabled on the surveyed store, so no credential is assembled; the
        connection is entirely from the locator.
        """
        if self._collection is not None:
            return self._collection
        try:
            from pymongo import MongoClient  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise ManifestSourceError(
                f"source {self.source.name!r}: the mongo adapter needs "
                "'pymongo', which is not installed. Install the backfill "
                "extra: pip install 'kbutillib[backfill]'. (The driver is "
                "imported lazily and only here, so every other adapter and "
                "the whole read surface work without it -- which is why this "
                "surfaces at first connection rather than at import.)"
            ) from exc

        uri = self.source.locator.get("uri")
        if uri:
            if not isinstance(uri, str):
                raise ManifestSourceError(
                    f"source {self.source.name!r}: mongo adapter 'uri', when "
                    "given, must be a string."
                )
            self._client = MongoClient(uri)
        else:
            host = self.source.locator.get("host", "localhost")
            port = int(self.source.locator.get("port", 27017))
            self._client = MongoClient(host, port)
        self._collection = self._client[self._database_name()][
            self._collection_name()
        ]
        return self._collection

    def _get_join_collection(self, join: MongoJoinSpec) -> Any:
        """Return the ``join`` locator's collection, injected or lazily resolved.

        Resolved from the SAME database as the primary collection -- the
        surveyed store keeps every protein collection in one database, and a
        cross-database join would need a second connection string the manifest
        does not carry.

        An INJECTED primary collection means no client was ever built, so there
        is nothing to resolve a second collection from. That is a test-shaped
        mistake and it is named as one rather than failing later on a ``None``.
        """
        if self._join_collection is not None:
            return self._join_collection
        self._get_collection()
        if self._client is None:
            raise ManifestSourceError(
                f"source {self.source.name!r}: mongo adapter 'join' needs the "
                f"collection {join.collection!r}, but the primary collection "
                "was injected so there is no client to resolve it from. Pass "
                "join_collection= as well, or let the adapter connect from the "
                "locator."
            )
        self._join_collection = self._client[self._database_name()][
            join.collection
        ]
        return self._join_collection

    # -- SourceAdapter interface --------------------------------------------

    def estimate_bytes(self) -> int:
        """Estimate the source size via ``estimated_document_count()``.

        Used only to pick the shard COUNT (see
        :meth:`SourceAdapter.estimate_bytes`); it need not be exact.
        ``estimated_document_count()`` reads collection metadata rather than
        scanning, so it is cheap even at 400M+ documents. When a ``min_key`` /
        ``max_key`` slice is set the metadata count over-estimates that slice,
        which only cuts more (smaller) ranges -- never a correctness problem.
        Returns at least 1 so ``ceil(total / target_bytes)`` is always >= 1.
        """
        collection = self._get_collection()
        count = int(collection.estimated_document_count())
        return max(1, count * MONGO_BYTES_PER_DOC)

    def iter_records(self) -> Iterator[Mapping[str, Any]]:
        """Yield each document as a dict row, paged, sorted and resumable.

        Streams a batched cursor -- NEVER loading the collection into memory --
        sorted ascending on ``sort_key`` so the read is a STABLE window that a
        killed run can resume (``start_after``) and a parallel run can slice
        (``min_key`` / ``max_key``) without overlap. Each document is copied to
        a plain dict, ``decompress`` fields are zlib-decoded (see
        ``decompress`` in the class docstring), and the row is yielded keyed by
        field name for the sharder to map.

        THE CURSOR IS CONSUMED A PAGE AT A TIME because a ``join`` is one
        ``$in`` per page; the page is ``batch_size`` documents, which is the
        granularity the cursor already fetches at. With no ``join`` declared
        the paging is pure chunking: the same documents, in the same order,
        with the same memory ceiling the cursor's own batch already imposes.

        The order of operations per page is the contract:

        1. REQUIRE the primary-only requirements, so a stub is dropped before
           the adapter pays to look its sequence up.
        2. JOIN the survivors with ONE ``$in``, dropping and counting the
           records with no match.
        3. DECOMPRESS -- the primary ``decompress`` mapping and the join's own
           ``decompress`` list.
        4. REQUIRE again, now over the merged record, for any requirement the
           join or a decompression produces.
        5. Claim the ``--limit`` budget and yield. The budget is claimed LAST,
           so a skipped record does not spend a row of the allowance; and it
           is claimed in the adapter's own loop so a ``--limit`` stop is NOT
           exhaustion (see :meth:`SourceAdapter._admit_record`). For mongo the
           budget is per source per slice, so each slice reads up to ``--limit``
           documents of its own.
        """
        collection = self._get_collection()
        cursor = collection.find(
            self._query_filter(), batch_size=self._batch_size()
        ).sort(self._sort_key(), 1)
        decompress = self._decompress_spec()
        join = self._join_spec()
        required = self._require_fields()
        # A requirement the join or a decompression PRODUCES cannot be judged
        # before the merge; every other one can, and judging it there is what
        # keeps a 28%-stub collection from driving 28% more join lookups.
        produced = set(decompress)
        if join is not None:
            produced.update(join.fields)
        pre_required = tuple(f for f in required if f not in produced)
        self._exhausted = False
        for page in _mongo_pages(cursor, self._batch_size()):
            kept = [doc for doc in page if self._satisfies(doc, pre_required)]
            if join is not None:
                kept = self._join_page(kept, join)
            for document in kept:
                row = self._prepare_row(document, decompress, join)
                if not self._satisfies(row, required):
                    continue
                if not self._admit_record():
                    return
                yield row
        self._exhausted = True

    # -- require_fields / join ----------------------------------------------

    def _skip(self, reason: str, count: int = 1) -> None:
        self._skipped[reason] = self._skipped.get(reason, 0) + count

    def _satisfies(
        self, row: Mapping[str, Any], required: Sequence[str]
    ) -> bool:
        """Whether ``row`` carries every one of ``required`` non-empty.

        Counts the FIRST failed requirement and returns immediately, so the
        per-reason counts sum to the number of records dropped rather than to
        the number of (record, requirement) pairs that failed.
        """
        for name in required:
            if _mongo_is_absent(row.get(name)):
                self._skip(f"{MONGO_SKIP_MISSING_PREFIX}{name}")
                return False
        return True

    def _join_page(
        self, page: Sequence[Mapping[str, Any]], join: MongoJoinSpec
    ) -> list[dict[str, Any]]:
        """Merge the join collection's fields into one page, with ONE ``$in``.

        The ``$in`` list is DEDUPLICATED and the projection is exactly
        ``join.on`` plus the fields ``join.fields`` reads, so the lookup moves
        the sequences and nothing else. A record with no match is dropped and
        counted under :data:`MONGO_SKIP_NO_JOIN_MATCH`; a match missing a
        projected field leaves that output field unset, exactly as a stub does
        under ``decompress``.
        """
        if not page:
            return []
        keys: list[Any] = []
        seen: set[Any] = set()
        for document in page:
            key = document.get(join.on)
            if key is not None and key not in seen:
                seen.add(key)
                keys.append(key)
        matches: dict[Any, Mapping[str, Any]] = {}
        if keys:
            projection = {join.on: 1}
            for input_field in join.fields.values():
                projection[input_field] = 1
            cursor = self._get_join_collection(join).find(
                {join.on: {"$in": keys}}, projection=projection
            )
            for document in cursor:
                matches[document.get(join.on)] = document
        merged: list[dict[str, Any]] = []
        for document in page:
            match = matches.get(document.get(join.on))
            if match is None:
                self._skip(MONGO_SKIP_NO_JOIN_MATCH)
                continue
            row = dict(document)
            for output_field, input_field in join.fields.items():
                if input_field in match:
                    row[output_field] = match[input_field]
            merged.append(row)
        return merged

    def estimate_rows(self) -> int | None:
        """Row count from ``estimated_document_count()`` -- collection metadata.

        Reads the collection's metadata rather than scanning, so it is cheap
        even at 400M+ documents, which is what makes ``kbu clearinghouse plan``
        usable on the real protein store at all. It is the collection-wide
        count: with a ``min_key``/``max_key`` slice set it OVER-estimates that
        slice, exactly as :meth:`estimate_bytes` does, and
        :meth:`estimate_rows_method` names the mechanism so the number is read
        as what it is.
        """
        return int(self._get_collection().estimated_document_count())

    def estimate_rows_method(self) -> str:
        return "mongo-estimated-document-count"

    def _prepare_row(
        self,
        document: Mapping[str, Any],
        decompress: Mapping[str, str],
        join: MongoJoinSpec | None = None,
    ) -> dict[str, Any]:
        """Copy a Mongo document to a plain dict, decoding compressed fields.

        For each ``output_field: input_field`` in ``decompress``, the raw
        (compressed) input field is replaced by its zlib-decompressed,
        UTF-8-decoded value under ``output_field``. A document missing the
        input field is yielded unchanged (no output field added) -- a stub row,
        which a manifest requiring the decoded column naturally rejects rather
        than the adapter fabricating a value.

        ``join.decompress`` is decoded IN PLACE, under the output name the
        merge already put the value under: a joined blob has no separate input
        field left to pop, because :meth:`_join_page` renamed it on the way in.
        """
        row = dict(document)
        for output_field, input_field in decompress.items():
            if input_field not in row:
                continue
            row[output_field] = _zlib_decode(row.pop(input_field))
        if join is not None:
            for output_field in join.decompress:
                if output_field in row:
                    row[output_field] = _zlib_decode(row[output_field])
        return row


def _mongo_pages(
    cursor: Iterable[Mapping[str, Any]], size: int
) -> Iterator[list[Mapping[str, Any]]]:
    """Chunk a cursor into lists of at most ``size`` documents, in order.

    A GENERATOR over the cursor, never a materialisation of it: at most
    ``size`` documents are held at once, which is the batch the cursor already
    fetches. The final page is short, and an EXACTLY FULL final page is
    followed by no empty page.
    """
    page: list[Mapping[str, Any]] = []
    for document in cursor:
        page.append(document)
        if len(page) >= size:
            yield page
            page = []
    if page:
        yield page


def _mongo_is_absent(value: Any) -> bool:
    """Whether ``require_fields`` should treat ``value`` as not carried.

    ``None`` and an EMPTY string / bytes / list / tuple / mapping / set are all
    absent: ``seq_protein_kofam2``'s ``kos`` was null in the one sampled
    document and ``protein_to_rast2``'s stubs carry no ``function`` at all, so
    "present but empty" and "absent" are the same fact about the store and
    deserve the same count.

    A NUMBER AND A BOOLEAN ARE PRESENT, including ``0`` and ``False``. A
    requirement is about whether the store recorded a value, not about whether
    the value is truthy, and dropping a legitimate ``0`` would be a silent data
    loss no count could explain.
    """
    if value is None:
        return True
    if isinstance(value, (str, bytes, bytearray, memoryview, list, tuple, dict, set)):
        return len(value) == 0
    return False


def _zlib_decode(value: Any) -> str:
    """Decode a zlib-compressed ``bytes``-like value to a UTF-8 string.

    Accepts ``bytes`` / ``bytearray`` / ``memoryview`` (a pymongo ``Binary`` is
    a ``bytes`` subclass, so it passes through). The blob is the amino-acid
    sequence compressed with zlib (magic ``0x789c``); decompression yields a
    plain uppercase amino-acid string. A value that is already a plain ``str``
    is returned as-is, so a fake double may yield an uncompressed sequence
    without special-casing.
    """
    import zlib  # noqa: PLC0415

    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return zlib.decompress(bytes(value)).decode("utf-8")
    raise ManifestSourceError(
        f"mongo adapter: cannot zlib-decode a value of type "
        f"{type(value).__name__}; expected bytes-like or str."
    )


class ManifestSourceError(RuntimeError):
    """An adapter could not read its declared source (missing path, etc.)."""


class AdapterNotImplementedError(NotImplementedError):
    """A manifest named a planned adapter with no class registered.

    Every declared adapter (``file``, ``lakehouse``, ``mongo``, ``arc``,
    ``kbdl_hash``) is now
    built, so this cannot fire for any of them in practice; it remains the
    distinct signal for a name that is in :data:`ADAPTER_NAMES` but maps to no
    class, versus a name that is not a known adapter at all.
    """


class ShardOrderError(RuntimeError):
    """A written shard violated the sort/range invariant.

    Raised by :func:`_assert_sorted_and_disjoint` when a shard's key column
    (``entity_hash``, or ``parameter_set_hash`` for the registry) is not
    ascending, or when two shards' ranges overlap by more than a shared
    BOUNDARY value. The build fails rather than shipping data files with
    useless min/max stats.
    """


# --------------------------------------------------------------------------
# The arc adapter: Chris's KOROS arc store, read in place
# --------------------------------------------------------------------------

#: The one table in ``runs.sqlite`` the adapter reads. Named rather than
#: inlined because every query below must name the SAME table, and because
#: the adapter must never reach for ``KorosArcStore``/``RunDatabase`` to get
#: at it -- their read path CREATES a missing database, which would turn "the
#: store is not where you said" into "the store is empty", silently, and the
#: run would report a clean zero.
ARC_RUNS_TABLE = "runs"

#: The two run kinds a clearinghouse backfill can read. Everything else in
#: the store (``kbdl.model_build``, ``kbdl.fitness_analysis``,
#: ``kbutillib.reconstruct``) produces no entity, content or result row this
#: corpus holds, so it is COUNTED AND SKIPPED by kind rather than ignored:
#: "430 records, 425 read, 5 not backfillable: kbdl.model_build 3, ..."
#: reconciles, and "425 records" does not.
ARC_BUILD_KIND = "kbdl.build_genome"
ARC_ANNOTATION_KIND = "kbdl.genome_annotation"
ARC_BACKFILLABLE_KINDS: tuple[str, ...] = (ARC_BUILD_KIND, ARC_ANNOTATION_KIND)

#: What a source's ``emit`` locator key may be. ONE adapter instance emits ONE
#: of these, because the four are four different record shapes over one store
#: and a single iterator yielding a mixture could not be mapped by one
#: manifest source. Four sources read the store, each naming its own.
ARC_EMIT_CHOICES: tuple[str, ...] = ("genome", "protein", "gene", "annotation")

#: The tarball's ledger members, paired with the annotation directory each one
#: keys. BOTH are read, and ONLY these paths: the ledger's keys are the file
#: STEMS inside its directory, so the adapter opens
#: ``data/annotations/<ledger key>.json`` BY NAME via ``tarfile.getmember``
#: and never walks the archive. A tarball is untrusted input -- a member named
#: ``../../etc/x`` or an absolute path is how extraction becomes arbitrary
#: file write -- and nothing here extracts anything: members are read as
#: streams and parsed in memory.
ARC_TARBALL_LEDGERS: tuple[tuple[str, str], ...] = (
    ("data/batch_ledger.json", "data/annotations"),
    ("data/pmi_ledger.json", "data/annotations_pmi"),
)

#: The tool name every tarball annotation member's terms are attributed to,
#: and the single ontology they use. The tarball shape carries no tool key of
#: its own -- the arc wrote one tool's output into these files -- so the
#: attribution is a fact about the archive format and belongs here rather
#: than being guessed per member.
ARC_TARBALL_TOOL = "RAST"

#: Per-arc-record byte estimate used ONLY by
#: :meth:`ArcSourceAdapter.estimate_bytes` to turn the record count into a
#: shard COUNT. It stands for the genome object a record pulls from KBDL, not
#: for the record row, because the objects are the bulk by three orders of
#: magnitude. An over- or under-estimate at worst changes how many
#: equal-width hash ranges are cut, never correctness.
ARC_BYTES_PER_RECORD = 1 << 20

#: Skip reasons the ``arc`` adapter reports. Short, stable slugs: they are
#: keys in ``run.json``, so renaming one invalidates every prior run's
#: inventory. They divide into two groups that must not be confused, because
#: the 430 records in the store only reconcile with the report if they are
#: kept apart (four sources read the same store, so a record "skipped" by one
#: source may be emitted by another):
#:
#: SOURCE-RECORD failures -- the arc record itself could not be read:
ARC_SKIP_RESULT_FILE_MISSING = "result-file-missing"
ARC_SKIP_KIND_PREFIX = "kind-not-backfillable:"
#: PER-TARGET-ROW drops -- the record was read, but one row it would have
#: produced cannot be mapped:
ARC_SKIP_OBJECT_UNAVAILABLE = "object-unavailable"
ARC_SKIP_NO_GENOME_FOR_PREFIX = "no-genome-for-prefix"
ARC_SKIP_FEATURE_NOT_IN_GENOME = "feature-not-in-genome"
ARC_SKIP_TRANSYT_WITHOUT_TAXONOMY = "transyt-without-taxonomy"
ARC_SKIP_TRANSYT_INVALID = "transyt-parameter-set-invalid"
ARC_SKIP_FASTA_POINTER_MISSING = "fasta-pointer-missing"
#: NOT a drop at all -- the row IS emitted, with a fallback timestamp, and
#: this counts how often that happened so the fallback is visible rather than
#: silent.
ARC_SKIP_TIMESTAMP_FALLBACK = "timestamp-fallback"
#: The fast path disagreed with the downloaded object and was discarded in
#: favour of the exact feature index. Not a drop either: the lookup still
#: succeeds, this records that ``fasta_object_id[:16]`` was not this genome's
#: ``genome_id``.
ARC_SKIP_FASTA_PREFIX_MISMATCH = "fasta-prefix-mismatch"
#: Two genome objects claim ONE feature id. Counted and then RAISED -- see
#: :class:`ArcFeatureIdConflictError`.
ARC_SKIP_FEATURE_ID_CONFLICT = "feature-id-conflict"

#: The SOURCE-RECORD failure reasons: an arc record that could not be READ at
#: all. Exposed as a named set, not left implicit in the comments above,
#: because the store's 430 records only reconcile with a report if these are
#: counted apart from the per-row drops. FOUR SOURCES READ ONE STORE: the same
#: record is read by all four, so a per-row drop in one source says nothing
#: about the other three, while a source-record failure applies to every
#: source that reads it. Summing the two groups together would
#: quadruple-count.
#:
#: ``kind-not-backfillable:<kind>`` is matched by PREFIX
#: (:data:`ARC_SKIP_KIND_PREFIX`) rather than listed, because the kind is part
#: of the slug.
ARC_SOURCE_RECORD_SKIPS: frozenset[str] = frozenset({ARC_SKIP_RESULT_FILE_MISSING})

#: The PER-TARGET-ROW drop reasons: the arc record was read, but one row it
#: would have produced could not be mapped. Counted per ROW, so a record whose
#: three tools all failed contributes three.
ARC_TARGET_ROW_SKIPS: frozenset[str] = frozenset(
    {
        ARC_SKIP_OBJECT_UNAVAILABLE,
        ARC_SKIP_NO_GENOME_FOR_PREFIX,
        ARC_SKIP_FEATURE_NOT_IN_GENOME,
        ARC_SKIP_TRANSYT_WITHOUT_TAXONOMY,
        ARC_SKIP_TRANSYT_INVALID,
        ARC_SKIP_FASTA_POINTER_MISSING,
    }
)

#: Reasons that are NEITHER a failure nor a drop: the row WAS emitted, and the
#: count records that something about it needed a fallback. Kept out of both
#: groups above so a reconciliation does not subtract rows that are present.
ARC_NOTED_REASONS: frozenset[str] = frozenset(
    {ARC_SKIP_TIMESTAMP_FALLBACK, ARC_SKIP_FASTA_PREFIX_MISMATCH}
)


#: The reason that is counted and then RAISED. It appears in a counter purely
#: so an inspector of the adapter can see it; the read did not continue, so no
#: reconciliation includes it.
ARC_REFUSAL_REASONS: frozenset[str] = frozenset({ARC_SKIP_FEATURE_ID_CONFLICT})


def classify_arc_skip(reason: str) -> str:
    """Which accounting group an ``arc`` skip reason belongs to.

    Returns ``"source-record"``, ``"target-row"``, ``"noted"``,
    ``"refusal"``, or ``"unknown"``. One function so a report consumer never
    has to re-derive the grouping -- and so adding a reason without
    classifying it shows up as ``"unknown"`` rather than silently landing in
    whichever bucket a caller guessed.
    """
    if reason.startswith(ARC_SKIP_KIND_PREFIX):
        return "source-record"
    if reason in ARC_SOURCE_RECORD_SKIPS:
        return "source-record"
    if reason in ARC_TARGET_ROW_SKIPS:
        return "target-row"
    if reason in ARC_NOTED_REASONS:
        return "noted"
    if reason in ARC_REFUSAL_REASONS:
        return "refusal"
    return "unknown"


#: The retryable skip reasons. A unit that recorded one of these is
#: INCOMPLETE even when every batch it wrote verified, because a retry would
#: get the missing records (see
#: :meth:`SourceAdapter.incomplete_reasons`). A missing result file or an
#: annotation for a genome that was never built is NOT retryable -- reading
#: it again reads the same absence.
ARC_RETRYABLE_SKIPS: frozenset[str] = frozenset({ARC_SKIP_OBJECT_UNAVAILABLE})

#: Adapter-state keys the ``arc`` adapter persists through the run context.
#: The SELECTION is the important one: it is frozen before any record is
#: read, so all four sources -- and a resume days later -- emit from exactly
#: the same ordered list of arc records rather than each re-querying a store
#: that has moved underneath them.
ARC_STATE_SELECTION = "selection"
ARC_STATE_SINCE = "since"
ARC_STATE_UPPER_BOUND = "upper_bound"
ARC_STATE_CAPPED = "capped"
ARC_STATE_TARBALLS = "tarballs"
ARC_STATE_RETRY = "retry"
ARC_STATE_INCOMPLETE = "incomplete"

#: Subdirectory of the clearinghouse state root holding per-SOURCE watermark
#: checkpoints. Not under a run id: a checkpoint's whole purpose is to outlive
#: the run that wrote it and be found by the NEXT run, which has a different
#: run id (see :func:`arc_checkpoint_path`).
ARC_CHECKPOINT_DIR = "sources"

#: Suffix of an UNPROMOTED checkpoint. A complete, uncapped run writes its
#: candidate here; only :func:`promote_arc_checkpoints`, called after the
#: unit's batches have VERIFIED, renames it to the real name. The two-step is
#: the whole safety property: the adapter cannot know whether what it emitted
#: landed, so it is not allowed to publish a watermark that says it did.
ARC_CHECKPOINT_CANDIDATE_SUFFIX = ".candidate.json"

#: Field on a ``run.json`` source entry listing the checkpoint candidate keys
#: that source wrote. The ONE name shared by the writer (``shard_manifest``)
#: and the reader (:func:`arc_checkpoint_candidates`), so the keys a unit
#: earned cannot be recorded under one spelling and promoted under another.
ARC_CHECKPOINT_CANDIDATES_FIELD = "arc_checkpoint_candidates"


class ArcFeatureIdConflictError(ManifestSourceError):
    """Two genome objects claim ONE feature id. The read is REFUSED.

    Never last-write-wins. A feature id is the join key between an annotation
    and the protein it annotates, so two objects claiming one id means every
    annotation on that id might belong to either genome -- and silently
    picking one would attribute a protein's annotations to a different
    organism's protein, with the entity hash of whichever sequence happened to
    be indexed last. There is no correct guess available, so the adapter
    stops and names both objects.
    """

    def __init__(self, feature_id: str, first: str, second: str) -> None:
        self.feature_id = feature_id
        self.object_ids = (first, second)
        super().__init__(
            f"arc adapter: {ARC_SKIP_FEATURE_ID_CONFLICT} -- feature id "
            f"{feature_id!r} is claimed by TWO genome objects, {first!r} and "
            f"{second!r}. A feature id is the join key from an annotation to "
            "its protein, so choosing one would attribute annotations to the "
            "wrong organism's sequence. The read is refused rather than "
            "resolved last-write-wins; reconcile the two objects in the "
            "store first."
        )


@dataclass
class _ArcGenome:
    """One genome object the adapter needs, and what the store says about it.

    Assembled from the arc store BEFORE the object is downloaded (which is
    why ``contigs``/``features`` start empty) and completed by
    :meth:`ArcSourceAdapter._load_objects`.
    """

    genome_object_id: str
    #: The FASTA object id the build record recorded, or ``None`` for a
    #: genome known only from a tarball ledger. ``None`` means no
    #: ``fasta_reference`` pointer can be written, which is a SKIP for the
    #: genome row and not an invented pointer.
    fasta_object_id: str | None = None
    taxonomy: dict[str, Any] = field(default_factory=dict)
    #: The EARLIEST build record timestamp seen for this genome, or the
    #: ledger's ``done_ts`` for a ledger-only genome.
    created_at: str | None = None
    contigs: list[dict[str, Any]] = field(default_factory=list)
    features: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: ``True`` once the object's bytes have been read.
    loaded: bool = False

    @property
    def ncbi_taxid(self) -> str | None:
        """The NCBI taxonomy id as a DECIMAL STRING, or ``None``.

        A string because that is what the clearinghouse stores and what
        lake-5's TRANSYT validator requires: an int would be reformatted by
        whichever JSON reader saw it next, and the parameter set would stop
        being byte-identical to what was asked for.
        """
        value = self.taxonomy.get("ncbi_taxid")
        if value is None or isinstance(value, bool):
            return None
        text = str(value).strip()
        return text or None

    def note_created_at(self, value: str | None) -> None:
        """Keep the EARLIEST timestamp offered for this genome.

        A genome can appear in several build records (a rebuild, a retry), and
        its ``created_at`` must not depend on which record the iteration
        happened to see last. The earliest is the one that says when this
        genome first existed.
        """
        if not value:
            return
        if self.created_at is None or value < self.created_at:
            self.created_at = value

#: Default ``db_path``: the arc store's run index on the BERDL pod, as a
#: manifest PLACEHOLDER WITH A DEFAULT so the preset works untouched and an
#: operator can point it elsewhere without editing package data.
ARC_DEFAULT_DB_PATH = "~/.kbdl/runs.sqlite"

#: Default ``tarballs``: the pod's arc backup. A DEFAULT and not a required
#: value -- but once configured (default or not) an absent file is an ERROR,
#: because the operator is then relying on annotations that would silently
#: not be there (see :meth:`ArcSourceAdapter._tarball_paths`).
ARC_DEFAULT_TARBALL = (
    "~/arc-relocate-backup-20260923T1307Z/"
    "inner-loop__additional-genome-annotation.tar.gz"
)

#: Default object cache when the adapter is NOT running under a backfill run
#: (a plain ``kbu clearinghouse shard``, or a test). Under a run, the cache
#: goes to ``<shard out>/objects`` so the four sources of one unit share it.
ARC_DEFAULT_CACHE_DIR = "~/clearinghouse-shards/objects"


def arc_checkpoint_dir() -> Path:
    """Where per-source arc watermark checkpoints live.

    Under the clearinghouse STATE ROOT (``$KBU_CLEARINGHOUSE_STATE_DIR``, or
    the pod-persistent default), beside the per-execution ledger directories
    rather than inside one: a checkpoint must be found by the NEXT run, which
    has a different run id, and must survive the shard output being
    regenerated, moved or deleted.
    """
    from . import clearinghouse_ingest as ci  # noqa: PLC0415 - avoids a cycle

    return ci.resolve_state_root() / ARC_CHECKPOINT_DIR


def arc_checkpoint_key(
    *,
    tenant: str,
    dataset: str,
    db_path: str,
    base_url: str,
    principal: str,
) -> str:
    """The identity a watermark checkpoint is keyed by.

    FIVE components, and every one of them changes what the watermark MEANS:

    - ``tenant`` and ``dataset`` -- the WRITE TARGET. A watermark says "every
      arc record up to this time is already in the lake", which is a claim
      about ONE namespace. A scratch dataset's checkpoint applied to the
      production dataset would skip the records production has never seen,
      and the gap would be invisible: the run would report a clean, complete,
      empty-ish pass. This is the component that makes "scratch never applies
      to production" true by construction rather than by a flag someone has
      to remember to pass.
    - ``db_path`` -- the STORE, absolute. Two arc stores have two independent
      histories.
    - ``base_url`` -- the KBDL endpoint the genome objects came from. The same
      object id means the same bytes, but a different endpoint may simply not
      have them.
    - ``principal`` -- WHO read it. KBDL shows a caller only the objects it
      may see, so a watermark earned with broad access would tell a narrower
      caller it had already covered records whose objects it cannot fetch.

    Returned as a sha256 hex digest, because the components include an
    absolute path and a URL and the result has to be one safe filename. The
    components themselves are stored INSIDE the checkpoint file, so a
    checkpoint is still diagnosable without reversing the hash.
    """
    material = "\\n".join(
        [
            f"tenant={tenant}",
            f"dataset={dataset}",
            f"db_path={db_path}",
            f"base_url={base_url}",
            f"principal={principal}",
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def arc_checkpoint_path(key: str, *, candidate: bool = False) -> Path:
    """The promoted (or candidate) checkpoint file for one identity key."""
    suffix = ARC_CHECKPOINT_CANDIDATE_SUFFIX if candidate else ".json"
    return arc_checkpoint_dir() / f"arc-{key}{suffix}"


def read_arc_checkpoint(key: str) -> dict[str, Any]:
    """The PROMOTED checkpoint for one identity key, or ``{}``.

    A candidate is never read. An unreadable or non-object file reads as
    ``{}``, which degrades to "no checkpoint, start from the beginning" -- the
    safe direction, because a full re-read of an arc store is cheap
    (fill-only ingest discards what is already there) while a bad watermark
    skips records permanently.
    """
    path = arc_checkpoint_path(key)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def promote_arc_checkpoints(keys: Iterable[str]) -> list[Path]:
    """Promote the NAMED candidate checkpoints to real ones. Returns what moved.

    Called by the backfill driver AFTER a unit's batches have been ingested
    and VERIFIED, and only for a unit that came out ``done``. Until then a
    candidate exists and nothing reads it, so a run that was killed, capped,
    or left incomplete advances no watermark -- which is the property the
    two-step exists for. The adapter writes the candidate because only it
    knows the ``upper_bound`` and which tarballs it finished; the driver
    promotes it because only the driver knows whether the rows landed.

    THE KEYS ARE REQUIRED, and there is deliberately no promote-everything
    form. :func:`arc_checkpoint_dir` is ONE directory under the state root,
    shared by every arc source of every manifest on the machine, so a glob
    over it is not scoped to the unit that just verified. Promoting what it
    finds means: store B's run leaves a candidate without reaching ``done``
    (its batches did not verify, or it was killed after sharding); any later
    run A, from any manifest, finishes any unit; A promotes B's candidate;
    B's next run starts after rows that never landed. The lake is
    append-only and fill-only, so nothing ever comes back for that range --
    the gap is permanent and reports clean. The driver therefore promotes
    only the keys the just-completed unit's own run inventory recorded (see
    :func:`arc_checkpoint_candidates`).

    Idempotent, per key: a named key whose candidate is absent -- already
    promoted, or never written -- moves nothing and raises nothing.
    """
    moved: list[Path] = []
    for key in dict.fromkeys(keys):
        candidate = arc_checkpoint_path(key, candidate=True)
        if not candidate.is_file():
            continue
        target = arc_checkpoint_path(key)
        os.replace(candidate, target)
        moved.append(target)
    return moved


def _arc_is_iso_timestamp(value: Any) -> bool:
    """Whether ``value`` is a string ``datetime.fromisoformat`` can parse.

    Used to decide whether a ledger's ``done_ts`` is USABLE, which is the
    only question asked of it: a ledger entry that was written mid-crash can
    carry ``null``, ``""`` or a placeholder, and a tarball row must then fall
    back to the run start time WITH A COUNTED REASON rather than carry a
    timestamp that no reader can order.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        resolve_observed_at(value, where="arc ledger done_ts")
    except ShardValueError:
        return False
    return True


class ArcSourceAdapter(SourceAdapter):
    """The ``arc`` adapter: Chris's KOROS arc store, read IN PLACE on the pod.

    Reads four things and joins them:

    1. ``~/.kbdl/runs.sqlite`` -- the arc's run index, opened through a
       READ-ONLY sqlite URI (``file:...?mode=ro``). A missing file RAISES and
       CREATES NOTHING, which is the whole reason the adapter opens the
       database itself instead of going through ``KorosArcStore`` /
       ``RunDatabase``: their read path creates a missing database, so a
       mistyped path would have turned "the store is not there" into "the
       store is empty" and the run would have reported a clean zero.
    2. ``~/.kbdl/job-results/KBDLBuildGenome/<job>.json.gz`` and
       ``.../KBDLGenomeAnnotation/<job>.json.gz`` -- the preserved job
       results, opened with :mod:`gzip` for reading only.
    3. Optional backup TARBALLS of a KOROS arc, streamed with :mod:`tarfile`.
       Only the four member paths in :data:`ARC_TARBALL_LEDGERS` are opened,
       BY NAME; nothing is ever extracted.
    4. The ``CanonicalGenome`` objects, pulled from KBDL through
       :meth:`~kbutillib.domains.external.kbdl_service_utils.KBDLServiceUtils.download_object`
       -- content-addressed, so each download verifies its own sha256 against
       the object id. THE POD HAS NO SEQUENCES; every sequence in the corpus
       comes from these objects, which is why an unavailable object is a
       retryable failure and not a drop.

    KBUtilLib NEVER IMPORTS ``kbdl_service``. The store is read as files and
    rows; the objects are fetched over KBDL's HTTP contract. Nothing here
    imports the service package, and the facts the adapter relies on about
    the service's formats (a ``CanonicalGenome``'s ``genome_id`` is
    ``fasta_object_id[:16]``; its feature ids are ``f"{genome_id}_{index}"``)
    are encoded here as read-only expectations, checked against the
    downloaded bytes.

    ONE SOURCE EMITS ONE SHAPE. ``emit`` selects which: ``genome`` (one record
    per distinct genome object), ``protein`` / ``gene`` (one per feature of
    each genome object) or ``annotation`` (one per ``(feature, tool)`` pair
    with a non-empty ontology map). The arc-kbdl preset declares four sources
    over one store, one per emit, and they share ONE FROZEN SELECTION so the
    rows they load are a coherent set rather than four independent samples.

    Locator keys:

    - ``db_path`` -- the runs database. Defaults to
      :data:`ARC_DEFAULT_DB_PATH`.
    - ``emit`` -- REQUIRED, one of :data:`ARC_EMIT_CHOICES`.
    - ``tarballs`` -- optional; a list of paths, or one comma-separated
      string (which is what a ``${VAR}`` placeholder expands to). A configured
      tarball that DOES NOT EXIST IS AN ERROR, not a skip: the operator named
      it, so its absence is a mistake to fix and not a quiet reduction in
      what the backfill covers. An empty string is "no tarballs" and is fine.
    - ``since`` -- optional ISO timestamp, INCLUSIVE, compared against
      ``updated_at``. Omitted, the adapter starts from its persisted
      watermark checkpoint if there is one, and from the beginning otherwise.
      Given, it OVERRIDES the checkpoint -- an explicit instruction beats a
      remembered one.
    - ``base_url`` -- optional KBDL endpoint; the client's own default
      otherwise.
    - ``cache_dir`` -- where downloaded genome objects are kept, so the four
      sources (and a resume) fetch each object ONCE. Defaults to
      ``<shard out>/objects`` when the adapter is running under a backfill run
      and ``~/clearinghouse-shards/objects`` otherwise.

    THE DEPENDENCY INDEX AND THE SELECTION ARE DIFFERENT THINGS, and
    conflating them is the subtle failure this adapter is shaped to avoid.
    The INDEX -- which genome object a feature id belongs to, what its
    taxonomy is, where its FASTA lives -- is built from EVERY build record and
    EVERY tarball ledger, REGARDLESS of ``since``. It drives JOINS only.
    The SELECTION -- which records this run EMITS from -- is the arc record
    ids with ``since <= updated_at <= upper_bound``, in ``(updated_at,
    record_id)`` order, frozen through the run context before anything is
    read. An incremental run must still be able to join a late annotation to
    a genome built months ago (so the index ignores ``since``) without
    re-emitting that genome's rows (so the selection does not).

    Args:
        source: The parsed ``[[source]]`` block.
        client: An object exposing ``download_object(object_id, dest)`` --
            a real
            :class:`~kbutillib.domains.external.kbdl_service_utils.KBDLServiceUtils`
            in production, a fake in tests. Constructed lazily on first use
            when not given, so building the adapter (as :func:`get_adapter`
            does) makes no network call and needs no credentials.
    """

    adapter_name = "arc"

    def __init__(self, source: cm.Source, *, client: Any = None):
        super().__init__(source)
        self._client = client
        #: The greatest ``updated_at`` among the records READ. See
        #: :meth:`watermark`.
        self._watermark: str | None = None
        #: Retryable failures, as human strings. Non-empty => incomplete.
        self._retry: list[str] = []
        #: Identity keys this read wrote a checkpoint CANDIDATE for. See
        #: :meth:`checkpoint_candidates`.
        self._candidates: list[str] = []
        #: ``True`` when ``--limit`` truncated the frozen selection, which
        #: makes the read non-exhaustive and forbids a checkpoint.
        self._capped = False
        #: This read's start time, ISO-8601 ``Z``. The fallback timestamp for
        #: a tarball member whose ledger ``done_ts`` is unusable.
        self._run_start = _iso_z(utc_now())
        self._index: dict[str, _ArcGenome] | None = None
        #: ``fasta_object_id[:16]`` -> genome object id. The FAST PATH, used
        #: to decide which objects to download; checked against the exact
        #: feature index once they are.
        self._fast_path: dict[str, str] = {}
        #: Exact feature id -> genome object id, built from the DOWNLOADED
        #: objects. The authoritative join.
        self._feature_owner: dict[str, str] = {}
        #: Ledger key -> genome object id, per tarball path.
        self._ledger_genomes: dict[str, dict[str, str]] = {}
        #: Ledger key -> ledger entry, per tarball path.
        self._ledger_entries: dict[str, dict[str, Any]] = {}
        #: sha256 of each configured tarball, by path.
        self._tarball_digests: dict[str, str] = {}

    # -- locator ------------------------------------------------------------

    def _locator(self, key: str, default: Any = None) -> Any:
        return self.source.locator.get(key, default)

    def _db_path(self) -> Path:
        raw = self._locator("db_path") or ARC_DEFAULT_DB_PATH
        return Path(str(raw)).expanduser()

    def _emit(self) -> str:
        value = self._locator("emit")
        if value not in ARC_EMIT_CHOICES:
            raise ManifestSourceError(
                f"source {self.source.name!r}: arc adapter requires "
                f"'emit' to be one of {', '.join(ARC_EMIT_CHOICES)}, got "
                f"{value!r}. One source emits ONE shape; the arc preset "
                "declares four sources over the one store."
            )
        return str(value)

    def _since(self) -> str | None:
        """The INCLUSIVE lower bound on ``updated_at``, or ``None``.

        An explicit ``since`` wins over the persisted checkpoint, always: the
        operator is saying where to start and a remembered position must not
        override an instruction. With no ``since``, the promoted checkpoint's
        ``upper_bound`` becomes this run's lower bound -- inclusive on both
        ends, which can re-read the single boundary record and is the right
        side to err on (fill-only ingest discards a row already present,
        while an exclusive bound that was off by a tick would drop one
        permanently).
        """
        explicit = self._locator("since")
        if explicit:
            return str(explicit)
        checkpoint = read_arc_checkpoint(self._checkpoint_key())
        bound = checkpoint.get(ARC_STATE_UPPER_BOUND)
        return str(bound) if bound else None

    def _tarball_paths(self) -> list[Path]:
        """The configured tarballs. A configured-but-absent one RAISES."""
        raw = self._locator("tarballs", ARC_DEFAULT_TARBALL)
        if raw is None:
            raw = ""
        if isinstance(raw, (list, tuple)):
            candidates = [str(item).strip() for item in raw]
        else:
            # A ``${KBU_ARC_TARBALL:-...}`` placeholder expands to a string,
            # and the empty expansion must mean "none" rather than one
            # tarball named "".
            candidates = [part.strip() for part in str(raw).split(",")]
        paths: list[Path] = []
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate).expanduser()
            if not path.is_file():
                raise ManifestSourceError(
                    f"source {self.source.name!r}: arc adapter was "
                    f"configured with tarball {str(path)!r}, which does not "
                    "exist. A configured tarball is not optional -- an "
                    "operator named it, so its absence is a mistake to fix "
                    "and not a quiet reduction in what this backfill covers. "
                    "Set tarballs = \"\" to run without any."
                )
            paths.append(path)
        return paths

    def _cache_dir(self) -> Path:
        # An EMPTY string is "unset", not a cache in the current directory:
        # the preset declares ``cache_dir = "${KBU_ARC_CACHE_DIR:-}"`` so the
        # variable is optional, and an unset optional placeholder expands to
        # "" rather than disappearing.
        raw = self._locator("cache_dir")
        if raw and str(raw).strip():
            return Path(str(raw)).expanduser()
        context = self.run_context
        out_dir = getattr(context, "out_dir", None) if context else None
        if out_dir is not None:
            return Path(out_dir) / "objects"
        return Path(ARC_DEFAULT_CACHE_DIR).expanduser()

    def _base_url(self) -> str:
        return str(self._locator("base_url") or "")

    # -- the KBDL client and the object cache --------------------------------

    def _get_client(self) -> Any:
        """The KBDL client, deferring the import until first use.

        Deferred (not at module scope) so this module stays importable
        wherever the shard stage is -- exactly as
        :meth:`LakehouseSourceAdapter._get_capability` defers its capability
        import.
        """
        if self._client is None:
            from ...external.kbdl_service_utils import (  # noqa: PLC0415
                KBDLServiceUtils,
            )

            base_url = self._base_url()
            self._client = (
                KBDLServiceUtils(base_url=base_url)
                if base_url
                else KBDLServiceUtils()
            )
        return self._client

    def _principal(self) -> str:
        """A stable, non-secret fingerprint of WHO is reading KBDL.

        A component of the checkpoint key (see :func:`arc_checkpoint_key`),
        because KBDL shows a caller only the objects it may see. It is the
        first 16 hex characters of the sha256 of the bearer token, NOT the
        token: a checkpoint file name is written to disk and read by anything
        that can list the directory, so the raw credential must never be in
        it. ``"anonymous"`` when there is no token -- itself a distinct
        principal, since an unauthenticated reader sees a different set of
        objects again.
        """
        client = self._client
        token = None
        if client is not None:
            getter = getattr(client, "get_token", None)
            if callable(getter):
                try:
                    token = getter(namespace="kbase")
                except Exception:  # pragma: no cover - a fake with no token
                    token = None
        if not token:
            return "anonymous"
        return hashlib.sha256(str(token).encode("utf-8")).hexdigest()[:16]

    def _checkpoint_key(self) -> str:
        context = self.run_context
        return arc_checkpoint_key(
            tenant=str(getattr(context, "tenant", "") or ""),
            dataset=str(getattr(context, "dataset", "") or ""),
            db_path=str(self._db_path().resolve()),
            base_url=self._base_url(),
            principal=self._principal(),
        )

    def _object_path(self, object_id: str) -> Path:
        return self._cache_dir() / f"{object_id}.json"

    def _cached_object(self, object_id: str) -> Path | None:
        """A cached file for ``object_id`` whose sha256 MATCHES, or ``None``.

        The digest is re-checked on REUSE and not only on download. A cache
        entry is a file on a shared pod that something else may have
        truncated, and the cost of being wrong is every entity hash derived
        from those bytes -- so the cheap re-hash is paid every time rather
        than trusted because the file exists. A reference object (``ref:``)
        has no digest to check and is reused on presence alone.
        """
        from ...external.kbdl_service_utils import (  # noqa: PLC0415
            is_content_object,
        )

        path = self._object_path(object_id)
        if not path.is_file():
            return None
        if not is_content_object(object_id):
            return path
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        if digest.hexdigest() == object_id:
            return path
        path.unlink(missing_ok=True)
        return None

    def _fetch_object(self, object_id: str) -> dict[str, Any] | None:
        """Download (or reuse) and parse one genome object; ``None`` if
        unavailable.

        An unavailable object is a RETRYABLE failure: the record is not
        emitted, the reason is counted as
        :data:`ARC_SKIP_OBJECT_UNAVAILABLE`, and the unit comes out
        INCOMPLETE even if every batch it did write verified -- so a later
        ``--resume`` comes back for it. That is the opposite of a missing
        result file, which is an absence in the store itself and will read
        the same way forever.
        """
        cached = self._cached_object(object_id)
        if cached is None:
            try:
                cached = self._get_client().download_object(
                    object_id, self._object_path(object_id)
                )
            except Exception as exc:
                self._note_retryable(
                    f"genome object {object_id!r} could not be downloaded: "
                    f"{type(exc).__name__}: {exc}"
                )
                return None
        try:
            with Path(cached).open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            self._note_retryable(
                f"genome object {object_id!r} was downloaded but could not be "
                f"parsed as JSON: {type(exc).__name__}: {exc}"
            )
            return None
        if not isinstance(payload, dict):
            self._note_retryable(
                f"genome object {object_id!r} is not a JSON object "
                f"({type(payload).__name__}); a CanonicalGenome is."
            )
            return None
        return payload

    # -- skip accounting ----------------------------------------------------

    def _skip(self, reason: str, count: int = 1) -> None:
        self._skipped[reason] = self._skipped.get(reason, 0) + count

    def _note_retryable(self, detail: str) -> None:
        """Record a retryable failure: counted AND makes the unit incomplete."""
        self._skip(ARC_SKIP_OBJECT_UNAVAILABLE)
        if detail not in self._retry:
            self._retry.append(detail)

    def skipped(self) -> dict[str, int]:
        return dict(self._skipped)

    def watermark(self) -> str | None:
        return self._watermark

    def incomplete_reasons(self) -> tuple[str, ...]:
        return tuple(self._retry)

    def checkpoint_candidates(self) -> tuple[str, ...]:
        return tuple(self._candidates)

    def _note_watermark(self, updated_at: Any) -> None:
        if not isinstance(updated_at, str) or not updated_at:
            return
        if self._watermark is None or updated_at > self._watermark:
            self._watermark = updated_at

    # -- estimates ----------------------------------------------------------

    def estimate_bytes(self) -> int:
        """Rough total size: the index, the tarballs, and the objects behind
        them.

        The genome objects dominate by three orders of magnitude and are not
        on disk yet, so they are estimated as
        :data:`ARC_BYTES_PER_RECORD` per backfillable arc record. The figure
        only chooses a shard COUNT.
        """
        db_path = self._db_path()
        total = db_path.stat().st_size if db_path.is_file() else 0
        for tarball in self._tarball_paths():
            total += tarball.stat().st_size
        with self._connect() as connection:
            placeholders = ", ".join("?" for _ in ARC_BACKFILLABLE_KINDS)
            (count,) = connection.execute(
                f"SELECT COUNT(*) FROM {ARC_RUNS_TABLE} WHERE kind IN "
                f"({placeholders})",
                ARC_BACKFILLABLE_KINDS,
            ).fetchone()
        return total + int(count) * ARC_BYTES_PER_RECORD

    def estimate_rows(self) -> int | None:
        """``None`` -- the arc store has no cheap count of EMITTED rows.

        A ``COUNT(*)`` over the index is cheap, but it counts ARC RECORDS, and
        an arc record is not a row for any of the four emits: one build
        record becomes one genome row, a few thousand protein rows and as
        many gene rows, and one annotation record becomes one row per
        ``(feature, tool)`` pair with a non-empty ontology map. Reporting the
        record count as a row count would be a confident wrong number, which
        is worse for ``plan`` than no number: the honest answer is that the
        row count is only knowable by reading the results, and
        :meth:`SourceAdapter.estimate_rows` exists precisely so an adapter
        can decline rather than pay for a scan behind the caller's back.
        """
        return None

    # -- the store ----------------------------------------------------------

    def _connect(self) -> Any:
        """Open ``runs.sqlite`` READ-ONLY, creating nothing.

        ``file:<path>?mode=ro`` with ``uri=True``: a missing file raises
        ``sqlite3.OperationalError`` and NO FILE IS CREATED. That is the
        difference between this and ``KorosArcStore``/``RunDatabase``, whose
        read path creates a missing database -- against which a mistyped
        ``db_path`` reads as an empty store and the run reports a clean zero.
        """
        import sqlite3  # noqa: PLC0415 - stdlib, deferred to keep import cheap

        path = self._db_path()
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.OperationalError as exc:
            raise ManifestSourceError(
                f"source {self.source.name!r}: arc adapter could not open "
                f"the runs database {str(path)!r} read-only: {exc}. Nothing "
                "was created -- the read-only URI is deliberate, so a "
                "mistyped path fails here instead of reading as an empty "
                "store and reporting a clean zero."
            ) from exc
        connection.row_factory = sqlite3.Row
        return connection

    def _freeze_selection(self) -> dict[str, Any]:
        """Return the FROZEN selection, reading it from state when it exists.

        THE SELECTION IS PERSISTED BEFORE ANY RECORD IS READ, and every later
        reader -- the other three sources of the preset, and a resume days
        later -- reuses it rather than re-querying. Two reasons, both load
        bearing:

        - FOUR SOURCES, ONE SAMPLE. ``--limit 1`` must select ONE arc record
          that all four sources then work from, so the loaded rows are one
          coherent record's genome, proteins, genes and annotations. Four
          independent queries with four independent caps would load one
          genome, a different genome's proteins, and so on.
        - A RESUME MUST NOT MOVE. Re-querying on resume would pick up records
          written since the run started, which the run's own ``upper_bound``
          says it does not cover, and would renumber the ``--limit`` window.

        The ``upper_bound`` is captured BEFORE the select, so a record written
        while the run is reading is outside this run's window by construction
        rather than by luck, and the watermark this run may later publish is
        a time it genuinely covered.
        """
        state = self._read_state()
        if state.get(ARC_STATE_SELECTION) is not None:
            self._capped = bool(state.get(ARC_STATE_CAPPED))
            return state

        upper_bound = _iso_z(utc_now())
        since = self._since()
        limit = self._record_limit()

        with self._connect() as connection:
            placeholders = ", ".join("?" for _ in ARC_BACKFILLABLE_KINDS)
            params: list[Any] = [*ARC_BACKFILLABLE_KINDS, upper_bound]
            clause = (
                f"kind IN ({placeholders}) AND updated_at <= ?"
            )
            if since:
                clause += " AND updated_at >= ?"
                params.append(since)
            rows = connection.execute(
                f"SELECT record_id FROM {ARC_RUNS_TABLE} WHERE {clause} "
                "ORDER BY updated_at, record_id",
                params,
            ).fetchall()
            # Everything that is NOT backfillable is counted by kind, so the
            # store's record total reconciles with the report instead of
            # quietly shrinking to the kinds this corpus happens to hold.
            for row in connection.execute(
                f"SELECT kind, COUNT(*) AS n FROM {ARC_RUNS_TABLE} WHERE kind "
                f"NOT IN ({placeholders}) GROUP BY kind",
                ARC_BACKFILLABLE_KINDS,
            ).fetchall():
                self._skip(f"{ARC_SKIP_KIND_PREFIX}{row['kind']}", int(row["n"]))

        selection = [row["record_id"] for row in rows]
        capped = limit is not None and len(selection) > limit
        if capped:
            selection = selection[:limit]
        self._capped = capped

        frozen = {
            ARC_STATE_SELECTION: selection,
            ARC_STATE_SINCE: since,
            ARC_STATE_UPPER_BOUND: upper_bound,
            ARC_STATE_CAPPED: capped,
            ARC_STATE_TARBALLS: self._tarball_selection(),
        }
        self._write_state(frozen)
        return frozen

    def _record_limit(self) -> int | None:
        """``--limit`` as a count, or ``None``.

        Applied ONCE, to the frozen selection, rather than per reader through
        :meth:`SourceAdapter._admit_record`. The arc store's four sources are
        four readers of ONE ordered selection, and the cap belongs to the
        selection so that all four see the same records -- which is exactly
        the case :meth:`SourceAdapter._admit_record` documents as the
        adapter's own responsibility to arrange.
        """
        context = self.run_context
        if context is None:
            return None
        getter = getattr(context, "record_limit", None)
        if not callable(getter):
            return None
        value = getter(self.source.name)
        return None if value is None else int(value)

    def _read_state(self) -> dict[str, Any]:
        context = self.run_context
        if context is None:
            return {}
        getter = getattr(context, "get_state", None)
        if not callable(getter):
            return {}
        # The selection is shared by every source reading this store, so it
        # is stored under the ADAPTER NAME rather than the source name --
        # four sources, one frozen list.
        return getter(self.adapter_name)

    def _write_state(self, value: Mapping[str, Any]) -> None:
        context = self.run_context
        if context is None:
            return
        putter = getattr(context, "put_state", None)
        if not callable(putter):
            return
        putter(self.adapter_name, dict(value))

    def _tarball_selection(self) -> dict[str, str]:
        """``{sha256: path}`` for every configured tarball NOT yet completed.

        Completion is recorded BY DIGEST, not by path: a backup tarball can
        be moved or renamed and is still the same archive, while a file at
        the same path may have been replaced by a different one.
        """
        done = set(read_arc_checkpoint(self._checkpoint_key()).get("tarballs") or [])
        selected: dict[str, str] = {}
        for path in self._tarball_paths():
            digest = _arc_file_sha256(path)
            self._tarball_digests[str(path)] = digest
            if digest not in done:
                selected[digest] = str(path)
        return selected

    # -- the dependency index ------------------------------------------------

    def _result_path(self, artifacts: Any) -> Path | None:
        """The ``.json.gz`` a record's ``artifacts`` JSON points at."""
        if isinstance(artifacts, str):
            try:
                artifacts = json.loads(artifacts)
            except ValueError:
                return None
        if not isinstance(artifacts, Mapping):
            return None
        result = artifacts.get("result")
        if not result or not isinstance(result, str):
            return None
        return Path(result).expanduser()

    def _read_result(self, record: Mapping[str, Any]) -> Any | None:
        """Read one record's gzipped job result, or ``None`` (counted).

        Opened ``"rt"`` -- READ ONLY -- and a missing file is counted as
        :data:`ARC_SKIP_RESULT_FILE_MISSING` and skipped. That is a
        SOURCE-RECORD failure, not a per-row drop, and it is NOT retryable:
        the file is absent from the pod's disk and will be absent next time
        too.
        """
        path = self._result_path(record.get("artifacts"))
        if path is None or not path.is_file():
            self._skip(ARC_SKIP_RESULT_FILE_MISSING)
            return None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            self._skip(ARC_SKIP_RESULT_FILE_MISSING)
            return None

    def _build_index(self) -> dict[str, _ArcGenome]:
        """Build the JOIN index from EVERY build record and EVERY ledger.

        ``since`` IS DELIBERATELY IGNORED HERE. An incremental run has to join
        a late annotation to a genome that was built months before the
        window, so the index covers the whole store; which records are
        EMITTED is the frozen selection's business and nothing in this method
        touches it.

        THE JOIN KEY IS ``fasta_object_id[:16]``, NEVER
        ``BuiltGenomeEntry.prefix``. The build result's ``prefix`` is a CLASS
        prefix -- ``'unk'``, ``'eco'`` -- shared by every genome of that
        class, so using it as a join key would attribute one genome's
        annotations to another's proteins. A ``CanonicalGenome``'s own
        ``genome_id`` is ``fasta_object_id[:16]`` and its feature ids are
        ``f"{genome_id}_{index}"`` (KBDLJobRunningPrototype
        ``build_genome.py``), which is what this index is keyed on -- and the
        key is only a FAST PATH: it decides which objects to download, and
        :meth:`_load_objects` then checks it against each object's real
        feature ids.
        """
        if self._index is not None:
            return self._index
        index: dict[str, _ArcGenome] = {}

        with self._connect() as connection:
            records = connection.execute(
                f"SELECT record_id, run_uid, artifacts, created_at, updated_at "
                f"FROM {ARC_RUNS_TABLE} WHERE kind = ? "
                "ORDER BY created_at, record_id",
                (ARC_BUILD_KIND,),
            ).fetchall()
        # Materialised as plain dicts: a ``sqlite3.Row`` is a sequence with
        # keyed access but NO ``.get``, and every reader below treats a
        # record as a Mapping.
        for record in (dict(row) for row in records):
            payload = self._read_result(record)
            if not isinstance(payload, Mapping):
                continue
            genomes = payload.get("genomes")
            if not isinstance(genomes, Mapping):
                continue
            for entry in genomes.values():
                if not isinstance(entry, Mapping):
                    continue
                genome_object_id = entry.get("genome_object_id")
                fasta_object_id = entry.get("fasta_object_id")
                if not genome_object_id:
                    continue
                known = index.setdefault(
                    str(genome_object_id),
                    _ArcGenome(genome_object_id=str(genome_object_id)),
                )
                if fasta_object_id:
                    known.fasta_object_id = str(fasta_object_id)
                    self._fast_path.setdefault(
                        str(fasta_object_id)[:16], str(genome_object_id)
                    )
                taxonomy = entry.get("taxonomy")
                if isinstance(taxonomy, Mapping) and not known.taxonomy:
                    known.taxonomy = dict(taxonomy)
                known.note_created_at(record["created_at"])

        self._index_ledgers(index)
        self._index = index
        return index

    def _index_ledgers(self, index: dict[str, _ArcGenome]) -> None:
        """Fold every configured tarball's ledgers into the index.

        A ledger-only genome -- one the ledger names but no build record in
        this store does -- is INDEXED and its proteins, genes and annotations
        are emitted, because the genome object itself has every sequence they
        need. Its ``genome`` row is a different matter: without a real
        ``fasta_object_id`` there is no ``fasta_reference`` pointer to write,
        and :meth:`_emit_genomes` skips it as
        :data:`ARC_SKIP_FASTA_POINTER_MISSING` rather than inventing one.
        """
        for path in self._tarball_paths():
            key = str(path)
            self._ledger_genomes.setdefault(key, {})
            self._ledger_entries.setdefault(key, {})
            with tarfile.open(path, "r:gz") as archive:
                for ledger_member, _annotation_dir in ARC_TARBALL_LEDGERS:
                    ledger = _arc_read_member(archive, ledger_member)
                    if not isinstance(ledger, Mapping):
                        continue
                    for ledger_key, entry in ledger.items():
                        if not isinstance(entry, Mapping):
                            continue
                        genome_object_id = entry.get("genome_object_id")
                        if not genome_object_id:
                            continue
                        self._ledger_genomes[key][str(ledger_key)] = str(
                            genome_object_id
                        )
                        self._ledger_entries[key][str(ledger_key)] = dict(entry)
                        known = index.setdefault(
                            str(genome_object_id),
                            _ArcGenome(genome_object_id=str(genome_object_id)),
                        )
                        taxonomy = entry.get("taxonomy")
                        if isinstance(taxonomy, Mapping) and not known.taxonomy:
                            known.taxonomy = dict(taxonomy)
                        if known.created_at is None and _arc_is_iso_timestamp(
                            entry.get("done_ts")
                        ):
                            known.note_created_at(str(entry["done_ts"]))

    def _load_objects(self, object_ids: Sequence[str]) -> None:
        """Download each needed genome object ONCE and index its features
        EXACTLY.

        This is where the fast path stops being trusted. Each downloaded
        object's own ``features[].id`` values are indexed verbatim, and:

        - a feature id claimed by TWO objects is
          :class:`ArcFeatureIdConflictError` -- reported and REFUSED, never
          resolved last-write-wins, because the id is the join key from an
          annotation to its protein and guessing would attribute annotations
          to the wrong organism's sequence;
        - a ``fasta_object_id[:16]`` fast-path entry that does not match the
          object's real feature ids is DISCARDED (counted as
          :data:`ARC_SKIP_FASTA_PREFIX_MISMATCH`), so lookups fall through to
          the exact index rather than following a key that happens to be
          wrong for this genome.
        """
        index = self._build_index()
        for object_id in object_ids:
            genome = index.get(object_id)
            if genome is None or genome.loaded:
                continue
            payload = self._fetch_object(object_id)
            if payload is None:
                continue
            contigs = payload.get("contigs")
            features = payload.get("features")
            genome.contigs = [c for c in contigs if isinstance(c, Mapping)] if (
                isinstance(contigs, list)
            ) else []
            genome.features = {}
            if isinstance(features, list):
                for feature in features:
                    if not isinstance(feature, Mapping):
                        continue
                    feature_id = feature.get("id")
                    if not feature_id:
                        continue
                    feature_id = str(feature_id)
                    owner = self._feature_owner.get(feature_id)
                    if owner is not None and owner != object_id:
                        self._skip(ARC_SKIP_FEATURE_ID_CONFLICT)
                        raise ArcFeatureIdConflictError(
                            feature_id, owner, object_id
                        )
                    self._feature_owner[feature_id] = object_id
                    genome.features[feature_id] = dict(feature)
            taxonomy = payload.get("taxonomy")
            if isinstance(taxonomy, Mapping) and not genome.taxonomy:
                genome.taxonomy = dict(taxonomy)
            genome.loaded = True
            self._check_fast_path(genome)

    def _check_fast_path(self, genome: _ArcGenome) -> None:
        """Verify ``fasta_object_id[:16]`` against the object's feature ids."""
        if not genome.fasta_object_id:
            return
        fast_key = genome.fasta_object_id[:16]
        if self._fast_path.get(fast_key) != genome.genome_object_id:
            return
        if not genome.features:
            return
        if any(
            feature_id.rsplit("_", 1)[0] == fast_key
            for feature_id in genome.features
        ):
            return
        del self._fast_path[fast_key]
        self._skip(ARC_SKIP_FASTA_PREFIX_MISMATCH)

    def _owner_of_feature(self, feature_id: str) -> str | None:
        """The genome object id owning ``feature_id``: EXACT index first."""
        owner = self._feature_owner.get(feature_id)
        if owner is not None:
            return owner
        return self._fast_path.get(feature_id.rsplit("_", 1)[0])

    # -- iteration ----------------------------------------------------------

    def iter_records(self) -> Iterator[Mapping[str, Any]]:
        """Yield this source's records for its ``emit`` shape.

        The order of operations is the contract:

        1. FREEZE THE SELECTION (before anything is read), so every source and
           every resume emits from the same ordered record list.
        2. BUILD THE INDEX from the whole store, ignoring ``since``, so joins
           reach genomes outside the window.
        3. DOWNLOAD only the objects the selection actually needs, once each.
        4. EMIT, counting every unusable item by reason.
        5. WRITE A CHECKPOINT CANDIDATE -- only if the read was uncapped,
           reached exhaustion, and left no retryable failure. It is a
           candidate and not a watermark: whether the rows landed is not
           something this adapter can know (see
           :func:`promote_arc_checkpoints`).
        """
        self._exhausted = False
        emit = self._emit()
        frozen = self._freeze_selection()
        selected_ids = list(frozen.get(ARC_STATE_SELECTION) or [])
        tarballs = dict(frozen.get(ARC_STATE_TARBALLS) or {})
        if self._capped and tarballs:
            # Said BEFORE any work, because it is counter-intuitive: a
            # tarball is selected by digest, not by record, so an arc
            # --limit does not reduce it. An operator sampling with --limit 1
            # would otherwise be surprised by a full tarball's annotations.
            self.log_note(
                f"source {self.source.name!r}: arc --limit caps the SQLite "
                f"record selection only; {len(tarballs)} not-yet-completed "
                "tarball(s) are selected by digest and will be processed in "
                "full."
            )

        self._build_index()
        build_records, annotation_records = self._partition(selected_ids)
        needed = self._needed_objects(
            emit, build_records, annotation_records, tarballs
        )
        self._load_objects(sorted(needed))

        if emit == "genome":
            rows = self._emit_genomes(needed)
        elif emit in ("protein", "gene"):
            rows = self._emit_features(emit, needed)
        else:
            rows = self._emit_annotations(annotation_records, tarballs)

        for row in rows:
            yield row

        self._record_outcome(frozen, tarballs)
        # Reached only by running off the end of the selection. A capped
        # selection is NOT exhaustion: the records beyond the cap were never
        # offered, so the unit is `partial`, not `done`.
        self._exhausted = not self._capped

    def log_note(self, message: str) -> None:
        """Report an operator-facing note. Overridden by nothing; kept small.

        The adapter has no logger of its own (``SourceAdapter`` is driver- and
        transport-free by design), so this is the one seam a caller can
        patch to capture the note in a test, and it prints otherwise.
        """
        print(message)

    def _partition(
        self, selected_ids: Sequence[Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Fetch the selected records and split them by kind.

        Read back BY RECORD ID from the frozen selection, in the selection's
        own order, so the emission order is the frozen order and not whatever
        a fresh ``ORDER BY`` would produce today.
        """
        if not selected_ids:
            return ([], [])
        with self._connect() as connection:
            rows: dict[Any, dict[str, Any]] = {}
            # Chunked so a large selection cannot exceed SQLite's variable
            # limit (999 by default).
            for start in range(0, len(selected_ids), 500):
                chunk = list(selected_ids[start : start + 500])
                placeholders = ", ".join("?" for _ in chunk)
                for row in connection.execute(
                    f"SELECT record_id, kind, run_uid, artifacts, created_at, "
                    f"updated_at FROM {ARC_RUNS_TABLE} WHERE record_id IN "
                    f"({placeholders})",
                    chunk,
                ).fetchall():
                    rows[row["record_id"]] = dict(row)
        builds: list[dict[str, Any]] = []
        annotations: list[dict[str, Any]] = []
        for record_id in selected_ids:
            record = rows.get(record_id)
            if record is None:
                continue
            self._note_watermark(record.get("updated_at"))
            if record["kind"] == ARC_BUILD_KIND:
                builds.append(record)
            elif record["kind"] == ARC_ANNOTATION_KIND:
                annotations.append(record)
        return (builds, annotations)

    def _needed_objects(
        self,
        emit: str,
        build_records: Sequence[Mapping[str, Any]],
        annotation_records: Sequence[Mapping[str, Any]],
        tarballs: Mapping[str, str],
    ) -> set[str]:
        """Which genome objects this read needs -- and no more.

        The selection's build records contribute their own genomes; the
        selection's annotation records contribute, through the fast path, the
        genomes their feature ids belong to; the selected tarballs contribute
        their ledgers' genomes. Downloading the whole index instead would
        fetch 235 genome objects to emit one record's rows.
        """
        needed: set[str] = set()
        index = self._build_index()
        for record in build_records:
            payload = self._read_result(record)
            if not isinstance(payload, Mapping):
                continue
            genomes = payload.get("genomes")
            if not isinstance(genomes, Mapping):
                continue
            for entry in genomes.values():
                if isinstance(entry, Mapping) and entry.get("genome_object_id"):
                    needed.add(str(entry["genome_object_id"]))
        for record in annotation_records:
            payload = self._read_result(record)
            if not isinstance(payload, Mapping):
                continue
            for feature_id in payload:
                owner = self._fast_path.get(str(feature_id).rsplit("_", 1)[0])
                if owner is not None:
                    needed.add(owner)
        for path in tarballs.values():
            for genome_object_id in self._ledger_genomes.get(path, {}).values():
                needed.add(genome_object_id)
        return {object_id for object_id in needed if object_id in index}

    # -- the four emit shapes ------------------------------------------------

    def _emit_genomes(self, needed: set[str]) -> Iterator[Mapping[str, Any]]:
        """One record per DISTINCT genome object.

        ``created_at`` is the EARLIEST build record timestamp for the genome
        (see :meth:`_ArcGenome.note_created_at`), so a rebuilt genome does
        not appear to have come into existence on the day it was rebuilt;
        for a ledger-only genome it is the ledger's ``done_ts``.

        A genome with no real ``fasta_object_id`` is SKIPPED, not emitted
        with an invented pointer: ``genome_content.fasta_reference`` is a
        reference to a KBDL object, and a fabricated one is
        indistinguishable in the lake from a real one.
        """
        index = self._build_index()
        for object_id in sorted(needed):
            genome = index[object_id]
            if not genome.loaded:
                # object-unavailable, counted ONCE PER OBJECT by
                # _note_retryable. It cannot be counted per lost ROW: how
                # many genome/protein/gene rows the object would have
                # produced is a property of bytes that could not be fetched.
                # The retry list names the object, which is what a resume
                # needs.
                continue
            if not genome.fasta_object_id:
                self._skip(ARC_SKIP_FASTA_POINTER_MISSING)
                continue
            sequences = [
                str(contig.get("sequence"))
                for contig in genome.contigs
                if contig.get("sequence") is not None
            ]
            yield {
                "genome_object_id": object_id,
                "contig_sequences": sequences,
                "fasta_object_id": genome.fasta_object_id,
                "ncbi_taxid": genome.ncbi_taxid,
                "n_contigs": len(sequences),
                "total_length": sum(len(sequence) for sequence in sequences),
                "created_at": genome.created_at or self._run_start,
            }

    def _emit_features(
        self, emit: str, needed: set[str]
    ) -> Iterator[Mapping[str, Any]]:
        """One record per FEATURE of each needed genome object.

        ``protein`` carries only ``protein_translation``; ``gene`` carries
        ``dna_sequence`` as well as the translation, because
        ``gene_content.protein_entity_hash`` is derived from the protein
        sequence and the gene row has to carry it to compute the link.

        A feature missing the sequence its emit needs is skipped as
        :data:`ARC_SKIP_FEATURE_NOT_IN_GENOME`: ``dna_sequence`` and
        ``protein_translation`` are REQUIRED on every ``CanonicalGenome``
        feature, so an absent one is a malformed object, and a row hashed
        from ``None`` would be a protein that is not a protein.
        """
        index = self._build_index()
        wanted = "dna_sequence" if emit == "gene" else "protein_translation"
        for object_id in sorted(needed):
            genome = index[object_id]
            if not genome.loaded:
                continue
            created_at = genome.created_at or self._run_start
            for feature_id in sorted(genome.features):
                feature = genome.features[feature_id]
                translation = feature.get("protein_translation")
                if feature.get(wanted) is None or translation is None:
                    self._skip(ARC_SKIP_FEATURE_NOT_IN_GENOME)
                    continue
                row: dict[str, Any] = {
                    "feature_id": feature_id,
                    "genome_object_id": object_id,
                    "protein_translation": str(translation),
                    "created_at": created_at,
                }
                if emit == "gene":
                    row["dna_sequence"] = str(feature["dna_sequence"])
                yield row

    def _emit_annotations(
        self,
        annotation_records: Sequence[Mapping[str, Any]],
        tarballs: Mapping[str, str],
    ) -> Iterator[Mapping[str, Any]]:
        """One record per ``(feature, tool)`` pair with a NON-EMPTY ontology
        map.

        Both annotation shapes are the same three-level mapping -- ``{feature:
        {TOOL: {ONTOLOGY: [term, ...]}}}`` -- so one emitter serves the
        gzipped job results and the tarball members. A feature whose map is
        ``{}`` is UNANNOTATED and produces nothing (not a skip: there is
        nothing there to lose), and a tool whose ontology map is empty is
        likewise passed over.
        """
        for record in annotation_records:
            payload = self._read_result(record)
            if not isinstance(payload, Mapping):
                continue
            yield from self._annotation_rows(
                payload,
                kbdl_job_id=str(record.get("run_uid") or ""),
                created_at=str(record.get("created_at") or self._run_start),
            )
        for digest, path in sorted(tarballs.items()):
            yield from self._tarball_annotation_rows(Path(path))

    def _tarball_annotation_rows(
        self, path: Path
    ) -> Iterator[Mapping[str, Any]]:
        """Stream one tarball's annotation members, matched to its ledgers.

        ONLY the ledger's own keys are opened, and only under the two
        directories :data:`ARC_TARBALL_LEDGERS` names
        (``data/annotations/<key>.json`` and
        ``data/annotations_pmi/<key>.json``), resolved with
        ``tarfile.getmember`` -- so no member path from the archive is ever
        joined to a filesystem path and nothing is extracted. A tarball is
        untrusted input and this is the whole of the trust placed in it.

        ``created_at`` is the ledger's ``done_ts`` when that is a usable
        timestamp, and the RUN START otherwise -- counted as
        :data:`ARC_SKIP_TIMESTAMP_FALLBACK`, so a fallback is visible in the
        inventory instead of looking like the archive's own time.
        """
        key = str(path)
        with tarfile.open(path, "r:gz") as archive:
            for ledger_member, annotation_dir in ARC_TARBALL_LEDGERS:
                ledger = _arc_read_member(archive, ledger_member)
                if not isinstance(ledger, Mapping):
                    continue
                for ledger_key in sorted(ledger):
                    entry = ledger.get(ledger_key)
                    if not isinstance(entry, Mapping):
                        continue
                    payload = _arc_read_member(
                        archive, f"{annotation_dir}/{ledger_key}.json"
                    )
                    if not isinstance(payload, Mapping):
                        continue
                    done_ts = entry.get("done_ts")
                    if _arc_is_iso_timestamp(done_ts):
                        created_at = str(done_ts)
                    else:
                        created_at = self._run_start
                        self._skip(ARC_SKIP_TIMESTAMP_FALLBACK)
                    yield from self._annotation_rows(
                        payload,
                        kbdl_job_id=str(entry.get("ann_job_id") or ""),
                        created_at=created_at,
                        genome_object_id=self._ledger_genomes.get(key, {}).get(
                            str(ledger_key)
                        ),
                    )

    def _annotation_rows(
        self,
        payload: Mapping[str, Any],
        *,
        kbdl_job_id: str,
        created_at: str,
        genome_object_id: str | None = None,
    ) -> Iterator[Mapping[str, Any]]:
        """Turn one ``{feature: {TOOL: {ONTOLOGY: [term]}}}`` map into rows.

        One row per ``(feature, tool)`` with a non-empty ontology map. Each
        row carries the protein sequence its annotation belongs to, looked up
        through the EXACT feature index built from the downloaded objects
        (``genome_object_id``, when the caller knows it from a ledger, is the
        fallback for a feature whose prefix does not resolve).

        TRANSYT IS THE ONE TOOL WITH A PARAMETER SET. A TRANSYT result is
        computed against one NCBI taxonomy, so the taxonomy id is not a
        tuning knob but part of what the result MEANS -- there is no such
        thing as a default TRANSYT run. This emitter therefore:

        - skips a TRANSYT row whose genome has no ``ncbi_taxid`` at all,
          counted as :data:`ARC_SKIP_TRANSYT_WITHOUT_TAXONOMY`;
        - builds ``{"taxonomy_id": "<decimal string>"}`` otherwise and passes
          it to lake-5's SHARED TRANSYT validator
          (:func:`clearinghouse_parameters.validate_transyt_parameter_set`) --
          called, never restated -- so a taxonomy id that is not a decimal
          id with no leading zeros is skipped and counted as
          :data:`ARC_SKIP_TRANSYT_INVALID` rather than loaded;
        - yields ``{}`` for every other tool, the default run.

        The row also carries a ready ``source_label`` (``"<tool>/<version>"``,
        lowercased tool, version from the preset's
        ``[backfill.tool_versions]``), because ``source`` is the permanent
        partition column on ``protein_result`` and resolving the version in
        one place is what stops two of the four sources spelling one tool
        two ways.
        """
        index = self._build_index()
        for feature_id, tools in payload.items():
            if not isinstance(tools, Mapping) or not tools:
                continue
            feature_id = str(feature_id)
            owner = self._owner_of_feature(feature_id) or genome_object_id
            if owner is None:
                self._skip(ARC_SKIP_NO_GENOME_FOR_PREFIX, len(tools))
                continue
            genome = index.get(owner)
            if genome is None or not genome.loaded:
                self._skip(ARC_SKIP_OBJECT_UNAVAILABLE, len(tools))
                continue
            feature = genome.features.get(feature_id)
            translation = feature.get("protein_translation") if feature else None
            if translation is None:
                self._skip(ARC_SKIP_FEATURE_NOT_IN_GENOME, len(tools))
                continue
            taxid = genome.ncbi_taxid
            for tool, ontologies in tools.items():
                if not isinstance(ontologies, Mapping) or not ontologies:
                    continue
                tool = str(tool)
                source_label = (
                    f"{tool.lower()}/{self.tool_version(tool)}"
                )
                is_transyt = (
                    cp.tool_of_source(source_label) in cp.TRANSYT_TOOLS
                )
                if is_transyt and taxid is None:
                    self._skip(ARC_SKIP_TRANSYT_WITHOUT_TAXONOMY)
                    continue
                parameter_set: dict[str, Any] = (
                    {cp.TRANSYT_TAXONOMY_ID_KEY: taxid} if is_transyt else {}
                )
                if is_transyt:
                    try:
                        cp.validate_transyt_parameter_set(
                            parameter_set,
                            source=source_label,
                            where=f"arc adapter feature {feature_id!r}",
                        )
                    except ParameterSetError:
                        self._skip(ARC_SKIP_TRANSYT_INVALID)
                        continue
                yield {
                    "feature_id": feature_id,
                    "genome_object_id": owner,
                    "protein_translation": str(translation),
                    "tool": tool,
                    "source_label": source_label,
                    "terms": {
                        str(ontology): terms
                        for ontology, terms in ontologies.items()
                    },
                    "kbdl_job_id": kbdl_job_id,
                    "ncbi_taxid": taxid,
                    "parameter_set": parameter_set,
                    "created_at": created_at,
                }

    # -- the watermark checkpoint -------------------------------------------

    def _record_outcome(
        self, frozen: Mapping[str, Any], tarballs: Mapping[str, str]
    ) -> None:
        """Persist what this read did, and a checkpoint CANDIDATE if earned.

        THREE CONDITIONS, all required, before a candidate is written at all:

        1. UNCAPPED. A ``--limit`` run read a sample, so its ``upper_bound``
           is a time it did NOT cover.
        2. EXHAUSTED -- the selection was walked to the end.
        3. NO RETRYABLE FAILURE. An object the run could not fetch means
           records are missing from the window the watermark would claim; a
           later run starting from it would never come back for them.

        And even then it is a CANDIDATE. Whether the rows reached the lake is
        not knowable here, so the promotion to a real watermark belongs to
        the driver, after verification (:func:`promote_arc_checkpoints`).
        """
        state = dict(frozen)
        state[ARC_STATE_RETRY] = list(self._retry)
        state[ARC_STATE_INCOMPLETE] = bool(self._retry)
        self._write_state(state)

        if self._capped or self._retry:
            return
        key = self._checkpoint_key()
        previous = read_arc_checkpoint(key)
        completed = sorted(
            {*(previous.get("tarballs") or []), *tarballs.keys()}
        )
        directory = arc_checkpoint_dir()
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            ARC_STATE_UPPER_BOUND: frozen.get(ARC_STATE_UPPER_BOUND),
            "tarballs": completed,
            "written_at": _iso_z(utc_now()),
            # The key's components, stored so a checkpoint is diagnosable
            # without reversing the hash that names it.
            "key": {
                "tenant": str(getattr(self.run_context, "tenant", "") or ""),
                "dataset": str(getattr(self.run_context, "dataset", "") or ""),
                "db_path": str(self._db_path().resolve()),
                "base_url": self._base_url(),
                "principal": self._principal(),
            },
            "source": self.source.name,
        }
        candidate = arc_checkpoint_path(key, candidate=True)
        tmp = candidate.with_name(f".{candidate.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, candidate)
        # Reported to the driver through `run.json`, so the promotion after
        # verification names THIS key rather than sweeping the shared
        # checkpoint directory. See :meth:`checkpoint_candidates`.
        if key not in self._candidates:
            self._candidates.append(key)


def _arc_file_sha256(path: Path) -> str:
    """The sha256 of a file, read in bounded chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _arc_read_member(archive: Any, member_name: str) -> Any | None:
    """Read and JSON-parse ONE named member of an open tarball, or ``None``.

    ``getmember`` by exact name and ``extractfile`` to a stream: NOTHING IS
    EXTRACTED TO DISK, and no member path from the archive is ever joined to
    a filesystem path. That is the point -- a tarball is untrusted input, and
    a member named ``../../etc/x`` or an absolute path is how extraction
    becomes an arbitrary file write. A member that is absent, is not a
    regular file, or does not parse reads as ``None``.
    """
    try:
        member = archive.getmember(member_name)
    except KeyError:
        return None
    if not member.isfile():
        return None
    handle = archive.extractfile(member)
    if handle is None:  # pragma: no cover - isfile() already excludes this
        return None
    with handle:
        try:
            return json.loads(handle.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None


# --------------------------------------------------------------------------
# The `kbdl_hash` adapter: KBDL's own KBDLHashGenomes job, one job per archive
# --------------------------------------------------------------------------

#: Default ``max_in_flight``: ONE. The adapter runs one archive per unit and
#: the driver runs units sequentially, so one job is outstanding at a time by
#: construction; the key exists to BOUND that rather than to raise it, and the
#: default is the bound the lifecycle actually implements. Round 1 of the
#: confront set it to 1 for that reason -- a default of 4 would have described
#: a concurrency the driver does not offer.
KBDL_HASH_DEFAULT_MAX_IN_FLIGHT = 1

#: Seconds between polls of a submitted hash job, and the wall-clock budget a
#: single job's polling is given. The budget exists because a job CANCELLED
#: while we are polling never reaches one of the client's own terminal states
#: (:data:`~kbutillib.domains.external.kbdl_service_utils._TERMINAL_STATES` is
#: ``completed``/``failed``), so an unbounded poll would wait on it forever.
KBDL_HASH_POLL_INTERVAL = 30.0
KBDL_HASH_POLL_TIMEOUT = 86400.0

#: The job states this adapter treats as terminal. A SUPERSET of the client's
#: own: ``cancelled`` is terminal here because an operator cancelling a job is
#: a terminal cause the run must stop on, not a state to keep polling.
KBDL_HASH_TERMINAL_STATES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled"}
)

#: Skip-reason prefixes and slugs this adapter reports.
#:
#: ``hash-error:<code>`` is one entry of the job output's ``errors`` map.
#: ``hash-job-failed:<cause>`` is the whole job failing. The
#: standardizer-version slug is a per-entry mismatch between the version KBDL
#: hashed with and the version THIS pod would hash with -- two digests from
#: two rules are two different identities, and loading one under the other's
#: name is the one error the whole clearinghouse exists to prevent.
KBDL_HASH_SKIP_ERROR_PREFIX = "hash-error:"
KBDL_HASH_SKIP_JOB_FAILED_PREFIX = "hash-job-failed:"
KBDL_HASH_SKIP_VERSION_MISMATCH = "standardizer-version-mismatch"
KBDL_HASH_SKIP_ENTRY_UNUSABLE = "hash-entry-unusable"

#: Member-error codes that are TERMINAL -- a reported exclusion, not a retry.
#: ``invalid_input`` means the archive member is not something the hasher can
#: read, and it will not be next time either (see
#: ``src/kbdl_service/schemas/hash_genomes.py`` in KBDLJobRunningPrototype).
#: Every OTHER code, ``internal_error`` included, is treated as retryable:
#: ``job_types/hash_genomes.py`` returns ``internal_error`` for a stream read
#: failure, which a later attempt may well not hit, and an unrecognised code
#: is not provably terminal so it is not assumed to be.
KBDL_HASH_TERMINAL_ERROR_CODES: frozenset[str] = frozenset({"invalid_input"})

#: Keys of the adapter's durable per-(source, unit) state. ``attempts`` is the
#: ordered list of hash-job attempts for this archive -- each one
#: ``{job_id, submitted_at, reason}`` -- and the LAST entry is the ACCEPTED
#: attempt. It is a list and not a single id so that a retry is a RECORDED NEW
#: ATTEMPT rather than an overwrite: "this archive was hashed twice and the
#: second result is the one loaded" has to stay readable after the fact.
KBDL_HASH_STATE_ARCHIVE = "archive"
KBDL_HASH_STATE_ATTEMPTS = "attempts"
KBDL_HASH_STATE_RETENTION_RESUBMITS = "retention_resubmits"
KBDL_HASH_STATE_RETRY = "retry"
KBDL_HASH_STATE_INCOMPLETE = "incomplete"
KBDL_HASH_STATE_EMITTED = "emitted"
KBDL_HASH_STATE_PROBE = "standardizer_version_probe"

#: ``reason`` values recorded on an attempt, naming WHY it was submitted.
KBDL_HASH_ATTEMPT_INITIAL = "initial"
KBDL_HASH_ATTEMPT_RETENTION_LOSS = "retention-loss"
KBDL_HASH_ATTEMPT_OPERATOR_RETRY = "retry-failed-archives"


class KbdlHashVersionMismatchError(ManifestSourceError):
    """KBDL hashes with a DIFFERENT standardizer version than this pod.

    The run is REFUSED. ``entity_hash`` is the join key of the entire
    clearinghouse, and two standardizer versions are two hash RULES: rows
    loaded under one rule and rows loaded under the other look like different
    entities where they are the same one, and like the same entity where a
    rule changed. Nothing downstream can detect or repair that, so the only
    safe response is to not load any of it.
    """


class KbdlHashJobFailedError(ManifestSourceError):
    """A ``KBDLHashGenomes`` job reached ``failed`` or ``cancelled``.

    The command STOPS with the terminal cause (round 1 of the confront). A
    failed job is not a shortage of rows that a skip-and-continue would
    describe honestly -- it is an archive whose contents are unknown, and
    every later unit would report the same way while the operator read a
    success. ``--retry-failed-archives`` is the explicit instruction that
    turns it into a recorded new attempt instead.
    """


class KbdlHashArchiveUnavailableError(ManifestSourceError):
    """A frozen archive is gone, or is no longer visible to this caller.

    Round 2 of the confront: the unit cannot complete and the run stops. The
    archive list was frozen when the run was created precisely so the unit set
    could not drift; an archive that has since disappeared is a change to the
    corpus the run claims to cover, and continuing would load a subset while
    reporting the whole.
    """


class KbdlHashRetentionLostError(ManifestSourceError):
    """A hash job's result was reclaimed twice. The run stops.

    KBDL deletes a job's result 30 days after completion. ONE automatic
    resubmission is the right response to that; a second loss on a freshly
    submitted job is not a retention problem and resubmitting again would be
    an unbounded loop against a service that is telling us something else.
    """


class KbdlHashSourceAdapter(SourceAdapter):
    """The ``kbdl_hash`` adapter: every genome KBDL already holds, hashed in place.

    NO SEQUENCE CROSSES THE NETWORK. KBDL's own ``KBDLHashGenomes`` job reads
    each ``GenomeArchive``'s FASTA members on poplar, read-only, and hashes
    them with the SAME ``kbutillib`` standardizer this pod would use; only the
    digests and their counts come back. That is the whole point of routing a
    genome backfill through a job rather than a download: the archives are
    large, the pod is small, and a hash computed beside the bytes is the same
    hash computed over them.

    ONE ARCHIVE IS ONE UNIT. :meth:`units` reports one label per archive --
    the archive's object id -- so the driver shards, ingests, verifies and
    records one archive before starting the next, and a run killed anywhere
    loses at most the archive in flight.

    Locator keys:

    - ``archives`` -- an explicit list of ``GenomeArchive`` object ids (or one
      comma-separated string, which is what a ``${VAR}`` placeholder expands
      to). Mutually exclusive with ``all_visible``.
    - ``all_visible`` -- ``true`` to hash EVERY object
      :meth:`~kbutillib.domains.external.kbdl_service_utils.KBDLServiceUtils.list_objects`
      reports whose type is
      :data:`~kbutillib.domains.external.kbdl_service_utils.OBJECT_TYPE_GENOME_ARCHIVE`.
    - ``max_in_flight`` -- bound on outstanding hash jobs; defaults to
      :data:`KBDL_HASH_DEFAULT_MAX_IN_FLIGHT`.
    - ``base_url`` -- optional KBDL endpoint; the client's own default
      otherwise.
    - ``poll_interval`` / ``poll_timeout`` -- optional polling tuning.

    THE ARCHIVE LIST IS FROZEN, NOT REDISCOVERED. ``units()`` resolves
    ``all_visible`` once, sorts the ids LEXICOGRAPHICALLY and returns them in
    that order; :func:`clearinghouse_backfill.freeze_adapter_units` persists
    that list in the run's own state file before any job is submitted, and
    every later invocation of the run reads it from there. KBDL's
    ``ObjectStore.list`` orders by ``added_at`` with no tie-break, so live
    rediscovery is not a stable order -- a resume that re-listed would
    renumber a ``--limit`` window and silently cover a different corpus.

    THE JOB ID IS PERSISTED BEFORE THE FIRST POLL, through the run context
    (:meth:`SourceAdapter.bind_run_context`). A hash job over a large archive
    runs for hours; an id held only in memory would be lost to the first pod
    restart and the resume would submit a SECOND job for the same archive.
    There remains a gap this adapter does not try to close: a crash in the
    window between ``POST /jobs`` returning and the state file being replaced
    leaves a running job with no recorded id, and the resume submits another.
    Closing it needs a server-side idempotency key (submit-with-token), which
    KBDL does not offer; the cost of the gap is one wasted job and a second
    identical result, not a wrong row, because the rows are keyed by
    ``entity_hash`` and the ingest is fill-only.

    TWO KINDS, ONE JOB. A ``kinds = ["entity", "content"]`` source resolves to
    two plans and therefore two adapter INSTANCES, each with its own
    ``iter_records``. They do not submit two jobs: the first persists its job
    id in the run context's per-(source, unit) state and the second reads the
    recorded attempt. Without a run context -- a plain ``kbu clearinghouse
    shard`` -- there is nowhere to record it and each instance submits its own
    job, which is one reason this source is driven through ``kbu
    clearinghouse backfill run``.

    AN ARCHIVE IS NOT DONE WHEN ITS JOB SUCCEEDS. It is done when its rows are
    ingested and VERIFIED and the driver records the unit, which is a judgement
    only :func:`clearinghouse_backfill.unit_outcome` can make. This adapter
    records what it submitted, read and emitted; it never records completion.

    Args:
        source: The parsed ``[[source]]`` block.
        client: An object exposing ``list_objects``, ``get_object_metadata``,
            ``submit_hash_genomes``, ``check_job``, ``get_job_result`` and
            ``poll_until_terminal`` -- a real
            :class:`~kbutillib.domains.external.kbdl_service_utils.KBDLServiceUtils`
            in production, a fake in tests. Constructed lazily on first use
            when not given, so building the adapter makes no network call.
    """

    adapter_name = "kbdl_hash"

    def __init__(self, source: cm.Source, *, client: Any = None):
        super().__init__(source)
        self._client = client
        #: Retryable failures, as human strings. Non-empty => incomplete.
        self._retry: list[str] = []
        #: This read's start time, ISO-8601 ``Z``; the ``observed_at`` fallback
        #: for a job status record carrying no usable completion time.
        self._run_start = _iso_z(utc_now())
        #: Memoised :meth:`units` -- resolving ``all_visible`` twice in one
        #: process would be two ``GET /objects`` calls for one answer.
        self._unit_labels: tuple[str, ...] | None = None

    # -- locator ------------------------------------------------------------

    def _locator(self, key: str, default: Any = None) -> Any:
        return self.source.locator.get(key, default)

    def _base_url(self) -> str:
        return str(self._locator("base_url") or "")

    def _archives(self) -> list[str] | None:
        """The explicit ``archives`` list, or ``None`` when not declared.

        Accepts a TOML list or one comma-separated string, because a
        ``${KBU_KBDL_ARCHIVES}`` placeholder can only ever expand to a string.
        """
        raw = self._locator("archives")
        if raw is None or raw == "":
            return None
        if isinstance(raw, str):
            values = [part.strip() for part in raw.split(",") if part.strip()]
        elif isinstance(raw, (list, tuple)):
            values = [str(part).strip() for part in raw if str(part).strip()]
        else:
            raise ManifestSourceError(
                f"source {self.source.name!r}: kbdl_hash 'archives' must be a "
                "list of object ids or one comma-separated string, got "
                f"{type(raw).__name__}."
            )
        if not values:
            return None
        return values

    def _all_visible(self) -> bool:
        value = self._locator("all_visible", False)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        raise ManifestSourceError(
            f"source {self.source.name!r}: kbdl_hash 'all_visible' must be a "
            f"boolean, got {type(value).__name__}."
        )

    def _max_in_flight(self) -> int:
        raw = self._locator("max_in_flight", KBDL_HASH_DEFAULT_MAX_IN_FLIGHT)
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ManifestSourceError(
                f"source {self.source.name!r}: kbdl_hash 'max_in_flight' must "
                f"be a positive integer, got {raw!r}."
            ) from exc
        if value < 1:
            raise ManifestSourceError(
                f"source {self.source.name!r}: kbdl_hash 'max_in_flight' must "
                f"be at least 1, got {value}. Zero outstanding jobs is not a "
                "slower backfill, it is no backfill."
            )
        return value

    def _poll_interval(self) -> float:
        return float(self._locator("poll_interval", KBDL_HASH_POLL_INTERVAL))

    def _poll_timeout(self) -> float:
        return float(self._locator("poll_timeout", KBDL_HASH_POLL_TIMEOUT))

    # -- the KBDL client ----------------------------------------------------

    def _get_client(self) -> Any:
        """The KBDL client, deferring the import until first use."""
        if self._client is None:
            from ...external.kbdl_service_utils import (  # noqa: PLC0415
                KBDLServiceUtils,
            )

            base_url = self._base_url()
            self._client = (
                KBDLServiceUtils(base_url=base_url)
                if base_url
                else KBDLServiceUtils()
            )
        return self._client

    @staticmethod
    def _object_type(record: Mapping[str, Any]) -> str:
        """An object record's type, under either of the service's two spellings.

        ``upload_object`` posts the field as ``object_type`` and that is what
        ``GET /objects`` returns; ``type`` is accepted as well so a listing
        shape that spells it the shorter way is not silently read as "no
        type" -- which, in a filter, is indistinguishable from "no archives".
        """
        for key in ("object_type", "type"):
            value = record.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    def _visible_archives(self) -> list[str]:
        """Every visible object whose type is ``GenomeArchive``, by object id."""
        from ...external.kbdl_service_utils import (  # noqa: PLC0415
            OBJECT_TYPE_GENOME_ARCHIVE,
        )

        records = self._get_client().list_objects()
        out: list[str] = []
        for record in records or []:
            if not isinstance(record, Mapping):
                continue
            if self._object_type(record) != OBJECT_TYPE_GENOME_ARCHIVE:
                continue
            object_id = record.get("object_id") or record.get("id")
            if isinstance(object_id, str) and object_id:
                out.append(object_id)
        return out

    def _require_visible(self, archive: str) -> None:
        """Refuse to submit for an archive that is gone or no longer visible.

        Checked BEFORE every submission and not before a READ: a recorded
        finished job's result stands on its own, and re-checking the archive
        it came from would make a resume fail on an archive whose work is
        already done.
        """
        from ...external.kbdl_service_utils import (  # noqa: PLC0415
            OBJECT_TYPE_GENOME_ARCHIVE,
        )

        client = self._get_client()
        try:
            metadata = client.get_object_metadata(archive)
        except Exception as exc:
            reason = (
                f"archive {archive!r} is no longer readable from KBDL "
                f"({type(exc).__name__}: {exc})"
            )
            self._note_retryable(reason)
            raise KbdlHashArchiveUnavailableError(
                f"source {self.source.name!r}: {reason}. The archive list was "
                "FROZEN when this run was created, so an archive that has "
                "since vanished or lost visibility leaves this unit "
                "incomplete and stops the run rather than loading a subset "
                "of a corpus the run claims to cover in full."
            ) from exc
        if not isinstance(metadata, Mapping):  # pragma: no cover - defensive
            metadata = {}
        object_type = self._object_type(metadata)
        if object_type != OBJECT_TYPE_GENOME_ARCHIVE:
            reason = (
                f"archive {archive!r} is a {object_type or 'typeless'} object, "
                f"not a {OBJECT_TYPE_GENOME_ARCHIVE}"
            )
            self._note_retryable(reason)
            raise KbdlHashArchiveUnavailableError(
                f"source {self.source.name!r}: {reason}. KBDLHashGenomes hashes "
                "the FASTA members of a genome archive; another object type has "
                "none, and submitting anyway would spend a job to be told so."
            )

    # -- accounting ---------------------------------------------------------

    def _skip(self, reason: str, count: int = 1) -> None:
        self._skipped[reason] = self._skipped.get(reason, 0) + count

    def _note_retryable(self, detail: str) -> None:
        """Record a retryable failure: it makes the UNIT incomplete.

        Deliberately does NOT also count a skip reason of its own -- the
        caller counts the reason that fits (``hash-error:internal_error``, a
        version mismatch, a job failure), and a second generic count would
        double-report one event.
        """
        if detail not in self._retry:
            self._retry.append(detail)

    def skipped(self) -> dict[str, int]:
        return dict(self._skipped)

    def incomplete_reasons(self) -> tuple[str, ...]:
        return tuple(self._retry)

    def estimate_bytes(self) -> int:
        """One archive's hash output, not one archive's sequence.

        The sharder uses this only to choose a shard COUNT, and what this
        adapter emits is a handful of digests and counts per genome -- never a
        sequence. A deliberately small, honest figure beats an estimate of the
        archive's size, which would split a few hundred rows across hundreds of
        shards.
        """
        return 1 << 16

    # -- units of work ------------------------------------------------------

    def units(self) -> tuple[str, ...]:
        """One label per archive: the archive's object id, LEXICOGRAPHICALLY sorted.

        Resolved ONCE per process and memoised. ``all_visible`` costs one
        ``GET /objects``; it reads NO RECORDS and submits NO JOB, so planning
        is cheap and off-pod planning of an explicit ``archives`` list needs no
        network at all.

        Sorted rather than left in listing order because KBDL's
        ``ObjectStore.list`` orders by ``added_at`` with no tie-break: two
        archives added in the same instant can come back in either order, so
        the listing order is not a stable basis for a ``--limit`` window or for
        a resume. Lexicographic order of the ids is.

        Raises:
            ManifestSourceError: Neither or both of ``archives``/
                ``all_visible`` were declared, or the resolved list is empty.
                An empty list is REFUSED rather than returned: a zero-unit run
                reports a clean success having loaded nothing, which is the one
                outcome indistinguishable from a working backfill of an empty
                corpus.
        """
        if self._unit_labels is not None:
            return self._unit_labels
        explicit = self._archives()
        all_visible = self._all_visible()
        if explicit is not None and all_visible:
            raise ManifestSourceError(
                f"source {self.source.name!r}: kbdl_hash takes EITHER an "
                "explicit 'archives' list OR 'all_visible = true', not both. "
                "They are two answers to one question (which archives does "
                "this run cover), and intersecting them would cover neither "
                "of the two sets the operator named."
            )
        if explicit is None and not all_visible:
            raise ManifestSourceError(
                f"source {self.source.name!r}: kbdl_hash requires either an "
                "'archives' list of GenomeArchive object ids or "
                "'all_visible = true'."
            )
        resolved = explicit if explicit is not None else self._visible_archives()
        labels = tuple(sorted(dict.fromkeys(resolved)))
        if not labels:
            raise ManifestSourceError(
                f"source {self.source.name!r}: kbdl_hash resolved NO archives "
                + (
                    "from its 'archives' list."
                    if explicit is not None
                    else
                    "-- no object visible to this caller has type "
                    "'GenomeArchive'. Check the bearer token: KBDL shows a "
                    "caller only the objects it may see, and an "
                    "under-privileged token is indistinguishable from an "
                    "empty store."
                )
                + " Refusing rather than planning zero units, because a "
                "zero-unit run reports a clean success having loaded nothing."
            )
        self._unit_labels = labels
        return labels

    def restrict_to_unit(self, unit: str | None) -> None:
        """Narrow this read to ONE archive, named by its object id.

        Accepts the label WITHOUT re-resolving :meth:`units` to validate it.
        That is deliberate: the label the driver hands back comes from the
        FROZEN list persisted when the run was created, and re-listing KBDL to
        check it would reintroduce exactly the live rediscovery the freeze
        exists to prevent -- an archive added since the freeze would be
        "valid" and one removed since would fail validation here instead of
        failing where it can be reported (see :meth:`_require_visible`).
        """
        if unit is None:
            self._unit = None
            return
        if not isinstance(unit, str) or not unit:
            raise ManifestSourceError(
                f"source {self.source.name!r}: kbdl_hash unit labels are "
                f"GenomeArchive object ids; got {unit!r}."
            )
        self._unit = unit

    def _archive(self) -> str:
        """This read's archive: the restricted unit, or a lone configured one."""
        if self._unit:
            return self._unit
        labels = self.units()
        if len(labels) == 1:
            return labels[0]
        raise ManifestSourceError(
            f"source {self.source.name!r}: kbdl_hash covers {len(labels)} "
            "archives and was not restricted to one of them. One archive is "
            "one UNIT -- a job per archive, hours long, resumable on its own "
            "-- so this source is read through `kbu clearinghouse backfill "
            "run`, which drives the units, rather than as one undivided "
            "shard."
        )

    # -- durable state ------------------------------------------------------

    def _read_state(self) -> dict[str, Any]:
        """This (source, unit)'s durable state; ``{}`` without a run context.

        Keyed by the SOURCE name, not the adapter name (which is how the arc
        adapter keys its shared frozen selection): this state is one archive's
        job attempts, and the run context already scopes the file per unit.
        """
        context = self.run_context
        if context is None:
            return {}
        getter = getattr(context, "get_state", None)
        if not callable(getter):
            return {}
        return getter(self.source.name)

    def _write_state(self, value: Mapping[str, Any]) -> None:
        context = self.run_context
        if context is None:
            return
        putter = getattr(context, "put_state", None)
        if not callable(putter):
            return
        putter(self.source.name, dict(value))

    def _retry_failed_archives(self) -> bool:
        """Whether ``--retry-failed-archives`` is in force for this run."""
        return bool(getattr(self.run_context, "retry_failed_archives", False))

    def _persist(self, state: dict[str, Any]) -> None:
        state[KBDL_HASH_STATE_RETRY] = list(self._retry)
        state[KBDL_HASH_STATE_INCOMPLETE] = bool(self._retry)
        self._write_state(state)

    # -- the job lifecycle --------------------------------------------------

    def _submit(self, archive: str, state: dict[str, Any], *, reason: str) -> str:
        """Submit one hash job and PERSIST ITS ID BEFORE RETURNING.

        The id is written through the run context before a single poll, which
        is the whole reason the run context has durable state: the job runs for
        hours on poplar, and an id that only ever existed in this process is an
        id a pod restart turns into a duplicate submission.
        """
        # Validated FIRST, and validated rather than consulted. ONE archive is
        # one unit and the driver runs units sequentially, so exactly one job
        # is outstanding when this returns -- the bound is satisfied by the
        # lifecycle's shape rather than by arithmetic here. Resolving it anyway
        # is what makes a malformed `max_in_flight` a refusal before anything
        # is spent on it, instead of a locator key nobody ever reads.
        self._max_in_flight()
        self._require_visible(archive)
        attempts = list(state.get(KBDL_HASH_STATE_ATTEMPTS) or [])
        job_id = self._get_client().submit_hash_genomes([archive])
        attempts.append(
            {
                "job_id": str(job_id),
                "archive": archive,
                "reason": reason,
                "submitted_at": _iso_z(utc_now()),
            }
        )
        state[KBDL_HASH_STATE_ATTEMPTS] = attempts
        self._persist(state)
        return str(job_id)

    def _status(self, job_id: str) -> Mapping[str, Any]:
        """A job's terminal status record, polling only if it is not yet terminal.

        Checked ONCE first so an already-``cancelled`` job is recognised
        immediately: ``cancelled`` is not in the client's own terminal set, so
        handing it straight to ``poll_until_terminal`` would wait out the whole
        timeout on a job that is never going to move.
        """
        client = self._get_client()
        status = client.check_job(job_id)
        if not isinstance(status, Mapping):  # pragma: no cover - defensive
            status = {}
        if str(status.get("state") or "") in KBDL_HASH_TERMINAL_STATES:
            return status
        polled = client.poll_until_terminal(
            job_id,
            interval=self._poll_interval(),
            timeout=self._poll_timeout(),
        )
        return polled if isinstance(polled, Mapping) else {}

    def _failure_cause(self, status: Mapping[str, Any]) -> str:
        """A short, stable cause slug for a failed or cancelled job."""
        state_name = str(status.get("state") or "unknown")
        failure = status.get("failure") or status.get("error")
        if isinstance(failure, Mapping):
            code = failure.get("code") or failure.get("type")
            if code:
                return f"{state_name}:{code}"
        return state_name

    def _resolve_result(
        self, archive: str, state: dict[str, Any]
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        """Get this archive's accepted hash-job ``(result, status)``.

        The lifecycle, in the order round 1 of the confront fixed it:

        1. NO RECORDED ATTEMPT -> confirm the archive is still visible, submit,
           persist the id, poll, read.
        2. A RECORDED ATTEMPT -> REATTACH. A ``queued``/``running`` job is
           polled; a ``completed`` one is read. Neither resubmits: a succeeded
           job is NEVER resubmitted automatically, because a second result for
           one input is not a repair.
        3. ``failed`` / ``cancelled`` -> STOP with the terminal cause, unless
           ``--retry-failed-archives``, which submits a recorded NEW attempt.
        4. A RECLAIMED RESULT (KBDL deletes results 30 days after completion,
           and a reclaimed one reads as not-found/conflict) is the ONLY case
           that resubmits automatically, and only ONCE. Transport and auth
           errors do not resubmit -- they say nothing about the job.
        """
        from ...external.kbdl_service_utils import (  # noqa: PLC0415
            KBDLConflictError,
            KBDLNotFoundError,
        )

        attempts = list(state.get(KBDL_HASH_STATE_ATTEMPTS) or [])
        if attempts:
            job_id = str(attempts[-1].get("job_id") or "")
        else:
            job_id = ""
        if not job_id:
            job_id = self._submit(
                archive, state, reason=KBDL_HASH_ATTEMPT_INITIAL
            )

        # At most three passes: the recorded attempt, one retention
        # resubmission, and one operator-instructed retry. The bound is
        # explicit so a service answering oddly cannot spin here.
        for _ in range(3):
            try:
                status = self._status(job_id)
            except KBDLNotFoundError as exc:
                job_id = self._resubmit_after_retention_loss(
                    archive, state, job_id, detail=f"job record gone: {exc}"
                )
                continue
            job_state = str(status.get("state") or "")
            if job_state in ("failed", "cancelled"):
                cause = self._failure_cause(status)
                self._skip(f"{KBDL_HASH_SKIP_JOB_FAILED_PREFIX}{cause}")
                if not self._retry_failed_archives():
                    reason = (
                        f"hash job {job_id!r} for archive {archive!r} is "
                        f"{cause}"
                    )
                    self._note_retryable(reason)
                    self._persist(state)
                    raise KbdlHashJobFailedError(
                        f"source {self.source.name!r}: {reason}. The command "
                        "STOPS on the terminal cause rather than skipping the "
                        "archive: a failed job leaves this archive's contents "
                        "unknown, and continuing would report a success over a "
                        "corpus nobody has read. Re-run with "
                        "--retry-failed-archives to submit a recorded NEW "
                        "attempt."
                    )
                job_id = self._submit(
                    archive, state, reason=KBDL_HASH_ATTEMPT_OPERATOR_RETRY
                )
                continue
            if job_state != "completed":  # pragma: no cover - _status is terminal
                raise ManifestSourceError(
                    f"source {self.source.name!r}: hash job {job_id!r} reported "
                    f"non-terminal state {job_state!r} after polling to "
                    "terminal."
                )
            try:
                result = self._get_client().get_job_result(job_id)
            except (KBDLNotFoundError, KBDLConflictError) as exc:
                job_id = self._resubmit_after_retention_loss(
                    archive, state, job_id, detail=f"result reclaimed: {exc}"
                )
                continue
            if not isinstance(result, Mapping):
                raise ManifestSourceError(
                    f"source {self.source.name!r}: hash job {job_id!r} returned "
                    f"a {type(result).__name__} result; KBDLHashGenomes returns "
                    "a JSON object with 'genomes' and 'errors'."
                )
            return result, status

        raise KbdlHashRetentionLostError(  # pragma: no cover - bound guard
            f"source {self.source.name!r}: archive {archive!r} could not be "
            "resolved to a readable hash result within the attempt bound."
        )

    def _resubmit_after_retention_loss(
        self, archive: str, state: dict[str, Any], job_id: str, *, detail: str
    ) -> str:
        """Resubmit ONCE for a confirmed retention loss; refuse a second time."""
        used = int(state.get(KBDL_HASH_STATE_RETENTION_RESUBMITS) or 0)
        if used >= 1:
            reason = (
                f"hash job {job_id!r} for archive {archive!r} was lost a "
                f"second time ({detail})"
            )
            self._note_retryable(reason)
            self._persist(state)
            raise KbdlHashRetentionLostError(
                f"source {self.source.name!r}: {reason}. ONE automatic "
                "resubmission answers KBDL's 30-day result retention; a "
                "freshly submitted job whose result is gone again is not a "
                "retention problem, and resubmitting on it would loop."
            )
        state[KBDL_HASH_STATE_RETENTION_RESUBMITS] = used + 1
        self._persist(state)
        return self._submit(
            archive, state, reason=KBDL_HASH_ATTEMPT_RETENTION_LOSS
        )

    # -- the standardizer-version probe -------------------------------------

    def _probe_standardizer_version(
        self, archive: str, result: Mapping[str, Any], state: dict[str, Any]
    ) -> None:
        """REFUSE the run if KBDL hashes with a different standardizer version.

        Round 2 of the confront requires a version probe before any hash job
        is submitted for a run. KBDL exposes no version endpoint, so the probe
        IS A ONE-ARCHIVE HASH JOB -- and this archive's own job is exactly
        that, so the probe costs no extra job and wastes no work. Because the
        driver runs units sequentially, one archive at a time, a mismatch
        raises here before ANY OTHER archive's job is submitted.

        THE PROBED VERSION IS THE JOB'S, NOT AN ENTRY'S OPINION: the result's
        top-level ``standardizer_version`` when it carries one, else the first
        ``genomes`` entry's. A mismatch there is a statement about the
        SERVICE and refuses the run; a mismatch on a LATER entry of an
        otherwise-agreeing job is a per-entry problem, counted as
        :data:`KBDL_HASH_SKIP_VERSION_MISMATCH` and left to make the archive
        incomplete (see :meth:`_rows`).
        """
        reported = result.get("standardizer_version")
        if not isinstance(reported, str) or not reported:
            genomes = result.get("genomes")
            if isinstance(genomes, Mapping):
                for entry in genomes.values():
                    if isinstance(entry, Mapping):
                        candidate = entry.get("standardizer_version")
                        if isinstance(candidate, str) and candidate:
                            reported = candidate
                            break
        if not isinstance(reported, str) or not reported:
            # An errors-only job reports no version at all. Nothing to
            # compare, so nothing is claimed: the per-entry check still gates
            # every row that does arrive.
            state[KBDL_HASH_STATE_PROBE] = None
            return
        state[KBDL_HASH_STATE_PROBE] = reported
        if reported != standardizers.STANDARDIZER_VERSION:
            raise KbdlHashVersionMismatchError(
                f"source {self.source.name!r}: KBDL hashed archive "
                f"{archive!r} with standardizer version {reported!r}, and this "
                f"pod runs {standardizers.STANDARDIZER_VERSION!r}. The run is "
                "REFUSED before any further hash job is submitted. "
                "entity_hash is the clearinghouse's join key, so two "
                "standardizer versions are two hash RULES: rows loaded under "
                "one would look like different entities from the same genome "
                "loaded under the other, and nothing downstream could detect "
                "it. Upgrade KBDL on poplar, or run the backfill from a pod on "
                f"standardizer {reported!r}."
            )

    # -- emission -----------------------------------------------------------

    def iter_records(self) -> Iterator[Mapping[str, Any]]:
        """Yield one record per hashed genome of this unit's archive.

        The order of operations is the contract:

        1. RESOLVE THE ACCEPTED JOB -- read a recorded one, or submit (id
           persisted first), poll and read. See :meth:`_resolve_result`.
        2. PROBE THE VERSION, refusing the run on a service-level mismatch
           before any other archive's job is submitted.
        3. EMIT one row per ``genomes`` entry, counting every ``errors`` entry
           and every per-entry version mismatch by reason -- and making the
           archive INCOMPLETE for the retryable ones.
        """
        self._exhausted = False
        archive = self._archive()
        state = dict(self._read_state())
        state[KBDL_HASH_STATE_ARCHIVE] = archive
        result, status = self._resolve_result(archive, state)
        self._probe_standardizer_version(archive, result, state)

        emitted = 0
        for row in self._rows(archive, result, status):
            if not self._admit_record():
                break
            emitted += 1
            yield row
        else:
            # Reached only by running off the end of the entries -- a --limit
            # break leaves `exhausted` False, which is what makes the unit
            # `partial` rather than `done`.
            self._exhausted = True

        state[KBDL_HASH_STATE_EMITTED] = emitted
        self._persist(state)

    def _rows(
        self,
        archive: str,
        result: Mapping[str, Any],
        status: Mapping[str, Any],
    ) -> Iterator[Mapping[str, Any]]:
        """One row per usable ``genomes`` entry; everything else counted.

        THE ERROR MAP IS READ BY CODE, because the codes mean different things
        (round 2 of the confront):

        - ``invalid_input`` -> TERMINAL. A reported exclusion: counted, and the
          archive is still DONE, because the member will read the same way
          forever.
        - anything else, ``internal_error`` included -> RETRYABLE. The archive
          is INCOMPLETE: its successful rows are still emitted (and ingested,
          and verified), and the command exits non-zero so a later run comes
          back for the rest. Nothing is auto-resubmitted.

        A PER-ENTRY STANDARDIZER VERSION MISMATCH is retryable in the same
        sense: the digest was computed by a rule this pod does not implement,
        so the row is dropped rather than loaded under the wrong rule, and the
        archive is not finished.
        """
        observed_at = self._observed_at(status)
        genomes = result.get("genomes")
        if genomes is None:
            genomes = {}
        if not isinstance(genomes, Mapping):
            raise ManifestSourceError(
                f"source {self.source.name!r}: hash result for archive "
                f"{archive!r} has a {type(genomes).__name__} 'genomes'; "
                "KBDLHashGenomes returns a mapping of genome key -> entry."
            )
        errors = result.get("errors")
        if isinstance(errors, Mapping):
            for key, failure in errors.items():
                code = ""
                message = ""
                if isinstance(failure, Mapping):
                    code = str(failure.get("code") or "")
                    message = str(failure.get("message") or "")
                code = code or "unknown"
                self._skip(f"{KBDL_HASH_SKIP_ERROR_PREFIX}{code}")
                if code not in KBDL_HASH_TERMINAL_ERROR_CODES:
                    self._note_retryable(
                        f"archive {archive!r} member {key!r} failed to hash "
                        f"with a retryable code {code!r}: "
                        f"{message or 'no message'}"
                    )

        for genome_key, entry in genomes.items():
            if not isinstance(entry, Mapping):
                self._skip(KBDL_HASH_SKIP_ENTRY_UNUSABLE)
                self._note_retryable(
                    f"archive {archive!r} genome {genome_key!r} is a "
                    f"{type(entry).__name__}, not a hash entry"
                )
                continue
            version = entry.get("standardizer_version")
            if version != standardizers.STANDARDIZER_VERSION:
                # NOT loaded under this pod's rule. A digest from another
                # standardizer version is another identity space; a row that
                # carried it would collide with nothing and join to nothing
                # while looking exactly like a genome entity.
                self._skip(KBDL_HASH_SKIP_VERSION_MISMATCH)
                self._note_retryable(
                    f"archive {archive!r} genome {genome_key!r} was hashed "
                    f"with standardizer version {version!r}, not this pod's "
                    f"{standardizers.STANDARDIZER_VERSION!r}"
                )
                continue
            entity_hash = entry.get("entity_hash")
            entry_name = entry.get("entry_name")
            if not entity_hash or not entry_name:
                self._skip(KBDL_HASH_SKIP_ENTRY_UNUSABLE)
                self._note_retryable(
                    f"archive {archive!r} genome {genome_key!r} is missing "
                    "entity_hash or entry_name"
                )
                continue
            yield {
                "genome_key": genome_key,
                "entity_hash": entity_hash,
                "standardizer_version": version,
                # The entry's own archive id when it carries one (it does),
                # falling back to the unit's -- the two agree, and preferring
                # the entry's keeps the pointer a statement about the result
                # rather than about the loop.
                "archive_object_id": entry.get("archive_object_id") or archive,
                "entry_name": entry_name,
                "n_contigs": entry.get("n_contigs"),
                "total_length": entry.get("total_length"),
                "longest_contig": entry.get("longest_contig"),
                "shortest_contig": entry.get("shortest_contig"),
                "observed_at": observed_at,
            }

    def _observed_at(self, status: Mapping[str, Any]) -> str:
        """When the hash was observed: the job's completion time, else run start.

        A job's own completion time is the honest observation time for every
        digest it produced -- the hash is a property of the archive as it stood
        when the job read it, not of when this backfill got round to loading
        it. The run start is the fallback when the status record carries no
        usable time, which keeps ``observed_at`` non-null (the sharder refuses
        a null in a DECLARED observed_at column).
        """
        for key in ("completed_at", "finished_at", "ended_at", "updated_at"):
            value = status.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return self._run_start


#: Concrete adapter classes, by name. Every declared adapter is built:
#: ``file`` (CPU/disk only), ``lakehouse`` (in-pod / off-pod read), ``mongo``
#: (a batched collection read), ``arc`` (Chris's KOROS arc store on the
#: BERDL pod -- a read-only sqlite index, gzipped job results, optional backup
#: tarballs, joined to genome objects pulled from KBDL) and ``kbdl_hash``
#: (KBDL's own ``KBDLHashGenomes`` job, one per GenomeArchive, hashing the
#: FASTA members in place on poplar so no sequence crosses the network).
#: :func:`get_adapter` no longer reports any declared name as unbuilt.
_ADAPTER_CLASSES: dict[str, type[SourceAdapter]] = {
    "file": FileSourceAdapter,
    "lakehouse": LakehouseSourceAdapter,
    "mongo": MongoSourceAdapter,
    "arc": ArcSourceAdapter,
    "kbdl_hash": KbdlHashSourceAdapter,
}


def get_adapter(
    source: cm.Source,
    *,
    run_context: Any = None,
    unit: str | None = None,
    backfill: cm.BackfillInfo | None = None,
) -> SourceAdapter:
    """Construct the adapter a source's ``adapter`` key names.

    Args:
        source: The parsed ``[[source]]`` block.
        run_context: The backfill run context to bind (see
            :meth:`SourceAdapter.bind_run_context`), or ``None`` for a direct
            ``kbu clearinghouse shard``.
        unit: A unit label to restrict the read to (see
            :meth:`SourceAdapter.restrict_to_unit`), or ``None``.
        backfill: The manifest's ``[backfill]`` table to bind (see
            :meth:`SourceAdapter.bind_backfill_info`), or ``None``.

    Raises:
        cm.ManifestError: The ``adapter`` name is not one of
            :data:`ADAPTER_NAMES` at all.
        AdapterNotImplementedError: The name is in :data:`ADAPTER_NAMES` but
            maps to no registered class. With ``file``, ``lakehouse``,
            ``mongo``, ``arc`` and ``kbdl_hash`` all built, this no longer
            fires for any declared name.
        ManifestSourceError: ``unit`` names a unit this adapter does not
            decompose into.
    """
    name = source.adapter
    if name not in ADAPTER_NAMES:
        raise cm.ManifestError(
            f"source {source.name!r}: unknown adapter {name!r}; expected one "
            f"of {', '.join(ADAPTER_NAMES)}."
        )
    cls = _ADAPTER_CLASSES.get(name)
    if cls is None:
        raise AdapterNotImplementedError(
            f"source {source.name!r}: adapter {name!r} is planned but not "
            "implemented yet."
        )
    adapter = cls(source)
    if run_context is not None:
        adapter.bind_run_context(run_context)
    if backfill is not None:
        adapter.bind_backfill_info(backfill)
    if unit is not None:
        adapter.restrict_to_unit(unit)
    return adapter


# --------------------------------------------------------------------------
# Derivation evaluation
# --------------------------------------------------------------------------


def _derive(derivation: cm.Derivation, row: Mapping[str, Any]) -> Any:
    """Evaluate one parsed :class:`~clearinghouse_manifest.Derivation` on a row.

    Hashing goes through :mod:`standardizers` ONLY -- never a rule invented
    here -- which is the invariant that keeps the spool and the lakehouse
    agreeing about what a protein is.
    """
    name = derivation.name
    args = derivation.args
    if name == "column":
        return row.get(args[0])
    if name == "len":
        value = row.get(args[0])
        if value is None:
            return None
        # A LIST's length is its element count, not the character count of its
        # repr. ``len(str(["c1", "c2"]))`` is 14, so the worked example's
        # ``n_contigs = "@len(contig_ids)"`` recorded 14 contigs for a
        # two-contig genome. Only a scalar is stringified.
        if isinstance(value, (list, tuple)):
            return len(value)
        return len(str(value))
    if name == "lower":
        value = row.get(args[0])
        return None if value is None else str(value).lower()
    if name == "prefix":
        text, column = args
        value = row.get(column)
        # NULL IN, NULL OUT. A prefixed null would become the literal
        # pointer "kbdl:obj:None" -- a reference to an object that does not
        # exist, and indistinguishable in the lake from a real one. A missing
        # pointer stays missing.
        return None if value is None else f"{text}{value}"
    if name == "concat":
        # STRICTLY ALTERNATING literal, column, literal, column -- the arity
        # check in clearinghouse_manifest guarantees an even count and a
        # non-empty name in every odd position, so this walk needs no
        # defence of its own.
        pieces: list[str] = []
        for index in range(0, len(args), 2):
            value = row.get(args[index + 1])
            # NULL IN, NULL OUT, across EVERY named column, for the same
            # reason @prefix is null-preserving: a pointer assembled from a
            # missing half ("kbdl:archive:None:x") is a reference to nothing
            # and is indistinguishable in the lake from a real one.
            if value is None:
                return None
            pieces.append(f"{args[index]}{value}")
        return "".join(pieces)
    if name == "const":
        return args[0]
    if name == "hash":
        entity_type, column = args
        value = row.get(column)
        if value is None:
            return None
        return standardizers.entity_hash(entity_type, value)
    if name == "json":
        payload = {col: row.get(col) for col in args}
        return standardizers.canonical_payload(payload).decode("utf-8")
    raise ValueError(f"unknown derivation {name!r}")  # pragma: no cover


def _compute_entity_hash(plan: cm.TablePlan, row: Mapping[str, Any]) -> str:
    """Compute the canonical ``entity_hash`` for a row per the plan's hash spec.

    ``raw`` mode hashes the raw column value through
    :func:`standardizers.entity_hash`; ``precomputed`` mode RE-CANONICALISES
    the source's digest through :func:`schema.encode_entity_hash` and never
    trusts it raw; ``contig_set`` mode (genome only) hashes the column's LIST
    of contig sequences through the same standardizer. Either way the result
    is the 64-char lowercase hex form.

    ``contig_set`` IS WHERE THE LIST-NESS IS ENFORCED, and it refuses a
    ``str`` explicitly. ``entity_hash("genome", "ACGT")`` does not raise -- the
    genome standardizer iterates its argument, so a string is hashed as the
    SET OF ITS CHARACTERS and yields a confident, wrong, reproducible digest.
    That is the one failure mode this mode exists to prevent, so a non-list
    is a :class:`ShardValueError` for that row (counted like any other
    unmappable record) rather than a hash nobody can explain.
    """
    mode, column, entity_type = plan.hash_source
    value = row.get(column)
    if value is None:
        raise ShardValueError(
            f"source {plan.source_name!r} table {plan.table!r}: hash column "
            f"{column!r} is missing/None on a record; every record must carry "
            "a hashable value."
        )
    if mode == "raw":
        return standardizers.entity_hash(entity_type, value)
    if mode == "contig_set":
        if isinstance(value, tuple):
            value = list(value)
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise ShardValueError(
                f"source {plan.source_name!r} table {plan.table!r}: "
                f"[source.hash].contig_set column {column!r} must hold a LIST "
                f"of contig sequence STRINGS, got {type(value).__name__}. A "
                "bare string is refused rather than hashed: the genome "
                "standardizer iterates its argument, so a string would be "
                "hashed as the set of its characters and produce a confident "
                "wrong digest."
            )
        return standardizers.entity_hash(entity_type, value)
    # precomputed: re-canonicalise, never trust raw.
    return schema.encode_entity_hash(str(value))


class ShardValueError(ValueError):
    """A source record could not be mapped to a target row (e.g. null hash)."""


class ShardParameterSetError(ShardValueError):
    """A result row's PER-ROW parameter set was refused.

    A subclass of :class:`ShardValueError` so the sharder's existing
    one-record-skip handling applies unchanged, but a distinct type so the
    skip is counted under its own reason
    (:data:`SKIP_PARAMETER_SET`) instead of the generic
    ``shard-value-error``: "forty rows carried an unusable parameter set" and
    "forty rows had no hashable value" are different operational problems,
    and a run inventory that merged them would name neither.

    Raised only for the ``parameter_set_column`` form. The static form is
    validated once at plan time, where a failure is a
    :class:`~clearinghouse_manifest.ManifestError` and the run never starts.
    """


class ShardRegistryError(RuntimeError):
    """A result row carries a ``parameter_set_hash`` no registry batch explains.

    The registry is written FIRST and ingested first precisely so this cannot
    happen; this is the enforcement of that ordering rather than trust in it.
    Raised after a unit's batches are written and BEFORE they are published to
    ``run.json``, so an orphaned hash never becomes part of the run inventory.
    """


#: Skip reason slugs the SHARDER itself reports, merged with whatever an
#: adapter's :meth:`SourceAdapter.skipped` returns. Short, stable strings:
#: they are keys in ``run.json``, so renaming one invalidates every prior
#: run's inventory.
SKIP_NULL_OBSERVED_AT = "null-observed_at"
SKIP_SHARD_VALUE_ERROR = "shard-value-error"

#: A result row whose PER-ROW parameter set (``parameter_set_column``) was not
#: a mapping, carried a float/``None``/bad key, or failed the tool-specific
#: boundary rule. Counted separately from
#: :data:`SKIP_SHARD_VALUE_ERROR` -- see :class:`ShardParameterSetError`.
SKIP_PARAMETER_SET = "parameter-set-invalid"

#: The ``source`` recorded on a registry batch holding PER-ROW parameter sets.
#: A single label rather than one source name: a unit's per-row sets are
#: merged across every source that declared ``parameter_set_column`` (two
#: sources yielding the same set must produce ONE registry row, not two), so
#: no single source name is the truthful answer.
_PER_ROW_REGISTRY_SOURCE = "parameter_set_column"


# --------------------------------------------------------------------------
# ULID
# --------------------------------------------------------------------------

#: Crockford base32: the digits, then the uppercase letters with I, L, O and U
#: removed (I/L look like 1, O looks like 0, U is dropped to avoid accidental
#: obscenities). This exact alphabet is what makes a ULID's lexicographic
#: string order match its binary order, which is the property every consumer
#: of ``ingest_batch_id`` relies on.
_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: 48-bit millisecond timestamp, encoded in 10 base32 characters.
_ULID_TIME_CHARS = 10
#: 80 random bits, encoded in 16 base32 characters. 26 characters in total.
_ULID_RANDOM_CHARS = 16

_ULID_TIME_BITS = 48
_ULID_RANDOM_BITS = 80

#: Monotonic state, so two ULIDs minted inside one millisecond still order.
#: Guarded by a lock because the sharder is explicitly designed to be fanned
#: out (see :func:`shard_source`) and two threads minting the same batch id
#: would produce two batch FILES with one name.
_ULID_LOCK = threading.Lock()
_ULID_STATE: dict[str, int] = {"ms": -1, "random": -1}

#: What a ULID looks like. Crockford base32 excludes I, L, O and U, so the
#: character class is not simply ``[0-9A-Z]``.
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def _encode_crockford(value: int, chars: int) -> str:
    """Encode ``value`` as exactly ``chars`` Crockford base32 characters."""
    out: list[str] = []
    for _ in range(chars):
        out.append(_CROCKFORD_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def new_ulid() -> str:
    """Mint a ULID: 48-bit ms timestamp + 80 random bits, Crockford base32.

    DEPENDENCY-FREE on purpose. The shard stage must stay importable on any
    machine with CPU and disk and no credentials (see the module docstring),
    so it does not acquire a package to produce 26 characters.

    WHY A ULID AND NOT A STABLE NAME. ``ingest_batch_id`` is one of the two
    columns the current-state derivation orders by
    (:data:`~clearinghouse_derivation.ORDER_COLUMNS`): two appends can share
    an ``observed_at``, and without a second, MONOTONIC key "the current row"
    for a slot is genuinely ambiguous. The sharder used to stamp
    ``f"{source_name}:{table}"`` -- one constant per source, which is neither
    monotonic nor unique, so every row a source ever wrote, across every run,
    tied on both ordering columns. A ULID is time-ordered, so comparing it as
    an ordinary string ``ORDER BY`` column is correct, which is exactly what
    the derivation does.

    MONOTONIC WITHIN A PROCESS: minting twice inside one millisecond
    increments the random component rather than drawing a fresh one, so the
    second id still sorts after the first. (Across processes, ordering is by
    the millisecond timestamp; two batches minted in the same millisecond on
    two machines are not ordered relative to each other, which is correct --
    they are concurrent.)

    Returns:
        A 26-character uppercase Crockford base32 string.
    """
    with _ULID_LOCK:
        now_ms = int(time.time() * 1000) & ((1 << _ULID_TIME_BITS) - 1)
        if now_ms > _ULID_STATE["ms"]:
            ms, rand = now_ms, secrets.randbits(_ULID_RANDOM_BITS)
        else:
            # Same millisecond (or a clock that went backwards): keep the
            # recorded millisecond and step the random component so the id is
            # still strictly greater than the last one this process minted.
            ms = _ULID_STATE["ms"]
            rand = _ULID_STATE["random"] + 1
            if rand >= (1 << _ULID_RANDOM_BITS):  # pragma: no cover - 2^80 ids
                ms += 1
                rand = secrets.randbits(_ULID_RANDOM_BITS)
        _ULID_STATE["ms"], _ULID_STATE["random"] = ms, rand
    return _encode_crockford(ms, _ULID_TIME_CHARS) + _encode_crockford(
        rand, _ULID_RANDOM_CHARS
    )


def is_ulid(value: Any) -> bool:
    """Whether ``value`` is a well-formed ULID as :func:`new_ulid` mints them."""
    return isinstance(value, str) and bool(_ULID_RE.match(value))


# --------------------------------------------------------------------------
# observed_at
# --------------------------------------------------------------------------


def utc_now() -> datetime:
    """The current UTC instant as a NAIVE datetime.

    Naive, not tz-aware, because every ``observed_at`` column in
    :func:`clearinghouse_schema.table_configs` is declared ``TIMESTAMP`` (not
    ``TIMESTAMP WITH TIME ZONE``). Carrying a tz-aware value here would make
    pyarrow infer ``timestamp[us, tz=UTC]`` for the bronze column and hand the
    ingest a type the DDL does not declare. The value is UTC; the tzinfo is
    dropped only after the conversion, never instead of it.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def resolve_observed_at(value: Any, *, where: str) -> datetime:
    """Normalise a declared ``observed_at_column`` value to naive UTC.

    Accepts a :class:`~datetime.datetime` (tz-aware is converted to UTC; naive
    is ASSUMED to be UTC already -- a source that stores local times without
    an offset cannot be disambiguated here and must be fixed at the source), a
    :class:`~datetime.date` (midnight), or an ISO-8601 string, including the
    ``Z`` suffix :func:`datetime.fromisoformat` rejects before Python 3.11.

    Raises:
        ShardValueError: ``value`` is null, blank, not a recognised type, or
            is a string that is not ISO-8601. Null is a skip the caller counts
            as :data:`SKIP_NULL_OBSERVED_AT`, never a silently defaulted row:
            a source that DECLARED a column and then has no value for it is
            making a claim the sharder cannot quietly substitute the run start
            time for, because the two mean different things.
    """
    if value is None:
        raise ShardValueError(f"{where}: observed_at is null.")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ShardValueError(f"{where}: observed_at is blank.")
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ShardValueError(
                f"{where}: observed_at {value!r} is not ISO-8601 ({exc})."
            ) from exc
        return resolve_observed_at(parsed, where=where)
    raise ShardValueError(
        f"{where}: observed_at {value!r} is a {type(value).__name__}; expected "
        "an ISO-8601 string or a datetime."
    )


def _iso_z(moment: datetime) -> str:
    """Render a naive-UTC datetime as an ISO-8601 ``Z`` string."""
    return moment.isoformat() + "Z"


def _observed_micros(value: Any) -> int:
    """Microseconds since the epoch for a naive-UTC ``observed_at``, for sorting."""
    if not isinstance(value, datetime):
        return 0
    moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1_000_000)


def _row_parameter_set(
    plan: cm.TablePlan,
    row: Mapping[str, Any],
    *,
    source_value: Any,
    collector: dict[str, str] | None,
) -> str:
    """Canonicalise and hash ONE row's own parameter set; record it for the
    registry.

    The per-row counterpart of
    :func:`clearinghouse_manifest._plan_parameter_set`, and deliberately a
    call INTO the same shared function
    (:func:`clearinghouse_parameters.result_parameter_set_identity`) rather
    than a second implementation: a second answer to "what is this parameter
    set" is the one thing the registry exists to prevent, and the test that
    an equal static and per-row set produce the same canonical text and hash
    is only meaningful because both go through this one rule.

    Returns:
        The row's 64-char lowercase hex ``parameter_set_hash``.

    Raises:
        ShardParameterSetError: The value is absent, is not a mapping, or
            either validation layer refused it. ONE row is lost, counted
            under :data:`SKIP_PARAMETER_SET`.
    """
    column = plan.parameter_set_column
    where = (
        f"source {plan.source_name!r} [source.result]."
        f"{cm.PARAMETER_SET_COLUMN_KEY} {column!r}"
    )
    if column not in row:
        raise ShardParameterSetError(
            f"{where}: the record carries no such column. The source declared "
            "its parameter set comes from this column, so a record without it "
            "has no parameter set and cannot be keyed."
        )
    params = row[column]
    try:
        canonical, digest = cp.result_parameter_set_identity(
            params, source=source_value, where=where
        )
    except ParameterSetError as exc:
        raise ShardParameterSetError(str(exc)) from exc
    if collector is not None:
        collector.setdefault(digest, canonical)
    return digest


def _build_row(
    plan: cm.TablePlan,
    row: Mapping[str, Any],
    *,
    run_observed_at: datetime,
    parameter_sets: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build one bronze output row from a source record for a target table.

    Fills the mapped columns via :func:`_derive`, computes ``entity_hash``, and
    stamps the identity/provenance columns (``entity_type``,
    ``standardizer_version``, ``observed_at``) the sharder owns.
    ``ingest_batch_id`` is deliberately NOT set here: it is the ULID of the
    OUTPUT BATCH this row ends up in, and which batch that is is not known
    until the external merge cuts one, so :func:`_write_batch` stamps it.

    ``observed_at`` IS NEVER NULL. It is either this record's own
    ``observed_at_column`` value (normalised to naive UTC by
    :func:`resolve_observed_at`) or ``run_observed_at`` -- the shard run's
    start time, one value for the whole run. It used to be left ``None`` here
    with a docstring claiming ingest would stamp it; nothing did, and
    ``observed_at`` is one of the two columns the current-state derivation
    orders by, so every row in the corpus tied on it.

    On a ``result`` row it also stamps ``parameter_set_hash``, from ONE of two
    places depending on which form the source declared:

    - STATIC ``parameter_set``: the hash the plan computed ONCE at plan time,
      not re-derived per row. When the plan's result ``source`` is a
      derivation rather than a ``@const(...)``, the tool was unknown at plan
      time, so the shared TRANSYT result-boundary validator
      (:func:`clearinghouse_parameters.validate_transyt_parameter_set`) runs
      HERE, against this row's resolved ``source``.
    - PER-ROW ``parameter_set_column``: the row's OWN set, canonicalised and
      hashed here through
      :func:`clearinghouse_parameters.result_parameter_set_identity` -- the
      same function the static form uses at plan time, which is what makes
      the two forms produce byte-identical canonical text and an identical
      hash for an equal set. Both layers (the generic parameter-set rule and
      the tool-specific TRANSYT rule) run, against this row's resolved
      ``source``, and a refusal is a :class:`ShardParameterSetError` that
      skips THIS ROW rather than failing the run.

    ``parameter_sets`` is the per-row form's only other output: the sharder
    must register every DISTINCT set it saw, and the canonical text that
    explains a hash is produced here and nowhere else. It is a
    ``{hash: canonical_json}`` collector the caller owns; ``None`` when the
    caller does not need it (a direct ``_build_row`` call in a test).
    """
    out: dict[str, Any] = {}
    for column, derivation in plan.columns.items():
        out[column] = _derive(derivation, row)
    out["entity_hash"] = _compute_entity_hash(plan, row)
    out["standardizer_version"] = standardizers.STANDARDIZER_VERSION
    if plan.kind in ("entity", "result"):
        out["entity_type"] = plan.entity_type
    if plan.kind == "result":
        if plan.parameter_set_column is not None:
            out["parameter_set_hash"] = _row_parameter_set(
                plan, row, source_value=out.get("source"), collector=parameter_sets
            )
        else:
            out["parameter_set_hash"] = plan.parameter_set_hash
            if cm.constant_result_source(plan.columns) is None:
                cp.validate_transyt_parameter_set(
                    plan.parameter_set or {},
                    source=out.get("source"),
                    where=(
                        f"source {plan.source_name!r} [source.result]"
                        ".parameter_set"
                    ),
                )
    if plan.observed_at_column is None:
        out["observed_at"] = run_observed_at
    else:
        out["observed_at"] = resolve_observed_at(
            row.get(plan.observed_at_column),
            where=(
                f"source {plan.source_name!r} table {plan.table!r} column "
                f"{plan.observed_at_column!r}"
            ),
        )
    return out


# --------------------------------------------------------------------------
# The sharder
# --------------------------------------------------------------------------

#: The file name of the run inventory, written at the bronze output ROOT (not
#: inside a table directory, so ``_discover_shards`` -- which only descends
#: into directories named for a known table -- never mistakes it for a shard).
RUN_MANIFEST_FILE = "run.json"

#: Schema version of ``run.json``. Bump when a field changes meaning.
RUN_MANIFEST_VERSION = 1

#: Upper bound on rows decoded per record batch when reading a sorted run
#: back during a merge.
_RUN_READ_BATCH_ROWS = 1024

#: HARD CEILING on the rows the merge frontier holds, across every open run,
#: independent of source size. The per-run read batch is
#: ``min(_RUN_READ_BATCH_ROWS, _MERGE_ROW_BUDGET // open_runs)``, so adding
#: runs shrinks each one's batch instead of growing the total. Without this
#: the frontier would be ``runs x batch``, which grows with the source -- the
#: exact unbounded-memory shape this rewrite exists to remove.
_MERGE_ROW_BUDGET = 4096

#: Maximum runs merged in ONE pass. A range with more is reduced first, in
#: groups of this size, into fewer intermediate runs -- an ordinary multi-pass
#: external merge. Two things make the cap necessary rather than tidy at this
#: scale: a single-pass merge of a 264M-record source at a 512 MB run size
#: opens every run at once (hundreds of file descriptors, and on a smaller
#: ``target_bytes`` thousands), and :data:`_MERGE_ROW_BUDGET` divided by
#: thousands of runs floors at one row per read, which turns the merge into a
#: per-row parquet decode. Bounded fan-in keeps both finite.
_MAX_MERGE_FANIN = 64

#: Transient per-row keys the sharder carries through a spill and strips
#: before writing an output batch. They are prefixed so they cannot collide
#: with a clearinghouse column name.
_ROW_RANGE_KEY = "__range"
_ROW_SOURCE_KEY = "__source"

#: The result slot key, IMPORTED, never restated here. See
#: :data:`clearinghouse_derivation.SLOT_KEY_COLUMNS` for why there is exactly
#: one definition of this tuple and this is not it.
SLOT_KEY_COLUMNS: tuple[str, ...] = cd.SLOT_KEY_COLUMNS


@dataclass
class ShardMetrics:
    """Instrumentation of ONE sharding call: what it held in memory, and when.

    The bounded-memory property this stage exists to have is not visible in
    its output -- a correct 300M-row run and a run that materialised 300M rows
    in a list write the same parquet. So the sharder records what it actually
    held, and the tests assert on THESE numbers rather than on the files.

    Attributes:
        ranges: Equal-width hash ranges cut for the table.
        rows_per_range: Rows ROUTED to each range index. A range's row count
            is emphatically not uniform even though the hash space is: a
            function corpus maps millions of records onto a few repeated
            strings, so one hash -- one range -- can carry millions of rows.
        sorted_runs: Bounded sorted runs spilled to disk, across all ranges.
        max_sort_buffer_rows: High-water mark of the streaming sort buffer.
            Bounded by ``target_bytes``, NOT by a range's size.
        max_batch_buffer_rows: High-water mark of the output batch being
            accumulated during a merge.
        max_resident_rows: High-water mark of total row dicts held at once --
            the number that must stay well under ``max(rows_per_range)`` for
            the "never materialise a range" claim to be true.
        rows_emitted: Rows the adapters yielded.
        rows_accepted: Rows that became bronze rows (emitted minus skipped).
        rows_written: Rows written to parquet.
        rows_deduplicated: Rows dropped as duplicates during the merges.
    """

    ranges: int = 0
    rows_per_range: dict[int, int] = field(default_factory=dict)
    sorted_runs: int = 0
    max_sort_buffer_rows: int = 0
    max_batch_buffer_rows: int = 0
    max_resident_rows: int = 0
    rows_emitted: int = 0
    rows_accepted: int = 0
    rows_written: int = 0
    rows_deduplicated: int = 0
    #: High-water mark of bytes the spill directory held -- evidence that the
    #: records really went to DISK rather than being accumulated in a list.
    spill_dir_bytes: int = 0
    #: Merge passes performed. More than one per non-empty range means the
    #: fan-in cap forced a multi-pass merge.
    merge_passes: int = 0

    def note_resident(self, rows: int) -> None:
        """Record a point-in-time count of row dicts held in memory."""
        if rows > self.max_resident_rows:
            self.max_resident_rows = rows


@dataclass(frozen=True)
class ShardReport:
    """What one written shard is, for the build's own verification and ledger.

    ``hash_min``/``hash_max`` are the shard's contiguous range over
    ``key_column`` -- ``entity_hash`` for the fifteen entity-typed tables, and
    ``parameter_set_hash`` for the registry, which has no ``entity_hash`` at
    all. ``split`` is ``True`` when this shard is one of several cut from a
    single hash range.
    """

    table: str
    batch_id: str
    path: Path
    rows: int
    hash_min: str
    hash_max: str
    split: bool = False
    #: Rows dropped as duplicates while this batch was being accumulated.
    rows_deduplicated: int = 0
    #: sha256 of the written file, so ``run.json`` can be checked against disk.
    sha256: str = ""
    #: The sorted source names that contributed rows to this batch. A unit
    #: merges every plan targeting one table TOGETHER, so a batch is routinely
    #: mixed and a single ``source`` string would be a lie.
    sources: tuple[str, ...] = ()
    #: Which column ``hash_min``/``hash_max`` refer to.
    key_column: str = "entity_hash"
    #: The slice label this batch belongs to, or ``None``.
    slice: str | None = None
    #: The unit that produced this batch, and that unit's attempt.
    unit_id: str = ""
    attempt_id: str = ""


@dataclass(frozen=True)
class SkipReport:
    """Records that one source dropped ``count`` records for one ``reason``."""

    source: str
    reason: str
    count: int


@dataclass(frozen=True)
class SourceOutcome:
    """Per-source accounting for ``run.json``.

    ``exhausted`` is recorded SEPARATELY from the counts and is the only field
    that distinguishes a complete unit from a stopped one; see
    :meth:`SourceAdapter.exhausted`.
    """

    source: str
    table: str
    emitted: int
    accepted: int
    exhausted: bool | None
    #: The adapter's :meth:`SourceAdapter.watermark`, or ``None``. Recorded
    #: so a run says what window it covered, NOT as a completion claim.
    watermark: str | None = None
    #: The adapter's :meth:`SourceAdapter.incomplete_reasons`. A non-empty
    #: tuple makes the unit ``incomplete`` even when every batch verified.
    incomplete_reasons: tuple[str, ...] = ()
    #: The adapter's :meth:`SourceAdapter.checkpoint_candidates` -- the
    #: checkpoint keys this source earned, recorded so the driver promotes
    #: this unit's candidates and nobody else's.
    checkpoint_candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class ShardRunContext:
    """The run/unit identity and the one timestamp a shard run stamps.

    Threaded through the sharder rather than passed as six arguments, because
    every one of these is constant for the whole call and three of them
    (``run_id``, ``unit_id``, ``attempt_id``) end up on every batch entry in
    ``run.json``.
    """

    run_id: str
    unit_id: str
    attempt_id: str
    slice: str | None
    #: The run's start time, naive UTC. Stamped as ``observed_at`` on every row
    #: of every source that declares no ``observed_at_column`` -- ONE value for
    #: the whole run, deliberately, so a run is one observation event.
    observed_at: datetime
    target_bytes: int = DEFAULT_TARGET_BYTES
    metrics: ShardMetrics | None = None

    @classmethod
    def build(
        cls,
        *,
        run_id: str | None = None,
        unit_id: str | None = None,
        attempt_id: str | None = None,
        slice_label: str | None = None,
        observed_at: datetime | None = None,
        target_bytes: int = DEFAULT_TARGET_BYTES,
        metrics: ShardMetrics | None = None,
    ) -> ShardRunContext:
        """Build a context, minting any identifier the caller did not supply."""
        return cls(
            run_id=run_id or new_ulid(),
            unit_id=unit_id or new_ulid(),
            attempt_id=attempt_id or new_ulid(),
            slice=slice_label,
            observed_at=observed_at or utc_now(),
            target_bytes=target_bytes,
            metrics=metrics,
        )


def _range_boundaries(n: int) -> list[str]:
    """Return ``n + 1`` equal-width hex boundaries over ``[0, ffff...]``.

    Boundaries are COMPUTED from the uniform hash space, never observed. The
    returned list has ``n + 1`` entries; range ``i`` covers
    ``[boundaries[i], boundaries[i + 1])`` (the last is closed at the top).
    """
    space = 1 << (4 * _HASH_HEX_CHARS)  # 16 ** 64
    boundaries: list[str] = []
    for i in range(n + 1):
        value = (space * i) // n
        if value >= space:
            value = space - 1
        boundaries.append(f"{value:0{_HASH_HEX_CHARS}x}")
    return boundaries


def _range_index(entity_hash: str, boundaries: list[str], n: int) -> int:
    """Route a hash to its range index under the half-open ``[lo, hi)`` convention.

    Each range ``i`` covers ``[boundaries[i], boundaries[i + 1])``, so a record
    whose hash equals an interior boundary belongs to the range that STARTS at
    that boundary (range ``i``), not the one that ends at it. The rule is fixed
    and total, so a re-run reproduces the same shards deterministically. This
    routing is what keeps the emitted ranges disjoint (asserted in
    :func:`_assert_sorted_and_disjoint`).
    """
    # boundaries[i] <= entity_hash gives range i for i in [0, n); clamp the top.
    lo, hi = 0, n - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if boundaries[mid] <= entity_hash:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _row_bytes(row: Mapping[str, Any]) -> int:
    """Rough in-memory size of one row, for bounding buffers and cutting batches."""
    total = 0
    for key, value in row.items():
        if value is None or key == _ROW_RANGE_KEY:
            continue
        total += len(str(value))
    return total


def _write_parquet(rows: list[dict[str, Any]], columns: list[str], path: Path) -> None:
    """Write ``rows`` (already sorted) to a parquet file with ``columns`` order."""
    table = pa.Table.from_pylist(
        [{col: row.get(col) for col in columns} for row in rows]
    )
    pq.write_table(table, str(path))


def _file_sha256(path: Path) -> str:
    """Stream a file through sha256 -- never read a 512 MB shard into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Sort / dedup keys
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _KeySpec:
    """How one target table is keyed, ordered and deduplicated.

    ``dedup_columns`` is the identity a duplicate shares: ``entity_hash`` for
    an entity/content table, and the IMPORTED
    :data:`clearinghouse_derivation.SLOT_KEY_COLUMNS` for a result table --
    one hash can legitimately carry several slots (two tools, two parameter
    sets), so deduplicating a result table on ``entity_hash`` would throw away
    real rows.
    """

    key_column: str
    dedup_columns: tuple[str, ...]
    #: ``True`` for result tables: the winner among rows sharing a slot is the
    #: one with the LATEST ``observed_at``, matching the ORDER BY the
    #: current-state view will apply (``observed_at DESC``). For entity and
    #: content there is no superseding -- the rows are identical by
    #: construction -- so order is by canonical bytes and the first wins.
    observed_at_descending: bool
    #: The columns whose canonical bytes break a remaining tie, ASCENDING.
    tiebreak_columns: tuple[str, ...]


def _key_spec(entity_type: str | None, kind: str) -> _KeySpec:
    """Build the :class:`_KeySpec` for one target table."""
    if kind == "registry":
        return _KeySpec(
            key_column="parameter_set_hash",
            dedup_columns=("parameter_set_hash",),
            observed_at_descending=False,
            tiebreak_columns=("canonical_json",),
        )
    assert entity_type is not None
    if kind == "result":
        return _KeySpec(
            key_column="entity_hash",
            dedup_columns=tuple(SLOT_KEY_COLUMNS),
            observed_at_descending=True,
            tiebreak_columns=("payload",),
        )
    # entity / content: identity is the hash; the tie-break is the whole
    # mapped row, excluding the two provenance columns that differ per batch
    # (ingest_batch_id) or per record (observed_at) without making the row a
    # different row.
    columns = tuple(
        col
        for col in schema.column_names(entity_type, kind)
        if col not in ("observed_at", "ingest_batch_id")
    )
    return _KeySpec(
        key_column="entity_hash",
        dedup_columns=("entity_hash",),
        observed_at_descending=False,
        tiebreak_columns=columns,
    )


def _order_text(value: Any) -> str:
    """A total, deterministic string ordering for a key column value."""
    return "" if value is None else str(value)


def _canonical_tiebreak(spec: _KeySpec, row: Mapping[str, Any]) -> bytes:
    """Canonical bytes of the tie-break columns, through the shared canonicaliser."""
    if len(spec.tiebreak_columns) == 1:
        return standardizers.canonical_payload(row.get(spec.tiebreak_columns[0]))
    return standardizers.canonical_payload(
        {col: row.get(col) for col in spec.tiebreak_columns}
    )


def _make_sort_key(spec: _KeySpec) -> Callable[[Mapping[str, Any]], tuple]:
    """Return the total sort key for one table's rows.

    The key's PREFIX is ``dedup_columns``, which is what makes a merge-time
    duplicate check a comparison of ADJACENT rows rather than a dictionary of
    every key seen -- the difference between bounded memory and holding the
    whole range. Its first component is always ``key_column``, so sorting by
    this key also produces the ascending-hash physical order the whole stage
    exists to produce, and keeps every row of one key inside one hash range.
    """
    dedup = spec.dedup_columns
    if spec.observed_at_descending:
        # Slot key, then observed_at DESCENDING (the newest row for a slot
        # wins), then canonical payload bytes ASCENDING to make the choice
        # deterministic when two rows for a slot share a timestamp.
        def key(row: Mapping[str, Any]) -> tuple:
            return (
                tuple(_order_text(row.get(col)) for col in dedup),
                -_observed_micros(row.get("observed_at")),
                _canonical_tiebreak(spec, row),
            )

        return key

    def key(row: Mapping[str, Any]) -> tuple:
        return (
            tuple(_order_text(row.get(col)) for col in dedup),
            _canonical_tiebreak(spec, row),
            _observed_micros(row.get("observed_at")),
        )

    return key


def _make_dedup_key(spec: _KeySpec) -> Callable[[Mapping[str, Any]], tuple]:
    """Return the duplicate-identity key -- the sort key's own prefix."""
    dedup = spec.dedup_columns

    def key(row: Mapping[str, Any]) -> tuple:
        return tuple(_order_text(row.get(col)) for col in dedup)

    return key


# --------------------------------------------------------------------------
# Bounded sorted runs on disk, and the k-way merge that reads them back
# --------------------------------------------------------------------------


class _RunSpool:
    """Per-range SORTED RUNS on disk, and the external merge that drains them.

    This is the whole bounded-memory mechanism, and the reason it is an
    external SORT rather than "one spill file per range, sorted in memory":
    ``entity_hash`` is uniformly distributed, so equal-width ranges hold
    approximately equal numbers of DISTINCT hashes -- but not of RECORDS. A
    function corpus maps millions of records onto a few thousand repeated
    strings, so one hash, and therefore one range, can carry millions of rows.
    Sizing a range from an estimate and then sorting it in memory assumes away
    exactly the distribution that breaks it.

    So: records are routed to a bounded SORT BUFFER; when the buffer reaches
    ``target_bytes`` it is bucketed by range, each bucket sorted and appended
    as one more sorted RUN file for that range. A range is then produced by a
    k-way merge of its runs, which holds one decoded record batch per run and
    never the range.
    """

    def __init__(self, spill_dir: Path, n_ranges: int, columns: Sequence[str]):
        self._dir = spill_dir
        self._columns = list(columns)
        self._runs: list[list[Path]] = [[] for _ in range(n_ranges)]
        self._seq = 0
        #: Rows currently decoded inside open run readers -- the merge
        #: frontier's REAL residency, measured rather than bounded on paper.
        self._decoded_rows = 0
        #: Merge passes performed, across every range. More than one range's
        #: worth means a range exceeded :data:`_MAX_MERGE_FANIN`.
        self.merge_passes = 0

    def add_run(self, index: int, rows: list[dict[str, Any]]) -> Path:
        """Append one ALREADY-SORTED run of rows for range ``index``."""
        self._seq += 1
        path = self._dir / f"run-{index:06d}-{self._seq:06d}.parquet"
        _write_parquet(rows, self._columns, path)
        self._runs[index].append(path)
        return path

    def runs(self, index: int) -> list[Path]:
        return list(self._runs[index])

    def run_count(self) -> int:
        return sum(len(paths) for paths in self._runs)

    def decoded_rows(self) -> int:
        """Rows held right now in the open run readers' decoded batches."""
        return self._decoded_rows

    def iter_merged(
        self, index: int, key: Callable[[Mapping[str, Any]], tuple]
    ) -> Iterator[dict[str, Any]]:
        """Yield range ``index``'s rows in ``key`` order, merged from its runs.

        Reduces the fan-in to :data:`_MAX_MERGE_FANIN` first if it has to, so
        the final merge opens a bounded number of files and
        :data:`_MERGE_ROW_BUDGET` is divided by a bounded number rather than
        by however many runs the source happened to produce.
        """
        self._reduce_fanin(index, key)
        paths = self._runs[index]
        batch_rows = self._read_batch_rows(len(paths))
        streams = [self._iter_run(path, batch_rows) for path in paths]
        self.merge_passes += 1
        return heapq.merge(*streams, key=key)

    def release(self, index: int) -> None:
        """Delete range ``index``'s run files once it has been merged out."""
        for path in self._runs[index]:
            path.unlink(missing_ok=True)
        self._runs[index] = []

    @staticmethod
    def _read_batch_rows(open_runs: int) -> int:
        """Rows per read batch, so ``open_runs x batch`` stays within budget."""
        if open_runs <= 0:  # pragma: no cover - guarded by the caller
            return _RUN_READ_BATCH_ROWS
        return max(1, min(_RUN_READ_BATCH_ROWS, _MERGE_ROW_BUDGET // open_runs))

    def _reduce_fanin(
        self, index: int, key: Callable[[Mapping[str, Any]], tuple]
    ) -> None:
        """Merge runs in groups until at most :data:`_MAX_MERGE_FANIN` remain.

        Each pass merges a group of runs into ONE new run, streaming -- the
        intermediate is written row by row from the merged iterator, so a pass
        never holds more than its own frontier either. Duplicates are NOT
        dropped here: the winner rule is applied once, on the final merge, so
        an intermediate pass cannot discard a row whose winner has not been
        seen yet.
        """
        while len(self._runs[index]) > _MAX_MERGE_FANIN:
            groups = [
                self._runs[index][i : i + _MAX_MERGE_FANIN]
                for i in range(0, len(self._runs[index]), _MAX_MERGE_FANIN)
            ]
            merged: list[Path] = []
            for group in groups:
                if len(group) == 1:
                    merged.append(group[0])
                    continue
                batch_rows = self._read_batch_rows(len(group))
                streams = [self._iter_run(path, batch_rows) for path in group]
                self.merge_passes += 1
                merged.append(
                    self._write_run_stream(
                        index, heapq.merge(*streams, key=key)
                    )
                )
                for path in group:
                    path.unlink(missing_ok=True)
            self._runs[index] = merged

    def _write_run_stream(
        self, index: int, rows: Iterator[dict[str, Any]]
    ) -> Path:
        """Write a merged row STREAM out as one new run, in bounded chunks."""
        self._seq += 1
        path = self._dir / f"run-{index:06d}-{self._seq:06d}.parquet"
        writer: pq.ParquetWriter | None = None
        chunk: list[dict[str, Any]] = []
        try:
            for row in rows:
                chunk.append(row)
                if len(chunk) >= _RUN_READ_BATCH_ROWS:
                    writer = self._append_chunk(writer, chunk, path)
                    chunk = []
            writer = self._append_chunk(writer, chunk, path)
        finally:
            if writer is not None:
                writer.close()
        if writer is None:  # pragma: no cover - a run is never empty
            _write_parquet([], self._columns, path)
        return path

    def _append_chunk(
        self,
        writer: pq.ParquetWriter | None,
        chunk: list[dict[str, Any]],
        path: Path,
    ) -> pq.ParquetWriter | None:
        """Append one chunk as a row group, opening the writer on the first."""
        if not chunk:
            return writer
        table = pa.Table.from_pylist(
            [{col: row.get(col) for col in self._columns} for row in chunk]
        )
        if writer is None:
            writer = pq.ParquetWriter(str(path), table.schema)
        writer.write_table(table)
        return writer

    def _iter_run(self, path: Path, batch_rows: int) -> Iterator[dict[str, Any]]:
        """Stream one run file back, one bounded record batch at a time.

        Tracks the decoded rows it holds in :meth:`decoded_rows`, so the
        merge's residency is MEASURED. The previous batch's list is released
        as soon as the next is decoded, which is what keeps a reader's
        residency one batch rather than the whole run.
        """
        parquet_file = pq.ParquetFile(str(path))
        held = 0
        try:
            for batch in parquet_file.iter_batches(batch_size=batch_rows):
                rows = batch.to_pylist()
                self._decoded_rows += len(rows) - held
                held = len(rows)
                yield from rows
        finally:
            self._decoded_rows -= held


# --------------------------------------------------------------------------
# Sharding one target table
# --------------------------------------------------------------------------


@dataclass
class TableOutcome:
    """Everything one table's sharding produced, before it is published."""

    reports: list[ShardReport] = field(default_factory=list)
    skips: list[SkipReport] = field(default_factory=list)
    sources: list[SourceOutcome] = field(default_factory=list)
    parameter_set_hashes: set[str] = field(default_factory=set)
    #: ``{parameter_set_hash: canonical_json}`` for every PER-ROW parameter
    #: set this table's rows carried (see
    #: :data:`~clearinghouse_manifest.PARAMETER_SET_COLUMN_KEY`). Empty for a
    #: static ``parameter_set``, whose single row the plan already explains.
    #: ``shard_manifest`` turns these into registry rows -- one per DISTINCT
    #: set -- after the tables are sharded, because a per-row set is not
    #: knowable until the records have been read.
    parameter_sets: dict[str, str] = field(default_factory=dict)


def shard_table(
    work: Sequence[tuple[cm.TablePlan, SourceAdapter]],
    out_root: str | Path,
    *,
    context: ShardRunContext | None = None,
    target_bytes: int = DEFAULT_TARGET_BYTES,
) -> TableOutcome:
    """Shard every plan in ONE unit that targets ONE table, as a single merge.

    WHY THE PLANS ARE MERGED TOGETHER AND NOT SHARDED ONE AT A TIME. The
    winner rule for a result slot (latest ``observed_at`` wins) has to apply
    ACROSS the sources of a unit, not just within one. Two sources in a unit
    that both annotate the same protein with the same tool and parameters are
    writing the same SLOT; sharding them separately writes both rows and
    leaves the superseding to a reader. Merging them here means the unit emits
    one row for the slot and the loser never reaches the lake. Across UNITS
    and across RUNS, first-writer-wins via fill-only still applies -- a later
    unit does not get to retract an earlier unit's published batch.

    The algorithm (see :class:`_RunSpool` for why it is an external sort):

      1. ``N = ceil(sum(estimate_bytes) / target_bytes)`` equal-width hash
         ranges, boundaries COMPUTED from the uniform hash space.
      2. Stream every plan's adapter ONCE into a bounded sort buffer, routing
         each built row to its range. At ``target_bytes`` the buffer is
         bucketed by range, each bucket sorted and spilled as one SORTED RUN.
      3. Per range, k-way merge its runs, drop duplicates (adjacent in the
         merge, because the sort key's prefix IS the duplicate identity), and
         cut output batches as the merged stream goes by.

    Args:
        work: ``(plan, adapter)`` pairs, every plan targeting the SAME table.
        out_root: The bronze output root; ``<out_root>/<table>/`` is created.
        context: The run/unit identity and run ``observed_at``. Built with
            fresh ULIDs when omitted.
        target_bytes: Bounds the SORT BUFFER, and the size at which an output
            batch is cut (at :data:`SKEW_SPLIT_FACTOR` times this, Iceberg's
            own rewrite threshold). It does NOT bound a range.

    Returns:
        A :class:`TableOutcome` carrying the batch reports, per-source skip
        counts and per-source exhaustion.

    Raises:
        ShardOrderError: A written shard is unsorted, or two overlap by more
            than a shared boundary key.
    """
    if not work:
        return TableOutcome()
    context = context or ShardRunContext.build(target_bytes=target_bytes)
    target_bytes = context.target_bytes
    plans = [plan for plan, _ in work]
    table = plans[0].table
    if any(plan.table != table for plan in plans):
        raise ValueError(
            "shard_table() merges the plans of ONE table; got "
            f"{sorted({plan.table for plan in plans})}."
        )
    kind = plans[0].kind
    entity_type = plans[0].entity_type
    spec = _key_spec(entity_type, kind)
    sort_key = _make_sort_key(spec)
    dedup_key = _make_dedup_key(spec)
    metrics = context.metrics

    total_bytes = max(1, sum(max(1, adapter.estimate_bytes()) for _, adapter in work))
    n = max(1, math.ceil(total_bytes / target_bytes))
    boundaries = _range_boundaries(n)
    table_dir = Path(out_root) / table
    table_dir.mkdir(parents=True, exist_ok=True)
    out_columns = list(schema.column_names(entity_type, kind))
    # The spill carries the output columns MINUS ingest_batch_id (not known
    # until a batch is cut) PLUS the contributing source name, which is what
    # lets a mixed batch report every source that fed it.
    spill_columns = [col for col in out_columns if col != "ingest_batch_id"]
    spill_columns.append(_ROW_SOURCE_KEY)

    outcome = TableOutcome()
    spill_dir = Path(tempfile.mkdtemp(prefix=f"clh-spill-{table}-"))
    try:
        spool = _RunSpool(spill_dir, n, spill_columns)
        buffer: list[dict[str, Any]] = []
        buffer_bytes = 0
        skip_counts: dict[tuple[str, str], int] = {}

        for plan, adapter in work:
            emitted = accepted = 0
            for record in adapter.iter_records():
                emitted += 1
                try:
                    out_row = _build_row(
                        plan,
                        record,
                        run_observed_at=context.observed_at,
                        parameter_sets=outcome.parameter_sets,
                    )
                except ShardValueError as exc:
                    if isinstance(exc, ShardParameterSetError):
                        reason = SKIP_PARAMETER_SET
                    elif "observed_at" in str(exc):
                        reason = SKIP_NULL_OBSERVED_AT
                    else:
                        reason = SKIP_SHARD_VALUE_ERROR
                    key = (plan.source_name, reason)
                    skip_counts[key] = skip_counts.get(key, 0) + 1
                    continue
                accepted += 1
                if plan.kind == "result" and out_row.get("parameter_set_hash"):
                    outcome.parameter_set_hashes.add(out_row["parameter_set_hash"])
                out_row[_ROW_SOURCE_KEY] = plan.source_name
                index = _range_index(out_row[spec.key_column], boundaries, n)
                out_row[_ROW_RANGE_KEY] = index
                buffer.append(out_row)
                buffer_bytes += _row_bytes(out_row)
                if metrics is not None:
                    metrics.rows_per_range[index] = (
                        metrics.rows_per_range.get(index, 0) + 1
                    )
                    if len(buffer) > metrics.max_sort_buffer_rows:
                        metrics.max_sort_buffer_rows = len(buffer)
                    metrics.note_resident(len(buffer))
                if buffer_bytes >= target_bytes:
                    _spill_buffer(buffer, spool, sort_key, metrics)
                    buffer_bytes = 0
            # Adapter-reported skips are merged with the sharder's own, so a
            # source's shortfall is one number with one list of reasons.
            for reason, count in adapter.skipped().items():
                key = (plan.source_name, reason)
                skip_counts[key] = skip_counts.get(key, 0) + count
            outcome.sources.append(
                SourceOutcome(
                    source=plan.source_name,
                    table=table,
                    emitted=emitted,
                    accepted=accepted,
                    exhausted=adapter.exhausted(),
                    watermark=adapter.watermark(),
                    incomplete_reasons=tuple(adapter.incomplete_reasons()),
                    checkpoint_candidates=tuple(adapter.checkpoint_candidates()),
                )
            )
            if metrics is not None:
                metrics.rows_emitted += emitted
                metrics.rows_accepted += accepted

        _spill_buffer(buffer, spool, sort_key, metrics)
        if metrics is not None:
            metrics.ranges += n
            metrics.sorted_runs += spool.run_count()
            metrics.spill_dir_bytes = max(
                metrics.spill_dir_bytes,
                sum(p.stat().st_size for p in spill_dir.glob("*.parquet")),
            )

        outcome.reports = _merge_ranges(
            spool,
            n,
            table=table,
            table_dir=table_dir,
            out_columns=out_columns,
            spec=spec,
            sort_key=sort_key,
            dedup_key=dedup_key,
            context=context,
        )
        outcome.skips = [
            SkipReport(source=source, reason=reason, count=count)
            for (source, reason), count in sorted(skip_counts.items())
        ]
        if metrics is not None:
            metrics.merge_passes += spool.merge_passes
    finally:
        shutil.rmtree(spill_dir, ignore_errors=True)

    _assert_sorted_and_disjoint(outcome.reports, table, spec.key_column)
    return outcome


def _spill_buffer(
    buffer: list[dict[str, Any]],
    spool: _RunSpool,
    sort_key: Callable[[Mapping[str, Any]], tuple],
    metrics: ShardMetrics | None,
) -> None:
    """Bucket the sort buffer by range, sort each bucket, spill it as a run.

    The buffer is bucketed BEFORE it is sorted, so each bucket is sorted on
    its own: that is the same total work as one global sort followed by a
    partition, without assuming the range index is monotonic in the sort key
    (it is, but a correctness-critical partition should not rest on that).
    """
    if not buffer:
        return
    by_range: dict[int, list[dict[str, Any]]] = {}
    for row in buffer:
        by_range.setdefault(row.pop(_ROW_RANGE_KEY), []).append(row)
    for index in sorted(by_range):
        rows = by_range[index]
        rows.sort(key=sort_key)
        spool.add_run(index, rows)
    buffer.clear()


def _merge_ranges(
    spool: _RunSpool,
    n: int,
    *,
    table: str,
    table_dir: Path,
    out_columns: list[str],
    spec: _KeySpec,
    sort_key: Callable[[Mapping[str, Any]], tuple],
    dedup_key: Callable[[Mapping[str, Any]], tuple],
    context: ShardRunContext,
) -> list[ShardReport]:
    """Merge each range's sorted runs in turn, writing deduplicated batches.

    ONE RANGE AT A TIME, and within a range one merged row at a time: the only
    rows resident are the merge frontier (one decoded record batch per run)
    and the output batch being accumulated. A range is never materialised,
    which is the point -- see :class:`_RunSpool`.

    Duplicates are dropped across the WHOLE merge of a range, not per batch:
    ``last_key`` deliberately survives a batch cut, so a row whose winner was
    the final row of the previous batch is still recognised as a duplicate.
    The winner is the FIRST row in sort order, which for a result table means
    the latest ``observed_at`` (the sort key orders ``observed_at``
    descending) -- matching the ORDER BY the current-state view applies.
    """
    cut_bytes = max(1, int(SKEW_SPLIT_FACTOR * context.target_bytes))
    metrics = context.metrics
    reports: list[ShardReport] = []
    for index in range(n):
        if not spool.runs(index):
            continue
        range_batches: list[ShardReport] = []
        batch: list[dict[str, Any]] = []
        batch_bytes = 0
        batch_sources: set[str] = set()
        dropped = 0
        last_key: tuple | None = None
        for row in spool.iter_merged(index, sort_key):
            key = dedup_key(row)
            if key == last_key:
                dropped += 1
                continue
            last_key = key
            batch_sources.add(row.pop(_ROW_SOURCE_KEY, "") or "")
            batch.append(row)
            batch_bytes += _row_bytes(row)
            if metrics is not None:
                if len(batch) > metrics.max_batch_buffer_rows:
                    metrics.max_batch_buffer_rows = len(batch)
                metrics.note_resident(len(batch) + spool.decoded_rows())
            if batch_bytes >= cut_bytes:
                range_batches.append(
                    _write_batch(
                        batch,
                        table=table,
                        table_dir=table_dir,
                        out_columns=out_columns,
                        spec=spec,
                        context=context,
                        sources=batch_sources,
                        dropped=dropped,
                    )
                )
                batch, batch_bytes, batch_sources, dropped = [], 0, set(), 0
        if batch:
            range_batches.append(
                _write_batch(
                    batch,
                    table=table,
                    table_dir=table_dir,
                    out_columns=out_columns,
                    spec=spec,
                    context=context,
                    sources=batch_sources,
                    dropped=dropped,
                )
            )
        elif dropped and range_batches:
            # The merge ended on a run of duplicates whose WINNER was already
            # written, so there is no batch left to attribute them to. They
            # belong to the last batch written, not to nobody: the per-batch
            # rows_deduplicated counts must sum to the rows actually dropped,
            # or run.json understates the drop and the shortfall looks like
            # lost data.
            last = range_batches[-1]
            range_batches[-1] = replace(
                last, rows_deduplicated=last.rows_deduplicated + dropped
            )
        spool.release(index)
        if metrics is not None:
            metrics.rows_written += sum(r.rows for r in range_batches)
            metrics.rows_deduplicated += sum(
                r.rows_deduplicated for r in range_batches
            )
        if len(range_batches) > 1:
            # Several batches cut from one range: report every one of them as
            # a split, which is what the skew guard used to report.
            range_batches = [
                replace(report, split=True) for report in range_batches
            ]
        reports.extend(range_batches)
    return reports


def _write_batch(
    rows: list[dict[str, Any]],
    *,
    table: str,
    table_dir: Path,
    out_columns: list[str],
    spec: _KeySpec,
    context: ShardRunContext,
    sources: set[str],
    dropped: int,
) -> ShardReport:
    """Mint this batch's ULID, stamp it on every row, write and hash the file.

    The batch id IS the file stem, so a file on disk names the
    ``ingest_batch_id`` of every row inside it without opening it, and two
    sources -- or two slices, or two runs -- can never collide on a name. The
    old scheme named files ``batch-<NNNN>`` from a counter that restarted at
    zero on every call, so two sources feeding one table each wrote
    ``batch-0000.parquet`` and the second silently destroyed the first.
    """
    batch_id = new_ulid()
    for row in rows:
        row["ingest_batch_id"] = batch_id
    path = table_dir / f"{batch_id}.parquet"
    _write_parquet(rows, out_columns, path)
    return ShardReport(
        table=table,
        batch_id=batch_id,
        path=path,
        rows=len(rows),
        hash_min=rows[0][spec.key_column],
        hash_max=rows[-1][spec.key_column],
        rows_deduplicated=dropped,
        sha256=_file_sha256(path),
        sources=tuple(sorted(s for s in sources if s)),
        key_column=spec.key_column,
        slice=context.slice,
        unit_id=context.unit_id,
        attempt_id=context.attempt_id,
    )


def shard_source(
    plan: cm.TablePlan,
    adapter: SourceAdapter,
    out_root: Path,
    *,
    target_bytes: int = DEFAULT_TARGET_BYTES,
    run_id: str | None = None,
    slice_label: str | None = None,
    unit_id: str | None = None,
    attempt_id: str | None = None,
    observed_at: datetime | None = None,
    metrics: ShardMetrics | None = None,
) -> list[ShardReport]:
    """Shard ONE source's records for ONE target table -- a single-plan unit.

    Thin over :func:`shard_table`, which is where the work and the
    documentation are. This remains the entry point for a caller fanning the
    sharder out over independent sources; note that when two plans of the SAME
    unit target the SAME table they must go through :func:`shard_table`
    together, or the cross-source winner rule does not apply between them.

    Args:
        plan: The validated :class:`~clearinghouse_manifest.TablePlan`.
        adapter: The source adapter for ``plan``'s source.
        out_root: The bronze output root; ``<out_root>/<table>/`` is created.
        target_bytes: Bounds the sort buffer and the output batch size.
        run_id: The run this belongs to; a fresh ULID when omitted.
        slice_label: The slice label recorded on every batch, or ``None``.
        unit_id: The unit this belongs to; a fresh ULID when omitted.
        attempt_id: This unit's attempt; a fresh ULID when omitted.
        observed_at: The run start time stamped on rows whose source declares
            no ``observed_at_column``. Defaults to now, UTC.
        metrics: Optional :class:`ShardMetrics` to instrument the run.

    Returns:
        One :class:`ShardReport` per written shard, in ascending range order.
    """
    context = ShardRunContext.build(
        run_id=run_id,
        unit_id=unit_id,
        attempt_id=attempt_id,
        slice_label=slice_label,
        observed_at=observed_at,
        target_bytes=target_bytes,
        metrics=metrics,
    )
    return shard_table([(plan, adapter)], out_root, context=context).reports


def _assert_sorted_and_disjoint(
    reports: list[ShardReport], table: str, key_column: str = "entity_hash"
) -> None:
    """Fail the build if any shard is unsorted or two ranges truly overlap.

    Enforces the load-bearing invariant rather than trusting it: within each
    shard the parquet is read back and checked ascending, and across shards
    each range's min must be >= the previous range's max.

    WHY ``>=`` AND NOT ``>``. A result table's batches are cut mid-merge and
    its sort key is the five-column SLOT key, not ``entity_hash`` alone -- one
    hash can carry several slots (two tools, two parameter sets, two sources).
    A cut can therefore fall between two slots of the SAME hash, leaving
    adjacent batches that touch at one shared boundary value. That is sorted
    and it is not an overlap of ranges; an Iceberg point probe on that hash
    reads two files instead of one, which is the correct and bounded cost. A
    strict ``>`` would fail the build on a legitimate cut.
    """
    prev_max: str | None = None
    for report in reports:
        table_data = pq.read_table(str(report.path), columns=[key_column])
        hashes = table_data.column(key_column).to_pylist()
        if hashes != sorted(hashes):
            raise ShardOrderError(
                f"table {table!r} shard {report.batch_id!r}: {key_column} "
                "column is not ascending -- the sort invariant is violated and "
                "the file's min/max stats would prune nothing."
            )
        if hashes:
            if hashes[0] != report.hash_min or hashes[-1] != report.hash_max:
                raise ShardOrderError(
                    f"table {table!r} shard {report.batch_id!r}: reported "
                    "range does not match the written data."
                )
            if prev_max is not None and hashes[0] < prev_max:
                raise ShardOrderError(
                    f"table {table!r} shard {report.batch_id!r}: {key_column} "
                    f"range [{hashes[0]}..] overlaps the previous shard ending "
                    f"at {prev_max} by more than a shared boundary value -- "
                    "shard ranges must be sorted and non-overlapping."
                )
            prev_max = hashes[-1]


# --------------------------------------------------------------------------
# The parameter-set registry
# --------------------------------------------------------------------------


def discovered_parameter_sets(out_root: str | Path) -> set[str]:
    """Every ``parameter_set_hash`` already committed to the registry on disk.

    Read from the registry's own IMMUTABLE batch files rather than from
    ``run.json``, because the files are the thing a later ingest will load and
    they cannot drift from themselves. A unit emits only the sets NOT in this
    set, so a set discovered by an earlier unit is never re-emitted.
    """
    registry_dir = Path(out_root) / schema.PARAMETER_SET_TABLE
    known: set[str] = set()
    if not registry_dir.is_dir():
        return known
    for path in sorted(registry_dir.glob("*.parquet")):
        column = pq.read_table(str(path), columns=["parameter_set_hash"])
        known.update(column.column("parameter_set_hash").to_pylist())
    return known


def write_parameter_set_registry_shard(
    plans: Sequence[cm.TablePlan],
    out_root: str | Path,
    *,
    context: ShardRunContext | None = None,
    already_known: set[str] | None = None,
) -> ShardReport | None:
    """Write THIS UNIT's parameter-set registry batch, or ``None`` if empty.

    One row per parameter set this unit is the FIRST to discover: the hash and
    its ``canonical_json`` -- the text that says what the hash MEANS. A set an
    earlier unit already registered is not re-emitted, because registry
    batches are IMMUTABLE and the earlier batch already explains it (and must
    be committed before this unit's results load; the ingest side enforces
    that ordering). "One registry shard per run" means one logical TABLE,
    which may hold many immutable batch files.

    THE REGISTRY HAS NO ``entity_hash``. It is keyed, sorted, deduplicated and
    range-bounded on ``parameter_set_hash``, and the batch's
    ``key_column`` records which column its bounds refer to -- a consumer that
    assumed ``entity_hash`` would read the bounds of a column that does not
    exist in the table.

    The hash and canonical text are NOT computed here: they come from the plan,
    which computed them once through
    :mod:`kbutillib.domains.identity`'s ``canonical_parameter_set`` /
    ``parameter_set_hash`` at plan time. A second implementation of the rule is
    a second answer to "what is this parameter set", which is the one thing the
    registry exists to prevent.

    Args:
        plans: The unit's resolved plans. Non-``result`` plans are ignored.
        out_root: The bronze output root, as passed to :func:`shard_table`.
        context: Run/unit identity and the run ``observed_at``.
        already_known: Hashes an earlier unit registered. Discovered from the
            registry files under ``out_root`` when omitted.

    Returns:
        The :class:`ShardReport` for the written batch, or ``None`` when this
        unit discovers no new parameter set (including a unit with no
        ``result`` plan at all, which creates no registry directory).
    """
    context = context or ShardRunContext.build()
    if already_known is None:
        already_known = discovered_parameter_sets(out_root)

    # Insertion-ordered dedupe by hash: the first result plan to use a set
    # supplies its row.
    rows_by_hash: dict[str, dict[str, Any]] = {}
    for plan in plans:
        if plan.kind != "result" or plan.parameter_set_hash is None:
            continue
        digest = plan.parameter_set_hash
        if digest in rows_by_hash or digest in already_known:
            continue
        rows_by_hash[digest] = {
            "parameter_set_hash": digest,
            "canonical_json": plan.parameter_set_json,
            "observed_at": context.observed_at,
            _ROW_SOURCE_KEY: plan.source_name,
        }
    return _write_registry_batch(rows_by_hash, out_root, context=context)


def _write_registry_batch(
    rows_by_hash: Mapping[str, dict[str, Any]],
    out_root: str | Path,
    *,
    context: ShardRunContext,
) -> ShardReport | None:
    """Write one registry batch from ready rows, or ``None`` when there are
    none.

    The shared tail of both registry writers -- the plan-time one
    (:func:`write_parameter_set_registry_shard`) and the per-row one
    (:func:`write_discovered_parameter_set_shard`) -- so the registry's key
    column, sort order, column list and disjointness assertion have exactly
    one implementation regardless of where the rows came from.
    """
    if not rows_by_hash:
        return None
    spec = _key_spec(None, "registry")
    rows = [dict(rows_by_hash[key]) for key in sorted(rows_by_hash)]
    sources = {row.pop(_ROW_SOURCE_KEY) for row in rows}
    table_dir = Path(out_root) / schema.PARAMETER_SET_TABLE
    table_dir.mkdir(parents=True, exist_ok=True)
    report = _write_batch(
        rows,
        table=schema.PARAMETER_SET_TABLE,
        table_dir=table_dir,
        out_columns=list(schema.parameter_set_column_names()),
        spec=spec,
        context=context,
        sources=sources,
        dropped=0,
    )
    _assert_sorted_and_disjoint([report], schema.PARAMETER_SET_TABLE, spec.key_column)
    return report


def write_discovered_parameter_set_shard(
    discovered: Mapping[str, str],
    out_root: str | Path,
    *,
    context: ShardRunContext,
    already_known: set[str],
    source_name: str,
) -> ShardReport | None:
    """Write the registry batch for PER-ROW parameter sets, or ``None``.

    One row per DISTINCT set the unit's records carried -- exactly what
    :func:`write_parameter_set_registry_shard` does for the static form, but
    necessarily AFTER the result tables are sharded rather than before,
    because a per-row set does not exist until its record has been read. An
    arc annotation source feeding two TRANSYT taxa and four other tools
    therefore registers three sets: one per taxon, plus the ``{}`` the other
    four share.

    WRITING IT SECOND DOES NOT LOAD IT SECOND, which is the only thing the
    ordering rule actually requires. The ingest side discovers tables from
    the output directory and puts
    :data:`~clearinghouse_schema.PARAMETER_SET_TABLE` FIRST unconditionally
    (``clearinghouse_backfill._target_tables``), so a registry batch written
    last is still ingested before any ``<type>_result`` batch. The orphan
    check in :func:`shard_manifest` then runs after this call, so a result
    row whose hash reached neither registry batch still fails the unit before
    it is published.

    Args:
        discovered: ``{parameter_set_hash: canonical_json}``, as collected by
            :func:`_row_parameter_set` during sharding.
        out_root: The bronze output root.
        context: Run/unit identity and the run ``observed_at``.
        already_known: Hashes an earlier unit -- or this unit's plan-time
            batch -- already registered. Never re-emitted: registry batches
            are immutable and the earlier row already explains the hash.
        source_name: Recorded as the batch's contributing source.
    """
    rows_by_hash = {
        digest: {
            "parameter_set_hash": digest,
            "canonical_json": canonical,
            "observed_at": context.observed_at,
            _ROW_SOURCE_KEY: source_name,
        }
        for digest, canonical in sorted(discovered.items())
        if digest not in already_known
    }
    return _write_registry_batch(rows_by_hash, out_root, context=context)


# --------------------------------------------------------------------------
# The run inventory (run.json)
# --------------------------------------------------------------------------


def run_manifest_path(out_root: str | Path) -> Path:
    """Where the run inventory lives for a bronze output root."""
    return Path(out_root) / RUN_MANIFEST_FILE


def read_run_manifest(out_root: str | Path) -> dict[str, Any]:
    """Read ``<out>/run.json``, or ``{}`` when no unit has published yet."""
    path = run_manifest_path(out_root)
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def unit_ids(run: Mapping[str, Any]) -> list[str]:
    """The unit ids a run inventory holds, in publication order.

    The UNIT SELECTOR: ingest and verification address a run one unit at a
    time (a unit's batches are published atomically and never rewritten, so a
    unit is the smallest thing that is certainly complete), and this plus
    :func:`batches_for_unit` is how they name one.
    """
    return [str(unit["unit_id"]) for unit in run.get("units", [])]


def arc_checkpoint_candidates(run: Mapping[str, Any]) -> list[str]:
    """The checkpoint candidate keys a run inventory records, deduplicated.

    Read from the inventory of ONE unit directory, which is what scopes the
    answer: the driver shards each unit into its own ``<out>/units/<unit>``,
    so every source entry in that ``run.json`` belongs to that unit. The
    four sources of an arc preset read one store with one identity and so
    report the SAME key; order is preserved and repeats are dropped.

    Empty for every manifest with no arc source, and for an inventory written
    before this field existed -- which is the correct answer in both cases:
    nothing to promote.
    """
    keys: list[str] = []
    for outcome in run.get("sources") or []:
        for key in outcome.get(ARC_CHECKPOINT_CANDIDATES_FIELD) or []:
            text = str(key)
            if text and text not in keys:
                keys.append(text)
    return keys


def batches_for_unit(run: Mapping[str, Any], unit_id: str) -> list[dict[str, Any]]:
    """Every batch entry one unit published, in the order it published them."""
    return [
        dict(batch)
        for batch in run.get("batches", [])
        if batch.get("unit_id") == unit_id
    ]


def _batch_entry(report: ShardReport) -> dict[str, Any]:
    """One batch's entry in the run inventory, relative to the output root."""
    return {
        "table": report.table,
        "batch_id": report.batch_id,
        "file": f"{report.table}/{report.path.name}",
        "rows": report.rows,
        "rows_deduplicated": report.rows_deduplicated,
        "key_column": report.key_column,
        "hash_min": report.hash_min,
        "hash_max": report.hash_max,
        "sha256": report.sha256,
        "source": list(report.sources),
        "slice": report.slice,
        "unit_id": report.unit_id,
        "attempt_id": report.attempt_id,
    }


def publish_unit(
    out_root: str | Path,
    *,
    context: ShardRunContext,
    reports: Sequence[ShardReport],
    skips: Sequence[SkipReport],
    sources: Sequence[SourceOutcome],
    manifest_name: str | None = None,
    manifest_sha256: str = "",
) -> Path:
    """Publish ONE unit's batches into ``<out>/run.json``, atomically.

    ``run.json`` is an APPEND-ONLY INVENTORY of the whole run. A unit's
    batches appear all at once, when that unit has finished sharding, and a
    published entry is never rewritten or removed -- which is what makes the
    file safe to read while a run is still going: anything in it is final. The
    run-level header (``run_id``, ``created_at``, ``manifest_sha256`` ...) is
    written by the first unit to publish and preserved verbatim after that, so
    re-reading and re-serialising the file leaves every earlier unit's entry
    byte-identical.

    Atomic means temp file plus :func:`os.replace`, never an in-place
    rewrite: a reader concurrent with a publish sees the old inventory or the
    new one, and a crash mid-publish leaves the old one intact rather than a
    truncated inventory naming batches that may or may not exist.
    """
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    existing = read_run_manifest(out_root)
    if existing:
        run = dict(existing)
    else:
        run = {
            "run_manifest_version": RUN_MANIFEST_VERSION,
            "run_id": context.run_id,
            "created_at": _iso_z(utc_now()),
            "kbutillib_version": _kbutillib_version(),
            "standardizer_version": standardizers.STANDARDIZER_VERSION,
            "observed_at": _iso_z(context.observed_at),
            "manifest_name": manifest_name,
            "manifest_sha256": manifest_sha256,
            "slices": [],
            "units": [],
            "batches": [],
            "skipped": [],
            "sources": [],
        }
    if context.slice is not None and context.slice not in run["slices"]:
        run["slices"] = [*run["slices"], context.slice]
    run["units"] = [
        *run["units"],
        {
            "unit_id": context.unit_id,
            "attempt_id": context.attempt_id,
            "slice": context.slice,
            "published_at": _iso_z(utc_now()),
        },
    ]
    run["batches"] = [*run["batches"], *(_batch_entry(r) for r in reports)]
    run["skipped"] = [
        *run["skipped"],
        *(
            {
                "source": skip.source,
                "reason": skip.reason,
                "count": skip.count,
                "unit_id": context.unit_id,
            }
            for skip in skips
        ),
    ]
    run["sources"] = [
        *run["sources"],
        *(
            {
                "source": outcome.source,
                "table": outcome.table,
                "emitted": outcome.emitted,
                "accepted": outcome.accepted,
                "exhausted": outcome.exhausted,
                "watermark": outcome.watermark,
                "incomplete_reasons": list(outcome.incomplete_reasons),
                ARC_CHECKPOINT_CANDIDATES_FIELD: list(
                    outcome.checkpoint_candidates
                ),
                "unit_id": context.unit_id,
            }
            for outcome in sources
        ),
    ]

    path = run_manifest_path(out_root)
    tmp = path.with_name(f".{path.name}.{context.unit_id}.tmp")
    tmp.write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def _kbutillib_version() -> str:
    """The installed kbutillib version, or ``"unknown"`` -- never a raise.

    The run inventory is provenance; failing a 264M-row shard run because a
    version string could not be read would be absurd.
    """
    try:
        # Four dots: this module is kbutillib.domains.kbase.berdl.*, so the
        # package root is four levels up (``...`` is kbutillib.domains, which
        # has no __version__ and silently produced "unknown").
        from .... import __version__  # noqa: PLC0415

        return str(__version__)
    except Exception:  # pragma: no cover - defensive
        return "unknown"


def canonical_manifest_text(manifest: cm.Manifest) -> str:
    """Render a PARSED manifest as deterministic TOML text, for hashing.

    ``run.json`` records ``manifest_sha256`` so a run can be tied to the
    manifest that produced it. When the caller has the FILE, the hash is of
    its bytes. When the manifest was built in memory there are no bytes, so
    this renders the parsed object with keys in sorted order -- the same
    manifest always produces the same text, which is all a provenance hash
    needs. It is a canonical RENDERING for hashing, not a round-trippable
    serialiser: it emits what :class:`~clearinghouse_manifest.Source` carries,
    which is the manifest as the sharder actually read it.
    """
    lines: list[str] = []
    for source in manifest.sources:
        lines.append("[[source]]")
        scalars: dict[str, Any] = {
            "name": source.name,
            "adapter": source.adapter,
            "entity_type": source.entity_type,
            "kinds": list(source.kinds),
        }
        if source.observed_at_column is not None:
            scalars["observed_at_column"] = source.observed_at_column
        scalars.update(source.locator)
        for key in sorted(scalars):
            lines.append(f"{key} = {_toml_value(scalars[key])}")
        for block, mapping in (
            ("hash", source.hash_spec),
            ("content", source.content),
            ("result", source.result),
        ):
            if not mapping:
                continue
            lines.append(f"[source.{block}]")
            for key in sorted(mapping):
                lines.append(f"{key} = {_toml_value(mapping[key])}")
    return "\n".join(lines) + "\n"


def _toml_value(value: Any) -> str:
    """Render one value as deterministic TOML-ish text (sorted keys throughout)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, Mapping):
        inner = ", ".join(
            f"{key} = {_toml_value(value[key])}" for key in sorted(value)
        )
        return "{" + inner + "}"
    return json.dumps(str(value))


def manifest_identity(
    manifest: cm.Manifest, manifest_path: str | Path | None
) -> tuple[str | None, str]:
    """Return ``(manifest_name, manifest_sha256)`` for the run inventory."""
    if manifest_path is not None:
        path = Path(manifest_path)
        if path.is_file():
            return path.name, _file_sha256(path)
    digest = hashlib.sha256(
        canonical_manifest_text(manifest).encode("utf-8")
    ).hexdigest()
    return None, digest


# --------------------------------------------------------------------------
# Sharding a whole manifest: one UNIT
# --------------------------------------------------------------------------


def shard_manifest(
    manifest: cm.Manifest,
    out_root: str | Path,
    *,
    target_bytes: int = DEFAULT_TARGET_BYTES,
    run_id: str | None = None,
    slice_label: str | None = None,
    unit_id: str | None = None,
    attempt_id: str | None = None,
    manifest_path: str | Path | None = None,
    observed_at: datetime | None = None,
    metrics: ShardMetrics | None = None,
    run_context: Any = None,
    adapter_unit: str | None = None,
) -> list[ShardReport]:
    """Validate a manifest and shard every source it names as ONE UNIT.

    Convenience over :func:`clearinghouse_manifest.shard_plan` +
    :func:`shard_table`: validates the whole manifest at plan time (so a bad
    manifest fails BEFORE any file is written), then shards the resolved
    plans. One call is one UNIT of a run: pass ``run_id`` and ``slice_label``
    (and optionally ``unit_id``) to make several calls parts of one run, as a
    slice-driving CLI will.

    PLANS TARGETING ONE TABLE ARE SHARDED TOGETHER, in a single external
    merge, so the result winner rule and the entity/content first-row rule
    apply ACROSS the unit's sources rather than only within each one. (The
    driver used to loop per plan, which is why two sources feeding one table
    could not be reconciled -- and, with the old per-call batch counter, why
    the second silently overwrote the first's ``batch-0000.parquet``.)

    THE REGISTRY BATCH IS WRITTEN FIRST. Before any result shard, this emits a
    batch for :data:`~clearinghouse_schema.PARAMETER_SET_TABLE` holding the
    parameter sets THIS unit is the first to discover (see
    :func:`write_parameter_set_registry_shard`), so no result row is ever
    loadable before the registry row explaining its hash. A unit that
    discovers no new set writes no registry batch.

    A SOURCE DECLARING ``parameter_set_column`` ADDS A SECOND REGISTRY BATCH,
    necessarily written AFTER its result tables: a per-row parameter set does
    not exist until the record carrying it has been read, so there is nothing
    to register before the read. It is ordered FIRST in the returned reports
    all the same, and the ingest side puts the registry table first
    unconditionally, so "the registry loads before the results" still holds
    (see :func:`write_discovered_parameter_set_shard`). The orphan check below
    runs after BOTH batches and is what makes the claim checked rather than
    assumed.

    THE UNIT IS THEN PUBLISHED to ``<out_root>/run.json`` atomically, as one
    append (see :func:`publish_unit`). Nothing is published if anything raised.

    Args:
        manifest: The parsed manifest.
        out_root: The bronze output root.
        target_bytes: Bounds the sort buffer and the output batch size.
        run_id: The run id; a fresh ULID when omitted.
        slice_label: Recorded on every batch this call writes, or ``None``.
        unit_id: This unit's id; a fresh ULID when omitted.
        attempt_id: This unit's attempt id; a fresh ULID when omitted.
        manifest_path: The manifest's file path, when there is one, so
            ``manifest_sha256`` is of the real bytes.
        observed_at: The run start time stamped on rows whose source declares
            no ``observed_at_column``. Defaults to now, UTC.
        metrics: Optional :class:`ShardMetrics` to instrument the run.
        run_context: The backfill run context bound to every adapter this call
            constructs (see :meth:`SourceAdapter.bind_run_context`), or
            ``None`` -- the plain-``shard`` case, where there is no backfill
            run, no ``--limit`` budget and nothing durable to persist.
        adapter_unit: A unit label every adapter's read is restricted to (see
            :meth:`SourceAdapter.restrict_to_unit`), or ``None``. This is the
            UNIT OF WORK label, distinct from ``unit_id`` (the ULID naming
            this call's publication in ``run.json``).

    Returns:
        Every :class:`ShardReport` from every table -- the registry batch (if
        any) first, then the per-table batches in plan order.

    Raises:
        ShardRegistryError: A result row carries a ``parameter_set_hash`` that
            neither this unit's registry batch nor an earlier unit's explains.
    """
    out_root = Path(out_root)
    plans = cm.shard_plan(manifest)
    context = ShardRunContext.build(
        run_id=run_id,
        unit_id=unit_id,
        attempt_id=attempt_id,
        slice_label=slice_label,
        observed_at=observed_at,
        target_bytes=target_bytes,
        metrics=metrics,
    )

    already_known = discovered_parameter_sets(out_root)
    reports: list[ShardReport] = []
    registry = write_parameter_set_registry_shard(
        plans, out_root, context=context, already_known=already_known
    )
    registered = set(already_known)
    if registry is not None:
        reports.append(registry)
        registered.update(
            pq.read_table(str(registry.path), columns=["parameter_set_hash"])
            .column("parameter_set_hash")
            .to_pylist()
        )

    skips: list[SkipReport] = []
    sources: list[SourceOutcome] = []
    used_parameter_sets: set[str] = set()
    discovered: dict[str, str] = {}
    table_reports: list[ShardReport] = []
    for table, work in _group_plans_by_table(
        manifest, plans, run_context=run_context, adapter_unit=adapter_unit
    ):
        outcome = shard_table(work, out_root, context=context)
        table_reports.extend(outcome.reports)
        skips.extend(outcome.skips)
        sources.extend(outcome.sources)
        used_parameter_sets |= outcome.parameter_set_hashes
        for digest, canonical in outcome.parameter_sets.items():
            discovered.setdefault(digest, canonical)

    # The PER-ROW registry batch, for sets that did not exist at plan time
    # (parameter_set_column). Written after the tables but ordered FIRST in
    # the reports, so run.json lists the registry ahead of the result batches
    # it explains -- and ingested first regardless, because the ingest side
    # puts the registry table first unconditionally.
    if discovered:
        per_row = write_discovered_parameter_set_shard(
            discovered,
            out_root,
            context=context,
            already_known=registered,
            source_name=_PER_ROW_REGISTRY_SOURCE,
        )
        if per_row is not None:
            reports.append(per_row)
            registered.update(
                pq.read_table(str(per_row.path), columns=["parameter_set_hash"])
                .column("parameter_set_hash")
                .to_pylist()
            )
    reports.extend(table_reports)

    orphans = sorted(used_parameter_sets - registered)
    if orphans:
        raise ShardRegistryError(
            f"{len(orphans)} parameter_set_hash value(s) on result rows have no "
            f"registry row explaining them: {orphans[:3]}. Every result row's "
            "parameters must be nameable, which is why the registry batch is "
            "written and ingested first."
        )

    manifest_name, manifest_sha256 = manifest_identity(manifest, manifest_path)
    publish_unit(
        out_root,
        context=context,
        reports=reports,
        skips=skips,
        sources=sources,
        manifest_name=manifest_name,
        manifest_sha256=manifest_sha256,
    )
    return reports


def _group_plans_by_table(
    manifest: cm.Manifest,
    plans: Sequence[cm.TablePlan],
    *,
    run_context: Any = None,
    adapter_unit: str | None = None,
) -> list[tuple[str, list[tuple[cm.TablePlan, SourceAdapter]]]]:
    """Group a unit's plans by target table, in first-appearance order.

    One adapter per SOURCE, not per plan: a source with
    ``kinds = ["entity", "content"]`` resolves to two plans targeting two
    tables, and they are sharded separately -- so each gets its own adapter
    instance and its own read of the source, exactly as before.
    """
    grouped: dict[str, list[tuple[cm.TablePlan, SourceAdapter]]] = {}
    order: list[str] = []
    for plan in plans:
        source = _source_for_plan(manifest, plan)
        adapter = get_adapter(
            source,
            run_context=run_context,
            unit=adapter_unit,
            backfill=manifest.backfill,
        )
        if plan.table not in grouped:
            grouped[plan.table] = []
            order.append(plan.table)
        grouped[plan.table].append((plan, adapter))
    return [(table, grouped[table]) for table in order]


def _source_for_plan(manifest: cm.Manifest, plan: cm.TablePlan) -> cm.Source:
    for source in manifest.sources:
        if source.name == plan.source_name:
            return source
    raise ValueError(  # pragma: no cover - plans always come from this manifest
        f"no source named {plan.source_name!r} in manifest."
    )
