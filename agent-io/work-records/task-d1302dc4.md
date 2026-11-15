# Work Record: task-d1302dc4 — recon-env-smoke

## task_id
task-d1302dc4

## branch
maestro/developer/kbdl-recon-recon-env-smoke-r2

## base_commit
9b154dad8bc311ec79f8dd62c50c61db93fa91ce

## commit_shas
- aef6321 — test module + genome fixture
- (this commit) — work record + measurement artifact

## summary

Measured, by running it, how KBUtilLib's `MSReconstructionUtils` behaves against
upstream ModelSEEDpy `c0b94e3` on a role-based build. Every number in this record
was produced by executing code in a purpose-built scratch venv against the real
ModelSEED database; nothing here is inferred from reading source alone unless the
record says so explicitly.

**The headline result is negative and it is decisive for `kbu-recon-layer`:
`MSReconstructionUtils` cannot even be CONSTRUCTED against upstream ModelSEEDpy.**
It raises before any reconstruction code runs. The incompatibility the task
anticipated — the positional `builder.build(base_model, "0", False, False)` call —
is real and confirmed by signature binding, but it is the *second* blocker, not the
first.

Everything the layer actually needs, by contrast, works: the draft builds natively
from roles (957 reactions, 588 genes, 73 exchanges, `bio1` objective), both LP
backends carry ATP correction to completion, gap-fill runs on both prefilter
settings, and the genome classifier loads and classifies correctly. The chain is
not stopped.

## files_touched

- `tests/test_recon_upstream_real_dependency.py` — new, 10 measurement tests
  (a1, a2, b×2 backends, c, d×2 prefilter settings, e, f, g). Opt-in via
  `KBU_REQUIRE_MODEL_STACK=1`; **fails rather than skips** when opted in with a
  missing stack.
- `tests/fixtures/model/haemophilus_influenzae_71421_8_rast.json.gz` — new, 29 KB.
  Real genome: *Haemophilus influenzae* Rd KW20 (BV-BRC `71421.8`), 1826 CDS
  features carrying 1530 distinct real RASTtk role strings, downloaded 2026-11-15
  from the BV-BRC `genome_feature` API with `annotation=PATRIC`. Protein sequences
  are deliberately omitted — see caveat 1.
- `agent-io/research/task-d1302dc4-recon-env-smoke-measurements.json` — the raw
  measurement dump the harness writes, from the passing run below.

**No file under `src/` was modified** (the task forbids it).

## scratch venv (step 1)

`/scratch/chenry/venvs/kbdl-recon-scratch-smoke`, Python 3.11.15 (matching the
production venv's interpreter). `/scratch/chenry/venvs/kbdl` was **not** touched.

| Package | Version | Provenance |
|---|---|---|
| ModelSEEDpy | 0.4.3 | `pip install 'git+https://github.com/ModelSEED/ModelSEEDpy.git@c0b94e3e7c08f142a48c869808e7c0862f3c1e50'` |
| cobra | 0.32.1 | transitively, matches production |
| gurobipy | 13.0.0 | pinned — see the licence finding below |
| swiglpk / GLPK | 5.0.13 / 5.0 | transitively via optlang |
| KBUtilLib | 1.0.0 | `pip install -e .` from this worktree |

`direct_url.json` records the commit exactly:

```json
{"url": "https://github.com/ModelSEED/ModelSEEDpy.git", "vcs_info": {"commit_id": "c0b94e3e7c08f142a48c869808e7c0862f3c1e50", "requested_revision": "c0b94e3e7c08f142a48c869808e7c0862f3c1e50", "vcs": "git"}}
```

`c0b94e3` was reachable, and **`git ls-remote` confirms it is still the
`upstream/dev` tip as of 2026-11-15** (`c0b94e3e7c08f142a48c869808e7c0862f3c1e50
refs/heads/dev`). No fallback was needed. Note that the local clone at
`~/projects/ModelSEEDpy` is the FORK (its `origin` is
`~/Dropbox/Projects/ModelSEEDpy`) and does **not** contain `c0b94e3`; the install
went to `github.com/ModelSEED/ModelSEEDpy` directly.

## `~/.kbutillib/` on poplar (step 2)

Measured, not assumed — on primary-laptop this file was retired, so its presence
here had to be checked:

```
/home/chenry/.kbutillib/
├── dependencies.yaml                      330 bytes, mtime 2026-06-26 16:08  ← PRESENT
├── config.yaml                            16837 bytes, mtime 2026-10-31
├── config.yaml.bak.*                      (7 backups)
├── skani_databases.json, skani_sketches/
├── anthropic/, bakta_work/, checkm2_work/, kofamscan_work/, prokka_work/,
│   transyt_work/
```

`dependencies.yaml` declares four dependencies, all of which exist on disk:
`modelseedpy`, `ModelSEEDDatabase`, `cobrakbase`, `cb_annotation_ontology_api`
(all under `~/Dropbox/Projects/`).

`get_data_path` is **not None**: the harness asserts this and the run passes.
`ModelSEEDDatabase` resolves to `/home/chenry/Dropbox/Projects/ModelSEEDDatabase`
and `cb_annotation_ontology_api` to
`/home/chenry/Dropbox/Projects/cb_annotation_ontology_api`.

### Two things about dependency-manager setup that the layer must know

**(i) `initialize_dependencies()` silently replaces upstream ModelSEEDpy with the
fork.** `DependencyManager.initialize_dependencies` does
`sys.path.insert(0, dep_path)` for *every* declared entry
(`src/kbutillib/core/dependency_manager.py:141`), and `dependencies.yaml` declares
`modelseedpy: /home/chenry/Dropbox/Projects/ModelSEEDpy`. Observed directly: a
process that printed `modelseedpy 0.4.3` (the pinned upstream wheel) before the
call printed `modelseedpy 0.4.2` (the fork checkout) after it. **Any measurement
or CTS job that initialises the dependency manager the obvious way is silently
running the fork.** The harness therefore writes a reduced config containing only
`ModelSEEDDatabase` and `cb_annotation_ontology_api` — the two the task names — so
no fork checkout reaches `sys.path`.

**(ii) The `SharedEnvUtils.__init__` monkeypatch in GAA's `make_recon` is the part
that actually works.** `SharedEnvUtils.__init__` constructs
`DependencyManager(auto_init=False)` (`core/shared_env_utils.py:106`), so each
instance's `dependency_paths` is empty and `self.get_data_path(...)` returns
`None`. Without the mirror, `KBAnnotationUtils.__init__` falls through to its
sibling-directory fallback and tries to read
`/mnt/homes/chenry/.maestro/worktrees/cb_annotation_ontology_api/data/FilteredReactions.csv`
— observed as a `FileNotFoundError` that stops construction outright. The harness
reproduces the patch; with it, construction gets past `kb_annotation_utils.py:101`.

I read `make_recon` as instructed and reproduced its *essential* setup. One
correction to its docstring, which the PRD may be relying on: the docstring says it
"forwards them to the constructor so `MSReconstructionUtils.__init__` cannot fall
back to the broken sibling-directory default". **It does not.** `make_recon` ends
with `return MSReconstructionUtils(**kwargs)` and `build_model` calls `make_recon()`
with no arguments, so no path is ever forwarded. The monkeypatch is the only
mechanism doing the work. My harness *does* pass `modelseed_db_path` explicitly,
as the task asks.

## Measurements

Raw dump: `agent-io/research/task-d1302dc4-recon-env-smoke-measurements.json`.

### (a) Does `build_metabolic_model` work against upstream? **No — on two counts.**

**Blocker 1 (fires first): construction fails.**
`MSBiochemUtils.__init__` eagerly calls `_ensure_database_available()`
(`src/kbutillib/domains/biochem/ms_biochem_utils.py:95`), which calls
`ModelSEEDBiochem.get(path=self.modelseed_db_path)` (`:394`). Upstream's signature
is `get(create_if_missing=True)` — **no `path` parameter**. Observed:

```
TypeError: ModelSEEDBiochem.get() got an unexpected keyword argument 'path'
  at src/kbutillib/domains/biochem/ms_biochem_utils.py:394
```

This is reached before any reconstruction code, so `MSReconstructionUtils` is
unconstructable against upstream. The fork's signature is
`get(create_if_missing=True, path=config.get("biochem","path"))`, which is why this
has never been seen.

**Blocker 2: the positional `build` call.** Upstream signature, read off the
installed module:

```
build(self, model_or_id, gapfill_media, index='0', allow_all_non_grp_reactions=False,
      annotate_with_rast=True, biomass_classic=False, biomass_gc=0.5,
      add_reaction_from_rast_annotation=True, add_maintenance_atp_reaction=True)
```

`builder.build(base_model, "0", False, False)`
(`src/kbutillib/domains/modeling/ms_reconstruction_utils.py:373`) binds as measured
by `inspect.Signature.bind`:

| parameter | bound value |
|---|---|
| `model_or_id` | the base model ✅ |
| `gapfill_media` | `'0'` ❌ (intended as `index`) |
| `index` | `False` ❌ |
| `allow_all_non_grp_reactions` | `False` ✅ |
| `annotate_with_rast` | `True` ❌ — **defaults on, so a live RAST network call** |
| `add_maintenance_atp_reaction` | `True` |

Upstream `build()` additionally runs `MSATPCorrection` and
`MSGapfill.run_gapfilling` itself and returns a **4-tuple**
`(model_gapfilled, atp_correction, tests, gap_sol)`, which `build_metabolic_model`
assigns to a single `mdl` and then does `mdl.genome = genome` on.

Blocker 2 is established by signature binding rather than by execution, because
blocker 1 is reached first. That is the stronger statement anyway — it does not
depend on a network RAST call succeeding.

**`MSBuilder.build_base_model` called with keyword arguments, `annotate_with_rast=False`:**

```
build_base_model(self, model_or_id, index='0', allow_all_non_grp_reactions=False,
                 annotate_with_rast=True, biomass_classic=False, biomass_gc=0.5,
                 add_reaction_from_rast_annotation=True)
```

Returns a bare `cobra.core.model.Model` (not a tuple). On *H. influenzae* + native
gram-negative template:

| property | value |
|---|---|
| reactions | 957 |
| metabolites | 967 |
| genes | **588** (role matching worked — the build is genuinely role-based) |
| groups (complexes) | 533 |
| exchanges | **73 — PRESENT** |
| biomass | **`bio1` — PRESENT** |
| objective | **`1.0*bio1 - 1.0*bio1_reverse_b18f7` — PRESENT** |
| drains (`DM_`/`SK_`) | 4 |
| `ATPM` | **ABSENT** |

So `build_base_model` gives the layer exchanges, biomass and an objective for free,
but the layer must add `ATPM` itself — upstream does that in `build()`
(`MSBuilder.add_atpm`), which the layer must never call.

**The alias-matching parameters do not exist yet at `c0b94e3`.** Measured:
`match_role_aliases` → absent (`grep -c` = 0 in `msbuilder.py`);
`allow_incomplete_complexes` → absent from `build_base_model`'s signature.
`generate_reaction_complex_sets(self, allow_incomplete_complexes=True)` is at
`msbuilder.py:633` and is called without it at `msbuilder.py:1097` — both line
numbers exactly as the PRD states. The PRD's "one alias-matching commit" is still
unmade; `kbu-recon-layer` cannot pass those keywords to this pin.

The alias premise itself checks out: the native core template carries **20,552
roles** with aliases like `msfr.1407` (e.g. `ftr01407` → `aliases: ['msfr.1407']`).
The gram-negative template has 20,389 roles and additionally carries
`alias:<lowercased role name>` entries.

### (b) Which LP backend carries ATP correction to completion? **Both do.**

| backend | version | completed | wall clock | ATP tests | selected media | core gap-fill |
|---|---|---|---|---|---|---|
| **GLPK** | 5.0 | **yes** | **50.9 s** | 4 | `Glc`, `Glc.DMSO`, `Glc.TMAO` | 0 |
| **Gurobi** | **13.0.0** | **yes** | 59.3 s | 4 | `Glc`, `Glc.DMSO`, `Glc.TMAO` | 0 |

Identical results; GLPK was the faster of the two on this model. 54 default ATP
media were loaded from the packaged `atp_medias.tsv`.

Times are from the committed artifact. Across two full runs GLPK was 50.9-51.3 s
and Gurobi 59.3-65.6 s; every *structural* figure (4 tests, the same three
selected media, 0 core gap-filling, 958 reactions after) reproduced exactly. Only
wall clock moved, as expected on a shared host.

**On the executor note about issue #35.** Chris's claim that the poplar Gurobi
floating licence has been renewed is **substantiated, with a version ceiling he did
not mention.** Verified by real solves, never assumed:

- The licence is a token server, `lic-vmw-01.cels.anl.gov:27070`
  (`/opt/gurobi/gurobi.lic`; `GRB_LICENSE_FILE` is unset in the environment, so it
  must be set or the file found by default).
- `gurobipy` **14.0.0** (what plain `pip install gurobipy` gives you today) is
  **REJECTED**: `GurobiError: Request denied: license not valid for Gurobi
  version 14`.
- Versions **13.0.0, 12.0.3, 11.0.3 and 10.0.3 all solve successfully.** Probed one
  at a time in a throwaway venv.

So Gurobi is usable on poplar but **must be pinned `<14`**. GLPK 5.0 needs no
licence and completed every measurement here, so it remains the safe default.

A related trap, worth recording because it cost a confusing failure: with
`gurobipy` 14 installed, a *GLPK* solve also failed with the Gurobi licence error,
because `cobra`'s default interface is `optlang.gurobi_interface` and the backend is
chosen when the `Model` is constructed, before `model.solver = "glpk"` runs. Set
`cobra.Configuration().solver` first, or construct with the solver already chosen.

### (c) Does `ModelSEEDBiochem` resolve through the dependency manager? **Yes, but only by bypassing KBUtilLib's own call.**

| measurement | value |
|---|---|
| packaged `config.cfg` `[biochem] path` (upstream) | `data/ModelSEEDDatabase` — **relative, not `/deps/...`** |
| exists as given | **no** |
| resolved against the package dir | `.../site-packages/modelseedpy/data/ModelSEEDDatabase` — **does not exist** |
| `ModelSEEDBiochem.get()` (upstream default) | `FileNotFoundError: 'data/ModelSEEDDatabase/Biochemistry/'` |
| `ModelSEEDBiochem.get(path=...)` (KBUtilLib's call) | `TypeError: unexpected keyword argument 'path'` |
| dependency-manager path | `/home/chenry/Dropbox/Projects/ModelSEEDDatabase` — exists |
| `from_local(<that path>)` | **works**: 45,708 compounds, 56,012 reactions, 19.9 s |

**Issue #41's conclusion holds but its stated cause is fork-specific.** The task
says `config.cfg` sets `path = /deps/ModelSEEDDatabase`. That is true of the
**fork** (`~/projects/ModelSEEDpy/modelseedpy/config.cfg` and the installed fork in
`/scratch/chenry/venvs/kbdl`), but **upstream `c0b94e3` sets
`path = data/ModelSEEDDatabase`** — relative, resolved against the caller's cwd.
It still fails, so "never rely on that default" stands; the error is
`FileNotFoundError` on a *different* path than #41 records.

The actionable finding for the layer: **upstream's `ModelSEEDBiochem.get` accepts no
`path`, so a database path can only be injected via `from_local(path)`** (or by
assigning `ModelSEEDBiochem.default_biochemistry`). KBUtilLib's
`ms_biochem_utils.py:394` must change before anything in this stack constructs.

### (d) `MSGapfill.run_gapfilling`, both prefilter settings: **both work.**

One medium (aerobic glucose minimal, 20 ModelSEED compounds), gram-negative
template as the gap-fill source, target `bio1`, backend Gurobi 13.0.0:

| `prefilter` | completed | wall clock | new reactions | reversed |
|---|---|---|---|---|
| `True` | **yes** | **41.1 s** | 163 | 0 |
| `False` | **yes** | **36.3 s** | 163 | 0 |

Same solution either way — 163 new reactions, 0 reversed, identical id sets.
Result is a dict keyed
`{binary_check, media, minobjective, new, reversed, target}`.

Times are from the committed artifact. Across three runs `prefilter=True` took
41.1-50.8 s and `prefilter=False` 35.0-47.4 s, so **`prefilter=True` was never the
faster of the two on a model this size** while producing the same answer. Both
complete, which is the question the task asked; the timing ordering is a
single-organism, single-medium observation and should not be generalised.

### (e) Which genome classifier artifact loads? **`.npz`, pure numpy, no sklearn.**

| measurement | value |
|---|---|
| classifier dir | `.../site-packages/modelseedpy/data/ml` |
| artifacts present | `linear_svc_ACNP_RAST_full_11_11_2026.npz` **only** |
| pickles present | **none** |
| loader | `get_classifier(classifier_id)` → `MSGenomeClassifier` |
| model class | `MSLinearModel` — **not** an sklearn estimator |
| prediction needs scikit-learn | **no** |
| model features | 33,382 |
| classes | `A`, `C`, `N`, `P` |
| call | `classifier.classify(genome, ontology_term='RAST')` |
| verdict on *H. influenzae* | **`N`** (Gram Negative — correct) |
| time | 0.002 s |
| genome roles matching model features | **1444 / 1530** |

This is a real improvement for the layer: upstream ships a `.npz` loaded by
`load_classifier_from_npz` with `allow_pickle=False`, so the
scikit-learn-1.2.0-pin problem that the legacy `*.pickle` classifiers carried is
gone. The fallback pickle path still exists in `helpers.get_classifier` and
downloads on demand, but it is not exercised here because the `.npz` is present.
Note the classifier reads `ontology_terms[...]` only — it never touches sequences.

### (f) Do the roleless-path compensations change a ROLE-BASED draft? **No — both are strict no-ops.**

With `reactions_to_add=None` on the role-based draft:

| compensation | returned | added to the model |
|---|---|---|
| `builder.build_non_metabolite_reactions(model, allow_all_non_grp_reactions=False)` re-run | **0 reactions** | **none** |
| `MSBuilder.add_exchanges_to_model(model)` | — | **none** |

Draft stays at 957 reactions. This confirms the source comments' own claim that both
exist purely for the roleless path, and it means `kbu-recon-layer` can drop both
when it builds natively from roles — they cost a full template sweep and change
nothing.

**One prompt-vs-tree correction here.** The task names the two compensations as "the
'ATP-safe annotation overlay' and the spontaneous/universal re-run after
`reactions_to_add` injection, **both in `build_metabolic_model`**". The ATP-safe
annotation overlay is **not** in `build_metabolic_model` — it lives in GAA's
`recon_driver` (step 7 of its module docstring: "ATP-safe overlay of remaining
annotation reactions"). The second compensation actually inside
`build_metabolic_model` is the `add_exchanges_to_model` call before ATP correction
(#28). I measured the two that are really there. The overlay could not be measured
without importing GAA, which the PRD forbids.

### (g) Exchanges before ATP correction (#28) and spontaneous/universal in the draft (#29)

| measurement | value |
|---|---|
| exchanges on the draft ATP correction receives | **73** (#28 satisfied) |
| spontaneous/universal reactions in the template | 19 |
| of those, present in the draft | **19 — 100 % coverage** (#29 satisfied) |
| `rxn05468_c0` (O₂ diffusion) | **present** |
| `rxn05467_c0` (CO₂ diffusion) | **present** |
| `rxn05319_c0` (H₂O diffusion) | **present** |

Both #28 and #29 are **artifacts of the roleless path and do not occur on a
role-based build.** All three membrane-diffusion reactions whose absence #29 blames
for models "respiring" by running cytochrome oxidase backwards are present in the
role-based draft, and the draft reaches ATP correction with 73 exchanges rather
than zero.

Note the count discrepancy, which I did not chase: the source comment in
`build_metabolic_model` says "3-12 of the 42 spontaneous/universal reactions in
GramNeg V6". The template this harness downloaded
(`GramNegative.modeltemplate`, 20,389 roles / 9,197 complexes / 9,434 reactions)
reports **19**, not 42. Likely a different template version; the 100 % figure above
is relative to the 19 this template actually declares.

## tests_run

Full module, pinned upstream, real database, both backends:

```
cd /mnt/homes/chenry/.maestro/worktrees/task-d1302dc4
export TMPDIR=/scratch/chenry/tmp GRB_LICENSE_FILE=/opt/gurobi/gurobi.lic
export KBU_REQUIRE_MODEL_STACK=1 KBU_RECON_SMOKE_REPORT=/scratch/chenry/tmp/smoke-run2.json
/scratch/chenry/venvs/kbdl-recon-scratch-smoke/bin/python -m pytest \
    tests/test_recon_upstream_real_dependency.py -p no:cacheprovider --no-header -q
```

**Result: `10 passed, 2 warnings in 254.11s (0:04:14)`. Exit code 0.**

### Gate contract, both halves verified

```
# opted out -> inert
env -u KBU_REQUIRE_MODEL_STACK .../bin/python -m pytest tests/test_recon_upstream_real_dependency.py -q -rs
```
→ `1 skipped in 0.06s`, reason `real-dependency recon measurements are opt-in; set KBU_REQUIRE_MODEL_STACK=1`

```
# opted in with the stack missing (HOME pointed at an empty dir) -> MUST fail, not skip
HOME=<empty tmpdir> KBU_REQUIRE_MODEL_STACK=1 .../bin/python -m pytest tests/test_recon_upstream_real_dependency.py -q
```
→ `ERROR tests/test_recon_upstream_real_dependency.py - RuntimeError: KBU_REQUIR...`
   `Interrupted: 1 error during collection`, `2 warnings, 1 error in 4.77s`, **non-zero exit**

### Lint

```
/scratch/chenry/venvs/kbdl-recon-scratch-smoke/bin/python -m ruff check tests/test_recon_upstream_real_dependency.py
```
→ `All checks passed!` (exit 0; one `I001` import-order finding was fixed)

### Regression check

Per the developer-role instructions I used the cached baselines rather than
re-running the full suite. `build-baselines/kbutillib-h100-main-6310d61.json`
records the full `tests/` run on this machine at **2665–3416 s (44–57 min)**, which
is 4.4–5.7× the 600 s tool ceiling — a sub-agent cannot run it, and the file says
so explicitly.

A targeted run is not merely sufficient here, it is conclusive, because **this
change adds two new files and modifies nothing**:

```
$ git status --porcelain
?? tests/fixtures/model/haemophilus_influenzae_71421_8_rast.json.gz
?? tests/test_recon_upstream_real_dependency.py
```

I proved non-interference by collecting the whole `tests/` tree with and without
the new module, in the same location and the same interpreter
(`/home/chenry/venvs/kbdl/bin/python`, the baseline's interpreter), and diffing the
**node-id sets**:

```
PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest tests/ --collect-only -q
```

| run | collected node ids |
|---|---|
| with `tests/test_recon_upstream_real_dependency.py` | **8692** |
| with that file moved aside | **8692** |
| `diff` of the sorted node-id sets | **IDENTICAL** (exit 0) |

The new module contributes zero collected items because it skips at module scope
before producing any, and it touches no `conftest.py`, no `pyproject.toml` and no
`src/`. No pre-existing test's collection or execution can be affected, so the
passing set cannot have shrunk. I did **not** write a new `build-baselines/` entry:
the instructions say to use the cached baseline when one exists, and two do.

## reconciliation: what the prompt claimed vs what the tree held

Listed because this is how the taskplan gets corrected at source.

1. **"`modelseedpy` reporting 0.4.2 … Fork and upstream both report modelseedpy
   0.4.2"** → Upstream `c0b94e3` reports **0.4.3**. The fork reports 0.4.2. The
   prompt's underlying point stands and is *strengthened*: a version string does not
   identify the code, and here it does not even agree. I used keyword arguments at
   every call into ModelSEEDpy and KBUtilLib, as instructed.

2. **"`config.cfg` sets `[biochem] path = /deps/ModelSEEDDatabase`" (#41)** → True
   of the **fork** only. Upstream sets `path = data/ModelSEEDDatabase` (relative).
   Both are broken on poplar, with different exceptions. Recorded both; measured
   against upstream, which is what the task is about.

3. **"the 'ATP-safe annotation overlay' and the spontaneous/universal re-run …
   both in `build_metabolic_model`"** → The overlay is in **GAA's `recon_driver`**,
   not `build_metabolic_model`. The second in-method compensation is the
   `add_exchanges_to_model` call. Measured the two that exist; said so in the test
   body and in the recorded measurement.

4. **"The ModelSEEDpy change adds two opt-in keyword parameters to
   `build_base_model`: `match_role_aliases=False` and
   `allow_incomplete_complexes=True`"** → Neither exists at `c0b94e3`; that commit
   is not yet made. Verified rather than assumed. The prompt's own line numbers for
   `generate_reaction_complex_sets` (`:633` def, `:1097` call-without-it) are
   **exactly right**.

5. **"Upstream MSBuilder.build() (c0b94e3 msbuilder.py:978)"** → Correct, `build`
   is at `msbuilder.py:978`.

6. **"20,552 core roles"** → Correct: the native core template has exactly 20,552
   roles, aliased `msfr.*`. (I did not verify the "20,551 of 20,552" split; not
   needed for any decision here.)

7. **"GAA's `recon_driver.make_recon` … forwards them to the constructor"** → Its
   docstring says so; **its code does not.** The `SharedEnvUtils` monkeypatch is the
   only mechanism that works. My harness forwards `modelseed_db_path` explicitly, as
   the task asks.

8. **"poplar's Gurobi floating licence is EXPIRED" (#35) vs the executor note that
   it was renewed on 2026-11-15** → The **renewal is real**, with a ceiling the note
   omits: valid for Gurobi ≤ 13, **rejected for 14**. Recorded the version that
   actually completed ATP correction, observed.

9. **Issue #29's "42 spontaneous/universal reactions in GramNeg V6"** → The
   template downloaded today declares **19**. Not chased; flagged.

10. **Not in the prompt at all, and the most consequential thing I found**:
    `initialize_dependencies()` prepends every `dependencies.yaml` path to
    `sys.path`, including the fork `modelseedpy`, silently shadowing a pinned
    upstream install. Any task in this PRD that "installs upstream and then
    initialises the dependency manager" is measuring the fork.

## caveats

1. **The genome fixture carries no protein sequences, by choice.** Everything on
   the role-based path reads `feature.ontology_terms['RAST']` only —
   `MSBuilder.build_base_model(annotate_with_rast=False)` and
   `MSGenomeClassifier.classify` both do, as verified in their source. The role
   strings, feature ids, organism and counts are all real; the sequences are simply
   absent rather than faked. If a future task needs sequences, re-query BV-BRC with
   the `aa_sequence` field (the `select(...)` used here returned it empty) — the URL
   is recorded in the fixture's `source_url`.

2. **Blocker 2 in (a) is established by signature binding, not by execution**,
   because blocker 1 stops construction first. This is stated in the test docstring
   and in the recorded measurement. I consider it the stronger evidence, since
   executing it would have depended on a live RAST network call that
   `annotate_with_rast=True` would have triggered.

3. **The ATP-correction measurement passes `atp_hydrolysis_id="ATPM_c0"` and calls
   `MSBuilder.add_atpm` first**, mirroring what upstream `build()` does.
   `build_metabolic_model` does **neither** — it never adds `ATPM` and never names
   the hydrolysis reaction, leaving upstream to do a SEED reaction search. I did not
   measure that unnamed-reaction path; `kbu-recon-layer` should add `ATPM`
   explicitly.

4. **`MSATPCorrection(load_default_medias=True)` is unconditionally broken
   upstream** and I had to work around it to measure anything in (b). At
   `msatpcorrection.py:38` there is a module-level function `load_default_medias`,
   and `__init__` has a parameter of the same name; at `:115` the call
   `load_default_medias(default_media_path)` resolves to the **bool parameter**:
   `TypeError: 'bool' object is not callable`, for any truthy value. Upstream never
   trips over it because `build()` passes `load_default_medias=False` and pre-loads
   the medias. **KBUtilLib's `build_metabolic_model` passes it through with default
   `True`** (`ms_reconstruction_utils.py:251`, used at `:478`), so every ATP-safe
   build on this pin would raise before solving. The harness measures the defect and
   then runs the correction the way upstream does (medias pre-loaded,
   `load_default_medias=False`); test (b) asserts the defect is still present, so it
   will flag loudly if upstream fixes it.

5. **Only one medium and one genome.** The task asked for one of each. The gap-fill
   numbers in (d) and the ATP numbers in (b) are single-organism, single-medium
   observations and should not be read as performance characterisations.

6. **`/scratch` is at 90 % (900 GB free)** and `/scratch` and `/tmp` are ephemeral
   on this host. The scratch venv at
   `/scratch/chenry/venvs/kbdl-recon-scratch-smoke` will not survive a scratch
   wipe; the recipe to rebuild it is the table in the venv section above. The
   measurement JSON is committed into the repo precisely so the numbers outlive the
   venv.

## what this decides for kbu-recon-layer

1. **`MSReconstructionUtils` cannot be reached at all until
   `ms_biochem_utils.py:394` stops calling `ModelSEEDBiochem.get(path=...)`.** This
   is the first thing `kbu-recon-layer` must change, and it is in the biochem layer,
   not the reconstruction layer. Inject via `from_local(path)` instead.
2. **Build the draft with `build_base_model(annotate_with_rast=False, ...)`, by
   keyword, and never call `build()`** — confirmed by binding, not inferred.
3. **Add `ATPM` explicitly** (`MSBuilder.add_atpm`) and pass
   `atp_hydrolysis_id="ATPM_c0"`; `build_base_model` does not add it.
4. **Pre-load ATP media and pass `load_default_medias=False`**; the `True` path is
   broken at this pin.
5. **Drop both roleless compensations** — measured as exact no-ops on a role-based
   draft.
6. **`match_role_aliases` / `allow_incomplete_complexes` are not available at
   `c0b94e3`.** The alias-matching commit must land before the layer can opt in.
7. **Never let the dependency manager put the fork `modelseedpy` on `sys.path`**;
   declare only the data dependencies, or drop the `modelseedpy` entry.
8. **Pin `gurobipy<14` if Gurobi is used at all**; GLPK 5.0 needs no licence and was
   faster here. The chain is **not** stopped for want of a solver.
