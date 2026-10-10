# KBUtilLib Metabolic Modeling (kbu model)

You have a `kbu` CLI on PATH. Its `kbu model` verb group gives you
genome-scale metabolic reconstruction, gapfilling, FBA, and FVA, plus an
`exec` escape hatch for anything the verified verbs don't cover. This text
is injected into your orientation because your session has no `Skill` tool
— treat it as your onboarding for this capability. `kbu model` requires no
extra setup: the standard `kbu` interpreter already has kbutillib, cobra,
and modelseedpy importable, and every verb below runs with no KBase token
unless you explicitly pass a KBase workspace reference.

**One network call is not optional: `reconstruct` annotates.** If the genome
you hand it carries no functional roles — which is the case for every plain
protein FASTA — `reconstruct` calls the **RAST** service to annotate it
before building, so it is *not* offline for unannotated input. A genome that
already carries roles skips RAST and stays offline. `gapfill`, `fba` and
`fva` are always fully offline.

## The canonical arc

```
Genome → reconstruct → gapfill → fba (pFBA) → fva
```

Run the stages in that order. Each verb writes/reads a plain cobra JSON
model file (portable, inspectable) and supports `--json` for stable,
parseable output — always pass `--json` when you intend to consume the
result programmatically rather than read it as a human summary.

```
kbu model reconstruct --genome PATH --out MODEL.json [--template core|gn] [--model-id ID] [--atp-safe/--no-atp-safe] [--json]
kbu model gapfill --model MODEL.json --media MEDIA [--out MODEL.json] [--objective RXN_ID] [--atp-safe/--no-atp-safe] [--json]
kbu model fba --model MODEL.json --media MEDIA [--objective DSL] [--top N] [--json]
kbu model fva --model MODEL.json --media MEDIA [--reactions ID,ID,...] [--fraction-of-optimum F] [--json]
kbu model exec SCRIPT.py [--timeout SECONDS] [--json] [-- ARGS...]
```

### `reconstruct` — genome → draft model

Builds a draft model from a local protein FASTA (`.faa`/`.fasta`) via
`MSReconstructionUtils.build_metabolic_model`. `--json` emits
`{"model_path", "reactions", "metabolites", "genes"}`.

```
kbu model reconstruct --genome demo_genome.faa --out draft.json --json
```

**It annotates first when it has to.** `MSGenome.from_fasta` leaves every
feature's roles empty, and the builder maps reactions *from roles only* — so
an unannotated FASTA is auto-annotated via RAST before building. You will see
`[annotate] …` on stderr when this happens. Measured on E. coli K-12
(4285 proteins): RAST assigned roles to 4163 of them in ~18s, giving
**1740 reactions / 1451 metabolites / 1307 genes**.

**Sanity-check the numbers, not the exit code.** A real bacterial draft has
on the order of 1000+ reactions and 1000+ genes. **If you ever see a model
with ~5 reactions and 0 genes, the reconstruction did not work** — that is
the bare template biomass scaffold (`bio1` plus DNA/protein/mRNA synthesis
and a biomass sink), which is what the builder emits when it has no roles to
map. `reconstruct` now refuses rather than returning that, but check
`genes > 0` anyway before you build on a model. Reconstruct exiting `0` is
not by itself evidence of a usable model.

### `gapfill` — make the model feasible on a media

Gapfills to feasibility via `MSReconstructionUtils.gapfill_metabolic_model`
(always tries against both offline templates, `core` and `gn`). `--json`
emits `{"model_in", "media", "model_out", "reactions_added": [...]}`. Note
`gapfill`'s `--objective` is a **bare reaction id** (default `bio1`), not
the `MAX{}`/`MIN{}` DSL used by `fba`/`fva` (see "Objective vocabulary"
below).

```
kbu model gapfill --model draft.json --media glucose_minimal.json --out gapfilled.json --json
```

### `fba` — run pFBA

Runs pFBA via `MSFBAUtils.run_fba`. `--json` emits `{"objective_value",
"fluxes": [{"id","value"}, ...], "solver_status"}`, `fluxes` capped at
`--top` (default 25) sorted by `|value|` descending.

```
kbu model fba --model gapfilled.json --media glucose_minimal.json --json
```

### `fva` — flux variability (the `run_fva` workaround)

Runs FVA via **`MSFBAUtils.run_fva`** — this is a deliberate workaround,
not a facade shortcut. **Never expect or ask for canonical
`cobra.flux_variability_analysis`; it is broken in this environment.**
`kbu model fva` always routes through `run_fva`, and if you are writing an
`exec` script that needs FVA, you must call `MSFBAUtils.run_fva` yourself
too — do not call `cobra.flux_analysis.flux_variability_analysis`.
`--json` emits `{"reactions": [{"id","min","max"}, ...]}`.

```
kbu model fva --model gapfilled.json --media glucose_minimal.json --json
```

### `exec` — escape hatch for arbitrary KBUtilLib API use

Runs an agent-written Python script inside the `kbu` interpreter, with the
full KBUtilLib/cobra/modelseedpy API available — not just the four
verified verbs. Every invocation is provenance-preserving: it runs in a
**durable** run directory (never a throwaway temp dir), so any relative
output files your script writes are captured, and a copy of the script
plus `stdout.txt`/`stderr.txt`/`run.json` (script hash, exit code,
timestamps, package versions) survive the run. `--json` emits `{"stdout",
"stderr", "exit_code", "run_dir"}`. A failing script yields a nonzero
`exit_code` in this envelope — `kbu model exec` itself exits 0 unless the
script path is invalid, so check `exit_code`, not the CLI's own exit
status.

```
kbu model exec analysis.py --json -- --some-arg value
```

## Decision rule: verified verb vs. `exec`

- If your task is exactly "reconstruct a model", "gapfill it", "run FBA",
  or "run FVA" on files you already have paths for — **use the verified
  verb**. It is tested, has a stable JSON schema, and needs no code.
- If you need something the four verbs don't do — inspecting model
  internals, custom media/objective construction beyond what `--media`/
  `--objective` accept, batching multiple models, plotting, or any other
  KBUtilLib API call — **write a script and run it via `kbu model exec`**.
  Before writing that script, read the broader KBUtilLib API reference
  (see "API reference" below) rather than guessing method signatures.
- Do not hand-roll subprocess calls to `cobra`/`modelseedpy` outside `kbu
  model` — you would lose the durable provenance record and the offline
  construction workaround (see "Local/offline construction" below) that
  makes reconstruction and gapfilling work without a KBase token.

## Going bigger: KBDL, the at-scale job service

Everything above runs **in this process, on this host**, and is sized for one
genome at a time. When the work is bigger than that, there is a deployed
asynchronous job service — **KBDL**, on poplar — that runs the same class of
work on real compute, plus tools that are not installed here at all.

**It needs no setup and NO SSH TUNNEL.** `KBDLServiceUtils` defaults to
`http://poplar.cels.anl.gov:8791` and is reached *directly* over the
ANL-internal network from this pod. You do not need to set `KBDL_SERVICE_URL`,
and you should not go looking for a tunnel — that posture was retired. Auth
reuses the same KBase token the rest of `kbu` uses, so a zero-argument
constructor is the normal case:

```python
from kbutillib.domains.external.kbdl_service_utils import KBDLServiceUtils
kbdl = KBDLServiceUtils(config_file=False)       # endpoint + token resolved for you
```

### Decision rule: local verb vs. KBDL

| Use the local `kbu model` verb | Hand it to KBDL |
|---|---|
| One genome / one model, now | Many genomes, or a batch |
| Seconds to a couple of minutes | Long jobs you should not hold a session open for |
| You want the cobra JSON on this disk | You want it run on poplar's compute |
| Annotation via RAST is enough | You need bakta, kofamscan, CheckM2, SKANI — **not installed here** |

If the local verb can do it, use the local verb — it is simpler, synchronous,
and you get a file. Reach for KBDL when the job is genuinely bigger than this
host, not as a default.

### There is no `kbu kbdl` CLI — go through `exec`

This is the part worth knowing up front: KBDL is a **Python API only**. There
is no CLI verb for it, so do not go hunting for `kbu kbdl ...`. Call it from a
script run through `kbu model exec`, which gives you the right interpreter and
the durable provenance directory:

```python
# kbdl_job.py  →  kbu model exec kbdl_job.py
from kbutillib.domains.external.kbdl_service_utils import (
    KBDLServiceUtils, JOB_TYPE_MODEL_RECONSTRUCTION,
)

kbdl = KBDLServiceUtils(config_file=False)
result = kbdl.submit_and_wait(
    JOB_TYPE_MODEL_RECONSTRUCTION,
    {"mapping": ..., "annotations": ..., "tools": ..., "gapfill_media": ...},
    interval=5.0, timeout=3600,
)
```

### The ten job types

`KBDLGenomeAnnotation`, **`KBDLModelReconstruction`**,
**`KBDLFitnessModelAnalysis`**, `KBDLSKANI`, `KBDLCheckM2`,
`KBDLBuildGenome`, `KBDLBuildSKANIDB`, `KBDLStoreLoad`, `KBDLUploadObject`,
`KBDLHorizyn` — importable as `JOB_TYPE_*` constants. The two in bold are the
modelling ones and the reason this section exists; each has a thin
`submit_<name>(**params)` helper returning a job id.

Two ways to run one:

- **`submit_and_wait(job_type, params, interval=, timeout=)`** — submit, poll,
  return the result. Raises `KBDLJobFailedError` on a failed terminal state.
  Use this unless you have a reason not to.
- **`submit_*(...)` → `poll_until_terminal(job_id)` → `get_job_result(job_id)`**
  — when you want to hold the id, report progress, or submit a batch and
  collect later. `check_job(job_id)` for a single status, `list_jobs()` for all,
  `clear_job(job_id)` to discard.

### The two SKANI databases that cover the Clearinghouse

For an ANI / genome-similarity search (`KBDLSKANI`, via `submit_skani`), **use
one of these two unless you have a specific reason not to.** Together they
capture the full diversity in our Clearinghouse — the novel genomes we have
assembled, and the reference tree:

| `skani_db` | What it covers | `object_id` |
|---|---|---|
| `novel_all_representatives` | Representatives across all our **novel** Clearinghouse genomes | `ref:1be8415b2a773d2632949cc46f1b68d3956c4ce67eab4088a9446fadf5157404` |
| `gtdb_r220_consolidated` | **GTDB r220**, consolidated layout — the reference side | `ref:cee67d7f33f57c07d569f9fb0a0537a0263094f380fa0b10faa56aafdfcc56ae` |

Both are `visibility: "public"`, so no upload or setup is needed — pass the
name as `skani_db`. Querying both and merging is the normal way to ask "is this
genome novel, and if not what is it closest to": a hit in
`gtdb_r220_consolidated` places it against the published reference tree, while
a hit only in `novel_all_representatives` means its nearest relative is
something *we* assembled.

> **Do not confuse `novel_all_representatives` with `novel_all_consolidated`.**
> Both exist in the object store and the names differ by one word. The
> representatives DB is the curated, de-replicated one and is what you want
> for a diversity-spanning search. `list_objects()` shows 14 `SKANIDB` objects
> in total; most of the rest are small test or legacy builds (`DB_022`,
> `DB_031`, `subset_test`, `some_old_lib`, …) and are **not** what you want.
> Verified against the live service 2026-10-05.

### The object store, and one category error to avoid

`upload_object(file, object_type, name, visibility)` uploads inputs;
`list_objects()`, `get_object_metadata(id)`, `list_archive_files(id)` and
`delete_object(id)` manage them.

- **You may upload:** `SKANIDB`, `ModelMapping`, `Media`, `GenomeArchive`.
- **You may NOT upload:** `BaktaDB`, `KofamProfiles`, `CheckM2DB`. These are
  large operator-curated reference databases seeded on the host. They appear in
  `list_objects()` like anything else, but the service exposes **no upload
  route by design** — calling `upload_object(object_type="CheckM2DB")` is a
  category error, not a permissions problem. Do not retry it or look for a
  flag.

### Failures are typed — read the exception, don't retry blindly

`KBDLServiceError` is the base, with specific subclasses that tell you whether
retrying could ever help: `KBDLBadRequestError`, `KBDLInvalidInputError`,
`KBDLUnsupportedSchemaVersionError`, `KBDLAuthenticationError`,
`KBDLNotAuthorizedError`, `KBDLUpstreamAuthUnavailableError`,
`KBDLNotFoundError`, `KBDLConflictError`, `KBDLPayloadTooLargeError`,
`KBDLJobFailedError`. An input or schema error will fail identically forever;
an auth or upstream-auth error is about the token, not your params.

> **Service health is a separate question from your code.** If calls **hang**
> rather than erroring, that is the service, not your parameters — the client
> resolves its endpoint with no I/O, so a hang is never a misconfiguration on
> your side. **Say so and stop, rather than retrying or concluding your call
> was wrong.** poplar is not yours to restart; report it.
>
> Expected healthy behaviour, verified 2026-10-05: every route answers in
> ~5ms, unauthenticated requests get `401 invalid_token` (that *is* the healthy
> answer — the service gates on the bearer token), and a zero-config
> `list_jobs()` returns its full record count in a few seconds. A transient
> hang was observed once on 2026-10-04 and had cleared by the next day, so
> **re-probe before declaring the service down** — one bad reading is not an
> outage.

## Media vocabulary (`--media`)

1. **Local JSON file** (default, fully offline) — a path to a JSON file
   whose contents are a dict accepted by
   `modelseedpy.core.msmedia.MSMedia.from_dict`:
   - Minimal: `{"cpd00027": 10}` (uptake bound only).
   - Bounds: **do NOT use `{"cpd00027": [-10, 1000]}` from a JSON file** — it
     raises `TypeError: bad operand type for unary -: 'list'`.
     `MSMedia.from_dict` accepts that form only as a Python *tuple*, and JSON
     parses `[...]` to a `list`, so it can never match from a media file. Use
     the Complete form below for explicit bounds.
   - Complete: `{"cpd00027": {"lower_bound": -10, "upper_bound": 1000,
     "concentration": 5.0, "name": "D-Glucose"}}`.
2. **KBase workspace reference** — any value containing `/` that is not an
   existing local file is treated as `wsid/object_name` and resolved via
   `KBModelUtils.get_media(..., msmedia=True)`. Requires `KB_AUTH_TOKEN`
   and network — do not use this form in a local-only KIND session unless
   you know a token is configured.

## Objective vocabulary (`--objective`)

`fba`/`fva`'s `--objective` is modelseedpy's `ObjectivePkg` DSL (default
`MAX{bio1}`):

```
MAX{<rxn_id>}                          maximize a single reaction
MIN{<rxn_id>}                          minimize a single reaction
MAX{(2.0)+rxn00001|(1.0)-rxn00002}     linear combination; "+"/"-" pick
                                        forward/reverse; coefficient and
                                        direction prefix are both optional
```

`gapfill`'s `--objective` is different: it is a **bare target reaction
id** (default `bio1`), passed straight to `MSGapfill(default_target=...)`
— do not pass `MAX{}`/`MIN{}` syntax to `gapfill`.

## Template vocabulary (`--template`, reconstruct/gapfill)

`core` and `gn` (gram-negative) are the two templates modelseedpy ships
locally/offline; `--template` on `reconstruct` defaults to `gn`. A KBase
workspace reference (containing `/`) is also accepted but requires auth —
avoid it in a local-only session. There is no offline `auto` template in
this environment.

## The LP solver — GLPK by default, and why you should leave it alone

Every `kbu model` verb pins cobra's solver to **GLPK** before running.
Override with `kbu model --solver NAME ...`, the `KBU_SOLVER` env var, or the
`modeling.solver` config key; hosts holding an unrestricted commercial
licence (h100/poplar) default to `gurobi` instead.

GLPK is not a fallback — it is measured sufficient. On an E. coli-sized
genome against a 9434-reaction candidate set, GLPK gapfilled in 28.1s
(437 reactions added) versus 27.4s for an *unrestricted* gurobi. **If you
ever see `Model too large for size-limited license`, that is a licence
ceiling and not a large problem** — `optlang` prefers gurobi whenever it is
merely importable, so a size-capped licence silently captures the solver and
then fails with a message that misdescribes its own cause. The fix is to
select GLPK, never to go looking for a bigger solver.

## Your runs are indexed — and they show up in the Models and Analyses app

Every `reconstruct` / `gapfill` / `fba` / `fva` run writes one **AnalysisRecord**
into the KOROS run index (`~/.kbdl/runs.sqlite` by default), and the
**Models and Analyses** KIND app reads that index. So the work you do here is
what populates that app — nothing else does.

**You do not need to pass anything for this to happen.** Your session has an arc,
and KING exports `KOROS_ARC` for you, so the index resolves automatically.
Verified 2026-10-02: a bare arc slug in `KOROS_ARC` resolves, and
reconstruct/gapfill/fba each wrote exactly one record with no extra flags.

The arc is resolved from, in order: `--arc PROJECT/SLUG` (or a bare `SLUG`),
`$KOROS_ARC`, or a cwd inside an arc directory. **If none of those resolve you
will see this on stderr:**

```
warning: analysis result was not indexed: no KOROS arc resolved
         (pass --arc, set KOROS_ARC, or run inside an arc directory)
```

That warning means the run happened but is invisible to the app. It is the only
signal, so do not ignore it — and note that passing `--arc` SILENCES it, so
never add `--arc` just to quiet the warning without checking a record appeared.
A session with no arc gets no `KOROS_ARC`, which is the usual cause.

Re-running the same analysis with the same inputs **upserts** rather than
duplicating: the index is keyed on `(analysis_id, run_uid)`, so one logical
analysis re-run six times is one row with the latest payload.

The index is ON by default as of 2026-10-02. It was OFF before that, and a
disabled index is silent — `record_analysis` validates, returns normally and
writes nothing — so if you are on an older build, every verb will look like it
stamped while the app reads empty. `KBDL_RUN_DB_ENABLED=0` turns it off
deliberately.

**What is NOT indexed:** jobs submitted through the KBDL client
(`submit_genome_annotation`, `submit_build_genome`,
`submit_model_reconstruction`, …) write no record — that client has no arc-store
wiring. Historical KBDL jobs were backfilled from the job API on 2026-10-02
(429 records, unattributed), but new KBDL jobs still will not appear. Only the
`kbu model` verbs above self-index.

## Local/offline construction

`kbu model` verbs work with no KBase token by bypassing
`KBModelUtils.__init__`'s cobrakbase-dependent setup (see
`kbu model --help` and the `model.py` module docstring for the mechanism
if you're curious) — you do not need to do anything special; this is
handled for you. The one exception to "no network" is RAST annotation inside
`reconstruct`, described above. `--atp-safe` (off by default on
`reconstruct`/`gapfill`)
is the one option that needs a local ModelSEED biochemistry database
checkout; leave it off unless you know one is configured
(`KBU_MODELSEED_DB_PATH`).

## API reference (read before writing an `exec` script)

Before writing a `kbu model exec` script, read:

- `~/Dropbox/Projects/KBUtilLib/agent-io/docs/kbu-model-cli.md` — the full
  CLI reference this skill text summarizes (verb-by-verb JSON schemas,
  accepted forms, provenance contract).
- `~/Dropbox/Projects/KBUtilLib/src/kbutillib/interfaces/cli/model.py` — the module
  docstring and each verb's implementation; every verb is a thin facade,
  so reading it shows you exactly which `MSReconstructionUtils`/
  `MSFBAUtils` methods to call yourself.
- `~/Dropbox/Projects/KBUtilLib/src/kbutillib/domains/modeling/ms_reconstruction_utils.py`
  and `~/Dropbox/Projects/KBUtilLib/src/kbutillib/domains/modeling/ms_fba_utils.py` — the
  underlying methods (`build_metabolic_model`, `gapfill_metabolic_model`,
  `run_fba`, `run_fva`) with their full signatures and docstrings.

If KBUtilLib is installed somewhere other than the conventional
`~/Dropbox/Projects/KBUtilLib` path on this machine, resolve it
programmatically instead of guessing:

```
kbu model exec - <<'PY'
import kbutillib, pathlib
print(pathlib.Path(kbutillib.__file__).parent)
PY
```

## Community modeling — `exec` only, and two things you MUST pass

There is **no `kbu community` verb**. Community modeling is library-only, so it
is always an `exec` script. The entry point is `MSCommunityUtils`, and the 14
`community.*` capabilities (`kbu cap list`) are its methods.

Working as of 2026-10-10, verified end to end on two real ModelSEED models. Two
arguments are **not optional on this host**, and both fail in misleading ways if
you omit them:

1. **`final_solver="glpk"` on `build_community`.** The merged community model
   otherwise defaults to gurobi, and this pod's gurobi is a SIZE-LIMITED
   license that refuses a genome-scale LP with `Model too large for
   size-limited license`. That error names the license, not the model, and is
   easy to misread as the community being too big. It is not. Set the member
   models' own `.solver = "glpk"` too.
2. **A real `MSMedia` for `media=`, not a dict.** `run_community_fba(comm,
   media=...)` wants a media OBJECT or an id string. Build one from a media
   JSON with `MSMedia.from_dict(json.load(open(path)))`. A raw dict raises on
   `media_obj.id`.

```
kbu model exec - <<'PY'
import json, cobra
from modelseedpy.core.msmedia import MSMedia
from kbutillib.domains.modeling.ms_community_utils import MSCommunityUtils

def member(path, mid):
    m = cobra.io.load_json_model(path); m.id = mid; m.solver = "glpk"; return m

cu = MSCommunityUtils()
comm = cu.build_community([member(p1, "orgA"), member(p2, "orgB")],
                          final_solver="glpk")
media = MSMedia.from_dict(json.load(open(media_json)))
res = cu.run_community_fba(comm, media=media)
print(res.status, res.community_growth, res.member_growth)
tbl, totals = cu.cross_feeding_table(comm)
PY
```

`CommunityFBAResult` fields are `status`, `community_growth`, `member_growth`,
`exchange_fluxes`, `fluxes`, `media_id`, `trustworthy`, `kinetics_relaxed`,
`notes` — note it is `community_growth`, **not** `objective_value`.

**Always run a no-carbon control.** A positive growth number is not evidence of
a working community: check that growth is ~0 on a carbon-free medium as well as
> 0 with carbon. `trustworthy` and `kinetics_relaxed` are the other two fields
to read — MSCommunity will silently drop its kinetic constraints on a
zero-growth community and proceed, and `kinetics_relaxed=True` is how you find
out.

**`render_community_map` / `render_member_map` do not work** and will fail at
call time even though `kbu cap list` reports them available: they need the
`escher_edit` backend, which is not installed on this pod. Everything else in
the domain works.

## Boundaries

- This skill covers `kbu model` and the KBDL job service it hands large work
  off to. It does not describe the rest of the `kbu` CLI (projects, sessions,
  notebooks) — those are out of scope here.
- This text is injected via `KING_CONTEXT` only; it is **not** registered
  as a Claude Code skill (no `~/.claude/skills/` entry), because this
  session has no `Skill` tool to invoke one with.
- The four `kbu model` verbs are **local** — your data stays on this host
  (the one exception is RAST annotation inside `reconstruct`, which sends
  protein sequences to the RAST service). If `kbu kind status` (or the
  orientation you were given) warns that KIND's LLM route is non-local, treat
  any model/genome data you handle as something you should not casually paste
  into that route.
- **KBDL is deliberately not local**: submitting a job or uploading an object
  sends that data to poplar. That is the point of it, but it is a real
  boundary crossing — apply the same judgement you would to any other
  off-host destination, and prefer the local verbs when they suffice.
