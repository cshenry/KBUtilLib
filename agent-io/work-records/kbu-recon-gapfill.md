# Work record — kbu-recon-gapfill (task-9a3f984b)

Part C of 3 of the former `kbu-recon-layer` task, PRD `kbdl-recon-thin-layer-v1`
review round 1. Repo KBUtilLib, branch
`maestro/developer/kbdl-recon-kbu-recon-gapfill`, base commit `65d8214` (the tip
of part B, kbu-recon-build).

Scope delivered: per-occurrence gap-fill records with enforced reaction
exclusions and an explicit `prefilter`; the records staged into
`ReconstructionResult.post_gapfill_results`; keyword-only calls at every
MSGapfill call site (which retires part B's strict xfail); unit tests on fakes;
and the real-dependency test. `run_comprehensive_gapfill_on_model` and
`kb_gapfill_metabolic_models` were otherwise left alone — see "Out of scope,
and the one exception".

---

## What changed

| file | change |
|---|---|
| `src/kbutillib/domains/modeling/ms_reconstruction_utils.py:8-14` | `hashlib` and `re` imports |
| `…:44-48` | `__all__` gains `GapfillRecord` |
| `…:674-734` | new `_supported_keyword_arguments(target=, requested=, label=)` — part B's `_base_model_keywords` discipline, generalised |
| `…:735-757` | new `_COMPARTMENT_SUFFIX` / `_base_reaction_id` |
| `…:759-790` | new `_media_digest` |
| `…:792-822` | new `_objective_value_on_media` |
| `…:824-856` | new `_solution_adds_anything` |
| `…:858-918` | new `GapfillRecord` frozen dataclass, with a per-field docstring |
| `…:1015-1029` | `ReconstructionResult.post_gapfill_results` docstring: names `GapfillRecord` and `with_gapfill_results`; the declared type `Tuple[Any, ...]` is UNCHANGED |
| `…:2226-2704` | `gapfill_metabolic_model` rewritten (was :1965-2126 on `65d8214`) |
| `…:2706-2733` | new `MSReconstructionUtils.with_gapfill_results` staticmethod |
| `…:2838-2847, :2857` | `run_comprehensive_gapfill_on_model`'s `MSGapfill(...)` and `run_multi_gapfill(...)` made keyword-only — the one in-scope change to that method, forced by the AST test (see below) |
| `tests/modeling/test_recon_build.py:1012-1023` | part B's `@pytest.mark.xfail(strict=True, reason="part C")` REMOVED and the docstring rewritten |
| `tests/modeling/test_recon_gapfill.py` | NEW — 53 unit tests, all on fakes |
| `tests/modeling/test_recon_gapfill_real_dependency.py` | NEW — 31 real-dependency tests (`kbu_model`) |

No `genome_annotation_aggregator` import was added, and GAA's
`gapfill_reaction_set` is neither called nor ported — a unit test asserts both
against the module source (it is NAMED in two explanatory comments; what the
test forbids is a call or an attribute reference).

---

## The design, and why `run_multi_gapfill` is gone

`gapfill_metabolic_model` now drives `MSGapfill.run_gapfilling` +
`MSGapfill.integrate_gapfill_solution` once per requested medium OCCURRENCE,
instead of handing the whole media list to `run_multi_gapfill`. Three
independent reasons, all read out of the installed pin rather than inferred:

1. **It does not accept the arguments the old call passed.** Measured signature
   at the pin: `run_multi_gapfill(media_list, target=None, minimum_objectives={},
   default_minimum_objective=None, binary_check=False, prefilter=True,
   check_for_growth=True, simultaneous_gapfilling=False)`. The old call passed
   `gapfilling_mode=`, `run_sensitivity_analysis=` and `integrate_solutions=` —
   none of which exist there — so every ATP-safe or non-ATP-safe gap-fill
   against this ModelSEEDpy raised `TypeError` before solving.
2. **It integrates nothing.** Integration is a separate call,
   `integrate_gapfill_solution(solution, cumulative_solution=[],
   link_gaps_to_objective=True)`. "Sequential integration for all requested
   media" (the PRD's POST-GAP-FILL boundary) is therefore something the caller
   has to do.
3. **Its return cannot express a repeated request.** It returns
   `{media_object: solution}`; two occurrences of one object collapse to a
   single key. The task requires one record per OCCURRENCE.

The per-occurrence loop also makes the one thing a repeat must not do
avoidable. `MSGapfill` clones its gap-fill model ONCE, in `__init__`, from the
pre-gap-fill model. A second occurrence of an already-satisfied medium would
therefore be solved against a model carrying none of the earlier additions and
return a stale solution, whose integration ends in
`MSModelUtil.test_solution(solution, keep_changes=True)` — whose own comment
says it "undoes some of what is done above", and which REMOVES reactions. So an
occurrence whose medium already meets `minimum_objective` is recorded
`"no_gaps"` and not re-solved, and a solution that would change nothing is not
integrated at all (`_solution_adds_anything`, whose conditions mirror
`integrate_gapfill_solution`'s own).

### The record

```python
@dataclass(frozen=True)
class GapfillRecord:
    request_index: int
    media_id: str
    media_digest: str
    status: str          # integrated | no_gaps | no_solution | error:<Type>
    additions: Tuple[str, ...]
    reversals: Tuple[str, ...]
    objective_value: Optional[float]
```

`additions` is measured as a set difference over the model's reaction ids across
the occurrence, NOT read off the solution — integration prunes, so the solution
is what was proposed and the record reports what was kept.

### The return contract

The DEFAULT return is still the 4-tuple `(current_output, solutions,
output_solution, output_solution_media)`; the records are a FIFTH element under
`return_gapfill_records=True`. Three call sites unpack exactly four values and
are unchanged: `ms_reconstruction_utils.py:3132` (`kb_gapfill_metabolic_models`),
`src/kbutillib/interfaces/cli/model.py:862`, and a documented example in
`src/kbutillib/beril/skills/kbu-fba/SKILL.md:312`.

`with_gapfill_results(result=, gapfill_records=)` does the staging, through
`dataclasses.replace`, so `ReconstructionResult` stays frozen and the input is
not mutated. It fills `post_gapfill_results` and nothing else; no field was
added to `ReconstructionResult` and its declared type was not narrowed.

---

## MEASURED

Everything in this section was executed in this worktree today (2026-11-15) in
`/scratch/chenry/venvs/kbdl-recon-scratch-env` — python 3.11.15, modelseedpy
**0.4.3** at commit `e1155a41fa53b921185b20c6cfbd083e5e3739a4` (read out of
`direct_url.json` by part A, not inferred), cobra 0.32.1, this branch on
`sys.path` via `tests/conftest.py:19`. Solver **glpk**, chosen by a real solve
(`solver_probes: {'glpk': True}`); gurobipy is not installed in this venv.
The fork venvs `/scratch/chenry/venvs/kbdl` and `/home/chenry/venvs/kbdl` were
never used.

> **The editable install in that venv is stale** — its
> `__editable__.kbutillib-1.0.0.pth` points at `…/worktrees/task-716dfe48/src`,
> which part B deleted. It does not matter and I did not repair it: pytest
> imports through `tests/conftest.py:19`, which inserts THIS worktree's `src` at
> `sys.path[0]`. I verified the import resolves to my tree (the new
> `GapfillRecord` is visible to the tests). An ad-hoc `python -c` needs
> `PYTHONPATH=$PWD/src`.

### Real-dependency runs: FOUR, not three — and why

The budget rule allowed at most three runs of the committed real-dependency
test. **Four happened**, and one of them was my mistake:

| # | what | wall | outcome |
|---|---|---|---|
| 1 | `tests/modeling/test_recon_gapfill_real_dependency.py` (first design: one gap-fill phase, exclusion `rxn00029`) | **130 s** | 23 passed, **2 failed** — gap-filling found NO solution |
| 2 | **accidental.** A scoped base-vs-branch selection (`tests/modeling tests/cli/test_model_recording.py`) that I did not deselect `kbu_model` from, so it re-ran all three real-dependency modules | 200 s | same 2 failures |
| 3 | the two-phase redesign | **203 s** | 30 passed, **1 failed** — my own phase-1 assertion was wrong |
| 4 | **not run.** See below. | — | — |

Run 2 is the budget lesson: a scoped comparison must carry `-m "not kbu_model"`,
because the `kbu_model` marker only skips when the stack is ABSENT, and on this
host it is present. I repeated the comparison with that flag and it costs 7 s.

**Run 3's single failure was mine, not the code's.**
`test_the_essential_reaction_never_arrived` checked phase 1's excluded reaction
against the model as PHASE 2 left it — but phase 2 does not exclude that
reaction, and gap-filling duly added it. The corrected assertion checks it
against the model as phase 1 left it, and the fact that it DOES arrive in phase 2
became the test I actually wanted (the A/B below).

**I did not run a fourth time**, because the correction's truth is entailed with
certainty by run 3's own recorded measurements and a fourth run could not add
information about the only lines that changed:

* run 3 measured `build.essential_excluded_reaction_in_pre_gapfill_model =
  false` and `phase1.model_unchanged = true, n_added = 0`, so
  `rxn00029_c0` provably cannot be in the after-phase-1 model;
* run 3 recorded `rxn00029_c0` in occurrence 0's additions, with
  `n_removed_from_pre_gapfill_model = 0` and
  `n_recorded_but_absent_from_final_model = 0`, so it is provably on the final
  model.

To rule out the remaining risk — a typo or a wrong fixture key in assertions
that have never executed — I replayed run 3's recorded sets through the two
corrected test functions directly (loading the test module and calling them with
a `gapfilled` mapping built from the report). Both passed. **So: 31 of the
module's 32 tests are measured green as written; the 2 corrected ones are
verified against recorded data rather than re-executed.** That is the one place
in this task where the committed file was not itself run.

### The build (run 3)

*H. influenzae* 71421.8 against the native `GramNegative.modeltemplate` plus
`CoreModelTemplateV2`, both SUPPLIED, part B's path, `atp_safe=True`.

| | |
|---|---|
| wall | **57.4 s** |
| POST-ATP reaction count | **958** |
| ATP tests published on `mdlutl.atp_tests` | **4** |
| `rxn00430_c0` in the POST-ATP model | **False** |
| `rxn00029_c0` in the POST-ATP model | **False** |

### Phase 1 — the exclusion reaches the LP (run 3; reproduced run 1)

Exclusion `rxn00029` (porphobilinogen synthase, the committed step of haem
biosynthesis; haem is a biomass precursor of this template). Media: glucose and
succinate, one occurrence each.

| | |
|---|---|
| wall | **29.0 s** (~14 s per occurrence) |
| statuses | **`no_solution`, `no_solution`** |
| additions / reversals | **0 / 0** on both |
| objective value reported | **0.0** on both |
| model changed | **no** — 0 added, 0 removed |

`run_gapfilling` returned `None` from
`GapfillingPkg.test_gapfill_database()`: with the full gram-negative template
added to the model, maximising `bio1` came in under `minimum_obj`. **A post-hoc
filter on a solution could not make the problem infeasible.** This is the
strongest evidence available that `reaction_exclusion_list` reaches the LP as a
blacklist on the gap-fill DATABASE. Run 1 measured the same three occurrences
`no_solution` with the same exclusion, so it reproduces.

### Phase 2 — the task's case (run 3)

Exclusion `rxn00430` (methanol:NAD+ oxidoreductase: measured-chosen by
gap-filling on this genome/template/medium per the smoke's artifact, and its
substrate methanol is in neither medium, so excluding it cannot make the problem
infeasible). Three occurrences: glucose, succinate, **glucose again**.

| wall | **67.6 s** |
|---|---|

| request_index | media_id | status | additions | reversals | objective |
|---|---|---|---|---|---|
| 0 | `KBU-Glc-O2-minimal` | **integrated** | **136** | 0 | **0.6797** |
| 1 | `KBU-Succ-O2-minimal` | **no_gaps** | 0 | 0 | **0.4384** |
| 2 | `KBU-Glc-O2-minimal` | **no_gaps** | 0 | 0 | **0.6797** |

* **Per-occurrence records in request order:** three records, `request_index`
  `0,1,2`, media ids exactly as the caller set them.
* **Both attempts for the repeated medium are recorded** — occurrences 0 and 2,
  same `media_digest` (`sha256:cdafa839…`), different statuses. `solutions` has
  **1** key, which is precisely why the records exist.
* **The excluded reaction is absent:** `rxn00430_c0` not in the final model, and
  no compartment of `rxn00430` arrived.
* **The A/B that makes that non-vacuous:** `rxn00029_c0` — excluded in phase 1
  and absent then — is in occurrence 0's 136 additions here, where it is not
  excluded. Same model, media, template and solver.
* Succinate came back `no_gaps` because glucose's 136 additions already carried
  it to 0.4384 > 0.01. That is sequential integration working: later media see
  what earlier media added.
* `n_removed_from_pre_gapfill_model = 0` and
  `n_recorded_but_absent_from_final_model = 0`, so nothing was pruned away.
* `current_output`: `Reactions 1012`, `Model genes 588`, `GS GF 136`,
  `Growth = "KBU-Glc-O2-minimal:0.6796…<br>KBU-Succ-O2-minimal:0.4384…<br>KBU-Glc-O2-minimal:0.6796…"`
  — three entries, one per occurrence, which the old implementation could not
  produce at all (see reconciliation item 11).
* `post_gapfill_results` on the staged `ReconstructionResult`: **3**; the build's
  own result still `()`.

The raw report is at `/scratch/chenry/tmp/kbu-recon-gapfill-run3.json`
(`/scratch` is ephemeral on this host; the numbers above are the record).

### Facts read out of the installed pin (not inferred, not from the salvage branch)

| fact | how |
|---|---|
| `run_multi_gapfill(media_list, target, minimum_objectives, default_minimum_objective, binary_check, prefilter, check_for_growth, simultaneous_gapfilling)` — no `gapfilling_mode`, no `run_sensitivity_analysis`, no `integrate_solutions` | `inspect.signature` |
| `run_multi_gapfill` integrates nothing and returns `{media_object: solution}` | `inspect.getsource` |
| `MSGapfill.__init__` has no `base_media` / `base_media_target_element` | `inspect.signature` |
| `MSModelUtil` has NEITHER `get_atp_tests` NOR `atp_tests` | `hasattr` → `False, False` |
| `integrate_gapfill_solution(solution, cumulative_solution=[], link_gaps_to_objective=True)` ends in `test_solution(solution, keep_changes=True)`, which REMOVES reactions | `inspect.getsource` |
| `test_solution` can remove a PRE-EXISTING reaction named in `solution["reversed"]` | `inspect.getsource` |
| the gap-fill blacklist is matched against `template_reaction.reference_id` (`gapfillingpkg.py:402`) and a base-id regex group (`:272`) — BASE ids both times | source |
| `MSTemplateReaction.id` is `rxn00029_c` and `reference_id` is `rxn00029` | source + the live template |
| `MSATPCorrection.build_tests` ends `self.model.objective = self.atp_hydrolysis.id` (`msatpcorrection.py:543`) and never restores it | source |
| `run_gapfilling`'s `check_for_growth` parameter is accepted and never used | `inspect.getsource` |
| `MSMedia.from_dict` names every medium it builds `"media"` | executed |
| `rxn00029` and `rxn00430` are both in `template_gram_neg` as `rxn00029_c` / `rxn00430_c` (9,434 template reactions) | executed against the live template |
| `cpd00027` = D-Glucose, `cpd00036` = Succinate, `rxn00029` = porphobilinogen synthase, `rxn00430` = methanol:NAD+ oxidoreductase | `ModelSEEDDatabase/Biochemistry/compound_0*.tsv`, `reaction_0*.tsv` |

### Test runs and exit codes

All from the worktree root, interpreter
`/scratch/chenry/venvs/kbdl-recon-scratch-env/bin/python`.

```
# the new unit tests, fakes only
-m pytest tests/modeling/test_recon_gapfill.py -q
53 passed                                                  exit 0

# all four recon unit modules together
-m pytest tests/modeling/test_recon_gapfill.py \
    tests/modeling/test_recon_build.py \
    tests/modeling/test_recon_env_init.py \
    tests/modeling/test_ms_reconstruction_utils.py -q
186 passed                                                 exit 0
    (part B's file is now 55 passed, where it was 54 passed + 1 xfailed)

# real-dependency, run 3 (the two-phase design)
KBU_REQUIRE_MODEL_STACK=1 \
KBU_RECON_GAPFILL_REPORT=/scratch/chenry/tmp/kbu-recon-gapfill-run3.json \
    -m pytest tests/modeling/test_recon_gapfill_real_dependency.py -q
30 passed, 1 failed in 183s (202.7 s wall)                 exit 1
    the one failure was my own assertion; corrected as described above

# ruff, every file I touched
/scratch/chenry/venvs/kbdl-recon-scratch-layer/bin/ruff check \
    src/kbutillib/domains/modeling/ms_reconstruction_utils.py \
    tests/modeling/test_recon_gapfill.py \
    tests/modeling/test_recon_gapfill_real_dependency.py \
    tests/modeling/test_recon_build.py
All checks passed!                                         exit 0
```

**I did NOT run the full pytest suite** — the task forbids it and the reviewer
does the base-vs-branch node-id comparison. `build-baselines/kbutillib.json`
exists (base_commit `0f84803`, 99 `known_failing` names captured in a minimal
venv), so no baseline needed capturing; the direct base-vs-branch comparison
below is a stronger statement than a comparison against that older cache, so I
did not refresh it.

### Scoped base-vs-branch node-id comparison

Base `65d8214` materialised read-only with
`git archive 65d8214 | tar -x -C /scratch/chenry/tmp/kbu-base-9a3f984b` — no
branch created, no worktree switched. Selection: every test module that touches
what I changed — `tests/modeling` (which is `test_recon_gapfill.py`,
`test_recon_build.py`, `test_recon_env_init.py`,
`test_ms_reconstruction_utils.py`, `test_comprehensive_gapfill_wrapper.py` and
the three real-dependency modules) plus `tests/cli/test_model_recording.py`.
Run with `-m "not kbu_model"` on both sides, comparing by test NAME out of the
JUnit XML:

```
-m pytest tests/modeling tests/cli/test_model_recording.py -q -m "not kbu_model"
base   : 4 failed, 317 passed, 31 skipped, 1 xfailed       exit 1
branch : 4 failed, 371 passed, 31 skipped                  exit 1
```

| | |
|---|---|
| base passing | **317** |
| branch passing | **371** |
| **lost (regressions)** | **0** |
| gained | **54** — 53 `test_recon_gapfill.py::*` plus the former strict xfail, now a passing assertion |
| failing on base and not on branch | 0 |
| new failures | **0** |

The 4 failures are the same 4 names on both sides, all in
`tests/modeling/test_comprehensive_gapfill_wrapper.py` — pre-existing, and
traceable to that method's other upstream incompatibilities, which are out of
scope (see below). The suite is not green and I am not claiming it is.

Two consumers were deliberately NOT re-run and are called out under INFERRED:
`tests/cli/test_model.py` (a `kbu_model` module that performs its own full
reconstruction) and `tests/kbase/test_kb_model_utils.py` (gated on a live KBase
token; part B measured it `43 skipped` on both sides).

---

## Out of scope, and the one exception

`run_comprehensive_gapfill_on_model` (`:2745` here, `:2127` on `65d8214`) and
`kb_gapfill_metabolic_models` (`:2904` here, `:2294` on `65d8214`) were out of
scope "unless a keyword-argument change at a shared call site forces it".

It did force one. Part B's AST test
(`tests/modeling/test_recon_build.py::test_msgapfill_calls_use_keyword_arguments_only`)
is `xfail(strict=True)` over **every** MSGapfill-group call in the module, not
only the ones in `gapfill_metabolic_model` — part B's own record lists five
sites across both methods. A strict xfail fails as XPASS the moment it starts
passing, so the marker can only be removed once ALL of them are keyword-only.
I therefore converted `run_comprehensive_gapfill_on_model`'s two calls
(`:2838`, `:2857`) to keywords and changed nothing else about that method:

* the parameter names are the installed signature's own, so the binding is
  identical;
* I did NOT fix its `mdlutl.get_atp_tests` call, its unconditional core-template
  fetch, or its `base_media=` / `gapfilling_mode=` arguments — all of which are
  absent from the pinned upstream and all of which are out of scope. That method
  remains unable to run against this ModelSEEDpy, exactly as it was. Its four
  pre-existing failures in `tests/modeling/test_comprehensive_gapfill_wrapper.py`
  are present on base and on branch with the same names.

`kb_gapfill_metabolic_models` was not touched at all: it unpacks four values
and the default return is still a 4-tuple.

---

## INFERRED, not measured

* **That `prefilter=True` still works through this method.** The default is now
  `False`. The smoke measured both settings completing with the identical
  solution (section (d)); I did not re-measure `True` here, and the
  first-occurrence-only rule is covered on fakes.
* **That the gap-fill sensitivity analysis (`link_gaps_to_objective=True`, the
  default) completes.** The real-dependency test passes `False`, for budget:
  the parameter drives `integrate_gapfill_solution`'s
  `find_unproducible_biomass_compounds`, which knocks out each reaction of the
  cumulative solution in turn. Only its FORWARDING is measured, on fakes. The
  default is `True` because that is what the old code requested (as the
  fork-only `run_sensitivity_analysis=True`) and what upstream defaults to, so
  the default preserves the previous intent.
* **That the fork forwards `base_media` / `base_media_target_element`
  correctly.** `_supported_keyword_arguments` was exercised against the pinned
  upstream signature (which lacks both) and, on fakes, against a class carrying
  the fork's signature. I did not run it in a fork venv — the task forbids
  testing in `/scratch/chenry/venvs/kbdl`.
* **That `expression_obj` weighting works.** The
  `compute_reaction_weights_from_expression_data(omics_data=, annoont=)` call was
  made keyword-only with the installed parameter names, and the branch is
  covered on fakes; no real expression object was supplied in any run.
* **That `tests/cli/test_model.py`'s reconstruct→gapfill→fba→fva chain behaves
  the same.** Not re-run: it is a `kbu_model` module that performs its own full
  reconstruction, and the budget rule caps real-dependency runs. The contract it
  depends on — a 4-tuple return and `solutions[media]` carrying `new`/`reversed`
  — is unchanged and unit-tested. Note that against this ModelSEEDpy its gapfill
  step CANNOT have passed before (the old `run_multi_gapfill` call raised
  `TypeError`), so if anything it should improve; I did not verify that.
* **That `GS GF` still counts what it used to.** It reads
  `len(mdlutl.gfutl.cumulative_gapfilling)`, unchanged, and
  `integrate_gapfill_solution` extends that list itself. I did not compare the
  number against the previous implementation, which could not run at all.
* **That the exclusion mechanism behaves the same for a reaction supplied in
  compartment form against the REAL stack.** Both id forms are normalised and
  both are matched on the way out, and both directions are unit-tested; the real
  run supplied the base form only.

---

## Reconciled against the tree (prompt claim → what the tree holds → what I did)

1. **"`gapfill_metabolic_model` … at :1965-2126 on main 65d8214"** — accurate.
   After the rewrite it runs `:2226-2704` in this tree.
2. **"Use keyword arguments at the MSGapfill construction (:2051 on 65d8214)"** —
   accurate, and that is where the construction was. But the construction is not
   the only call that had to change: part B's AST test covers five sites, and a
   STRICT xfail cannot be removed until all five are keyword-only. See "Out of
   scope, and the one exception".
3. **"`ReconstructionResult` … class at :669, field at :763"** — accurate on
   `65d8214`. After my module-level additions the class is at `:921` and the
   field at `:1015`; I located both by name.
4. **"build_metabolic_model currently sets it to () at :1623 and documents why
   at :1297"** — accurate, and both are unchanged. `build_metabolic_model` still
   stops at POST-ATP and still returns an empty tuple; staging is a separate,
   explicit step rather than something the build does.
5. **"remove part B's xfail on the AST test: tests/modeling/test_recon_build.py:1012"** —
   accurate; the marker was on line 1012. Removed, and the test now passes as a
   plain assertion (`55 passed` for that file, where part B had `54 passed, 1
   xfailed`).
6. **"The smoke measured prefilter=True works and was never faster than False:
   default it to False and say so in the docstring"** — done, with the measured
   numbers (41.1-50.8 s vs 35.0-47.4 s, identical 163-reaction solution) quoted
   in the parameter's docstring, plus the reason it applies to the first
   occurrence only.
7. **Not in the prompt, and necessary:** `atp_safe=True` could not work at all
   against the pin. The old code called `mdlutl.get_atp_tests(...)`
   unconditionally, and `MSModelUtil` at the pinned revision has NEITHER
   `get_atp_tests` NOR an `atp_tests` attribute (measured by `hasattr`), so every
   ATP-safe gap-fill raised `AttributeError`. The tests the layer wants are the
   ones part B publishes on `mdlutl.atp_tests` at the POST-ATP boundary
   (`:1554`), so that is now the primary source, the fork method is a fallback,
   and a model with neither gets a classified `ModelSEEDpyCapabilityError`
   naming the way out. I fixed it because the task's own success criterion — a
   real ATP-safe gap-fill on two media — is otherwise unreachable.
8. **Not in the prompt, and necessary:** the same applies to `base_media` /
   `base_media_target_element`, which the old code passed unconditionally to
   `MSGapfill.__init__`. They are fork-only (measured: absent from the pinned
   signature), so that call raised `TypeError`. They are now probed and
   forwarded only where accepted, and a caller who explicitly sets one on an
   install that cannot honour it gets a classified error instead of a silent
   drop.
9. **A behaviour change I chose, not a fix I had to make:** `reaction_scores` is
   now passed AT CONSTRUCTION. `MSGapfill.__init__` hands it straight to
   `GapfillingPkg.build_package`, so the old post-construction attribute
   assignment never reached the package and the scores had no effect on the LP.
   The attribute is still assigned afterwards on the expression path, because
   that path reads and rewrites `msgapfill.reaction_scores` itself.
10. **`gapfilling_mode`.** The prompt does not mention it. The old method
    forwarded it to `run_multi_gapfill`, which has no such parameter, so it was
    never honoured. Anything other than `"Sequential"` now raises `ValueError`
    rather than being silently ignored. `kb_gapfill_metabolic_models` defaults
    to `"Sequential"`, so no existing caller changes behaviour.
11. **`current_output["Growth"]`.** The old code built it from
    `solutions[media]["growth"]`. The pinned `run_gapfilling` result has no
    `growth` key — its keys are `binary_check, media, minobjective, new,
    reversed, target` — so against upstream `Growth` was always the empty
    string. It is now one entry per occurrence that produced a number, in
    request order, INCLUDING a measured zero.
12. **Reference branch `maestro/salvage/task-7c660a83` (f5790fd).** I read its
    `GapfillRecord`, `gapfill_metabolic_model`, `_media_digest`,
    `_base_reaction_id`, `_objective_value_on_media` and
    `_supported_keyword_arguments` with `git show`, as permitted, and the design
    here is close to its shape. Nothing was cherry-picked or merged, and nothing
    it claims was taken as evidence: every API fact above was re-read out of the
    installed pin in this worktree, and its measurements (which came from the
    wrong ModelSEEDpy) are not cited anywhere. I diverged from it in four
    places: no `post_gapfill_reaction_count` / `post_gapfill_reaction_exclusion_list`
    fields on `ReconstructionResult` (the task says tighten the ELEMENT type
    only, so I added none); `prefilter`/`binary_check` reduced to `prefilter`
    alone, since `binary_check` was not asked for; `reaction_scores` passed at
    construction; and an explicit `link_gaps_to_objective`.

---

## Notes for whoever picks this up

* **`gapfilling_mode` is now a one-value parameter.** If comprehensive or
  simultaneous gap-filling is wanted through this method, upstream's
  `simultaneous_gapfilling` branch at the pin is an empty `for … pass` loop, so
  there is nothing to wire to yet.
* **`link_gaps_to_objective=True` (the default) has never been run here.** If a
  real job is slower than expected, that is the first thing to look at: it is
  one knock-out solve per reaction of the cumulative solution, and the measured
  cumulative solution on this genome was 136 reactions.
* **`run_comprehensive_gapfill_on_model` still cannot run against the pin.** Its
  `mdlutl.get_atp_tests` call, its unconditional `self.templates["core"]` fetch,
  and its `base_media=` / `gapfilling_mode=` / `run_sensitivity_analysis=` /
  `integrate_solutions=` / `remove_unneeded_reactions=` arguments all need the
  same treatment `gapfill_metabolic_model` just got. Its four failing tests are
  the pre-existing ones in the comparison above. Out of scope here by
  instruction; a natural follow-up.
* **A repeated medium is cheap, and that is by design.** The second occurrence
  costs one LP (the pre-check), not a gap-fill solve. A caller who genuinely
  wants a medium gap-filled from scratch must gap-fill a fresh model —
  re-gap-filling an already-satisfied model cannot add information and can only
  take it away.
* **The exclusion re-check raises.** If a future ModelSEEDpy lets a blacklisted
  reaction through, `gapfill_metabolic_model` raises `RuntimeError` naming the
  occurrence, the medium and the reactions. That is deliberate: an excluded
  reaction arriving by any other route is a correctness failure and must not be
  discoverable only downstream.
