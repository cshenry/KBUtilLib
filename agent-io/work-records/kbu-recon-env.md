# Work record — kbu-recon-env (task-39488b88)

Part A of 3 of the former `kbu-recon-layer` task, PRD `kbdl-recon-thin-layer-v1`
review round 1. Repo KBUtilLib, branch
`maestro/developer/kbdl-recon-kbu-recon-env`, base commit `30399ac`.

Scope delivered: the construction blocker, environment initialisation with three
classified errors, `classify_genome`, the `model` extra, the `kbu_model`
fail-rather-than-skip rule, unit tests, real-dependency tests, and the scoped
node-id comparison. `build_metabolic_model`'s `builder.build` call (:373) and its
compensations, `gapfill_metabolic_model`, `ReconstructionResult` and the AST
keyword test were left untouched (parts B and C).

---

## What changed

| file | change |
|---|---|
| `src/kbutillib/core/errors.py:71-117` | `ReconstructionEnvironmentError` base + the three leaf classes `MissingSolverError`, `MissingBiochemistryError`, `ModelSEEDpyCapabilityError`; added to `__all__` |
| `src/kbutillib/domains/biochem/ms_biochem_utils.py:35-113` | new module-level `load_modelseed_biochem(db_path)` — the single signature-dispatching helper |
| `src/kbutillib/domains/biochem/ms_biochem_utils.py:469-478` | `MSBiochemUtils._ensure_database_available` now calls the helper (was `ModelSEEDBiochem.get(path=...)` at the old :394) |
| `src/kbutillib/domains/biochem/ms_biochem_utils.py:1199-1207` | `MSBiochemUtilsImpl._ensure_database_available` likewise (old :1122) |
| `src/kbutillib/core/dependency_manager.py:19-51` | `register_process_dependencies` / `get_process_dependencies` / `clear_process_dependencies`, and `DependencyManager.__init__` seeds `dependency_paths` from the registry |
| `src/kbutillib/domains/modeling/ms_reconstruction_utils.py:15-56` | re-export of the four error classes; `__all__` extended |
| `src/kbutillib/domains/modeling/ms_reconstruction_utils.py:58-550` | the environment section: `RECONSTRUCTION_DEPENDENCIES`, `DEFAULT_SOLVER_PREFERENCE`, `GENOME_CLASS_TO_TEMPLATE_KEY`, `DEFAULT_TEMPLATE_KEY`, `_probe_solver`, `_set_cobra_default_solver`, `_select_lp_solver`, `_build_alias_probe_template`, `_build_alias_probe_genome`, `_genes_from_alias_probe`, `verify_role_alias_matching`, `_load_upstream_genome_classifier`, `_resolve_reconstruction_dependencies`, `initialize_reconstruction_environment` |
| `src/kbutillib/domains/modeling/ms_reconstruction_utils.py:2015-2075` | `MSReconstructionUtils.classify_genome` |
| `src/kbutillib/domains/modeling/kb_model_utils.py:11, 120-139, 809-817` | `modelseedpy.core.msfba` made optional (fork-only module) + explicit guard in `save_solution_as_fba` |
| `pyproject.toml:199-222` | `[project.optional-dependencies] model` with the SHA-pinned modelseedpy |
| `tests/conftest.py:297-390` | `kbu_model` stack guard: `model_stack_available`, `model_stack_required`, `require_model_stack`, marker registration, `pytest_runtest_setup` (`tryfirst`) |
| `tests/cli/test_model.py:23-30` | module guard now `require_model_stack()` instead of an unconditional module-level skip |
| `tests/modeling/test_recon_env_init.py` | 40 unit tests, all on fakes |
| `tests/modeling/test_recon_env_real_dependency.py` | 14 real-dependency tests (`kbu_model`) |

No `genome_annotation_aggregator` import was added anywhere; `grep -rn
genome_annotation_aggregator src/kbutillib` returns nothing.

---

## MEASURED

Everything in this section was executed in this worktree today (2026-11-15).

### The scratch venv

`/scratch/chenry/venvs/kbdl-recon-scratch-env`, python 3.11.15, created for this
task. `modelseedpy` installed from
`git+https://github.com/cshenry/ModelSEEDpy.git@e1155a41fa53b921185b20c6cfbd083e5e3739a4`
— the GitHub URL resolved, and `direct_url.json` records
`commit_id = e1155a41fa53b921185b20c6cfbd083e5e3739a4`, so the pin is the
published commit and not a local clone. Plus this branch editable, `pytest`,
`pyyaml`; `cobra 0.32.1`, `optlang 1.9.1` and `swiglpk 5.0.13` arrived as
modelseedpy's own dependencies. `/scratch/chenry/venvs/kbdl` and the smoke's
venv were not modified (the one use of `/scratch/chenry/venvs/kbdl` below is a
read-only `python -c`).

**The pinned target reports `modelseedpy 0.4.3`, not 0.4.2.** The fork in
`/scratch/chenry/venvs/kbdl` reports `0.4.2`. The programme context says both
report 0.4.2; for the pinned commit that is no longer true. It changes nothing —
the code is still identified behaviourally, never by the version — but a check
written against "both say 0.4.2" would be wrong.

### Construction blocker — TWO blockers, not one

1. As briefed: `ModelSEEDBiochem.get(path=...)`. Measured upstream signature
   `get(create_if_missing=True)`; measured fork signature
   `get(create_if_missing=True, path='/deps/ModelSEEDDatabase')`.
2. **Not briefed, measured here:** `KBModelUtils._import_modules` did
   `from modelseedpy.core.msfba import MSFBA`, and upstream `e1155a4` ships no
   `modelseedpy/core/msfba.py`:

   ```
   ModuleNotFoundError: No module named 'modelseedpy.core.msfba'
     at src/kbutillib/domains/modeling/kb_model_utils.py:115   (pre-change line number)
   ```

   This fires *after* the biochem blocker is fixed and *before* anything else,
   so fixing only blocker 1 still leaves `MSReconstructionUtils` unconstructable
   against upstream. I probed every module in that import list: `msfba` is the
   only one absent upstream (`annotationontology`, `msgenomeclassifier`,
   `msgrowthphenotypes`, `msmedia`, `msmodelutl` all import cleanly).

Both routes of the helper were exercised against REAL code, not only fakes:

| interpreter | `ModelSEEDBiochem.get` signature | route taken | result |
|---|---|---|---|
| `/scratch/chenry/venvs/kbdl-recon-scratch-env` (upstream `e1155a4`) | `(create_if_missing=True)` | `default_biochemistry = from_local(path)` then `get()` | loaded, 45,708 compounds |
| `/scratch/chenry/venvs/kbdl` (fork, read-only run) | `(create_if_missing=True, path='/deps/ModelSEEDDatabase')` | `get(path=...)` | loaded, 45,708 compounds |

In both cases the path came from `~/.kbutillib/dependencies.yaml`
(`/home/chenry/Dropbox/Projects/ModelSEEDDatabase`), never from modelseedpy's
`config.cfg`.

### Environment initialisation

`initialize_reconstruction_environment()` in the scratch venv returned:

```
solver            : glpk
solver_probes     : {'glpk': True}
dependencies      : ModelSEEDDatabase          -> /home/chenry/Dropbox/Projects/ModelSEEDDatabase
                    cb_annotation_ontology_api -> /home/chenry/Dropbox/Projects/cb_annotation_ontology_api
                    cobrakbase                 -> /home/chenry/Dropbox/Projects/cobrakbase
alias_matching    : {'genes_without_aliases': ['probe_gene_named'],
                     'genes_with_aliases': ['probe_gene_alias', 'probe_gene_named'],
                     'matched': True}
modelseedpy_file  : .../kbdl-recon-scratch-env/lib/python3.11/site-packages/modelseedpy/__init__.py
```

**Solver chosen: GLPK**, confirmed by a real two-reaction solve returning
`optimal` with the expected objective value 10.0. Gurobi was NOT probed in this
run: the preference order is GLPK-first and the search short-circuits on the
first backend that solves. `gurobipy` is not installed in this venv either, so
its probe would have returned False regardless. The licence ceiling
(`gurobipy < 14`, measured by the smoke) is therefore carried as documentation,
not as a measurement of mine.

`MSReconstructionUtils(config_file=False, token_file=None,
kbase_token_file=None)` then **constructed**, with `biochem_db` loaded and
`FBAModel` bound to the real cobrakbase class.

`classify_genome` on `tests/fixtures/model/haemophilus_influenzae_71421_8_rast.json.gz`
returned **`gn`**, from a raw classifier verdict of **`N`** — the verdict the
smoke measured in its section (e). The classifier was the shipped
`linear_svc_ACNP_RAST_full_11_11_2026.npz`, and its model class is not an
sklearn estimator.

The alias check discriminates, which is the whole point of doing it
behaviourally:

| `match_role_aliases` | genes that reached the model |
|---|---|
| `False` | `probe_gene_named` only |
| `True` | `probe_gene_alias`, `probe_gene_named` |

### Fail-rather-than-skip

Run in `/scratch/chenry/venvs/kbu39` (has pytest, has no cobra/modelseedpy;
not modified):

| selection | without the env var | with `KBU_REQUIRE_MODEL_STACK=1` |
|---|---|---|
| `tests/cli/test_model.py` (module-level guard) | `1 skipped` | `1 error` — `RuntimeError: KBU_REQUIRE_MODEL_STACK=1 demands the real model stack, but: ... No module named 'cobra'` |
| `tests/cli/test_model_recording.py` (per-test `skipif`) | `12 passed, 2 skipped` | `12 passed, 2 errors` |

`pytest.fail` inside `pytest_runtest_setup` is reported as ERROR rather than
FAILED. It is a non-passing outcome either way, which is the requirement; I did
not try to force the word "failed".

The `tryfirst=True` on that hook is load-bearing and was established by
experiment: without it, pytest's own skipping plugin (also `tryfirst`) evaluated
the `skipif` on the two `test_model_recording.py` items and skipped them before
the guard ran — the run reported `12 passed, 2 skipped` under
`KBU_REQUIRE_MODEL_STACK=1`.

### Test runs and the node-id comparison

Commands and exit codes, all from the worktree root unless stated:

```
# new unit tests (fakes only)
/scratch/chenry/venvs/kbdl-recon-scratch-env/bin/python -m pytest \
    tests/modeling/test_recon_env_init.py -q
40 passed                                            exit 0

# new real-dependency tests
KBU_REQUIRE_MODEL_STACK=1 \
/scratch/chenry/venvs/kbdl-recon-scratch-env/bin/python -m pytest \
    tests/modeling/test_recon_env_real_dependency.py -q
14 passed                                            exit 0
```

Scoped comparison, base vs branch, by test NAME. Base `30399ac` was materialised
read-only with `git archive 30399ac | tar -x -C /scratch/chenry/tmp/kbu-base-30399ac`
(no branch created, no worktree switched). Selection = every test path matching
`biochem` or `ms_reconstruction`:
`tests/biochem tests/domains/test_biochem_smoke.py tests/modeling/test_ms_reconstruction_utils.py`,
plus `tests/modeling/test_recon_env_init.py` on the branch side.

```
/scratch/chenry/venvs/kbdl-recon-scratch-env/bin/python -m pytest <selection> -q --tb=no --junit-xml=...
base   : 22 failed, 150 passed, 22 skipped, 26 errors     exit 1
branch : 22 failed, 190 passed, 22 skipped, 26 errors     exit 1
```

Passing node-id sets, compared by name (full lists in
`/scratch/chenry/tmp/passing-base.txt` and `passing-branch.txt`, which are
ephemeral):

* base passing: **150**
* branch passing: **190**
* **lost (regressions): 0**
* gained: **40** — every one of them `tests/modeling/test_recon_env_init.py::*`

The 22 failures / 26 errors are pre-existing and identical on both sides
(`test_escher_utils` needs the `escher` package, and so on). The suite is not
green and I am not claiming it is.

Second scoped run in the minimal venv `/scratch/chenry/venvs/kbu39`, same
selection minus the two modules that cannot be collected without pandas:
base `37 failed, 13 passed, 56 skipped`; branch `37 failed, 13 passed,
57 skipped`. The one extra skip is `test_recon_env_init.py` declining to run
(it `importorskip`s the modules under test, as
`tests/modeling/test_ms_reconstruction_utils.py` effectively does).

`tests/kbase/test_kb_model_utils.py` (the only other consumer of the
`kb_model_utils` code I touched) is `43 skipped` on both sides — it is gated on a
live KBase token, so it gave no signal in either direction.

I did NOT run the full suite. `build-baselines/kbutillib.json` exists
(base_commit `0f84803`, 99 `known_failing` names captured in a minimal venv) and
the task directs scoped runs; the direct base-vs-branch comparison above is a
stronger statement than a comparison against that older cache, so I did not
refresh it.

---

## INFERRED, not measured

* **`gurobipy < 14` as the licence ceiling.** Taken from the smoke record
  (section (b)); I never installed gurobipy or contacted the licence server. The
  constraint appears only as a comment in the `model` extra, because gurobipy is
  not declared in `pyproject.toml` at all.
* **That GLPK is the faster backend.** Smoke measurement (50.9 s vs 59.3 s on a
  full ATP correction), not re-measured here. My preference order rests on it
  plus the fact that GLPK needs no licence.
* **That `save_solution_as_fba` is the only consumer of `MSFBA`.** Established
  by `grep -rn MSFBA src/kbutillib tests`, not by running that path — it needs a
  live KBase workspace.
* **That `cb_annotation_ontology_api` resolving is sufficient for
  `KBAnnotationUtils`.** I observed `MSReconstructionUtils` constructing with it
  forwarded; I did not test what specifically breaks without it.
* **The `[model]` extra's resolvability.** The SHA is published (the install
  above proves the commit fetches from GitHub), but I did not run
  `pip install '.[model]'` end to end, because that would have rebuilt the
  scratch venv's modelseedpy from the same URL for no new information.

---

## Reconciled against the tree (prompt claim → what the tree holds → what I did)

1. **"The ModelSEEDpy change adds two opt-in keyword parameters to
   `build_base_model` … forwarded to `generate_reaction_complex_sets`, which
   accepts it at :633 but is called without it at :1097."** Stale: at the pinned
   `e1155a4` the change is already landed. `build_base_model` carries
   `*, match_role_aliases=False, allow_incomplete_complexes=True`
   (`msbuilder.py:1069-1081`) and DOES forward both to
   `generate_reaction_complex_sets` (`:1114-1117`), which accepts
   `match_role_aliases` keyword-only at `:643`. Nothing was needed upstream; the
   layer verifies the capability rather than adding it.
2. **"CONSTRUCTION BLOCKER … `ms_biochem_utils.py:394` and `:1122`."** Correct as
   far as it goes, and both line numbers were accurate. But it is not the only
   construction blocker — see `modelseedpy.core.msfba` above. I fixed both,
   because fixing only the briefed one leaves the stated success criterion
   ("`MSReconstructionUtils` CONSTRUCTS") unreachable.
3. **"Three DISTINCT exception classes … exported from that module."** The three
   classes are defined in `src/kbutillib/core/errors.py` and re-exported from
   `ms_reconstruction_utils` (and are in its `__all__`). Reason: `domains/biochem`
   must raise `MissingBiochemistryError` too, and `domains/modeling` imports
   `domains/biochem` (`kb_model_utils.py:11`), so defining them in the modeling
   module would make the biochem module import modeling — a cycle. `core/errors.py`
   states in its own docstring that it is the single source of truth for exactly
   this reason. A unit test asserts the two modules export the same objects.
4. **"`gurobipy<14` if gurobipy is declared at all."** It is not declared
   anywhere in `pyproject.toml`, so nothing was added; the ceiling is recorded in
   the extra's comment for whoever declares it.
5. **"Reuse the existing class → template-key map … around
   `ms_reconstruction_utils.py:330-345`."** The map runs to :357, not :345 — the
   `C` and unrecognised-class branches are at :346-357, and both `return
   current_output, None` (abandon the genome). I lifted the map to
   `GENOME_CLASS_TO_TEMPLATE_KEY` and applied the "no dedicated template → gn"
   rule to both of those cases, with a warning. `self.templates`
   (`kb_model_utils.py:138`) has no `cyano` key at all, so the legacy branch
   would have raised `KeyError` had it not returned early.
6. **"marker at pyproject line 371."** Accurate. The marker was declared in
   `[tool.pytest.ini_options]` but NOT registered in `conftest.pytest_configure`
   alongside the others; I added it there as well.
7. **"MSReconstructionUtils CONSTRUCTS … with KBUtilLib + ModelSEEDpy alone, no
   KBDL import."** No KBDL import is needed, confirmed. But the class still
   inherits `KBModelUtils`, whose `_import_modules` imports `cobrakbase`, so
   constructing it needs the cobrakbase checkout resolvable. The layer's default
   `RECONSTRUCTION_DEPENDENCIES` is the two dependencies the task names; the
   real-dependency test asks for `cobrakbase` as a third via the function's
   `dependencies=` argument. Severing the KBase inheritance is parts B/C, not
   this part, and I did not restructure the class.
8. **Minor API facts the prompt did not state, found by reading the installed
   tree:** `NewModelTemplateComplex.add_role(role, triggering=True,
   optional=False)` (not `optional_role`); `modelseedpy.helpers.get_classifier`
   takes a `classifier_id` (file stem), so the shipped `.npz` has to be
   discovered; `from_local` is a module-level function in
   `modelseedpy.biochem.modelseed_biochem`, not a classmethod.

---

## Notes for parts B and C

* `initialize_reconstruction_environment()` must run BEFORE constructing
  `MSReconstructionUtils` — the dependency forwarding is what makes
  `get_data_path` non-None, and cobra binds its solver interface when the first
  `Model` is constructed.
* `_genes_from_alias_probe` builds a complete model from a two-role template in
  well under a second with no biochemistry database and no solver; it is a cheap
  harness for anything that needs a tiny role-based draft.
* `self.MSFBA` is now `None` against upstream. Anything that touches it will get
  a `ModelSEEDpyCapabilityError` from `save_solution_as_fba`; other consumers, if
  any appear, need the same guard rather than an `isinstance(x, None)`.
