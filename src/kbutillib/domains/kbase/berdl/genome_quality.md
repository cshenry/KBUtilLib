DRAFT -- not approved for publication

# genome_clearhouse.genome_quality

One row per genome in the KBase genome clearinghouse, with its genome quality metrics.
This file set is a full snapshot of the lakehouse table `kbaseincubator.genome_clearhouse.genome_quality`,
pinned to the Iceberg snapshot id recorded in `MANIFEST.json` (`source.snapshot_id`).

## Columns to read first

- `released` (boolean). `true` means the genome is part of the released genome inventory.
  1,634,825 SPIRE metagenome bins that SPIRE itself did not release are kept in the table with `released = false`;
  they were flagged, not deleted, so nothing is lost. Count genomes with `released = true`.
- `gtdb_release` (string). For a genome drawn from GTDB, the GTDB release it came from (`r220` or `r232`).
  It is not set for genomes from other sources.

## Provenance and verification

The manifest lists every file with its SHA-256 (equal to its KBDL object id), size and row count,
and records the row count checked three ways at export time.
