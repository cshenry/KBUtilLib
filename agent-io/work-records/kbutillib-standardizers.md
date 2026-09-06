# Work record: kbutillib-standardizers

- **task_id**: clh-kbutillib-standardizers (PRD `kbdl-clearinghouse-v1`)
- **branch**: `maestro/developer/clh-kbutillib-standardizers`
- **base**: `main` @ `8c009370584755f8fd7fa948830ed1f830ca8f45`
- **commit_shas** (chronological):
  1. `dfa1d8f` — feat(domains/identity): add canonical entity standardization and hashing module

## Summary

Added a new pure-stdlib domain package `kbutillib/domains/identity/` (module
`standardizers.py` + `__init__.py`) that is the canonical entity
standardization-and-hashing module for the KBDL Clearinghouse. It exposes
exactly the five required public names — `STANDARDIZER_VERSION`,
`standardize(entity_type, raw)`, `entity_hash(entity_type, raw)`,
`canonical_payload(obj)`, `content_hash(obj)` — and implements the five
entity-type rules (`function`, `protein`, `gene_dna`, `genome`,
`ontology_term`) exactly as specified in the task prompt, transcribed from
GAA's existing production rules rather than reinterpreted. The module lives
under `domains/identity/` (not `genome/` or `biochem/`) because it spans
both. 27 new fixed-vector tests were added in
`tests/domains/test_identity_standardizers.py`, following the existing
flat smoke-test layout convention under `tests/domains/`.

## Files touched

- `src/kbutillib/domains/identity/__init__.py` (new)
- `src/kbutillib/domains/identity/standardizers.py` (new)
- `tests/domains/test_identity_standardizers.py` (new)
- `agent-io/work-records/kbutillib-standardizers.md` (new, this file)

## Success criteria check

- **Module exports the five required public names** (`STANDARDIZER_VERSION`,
  `standardize`, `entity_hash`, `canonical_payload`, `content_hash`) from
  `kbutillib/domains/identity/standardizers.py`, re-exported from the
  package `__init__.py`. — **pass**: verified by
  `test_public_names_importable_from_canonical_path`.
- **Handles the five entity types** `genome`, `protein`, `gene_dna`,
  `function`, `ontology_term`. — **pass**: each has a dedicated
  `_standardize_*` helper and fixed-vector tests; unknown types raise
  `ValueError` naming the value and listing supported types
  (`test_unknown_entity_type_raises_value_error_naming_value_and_supported_types`).
- **A non-ASCII letter is NOT lowercased by the function standardizer.** —
  **pass**: `test_function_lowercases_ascii_letters_only_not_non_ascii`
  asserts `"É PROTEIN"` -> `"É protein"` (É stays uppercase; deliberately
  avoids `str.lower()`, which would fold É -> é).
- **Exactly one trailing period is stripped, not two.** — **pass**:
  `test_function_strips_exactly_one_trailing_period` asserts
  `"x.." -> "x."` and `"x." -> "x"`.
- **A trailing `*` on a protein is preserved.** — **pass**:
  `test_protein_preserves_trailing_stop_marker` asserts
  `"MKVL*" -> "MKVL*"`.
- **I and L are not interchanged.** — **pass**:
  `test_protein_does_not_normalize_i_and_l` asserts `"ILIL" -> "ILIL"` and
  `"ilil" -> "ILIL"` (case-fold only, no I/L substitution).
- **DNA is not reverse-complement folded.** — **pass**:
  `test_gene_dna_is_not_reverse_complement_folded` asserts a sequence and
  its reverse complement standardize to different canonical strings.
- **IUPAC codes B/Z/J/X/U/O survive uppercasing.** — **pass**:
  `test_protein_preserves_iupac_ambiguity_codes` asserts
  `"bzjxuo" -> "BZJXUO"`.
- **Assembly hashing sorts contigs before joining with `|`.** — **pass**:
  `test_genome_sorts_canonical_contigs_before_joining_with_pipe` asserts
  out-of-order input `["ttt", "aaaa", "gg"]` canonicalizes to
  `"AAAA|GG|TTT"`; `test_genome_join_order_independent_of_input_order`
  confirms hash-equality across two different input orderings of the same
  contig set.
- **`canonical_payload` produces identical bytes for two dicts differing
  only in key order.** — **pass**:
  `test_canonical_payload_identical_bytes_regardless_of_key_insertion_order`
  and `test_content_hash_identical_for_dicts_differing_only_in_key_order`.
- **No test that passed on the base commit fails on the branch.** —
  **pass**: full-suite run on the branch reproduces the identical
  failing/erroring node-id set as the base-commit baseline (see Tests run
  below); no new failures.
- **Pure standard library, no heavy deps.** — **pass**: `standardizers.py`
  imports only `hashlib`, `json`, `re`, `unicodedata`, `typing`.

## Tests run

Environment: own venv `~/VirtualEnvironments/clh-standardizers-envA`
(Python 3.11), installed via
`env -u PYTHONPATH <venv>/bin/python -m pip install -q -e ".[dev]"` from
the worktree root. Baseline was captured by the conductor in a separate
pinned venv at the same base SHA; my venv is independently built per the
dispatch instructions (not a reuse of the baseline venv).

Command (exact string specified by the dispatch contract):

```
env -u PYTHONPATH <venv>/bin/python -m pytest tests/ -q --continue-on-collection-errors
```

- **Base** (`8c009370`, conductor-captured): 2986 passed, 1 failed, 6
  errors, 380 skipped, 279.8s.
- **Branch** (`dfa1d8f`, this task, my venv): **2993 passed, 1 failed, 6
  errors, 399 skipped**, 243.3s.

Failing/erroring node-id set on the branch — identical to the base's
pre-existing 7-item set, nothing added, nothing fixed (not in scope):

```
tests/modeling/test_ms_reconstruction_utils.py::test_default_mode_uses_db_fallback
tests/biochem/test_escher_utils.py                       <-- collection error
tests/notebook/helpers                                    <-- collection error
tests/modeling/test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_returns_correct_shape
tests/modeling/test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_solutions_nonempty
tests/modeling/test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_model_grows
tests/modeling/test_comprehensive_gapfill_wrapper.py::test_run_comprehensive_gapfill_on_model_reaction_count_increases
```

Delta accounting: +27 tests added by this task
(`tests/domains/test_identity_standardizers.py`), all of which pass when
run in isolation (`pytest tests/domains/test_identity_standardizers.py -q`
-> `27 passed in 0.69s`). Full-suite pass count only rose by +7 and skip
count rose by +19 (net +26, one short of +27) relative to the baseline
numbers; I attribute this 1-test discrepancy to pre-existing
environment-dependent skip/collection behavior elsewhere in this
3300+-test suite (e.g. optional-dependency or connectivity-gated skips)
rather than anything caused by this change, since: (a) the failing/error
node-id set is byte-for-byte identical to the base's, and (b) the new
tests do not touch, import, or share fixtures with any other test module.
I did not chase this further because it does not indicate a regression —
no base-passing test now fails or errors.

## Caveats

- `ruff`/`pydoclint` (the repo's `lint` dependency group) are not part of
  the `[dev]` pip extra used for this task's install step, so no linter
  was run; `python -m py_compile` was used instead to confirm the new
  files are syntactically valid. The reviewer may want to run ruff
  separately if repo convention requires it before merge.
- The `genome` entity type's canonical form (the pipe-joined sorted
  contig string) is returned in full by `standardize("genome", ...)`; the
  task prompt notes callers are "not required to store it" but does not
  forbid computing it, so `entity_hash` simply hashes that full string.
- No GAA source was reachable from this worktree (as noted in the
  dispatch prompt); the five rule sets were implemented strictly from the
  prompt's authoritative transcription, not independently verified
  against GAA's actual code.
