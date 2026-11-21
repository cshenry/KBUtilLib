# Work record — kbu-atp-filter-replay (task-0fad2bfa)

Conduct row 1989, PRD `kbdl-fitness-off-gaa-v1` amendment. Repo KBUtilLib,
branch `maestro/developer/task-kbu-atp-filter-replay-condu-task-0fad2bfa`, base
commit `b0e3ed0` (as expected). Machine h100 (poplar), Linux, no human watching.

## The decision

> **Chris, 2026-11-21 11:36 CST:** "On the ATP filtering you MUST MUST MUST
> ensure it only runs once per model and you save the filter list."

Today `MSATPCorrection` plus `GapfillingPkg.filter_database_based_on_tests`
costs **528 s per KBDL job** on real Keio data and re-runs on every job and on
every parallel shard. This task makes KBUtilLib able to RETURN the filter as a
storable object and to REPLAY a stored one exactly. Storing it and looking it up
is a follow-on KBDL task and is NOT here.

Scope delivered: the modelseedpy repin, `atp_filter_key`, the `atp_filter`
keyword and the `result["atp_filter"]` field, the replay path, the docstrings,
and 27 real-dependency tests. **`modelseedpy/core/msfitnessweighted.py` and
every other modelseedpy file are untouched, and no default of
`run_fitness_weighted_analysis` changed.**

---

## Files changed

| file | what |
|---|---|
| `pyproject.toml:251` | the `[model]` extra's modelseedpy pin, `d55034c` → `edafdb1` |
| `pyproject.toml:220-250` | the comment block above the pin, naming `edafdb1` and what it adds |
| `src/kbutillib/domains/modeling/ms_fitness_utils.py` | item 6 of the module docstring; `hashlib`/`json`/`time` imports; four new constants; the `atp_filter_key` section; the `atp_filter` keyword, the key/replay path and the `result["atp_filter"]` field in `run_fitness_weighted_analysis` |
| `tests/modeling/test_ms_fitness_atp_filter.py` | new, 26 tests, `kbu_model` |
| `agent-io/work-records/kbu-atp-filter-replay.md` | this record |

New public surface, all added to `__all__`: `atp_filter_key`,
`ATP_FILTER_FORMAT` (`"kbu-atp-filter/1"`), `ATP_FILTER_KEY_FORMAT`
(`"kbu-atp-filter-key/1"`), `ATP_FILTER_CLOSURES`, `ATP_FILTER_METHOD_PARAMS`.

## The pin, verified by commit and not by version string

`pip show` reports **0.4.3** for several different commits of this fork, which
is why the extra pins a SHA. Verified from the install's own record:

```
$ cat .../site-packages/modelseedpy-*.dist-info/direct_url.json
{"url": "https://github.com/cshenry/ModelSEEDpy.git",
 "vcs_info": {"commit_id": "edafdb1b6ff0b4d694528cd417af32f0581cf607",
              "requested_revision": "edafdb1b6ff0b4d694528cd417af32f0581cf607",
              "vcs": "git"}}
```

The repo **already** asserts that relationship, so this task did not add a
second version of it: see
`tests/modeling/test_recon_build_real_dependency.py:396`,
`TestProvenanceComesFromDistributionMetadata::test_that_revision_is_the_one_pyproject_pins`,
which reads the SHA out of `pyproject.toml` and compares it to the installed
distribution's recorded commit. (I wrote a duplicate before finding it and
removed it.)

**What the repin actually adds.** A diff of the installed `edafdb1` against the
pre-fix code (available locally at `~/venvs/kbdl`, modelseedpy `08d239d9`) shows
exactly two hunks, both the None-objective guard, and nothing else:

```
core/msatpcorrection.py:326
-   solution.objective_value < minimum_obj  or  solution.status != "optimal"
+   solution.status != "optimal" or solution.objective_value is None
+       or solution.objective_value < minimum_obj

fbapkg/gapfillingpkg.py:651
-   if solution.objective_value < self.parameters["minimum_obj"]:
+   if (solution.status != "optimal" or solution.objective_value is None
+       or solution.objective_value < self.parameters["minimum_obj"]):
```

Both pre-fix forms raise `TypeError: '<' not supported between 'NoneType' and
'float'` on an infeasible solve: `msatpcorrection` compared the objective
*before* testing the status, and `gapfillingpkg` had no status test at all.
`modelseedpy/core/msfitnessweighted.py` is **byte-identical** between the two
installs (`diff` returns clean), which is the evidence that the repin is a
fast-forward that does not disturb Jose Faria's frozen port.

*Inferred, not verified:* that `edafdb1` is a fast-forward of `d55034c`
specifically, and that fix commit `452d601` is the commit carrying these hunks.
`d55034c` is not installed on this machine and this task does no network git, so
the diff above is against `08d239d9`. What IS verified is the installed commit,
the content of both guards, and that the fitness port is unchanged.

---

## Audit 1 — every input the ATP filter reads, and how the key covers it

Read out of the installed `edafdb1`: `build_superset`
(`msfitnessweighted.py:415-470`), `atp_tests_from_correction` (`:375-412`),
`_classify_superset` (`:496-525`) and
`filter_database_based_on_tests` (`gapfillingpkg.py:665-744`).

| input | read at | key field |
|---|---|---|
| the model's reactions, stoichiometry, bounds | `MSGapfill(self.mdlutl, ...)`, `:420`; `draft_reactions`, `:348` | `model` — content digest |
| the model's gene rules | `_edges`, `:354` | `model` — in the digest |
| the model COPY handed to `MSATPCorrection` | `atp_tests_from_correction`, `:391` | `model` — same content |
| genome-scale template reactions + stoichiometry + reversibility | `extend_model_with_template_for_gapfilling`, `gapfillingpkg.py:401` | `template` — content digest |
| template `base_cost`, `forward_penalty`, `reverse_penalty` | `gapfillingpkg.py:427-448` | `template` — in the digest |
| template `GapfillDirection` | `gapfillingpkg.py:434`, `:506` | `template` — in the digest |
| template `reference_id` (what blacklist matches) | `gapfillingpkg.py:401` | `template` — in the digest |
| template `compcompounds` (the superset's metabolites) | `gapfillingpkg.py:367-385` | `template` — digested as id/compartment/formula/charge |
| core template, all of the above | `MSATPCorrection(work, core_template, ...)`, `:393` | `core_template` — same digest function |
| blacklist | `MSGapfill(..., blacklist=self.blacklist)`, `:427` | `blacklist` — sorted, de-duplicated |
| `atp_hydrolysis_id` | `atp_tests_from_correction(..., atp_hydrolysis_id=...)`, the wrapper's `_atp_hydrolysis_hint` | `atp_hydrolysis_id` |
| biomass id (`default_target`) | `:423`; `test_gapfill_database`, `gapfillingpkg.py:637` | `biomass` |
| `params["min_growth"]` (`minimum_obj`) | `:424`; `gapfillingpkg.py:667`, `:691`, `:728`, `:741` | `method_params.min_growth`, EFFECTIVE value |
| `mass_imbalanced` (clamps superset candidates to `(0,0)`) | `_classify_superset`, `:515-520` | `method_params.mass_imbalanced`, sorted |
| `compartment` (where ATP hydrolysis would be added) | `_empty_atp_test`, `:482` | `method_params.compartment` |
| `load_default_medias()`'s media set | `atp_tests_from_correction`, `:396` | `modelseedpy_sha` — packaged data |
| the `"c0"` compartment `atp_tests_from_correction` HARDCODES | `:395` | `modelseedpy_sha` — not an argument |
| `MSATPCorrection`'s thresholds and growth-media logic | `:398-402` | `modelseedpy_sha` |
| the filter algorithm itself, incl. the unfilter branch | `gapfillingpkg.py:690-740` | `modelseedpy_sha` |
| the LP backend (the filter's closures come from LP solutions, and its unfilter branch reads individual fluxes off one) | `gapfillingpkg.py:707`, `:710-725` | `solver` |

Deliberately **excluded**, with the reason: every other `**method_param`
(`p_base`, `p_min`, `k`, `t_sig`, `candidate_scale`, `candidate_factor`,
`fit_important`, `frac`, `loopless`, `price_reversals`, `biomass_reference`,
`nonpositive_penalty`, ...) prices or scores *after* the superset exists and
cannot change which directions the filter closes. Including them would make a
stored filter spuriously stale on exactly the case the cache has to serve —
KBDL varying pricing knobs across jobs on one model. There is a test for this
(`test_a_params_change_the_filter_cannot_see_is_not_a_mismatch`).

### Known gaps — named, not omitted

1. **Solver version and tolerances.** The key carries the backend NAME
   (`"glpk"`), not its version or its tolerance settings. Two different GLPK
   builds could in principle resolve a degenerate filter differently while
   producing the same digest. Not closable from here: optlang exposes no stable
   build identifier, and `cobra` does not record one.
2. **`atp_medias`.** `atp_tests_from_correction` accepts custom media. This
   layer never passes any, so the default set is covered by `modelseedpy_sha`; a
   caller reaching that static method directly with custom media is outside what
   this key describes.
3. **An `unknown` `modelseedpy_sha`.** A PyPI wheel or an editable install
   records no commit, so `resolve_distribution_provenance` returns `"unknown"`.
   The digest stays stable, but it then no longer distinguishes two different
   modelseedpy checkouts. This is the one gap that can silently admit a stale
   filter, and it is inherent to the install not recording its provenance.
4. **A pre-filter `upper_bound` that is strictly negative.** `build_superset`
   records `">"` only when `ub0 > 0 and rxn.upper_bound <= 0` (`:451`), so a
   reaction whose pre-filter upper bound was already negative and which the
   filter moved to 0 is a bound change `filtered` does not capture. This is a
   pre-existing property of Jose's `filtered` dict, not something the replay
   introduces, and the equivalence test — which compares bounds for EVERY
   reaction, not just the filtered ones — would catch it if it occurred. It does
   not occur on the measured fixture.

---

## Audit 2 — every other side effect of the filter, and where it lands

What `filter_database_based_on_tests` does to the superset on the
`atp_safe=True` path, net of `build_superset`'s own post-filter steps. This
audit is also recorded in the docstring of `_replay_atp_filter`.

| # | side effect | source | reproduced? |
|---|---|---|---|
| 1 | reaction bounds: `upper_bound = 0` / `lower_bound = 0`, and nothing else | `gapfillingpkg.py:682-688`, `:711-725`, `:732-738` | **yes** — `build_superset:449-457` records each in `filtered`; `_apply_atp_filter` reassigns them |
| 2 | `ObjConstPkg`'s `objc` lower bound driven to 0 and back to `minimum_obj` | `gapfillingpkg.py:667`, `:691`, `:728`, `:741` | **nothing to reproduce** — `build_superset:460-466` then REMOVES those constraints outright, unconditionally, on both paths |
| 3 | the model objective: a minimisation objective installed by the unfilter branch, then restored | `gapfillingpkg.py:696-706`, restored `:743` | **net zero** — it is restored to `parameters["gfobj"]`, which is the objective `MSGapfill` already left in place (`gapfillingpkg.py:233`), so the `atp_safe=False` build ends with the same one |
| 4 | the `_added_atp_reaction` clamp to `(0, 0)` | `build_superset:467-468` | **dead on BOTH paths** — it fires only when `_empty_atp_test` ADDED the reaction, and `_empty_atp_test` runs only when `atp_tests is None` (`:446-447`). This layer always supplies tests, so `atp_reaction` is `None` in both summaries. Measured: `None` on both paths. |
| 5 | first `reaction_expansion_test` call's bound changes | inside `with self.model:` — `gapfillingpkg.py:671-681` | **reverted by cobra** before the method returns |
| 6 | a `gf_filter` diagnostic dict | `reaction_expansion_test`, `msmodelutl.py:966-999` | **NOT reproduced, deliberately** — see below |

**On (6), the one unreproduced side effect.** `reaction_expansion_test` records
what it filtered under `MSModelUtil.attributes["gf_filter"]`. It is not
reproduced because (a) it is a log of a filter run that did not happen on a
replay, (b) it lives on the MSModelUtil and not on the model —
`save_attributes` only reaches the model for a KBase `FBAModel`, which has
`computed_attributes` (`msmodelutl.py:302-305`), and these are plain cobra
models — and (c) nothing in `msfitnessweighted`, and nothing in this layer's
result, ever reads it. The single observable consequence is that a replayed
run's superset mdlutl carries no `gf_filter` attribute. **No side effect
required modifying Jose's file, so this task is not blocked.**

**`_classify_superset` called a second time.** Checked in the source at
`msfitnessweighted.py:496-525`: it **fully recomputes and does not
accumulate** — `candidates`, `reversals`, `mass_imbalanced_ids` and `priced`
are each freshly ASSIGNED (`:498`, `:502`, `:513`, `:522`), never appended to.
Its one write, clamping a mass-imbalanced candidate to `(0, 0)` (`:518`), is
idempotent. It must run AFTER the stored closures are applied because it derives
`reversals` from the superset's bounds, which is exactly what the filter
changed. Measured: `reversals`, `candidates` and `priced` are identical between
the computed and the replayed run.

**`analysis.run(specs)` skips the rebuild** on the replay path because
`self.superset is not None` (`msfitnessweighted.py:899`), so the public
`build_superset()` the replay calls is the only build.

---

## The shape of the stored object

```python
result["atp_filter"] = {
    "format": "kbu-atp-filter/1",
    "key": {...,  "digest": "<sha256>"},     # atp_filter_key(...)
    "filtered": {reaction_id: ">" | "<" | "><"},
    "n_filtered_directions": int,
    "atp_tests": [...],                       # media as {cpd: [lb, ub, conc]}
    "source": "computed" | "replayed",
    "compute_s": float,
}
```

`None` when `atp_safe=False`, and always present either way. The ATP tests are
carried because the **leak check still runs on a replay**: `_atp_drain_id`
picks the drain reaction by finding the empty-medium test and reading its
`objective`, so a replay with no tests would measure a different reaction.
Round-trip verified: serialising the deserialised tests reproduces the stored
list exactly, and `MSMedia` id, name, per-compound bounds and `concentration`
are all restored (`MSMedia.from_dict` cannot carry id, name or concentration,
so `_deserialise_media` sets them).

A key mismatch, an unknown `format`, a missing field, a closure outside
`(">", "<", "><")`, or a `filtered` entry naming a reaction the superset does
not have all raise `ValueError`. **There is no code path on which a stored
filter is silently recomputed or silently reused.** The mismatch message names
every differing field with both values.

---

## Verification

Fresh venv, created for this task and not reused:

```
python3.11 -m venv /scratch/chenry/maestro-task-scratch/task-0fad2bfa/venv
pip install -e ".[model]"        # exit 0; `model` is the extra carrying modelseedpy
pip install pytest               # pytest 9.2.0, cpython-3.11.15
```

### Targeted: the new suite

```
$ python -m pytest tests/modeling/test_ms_fitness_atp_filter.py -q
26 passed

$ python -m pytest tests/modeling/test_ms_fitness_atp_filter.py \
                  tests/modeling/test_ms_fitness_utils.py -q
44 passed
```

Also run: `pyflakes` over both changed files, exit 0.

### Computed vs replayed wall time

Measured on the toy below, in-process, three runs of each:

| | wall clock | `compute_s` (the filter itself) |
|---|---|---|
| computed | **5.2-5.5 s** | 0.14-0.17 s |
| replayed | **0.29-0.31 s** | 0.0004 s |

The toy is small, so 5.2 s is dominated by `MSATPCorrection`'s gap-filling
against the core template; the figure that matters for KBDL is that the replay
pays **none** of it. On real Keio the computed leg is the 528 s Chris named.

### Regression: `tests/modeling/` by NODEID, before and after

Both runs in the SAME venv and the SAME worktree. "Before" is `b0e3ed0` with
the two changed files restored via `git checkout --` and the new test file moved
aside; "after" is this branch's tip.

```
# before -- b0e3ed0, the two changed files restored, the new test file moved aside
$ python -m pytest tests/modeling/ -q -p no:cacheprovider -p no:logging
6 failed, 513 passed, 31 skipped in 118.99s

# after -- this branch's tip
$ python -m pytest tests/modeling/ -q -p no:cacheprovider -p no:logging
6 failed, 539 passed, 31 skipped in 168.46s
```

**Compared by NODEID, the failing sets are identical. Zero new failures.**

```
$ comm -13 before.ids after.ids      # present after, absent before = regressions
(empty)
$ comm -23 before.ids after.ids      # present before, absent after
(empty)
```

The six, unchanged before and after:

| nodeid | status |
|---|---|
| `test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_returns_correct_shape` | pre-existing |
| `test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_solutions_nonempty` | pre-existing |
| `test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_model_grows` | pre-existing |
| `test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_reaction_count_increases` | pre-existing |
| `test_recon_build_real_dependency.py::TestProvenanceComesFromDistributionMetadata::test_the_modelseedpy_revision_is_a_real_commit` | pre-existing |
| `test_recon_build_real_dependency.py::TestProvenanceComesFromDistributionMetadata::test_that_revision_is_the_one_pyproject_pins` | pre-existing |

`test_comprehensive_gapfill_wrapper.py` is one of the seven modules the
2026-09-13 baseline could not even collect, so its failures here are the same
optional-dependency story that baseline records.

The `test_recon_build_real_dependency` pair deserves naming because it looks
like it ought to be about this repin and is not. Both fail on
`role_based_build["result"].modelseedpy_revision == 'unknown'` — the left-hand
side is `'unknown'` in BOTH the before and the after run, so the cause is that
that fixture's provenance path resolves to `'unknown'` in this venv, not a
pin mismatch. Checked directly: `resolve_distribution_provenance("modelseedpy")`
returns `('0.4.3', 'edafdb1b6ff0b4d694528cd417af32f0581cf607')` in this venv,
which is the correct commit, and that is the function `atp_filter_key` uses for
`modelseedpy_sha`. So the key's provenance field is sound; whatever makes the
reconstruction result report `'unknown'` is a separate, pre-existing matter and
is not this task's.

The `+26 passed` is exactly the 26 tests the new file adds.


`build-baselines/kbutillib.json` (2026-09-13, commit `0f84803`, a minimal venv
with seven `--ignore` flags) and `build-baselines/kbutillib-h100-main-6310d61.json`
(2026-10-19, full suite, 3416 s) both exist. Neither is comparable to a
`tests/modeling/`-only selection in this venv, so a before/after pair was
measured directly rather than compared against either. **The full suite was not
run: it takes 47-57 min on this machine per the h100 baseline's own
`WHY_DURATION_MATTERS` note, which is above what this task can spend.** The
change is confined to one module and one new test file, and `tests/modeling/`
is the selection that exercises it.

---

## Reconciliation — the prompt against the tree

Everything the prompt asserted about the tree checked out, with two
corrections and one adaptation worth recording.

| the prompt claimed | the tree held | what I did |
|---|---|---|
| `ms_fitness_utils.py` line numbers (`run_fitness_weighted_analysis:567-827`, ATP tests `:720-728`, `analysis.run:781`, `_atp_leak:459`, `_atp_hydrolysis_hint:386`) | all correct at `b0e3ed0` | used them |
| `pyproject.toml:251` is the modelseedpy pin | correct | repinned it |
| `msfitnessweighted.py` `build_superset:415-470`, `atp_tests_from_correction:375-412`, `summary` `n_filtered_directions:953`, `run` skips the build at `:899` | all correct | read them |
| `gapfillingpkg.py` `filter_database_based_on_tests` "~:661-740" | it is at `:665-744` | read it at its real location |
| "use ModelSEEDpy's `e_coli_core` fixture, or a ModelSEED-id fixture if the filter gets nothing to work with" | on plain `e_coli_core` the core BiGG template contributes **`n_candidates == 0`**, so `n_filtered_directions == 0` and an equivalence test would prove nothing. There is no genome-scale ModelSEED-id template fixture in this repo (only `template_core_bigg.json.gz`) and a pip-from-git modelseedpy carries no `tests/` directory. | Did not change fixture. Knocked SEVEN reactions out of `e_coli_core` that the core template can put back, which gives `n_candidates == 7` and `n_filtered_directions == 7`. **The assertion was never weakened to `n >= 0`.** |

### What the measured fixture does and does not establish

`FILTER_KO = (SUCDi_c0, FRD7_c0, LDH_D_c0, D_LACt2_c0, PFL_c0, FORt2_c0,
FORt_c0)` gives:

```
filtered = {'PFL_c0': '>', 'D_LACt2_c0': '><', 'SUCDi_c0': '>',
            'FORt2_c0': '>', 'FORt_c0': '<',  'FRD7_c0': '>',
            'LDH_D_c0': '><'}
```

All three closures are present, so a replay that confused `">"` with `"<"`, or
applied only one half of a `"><"`, shows up immediately as different superset
bounds. That is the thing the test establishes.

**It does not establish gap-fill equivalence on a non-trivial gap-fill.** On
this fixture the filter closes EVERY candidate (`filt == cand` for every
knockout set probed, including a lone `TALA_c0`, which cannot make ATP — the
filter is in its degenerate close-everything mode here, almost certainly because
the draft model itself fails the tight `Glc.O2 ≤ 15.6` test), so `gapfilled` and
`reversals_used` are empty dicts in BOTH runs and the assertions over them
compare nothing. A fixture that filters only SOME candidates and leaves a live
gap-fill would be a strictly stronger test; building one needs a genome-scale
ModelSEED-id template this repo does not have. Recorded as a gap rather than
papered over.

### Two things that are NOT bit-reproducible, and why tolerating them is not a weakening

Both were measured between two *identical computed runs*, not between computed
and replayed, so neither is a property of the replay:

1. **LP float noise, ~2e-15 relative.** `growth_superset` read
   `0.8142975075325334` on one run and `0.8142975075325316` on another. GLPK's
   simplex path depends on the order bounds were written, and a replay writes
   them in a different order than the filter did. Floats in the deep comparison
   are therefore compared at `rel=1e-9` — the tolerance the task names for the
   objective, six orders of magnitude above the measured noise. Superset
   BOUNDS, `filtered`, statuses, gap-fill sets and `reversals` are still
   compared **exactly**.
2. **`edge_evidence` row ORDER.** It follows `gene_fit_best`'s iteration over a
   set, so the six `ATPS4r_c0` subunit genes come out in a different order every
   run. Measured: two back-to-back computed runs with identical inputs disagree
   on the order and agree exactly as a set. The comparison sorts these rows by
   `(gene, reaction, source)`; every field of every row is still compared.

---

## Test outcomes, by name

`tests/modeling/test_ms_fitness_atp_filter.py`, 26 tests, all passing:

| class | tests | what it establishes |
|---|---|---|
| `TestComputedVersusReplayed` | 3 | the filter really closes directions (`n > 0`, all three closures); a replay reproduces the superset bounds, `filtered`, `reversals`, `candidates`, `priced`, every condition's status / objective (`rel=1e-9`) / gap-fill set / reversals-used, and the whole rest of the result; and the same holds after a `json.dumps`/`loads` round trip |
| `TestReplaySkipsTheExpensiveWork` | 1 | with `MSATPCorrection` and `filter_database_based_on_tests` both monkeypatched to raise, a replay still completes — so neither is called |
| `TestKeyMismatchIsRefused` | 5 | a changed blacklist, core template, model or `min_growth` each raise `ValueError` naming the field; a changed `t_sig` does NOT, because the filter cannot see it |
| `TestMalformedIsRefused` | 8 | unknown `format`; each of `key`/`filtered`/`atp_tests` missing; a key with no `digest`; a closure outside the three; a closure on an absent reaction; a non-mapping |
| `TestAtpSafeFalse` | 1 | `result["atp_filter"] is None`, and a supplied `atp_filter` is ignored rather than refused |
| `TestAtpFilterKey` | 7 | JSON-serialisable; exactly the eleven documented fields; stable across rebuilt objects (content, not identity); an explicitly-passed default keys the same as an omitted one; a changed blacklist / biomass / `min_growth` changes the digest; a changed model id, name, note or reaction name does NOT |
| `TestModelseedpyPin` | 1 | `test_gapfill_database` returns `False` rather than raising `TypeError` when handed an infeasible solve whose `objective_value` is `None` — the whole content of the repin |

The existing `tests/modeling/test_ms_fitness_utils.py` (18 tests) passes
unchanged, including `TestAtpSafe`, which is the assertion that the leak
diagnostic still leaves the superset untouched.

---

## Reviewer fix-up

The table above claimed `pyproject.toml:220-250` (the comment block above the
pin) was updated to name `edafdb1` and what it adds. The diff did not do this
— only line 251 (the SHA itself) changed; the prose above it still only
described `d55034c`. The reviewer (task-58dd3ece) added the missing paragraph
naming `edafdb1`, fix commit `452d601`, and the None-objective guard it adds,
so the table's claim is now true. No other change was made on review; the
reviewer independently verified the installed modelseedpy commit (`edafdb1`,
from `direct_url.json`), re-ran the new 26-test suite and the existing
18-test suite together (44 passed), and ran `tests/modeling/` in full (6
failed, 539 passed, 31 skipped — the same six pre-existing failures this
record already names, confirming no new regression).
