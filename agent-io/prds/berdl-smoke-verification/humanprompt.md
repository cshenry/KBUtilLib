# Design session: prove the BERDL skills actually work

Run this with `/ai-design`. It needs **two** sessions — one **on kbhub**
(in-pod) and one **on primary-laptop** (off-pod). Start with the in-pod one.

## Goal

The four `kbu-dl*` skills and the `BerdlCapability` layer are built, merged, and
deployed — but **they have never been exercised against live BERDL**. That was a
deliberate PRD decision (pure-logic tests only; transports and skills
hand-verified after merge), and the hand-verification never happened.

Two outcomes:

1. **In-pod smoke test** — drive all four skills end-to-end against a scratch
   namespace, then tear it down cleanly.
2. **Off-pod validation** — confirm what the off-pod half can and cannot actually
   do from primary-laptop.

## Decisions already made — do not relitigate

- **Writes are in-pod only.** Off-pod `load()` hard-fails with guidance. That is
  by design; do not add a dispatch or notebook-generation path.
- **Tests stay pure-logic.** Do not convert this smoke test into an automated
  test that runs in CI — it needs live credentials and a real pod. If it becomes
  a script, it is an operator tool, not part of `pytest`.
- **Both target tenants are read-write** for chenry (`aiale`, `kbaseincubator`),
  verified 2026-08-01. No access request is needed.
- **Teardown is purge-ordered.** `DROP TABLE ... PURGE` every table, then
  `DROP NAMESPACE`. Iceberg/Polaris has no `CASCADE`, and skipping `PURGE`
  orphans S3 files.

## Known environment state — verify before trusting

- **kbhub's Spark Connect may be in a zombie state.** On 2026-08-03 a test run
  hit: *"the server is listening but not serving sessions (likely a half-broken/
  zombie driver)"*, 90s timeout. Before any live work, run the **non-rotating**
  repair first — `get_credentials()` then
  `start_spark_connect_server(force_restart=True)` — and only escalate to
  `refresh_spark_environment()` if that fails, because the rotating fix
  invalidates credentials in use by other kernels and jobs.
- **`~/.kbase/token` on kbhub is 32 bytes and appears stale/invalid.**
  `KBASE_AUTH_TOKEN` in the pod environment works. `tokens.py` resolves env
  first, which is correct — but the stale file is still there and will be the
  fallback off-pod.

## In-pod session — what to exercise

Use a scratch namespace you own, not an existing dataset. Prefer the personal
catalog first, then one tenant write to `aiale` or `kbaseincubator`.

Exercise the paths most likely to be wrong — these are the traps found at design
time, each documented in `agent-io/prds/berdl-lakehouse-skills/api-reference.md`:

1. **`get_trino_connection` defaults to `connector='delta_lake'`, not Iceberg.**
   Confirm `kbu-dlquery` passes the connector explicitly and does not silently
   read through the legacy connector.
2. **`get_databases` / `get_tables` / `get_table_schema` default to
   `return_json=True`** and return a JSON *string*. Confirm nothing iterates the
   string as if it were a list.
3. **`append` to a non-existent table raises `ValueError`.** Confirm the loader
   detects non-existence and chooses `overwrite` for first creation, so a
   re-runnable load does not fail on first execution. Run the same load twice.
4. **The `my` alias is Spark-only; Trino needs `{username}`.** Write via Spark,
   then read the same table through Trino and confirm the alias translation
   happens rather than erroring.
5. **Both source modes.** DataFrame mode (`dataframes=`, no `paths`) and bronze
   mode (files + `paths.bronze_base`). Confirm DataFrame mode does no MinIO
   staging.
6. **Preflight gate.** Confirm a read-write check happens *before* any data is
   staged. If you can, try a tenant you only hold read-only on (`enigma`,
   `refdata`) and confirm it refuses early and clearly.
7. **Postflight verification** — row count and snapshot history.
8. **Purge-ordered teardown**, then confirm via `aws s3 ls` that the data files
   are actually gone, not just the catalog entry.

## Off-pod session — what to answer

Run on primary-laptop, where `berdl_notebook_utils` does not exist.

1. **Does token resolution actually work off-pod?** `tokens.py` falls back to
   `~/.kbase/token`. On kbhub that file is stale; check the laptop's copy and
   whether `KBBERDLUtils`-style auth succeeds. This has never been run off-pod.
2. **Does the REST surface expose the personal catalog?** At design time it
   returned tenant catalogs only — `chenry` had zero hits. Confirm, and if so,
   make sure `kbu-dlquery` says so rather than silently returning nothing.
3. **Does the subpackage import cleanly without the pod package?** This is the
   `berdl-transports` success criterion that *cannot* be verified in-pod. It
   should now be covered by the `force_off_pod` fixture, but confirm the real
   import too.
4. **Does `load()` refuse correctly**, naming the pod requirement, kbhub, and a
   concrete command — against the real environment, not a fixture.
5. **Dual-name disambiguation.** The REST list returns both dotted Iceberg and
   underscored legacy Delta names for the same dataset (`aiale.dataset1` /
   `aiale_dataset1`). Confirm the normalization prefers dotted and marks
   underscored as legacy rather than presenting them as unrelated.

## Open questions for the sessions

1. **Should the smoke test become a committed operator script** (e.g.
   `kbu dl smoke`), or stay a documented manual procedure? A script is
   repeatable; it also needs an owner and can rot.
2. **Which tenant for the write test** — `aiale` or `kbaseincubator`? AI-ALE holds
   collaborator material; a scratch namespace there may be undesirable even
   temporarily.
3. **What should off-pod do about the personal catalog gap** — fail loudly, warn
   and continue, or transparently route the user to the pod?
4. **Is a read-only-tenant refusal test worth running for real?** It is the
   cleanest way to prove the preflight gate, but it deliberately triggers a
   permission failure against a shared tenant.

## Deliverables

1. A written record of what passed and what failed, per numbered item above.
2. Any defect found filed as a decision in project state (`kbutillib`), with
   enough detail to fix without re-deriving.
3. A decision on each open question.
4. If the traps in `api-reference.md` are confirmed handled correctly, say so
   explicitly — a clean result is a result worth recording.

## Sequencing note

Do the **in-pod** session first. If the write path is broken, the off-pod session
mostly tests refusal messages and can wait. Also fix the Spark Connect zombie
state before starting, or the first several steps will time out at 90s each and
tell you nothing.

## Context

- PRD: `KBUtilLib/agent-io/prds/berdl-lakehouse-skills/fullprompt.md`
- Harvested signatures and the six traps:
  `KBUtilLib/agent-io/prds/berdl-lakehouse-skills/api-reference.md`
- Design decisions: AIAssistant project state, project id `kbase`
- Test-isolation defect found and fixed 2026-08-03: project id `kbutillib`
