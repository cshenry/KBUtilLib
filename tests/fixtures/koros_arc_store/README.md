# koros_arc_store test fixtures

Local-only fixtures for the run-database tests. No network is used by any test.

- `runs_db/`   — sample SQLite run databases / seed material (built at test time).
- `subject_blobs/` — sample detail-blob JSON documents used to exercise the
  blob tier (`subject_detail`) without hand-writing them inline.

Names here are domain-neutral: a second app (model analysis) shares this store,
so nothing is named for annotation, genomes, models or flux.
