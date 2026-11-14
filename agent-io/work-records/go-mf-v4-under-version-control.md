# Work record: go_mf@4 under version control

- task_id: task-52094726
- branch: maestro/developer/the-query-spec-that-decided-the--task-52094726
- base: 98e9f55 (`main`)
- machine: h100 (poplar) -- the machine holding the live council store

## Summary

`go_mf@4` -- the query spec all 28 done jobs and 47,185 decided items were
produced under -- existed only inside the live store's `query_types` table.
It is now `src/kbutillib/domains/ai/council/queries/builtin/go_mf_v4.yaml`,
registered as a shipped builtin. `go_mf@3` is committed beside it
(`go_mf_v3.yaml`) because the two differ in exactly one substantive field, and
keeping both turns "the extraction is correct" from an assertion in this file
into a test in the suite.

Registering the committed specs back against the live store raised NO
exception for either version, which -- given that `put_query_type` refuses a
DIFFERENT spec under an existing `(name, version)` with `QueryTypeExists` -- is
the proof that the extraction is byte-equivalent to what the corpus was decided
under. Row count, `source`, `created_by` and `created_at` are unchanged in the
live store: the identical re-put returns before any INSERT.

This was NOT a pure data add. The builtin loader addressed its YAML by name
alone and `seed_builtins` discarded the version, so a second `("go_mf", "4")`
tuple would have read `go_mf.yaml` twice and stored `@1`'s prompts under `@4`'s
number. Lookup and seeding are now keyed on `(name, version)`.

## Where the row came from

Primary source -- the live store:

- file: `/scratch/chenry/council/council.sqlite` (1.57 GB, mtime 2026-11-02)
- opened as `sqlite:////scratch/chenry/council/council.sqlite`, schema_version 2
- table `query_types`, primary key `(name, version)`
- the row: `name='go_mf'`, `version='4'`, `status='active'`, `source='user'`,
  `created_by='chenry'`, `created_at='2026-10-20 17:43:46.472532+00:00'`,
  `length(spec)=9289` bytes of JSON

The table holds five rows in total: `go_mf` at `1`, `2`, `3`, `4` and
`reaction_pickup` at `1`, all `active`.

Second, independent source -- cross-checked, not preferred:

- `/scratch/chenry/council/go_mf_v4.yaml` (10,213 bytes, mtime 2026-10-20 12:43)
- **It is identical to the store row.** Parsed to a mapping and compared by
  canonical JSON (`sort_keys=True`), `yaml.safe_load(scratch_v4) ==
  json.loads(store_row)` is True -- every key, every prompt byte, every schema.
  There is no difference to record.
- Confront round 1 was right and `FINDINGS.md` was right about different
  directories: `/scratch/chenry/council/queries/` really does hold only
  `go_mf_v3.yaml`; `go_mf_v4.yaml` is one level up, in
  `/scratch/chenry/council/`. Both claims were true of the path each was
  looking at.

### How the committed file's layout was chosen

The layout is not hand-authored and not invented. A generator emitted the YAML
from the store's JSON, and its self-check was: regenerate the SHIPPED
`builtin/go_mf.yaml` from the store's `go_mf@1` row and compare bytes. That
reproduced the shipped file **byte-for-byte**, which is what establishes that
`go_mf_v3.yaml` and `go_mf_v4.yaml` follow the existing convention rather than
a new one:

- top-level key order `name, version, description, entity_field,
  evidence_fields, menu_fields, id_field, renderer, adapter,
  critic_truncate_chars, candidate_templates, roles, attribution`
- per-role key order `system, user, schema`; multi-line strings as `|`/`|-`
  block scalars; no line wrapping
- the pydantic default `fragments: {}` OMITTED (the shipped `go_mf.yaml` omits
  it too; `QueryType.to_spec()` puts it back on the round trip, which is why
  the store rows carry it and the files do not)

Filename convention: `(name, version) -> filename` is now an explicit map,
`BUILTIN_SPEC_FILES`, rather than a derivation rule. A rule would have needed a
special case on day one, because `go_mf.yaml` and `reaction_pickup.yaml` predate
versioned builtins and are named for the query type alone.

## The v3 -> v4 diff actually observed

The task predicted "exactly ONE field changed". **Three fields differ**, and
the extra two are both expected and self-evidencing; the prompt's own text
names the second of them ("Its own attribution records why"):

| path | `@3` | `@4` |
|---|---|---|
| `version` | `'3'` | `'4'` |
| `roles.meta.schema.additionalProperties` | `false` | `true` |
| `attribution` | V3 port provenance | the same text plus an appended `--- go_mf@4, 2026-10-20 ---` block |

Everything else is byte-identical: all six prompt strings, the proposer and
critic schemas (both still `additionalProperties: false`), `renderer`,
`adapter`, `id_field`, `entity_field`, `critic_truncate_chars`,
`candidate_templates`, `description`. `@4`'s `attribution` *startswith* `@3`'s,
so the change is purely an append.

As a diff of the two committed files, it is 3 hunks: line 2, line 188 (inside
`roles.meta.schema`) and a 19-line append at the end.

The appended attribution records the reason, in the run's own words: on a
500-item run under `@3`, 25 of 184 meta calls were rejected for "Additional
properties are not allowed" while carrying a complete answer -- 13.6% of the
meta seat, ~5% of all items -- and six different extra-key names appeared in
those 25, so enumerating them as optional properties would not have held. The
adapter reads only the four required meta fields, so an extra key is ignored
rather than interpreted: the scientific effect on the decision is none.

## Was v3 recoverable?

Yes. `/scratch/chenry/council/queries/go_mf_v3.yaml` (9,091 bytes, mtime
2026-10-19 00:01) still exists and is committed as
`builtin/go_mf_v3.yaml`.

It is semantically identical to the store's `go_mf@3` row. The only textual
difference is that the scratch file omits `fragments: {}` on each of the three
roles while the store row carries it -- the pydantic default added by
`to_spec()` when the spec was registered. That is a round-trip artefact, not an
edit: with the default filled in, the two compare equal under canonical JSON.
The committed file is generated from the store row in the shipped layout (and
so also omits `fragments`), which makes it textually consistent with
`go_mf.yaml` and semantically identical to both the scratch file and the live
row.

## Registration against the live store

Ran in-process against `sqlite:////scratch/chenry/council/council.sqlite`
with the committed specs loaded through `load_builtin_spec` and registered
through `QueryRegistry.register`:

```
store: sqlite:////scratch/chenry/council/council.sqlite schema_version: 2
rows before: [('go_mf','1','active'), ('go_mf','2','active'), ('go_mf','3','active'),
              ('go_mf','4','active'), ('reaction_pickup','1','active')]
go_mf@3: registered with NO exception -> byte-equivalent to the live row
go_mf@4: registered with NO exception -> byte-equivalent to the live row
row count unchanged: True
```

**No `QueryTypeExists` was raised.** `put_query_type` compares the canonical
form of the incoming spec against the stored one and `return`s before the
INSERT when they match, so this verifies the extraction without writing: after
the run, `go_mf@4` still reads `source='user'`, `created_by='chenry'`,
`created_at='2026-10-20 17:43:46.472532+00:00'` and `go_mf@3` still reads
`source='directory'`, `created_by='chenry'`,
`created_at='2026-10-19 05:01:15.986050+00:00'` -- the original rows, untouched.
Nothing was deployed, migrated or restarted; the only live-store contact was
this one read-compare.

## Version-aware lookup and seeding (the code change)

`src/kbutillib/domains/ai/council/queries/builtin/__init__.py`:

- `BUILTIN_QUERY_TYPES` is now `(("go_mf","1"), ("go_mf","3"), ("go_mf","4"),
  ("reaction_pickup","1"))`. `@2` is deliberately absent -- it was an
  intermediate nothing was decided under.
- `BUILTIN_SPEC_FILES: Dict[Tuple[str,str], str]` maps each `(name, version)`
  to its file. This is the fix: the old code read `f"{name}.yaml"`.
- `load_builtin_spec(name, version=None)`. A version-less load is permitted
  only where the name ships exactly one version (`reaction_pickup`); for
  `go_mf` it RAISES `UnknownRenderer` rather than resolving to the newest. The
  three versions are different prompts and different answer schemas, so
  guessing would hand a caller a different question under a name it thought it
  had pinned -- the same reasoning the module already applies to renderer
  versions.
- After loading, the file's own `name`/`version` are checked against the key it
  was filed under. A wrong map entry would otherwise be invisible: the spec
  would be stored under the key asked for while carrying another version's
  prompts, and every prompt hash taken from it would be wrong.
- `builtin_versions(name)` added; `builtin_spec_paths()` resolves through the
  map.

`registry.py`: `seed_builtins` passes the version
(`load_builtin_spec(name, version)`) instead of discarding it.

### Behaviour change callers must know about

`QueryRegistry.get(name)` with no version returns the latest ACTIVE version.
Seeding three `go_mf` versions therefore moves a version-less lookup: in a
freshly seeded store `get("go_mf")` is now `@4`, where it used to be `@1`.
This is asserted explicitly in
`tests/domains/ai/council/test_go_mf_v4_builtin.py` so it cannot drift
silently, and every in-repo call site that meant `@1` now says so:

- `src/kbutillib/domains/ai/council/queries/__init__.py` (docstring example)
- `scripts/council/replay_go_v3.py` (3 sites -- it replays against `@1`'s
  reference answers)
- the test sites listed below

`DEFAULT_COUNCIL_QUERY = "go_mf@1"` in `ontology_mapping_utils.py` and the
`--query` CLI defaults are UNCHANGED: they are explicit, so they were never
affected, and moving them is not this task's call.

## files_touched

New:
- `src/kbutillib/domains/ai/council/queries/builtin/go_mf_v4.yaml` -- the
  extracted spec (10,019 bytes).
- `src/kbutillib/domains/ai/council/queries/builtin/go_mf_v3.yaml` -- its
  parent (8,988 bytes).
- `tests/domains/ai/council/test_go_mf_v4_builtin.py` -- 16 tests.

Changed:
- `src/kbutillib/domains/ai/council/queries/builtin/__init__.py` --
  `BUILTIN_SPEC_FILES`, version-aware `load_builtin_spec`,
  `builtin_versions`, `builtin_spec_paths`, the `BUILTIN_QUERY_TYPES` entries.
- `src/kbutillib/domains/ai/council/queries/registry.py:111` --
  `seed_builtins` passes the version.
- `src/kbutillib/domains/ai/council/queries/__init__.py` -- module docstring.
- `pyproject.toml:266` -- the package-data comment (the existing
  `builtin/*.yaml` glob already ships the new files; no glob change needed).
- `scripts/council/replay_go_v3.py` -- pinned `get("go_mf", "1")` at 3 sites.
- `tests/domains/ai/council/test_queries.py` -- pinned `@1` where the test
  means `@1`; loops over `BUILTIN_QUERY_TYPES` load by `(name, version)`; the
  seeded-set assertions name four entries; the "new version beside the old"
  and "retire" tests use `@5` so the newest-wins assertion is still about the
  version they register; `go_mf_v3.yaml`/`go_mf_v4.yaml` added to the
  `querytype.schema.json` validation list.
- `tests/domains/ai/council/test_council_utils.py:469`,
  `tests/cli/test_council_cli.py:274,530` -- the `db init` / `query list`
  expectations.
- `tests/domains/ai/council/test_engine.py:1560,1642`,
  `tests/domains/ai/council/test_go_replay.py:370` -- `get("go_mf", "1")`;
  both modules' own docstrings already said `go_mf@1`.

## success_criteria_check

1. *A `go_mf` version '4' query spec is committed under the repository's
   existing built-in query convention.* Yes -- `builtin/go_mf_v4.yaml`, in a
   layout proven against the shipped `go_mf.yaml` byte-for-byte, listed in
   `BUILTIN_QUERY_TYPES` and `BUILTIN_SPEC_FILES`, shipped by the existing
   package-data glob.
2. *A test asserts it loads through `validate_spec` with meta
   `additionalProperties` true and proposer/critic false.*
   `TestGoMfV4Loads::test_it_loads_through_validate_spec` and
   `::test_the_meta_schema_allows_extra_keys_and_the_others_do_not`. A third
   test runs a real `jsonschema` validator over a meta answer carrying an extra
   key: it passes under `@4` and raises under `@3`.
3. *The work record records the observed v3->v4 diff and the live-store
   registration result, including a `QueryTypeExists` outcome if one occurred.*
   Above. No `QueryTypeExists` occurred.
4. *No test that passed on the base commit fails.* See `tests_run`.
5. *The work record records a diff of the extracted spec against
   `/scratch/chenry/council/go_mf_v4.yaml`, or states plainly that the file was
   absent.* The file was present and is identical to the store row; there is no
   diff.
6. *Builtin lookup and seeding are version-aware, and a regression test asserts
   `go_mf@1` still registers unchanged alongside `go_mf@4`.*
   `TestSeedingIsVersionAware::test_go_mf_1_still_registers_unchanged_alongside_go_mf_4`
   asserts every seeded row is byte-equal to `to_spec()` of its OWN file, and
   that `@1`'s stored meta schema is still `additionalProperties: false` while
   `@4`'s is `true` -- which is exactly what the name-only loader would have got
   wrong.

## tests_run

Interpreter `/home/chenry/venvs/kbdl/bin/python` (CPython 3.11.15, pytest
9.1.1), from the worktree with `TMPDIR=/scratch/chenry/tmp
PYTHONPATH=$PWD/src`.

Baseline: `build-baselines/kbutillib-h100-main-6310d61.json` records the full
suite at 2,665-3,416 s -- 4.4x to 5.7x the 600 s sub-agent Bash ceiling -- and
a worktree-vs-main-clone trap that fabricates 5 phantom regressions in
`tests/core/test_composition_smoke.py`. So this task ran TARGETED selections,
as that file instructs, and compared by node id.

**The sweep, on this branch** (exit code 0):

```
python -m pytest tests/domains/ai/ tests/cli/ tests/berdl/ \
    tests/domains/test_ontomap_pipeline_rehearsal.py -q -p no:cacheprovider --no-header
27 failed, 4195 passed, 85 skipped, 4 warnings in 702.94s
```

**The same 27 node ids at the base commit** -- `git worktree add --detach
/scratch/chenry/t52094726-base 98e9f55`, same interpreter, same flags:

```
python -m pytest tests/cli/test_kind.py tests/cli/test_kind_install_containment.py \
    tests/cli/test_model_recording.py tests/domains/ai/llm/test_static_headers.py \
    tests/domains/ai/council/test_types_config.py -q -p no:cacheprovider --no-header
27 failed, 188 passed, 2 warnings in 52.89s
```

**ZERO REGRESSIONS.** The failing set is node-id-IDENTICAL between base and
branch, compared by name and not by count:

- 6 x `test_types_config.py::TestPackagedDefaultConfig::*` and
  1 x `test_static_headers.py::TestPackagedExample::test_the_default_config_declares_no_mag_endpoint`
  -- the machine-local config leak the baseline file documents (the h100
  `~/.kbutillib/config.yaml` is read past a monkeypatched `HOME`, bringing in
  the `mag` endpoint and the `smoke` profile). Present at both commits the
  baseline measures, and explicitly not a council task's to fix.
- 18 x `tests/cli/test_kind*.py` and 2 x `tests/cli/test_model_recording.py`
  -- the KIND install/status surface and the model-recording verbs. Nothing in
  this branch touches either; reproduced at base.

Nothing in `tests/domains/ai/council/`, `tests/berdl/`, `tests/cli/test_council_cli.py`
or the ontomap rehearsal fails on this branch.

Targeted runs during development:

- `tests/domains/ai/council/test_go_mf_v4_builtin.py` -- 16 passed.
- `tests/domains/ai/council/test_queries.py` -- 51 passed.
- `tests/domains/ai/council/{test_engine,test_go_replay}.py` -- 130 passed.
- `tests/cli/test_council_cli.py tests/cli/test_ontomap_cli.py
  tests/domains/test_ontomap_pipeline_rehearsal.py` -- 169 passed, 3 skipped.

Every test edited in this branch was first observed FAILING against the new
seeding and then passing -- including the two in `tests/cli/test_council_cli.py`,
which were re-checked against `git show HEAD:` of that file to confirm the
edits were responses to real failures rather than pre-emptive. Each edit pins a
version the test's own docstring already named, or extends an expected set that
legitimately grew by two builtins. No test was weakened, skipped or deleted.

## Divergences between the task prompt and the tree

1. **"exactly ONE field changed"** -- three differ (`version`, `attribution`,
   `roles.meta.schema.additionalProperties`). The substantive one is the
   `additionalProperties` relax, as the prompt says; the prompt's own next
   sentence depends on the attribution having changed too. Recorded rather than
   forced.
2. **"`/scratch/chenry/council/queries` holds only `go_mf_v3.yaml`"** -- true of
   that directory. `go_mf_v4.yaml` is in the PARENT, `/scratch/chenry/council/`.
   Both the prompt's claim and the confront round's correction are accurate
   about different paths.
3. **"following WHATEVER naming and layout the existing registry already uses
   for a versioned built-in"** -- there was no existing versioned builtin: both
   shipped YAMLs are `@1` and are named for the query type alone. Rather than
   invent a derivation rule (which would have needed a `version == "1"` special
   case immediately), the `(name, version) -> filename` mapping is explicit
   data, and the file layout was pinned by regenerating the shipped
   `go_mf.yaml` byte-for-byte.
4. **"This is the one task in the plan whose failure does not block the api
   type"** -- it did not fail; the store was reachable and nothing was
   reconstructed.
5. Committing `go_mf@3` as a seeded builtin goes slightly past "also commit
   `go_mf_v3.yaml`": it is in `BUILTIN_QUERY_TYPES`, so it is registered by
   `seed_builtins` and covered by the shipped-spec tests. A YAML sitting in the
   package directory outside the map would have been shipped but unvalidated
   and unreachable, and it is what makes the one-field diff testable.

## caveats

- The byte-equivalence verdict is only as good as the live store's own row. If
  the corpus was decided under a spec that was later edited in place, nothing
  here would detect it -- but `put_query_type` forbids exactly that, and the
  row's `created_at` (2026-10-20 17:43) sits before the campaign it was used
  for.
- `go_mf@2` is in the live store and is NOT committed. It was an intermediate;
  if some artefact turns out to cite `go_mf@2`, this branch did not rescue it.
- The live-store registration was run once, interactively, by this task. It is
  not in the test suite and cannot be -- a test must not reach
  `/scratch/chenry/council/council.sqlite`. The committed evidence is the
  transcript above plus the `@3`/`@4` diff test, which fails if either file
  drifts.
