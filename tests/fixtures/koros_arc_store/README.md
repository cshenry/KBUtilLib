# koros_arc_store test fixtures

Local-only fixtures for the runs-tree, run-database and contract tests. No
network is used by any test.

- `runs_tree/`   — an on-disk KOROS runs tree for the read-only enumeration
  path. It contains:
  - `proj_valid/arcs/arc_ok/PROVENANCE.json`   — a VALID provenance with empty
    `inputs` and `tool_versions` (the common live shape) plus an undocumented
    upstream key preserved in `raw`.
  - `proj_valid/arcs/arc_leg/PROVENANCE.json`   — a valid arc with `role: "leg"`
    and a populated `leg_of`.
  - `proj_valid/arcs/arc_bad/PROVENANCE.json`   — an UNPARSEABLE provenance
    (reads back as `valid=False`, `invalid_reason=json_parse_error`).
  - `proj_no_arcs/`                              — a project directory with NO
    `arcs/` subdirectory (reports `arc_count == 0`, `list_arcs` returns `[]`).
- `runs_db/`   — a seeded SQLite run database (`seeded.sqlite`), written by the
  real `RunDatabase` so its schema is authentic. Copy it into a temp path before
  opening if a test writes.
- `subject_blobs/` — sample detail-blob JSON documents used to exercise the
  blob tier (`subject_detail`) without hand-writing them inline.

Names here are domain-neutral: a second app (model analysis) shares this store,
so nothing is named for annotation, genomes, models or flux.
