"""Tests for the STREAMING bronze sharder: bounded memory, ULIDs, run.json.

Pure logic, no network, no pod, no credentials -- the shard stage needs only
CPU, disk and pyarrow, and these tests take it at its word.

The properties under test here are mostly INVISIBLE IN THE OUTPUT. A run that
streamed 264M records through a bounded buffer and a run that accumulated them
all in a list write the same parquet, so the memory assertions are made
against :class:`~clearinghouse_shard.ShardMetrics` -- the sharder's own record
of what it actually held -- rather than against the files. Everything else
(batch ids, observed_at, dedup winners, run.json) IS in the output and is
asserted by reading the bytes back off disk.

Success criteria exercised here:
    - the sharder spills to disk and never materialises a hash range, proven
      with a source whose single hash repeats far beyond the sort buffer
    - every bronze row has a non-null observed_at and an ingest_batch_id that
      is a per-batch ULID equal to its file stem
    - two sources into one table never collide, and are MERGED so the winner
      rule applies across them
    - duplicates are dropped: by entity_hash for entity/content, by the
      imported slot key for result, with the observed_at-descending then
      payload-bytes-ascending winner
    - adjacent batches may share only a boundary hash
    - run.json records every batch with a sha256 matching disk, per-source
      skip counts, per-source exhaustion, unit_id/attempt_id, and is
      append-only across units
    - the parameter_set registry is per-unit, keyed on parameter_set_hash
    - ``kbu clearinghouse plan`` never iterates source records
    - ``@len`` on a list returns its length
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from kbutillib.domains.identity import parameter_set_hash, standardizers
from kbutillib.domains.kbase.berdl import clearinghouse_derivation as cd
from kbutillib.domains.kbase.berdl import clearinghouse_manifest as cm
from kbutillib.domains.kbase.berdl import clearinghouse_shard as cs
from kbutillib.domains.kbase.berdl.clearinghouse_schema import PARAMETER_SET_TABLE

# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------


def _write_parquet(path: Path, columns: dict[str, list]) -> Path:
    pq.write_table(pa.table(columns), str(path))
    return path


def _gene_entity_toml(path: Path, *, name: str = "genes", kinds: str = '["content"]',
                      observed_at_column: str | None = None) -> str:
    extra = (
        f'observed_at_column = "{observed_at_column}"\n'
        if observed_at_column
        else ""
    )
    return f"""
[[source]]
name = "{name}"
adapter = "file"
path = "{path}"
format = "parquet"
entity_type = "gene"
kinds = {kinds}
{extra}
[source.hash]
raw_column = "dna_sequence"

[source.content]
sequence = "dna_sequence"
seq_length = "@len(dna_sequence)"
"""


def _gene_result_toml(
    path: Path,
    *,
    name: str,
    parameter_set: str = "{}",
    source_value: str = "@const(bakta/1.9)",
    observed_at_column: str | None = None,
    payload_columns: str = "function_id",
) -> str:
    extra = (
        f'observed_at_column = "{observed_at_column}"\n'
        if observed_at_column
        else ""
    )
    return f"""
[[source]]
name = "{name}"
adapter = "file"
path = "{path}"
format = "parquet"
entity_type = "gene"
kinds = ["result"]
{extra}
  [source.hash]
  raw_column = "dna_sequence"

  [source.result]
  source        = "{source_value}"
  result_type   = "@const(functional_annotation)"
  payload       = "@json({payload_columns})"
  parameter_set = {parameter_set}
"""


def _read_all(out_root: Path, table: str) -> list[dict]:
    rows: list[dict] = []
    for path in sorted((out_root / table).glob("*.parquet")):
        rows.extend(pq.read_table(str(path)).to_pylist())
    return rows


class _FakeAdapter(cs.SourceAdapter):
    """An adapter over an in-memory record list with a PINNED byte estimate.

    The estimate is what fixes the range count (``ceil(estimate / target)``),
    so a test that needs every row in ONE range -- to drive one range's run
    count past the fan-in cap -- can say so instead of reverse-engineering a
    parquet file size.
    """

    adapter_name = "file"

    def __init__(self, source, records, *, est_bytes: int):
        super().__init__(source)
        self._records = list(records)
        self._est = est_bytes

    def estimate_bytes(self) -> int:
        return self._est

    def iter_records(self):
        self._exhausted = False
        yield from self._records
        self._exhausted = True


def _seqs(n: int) -> list[str]:
    """``n`` distinct DNA-alphabet strings, so each hashes to its own digest."""
    letters = "ACGT"
    out = []
    for i in range(n):
        value = i
        chars = []
        for _ in range(12):
            chars.append(letters[value & 3])
            value >>= 2
        out.append("".join(chars))
    return out


# --------------------------------------------------------------------------
# 1. Bounded memory: the sharder spills, and never materialises a range
# --------------------------------------------------------------------------


def test_union_of_shards_equals_the_deduplicated_input(tmp_path):
    """A few thousand rows, a small target_bytes: the union of the shards is
    exactly the deduplicated input, and the sort buffer never held a range."""
    seqs = _seqs(3000)
    src = _write_parquet(tmp_path / "genes.parquet", {"dna_sequence": seqs})
    manifest = cm.load_manifest(_gene_entity_toml(src))
    out_root = tmp_path / "bronze"
    metrics = cs.ShardMetrics()
    cs.shard_manifest(manifest, out_root, target_bytes=4096, metrics=metrics)

    written = _read_all(out_root, "gene_content")
    assert {row["sequence"] for row in written} == set(seqs)
    assert len(written) == len(seqs)
    # The records went to DISK, not into a list.
    assert metrics.spill_dir_bytes > 0
    assert metrics.sorted_runs > 1
    # And the buffer never held even one range, let alone the source. (With
    # 3000 distinct uniformly-distributed hashes and several ranges, the
    # largest range is a few hundred rows; the buffer bound is what matters.)
    assert metrics.max_sort_buffer_rows < len(seqs)
    assert metrics.max_resident_rows < len(seqs)


def test_one_hash_repeated_far_beyond_the_buffer_never_materialises_its_range(
    tmp_path,
):
    """THE case equal-width hash ranges do not protect against.

    Twelve thousand records that all map to ONE hash all land in ONE range, so
    a design that sized a range from an estimate and sorted it in memory would
    hold all twelve thousand. The external merge holds the sort buffer, the
    merge frontier and one output batch -- and the assertion is that the
    high-water mark is a small fraction of the range, not merely less than it.
    """
    rows = 12_000
    src = _write_parquet(
        tmp_path / "one.parquet", {"dna_sequence": ["ACGTACGTACGT"] * rows}
    )
    manifest = cm.load_manifest(_gene_entity_toml(src))
    out_root = tmp_path / "bronze"
    metrics = cs.ShardMetrics()
    # ~120 bytes per row, so the sort buffer holds about a thousand rows and
    # the single hash repeats roughly twelve times the buffer size.
    cs.shard_manifest(manifest, out_root, target_bytes=120_000, metrics=metrics)

    # One range carries every row.
    assert max(metrics.rows_per_range.values()) == rows
    # The sort buffer is bounded by target_bytes, nowhere near the range.
    assert metrics.max_sort_buffer_rows < rows // 5
    # The merge frontier is bounded ABSOLUTELY, by row budget, not by range.
    assert metrics.max_resident_rows <= cs._MERGE_ROW_BUDGET + 64
    assert metrics.max_resident_rows < rows
    # Dedup collapses the repeated hash to a single row.
    written = _read_all(out_root, "gene_content")
    assert len(written) == 1
    assert written[0]["entity_hash"] == standardizers.entity_hash(
        "gene", "ACGTACGTACGT"
    )
    assert metrics.rows_deduplicated == rows - 1


def test_a_range_with_more_runs_than_the_fanin_cap_merges_in_several_passes(
    tmp_path,
):
    """Beyond :data:`_MAX_MERGE_FANIN` runs the merge goes multi-pass, and the
    output is still exactly the deduplicated input in sorted order.

    A single-pass merge of a very wide range would open every run file at once
    and floor the per-run read at one row; the cap reduces the fan-in first.
    The byte estimate is pinned to force ONE range, so every flush adds a run
    to the same range and the cap is actually exercised.
    """
    seqs = _seqs(60)
    records = [{"dna_sequence": s} for s in seqs for _ in range(15)]
    src = _write_parquet(tmp_path / "genes.parquet", {"dna_sequence": seqs})
    manifest = cm.load_manifest(_gene_entity_toml(src))
    (plan,) = cm.shard_plan(manifest)
    adapter = _FakeAdapter(manifest.sources[0], records, est_bytes=1)
    out_root = tmp_path / "bronze"
    metrics = cs.ShardMetrics()
    outcome = cs.shard_table(
        [(plan, adapter)],
        out_root,
        context=cs.ShardRunContext.build(target_bytes=256, metrics=metrics),
    )

    assert metrics.ranges == 1
    assert metrics.sorted_runs > cs._MAX_MERGE_FANIN
    # One range, so a single-pass merge would be exactly one pass. More than
    # that means the fan-in cap forced reduction passes first.
    assert metrics.merge_passes > 1
    written = _read_all(out_root, "gene_content")
    assert {row["sequence"] for row in written} == set(seqs)
    hashes = [row["entity_hash"] for row in written]
    assert hashes == sorted(hashes)
    assert len(hashes) == len(seqs)
    assert metrics.rows_deduplicated == len(records) - len(seqs)
    # Even across the reduction passes, residency stays inside the budget.
    assert metrics.max_resident_rows <= cs._MERGE_ROW_BUDGET + 64
    assert outcome.sources[0].exhausted is True


# --------------------------------------------------------------------------
# 2. Per-batch ULID ingest_batch_id, equal to the file stem
# --------------------------------------------------------------------------


def test_every_batch_id_is_a_distinct_ulid_equal_to_its_file_stem(tmp_path):
    seqs = _seqs(400)
    src = _write_parquet(tmp_path / "genes.parquet", {"dna_sequence": seqs})
    manifest = cm.load_manifest(_gene_entity_toml(src, kinds='["entity", "content"]'))
    out_root = tmp_path / "bronze"
    reports = cs.shard_manifest(manifest, out_root, target_bytes=2048)

    assert len(reports) > 1
    ids = [r.batch_id for r in reports]
    assert len(ids) == len(set(ids))
    for report in reports:
        assert cs.is_ulid(report.batch_id), report.batch_id
        assert report.path.stem == report.batch_id
        stamped = set(
            pq.read_table(str(report.path), columns=["ingest_batch_id"])
            .column("ingest_batch_id")
            .to_pylist()
        )
        # Every row of the batch carries the batch's own id -- one value.
        assert stamped == {report.batch_id}


def test_ulids_are_monotonic_within_a_process():
    minted = [cs.new_ulid() for _ in range(2000)]
    assert minted == sorted(minted)
    assert len(set(minted)) == len(minted)
    assert all(len(value) == 26 for value in minted)
    # Crockford base32 excludes I, L, O and U.
    assert not (set("".join(minted)) & set("ILOU"))


def test_two_sources_into_one_table_do_not_collide(tmp_path):
    """The defect that made this impossible: a per-call counter restarting at
    zero, so the second source's batch-0000 overwrote the first's."""
    a = _write_parquet(tmp_path / "a.parquet", {"dna_sequence": _seqs(50)})
    b = _write_parquet(
        tmp_path / "b.parquet", {"dna_sequence": _seqs(100)[50:]}
    )
    manifest = cm.load_manifest(
        _gene_entity_toml(a, name="src-a") + _gene_entity_toml(b, name="src-b")
    )
    out_root = tmp_path / "bronze"
    reports = cs.shard_manifest(manifest, out_root, target_bytes=512)

    files = sorted((out_root / "gene_content").glob("*.parquet"))
    assert len(files) == len({f.name for f in files})
    assert len(files) == len([r for r in reports if r.table == "gene_content"])
    # Nothing was lost: both sources' rows are present.
    written = {row["sequence"] for row in _read_all(out_root, "gene_content")}
    assert written == set(_seqs(100))


# --------------------------------------------------------------------------
# 3. observed_at is never null
# --------------------------------------------------------------------------


def test_no_row_has_a_null_observed_at(tmp_path):
    seqs = _seqs(200)
    src = _write_parquet(tmp_path / "genes.parquet", {"dna_sequence": seqs})
    manifest = cm.load_manifest(_gene_entity_toml(src, kinds='["entity", "content"]'))
    out_root = tmp_path / "bronze"
    run_start = datetime(2026, 3, 4, 5, 6, 7)
    cs.shard_manifest(
        manifest, out_root, target_bytes=1024, observed_at=run_start
    )
    for table in ("gene_entity", "gene_content"):
        rows = _read_all(out_root, table)
        assert rows
        for row in rows:
            assert row["observed_at"] is not None
            # One value for the whole run when no column is declared.
            assert row["observed_at"] == run_start


def test_observed_at_column_is_honoured_and_normalised_to_utc(tmp_path):
    src = _write_parquet(
        tmp_path / "genes.parquet",
        {
            "dna_sequence": _seqs(3),
            # An ISO string with Z, one with an offset, one naive.
            "seen": [
                "2026-01-02T03:04:05Z",
                "2026-01-02T05:04:05+02:00",
                "2026-02-03T04:05:06",
            ],
        },
    )
    manifest = cm.load_manifest(_gene_entity_toml(src, observed_at_column="seen"))
    out_root = tmp_path / "bronze"
    cs.shard_manifest(manifest, out_root, target_bytes=1024)
    stamped = {row["observed_at"] for row in _read_all(out_root, "gene_content")}
    assert stamped == {
        datetime(2026, 1, 2, 3, 4, 5),  # Z -> naive UTC
        datetime(2026, 1, 2, 3, 4, 5),  # +02:00 -> the same UTC instant
        datetime(2026, 2, 3, 4, 5, 6),  # naive -> assumed UTC
    }


def test_observed_at_column_is_mapping_not_locator(tmp_path):
    """It must not fall through to the adapter's locator, where it would be
    silently inert -- the manifest declared it and nothing would read it."""
    src = _write_parquet(
        tmp_path / "genes.parquet",
        {"dna_sequence": _seqs(1), "seen": ["2026-01-01T00:00:00Z"]},
    )
    (source,) = cm.load_manifest(
        _gene_entity_toml(src, observed_at_column="seen")
    ).sources
    assert source.observed_at_column == "seen"
    assert "observed_at_column" not in source.locator


def test_a_null_declared_observed_at_is_skipped_and_counted(tmp_path):
    src = _write_parquet(
        tmp_path / "genes.parquet",
        {
            "dna_sequence": _seqs(4),
            "seen": ["2026-01-02T03:04:05Z", None, "2026-01-03T00:00:00Z", None],
        },
    )
    manifest = cm.load_manifest(
        _gene_entity_toml(src, name="genes", observed_at_column="seen")
    )
    out_root = tmp_path / "bronze"
    cs.shard_manifest(manifest, out_root, target_bytes=1024)

    rows = _read_all(out_root, "gene_content")
    assert len(rows) == 2
    assert all(row["observed_at"] is not None for row in rows)

    run = cs.read_run_manifest(out_root)
    skips = {(s["source"], s["reason"]): s["count"] for s in run["skipped"]}
    assert skips == {("genes", cs.SKIP_NULL_OBSERVED_AT): 2}
    # The skip is accounted against the source, separately from its counts.
    (outcome,) = [s for s in run["sources"] if s["source"] == "genes"]
    assert outcome["emitted"] == 4
    assert outcome["accepted"] == 2


def test_an_unparseable_declared_observed_at_is_skipped_not_defaulted(tmp_path):
    src = _write_parquet(
        tmp_path / "genes.parquet",
        {"dna_sequence": _seqs(2), "seen": ["2026-01-02T03:04:05Z", "not-a-date"]},
    )
    manifest = cm.load_manifest(_gene_entity_toml(src, observed_at_column="seen"))
    out_root = tmp_path / "bronze"
    cs.shard_manifest(manifest, out_root, target_bytes=1024)
    assert len(_read_all(out_root, "gene_content")) == 1
    run = cs.read_run_manifest(out_root)
    assert sum(s["count"] for s in run["skipped"]) == 1


def test_an_adapters_own_skips_are_merged_with_the_sharders(tmp_path):
    """The ABC's ``skipped()`` default is empty; an adapter that reports skips
    has them merged into the same per-source list in run.json."""
    src = _write_parquet(tmp_path / "genes.parquet", {"dna_sequence": _seqs(3)})
    manifest = cm.load_manifest(_gene_entity_toml(src, name="genes"))
    (plan,) = cm.shard_plan(manifest)

    class _SkippingAdapter(cs.FileSourceAdapter):
        def iter_records(self):
            self._skipped["adapter-decided"] = 7
            yield from super().iter_records()

    adapter = _SkippingAdapter(manifest.sources[0])
    outcome = cs.shard_table([(plan, adapter)], tmp_path / "bronze")
    assert cs.SkipReport("genes", "adapter-decided", 7) in outcome.skips


# --------------------------------------------------------------------------
# 4. Dedup, with a defined winner, across the whole merge
# --------------------------------------------------------------------------


def test_consecutive_duplicate_hashes_are_dropped_for_content(tmp_path):
    seqs = _seqs(50)
    src = _write_parquet(
        tmp_path / "genes.parquet", {"dna_sequence": seqs + seqs + seqs}
    )
    manifest = cm.load_manifest(_gene_entity_toml(src))
    out_root = tmp_path / "bronze"
    metrics = cs.ShardMetrics()
    cs.shard_manifest(manifest, out_root, target_bytes=512, metrics=metrics)

    rows = _read_all(out_root, "gene_content")
    assert len(rows) == 50
    assert {r["sequence"] for r in rows} == set(seqs)
    assert metrics.rows_deduplicated == 100
    assert metrics.rows_written == 50
    # The per-batch counts sum to the rows actually dropped -- a drop that
    # landed on nobody would make run.json understate the shortfall.
    run = cs.read_run_manifest(out_root)
    assert sum(b["rows_deduplicated"] for b in run["batches"]) == 100


def test_duplicate_result_slots_are_dropped_by_the_imported_slot_key(tmp_path):
    """Three rows for ONE slot collapse to one; and the slot key is the
    IMPORTED tuple, so two rows differing only in result_type are two slots."""
    seq = "ACGTACGTACGT"
    src = _write_parquet(
        tmp_path / "ann.parquet",
        {"dna_sequence": [seq, seq, seq], "function_id": ["f1", "f1", "f1"]},
    )
    manifest = cm.load_manifest(_gene_result_toml(src, name="ann"))
    out_root = tmp_path / "bronze"
    cs.shard_manifest(manifest, out_root, target_bytes=512)
    rows = _read_all(out_root, "gene_result")
    assert len(rows) == 1
    # Sanity: the sharder deduplicates on exactly the derivation's tuple.
    assert cs.SLOT_KEY_COLUMNS is cd.SLOT_KEY_COLUMNS
    assert cs.SLOT_KEY_COLUMNS == (
        "entity_hash",
        "entity_type",
        "result_type",
        "source",
        "parameter_set_hash",
    )


def test_one_hash_with_several_slots_keeps_every_slot(tmp_path):
    """Deduplicating a result table on entity_hash would destroy real rows:
    one hash legitimately carries one slot per (tool, parameter set)."""
    seq = "ACGTACGTACGT"
    src = _write_parquet(
        tmp_path / "ann.parquet", {"dna_sequence": [seq], "function_id": ["f1"]}
    )
    manifest = cm.load_manifest(
        _gene_result_toml(src, name="bakta", source_value="@const(bakta/1.9)")
        + _gene_result_toml(
            src,
            name="rast",
            source_value="@const(rast/2.0)",
            parameter_set='{ evalue = "1e-5" }',
        )
    )
    out_root = tmp_path / "bronze"
    cs.shard_manifest(manifest, out_root, target_bytes=512)
    rows = _read_all(out_root, "gene_result")
    assert len(rows) == 2
    assert {r["source"] for r in rows} == {"bakta/1.9", "rast/2.0"}
    assert len({r["entity_hash"] for r in rows}) == 1


def test_result_winner_is_the_latest_observed_at_then_payload_bytes(tmp_path):
    """A/B/A for ONE slot: exactly one row survives, and it is an A.

    The rows differ only in payload and observed_at. The winner rule is
    ``observed_at`` DESCENDING, then canonical payload bytes ASCENDING, which
    is the ORDER BY the current-state view will apply -- so the row the
    sharder keeps is the row the lake would have chosen anyway.
    """
    seq = "ACGTACGTACGT"
    src = _write_parquet(
        tmp_path / "ann.parquet",
        {
            "dna_sequence": [seq, seq, seq],
            "function_id": ["A", "B", "A"],
            "seen": [
                "2026-01-01T00:00:00Z",  # A, oldest
                "2026-03-01T00:00:00Z",  # B, middle
                "2026-05-01T00:00:00Z",  # A, NEWEST -> must win
            ],
        },
    )
    manifest = cm.load_manifest(
        _gene_result_toml(src, name="ann", observed_at_column="seen")
    )
    out_root = tmp_path / "bronze"
    cs.shard_manifest(manifest, out_root, target_bytes=512)
    (row,) = _read_all(out_root, "gene_result")
    assert json.loads(row["payload"]) == {"function_id": "A"}
    assert row["observed_at"] == datetime(2026, 5, 1, 0, 0, 0)


def test_payload_bytes_break_an_observed_at_tie_deterministically(tmp_path):
    """Two rows for one slot sharing a timestamp: the LOWER canonical payload
    wins, so the choice is reproducible rather than input-order dependent."""
    seq = "ACGTACGTACGT"
    stamp = "2026-01-01T00:00:00Z"
    forward = _write_parquet(
        tmp_path / "f.parquet",
        {"dna_sequence": [seq, seq], "function_id": ["zzz", "aaa"],
         "seen": [stamp, stamp]},
    )
    reverse = _write_parquet(
        tmp_path / "r.parquet",
        {"dna_sequence": [seq, seq], "function_id": ["aaa", "zzz"],
         "seen": [stamp, stamp]},
    )
    kept = []
    for i, src in enumerate((forward, reverse)):
        manifest = cm.load_manifest(
            _gene_result_toml(src, name=f"ann{i}", observed_at_column="seen")
        )
        out_root = tmp_path / f"bronze{i}"
        cs.shard_manifest(manifest, out_root, target_bytes=512)
        (row,) = _read_all(out_root, "gene_result")
        kept.append(json.loads(row["payload"])["function_id"])
    assert kept == ["aaa", "aaa"]


def test_dedup_survives_a_batch_cut(tmp_path):
    """Duplicates are dropped across the WHOLE merge of a range, not per
    batch: a tiny target_bytes cuts batches mid-stream and the duplicate of a
    row that ended the previous batch must still be recognised."""
    seqs = _seqs(300)
    src = _write_parquet(
        tmp_path / "genes.parquet",
        {"dna_sequence": [s for s in seqs for _ in range(4)]},
    )
    manifest = cm.load_manifest(_gene_entity_toml(src))
    out_root = tmp_path / "bronze"
    cs.shard_manifest(manifest, out_root, target_bytes=400)
    rows = _read_all(out_root, "gene_content")
    assert len(rows) == 300
    assert len({r["entity_hash"] for r in rows}) == 300


# --------------------------------------------------------------------------
# 5. Winner scope: a unit's plans for one table share ONE merge
# --------------------------------------------------------------------------


def test_two_sources_in_one_unit_mapping_one_slot_keep_the_later(tmp_path):
    """The round-2 amendment: the winner rule applies ACROSS the sources of a
    unit, so the unit emits ONE row for a shared slot, not two."""
    shared = "ACGTACGTACGT"
    only_old, only_new = "TTTTGGGGCCAA", "GATTACAGATTA"
    old_path = _write_parquet(
        tmp_path / "old.parquet",
        {
            "dna_sequence": [shared, only_old],
            "function_id": ["old", "old-only"],
            "seen": ["2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"],
        },
    )
    new_path = _write_parquet(
        tmp_path / "new.parquet",
        {
            "dna_sequence": [shared, only_new],
            "function_id": ["new", "new-only"],
            "seen": ["2026-06-01T00:00:00Z", "2026-06-01T00:00:00Z"],
        },
    )
    manifest = cm.load_manifest(
        _gene_result_toml(old_path, name="src-old", observed_at_column="seen")
        + _gene_result_toml(new_path, name="src-new", observed_at_column="seen")
    )
    out_root = tmp_path / "bronze"
    reports = cs.shard_manifest(manifest, out_root, target_bytes=65536)

    rows = _read_all(out_root, "gene_result")
    # The SHARED slot collapsed to one row -- the later observed_at wins --
    # while each source's own slot survived. Three rows, not four.
    assert len(rows) == 3
    by_payload = {json.loads(r["payload"])["function_id"]: r for r in rows}
    assert set(by_payload) == {"new", "old-only", "new-only"}
    assert by_payload["new"]["observed_at"] == datetime(2026, 6, 1, 0, 0, 0)
    assert "old" not in by_payload  # the loser never reached disk

    # One batch, fed by BOTH sources: a mixed batch's `source` is the sorted
    # list of contributing names, because one string would be a lie.
    (batch,) = [r for r in reports if r.table == "gene_result"]
    assert batch.sources == ("src-new", "src-old")
    run = cs.read_run_manifest(out_root)
    (entry,) = [b for b in run["batches"] if b["table"] == "gene_result"]
    assert entry["source"] == ["src-new", "src-old"]


def test_entity_rows_from_two_sources_in_one_unit_collapse_to_one(tmp_path):
    """The entity/content first-row rule applies across sources too."""
    seqs = _seqs(20)
    a = _write_parquet(tmp_path / "a.parquet", {"dna_sequence": seqs})
    b = _write_parquet(tmp_path / "b.parquet", {"dna_sequence": seqs})
    manifest = cm.load_manifest(
        _gene_entity_toml(a, name="a", kinds='["entity"]')
        + _gene_entity_toml(b, name="b", kinds='["entity"]')
    )
    out_root = tmp_path / "bronze"
    cs.shard_manifest(manifest, out_root, target_bytes=1024)
    rows = _read_all(out_root, "gene_entity")
    assert len(rows) == 20


def test_shard_table_refuses_plans_for_different_tables(tmp_path):
    src = _write_parquet(tmp_path / "g.parquet", {"dna_sequence": _seqs(2)})
    manifest = cm.load_manifest(
        _gene_entity_toml(src, kinds='["entity", "content"]')
    )
    plans = cm.shard_plan(manifest)
    work = [(plan, cs.get_adapter(manifest.sources[0])) for plan in plans]
    with pytest.raises(ValueError, match="ONE table"):
        cs.shard_table(work, tmp_path / "bronze")


# --------------------------------------------------------------------------
# 6. Sorted, and non-overlapping except for a shared boundary hash
# --------------------------------------------------------------------------


def test_batches_are_sorted_and_may_share_only_a_boundary_hash(tmp_path):
    """One hash carrying many slots forces a cut inside the hash, so adjacent
    batches TOUCH at a boundary value. That is sorted and is not an overlap."""
    seq = "ACGTACGTACGT"
    n = 40
    src = _write_parquet(
        tmp_path / "ann.parquet",
        {
            "dna_sequence": [seq] * n,
            "function_id": [f"f{i:03d}" for i in range(n)],
            "tool": [f"tool-{i:03d}" for i in range(n)],
        },
    )
    manifest = cm.load_manifest(
        _gene_result_toml(src, name="ann", source_value="@lower(tool)")
    )
    out_root = tmp_path / "bronze"
    # A tiny target forces several batches inside the single hash.
    reports = [
        r
        for r in cs.shard_manifest(manifest, out_root, target_bytes=200)
        if r.table == "gene_result"
    ]
    assert len(reports) > 1
    digest = standardizers.entity_hash("gene", seq)
    prev_max = None
    for report in reports:
        hashes = (
            pq.read_table(str(report.path), columns=["entity_hash"])
            .column("entity_hash")
            .to_pylist()
        )
        assert hashes == sorted(hashes)
        if prev_max is not None:
            # Touching at the boundary is allowed; going backwards is not.
            assert hashes[0] >= prev_max
        prev_max = hashes[-1]
    # Here every batch's bound IS that one boundary hash.
    assert {r.hash_min for r in reports} == {digest}
    # All forty slots survive -- nothing was deduplicated away.
    assert len(_read_all(out_root, "gene_result")) == n
    # A range that yielded several batches reports every one as a split.
    assert all(r.split for r in reports)


def test_an_unsorted_shard_fails_the_build(tmp_path):
    """The invariant is ENFORCED, not trusted: a hand-written unsorted file is
    rejected with ShardOrderError."""
    table_dir = tmp_path / "gene_entity"
    table_dir.mkdir()
    path = table_dir / "01HZZZZZZZZZZZZZZZZZZZZZZZ.parquet"
    pq.write_table(pa.table({"entity_hash": ["ff" * 32, "00" * 32]}), str(path))
    report = cs.ShardReport(
        table="gene_entity",
        batch_id=path.stem,
        path=path,
        rows=2,
        hash_min="ff" * 32,
        hash_max="00" * 32,
    )
    with pytest.raises(cs.ShardOrderError, match="not ascending"):
        cs._assert_sorted_and_disjoint([report], "gene_entity")


# --------------------------------------------------------------------------
# 7. run.json
# --------------------------------------------------------------------------


def test_run_json_lists_every_file_with_a_matching_sha256(tmp_path):
    seqs = _seqs(300)
    src = _write_parquet(tmp_path / "genes.parquet", {"dna_sequence": seqs})
    manifest_path = tmp_path / "m.toml"
    manifest_path.write_text(
        _gene_entity_toml(src, kinds='["entity", "content"]'), encoding="utf-8"
    )
    out_root = tmp_path / "bronze"
    reports = cs.shard_manifest(
        cm.load_manifest(manifest_path),
        out_root,
        target_bytes=1024,
        manifest_path=manifest_path,
    )

    run = cs.read_run_manifest(out_root)
    assert run["run_manifest_version"] == cs.RUN_MANIFEST_VERSION
    assert cs.is_ulid(run["run_id"])
    assert run["standardizer_version"] == standardizers.STANDARDIZER_VERSION
    # Provenance, not a placeholder: the version really is read.
    assert run["kbutillib_version"] not in (None, "", "unknown")
    assert run["observed_at"].endswith("Z")
    assert run["manifest_name"] == "m.toml"
    assert run["manifest_sha256"] == hashlib.sha256(
        manifest_path.read_bytes()
    ).hexdigest()
    assert run["slices"] == []

    # Every batch is listed, with a sha256 that matches the file on disk and a
    # relative `file` path that resolves from the output root.
    assert len(run["batches"]) == len(reports)
    for entry in run["batches"]:
        path = out_root / entry["file"]
        assert path.is_file()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert entry["sha256"] == digest
        assert entry["batch_id"] == path.stem
        assert entry["key_column"] == "entity_hash"
        assert cs.is_ulid(entry["unit_id"]) and cs.is_ulid(entry["attempt_id"])
        assert entry["rows"] == pq.ParquetFile(str(path)).metadata.num_rows


def test_run_json_records_per_source_exhaustion(tmp_path):
    src = _write_parquet(tmp_path / "genes.parquet", {"dna_sequence": _seqs(10)})
    manifest = cm.load_manifest(_gene_entity_toml(src, name="genes"))
    out_root = tmp_path / "bronze"
    cs.shard_manifest(manifest, out_root, target_bytes=1024)
    run = cs.read_run_manifest(out_root)
    (entry,) = run["sources"]
    assert entry["source"] == "genes"
    assert entry["exhausted"] is True
    assert entry["emitted"] == 10
    assert entry["accepted"] == 10


def test_a_limit_stop_is_not_exhaustion(tmp_path):
    """A caller that stops consuming the iterator leaves exhausted() False --
    the whole point of recording it separately from the counts."""
    src = _write_parquet(tmp_path / "genes.parquet", {"dna_sequence": _seqs(10)})
    (source,) = cm.load_manifest(_gene_entity_toml(src)).sources
    adapter = cs.FileSourceAdapter(source)
    assert adapter.exhausted() is None
    taken = []
    for record in adapter.iter_records():
        taken.append(record)
        if len(taken) == 3:
            break
    assert adapter.exhausted() is False
    drained = list(cs.FileSourceAdapter(source).iter_records())
    assert len(drained) == 10


def test_run_json_is_append_only_across_units(tmp_path):
    """Two units into one output root: the inventory grows, and the first
    unit's entries are byte-identical to what it published."""
    a = _write_parquet(tmp_path / "a.parquet", {"dna_sequence": _seqs(30)})
    b = _write_parquet(tmp_path / "b.parquet", {"dna_sequence": _seqs(60)[30:]})
    out_root = tmp_path / "bronze"
    run_id = cs.new_ulid()

    cs.shard_manifest(
        cm.load_manifest(_gene_entity_toml(a, name="src-a")),
        out_root,
        target_bytes=1024,
        run_id=run_id,
        slice_label="slice-00",
    )
    first = cs.read_run_manifest(out_root)
    first_batches_text = json.dumps(first["batches"])
    first_unit = cs.unit_ids(first)[0]

    cs.shard_manifest(
        cm.load_manifest(_gene_entity_toml(b, name="src-b")),
        out_root,
        target_bytes=1024,
        run_id=run_id,
        slice_label="slice-01",
    )
    second = cs.read_run_manifest(out_root)

    # The header is the FIRST unit's and is preserved verbatim.
    assert second["run_id"] == first["run_id"] == run_id
    assert second["created_at"] == first["created_at"]
    # Both units are listed, with their own ids.
    units = cs.unit_ids(second)
    assert len(units) == 2 and units[0] == first_unit
    assert second["slices"] == ["slice-00", "slice-01"]
    # The first unit's batch entries are byte-identical to before.
    kept = second["batches"][: len(first["batches"])]
    assert json.dumps(kept) == first_batches_text
    # And the second unit added its own, addressable by the unit selector.
    assert len(second["batches"]) > len(first["batches"])
    assert cs.batches_for_unit(second, first_unit) == first["batches"]
    assert cs.batches_for_unit(second, units[1])
    for entry in cs.batches_for_unit(second, units[1]):
        assert entry["slice"] == "slice-01"
        assert entry["unit_id"] == units[1]


def test_run_json_is_not_mistaken_for_a_shard(tmp_path):
    """It lives at the output ROOT, so shard discovery -- which only descends
    into directories named for a known table -- never sees it."""
    from kbutillib.domains.kbase.berdl.clearinghouse_capability import (
        _discover_shards,
    )

    src = _write_parquet(tmp_path / "genes.parquet", {"dna_sequence": _seqs(10)})
    out_root = tmp_path / "bronze"
    cs.shard_manifest(
        cm.load_manifest(_gene_entity_toml(src)), out_root, target_bytes=1024
    )
    assert (out_root / cs.RUN_MANIFEST_FILE).is_file()
    discovered = _discover_shards(out_root)
    assert discovered
    assert all(path.suffix == ".parquet" for _, _, path in discovered)
    # The batch id discovery reports IS the ULID file stem the sharder minted.
    assert all(cs.is_ulid(batch) for _, batch, _ in discovered)


def test_manifest_sha256_falls_back_to_canonical_text_in_memory(tmp_path):
    src = _write_parquet(tmp_path / "genes.parquet", {"dna_sequence": _seqs(5)})
    manifest = cm.load_manifest(_gene_entity_toml(src))
    out_root = tmp_path / "bronze"
    cs.shard_manifest(manifest, out_root, target_bytes=1024)
    run = cs.read_run_manifest(out_root)
    assert run["manifest_name"] is None
    assert run["manifest_sha256"] == hashlib.sha256(
        cs.canonical_manifest_text(manifest).encode("utf-8")
    ).hexdigest()
    # The rendering is deterministic: the same manifest, the same text.
    assert cs.canonical_manifest_text(manifest) == cs.canonical_manifest_text(
        cm.load_manifest(_gene_entity_toml(src))
    )


# --------------------------------------------------------------------------
# 8. The parameter_set registry, per unit
# --------------------------------------------------------------------------


def test_two_parameter_sets_write_a_two_row_registry_bounded_by_its_own_key(
    tmp_path,
):
    seq = "ACGTACGTACGT"
    src = _write_parquet(
        tmp_path / "ann.parquet", {"dna_sequence": [seq], "function_id": ["f1"]}
    )
    manifest = cm.load_manifest(
        _gene_result_toml(src, name="default-run", parameter_set="{}")
        + _gene_result_toml(
            src,
            name="evalue-run",
            source_value="@const(rast/2.0)",
            parameter_set='{ evalue = "1e-5" }',
        )
    )
    out_root = tmp_path / "bronze"
    reports = cs.shard_manifest(manifest, out_root, target_bytes=4096)

    (registry,) = [r for r in reports if r.table == PARAMETER_SET_TABLE]
    assert registry.rows == 2
    rows = pq.read_table(str(registry.path)).to_pylist()
    expected = {parameter_set_hash({}), parameter_set_hash({"evalue": "1e-5"})}
    assert {r["parameter_set_hash"] for r in rows} == expected
    # Keyed, sorted, deduplicated and BOUNDED on parameter_set_hash -- the
    # registry has no entity_hash column at all.
    assert "entity_hash" not in rows[0]
    digests = [r["parameter_set_hash"] for r in rows]
    assert digests == sorted(digests)
    assert registry.key_column == "parameter_set_hash"
    assert registry.hash_min == digests[0]
    assert registry.hash_max == digests[-1]
    # Same provenance rules as every other table.
    assert all(r["observed_at"] is not None for r in rows)
    assert {r["ingest_batch_id"] for r in rows} == {registry.batch_id}
    assert cs.is_ulid(registry.batch_id)

    # run.json records the registry batch with parameter_set_hash bounds.
    run = cs.read_run_manifest(out_root)
    (entry,) = [b for b in run["batches"] if b["table"] == PARAMETER_SET_TABLE]
    assert entry["key_column"] == "parameter_set_hash"
    assert entry["hash_min"] == digests[0]
    assert entry["hash_max"] == digests[-1]
    # And it is the FIRST batch in the inventory, written before any result.
    assert run["batches"][0]["table"] == PARAMETER_SET_TABLE


def test_a_second_unit_registers_only_the_parameter_set_it_discovers(tmp_path):
    """Registry batches are IMMUTABLE and per unit: a set an earlier unit
    already registered is not re-emitted."""
    seq = "ACGTACGTACGT"
    src = _write_parquet(
        tmp_path / "ann.parquet", {"dna_sequence": [seq], "function_id": ["f1"]}
    )
    out_root = tmp_path / "bronze"
    run_id = cs.new_ulid()

    cs.shard_manifest(
        cm.load_manifest(
            _gene_result_toml(src, name="unit1", parameter_set="{}")
        ),
        out_root,
        run_id=run_id,
    )
    first_files = sorted((out_root / PARAMETER_SET_TABLE).glob("*.parquet"))
    assert len(first_files) == 1
    first_bytes = first_files[0].read_bytes()

    # Unit two uses one OLD set ({}) and one NEW set.
    reports = cs.shard_manifest(
        cm.load_manifest(
            _gene_result_toml(
                src, name="unit2-old", parameter_set="{}",
                source_value="@const(bakta/2.0)",
            )
            + _gene_result_toml(
                src,
                name="unit2-new",
                source_value="@const(rast/2.0)",
                parameter_set='{ evalue = "1e-9" }',
            )
        ),
        out_root,
        run_id=run_id,
    )
    (registry,) = [r for r in reports if r.table == PARAMETER_SET_TABLE]
    rows = pq.read_table(str(registry.path)).to_pylist()
    assert [r["parameter_set_hash"] for r in rows] == [
        parameter_set_hash({"evalue": "1e-9"})
    ]
    # The first unit's batch is untouched -- immutable means immutable.
    assert first_files[0].read_bytes() == first_bytes
    # Two batch files, one logical table.
    assert len(sorted((out_root / PARAMETER_SET_TABLE).glob("*.parquet"))) == 2
    assert cs.discovered_parameter_sets(out_root) == {
        parameter_set_hash({}),
        parameter_set_hash({"evalue": "1e-9"}),
    }


def test_a_unit_that_discovers_no_new_parameter_set_writes_no_registry_batch(
    tmp_path,
):
    seq = "ACGTACGTACGT"
    src = _write_parquet(
        tmp_path / "ann.parquet", {"dna_sequence": [seq], "function_id": ["f1"]}
    )
    out_root = tmp_path / "bronze"
    toml_text = _gene_result_toml(src, name="u", parameter_set="{}")
    cs.shard_manifest(cm.load_manifest(toml_text), out_root)
    reports = cs.shard_manifest(
        cm.load_manifest(
            _gene_result_toml(
                src, name="u2", parameter_set="{}",
                source_value="@const(bakta/2.0)",
            )
        ),
        out_root,
    )
    assert not [r for r in reports if r.table == PARAMETER_SET_TABLE]
    assert len(sorted((out_root / PARAMETER_SET_TABLE).glob("*.parquet"))) == 1


def test_every_result_rows_parameter_set_hash_has_a_registry_row(tmp_path):
    """The orphan check, run against the bytes on disk."""
    seq = "ACGTACGTACGT"
    src = _write_parquet(
        tmp_path / "ann.parquet", {"dna_sequence": [seq], "function_id": ["f1"]}
    )
    out_root = tmp_path / "bronze"
    cs.shard_manifest(
        cm.load_manifest(
            _gene_result_toml(src, name="u", parameter_set='{ evalue = "1e-5" }')
        ),
        out_root,
    )
    used = {r["parameter_set_hash"] for r in _read_all(out_root, "gene_result")}
    assert used
    assert used <= cs.discovered_parameter_sets(out_root)


# --------------------------------------------------------------------------
# 9. @len
# --------------------------------------------------------------------------


def test_len_on_a_list_returns_its_length(tmp_path):
    """``@len(contig_ids)`` on a two-contig genome must record 2, not the 14
    characters of ``str(["c1", "c2"])``. The worked example uses exactly this."""
    derivation = cm.parse_derivation("@len(contig_ids)", where="test")
    assert cs._derive(derivation, {"contig_ids": ["c1", "c2"]}) == 2
    assert cs._derive(derivation, {"contig_ids": ("c1", "c2", "c3")}) == 3
    # A scalar is still stringified, which is what seq_length needs.
    assert cs._derive(derivation, {"contig_ids": "ACGT"}) == 4
    assert cs._derive(derivation, {"contig_ids": 12345}) == 5
    assert cs._derive(derivation, {"contig_ids": None}) is None


def test_len_on_a_list_reaches_the_written_parquet(tmp_path):
    src = _write_parquet(
        tmp_path / "genomes.parquet",
        {
            "genome_hash": ["a" * 64],
            "contig_ids": [["c1", "c2", "c3"]],
            "fasta_path": ["/x"],
        },
    )
    toml_text = f"""
[[source]]
name = "genomes"
adapter = "file"
path = "{src}"
format = "parquet"
entity_type = "genome"
kinds = ["content"]

  [source.hash]
  precomputed = "genome_hash"

  [source.content]
  fasta_reference = "fasta_path"
  n_contigs       = "@len(contig_ids)"
"""
    out_root = tmp_path / "bronze"
    cs.shard_manifest(cm.load_manifest(toml_text), out_root)
    (row,) = _read_all(out_root, "genome_content")
    assert row["n_contigs"] == 3


# --------------------------------------------------------------------------
# 10. The worked example still plans
# --------------------------------------------------------------------------


def test_the_gaa_example_manifest_still_plans():
    example = (
        Path(cs.__file__).parent / "examples" / "gaa_store_parquet_dump.toml"
    )
    assert example.is_file()
    plans = cm.shard_plan(cm.load_manifest(example))
    assert {p.table for p in plans} == {
        "gene_entity",
        "gene_content",
        "gene_result",
        "function_entity",
        "function_content",
        "genome_entity",
        "genome_content",
    }
