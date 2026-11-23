# `kbu dataset` — the KBDL dataset mirror

KBDL serves **versioned, full-snapshot parquet copies** of selected BERDL
lakehouse (Iceberg) tables to external partners. This page documents the
operator CLI over that mirror: the six verbs, which machine each one runs on,
what `prune` and `pull` actually do, and the gates that stand in front of a
first publish.

Two halves sit behind it, and neither imports the other:

| half | module | what it owns |
|---|---|---|
| pod-side publisher | `kbutillib/domains/kbase/berdl/dataset_publish.py` | export, pinning, counting, the manifest, upload, registration |
| HTTP client | `kbutillib/domains/external/kbdl_service_utils.py` | the `/datasets` routes and `/objects/{id}/content` |

`DatasetPublisher` is written against a `KBDLPort` **protocol** rather than an
import of the client, and the real `KBDLServiceUtils` satisfies it
structurally. `tests/external/test_kbdl_dataset_client.py` proves that at
runtime by running `status()` through a real client over a faked session —
a Protocol alone proves nothing at import time.

---

## The locus table

| verb | runs | why |
|---|---|---|
| `list` | **anywhere** | one `GET /datasets` |
| `status <name>` | **IN-POD ONLY** | reads the Iceberg snapshot history through Spark |
| `publish <name>` | **IN-POD ONLY** | exports the pinned snapshot through Spark to MinIO |
| `pull <name>` | **anywhere** | HTTP plus the local filesystem |
| `hold <name> <version>` | **anywhere** | one HTTP POST |
| `prune <name>` | **anywhere** | HTTP POSTs only |

"In-pod" means inside the BERDL JupyterHub pod. The locus is detected by
testing whether `berdl_notebook_utils` is importable — the same probe
`BerdlCapability.locus()` uses — and **never** from an environment variable,
because an env var set in a laptop shell is how an off-pod process talks
itself into believing it has a Spark session.

`status` and `publish` refuse off-pod **before any transport call**, naming
the locus as the reason, with the message shape
`BerdlLoadRefusedError` carries for the `kbu clearinghouse load` verbs (that
exception is the raised `ClickException`'s `__cause__`). Nothing is exported,
nothing staged, and `_publisher()` — which would reach for a Spark session and
a MinIO client — is never even constructed.

---

## The `--json` output contract

Identical to `kbu clearinghouse`'s. Under `--json` **every verb emits exactly
one object** on stdout:

```json
{"schema_version": 1, "verb": "pull", "generated_at": "2026-11-23T20:00:00Z",
 "locus": "off_pod", "data": {...}, "warnings": [...]}
```

1. **Human output is a rendering of the same dict.** Each verb builds one
   structure and then either serialises it or hands it to `_render`. There is
   no second code path that computes anything.
2. `schema_version` is bumped only on a **breaking** change to `data`.
   Additive fields do not bump it; a consumer pinning major `1` must tolerate
   unknown keys.
3. **Stdout discipline.** Under `--json`, stdout carries the envelope and
   nothing else — no banner, no progress line, no warning text. Warnings go to
   stderr *and* into the envelope's `warnings` list (that is where a dashboard
   reads them).
4. **A refusal is not an envelope.** Every refusal — off-pod, a retired
   version, an existing `--dest`, a failed digest, a usage error — exits
   non-zero with the reason on **stderr** and **no object on stdout**, in both
   human and `--json` mode. A half-built envelope describing a refusal would
   be worse than none, because it would parse.

---

## `list`

```
kbu dataset list [--json]
```

Every mirrored dataset with `latest_version`, `latest_published_at` and
`version_count`. A dataset that has never had a version registered still
appears (with nulls) and is named in `warnings`.

## `status`

```
kbu dataset status <name> [--json]        # IN-POD ONLY
```

Where the table stands against its latest registered version, as one of five
answers: `no_versions`, `unchanged`, `compaction_only` (everything since the
published snapshot is a `replace`, i.e. compaction that changed no rows),
`changed`, or `unknown` (the parent chain ran out before the published
snapshot — a rollback, an expired snapshot, or a rebuilt table).

The target snapshot is read **once** and returned in the result, so a later
`publish` can be reasoned about against the same snapshot this verdict was
computed for.

## `publish`

```
kbu dataset publish <name> [--dry-run] [--force] [--keep-workdir] [--json]
```

Export, verify, upload and register **one pinned snapshot**. See
`dataset_publish.py` for the step order; the flags:

- `--dry-run` **always** returns a full report, whatever the status and
  whatever the approval state, and calls no export, staging, download or
  upload method. A gate that hid the plan would make the approval decision
  harder rather than safer.
- `--force` overrides only the `unchanged`/`compaction_only` skip.
- `--keep-workdir` keeps the local export directory after success.

**Registration is the only step that publishes anything.** Every upload is
private; an aborted publish discloses nothing.

### The publish gates

These are not enforced by flags alone and an agent must not satisfy any of
them on its own:

1. **The catalog's README approval fields must be filled by a reviewed
   commit.** `readme_approved_by` / `readme_approved_on` in
   `datasets.toml` ship **empty** and are the *only* publication gate — there
   is deliberately no separate release-authorisation or steward field, because
   publication rests on the PI's authority over the tables he manages. An
   empty field refuses a real publish (a dry run still reports). **Filling one
   is a human act, recorded in git by the human who did it.**
2. **Publish only after the source table's repair is done.** Mirroring a table
   that is known to need a fix exports the fault to partners under a version
   number that cannot be taken back — retirement withdraws it, but anyone who
   already pulled it has it.
3. **First publishes run in an attended session, not a 30-minute poll wake.**
   The first version of a dataset has nothing to compare against, so none of
   the lineage checks can tell a correct export from a wrong one; a human has
   to look at the row counts and the schema. An unattended wake that publishes
   a first version is publishing something nobody read.
4. **A schema-only or README-only change needs `--force`.** Neither changes a
   row, so the status is `unchanged` or `compaction_only` and the publish
   would otherwise skip. That is the correct default — most such runs are
   re-runs — so the override has to be asked for explicitly.

## `pull`

```
kbu dataset pull <name> [--version N] --dest DIR [--json]
```

Downloads a version and installs it at `--dest`, verifying every byte first.
**This verb never overwrites anything.** The order of operations *is* the
safety property:

1. **`--dest` must not exist.** Checked with `os.path.lexists`, so an empty
   directory, a file, a live symlink and a **broken** symlink are all refused
   alike. A broken symlink is the one case `Path.exists()` reports as absent,
   and renaming onto it would follow it somewhere nobody asked for.
2. **The version is fetched** (`latest` by default). A **retired** version
   (HTTP 410 → `KBDLGoneError`) exits non-zero printing the tombstone: a
   withdrawn snapshot must never land on a partner's disk looking current.
3. **A sibling working directory `W = <dest>.kbu-partial`** is created with
   `exist_ok=False`, holding `pull.json` = `{"manifest_object_id": ...}`.
   Everything is downloaded into W, so a partial pull is never visible at the
   path a partner was told to read.
   - W already there with the **same** manifest id → **resume** into it.
   - W already there with a **different** id, or with **no** `pull.json` →
     **refused by name**, and never deleted. A directory this tool did not
     create, and whose contents it cannot account for, is not its to remove.
4. **The manifest object's own bytes** are written to `W/MANIFEST.json`
   verbatim — **never re-serialised**. The partner verifies their copy the
   same way: `sha256(MANIFEST.json) == manifest_object_id`. Any re-encoding
   (key order, separators, unicode escaping) breaks that while leaving a file
   that still parses to the same dict. `W/README.md` is written from
   `manifest["readme"]`.
5. **Every file**, in manifest order:
   - the name must match `^part-[0-9]{5}\.parquet$` — a manifest does not get
     to name the file it is written to;
   - the bytes must hash to the object id (`download_object`'s
     content-addressing check, which hashes as it streams and renames only
     after);
   - the **parquet footer's `num_rows`** must equal the manifest's
     `row_count` for that file. This is what catches a file that is *intact
     but wrong* — the right bytes of the wrong export.
   - the footers must also total the manifest's own `row_count`.
6. **Only then the install:** remove `W/pull.json`, re-check that `--dest`
   still does not exist, and `os.rename(W, dest)` — **one** rename, so `dest`
   appears whole or not at all. Never a copy, which would be observable
   half-done. If `--dest` appeared in the meantime, the install is **refused**
   and W is left intact with everything verified in it; move the path that
   appeared aside and rename W yourself, or pull again to a free `--dest`.

**Resume is by digest, not by presence.** A file already in W is accepted only
if its sha256 *is* its object id. A truncated file from a killed run is not
evidence of anything, so it is re-downloaded. On **any** failure W is left
exactly as it is and the message says so: re-running the same command resumes
it rather than starting over.

## `hold`

```
kbu dataset hold <name> <version> (--reason TEXT | --release) [--json]
```

A hold exempts a version from retirement and reclamation. **Exactly one** of
`--reason` and `--release` is required; neither, or both, is a click usage
error (exit 2) and makes no HTTP call. There is no safe default: a hold with
no reason is a hold nobody can later justify removing, and treating a bare
`hold` as a release would silently unprotect a version somebody deliberately
pinned.

`--release` sends `{"reason": null}` — the explicit JSON null **is** the
release instruction, not "no reason supplied".

## `prune`

```
kbu dataset prune <name> --keep N [--apply] [--json]
```

`--keep` **must be at least 1** (usage error otherwise). Zero is not a
permitted "retire everything": the newest version is what partners read, the
service answers 409 to retiring it, and accepting `--keep 0` would make a typo
look like a policy.

**The plan.** Published versions are listed **oldest first**. The newest `N`
are kept, and **every held version is kept** however old — a hold exists
precisely to exempt a version from this, so held versions are filtered out of
the retire list here rather than left to fail at the service. Already-retired
versions are neither candidates nor counted toward `N` (`N` counts what
partners can still read).

**Without `--apply` nothing changes.** The only call made is

```
reclaim_dataset(name, dry_run=True, assume_retired=<exactly the versions the plan would retire>)
```

`assume_retired` is what makes the preview honest. A dry run without it would
report the reclaim of an empty retire list — i.e. nothing — and tell the
operator nothing about the bytes `--apply` would free. It is valid **only**
with `dry_run`; the client refuses it otherwise without making a request.

**With `--apply` the order is retire-then-reclaim and never the reverse.**
Reclamation only ever deletes objects of versions that are *already* retired,
so the retires are the authorisation for the delete:

1. retire each planned version, oldest first, each its own request, and
   **stop at the first error** — a 409 (the latest, or a hold placed since the
   plan was computed) means the plan is stale, and pressing on would act on a
   plan the service has already contradicted. The verb exits non-zero and the
   envelope carries `data.error`.
2. then `reclaim_dataset(name, dry_run=False)` — once — over whatever did
   retire, with no `assume_retired`.

Retirement is **metadata only and deletes nothing**; the bytes go only on a
reclaim without `dry_run`. Separating the two is what makes a mistaken
retirement recoverable. `kept_foreign` in the reclaim report is objects a
version references but does not own — never deleted.

---

## The client methods behind the verbs

All seven need a valid KBase token; the four POSTs additionally need the
caller on the service's `KBDL_DATASET_PUBLISHERS` list and answer **403**
(`KBDLNotAuthorizedError`) otherwise — a *missing permission*, not a stale
token, which is why it is a different type from 401.

| method | route | notable status codes |
|---|---|---|
| `list_datasets()` | `GET /datasets` | |
| `get_dataset_version(name, version="latest")` | `GET /datasets/{name}/latest` or `.../versions/{v}` | **410 → `KBDLGoneError`** (retired; carries the whole body and its tombstone), 404 → `KBDLNotFoundError` |
| `list_dataset_versions(name)` | `GET /datasets/{name}/versions` | newest first, retired included, no manifests; the client reorders nothing |
| `publish_dataset_version(name, manifest_object_id)` | `POST /datasets/{name}/versions` | 201 new, **200 idempotent repeat**, **409 + tombstone → error, never success** |
| `retire_dataset_version(name, version, reason)` | `POST .../retire` | 409 for the latest or a held version; 200 no-op when already retired |
| `hold_dataset_version(name, version, reason)` | `POST .../hold` | `reason=None` sends `{"reason": null}` and releases |
| `reclaim_dataset(name, dry_run=True, assume_retired=None)` | `POST /datasets/{name}/reclaim` | `dry_run` defaults **True**; `assume_retired` without `dry_run` is refused locally |

**410 is deliberately not 404.** "Withdrawn" and "never existed" lead to
different operator actions and only the first has a tombstone; a puller that
caught `KBDLNotFoundError` for both would report a retired version as
nonexistent.

**A 409 carrying a tombstone is never success.** On a version POST it means
the posted manifest object id belonged to a version that has since been
retired: the id is permanently spent and no retry can ever succeed. Reported
as an idempotent repeat, the operator would believe a withdrawn snapshot is
live.

---

## Tests

| file | covers |
|---|---|
| `tests/external/test_kbdl_dataset_client.py` | the seven methods against a faked `requests.Session` on the real client: routes, bodies, 410 → `KBDLGoneError`, 409-with-tombstone, `hold(None)` → explicit null, `assume_retired` refusal; plus `DatasetPublisher.status()` through a real client |
| `tests/cli/test_dataset_cli.py` | `pull` against real zstd parquet with measured sha256 ids (success, corruption, footer mismatch, bad manifest digest, every existing-`--dest` form, leftover W, the pre-rename race, interruption and resume), `prune`'s plan and preview, `hold`'s usage errors, the locus gate, and the one-object-per-verb contract |

The `pull` tests drive the **real** `KBDLServiceUtils` with only the HTTP
session faked — not a fake client object — so `download_object`'s streaming,
resume-by-digest and content-addressing check are the code under test.
