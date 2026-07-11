# `kbu model` CLI reference

`kbu model` is a thin CLI facade over KBUtilLib's metabolic-modeling API
(`ms_reconstruction_utils`, `ms_fba_utils`). It does not reimplement any
modeling math — every verb calls straight through to the existing
`MSReconstructionUtils`/`MSFBAUtils` methods. Built for
[king-integration-apps](../prds/) Module B: giving a KING session (or any
CLI-only caller) `reconstruct -> gapfill -> fba -> fva` plus an `exec`
escape hatch, all with `--json`.

Runs in the standard `kbu` interpreter (kbutillib + cobra + modelseedpy).
`cobrakbase` and a live KBase token are **not** required for the default,
fully local verbs — see "Local/offline construction" below.

## Verbs

```
kbu model reconstruct --genome PATH --out MODEL.json [--template core|gn] [--model-id ID] [--atp-safe/--no-atp-safe] [--json]
kbu model gapfill --model MODEL.json --media MEDIA [--out MODEL.json] [--objective RXN_ID] [--atp-safe/--no-atp-safe] [--json]
kbu model fba --model MODEL.json --media MEDIA [--objective DSL] [--top N] [--json]
kbu model fva --model MODEL.json --media MEDIA [--reactions ID,ID,...] [--fraction-of-optimum F] [--json]
kbu model exec SCRIPT.py [--timeout SECONDS] [--json] [-- ARGS...]
```

### `reconstruct`

Builds a draft model from a genome via `MSReconstructionUtils.build_metabolic_model`.
Writes a plain cobra JSON model to `--out`. `--json` output:

```json
{"model_path": "...", "reactions": 5, "metabolites": 82, "genes": 0}
```

### `gapfill`

Gapfills a model to feasibility on a media via
`MSReconstructionUtils.gapfill_metabolic_model`. Writes the gapfilled
model to `--out` (defaults to overwriting `--model` if `--out` is
omitted). `--json` output:

```json
{"model_in": "...", "media": "...", "model_out": "...", "reactions_added": ["rxn00001_c0", "..."]}
```

### `fba`

Runs pFBA via `MSFBAUtils.run_fba`. `--json` output:

```json
{"objective_value": 1.3967507474756427, "fluxes": [{"id": "bio1", "value": 1.397}], "solver_status": "optimal"}
```

`fluxes` is capped at `--top` entries (default 25), sorted by `|value|` descending.

### `fva`

Runs FVA via **`MSFBAUtils.run_fva`** — never
`cobra.flux_variability_analysis`, which is broken in this environment.
`--json` output:

```json
{"reactions": [{"id": "bio1", "min": 1.257, "max": 1.397}]}
```

`--reactions ID,ID,...` filters the (already-computed) result set to a
subset; it does not change how the underlying FVA is run.

### `exec` (provenance-preserving escape hatch)

Runs an agent-written Python script in the `kbu` interpreter with the full
KBUtilLib API available, for anything the four verified verbs above don't
cover. See "Provenance" below for the run-record contract. `--json`
envelope:

```json
{"stdout": "...", "stderr": "...", "exit_code": 0, "run_dir": "/path/to/runs/kbu-model-exec/20260711T...Z-abcdef012345"}
```

A failing script yields a nonzero `exit_code` in this envelope — `kbu
model exec` itself always exits 0 (unless invoked with a bad script path)
so callers must check `exit_code`, not the CLI process's own exit status.

## Accepted `--media` forms (Acceptance Criterion #10)

1. **Local JSON file** (default, fully offline) — a path to a JSON file
   whose contents are a dict accepted by
   `modelseedpy.core.msmedia.MSMedia.from_dict`:

   - Minimal: `{"cpd00027": 10}` — uptake bound only (becomes
     `lower_bound=-10, upper_bound=1000`).
   - Bounds: `{"cpd00027": [-10, 1000]}` — explicit `(lower_bound, upper_bound)`.
   - Complete: `{"cpd00027": {"lower_bound": -10, "upper_bound": 1000,
     "concentration": 5.0, "name": "D-Glucose"}}`.

   See `tests/fixtures/model/glucose_minimal.json` for a worked example
   (a minimal-format glucose-aerobic media used by the test suite).

2. **KBase workspace reference** — any value containing `/` that is not an
   existing local file is treated as a workspace object reference
   (`wsid/object_name`) and resolved via `KBModelUtils.get_media(id_or_ref,
   msmedia=True)`. Requires a configured KBase token (`KB_AUTH_TOKEN`) and
   network access; not exercised by the local test suite.

## Accepted `--objective` forms (Acceptance Criterion #10)

`fba`/`fva`'s `--objective` is the reaction-flux DSL parsed by
modelseedpy's `ObjectivePkg.build_package` (invoked via
`MSFBAUtils.run_fba`/`run_fva` -> `configure_fba_formulation` ->
`set_objective_from_string`):

```
MAX{<rxn_id>}                          maximize a single reaction (default: MAX{bio1})
MIN{<rxn_id>}                          minimize a single reaction
MAX{(2.0)+rxn00001|(1.0)-rxn00002}     linear combination: "+"/"-" select the
                                        forward/reverse variable; the
                                        parenthesized coefficient and the
                                        direction prefix are both optional
                                        (omit direction for net flux)
```

**`gapfill`'s `--objective` is different**: `gapfill_metabolic_model`'s
`objective` parameter is a *bare target reaction id* (default `bio1`), not
the `MAX{}`/`MIN{}` DSL — it is passed straight through to
`MSGapfill(default_target=objective)`.

## Accepted `--template` forms (reconstruct/gapfill)

modelseedpy 0.4.2 ships exactly two templates locally (no KBase workspace
needed): `core` and `gn` (gram-negative). `--template` on `reconstruct`
accepts either shorthand (default `gn`) for fully offline reconstruction,
or a KBase workspace reference (containing `/`), resolved via
`KBModelUtils.get_template` (requires KBase auth). There is no offline
`auto`/gram-positive/archaea template in this modelseedpy release — the
genome classifier data KBase uses for `auto` template selection is not
bundled with KBUtilLib, so `gs_template="auto"` is not exposed here.
`gapfill` always gapfills against both local templates (`core` + `gn`) for
simplicity, since those are the only two guaranteed to be present offline.

## Local/offline construction

The documented `kbu` interpreter contract is "kbutillib + cobra +
modelseedpy already importable" — it does **not** include `cobrakbase`
(KBase-workspace object I/O) or a checked-out `cb_annotation_ontology_api`
sibling repo, both of which `KBModelUtils.__init__` unconditionally
imports/reads. `tests/test_ms_fba_utils_eval.py` already established the
sanctioned pattern for constructing `MSFBAUtils`/`MSReconstructionUtils`
without that KBase-SDK plumbing (bypass `KBModelUtils.__init__`, construct
only the `MSBiochemUtils` base + the reconstruction/FBA-specific setup);
`kbu model` reuses that same pattern (`kbutillib/cli/model.py::
_construct_offline`) rather than inventing a second one. The modeling
algorithms themselves are called completely unmodified.

`--atp-safe` (off by default on `reconstruct`/`gapfill`) additionally
requires a local ModelSEED biochemistry database checkout. Resolution
order: `KBU_MODELSEED_DB_PATH` env var, then
`~/Dropbox/Projects/ModelSEEDDatabase`, then `~/code/ModelSEEDDatabase`,
then KBUtilLib's own default resolution.

## Provenance contract for `kbu model exec` (Acceptance Criterion #12)

Deliberately **does not** use a throwaway temp directory (this overrides
the general "use a temp dir for test determinism" convention — Chris's
provenance requirement supersedes it for this verb). Every invocation
creates a durable run directory:

```
<runs_root>/kbu-model-exec/<timestamp>-<script_hash prefix>/
├── <script_name>.py      # copy of the executed script
├── stdout.txt            # captured stdout (capped at 1MB)
├── stderr.txt            # captured stderr (capped at 1MB)
└── run.json              # {script_hash, exit_code, started_at, finished_at,
                           #  versions:{kbutillib,cobra,modelseedpy}, argv, cwd}
```

The script runs **with that directory as its cwd**, so any relative
output files it writes are captured there rather than lost to a
throwaway temp dir.

`<runs_root>` resolution:

1. If a `kbu-project.toml` is found walking up from the invocation cwd
   (KBUtilLib's standard project-root convention), `<runs_root>` =
   `<project_root>/runs/`, and a `kbu session save`-equivalent entry is
   also recorded (subproject `kbu-model-exec`) so the run shows up in
   `kbu session list`.
2. Otherwise, `<runs_root>` = `~/.kbcache/` (no session entry is
   recorded, since there is no real project/subproject context to attach
   it to — this avoids scattering `subprojects/` directories into
   whatever cwd happened to be current, e.g. KING's `~/koros`).
