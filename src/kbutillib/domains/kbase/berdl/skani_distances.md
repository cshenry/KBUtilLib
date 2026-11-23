DRAFT -- not approved for publication

# genome_clearhouse.skani_distances

skani average-nucleotide-identity results for the genomes in `genome_clearhouse.genome_quality`.
This file set is a full snapshot of the lakehouse table `kbaseincubator.genome_clearhouse.skani_distances`,
pinned to the Iceberg snapshot id recorded in `MANIFEST.json` (`source.snapshot_id`).

## This is NOT a pairwise distance table

There is one row per genome per reference database (`db_name`).
Each row carries `n_hits`, `top_hit` and `top_ani`, and a `distances` string column that holds the serialised list of hits.
To get pairs, parse `distances`.

## Relationship to genome_quality

Join on the genome identifier. SPIRE bins with `released = false` in `genome_quality` may have no row here.

## Provenance and verification

The manifest lists every file with its SHA-256 (equal to its KBDL object id), size and row count,
and records the row count checked three ways at export time.
