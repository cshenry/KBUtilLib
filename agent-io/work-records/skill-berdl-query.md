# Work Record: skill-berdl-query

## task_id
skill-berdl-query

## branch
conductor/berdl-lakehouse-skills/skill-berdl-query

## commit_shas
- 531b66de9edb395267a9903a93f395f7f5205734 — docs(skills): add berdl-query skill

## summary
Authors `agent-io/skills/berdl-query.md`, the `berdl-query` skill from
`agent-io/prds/berdl-lakehouse-skills/fullprompt.md` ("Skills"). The skill
covers the locus-transparent read path — discovery, Trino/Spark/REST
routing, the `my`/`{username}` personal-catalog alias trap, the dual
dotted/underscored dataset-naming problem, cross-catalog joins, and Iceberg
time travel/snapshot inspection — and calls into the already-landed
capability layer (`BerdlCapability.query()`, `BerdlCapability.databases()`,
`naming.to_trino_alias`/`to_spark_alias`/`normalize_databases`) rather than
reimplementing any of that logic inline. Frontmatter (`name`, `description`,
`scope: domain`) matches the convention already used by every other skill in
`agent-io/skills/` (`kbutillib-expert.md`, `kb-sdk-dev.md`, etc.).

## files_touched
- `agent-io/skills/berdl-query.md` — new (287 lines)

## success_criteria_check
- **`berdl-query` skill markdown exists with conforming frontmatter** — PASS. `agent-io/skills/berdl-query.md` has `name: BERDL Query`, a one-line `description`, and `scope: domain`, matching the pattern in every sibling skill file (checked `kbutillib-expert.md`, `kb-sdk-dev.md`, `kbase-genome-expert.md`, `kbutillib-dev.md`, `msmodelutl-expert.md`).
- **Trino-vs-Spark routing, Trino read-only and rejecting writes** — PASS. §1 ("Engine routing") tables Trino/Spark/REST by locus and write-capability, states Trino "rejects `INSERT`, `CREATE`, `DROP`, and every other write statement," and explicitly directs writes to `BerdlCapability.load()` instead of `query()`.
- **`my`/`{username}` alias trap, translation routed through the naming layer** — PASS. §2 states the trap (Spark `"my"` vs. Trino `{username}`, silent failure across engines) and requires calling `naming.to_trino_alias`/`to_spark_alias` rather than ad hoc string manipulation; explicitly warns against hand string-replacing `"my"` or the username.
- **Dual dotted/underscored name problem — prefer dotted, mark underscored legacy, never present as unrelated** — PASS. §3 states all three rules verbatim against the PRD's own wording, explains `NormalizedDatabase` fields (`name`, `is_iceberg`, `legacy_alias`), and instructs never to call the raw platform list functions directly for discovery (always through `normalize_databases()`/`BerdlCapability.databases()`).
- **Discovery commands for both engines** — PASS. §4 covers `BerdlCapability.databases()`, the transport-level `tables()`/`table_schema()`, the `return_json=False` JSON-string trap for raw `berdl_notebook_utils` calls, and Trino `SHOW CATALOGS`/`SHOW SCHEMAS`/`SHOW TABLES`/`DESCRIBE` plus `information_schema.tables`/`information_schema.columns` examples.
- **Cross-catalog joins** — PASS. §5 gives a worked example joining a `to_trino_alias`-translated personal-catalog table against a tenant catalog table in one Trino query.
- **Iceberg time travel and snapshot/history/files inspection identified as the post-overwrite recovery path** — PASS. §6 opens by stating time travel is the recovery path after `load()`'s destructive `overwrite`/`createOrReplace()`, gives `.snapshots`/`.history`/`.files` queries and `VERSION AS OF`/`TIMESTAMP AS OF` examples (all via `engine="spark"`, since these are Iceberg SQL extensions Trino doesn't support through this skill's routed path), and states time travel is read-only (does not itself roll the table back).
- **Off-pod REST exposes tenant catalogs only, not yet proven end to end** — PASS. §7 states both caveats explicitly and separately: (1) the personal catalog did not appear off-pod at design time, so off-pod discovery of personal tables must not be promised; (2) off-pod token resolution is unverified on a real off-pod machine even though the REST route itself authenticates and needs no SSH tunnel.
- **No Python module modified** — PASS. `git diff --stat` against `main` shows only the one new markdown file; no `.py` files were touched.

## tests_run
None. Per the task's mandatory process rules, this task modifies no Python
and no test run was required or performed. As a lightweight sanity check
only (not a substitute for tests), I confirmed by reading
`src/kbutillib/domains/kbase/berdl/capability.py`,
`transports.py`, and `naming.py` that every function/method the skill
references by name (`BerdlCapability.query`, `BerdlCapability.databases`,
`InPodTransport.tables`, `InPodTransport.table_schema`,
`InPodTransport.trino_connection`, `naming.to_trino_alias`,
`naming.to_spark_alias`, `naming.normalize_databases`,
`NormalizedDatabase.name`/`.is_iceberg`/`.legacy_alias`) exists with a
matching signature on `main` as of this branch's base commit.

## caveats
- `BerdlCapability` does not currently forward `tables()`/`table_schema()`
  at its own top level (only `databases()`, `memberships()`, `load()`,
  `query()` — confirmed by reading `capability.py`). §4 of the skill notes
  this and shows calling those two on the transport directly
  (`cap._get_transport().tables(...)`), including the private-looking
  `_get_transport()` accessor, since there is no public alternative today.
  If a future revision of `capability.py` adds top-level
  `tables()`/`table_schema()` wrappers, this skill should be updated to
  prefer those instead.
- Time-travel and snapshot-inspection examples route through
  `engine="spark"` rather than Trino. This follows the PRD's own placement
  ("Time travel... are Iceberg-only and belong to the query and load
  skills") and `capability.py`'s documented rationale for the `engine`
  kwarg ("needed for anything beyond a plain read, e.g. schema evolution or
  time-travel syntax that Trino does not support") — I did not independently
  verify Trino's `VERSION AS OF` support against a live cluster; the skill
  defers to Spark per that documented reasoning rather than asserting Trino
  cannot do it under any configuration.
- I did not coordinate with the sibling `berdl-session`, `berdl-load`, and
  `berdl-tenant` skill-authoring tasks (running concurrently on their own
  branches per the task instructions) beyond the one explicit cross-link:
  this skill tells the reader to read `berdl-session` first for locus
  detection and token resolution, and references `berdl-load` for the
  write-path/overwrite semantics that motivate time travel. I did not touch
  any other skill file.
