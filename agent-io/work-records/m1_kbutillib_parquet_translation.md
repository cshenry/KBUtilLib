# Work record: m1_kbutillib_parquet_translation

## task_id
m1_kbutillib_parquet_translation (Maestro developer task, PRD `gaa-mapping-model-loop`,
Module M1)

## branch
gaa-mml/m1-kbutillib-parquet-translation

## commit_shas
- a541ebeddb21ec6220f2f13061c91cfff3c42e9b — `feat(annotation): add parquet-backed reaction-mapping translation to KBAnnotationUtils`

## summary

Implements M1 of `~/Dropbox/Projects/GenomeAnnotationAggregator/agent-io/prds/gaa-mapping-model-loop/fullprompt.md`
in `KBUtilLib/src/kbutillib/kb_annotation_utils.py`, class `KBAnnotationUtils`. Added
two new methods and two new instance attributes (`self.reaction_mapping`,
`self.reaction_mapping_by_description`, both initialized to `{}` in `__init__`):

- `load_reaction_mapping_parquet(path)` — reads a mapping-version parquet (one row per
  `(function_hash, function_description, reaction_id, score, is_in_template,
  is_in_core)`), and builds `self.reaction_mapping = {function_hash: [(reaction_id,
  score, is_in_template, is_in_core), ...]}` plus a parallel
  `self.reaction_mapping_by_description` keyed by `function_description` normalized
  through the existing `convert_role_to_searchrole`. Each call fully replaces both
  dicts (built fresh from the just-loaded rows), so repeated/differing loads never
  accumulate stale entries — this is what makes reload idempotent.
- `translate_function_to_reactions(function_hash=None, description=None,
  with_scores=True)` — looks up by `function_hash` first if given, else by
  `description` (normalized the same way as the index), and returns `[]` if nothing
  has been loaded yet or the key isn't found. Applies the existing
  `self.msrxn_filter` / `self.filtered_rxn` (`FilteredReactions.csv`) exclusion set
  exactly like `translate_term_to_modelseed` does. Returns `(reaction_id, score)`
  tuples when `with_scores=True` (default), bare `reaction_id` strings otherwise.

This is a strictly additive sibling path: `translate_term_to_modelseed`,
`get_alias_hash`, and `translate_rast_function_to_sso` were not touched (verified via
`git diff main -- src/kbutillib/kb_annotation_utils.py`, which shows only the two new
`__init__` lines and the two new methods — no other lines changed). The
mapping-version parquet is expected to carry no EC rows; EC-derived reactions
continue to resolve solely through the unchanged `translate_term_to_modelseed("EC:...")`
path over the bundled `EC_translation.tsv`, which this task did not modify.

New tests live in `tests/test_kb_annotation_utils_parquet.py` (12 tests), using a
tiny fixture mapping parquet written to `tmp_path` via `pandas.DataFrame.to_parquet`
(pyarrow, already a project dependency). They reuse the same
`KBAnnotationUtils(...)` construction + `FileNotFoundError`-skip pattern already
established in `tests/test_composition_smoke.py::TestKBAnnotationUtils`.

## files_touched
- `src/kbutillib/kb_annotation_utils.py` — added `load_reaction_mapping_parquet`,
  `translate_function_to_reactions`, and two `__init__` state attributes.
- `tests/test_kb_annotation_utils_parquet.py` (new) — 12 tests covering load,
  function_hash lookup, description lookup, msrxn_filter on/off, missing-key
  behavior (both before-any-load and after-load-but-key-absent), and reload
  idempotency (same-file reload and different-file reload replacing prior content).
- `agent-io/work-records/m1_kbutillib_parquet_translation.md` (this file).

## success_criteria_check

Restating the task's SUCCESS CRITERIA:

- "`KBAnnotationUtils.load_reaction_mapping_parquet` + `translate_function_to_reactions`
  exist and pass new KBUtilLib pytest tests" — **pass**. Both methods exist; all 12
  new tests pass (see Tests Run below).
- "reactions+scores resolve by function_hash and by normalized description" — **pass**.
  `test_lookup_by_function_hash_with_scores` and
  `test_lookup_by_description_resolves_via_normalized_index` (description passed with
  different case/whitespace than stored) both assert the expected `(reaction_id,
  score)` tuples.
- "FilteredReactions.csv entries are excluded under msrxn_filter" — **pass**.
  `test_msrxn_filter_excludes_filtered_reactions_csv_entries` uses a real filtered
  reaction id (`rxn00008`, present in the live `FilteredReactions.csv` with reason
  `CI`) and confirms it's excluded when `msrxn_filter` is `True` (the default) and
  included when set `False` (`test_msrxn_filter_off_includes_filtered_reactions`).
- "missing keys return empty" — **pass**. Covered both pre-load
  (`test_no_mapping_loaded_returns_empty`) and post-load-but-absent-key
  (`test_missing_function_hash_returns_empty`, `test_missing_description_returns_empty`).
- "reload is idempotent" — **pass**. `test_reloading_same_parquet_yields_same_result`
  (same file loaded twice gives identical output) and
  `test_reloading_new_parquet_replaces_not_accumulates` (loading a second, disjoint
  parquet drops the first mapping's keys entirely rather than merging).
- "translate_term_to_modelseed/get_alias_hash/translate_rast_function_to_sso are
  byte-for-byte unchanged" — **pass**. Confirmed via `git diff main --
  src/kbutillib/kb_annotation_utils.py`: the only changes are 2 new `__init__` lines
  and 2 new methods inserted between `translate_term_to_modelseed` and
  `get_annotation_ontology_events`; no existing line was modified. Also
  `test_translate_term_to_modelseed_still_works` sanity-checks the unchanged path
  still runs, and the pre-existing
  `tests/test_composition_smoke.py::TestKBAnnotationUtils::test_translate_term_to_modelseed_known_term`
  passes unmodified.

## tests_run

- `python -m pytest tests/test_kb_annotation_utils_parquet.py -v` → **12 passed**.
- `python -m pytest tests/test_composition_smoke.py -k KBAnnotationUtils -v` →
  **1 passed** (pre-existing test, confirms the unchanged EC/bundled path).
- `python -m pytest tests/test_composition_smoke.py -v` (full file, for regression
  sanity) → 10 passed, 8 skipped, 1 xfailed, 2 failed, 3 errors. The 2 failures + 3
  errors are all in `TestThermoUtils` / `TestMSBiochemUtils` /
  `TestKBUtilLibFacade::test_facade_biochem_lazy_singleton` and are caused by a
  **pre-existing environment issue unrelated to this change**: this Maestro worktree
  (`~/.maestro/worktrees/gaa-mml-m1-kbutillib`) isn't a sibling directory of the
  `ModelSEEDDatabase` checkout the way the normal `~/Dropbox/Projects/KBUtilLib`
  checkout is, so `DependencyManager`'s sibling-repo path resolution can't find it
  from inside the worktree. Verified pre-existing by `git stash`-ing this task's
  changes and re-running the same file — identical 2 failed / 3 errors. Not
  introduced by this task.

Environment note for the reviewer: for the same reason, the
`cb_annotation_ontology_api` sibling-repo lookup that
`KBAnnotationUtils.__init__` depends on also fails to resolve from inside this
worktree by default (it isn't a sibling of the worktree directory the way it is a
sibling of `~/Dropbox/Projects/KBUtilLib`). To actually exercise the new tests
(rather than have them skip), I created a symlink
`~/.maestro/worktrees/cb_annotation_ontology_api ->
~/Dropbox/Projects/cb_annotation_ontology_api` — this lives outside the git
worktree/repo boundary (in the Maestro worktrees parent directory), is not tracked
by git, and was not committed. If the reviewer's worktree lands at a different path
and the same symlink isn't present, the new tests will report `SKIPPED` (not
failed) with reason "cb_annotation_ontology_api data files not available" — the
same graceful-skip behavior the pre-existing
`TestKBAnnotationUtils::test_translate_term_to_modelseed_known_term` already has.
Recreating the symlink (or otherwise pointing `~/.kbutillib/dependencies.yaml`'s
`cb_annotation_ontology_api` entry at the real data repo) makes all 12 new tests run
and pass.

## caveats

- `translate_function_to_reactions` checks `function_hash` first and only falls back
  to `description` when `function_hash is None` — it does not merge/union results if
  both are supplied simultaneously. This matches how the acceptance criteria and PRD
  describe the two lookup modes (tested independently), and is the more predictable
  behavior for callers; not explicitly specified further in the PRD, so flagging the
  interpretation here.
- Score is coerced to `float` and `is_in_template`/`is_in_core` to `bool` at load
  time (defensive against parquet dtype variance); `reaction_id` is passed through
  as-is from the parquet (the M2 schema-validation acceptance criteria — requiring
  `reaction_id` to match `^rxn\d+$` — belongs to a different module/task and was not
  implemented here, per the task's scope being M1 only).
- The worktree/sibling-repo dependency-resolution gap described above (both for
  `ModelSEEDDatabase` and `cb_annotation_ontology_api`) is a pre-existing
  environment characteristic of running tests inside an isolated Maestro worktree,
  not something this task introduced or is in scope to fix.
