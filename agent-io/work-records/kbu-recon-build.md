# Work record — kbu-recon-build (task-ec0feb19)

Part B of 3 of the former `kbu-recon-layer` task, PRD `kbdl-recon-thin-layer-v1`
review round 1. Repo KBUtilLib, branch
`maestro/developer/kbdl-recon-kbu-recon-build`, base commit `35309ff` (the tip
of part A, kbu-recon-env).

Scope delivered: the native role-based draft via `MSBuilder.build_base_model`,
the `ontology_term` / `match_role_aliases` / `allow_incomplete_complexes`
parameters, caller-supplied templates with classification skipped, the two
roleless-path compensations gated on `reactions_to_add`, ATP correction run
exactly once with the supplied core template, `ReconstructionResult`, the AST
keyword-argument test, and the real-dependency build test.
`gapfill_metabolic_model` was NOT touched — part C.

---

## What changed

| file | change |
|---|---|
| `src/kbutillib/domains/modeling/ms_reconstruction_utils.py:8-31` | `json`, `dataclass`, `Mapping`, `Tuple` imports |
| `…:35-54` | `__all__` extended with `DEFAULT_ONTOLOGY_TERM`, `TEMPLATE_KEY_TO_GENOME_CLASS_LABEL`, `UNKNOWN_REVISION`, `ReconstructionResult`, `resolve_distribution_provenance` |
| `…:111-131` | `TEMPLATE_KEY_TO_GENOME_CLASS_LABEL`, `DEFAULT_ONTOLOGY_TERM`, `UNKNOWN_REVISION` |
| `…:351-354` (unchanged line) | part A's alias probe `MSBuilder(...)` made keyword-only (the AST test covers it) |
| `…:593-621` | new `_draft_model_facts(model=...)` — the DRAFT measurement, read off the model |
| `…:624-666` | new `resolve_distribution_provenance(name)` — version + `direct_url.json` commit, else literal `"unknown"` |
| `…:668-840` | new `ReconstructionResult` frozen dataclass (`to_dict` at :830); a per-field docstring naming its stage |
| `…:1050-1127` | compensations lifted into `_apply_roleless_path_compensations(mdlutl=, builder=)`, comments preserved; `build_non_metabolite_reactions(cobra_model=…)` at :1100 and `add_exchanges_to_model(model=…)` at :1127 now keyword |
| `…:1129-1199` | new `_base_model_keywords(...)` — signature-probed kwargs for `build_base_model` |
| `…:1201-1304` | `build_metabolic_model` signature + docstring: four new keyword parameters, documented `Returns`/`Raises` |
| `…:1314-1322` | core template fetched only when `atp_safe` (it is a KBase workspace ref) |
| `…:1348-1374` | classification skipped when `gs_template_obj` is supplied; otherwise part A's `classify_genome` |
| `…:1384-1410` | `MSBuilder(genome=, template=, ontology_term=)` at :1398 + `build_base_model(**_base_model_keywords(...))` at :1403; `build()` never called |
| `…:1442-1475` | compensations gated on `reactions_to_add`; DRAFT facts and `pre_atp_reaction_ids` captured at the DRAFT boundary |
| `…:1476-1591` | ATP block: default-media pre-load with the flag forced False, keyword `MSATPCorrection(...)` at :1524, one `run_atp_correction()` at :1540, POST-ATP collection |
| `…:1593-1639` | the `ReconstructionResult` construction and the third return element |
| `tests/modeling/test_recon_build.py` | NEW — 55 unit tests on fakes (54 pass + 1 xfail), incl. the AST test |
| `tests/modeling/test_recon_build_real_dependency.py` | NEW — 25 real-dependency tests (`kbu_model`) |
| `tests/modeling/test_ms_reconstruction_utils.py` | fakes moved from `build` to `build_base_model`; two superseded assertions rewritten (see "What I changed in existing tests") |
| `tests/test_recon_upstream_real_dependency.py:484-501` | the smoke's `assert blocked` replaced by `assert it runs` (see below) |

No `genome_annotation_aggregator` import was added: `grep -rn
genome_annotation_aggregator src/kbutillib` returns nothing.

---

## MEASURED

Everything in this section was executed in this worktree today (2026-11-15) in
`/scratch/chenry/venvs/kbdl-recon-scratch-env` — python 3.11.15, modelseedpy
**0.4.3** at commit `e1155a41fa53b921185b20c6cfbd083e5e3739a4` (read out of
`direct_url.json`, not inferred), cobra 0.32.1, this branch editable. The fork
venvs `/scratch/chenry/venvs/kbdl` and `/home/chenry/venvs/kbdl` were never
used.

### Real-dependency runs: THREE, and what each cost

The budget rule allowed at most three. All three are runs of the committed test
file; no ad-hoc reconstruction script was run at any point.

| # | selection | wall time | outcome |
|---|---|---|---|
| 1 | `-k AliasMatchingOnTheClusterShape` (the cheap in-memory half, deliberately first to shake out API errors before paying for ATP correction) | **36.6 s** total, 26.8 s in pytest | 5 passed |
| 2 | the whole file | **107.9 s** total, 88.1 s in pytest; the ATP build itself **61.2 s** | 25 passed |
| 3 | not used | — | — |

Run 2 passed first time, so the third run was not needed.

### The role-based build, measured (run 2)

`H. influenzae` 71421.8 (1,826 features) against the native
`GramNegative.modeltemplate` plus `CoreModelTemplateV2`, both SUPPLIED,
`gs_template="auto"`, `reactions_to_add=None`, `atp_safe=True`,
`load_default_medias` default (54 media). Solver **glpk**, chosen by a real
solve (`solver_probes: {'glpk': True}`).

| field | value |
|---|---|
| `draft_reaction_count` | **957** |
| `draft_metabolite_count` | **967** |
| `draft_gene_count` | **588** |
| `draft_exchange_count` | **73** |
| `draft_drain_count` | 4 |
| `draft_biomass_ids` | `('bio1',)` |
| `draft_objective_expression` | `1.0*bio1 - 1.0*bio1_reverse_b18f7` |
| `draft_atpm_present` | **False** |
| `post_atp_correction_ran` / `_invocations` | True / **1** |
| `post_atp_reaction_count` | **958** |
| `post_atp_removed_reaction_ids` | **`()` — EMPTY** (see below) |
| `post_atp_core_gapfilling_count` | **0** |
| `post_atp_selected_media_ids` | `('Glc', 'Glc.DMSO', 'Glc.TMAO')` |
| `post_atp_test_count` | 4 |
| `post_atp_analysis["atp_hydrolysis_id"]` | `rxn00062_c0` |
| `post_atp_analysis["atp_media_count"]` | 54 |
| `post_atp_analysis["media_gapfill_stats"]` | 54 entries, keyed by media **ID** |
| `post_gapfill_results` | `()` |
| `genome_class` | **None** — classification was skipped |
| `template_id` / `core_template_id` | `GramNegative.modeltemplate` / `CoreModelTemplateV2` |
| `modelseedpy_version` / `_revision` | `0.4.3` / `e1155a41fa53b921185b20c6cfbd083e5e3739a4` |
| `kbutillib_version` / `_revision` | `1.0.0` / `unknown` |
| legacy `current_output` | `Genes 1826, Model genes 588, Reactions 880, Core GF 0, Class None` |

The draft numbers reproduce the smoke's section (a) measurements (957 / 588 / 73)
exactly, which is the strongest available statement that `build_base_model` is
doing the same thing through `build_metabolic_model` as it did when called
directly.

**`post_atp_removed_reaction_ids` came back EMPTY on this genome.** The reaction
count went 957 → 958, i.e. +1 for the `rxn00062_c0` ATP-hydrolysis scaffold
`MSATPCorrection.__init__` mints, and nothing was removed; core gap-filling
added nothing either. So "the ATP-removed set is reported" is satisfied — the
field is populated, sorted, agrees with `post_atp_analysis["removed_reaction_ids"]`
and is disjoint from the final model — but it is NOT demonstrated non-empty here.
I did not hard-code `== 0` in the test, because that number is a property of this
genome/template pair rather than of the code; the test asserts the internal
consistency properties instead. This is consistent with the smoke's conclusion
that #28/#29 are roleless-path artifacts: a role-based draft reaches ATP
correction with 73 exchanges and all its diffusion reactions, so there was
nothing ATP-breaking to filter.

### Alias matching on the KBDL cluster shape (runs 1 and 2)

One in-memory template role representing one mapping cluster: NAMED after the
cluster's first function, carrying BOTH cluster functions as aliases. One gene
carrying ONLY the second function. No biochemistry, no solver, no ATP step.

| `match_role_aliases` | genes on the model | reactions on the model |
|---|---|---|
| `False` | **`[]`** | `[]` |
| `True` | **`['cluster_gene_second']`** | `['rxn90011_c0']` |

The flag discriminates, and it carries the reaction the cluster drives with it.

### The upstream `load_default_medias` trap, measured in the source

`MSATPCorrection.__init__` (`msatpcorrection.py:113-114` in the installed pin)
reads:

```python
if load_default_medias:
    self.atp_medias = load_default_medias(default_media_path)
```

`load_default_medias` is BOTH a module-level function (`:38`) and the `__init__`
parameter (`:79`); inside the body the name resolves to the **bool parameter**,
so for any truthy value the call raises `TypeError: 'bool' object is not
callable`. Upstream's own `build()` never trips it because it passes
`load_default_medias=False` and pre-loads the media itself. The old
`build_metabolic_model` forwarded the flag straight through with default True,
so every ATP-safe build against upstream would have raised before solving.

The layer therefore pre-loads through the module function and forces the flag
False — and that is why `atp_media_count` above is 54 rather than 0, on a run
that completed. The fork has no module-level function (its loader is the METHOD
`self.load_default_medias`, which `__init__` calls unambiguously), so there the
flag is forwarded as the caller asked. The branch is on `callable(getattr(module,
"load_default_medias", None))`, i.e. probed, because the two trees report 0.4.2
and 0.4.3 and a version string identifies nothing.

### The AST keyword test, and what it found

`tests/modeling/test_recon_build.py` walks the module's AST, follows locals bound
to a ModelSEEDpy construction (`builder = self.MSBuilder(...)`,
`atpcorrection = ...`, `msgapfill = ...`), and requires zero positional
arguments. Receivers more than one attribute past the target are excluded
deliberately — `builder.base_model.add_metabolites(...)` and
`atpcorrection.media_gapfill_stats.items()` are calls on a cobra model and on a
dict, and the fork/upstream divergence this rule exists for is at the
ModelSEEDpy API boundary. A self-check test asserts the probe finds
`build_base_model`, `run_atp_correction`, `add_exchanges_to_model` and
`build_non_metabolite_reactions`, so it cannot pass vacuously.

It found three positional call sites outside gap-fill, all now fixed:

* `ms_reconstruction_utils.py:351` — part A's `MSBuilder(genome, template)`;
* the compensation's `builder.build_non_metabolite_reactions(mdlutl.model, …)`;
* `self.MSBuilder.add_exchanges_to_model(mdlutl.model)`.

**For part C**, these four MSGapfill-group call sites still pass positionals and
are covered by `test_msgapfill_calls_use_keyword_arguments_only`, marked
`xfail(strict=True, reason="part C")` — strict, so when part C fixes them the
test fails loudly and must be deleted rather than lingering:

| line | call | positionals |
|---|---|---|
| 2051 | `self.MSGapfill(mdlutl, templates, source_models, all_tests, …)` in `gapfill_metabolic_model` | 4 |
| 2068 | `msgapfill.compute_reaction_weights_from_expression_data(expression_obj, genome.annoont)` | 2 |
| 2081 | `msgapfill.run_multi_gapfill(media_list, …)` | 1 |
| 2228 | `self.MSGapfill(mdlutl, templates, source_models, all_tests, …)` in `run_comprehensive_gapfill_on_model` | 4 |
| 2247 | `msgapfill.run_multi_gapfill(media_list, …)` | 1 |

(The task prompt named one call at `:915`; by this tree it is two constructions
and three method calls at the lines above — see the reconciliation section.)

### Test runs and exit codes

All from the worktree root.

```
# unit tests, fakes only -- the new module
/scratch/chenry/venvs/kbdl-recon-scratch-env/bin/python -m pytest \
    tests/modeling/test_recon_build.py -q
54 passed, 1 xfailed                                       exit 0

# unit tests, the three recon modules together
… -m pytest tests/modeling/test_recon_build.py \
    tests/modeling/test_ms_reconstruction_utils.py \
    tests/modeling/test_recon_env_init.py -q
134 passed, 1 xfailed                                      exit 0

# real-dependency, run 1 (cheap half)
KBU_REQUIRE_MODEL_STACK=1 … -m pytest \
    tests/modeling/test_recon_build_real_dependency.py -q \
    -k AliasMatchingOnTheClusterShape
5 passed, 20 deselected in 26.75s                          exit 0

# real-dependency, run 2 (whole file)
KBU_REQUIRE_MODEL_STACK=1 \
KBU_RECON_BUILD_REPORT=/scratch/chenry/tmp/kbu-recon-build-full.json … -m pytest \
    tests/modeling/test_recon_build_real_dependency.py -q
25 passed in 88.08s                                        exit 0

# ruff, every file I touched
/scratch/chenry/venvs/kbdl-recon-scratch-layer/bin/ruff check \
    src/kbutillib/domains/modeling/ms_reconstruction_utils.py \
    tests/modeling/test_recon_build.py \
    tests/modeling/test_recon_build_real_dependency.py \
    tests/modeling/test_ms_reconstruction_utils.py \
    tests/test_recon_upstream_real_dependency.py
1 error: I001 import block un-sorted in tests/modeling/test_ms_reconstruction_utils.py
```

That one lint error is **pre-existing**: the same command against the base tree
reports the same single I001, and I did not touch that file's import block.

I did NOT run the full pytest suite — the task forbids it and the reviewer does
the base-vs-branch node-id comparison. The scoped comparisons I did run are
below.

### Scoped base-vs-branch node-id comparison

Base `35309ff` materialised read-only with
`git archive 35309ff | tar -x -C /scratch/chenry/tmp/kbu-base-ec0feb19` (no
branch created, no worktree switched), imported via
`PYTHONPATH=<archive>/src` and confirmed with
`python -c "import kbutillib; print(kbutillib.__file__)"` → the archive's copy,
not the editable worktree.

**(a) Every other consumer of what I changed** — `tests/cli/test_model.py`
(its `reconstruct` verb calls `build_metabolic_model`),
`tests/cli/test_model_recording.py`,
`tests/modeling/test_comprehensive_gapfill_wrapper.py`,
`tests/kbase/test_kb_model_utils.py`:

```
base  : 8 failed, 18 passed, 43 skipped, 2 errors   exit 1
branch: 8 failed, 18 passed, 43 skipped, 2 errors   exit 1
diff of the sorted FAILED/ERROR name lists: EMPTY (10 names on each side)
```

Identical name sets. **0 regressions.** The 8 failures and 2 errors are
pre-existing and unrelated (they need a live KBase workspace or the full
scientific stack); the suite is not green and I am not claiming it is.

**(b) The two recon test modules that existed at base**
(`test_ms_reconstruction_utils.py`, `test_recon_env_init.py`), comparing PASSING
node-ids by NAME:

```
base passing  : 80
branch passing: 80
lost  : 1 -- test_exchanges_are_added_when_there_are_no_reactions_to_add
gained: 1 -- test_the_compensations_are_skipped_when_there_are_no_reactions_to_add
```

That is the single deliberate rename described below, not a loss of coverage.
`test_recon_build.py` (+54 passing, +1 xfail) and
`test_recon_build_real_dependency.py` (+25) are new and have no base
counterpart.

---

## How each compensation was handled, and on what evidence

Both now run ONLY when `reactions_to_add` is supplied, inside
`_apply_roleless_path_compensations`. The original rationale comments are
preserved verbatim in that method, because on the roleless path they are still
the difference between a model that can respire and one that cannot.

**Evidence: MEASURED, not inferred.** `agent-io/work-records/task-d1302dc4.md`
section (f), on this exact genome and template, with `reactions_to_add=None`:

| compensation | returned | added to the model |
|---|---|---|
| `build_non_metabolite_reactions` re-run | **0 reactions** | **none** |
| `add_exchanges_to_model` | — | **none** |

The draft stayed at 957 reactions. My own run 2 corroborates the premise from
the other side: on the native path, with both compensations SKIPPED, the draft
still carries 73 exchanges, `bio1`, the objective, and 957 reactions — the same
figures the smoke measured with them running. Had either been contributing
anything, those numbers would differ.

Gating is not only an optimisation. `build_base_model` makes its own
`build_non_metabolite_reactions` call and its own `add_exchanges_to_model` call;
re-running both unconditionally means every native build re-sweeps the template
to confirm a no-op, and any future divergence between the two inclusion
predicates would double-apply silently.

One correction carried forward from the smoke: the task prompt calls the two
compensations "the ATP-safe annotation overlay and the spontaneous/universal
re-run". The ATP-safe annotation overlay is **not** in `build_metabolic_model`
— it lives in GAA's `recon_driver`, which the PRD forbids importing. The two
that are really in this method are the `build_non_metabolite_reactions` re-run
(#29) and the `add_exchanges_to_model` call before ATP correction (#28). Those
are the two I gated.

---

## The return contract, and which path existing callers take

`build_metabolic_model` keeps `(current_output, mdlutl)` as its DEFAULT return.
The structured result is opt-in through
**`return_reconstruction_result=True`**, which appends a third element:
`(current_output, mdlutl, ReconstructionResult)`.

Four call sites unpack exactly two values and are unchanged:
`ms_reconstruction_utils.py:1849` (`kb_build_metabolic_models`),
`src/kbutillib/interfaces/cli/model.py:763`, and two documented examples in
`src/kbutillib/beril/skills/kbu-fba/SKILL.md`. Returning the result alone would
have broken all of them and would also have discarded the live `MSModelUtil`,
which every caller needs.

The DRAFT facts and the pre-ATP reaction-id set are captured only when the flag
is set, so the legacy path reads nothing off the model or off `MSATPCorrection`
that it did not read before. `TestLegacyReturnPath` asserts the legacy
`current_output` dict is byte-identical between the two paths.

---

## What I changed in existing tests, and why that is not weakening them

Three existing assertions encoded the OLD design and could not survive it. Each
is replaced by an assertion of the new contract with the reasoning in its
docstring; none was deleted or skipped.

1. **`test_exchanges_are_added_when_there_are_no_reactions_to_add`** asserted the
   exchange call was *unconditional*. That premise was the roleless path's need.
   Renamed to `test_the_compensations_are_skipped_when_there_are_no_reactions_to_add`
   and inverted, citing the section (f) measurement. This is the one node-id that
   disappears from the passing set.
2. **`test_an_empty_gene_mapping_does_not_raise`** asserted
   `admitted_per_call == [0, 0]`, i.e. that the compensating rebuild ran anyway.
   Now `[0]` — only `build_base_model`'s own rebuild. Same node-id, same
   purpose (it must not raise), updated count.
3. **`tests/test_recon_upstream_real_dependency.py` `test_a1`** asserted
   `blocked`: that `build_metabolic_model` could NOT run against upstream. That
   was the measurement the layer was designed against, and part B is the change
   that retires it. It now asserts construction succeeded and nothing raised,
   with a comment naming both repairs. The `upstream_build_param_binding`
   assertions above it are untouched and are the durable half of that
   measurement: they are facts about upstream's `build()`, which is exactly why
   the layer must never call it. That module is opt-in
   (`KBU_REQUIRE_MODEL_STACK=1`) and skips in the ordinary suite, so it
   contributes no node-id change either way.

The `_Ordered*` and `_Spontaneous*` fakes moved from `build` to
`build_base_model` (with the pinned release's real parameter names, so the
signature probe sees the production shape) and now borrow
`_base_model_keywords` and `_apply_roleless_path_compensations` from the real
class rather than re-implementing them. Two helper calls gained
`load_default_medias=False`, because the pre-load reads a real TSV from
`self.modelseedpy_data_dir`, which those fakes deliberately point at a
nonexistent path; the pre-load has its own four tests.

---

## INFERRED, not measured

* **That the fork forwards the `load_default_medias` flag correctly.** The fork
  branch of that code is reached only when the module has no
  `load_default_medias` function. I established the fork's shape by reading the
  smoke's record and part A's, and I unit-test the branch by deleting the
  attribute with `monkeypatch`. I did NOT run it against the fork venv — the
  task forbids testing in `/scratch/chenry/venvs/kbdl`.
* **That `ModelSEEDpyCapabilityError` is the right failure for an unsupported
  keyword.** The raise path is unit-tested against a fake `build_base_model`
  without the keyword-only parameters; I did not install a ModelSEEDpy lacking
  them, so the shape of the OLD signature is taken from the smoke's reading of
  `c0b94e3` rather than measured here.
* **That `post_atp_removed_reaction_ids` is non-empty for some genome.** Not
  observed. On *H. influenzae* against these templates it is empty (above). The
  field's correctness rests on the set-difference arithmetic, which is unit
  tested against a fake corrector that removes a known reaction, plus the
  consistency assertions on the real run.
* **That `kbutillib_revision` would be a real SHA for a non-editable install.**
  Here it is `"unknown"`, which is what an editable install must report. The
  populated path is unit tested against a fabricated `direct_url.json`.
* **That `classify_genome` is reached in production.** The classification branch
  now calls part A's `classify_genome`; my real-dependency build supplies both
  templates, so it SKIPS classification by design. Part A measured
  `classify_genome` returning `gn` on this genome; I did not re-measure it, and
  no real run of mine exercised the `gs_template_obj=None` branch. It is covered
  by four unit tests on fakes.
* **That `allow_incomplete_complexes=False` changes a real build.** Only its
  forwarding is tested, not its effect.

---

## Reconciled against the tree (prompt claim → what the tree holds → what I did)

1. **"`build_metabolic_model` (…:238-503)"** and **"the positional
   `builder.build(base_model, "0", False, False)` at :373"** and **"MSATPCorrection
   construction near :474"** and **"compensations (:400-470)"**. All stale: part A
   inserted ~500 lines of environment code above the class. In this tree
   `build_metabolic_model` begins at **:1201**, the `build` call is now the
   `build_base_model` call at **:1403**, the compensations live in a method at
   **:1050-1127** called from **:1467**, and the `MSATPCorrection` construction is
   at **:1524**. I located every one by name.
2. **"gapfill call at :915"**. Stale, and singular where the tree is plural.
   `gapfill_metabolic_model` runs **:1965-2126** and `run_comprehensive_gapfill_on_model` from **:2127** and the MSGapfill-group
   positional calls are the five listed in the AST section above (two
   constructions, three method calls, across two methods). I marked ONE xfail
   assertion covering all five, as instructed, rather than five.
3. **"The ModelSEEDpy change adds two opt-in keyword parameters to
   `build_base_model` … which accepts it at :633 but is called without it at
   :1097."** Stale in the same way part A found: at the pinned `e1155a4` the
   change is already landed. `build_base_model` carries
   `*, match_role_aliases=False, allow_incomplete_complexes=True`
   (`msbuilder.py:1069-1081`) and forwards both to
   `generate_reaction_complex_sets` at `:1114-1117`, which accepts them at
   `:643`. Nothing was needed upstream. I still probe the signature rather than
   assume it, because the fork does not have them.
4. **"an explicit `ontology_term` (default 'RAST') passed to MSBuilder"**.
   Correct and available: `MSBuilder.__init__(genome, template=None, name=None,
   ontology_term="RAST", index="0", template_core=None)`. It raises `TypeError`
   on a non-`str`, so there is no None-means-default path. I did NOT also pass
   `template_core=` — not asked for, and ATP correction receives the core
   template directly.
5. **"Run ATP correction exactly once with the supplied core template."** Doing
   this at all required a fix the prompt does not mention: upstream's
   `load_default_medias` bool/function collision (measured above), which makes
   any ATP-safe build raise before solving. I fixed it because otherwise the
   stated criterion is unreachable. I also had to gate the
   `core_template is None` fetch on `atp_safe`: `self.templates["core"]` is a
   KBase workspace reference, so a KBase-free build with `atp_safe=False` would
   otherwise have needed a workspace to fetch a template it never uses.
6. **"`MSBuilder.add_atpm`"** is NOT called. The prompt does not ask for it, and
   it is unnecessary: `MSATPCorrection.__init__` mints `rxn00062_c0` itself via
   `MSModelUtil.add_atp_hydrolysis(compartment)` (`msmodelutl.py:1108-1141`),
   which is a direct construction and not the "SEED reaction search" the
   docstring warns about. Measured: `atp_hydrolysis_id` came back
   `rxn00062_c0` and `ATPM_c0` is absent from the draft, which is exactly the
   stage boundary the PRD specifies. Calling `add_atpm` before ATP correction
   would have put an ATP-specific maintenance reaction INSIDE the draft, which
   the fixed DRAFT definition forbids.
7. **"a per-medium gap-fill results field (an empty tuple from build; part C
   fills it)"**. Implemented as `post_gapfill_results: Tuple[Any, ...]`. I
   deliberately did NOT define the per-medium record type — that is part C's
   shape to choose, and the salvage branch's `GapfillRecord` is unreviewed. A
   field typed to the contract it has today is honest; one typed to a record
   class nothing produces is not.
8. **"No getattr defaults."** Honoured in the construction of
   `ReconstructionResult`: every value comes from a measured attribute or an
   explicit `None` for a stage that did not run. The two `getattr` calls that
   remain in the ATP block are capability PROBES
   (`getattr(module, "load_default_medias", None)` and
   `media.id if hasattr(media, "id")`), not field defaults.
9. **Reference branch `maestro/salvage/task-7c660a83` (f5790fd).** I read its
   `resolve_distribution_provenance`, `ReconstructionResult` and ATP block with
   `git show` to save design time, as permitted. Nothing was cherry-picked or
   merged, and nothing it claims is treated as evidence: its measurements came
   from the wrong ModelSEEDpy. I diverged from it in three places —
   no `add_atpm` (see 6), no `GapfillRecord` (see 7), and no `solver` field on
   the result (the task's enumerated field list does not include it).

---

## Notes for part C

* `post_gapfill_results` is the field to fill, and the type is open. If you add
  a record class, keep `ReconstructionResult` frozen and use
  `dataclasses.replace`.
* `mdlutl.atp_tests` is populated POST-ATP (4 tests on the measured build), so
  genome-scale gap-fill can be ATP-safe without a second ATP run.
* `test_msgapfill_calls_use_keyword_arguments_only` is `xfail(strict=True)`.
  When you make those five call sites keyword-only it will FAIL as "unexpectedly
  passed" — delete the marker, do not widen it.
* `_base_model_keywords` is the pattern for any further opt-in ModelSEEDpy
  keyword: probe the signature, forward what exists, raise
  `ModelSEEDpyCapabilityError` when a non-default request cannot be honoured.
* The real-dependency harness dumps every measurement to
  `$KBU_RECON_BUILD_REPORT` as JSON. Reuse it rather than printing.
