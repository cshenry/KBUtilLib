# KBUtilLib Metabolic Modeling (kbu model)

You have a `kbu` CLI on PATH. Its `kbu model` verb group gives you
genome-scale metabolic reconstruction, gapfilling, FBA, and FVA, plus an
`exec` escape hatch for anything the verified verbs don't cover. This text
is injected into your orientation because your session has no `Skill` tool
— treat it as your onboarding for this capability. `kbu model` requires no
extra setup: the standard `kbu` interpreter already has kbutillib, cobra,
and modelseedpy importable, and every verb below runs fully offline (no
KBase token, no network) unless you explicitly pass a KBase workspace
reference.

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

## Media vocabulary (`--media`)

1. **Local JSON file** (default, fully offline) — a path to a JSON file
   whose contents are a dict accepted by
   `modelseedpy.core.msmedia.MSMedia.from_dict`:
   - Minimal: `{"cpd00027": 10}` (uptake bound only).
   - Bounds: `{"cpd00027": [-10, 1000]}` (explicit lower/upper).
   - Complete: `{"cpd00027": {"lower_bound": -10, "upper_bound": 1000,
     "concentration": 5.0, "name": "D-Glucose"}}`.
2. **KBase workspace reference** — any value containing `/` that is not an
   existing local file is treated as `wsid/object_name` and resolved via
   `KBModelUtils.get_media(..., msmedia=True)`. Requires `KB_AUTH_TOKEN`
   and network — do not use this form in a local-only KING session unless
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

## Local/offline construction

`kbu model` verbs work with no KBase token and no network by bypassing
`KBModelUtils.__init__`'s cobrakbase-dependent setup (see
`kbu model --help` and the `model.py` module docstring for the mechanism
if you're curious) — you do not need to do anything special; this is
handled for you. `--atp-safe` (off by default on `reconstruct`/`gapfill`)
is the one option that needs a local ModelSEED biochemistry database
checkout; leave it off unless you know one is configured
(`KBU_MODELSEED_DB_PATH`).

## API reference (read before writing an `exec` script)

Before writing a `kbu model exec` script, read:

- `~/Dropbox/Projects/KBUtilLib/agent-io/docs/kbu-model-cli.md` — the full
  CLI reference this skill text summarizes (verb-by-verb JSON schemas,
  accepted forms, provenance contract).
- `~/Dropbox/Projects/KBUtilLib/src/kbutillib/cli/model.py` — the module
  docstring and each verb's implementation; every verb is a thin facade,
  so reading it shows you exactly which `MSReconstructionUtils`/
  `MSFBAUtils` methods to call yourself.
- `~/Dropbox/Projects/KBUtilLib/src/kbutillib/ms_reconstruction_utils.py`
  and `~/Dropbox/Projects/KBUtilLib/src/kbutillib/ms_fba_utils.py` — the
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

## Boundaries

- This skill covers `kbu model` only. It does not describe the rest of the
  `kbu` CLI (projects, sessions, notebooks) — those are out of scope here.
- This text is injected via `KING_CONTEXT` only; it is **not** registered
  as a Claude Code skill (no `~/.claude/skills/` entry), because this
  session has no `Skill` tool to invoke one with.
- These modeling verbs are intended **local-only**. If `kbu king status`
  (or the orientation you were given) warns that KING's LLM route is
  non-local, treat any model/genome data you handle as something you
  should not casually paste into that route.
