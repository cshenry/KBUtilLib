# Work Record: skill-berdl-load

## task_id
skill-berdl-load

## branch
conductor/berdl-lakehouse-skills/skill-berdl-load

## commit_shas
- ee3c972ed845e56f49eda5c8ccfdbe1271154651 — docs(skills): add berdl-load skill

## summary
Adds `agent-io/skills/berdl-load.md`, the `berdl-load` skill markdown file
called for in `agent-io/prds/berdl-lakehouse-skills/fullprompt.md` ("Skills").
The skill documents in-pod-only BERDL loading through the existing
`BerdlCapability` deep module (`src/kbutillib/domains/kbase/berdl/
capability.py`): the off-pod refusal (locus resolved first, refusal names
the reason, `kbhub`, and a concrete `BerdlCapability().load(...)` command,
and explicitly forbids staging or dispatching anything off-pod); the
read-write-membership preflight and its asynchronous Slack-approval
consequence; DataFrame-mode vs. bronze-mode source routing (DataFrame mode
omits the `'paths'` config section entirely, bronze mode requires
`paths.bronze_base` plus per-table `bronze_path`/`format`); write semantics
(`append` to a non-existent table raises inside `ingest`, so `load()`
substitutes `overwrite` for first creation; `overwrite` maps to a
destructive `createOrReplace()` recoverable only via Iceberg snapshot time
travel; `partition_by` accepts a string or list); postflight verification
by row count and Iceberg snapshot history; and namespace lifecycle,
including purge-ordered teardown (enumerate tables as a Python list, `DROP
TABLE ... PURGE` each — with an explicit warning that omitting `PURGE`
orphans S3 data files — then `DROP NAMESPACE`, with an explicit-display and
explicit-confirmation requirement before any teardown runs) and Iceberg
schema evolution (add/rename/drop column, metadata-only, no data rewrite).
The skill directs the reader to `BerdlCapability` for everything that
module already implements (locus, membership, load, postflight) and only
falls back to direct `InPodTransport`/Spark-SQL calls for namespace
teardown and schema evolution, which `capability.py` does not wrap — it
does not reimplement any of the capability layer's preflight/refusal/
mode-selection logic. Frontmatter (`name`/`description`/`scope: domain`)
and body structure (numbered sections, quick-reference code block, related-
skills footer) follow the convention already used by the other `agent-io/
skills/*.md` files in this repo (e.g. `kbase-genome-expert.md`).

## files_touched
- `agent-io/skills/berdl-load.md` — new skill file (393 lines)

## success_criteria_check
- **A berdl-load skill markdown file exists in KBUtilLib's agent-io/skills/ with conforming frontmatter** — PASS. `agent-io/skills/berdl-load.md` with `name`/`description`/`scope: domain` frontmatter matching the format of every other skill file in the directory.
- **Off-pod refusal names the pod requirement, kbhub, and a concrete command; explicitly forbids staging or dispatching** — PASS. Section 2 ("In-Pod Only: The Off-Pod Refusal") states the three things the raised message names (reason/Spark-only-in-pod, `kbhub`, a runnable command) and explicitly instructs the reader to stop rather than stage files, build a config, or hand the load off to another process.
- **Read-write membership precondition checked before staging, with the async Slack approval consequence stated** — PASS. Section 3a states rw/ro are different groups, shows the `memberships()` check, and states the approval is "an asynchronous human approval step via Slack, typically same-day during business hours but not instant," contrasted against a five-second retry.
- **Both source modes documented, DataFrame mode omitting 'paths', bronze mode requiring bronze_base** — PASS. Section 4 covers both with code examples; DataFrame mode's prose states `'paths'` is omitted (dropped even if passed); bronze mode's code example includes `paths={"bronze_base": ...}` and prose states it is required.
- **Append-to-nonexistent-raises rule, first creation must use overwrite** — PASS. Section 5, first bullet, states the `ValueError` and that `load()` substitutes `overwrite` for first creation.
- **Overwrite stated as destructive full replace** — PASS. Section 5, second bullet: "`overwrite` maps to `createOrReplace()` — a full, destructive replace... not a merge and not an upsert," with recovery only via snapshot time travel.
- **Postflight verification by row count and snapshot** — PASS. Section 6 documents both checks and the returned report shape, with a caveat about not trusting a `None` result silently.
- **Purge-ordered teardown, explicit warning that omitting PURGE orphans S3 files, and that teardown requires confirmation** — PASS. Section 7b gives the three-step order (enumerate-as-list, `PURGE` each, then `DROP NAMESPACE`), states PURGE is not optional and that omitting it "silently orphans the table's S3 data files," and includes a worked example with an explicit "STOP: display exactly what will be deleted and get explicit confirmation" block before any destructive call.
- **No Python module is modified** — PASS. `git diff --stat` against `main` shows exactly one file, `agent-io/skills/berdl-load.md`; no file under `src/` was touched.

## tests_run
None. This task adds only a markdown file and modifies no Python; per the
task's mandatory process rules, no test run was required or attempted. No
linter applies to markdown in this repo's CI (`ruff`/`mypy` target
`src/kbutillib/`; the skill file is not under that tree).

## caveats
- Namespace teardown and Iceberg schema evolution (Section 7) are **not**
  wrapped by `BerdlCapability` — `capability.py`'s docstring and PRD
  interface list only cover `locus`, `databases`, `memberships`, `load`,
  `query`. For those two operations the skill documents direct
  `InPodTransport.remove_table`/`spark.sql(...)` calls instead, consistent
  with the PRD's statement that `berdl-load` "owns namespace lifecycle
  including purge-ordered teardown and schema evolution" as skill-level
  knowledge, not necessarily capability-module-wrapped behavior. This is a
  judgment call I resolved by only routing through `BerdlCapability` where
  it actually has a corresponding method, and falling back to the
  documented import-map/Spark-SQL surface (per `/berdl-session`'s import
  map and `api-reference.md`) everywhere else — never inventing a
  `BerdlCapability` method that does not exist in `capability.py`.
- `list_namespaces`/`list_tables` (top-level `berdl_notebook_utils`
  functions named in the PRD's import map) have no harvested signature in
  `api-reference.md`, so Section 7b's enumeration example uses
  `spark.sql("SHOW TABLES IN <namespace>")` instead of a named
  `list_tables()` call — both return the same information, and the SQL
  form does not depend on an unverified `return_json` default. The prose
  still calls out the JSON-string-vs-Python-list trap generally, since it
  is the same trap documented for `get_databases`/`get_tables`/
  `get_table_schema` in `transports.py` and `api-reference.md`.
- `ALTER TABLE ... ADD/RENAME/DROP COLUMN` (Section 7c) is standard Iceberg
  Spark-SQL DDL, not a wrapped Python helper in this codebase either
  (nothing in `transports.py` or `capability.py` covers schema evolution).
  Documented as direct `spark.sql(...)` calls for the same reason as 7b.
- I discovered mid-task that my first `Write` call landed at
  `/Users/chenry/Dropbox/Projects/KBUtilLib/agent-io/skills/berdl-load.md`
  (the Dropbox parking repo's working tree, not the worktree) — a direct
  violation of the hard rule against editing that tree. It was caught
  immediately (before any other tool call), confirmed untracked/unstaged
  via `git status`, and moved into the correct worktree path with `mv`
  rather than deleted-and-rewritten, so no data was lost and the Dropbox
  working tree was left clean. Flagging this explicitly in case the
  reviewer wants to double check `git -C ~/Dropbox/Projects/KBUtilLib
  status` shows nothing under `agent-io/skills/berdl-load.md`.
