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

import hashlib
import heapq
import json
import math
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from ...identity import standardizers
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


#: Registry of adapter implementations by manifest ``adapter`` name. All three
#: names are DECLARED here so a manifest can be validated against the full set
#: of adapter names (the mapping half must be adapter-independent). All three
#: are now registered with real classes (see :data:`_ADAPTER_CLASSES`);
#: :func:`get_adapter` still distinguishes "not a real adapter name" from a
#: declared name with no class, but the latter no longer occurs for any of
#: these three.
ADAPTER_NAMES: tuple[str, ...] = ("file", "mongo", "lakehouse")

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

    A single manifest source names ONE collection. The join across collections
    is expressed as SEVERAL sources in one manifest -- one per collection, each
    hashing on the same protein identity -- not as a join inside this adapter,
    which keeps the adapter a pure per-collection reader and the sharder's
    range-cutting untouched. The ``protein_content`` source reads
    ``seq_protein`` and DECOMPRESSES ``z_seq`` (see ``decompress`` below); the
    ``protein_result`` source reads ``protein_to_rast2``.

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
    """

    adapter_name = "mongo"

    def __init__(self, source: cm.Source, *, collection: Any = None):
        super().__init__(source)
        self._collection = collection
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
        """Yield each document as a dict row, batched, sorted and resumable.

        Streams a batched cursor -- NEVER loading the collection into memory --
        sorted ascending on ``sort_key`` so the read is a STABLE window that a
        killed run can resume (``start_after``) and a parallel run can slice
        (``min_key`` / ``max_key``) without overlap. Each document is copied to
        a plain dict, ``decompress`` fields are zlib-decoded (see
        ``decompress`` in the class docstring), and the row is yielded keyed by
        field name for the sharder to map.
        """
        collection = self._get_collection()
        cursor = collection.find(
            self._query_filter(), batch_size=self._batch_size()
        ).sort(self._sort_key(), 1)
        decompress = self._decompress_spec()
        self._exhausted = False
        for document in cursor:
            # See FileSourceAdapter.iter_records: the budget is claimed in the
            # adapter's loop so a --limit stop is NOT exhaustion. For mongo the
            # budget is per source per slice, so each slice reads up to --limit
            # documents of its own.
            if not self._admit_record():
                return
            yield self._prepare_row(document, decompress)
        self._exhausted = True

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
        self, document: Mapping[str, Any], decompress: Mapping[str, str]
    ) -> dict[str, Any]:
        """Copy a Mongo document to a plain dict, decoding compressed fields.

        For each ``output_field: input_field`` in ``decompress``, the raw
        (compressed) input field is replaced by its zlib-decompressed,
        UTF-8-decoded value under ``output_field``. A document missing the
        input field is yielded unchanged (no output field added) -- a stub row,
        which a manifest requiring the decoded column naturally rejects rather
        than the adapter fabricating a value.
        """
        row = dict(document)
        for output_field, input_field in decompress.items():
            if input_field not in row:
                continue
            row[output_field] = _zlib_decode(row.pop(input_field))
        return row


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


#: Concrete adapter classes, by name. All three planned adapters are now built:
#: ``file`` (CPU/disk only), ``lakehouse`` (in-pod / off-pod read), and
#: ``mongo`` (a batched collection read). :func:`get_adapter` no longer reports
#: any planned name as unbuilt.
_ADAPTER_CLASSES: dict[str, type[SourceAdapter]] = {
    "file": FileSourceAdapter,
    "lakehouse": LakehouseSourceAdapter,
    "mongo": MongoSourceAdapter,
}


class ManifestSourceError(RuntimeError):
    """An adapter could not read its declared source (missing path, etc.)."""


class AdapterNotImplementedError(NotImplementedError):
    """A manifest named a planned adapter with no class registered.

    All three planned adapters (``file``, ``lakehouse``, ``mongo``) are now
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


def get_adapter(
    source: cm.Source,
    *,
    run_context: Any = None,
    unit: str | None = None,
) -> SourceAdapter:
    """Construct the adapter a source's ``adapter`` key names.

    Args:
        source: The parsed ``[[source]]`` block.
        run_context: The backfill run context to bind (see
            :meth:`SourceAdapter.bind_run_context`), or ``None`` for a direct
            ``kbu clearinghouse shard``.
        unit: A unit label to restrict the read to (see
            :meth:`SourceAdapter.restrict_to_unit`), or ``None``.

    Raises:
        cm.ManifestError: The ``adapter`` name is not one of
            :data:`ADAPTER_NAMES` at all.
        AdapterNotImplementedError: The name is in :data:`ADAPTER_NAMES` but
            maps to no registered class. With ``file``, ``lakehouse`` and
            ``mongo`` all built, this no longer fires for any planned name.
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
    trusts it raw. Either way the result is the 64-char lowercase hex form.
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
    # precomputed: re-canonicalise, never trust raw.
    return schema.encode_entity_hash(str(value))


class ShardValueError(ValueError):
    """A source record could not be mapped to a target row (e.g. null hash)."""


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


def _build_row(
    plan: cm.TablePlan,
    row: Mapping[str, Any],
    *,
    run_observed_at: datetime,
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

    On a ``result`` row it also stamps ``parameter_set_hash`` from the plan --
    the hash the plan computed ONCE at plan time, not re-derived per row. When
    the plan's result ``source`` is a derivation rather than a
    ``@const(...)``, the tool was unknown at plan time, so the shared TRANSYT
    result-boundary validator
    (:func:`clearinghouse_parameters.validate_transyt_parameter_set`) runs
    HERE, against this row's resolved ``source``.
    """
    out: dict[str, Any] = {}
    for column, derivation in plan.columns.items():
        out[column] = _derive(derivation, row)
    out["entity_hash"] = _compute_entity_hash(plan, row)
    out["standardizer_version"] = standardizers.STANDARDIZER_VERSION
    if plan.kind in ("entity", "result"):
        out["entity_type"] = plan.entity_type
    if plan.kind == "result":
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
                        plan, record, run_observed_at=context.observed_at
                    )
                except ShardValueError as exc:
                    reason = (
                        SKIP_NULL_OBSERVED_AT
                        if "observed_at" in str(exc)
                        else SKIP_SHARD_VALUE_ERROR
                    )
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
    if not rows_by_hash:
        return None

    spec = _key_spec(None, "registry")
    rows = [rows_by_hash[key] for key in sorted(rows_by_hash)]
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
    for table, work in _group_plans_by_table(
        manifest, plans, run_context=run_context, adapter_unit=adapter_unit
    ):
        outcome = shard_table(work, out_root, context=context)
        reports.extend(outcome.reports)
        skips.extend(outcome.skips)
        sources.extend(outcome.sources)
        used_parameter_sets |= outcome.parameter_set_hashes

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
        adapter = get_adapter(source, run_context=run_context, unit=adapter_unit)
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
