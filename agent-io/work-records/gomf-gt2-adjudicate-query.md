# Work Record — gomf-gt2-adjudicate-query

## task_id
task-9be92300 (PRD go-mf-council-autoresearch-v1, task 2 of 9)

## branch
maestro/developer/gomf-gt2-adjudicate-query

## commit_shas
- 49cdad8 — `feat(council): go_mf_adjudicate@1, a council that judges go_mf conflicts`

## base_commit
7ea608a — `docs(council): work record for the openpyxl tuning-sheet smoke`

## precondition
MET. `src/kbutillib/domains/ai/llm/apis/schema_compat.py` is on `main` with
`strict_problems()` and `strip_meta_keywords()`, i.e. the
`council-seat-schemas-and-capture-v1` build has landed.

## summary
Added a fourth builtin council query type, `go_mf_adjudicate@1`, plus the
package `kbutillib.domains.ai.council.go_tuning` holding the written mapping
policy it judges against.

The query type does not map annotations; it judges two finished `go_mf`
mappings of ONE annotation string against six numbered policy rules (P1-P6).
Answers arrive already rendered, labelled A and B in random order with their
source hidden and WITHOUT either side's reasoning, so the judgement is about
the terms against the policy rather than about whose pipeline produced them.
All three roles are shown the same policy text, every role's user prompt
wraps the annotation, the menu and both answers in `<<<LABEL>>>` delimiters,
and every system prompt says delimited content is data whose embedded
instructions must be ignored (contract C20).

The adapter maps the adjudication vocabulary onto the engine's generic shape
and deliberately drops the research fields (`prefers`, `failure_type`,
`rules_cited`, `correct_term_off_menu`, `off_menu_description`); they are read
back from `calls.reply_parsed`, which the new engine tests assert on directly.

The commented ADJUDICATE block in `default_config.yaml` records h100's
five-seat roster and the `models:` entries an operator must add.

## files_touched

New:
- `src/kbutillib/domains/ai/council/go_tuning/__init__.py` — re-exports `MAPPING_POLICY`.
- `src/kbutillib/domains/ai/council/go_tuning/policy.py` — `MAPPING_POLICY`, the six rules verbatim.
- `src/kbutillib/domains/ai/council/queries/builtin/go_mf_adjudicate_v1.yaml` — the spec: three role prompts, three strict-clean schemas, the attribution block.
- `tests/domains/ai/council/test_go_mf_adjudicate_builtin.py` — 64 tests (spec, schemas, policy pin, rendering, awkward annotations, the config block, the seating).
- `tests/domains/ai/council/test_go_mf_adjudicate_council.py` — 11 tests (three items through the real engine on the fake backend, plus the adapter in isolation).

Modified:
- `renderers.py:142` — `go_candidate_block()` gained `with_score: bool = True`; the default preserves `go_mf@1/@3/@4` byte for byte.
- `renderers.py:237` — `NO_TERMS`; `renderers.py:240` — `go_mf_adjudicate()`, the new renderer.
- `adapters.py:83` — `CONTENTLESS_FAILURE_TYPE`; `adapters.py:96` — `go_mf_adjudicate()`, the new adapter.
- `queries/builtin/__init__.py` — `go_mf_adjudicate` added to `BUILTIN_QUERY_TYPES`, `BUILTIN_SPEC_FILES`, `BUILTIN_RENDERERS` and `BUILTIN_ADAPTERS`.
- `queries/registry.py:111` — `seed_builtins()` docstring now names five builtins.
- `default_config.yaml` — the commented ADJUDICATE block (89 lines, between `# >>> BEGIN ADJUDICATE BLOCK` and `# <<< END ADJUDICATE BLOCK <<<`).
- Four existing tests that ENUMERATE the seeded builtins, updated to include the new type: `test_queries.py` (seed list ×2, the `querytype.schema.json` file list, and the retire-count test now counts off `BUILTIN_QUERY_TYPES` instead of a literal), `test_council_utils.py`, `tests/cli/test_council_cli.py` (×2), and `test_default_config_alcf_block.py` (the byte-identity check now cuts BOTH commented blocks before hashing; `BASE_SHA256` is unchanged).

## verification

Commands and exit codes, all run as
`TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src /home/chenry/venvs/kbdl/bin/python -m pytest ... -q -p no:cacheprovider --no-header`:

| selection | before | after |
|---|---|---|
| `tests/domains/ai/council/ tests/cli/test_council_cli.py tests/domains/ai/llm/test_schema_compat.py` | 6 failed, 1617 passed, 7 skipped (exit 1) | 6 failed, 1692 passed, 7 skipped (exit 1) |
| the four `tests/berdl/test_council_*.py`, `tests/domains/test_council_bundle.py`, `tests/domains/ai` | not measured before | 7 failed, 2176 passed, 75 skipped (exit 1) |
| the two new modules alone | — | 75 passed (exit 0) |

The 6 failures before and after are the SAME six nodeids, all in
`test_types_config.py::TestPackagedDefaultConfig`, and they are the
machine-local failures recorded in
`build-baselines/kbutillib-h100-main-6310d61.json`: h100's
`~/.kbutillib/config.yaml` leaks past the tests' monkeypatched `HOME`. No
regression: compared by nodeid, not count.

The seventh failure in the wider selection,
`tests/domains/ai/llm/test_static_headers.py::TestPackagedExample::test_the_default_config_declares_no_mag_endpoint`,
is the SAME leak and is NOT caused by this change. Verified directly: with
`src/kbutillib/domains/ai/council/default_config.yaml` restored to the base
commit's bytes and nothing else reverted, the test still fails (1 failed, 2
passed). It calls `load_council_config()` with no argument, which reads
h100's user config; that file declares a live `mag` endpoint.

No new baseline file was written — `build-baselines/kbutillib.json` and
`build-baselines/kbutillib-h100-main-6310d61.json` both already exist, and the
full suite takes ~57 minutes on this machine, well past the tool ceiling.

## success_criteria_check

- **`go_mf_adjudicate@1` loads via `load_builtin_spec` and validates** — PASS.
  `TestTheSpecLoads::test_it_loads_through_validate_spec_with_the_declared_wiring`
  and `test_it_is_registered_in_all_four_builtin_tables`. `seed_builtins()`
  seeds it (`test_queries.py::TestRegistry::test_seed_builtins_is_idempotent`,
  `kbu council db init` via `tests/cli/test_council_cli.py`).
- **No `$schema`, every property in `required`, `additionalProperties: false`
  on all three role schemas** — PASS.
  `TestTheSchemasAreStrictClean::test_no_schema_key_every_property_required_no_extras`,
  parametrised over the three roles.
- **`strict_problems()` returns `[]` for each of the three roles** — PASS.
  `TestTheSchemasAreStrictClean::test_strict_problems_is_empty`. Every
  adjudicate role is therefore servable by a `codex_cli` seat, which `go_mf@4`'s
  meta role is not.
- **`default_config.yaml` carries the commented ADJUDICATE block with the h100
  roster and model entries** — PASS. `TestTheCommentedAdjudicateBlock` (five
  tests): entirely commented, the packaged config declares no `adjudicate`
  profile and none of the three machine-local aliases, every seat alias and
  served name is named, and the three required statements are present.
- **Adjudicator prompts delimit the annotation and the menu as data** — PASS.
  `TestRendering::test_the_annotation_and_menu_are_delimited_as_data` for all
  three roles, plus `TestAwkwardAnnotations` over quotes, single braces,
  doubled braces, a mixed case and an 11 kB string (12 renders).
- **The new fake-backend tests pass (unanimous, meta, off-menu drop, no source
  leakage)** — PASS. `TestOneItemToUnanimity` (unanimous, `meta_skipped`, no
  meta call made), `TestCriticDisapprovalGoesToMeta` (an `inconsistent` critic
  sends a UNANIMOUS panel to meta, and meta overrides it),
  `TestAnOffMenuIdIsDropped` (the engine drops `GO:0008643`, records one
  `off_menu` error naming the seat, and still settles the item), and
  `test_the_prompts_the_models_received_leak_no_source` (no `ALCF`, no `go_mf`,
  no producer attribution on either answer, in every prompt every seat
  received).
- **`calls.reply_parsed` carries the extra fields for each settled ok proposer
  call and for meta** — PASS. `assert_research_fields_readable()` is asserted
  in all three engine scenarios; `attempts.reply_text` is not read anywhere.
- **No test that passed on the base commit fails** — PASS, by nodeid. See
  the verification table.

## reconciliation against the tree

Four places where the prompt and the worktree disagreed, all fixed rather than
escalated:

1. **The prompt said the adjudicate menu is `rank | GO id | name | definition`
   and that the renderer uses `go_candidate_block`.** `go_candidate_block` had
   no such mode: `with_def=True` renders `rank | id | score | name | def` and
   `with_def=False` renders `rank | id | name`. Added `with_score: bool = True`
   (additive, default preserves the reference output) rather than labelling a
   block with a header that does not match it. The score is dropped for the
   adjudicators on purpose — it is evidence about retrieval, not about the
   policy, and it would nudge a judge toward a high-scoring generic term in
   exactly the P3/P4 case where the prompt asks for a menu gap to be reported.
   `TestGoMfIsUnmovedByTheSharedCandidateBlock` pins both reference renderings
   and all three `go_mf` versions.
2. **The prompt described the policy as going into `{policy}` "for every
   role", which `go_mf_v3`'s renderer cannot do.** `go_mf_v3` returns
   `role_spec.system` unformatted. `go_mf_adjudicate` formats the SYSTEM
   template too, since the policy belongs in the system prompt. One field map
   is used for both templates per role.
3. **The prompt's proposer schema has no `no_functional_content`, but the
   engine's generic proposer shape and its `content_flag()` rule need one.**
   Derived in the adapter from `failure_type == "contentless_assigned"` AND an
   empty `correct_go_ids` (`CONTENTLESS_FAILURE_TYPE`). Both clauses, because
   the flag short-circuits the whole council for an item and a self-
   contradictory answer must not trigger it. Documented at the constant and
   tested in `TestTheAdapterInIsolation`.
4. **The prompt's roster cannot be seated as given.** With the packaged
   spellings, `argo-opus5` (ARGO `claudeopus5`) and the `opus-cli` proposer
   (claude CLI `opus`) both resolve to `anthropic/claude-opus-5` through
   `DEFAULT_CANONICAL` / `DEFAULT_CANONICAL_BY_KIND`, so
   `Roster._check_independence` refuses the seating. The documented fallback
   (meta moves to `argo-sonnet5`) collides the same way with the `sonnet-cli`
   critic, which is `anthropic/claude-sonnet-5`. The profile therefore declares
   `allow_meta_overlap: true` — the engine's own flag for exactly this — and
   the block states both collisions and what removing them would cost. Both are
   asserted as measured facts in
   `TestTheAdjudicateSeatingResolves::test_the_meta_seat_collides_with_a_proposer_which_is_why_the_flag_is_set`
   and `test_the_documented_fallback_collides_with_the_critic_instead`.

A fifth, smaller one: `test_queries.py::TestRegistry::test_retire_hides_a_version_from_the_default_lookup`
hard-coded `len(registry.list()) == 4`. Changed to count off
`BUILTIN_QUERY_TYPES` so the test stays about the hiding rather than about the
number of shipped builtins.

## inferred rather than verified

- **h100's `~/.kbutillib/config.yaml` was NOT read.** The ADJUDICATE block's
  claim about which aliases that file already holds (`gpt-5.6-sol`,
  `argo-opus5`, `gemini35flash` present; `opus-cli` and `sonnet-cli` absent)
  comes from the task prompt, not from the file. The block says h100's own
  file is the authority on the three existing entries' `served_name` and
  `canonical` and tells the operator not to re-derive them from the block.
  `gpt-5.6-sol`'s shown `canonical: openai/gpt-5.6-sol` is the shape a codex
  alias outside the built-in table needs, not a value read off that machine.
- `ruff` and `mypy` are not installed in `/home/chenry/venvs/kbdl`, so the
  repo's lint and type checks were not run. Unused imports were checked with
  an AST pass instead; the only hits are `from __future__ import annotations`.

## no-touch confirmations

The council engine (`C/engine/`), the store schema, and every existing query
type's spec file are unmodified. `go_mf@4`'s digest is unmoved:
`test_go_mf_v4_snapshot_pin.py` (10 tests, including the pinned
`SNAPSHOT_SHA256`) passes unchanged. The packaged `gpt54-cli` / `gpt55-cli`
entries (operator item OP-2) are untouched, and the block tells an operator
not to reuse them.
